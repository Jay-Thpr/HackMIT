import asyncio
import io
import json
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advanced import cli, manifests, runtime, store as store_mod, worker  # noqa: E402
from advanced import relay as relay_mod  # noqa: E402
from advanced import sql  # noqa: E402


def find(objects, kind, name=None):
    return [o for o in objects if o["kind"] == kind and (name is None or o["metadata"]["name"] == name)]


class _AC:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        self.conn.exits.append(exc_type)
        return False


class FakeConn:
    def __init__(self):
        self.calls = []
        self.exits = []
        self.rows = {}
        self.vals = {}
        self.closed = False

    def acquire(self, *a, **k):
        return _AC(self)

    def transaction(self):
        return _AC(self)

    def is_closing(self):
        return False

    async def close(self):
        self.closed = True

    async def execute(self, query, *args, **k):
        self.calls.append(("execute", query, args))

    async def fetch(self, query, *args, **k):
        self.calls.append(("fetch", query, args))
        return list(self.rows.get(query, []))

    async def fetchrow(self, query, *args, **k):
        self.calls.append(("fetchrow", query, args))
        return self.rows.get(query)

    async def fetchval(self, query, *args, **k):
        self.calls.append(("fetchval", query, args))
        return self.vals.get(query)


def make_store(conn):
    s = store_mod.CommerceStore(["d0", "d1", "d2"], ["r0", "r1", "r2"])
    s.primaries = [conn, conn, conn]
    s.replicas = [conn, conn, conn]
    return s


class Redis:
    def __init__(self, get=None, ttl=60000):
        self._get = get or (lambda key: None)
        self._ttl = ttl
        self.gets = []

    async def get(self, key):
        self.gets.append(key)
        return self._get(key)

    async def pttl(self, key):
        return self._ttl


class Stats:
    def __init__(self):
        self.counters = {}
        self.hists = {}

    def inc(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n

    def observe(self, name, ms):
        self.hists.setdefault(name, []).append(ms)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.objects = manifests.render("faultline-advanced")

    def test_namespace_guard(self):
        for bad in ("default", "kube-system", "faultline-advanced-x",
                    "faultline-advanced-clone-zzzzzzzzz", "faultline-advanced-clone-ABCD1234"):
            with self.assertRaises(ValueError, msg=bad):
                manifests.render(bad)
        manifests.render("faultline-advanced-clone-a1b2c3d4")

    def test_every_object_namespaced_and_labelled(self):
        for obj in self.objects:
            self.assertEqual(obj["metadata"]["labels"]["faultline.dev/managed-by"],
                             "distributed-demo")
            if obj["kind"] == "Namespace":
                self.assertNotIn("namespace", obj["metadata"])
            else:
                self.assertEqual(obj["metadata"]["namespace"], "faultline-advanced",
                                 msg=obj["kind"] + "/" + obj["metadata"]["name"])

    def test_namespace_quota_policies_serviceaccount(self):
        ns = find(self.objects, "Namespace")[0]
        self.assertEqual(ns["metadata"]["labels"]["faultline.dev/managed-by"], "distributed-demo")
        quota = find(self.objects, "ResourceQuota")[0]
        self.assertEqual(quota["spec"]["hard"]["pods"], "40")
        self.assertEqual(quota["spec"]["hard"]["requests.memory"], "10Gi")
        policies = find(self.objects, "NetworkPolicy")
        names = {p["metadata"]["name"] for p in policies}
        self.assertEqual(names, {"default-deny", "allow-same-namespace", "allow-dns",
                                 "allow-control-apiserver"})
        deny = next(p for p in policies if p["metadata"]["name"] == "default-deny")
        self.assertNotIn("ingress", deny["spec"])
        dns = next(p for p in policies if p["metadata"]["name"] == "allow-dns")
        self.assertEqual(dns["spec"]["egress"][0]["ports"], [{"port": 53, "protocol": "TCP"},
                                                             {"port": 53, "protocol": "UDP"}])
        sa = find(self.objects, "ServiceAccount")[0]
        for obj in self.objects:
            template = obj.get("spec", {}).get("template")
            if isinstance(template, dict):
                self.assertEqual(
                    template["metadata"]["labels"].get("faultline.dev/managed-by"),
                    "distributed-demo", obj["metadata"]["name"])
        self.assertEqual(sa["automountServiceAccountToken"], False)

    def test_six_postgres_pods_with_real_streaming_replica(self):
        primaries = find(self.objects, "StatefulSet", "shard-0-primary") \
            + find(self.objects, "StatefulSet", "shard-1-primary") \
            + find(self.objects, "StatefulSet", "shard-2-primary")
        self.assertEqual(len(primaries), 3)
        args = primaries[0]["spec"]["template"]["spec"]["containers"][0]["args"]
        for flag in ("wal_level=replica", "max_wal_senders=10", "hot_standby=on",
                     "synchronous_commit=on"):
            self.assertIn(flag, args)
        replicas = [find(self.objects, "StatefulSet", f"shard-{i}-replica")[0] for i in range(3)]
        self.assertEqual(len(replicas), 3)
        for i, sts in enumerate(replicas):
            init = sts["spec"]["template"]["spec"]["initContainers"][0]
            cmd = " ".join(init["command"])
            self.assertIn(f"pg_basebackup -h shard-{i}-primary -U replicator", cmd)
            self.assertIn("PG_VERSION", cmd)
            self.assertIn("-ge 120", cmd)
            self.assertNotIn("rm -", cmd)
            env_names = {e["name"] for e in init["env"]}
            self.assertIn("PGPASSWORD", env_names)
            ready = " ".join(sts["spec"]["template"]["spec"]["containers"][0]
                             ["readinessProbe"]["exec"]["command"])
            self.assertIn("pg_is_in_recovery", ready)
            self.assertIn("= t ]", ready)
        for sts in primaries + replicas:
            env_names = {e["name"] for e in sts["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertIn("REPLICATION_PASSWORD", env_names)
            self.assertIn("POSTGRES_PASSWORD", env_names)
            labels = sts["spec"]["template"]["metadata"]["labels"]
            self.assertTrue(labels["shard"].startswith("shard-"))
            affinity = sts["spec"]["template"]["spec"]["affinity"]["podAntiAffinity"]
            selector = affinity["preferredDuringSchedulingIgnoredDuringExecution"][0] \
                ["podAffinityTerm"]["labelSelector"]["matchLabels"]
            self.assertEqual(selector, {"shard": labels["shard"]})

    def test_init_script_expands_repl_pass(self):
        cm = find(self.objects, "ConfigMap", "advanced-init")[0]
        script = cm["data"]["01-init-replication.sh"]
        self.assertIn("-v repl_pass=\"$REPLICATION_PASSWORD\" <<'SQL'", script)
        self.assertIn(":'repl_pass'", script)
        self.assertIn("pg_hba.conf", script)

    def test_kafka_three_brokers_rf3_minisr2_topic6(self):
        sts = find(self.objects, "StatefulSet", "kafka")[0]
        self.assertEqual(sts["spec"]["replicas"], 3)
        self.assertEqual(sts["spec"]["podManagementPolicy"], "Parallel")
        env = {e["name"]: e.get("value") for e in sts["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertIn("0@kafka-0.kafka:9093", env["KAFKA_CONTROLLER_QUORUM_VOTERS"])
        self.assertIn("2@kafka-2.kafka:9093", env["KAFKA_CONTROLLER_QUORUM_VOTERS"])
        self.assertEqual(env["KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR"], "3")
        self.assertEqual(env["KAFKA_MIN_INSYNC_REPLICAS"], "2")
        self.assertEqual(env["KAFKA_INTER_BROKER_LISTENER_NAME"], "PLAINTEXT")
        self.assertEqual(env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"], "false")
        self.assertNotIn("KAFKA_CLUSTER_ID", env)
        self.assertNotIn("KAFKA_OFFSETS_TOPIC_MIN_ISR", env)
        cmd = " ".join(sts["spec"]["template"]["spec"]["containers"][0]["command"])
        self.assertIn("/etc/kafka/docker/run", cmd)
        job = find(self.objects, "Job", "kafka-topic-init")[0]
        container = job["spec"]["template"]["spec"]["containers"][0]
        cmd = " ".join(container["command"])
        self.assertIn("/opt/kafka/bin/kafka-topics.sh", cmd)
        self.assertIn("--topic orders --partitions 6 --replication-factor 3", cmd)
        self.assertIn("min.insync.replicas=2", cmd)
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertEqual(env["KAFKA_HEAP_OPTS"], "-Xms64m -Xmx128m")
        self.assertEqual(container["resources"]["requests"], {"cpu": "100m", "memory": "128Mi"})
        service = find(self.objects, "Service", "kafka")[0]
        self.assertEqual(service["spec"]["clusterIP"], "None")

    def test_cluster_id_is_namespace_derived(self):
        a = find(self.objects, "StatefulSet", "kafka")[0]
        env = {e["name"]: e.get("value") for e in a["spec"]["template"]["spec"]["containers"][0]["env"]}
        b = find(manifests.render("faultline-advanced-clone-a1b2c3d4"), "StatefulSet", "kafka")[0]
        env_b = {e["name"]: e.get("value") for e in b["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertNotEqual(env["CLUSTER_ID"], env_b["CLUSTER_ID"])

    def test_redis_auth_and_exec(self):
        sts = find(self.objects, "StatefulSet", "redis")[0]
        container = sts["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("exec redis-server", " ".join(container["command"]))
        env_names = {e["name"] for e in container["env"]}
        self.assertIn("REDISCLI_AUTH", env_names)
        ready = " ".join(container["readinessProbe"]["exec"]["command"])
        self.assertNotIn(" -a ", ready)
        self.assertIn("redis-cli ping", ready)

    def test_app_deployments(self):
        api = find(self.objects, "Deployment", "api")[0]
        self.assertEqual(api["spec"]["replicas"], 2)
        relay = find(self.objects, "Deployment", "relay")[0]
        self.assertEqual(relay["spec"]["strategy"]["type"], "Recreate")
        for i in range(3):
            w = find(self.objects, "Deployment", f"worker-{i}")[0]
            self.assertEqual(w["spec"]["replicas"], 1)
            env = {e["name"]: e.get("value") for e in
                   w["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(env["ROLE"], "worker")
        loadgen = find(self.objects, "Deployment", "loadgen")[0]
        self.assertEqual(loadgen["spec"]["replicas"], 1)

    def test_no_plaintext_secrets(self):
        self.assertEqual(find(self.objects, "Secret"), [])
        text = json.dumps(self.objects)
        for value in ("stringData", "password:", "CHANGE_ME"):
            self.assertNotIn(value, text)
        for key in ("postgres-password", "replication-password", "redis-password", "control-token"):
            self.assertIn(key, text)

    def test_kind_config_three_nodes_calico(self):
        config = manifests.kind_config()
        self.assertTrue(config["networking"]["disableDefaultCNI"])
        self.assertEqual(config["networking"]["podSubnet"], "192.168.0.0/16")
        self.assertEqual(len(config["nodes"]), 3)
        self.assertEqual(config["nodes"][0]["role"], "control-plane")
        self.assertTrue(all(n["image"].startswith("kindest/node:v1.32.2@sha256:") for n in config["nodes"]))


class RuntimeTest(unittest.TestCase):
    def test_shard_assignment_stable_and_bounded(self):
        for tenant in ("tenant-a", "tenant-b", "tenant-c", "tenant-d", "tenant-e", "tenant-f"):
            shard = runtime.shard_for(tenant)
            self.assertIn(shard, (0, 1, 2))
            self.assertEqual(shard, runtime.shard_for(tenant))
        self.assertEqual(
            runtime.shard_for("x"),
            int.from_bytes(__import__("hashlib").sha256(b"x").digest()[:8], "big") % 3)

    def test_effective_and_bench_keys(self):
        redis = Redis(get=lambda key: b'{"value": 7}' if key.startswith("control:") else None)
        self.assertEqual(asyncio.run(runtime.effective(redis, "consumer_backoff", "0", 100)), 7)
        self.assertEqual(redis.gets, ["control:consumer_backoff:0"])
        self.assertIsNone(asyncio.run(runtime.bench_override(redis, "worker_delay", "0")))
        self.assertEqual(redis.gets[-1], "bench:worker_delay:0")

    def test_persistent_or_expired_overrides_fall_back(self):
        for ttl, expect in ((-1, 100), (-2, 100), (0, 100), (60000, 7)):
            redis = Redis(get=lambda key: b"7", ttl=ttl)
            self.assertEqual(
                asyncio.run(runtime.effective(redis, "consumer_backoff", "0", 100)), expect)
        redis = Redis(get=lambda key: b"7", ttl=60000)
        self.assertEqual(asyncio.run(runtime.effective(redis, "b", "s", 5)), 7)

    def test_finite_positive(self):
        for good in (1, 0.5, "2", 100):
            self.assertTrue(runtime.finite_positive(good))
        for bad in (0, -1, float("nan"), float("inf"), None, "x", True):
            self.assertFalse(runtime.finite_positive(bad))


class StoreTest(unittest.TestCase):
    def test_accept_is_atomic_order_and_outbox(self):
        conn = FakeConn()
        conn.vals[sql.NEXT_SEQUENCE] = 7
        conn.vals[sql.INSERT_ORDER] = datetime(2026, 1, 1, tzinfo=timezone.utc)
        s = make_store(conn)
        order_id = uuid.uuid4()
        result = asyncio.run(s.accept("a", order_id, 500))
        self.assertEqual(result["sequence"], 7)
        queries = [q for _, q, _ in conn.calls]
        self.assertEqual(queries, [sql.LOCK_TENANT, sql.ENSURE_TENANT, sql.EXISTING_ORDER,
                                   sql.NEXT_SEQUENCE, sql.INSERT_ORDER, sql.INSERT_OUTBOX])
        self.assertEqual(len(conn.exits), 2)

    def test_accept_idempotent_and_conflict(self):
        tenant = "tenant-a"
        order_id = uuid.uuid4()
        existing = {"tenant_id": tenant, "order_id": order_id, "sequence": 3,
                    "amount_cents": 500, "accepted_at": None, "fulfilled_at": None}
        conn = FakeConn()
        conn.rows[sql.EXISTING_ORDER] = existing
        result = asyncio.run(make_store(conn).accept(tenant, order_id, 500))
        self.assertEqual(result["sequence"], 3)
        self.assertNotIn(sql.INSERT_ORDER, [q for _, q, _ in conn.calls])
        conn2 = FakeConn()
        conn2.rows[sql.EXISTING_ORDER] = existing
        with self.assertRaises(store_mod.ConflictError):
            asyncio.run(make_store(conn2).accept(tenant, order_id, 999))

    def test_fulfill_dedup_and_gap(self):
        tenant = "tenant-a"
        order_id = uuid.uuid4()
        event = {"tenant_id": tenant, "order_id": str(order_id), "sequence": 4, "amount_cents": 500}
        order = {"tenant_id": tenant, "order_id": order_id, "sequence": 4,
                 "amount_cents": 500, "accepted_at": None, "fulfilled_at": None}
        conn = FakeConn()
        conn.rows[sql.EXISTING_ORDER] = order
        conn.rows[sql.EXISTING_PAYMENT] = {"sequence": 4, "amount_cents": 500}
        self.assertFalse(asyncio.run(make_store(conn).fulfill(event)))
        self.assertNotIn(sql.INSERT_PAYMENT, [q for _, q, _ in conn.calls])
        conn2 = FakeConn()
        conn2.rows[sql.EXISTING_ORDER] = order
        conn2.vals[sql.LOCK_PROGRESS] = 1
        with self.assertRaises(store_mod.SequenceGap):
            asyncio.run(make_store(conn2).fulfill(event))
        self.assertNotIn(sql.INSERT_PAYMENT, [q for _, q, _ in conn2.calls])
        conn3 = FakeConn()
        conn3.rows[sql.EXISTING_ORDER] = order
        conn3.vals[sql.LOCK_PROGRESS] = 3
        self.assertTrue(asyncio.run(make_store(conn3).fulfill(event)))
        queries = [q for _, q, _ in conn3.calls]
        self.assertEqual(queries[-3:], [sql.INSERT_PAYMENT, sql.ADVANCE_PROGRESS, sql.FULFILL_ORDER])

    def test_mismatched_existing_payment_is_not_dedup(self):
        tenant = "tenant-a"
        order_id = uuid.uuid4()
        event = {"tenant_id": tenant, "order_id": str(order_id), "sequence": 4, "amount_cents": 500}
        order = {"tenant_id": tenant, "order_id": order_id, "sequence": 4,
                 "amount_cents": 500, "accepted_at": None, "fulfilled_at": None}
        conn = FakeConn()
        conn.rows[sql.EXISTING_ORDER] = order
        conn.rows[sql.EXISTING_PAYMENT] = {"sequence": 4, "amount_cents": 999}
        with self.assertRaises(store_mod.ConflictError):
            asyncio.run(make_store(conn).fulfill(event))
        conn2 = FakeConn()
        conn2.rows[sql.EXISTING_ORDER] = order
        conn2.rows[sql.EXISTING_PAYMENT] = {"sequence": 9, "amount_cents": 500}
        with self.assertRaises(store_mod.ConflictError):
            asyncio.run(make_store(conn2).fulfill(event))

    def test_get_order_stale_primary_and_unavailable(self):
        tenant = "tenant-a"
        order_id = uuid.uuid4()
        stale = {"tenant_id": tenant, "order_id": order_id, "sequence": 2,
                 "amount_cents": 500, "accepted_at": None, "fulfilled_at": None}
        conn = FakeConn()
        conn.rows[sql.EXISTING_ORDER] = stale
        s = make_store(conn)
        self.assertIsNone(asyncio.run(s.get_order(tenant, order_id, min_sequence=5)))
        self.assertEqual(s.get_order and asyncio.run(
            s.get_order(tenant, order_id, min_sequence=2))["sequence"], 2)

        class DownConn(FakeConn):
            async def fetchrow(self, query, *args, **k):
                raise OSError("connection refused")

        s2 = make_store(DownConn())
        with self.assertRaises(store_mod.StoreUnavailable):
            asyncio.run(s2.get_order(tenant, order_id))
        s3 = make_store(DownConn())
        with self.assertRaises(store_mod.StoreUnavailable):
            asyncio.run(s3.catalog(tenant, 1))

    def test_partial_connect_closes_opened_pools(self):
        created = []

        class Pool(FakeConn):
            pass

        async def fake_pool(dsn, **k):
            pool = Pool()
            created.append(pool)
            if dsn == "d2":
                raise OSError("refused")
            return pool

        s = store_mod.CommerceStore(["d0", "d1", "d2"], ["r0", "r1", "r2"])
        with patch("asyncpg.create_pool", fake_pool):
            with self.assertRaises(OSError):
                asyncio.run(s.connect())
        self.assertTrue(all(p.closed for p in created[:2]))
        self.assertEqual(s.primaries, [])


class RelayTest(unittest.TestCase):
    def test_publish_before_mark(self):
        tenant = "tenant-a"
        order_id = uuid.uuid4()
        row = {"tenant_id": tenant, "order_id": order_id, "sequence": 1, "amount_cents": 500,
               "accepted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        events = []

        class RecordingConn(FakeConn):
            async def execute(self, query, *args, **k):
                await super().execute(query, *args, **k)
                if query == sql.MARK_PUBLISHED:
                    events.append("mark")

        conn = RecordingConn()
        conn.rows[sql.PENDING_OUTBOX] = [row]
        s = make_store(conn)
        stop = asyncio.Event()

        class Producer:
            async def send_and_wait(self, topic, payload, key=None):
                events.append("send")
                stop.set()

        asyncio.run(relay_mod.relay_shard(s, Producer(), runtime.shard_for(tenant), stop))
        self.assertEqual(events, ["send", "mark"])

    def test_mark_absent_on_broker_failure(self):
        tenant = "tenant-a"
        order_id = uuid.uuid4()
        row = {"tenant_id": tenant, "order_id": order_id, "sequence": 1, "amount_cents": 500,
               "accepted_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        conn = FakeConn()
        conn.rows[sql.PENDING_OUTBOX] = [row]
        s = make_store(conn)
        stop = asyncio.Event()

        class FailingProducer:
            def __init__(self):
                self.calls = 0

            async def send_and_wait(self, topic, payload, key=None):
                self.calls += 1
                stop.set()
                raise RuntimeError("broker down")

        producer = FailingProducer()
        asyncio.run(relay_mod.relay_shard(s, producer, runtime.shard_for(tenant), stop))
        self.assertNotIn(sql.MARK_PUBLISHED, [q for _, q, _ in conn.calls])


class Msg:
    def __init__(self, event, partition=2, offset=41):
        self.topic = "orders"
        self.partition = partition
        self.offset = offset
        self.value = json.dumps(event) if isinstance(event, dict) else event


def good_event():
    return {"tenant_id": "tenant-a", "order_id": str(uuid.uuid4()), "sequence": 1,
            "amount_cents": 500,
            "accepted_at": datetime.now(timezone.utc).isoformat()}


class WorkerTest(unittest.TestCase):
    def test_parse_event_bounds(self):
        self.assertEqual(worker.parse_event(json.dumps(good_event()))["sequence"], 1)
        for bad in (json.dumps({**good_event(), "sequence": True}),
                    json.dumps({**good_event(), "sequence": 0}),
                    json.dumps({**good_event(), "amount_cents": -5}),
                    json.dumps({**good_event(), "order_id": "nope"}),
                    json.dumps({**good_event(), "tenant_id": "BAD TENANT"}),
                    "x" * (worker.MAX_EVENT_BYTES + 1),
                    "not json"):
            with self.assertRaises(ValueError, msg=str(bad)[:40]):
                worker.parse_event(bad)

    def test_commit_after_fulfill_and_retry_keeps_offset(self):
        class Consumer:
            def __init__(self):
                self.commits = []
                self.sent = False

            async def getone(self):
                if self.sent:
                    await asyncio.sleep(60)
                self.sent = True
                return Msg(good_event())

            async def commit(self, offsets):
                self.commits.append(offsets)

        class Store:
            def __init__(self):
                self.fulfilled = 0

            async def fulfill(self, e):
                self.fulfilled += 1
                if self.fulfilled == 1:
                    raise RuntimeError("db down")
                return True

        store, consumer = Store(), Consumer()
        stop = asyncio.Event()
        stats = Stats()

        async def go():
            task = asyncio.create_task(worker.consume(
                store, consumer, Redis(get=lambda k: b"1" if k.startswith("control:") else None),
                stats, stop))
            await asyncio.sleep(0.3)
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(worker, "CPU_WORK_MS", 0):
            asyncio.run(go())
        self.assertEqual(store.fulfilled, 2)
        tp = list(consumer.commits[0].keys())[0]
        self.assertEqual(consumer.commits[0][tp], 42)
        self.assertEqual(stats.counters.get("completed"), 1)

    def test_duplicates_counted_not_completed(self):
        class Consumer:
            def __init__(self):
                self.commits = []
                self.sent = False

            async def getone(self):
                if self.sent:
                    await asyncio.sleep(60)
                self.sent = True
                return Msg(good_event())

            async def commit(self, offsets):
                self.commits.append(offsets)

        class Store:
            async def fulfill(self, e):
                return False

        stop = asyncio.Event()
        stats = Stats()

        async def go():
            task = asyncio.create_task(
                worker.consume(Store(), Consumer(), Redis(), stats, stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(worker, "CPU_WORK_MS", 0):
            asyncio.run(go())
        self.assertEqual(stats.counters.get("duplicates"), 1)
        self.assertIsNone(stats.counters.get("completed"))

    def test_lost_assignment_abandons_without_commit(self):
        from aiokafka import TopicPartition

        class Consumer:
            def __init__(self):
                self.commits = []
                self.sent = False

            async def getone(self):
                if self.sent:
                    await asyncio.sleep(60)
                self.sent = True
                return Msg(good_event(), partition=2)

            def assignment(self):
                return {TopicPartition("orders", 0)}

            async def commit(self, offsets):
                self.commits.append(offsets)

        class Store:
            def __init__(self):
                self.fulfilled = 0

            async def fulfill(self, e):
                self.fulfilled += 1
                return True

        store, consumer = Store(), Consumer()
        stop = asyncio.Event()
        stats = Stats()

        async def go():
            task = asyncio.create_task(
                worker.consume(store, consumer, Redis(), stats, stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(worker, "CPU_WORK_MS", 0):
            asyncio.run(go())
        self.assertEqual(store.fulfilled, 0)
        self.assertEqual(consumer.commits, [])

    def test_commit_failure_returns_to_fetch(self):
        from aiokafka.errors import CommitFailedError

        class Consumer:
            def __init__(self):
                self.commits = 0
                self.sent = False

            async def getone(self):
                if self.sent:
                    await asyncio.sleep(60)
                self.sent = True
                return Msg(good_event())

            async def commit(self, offsets):
                self.commits += 1
                raise CommitFailedError("revoked")

        class Store:
            def __init__(self):
                self.fulfilled = 0

            async def fulfill(self, e):
                self.fulfilled += 1
                return True

        store, consumer = Store(), Consumer()
        stop = asyncio.Event()
        stats = Stats()

        async def go():
            task = asyncio.create_task(
                worker.consume(store, consumer, Redis(), stats, stop))
            await asyncio.sleep(0.3)
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(worker, "CPU_WORK_MS", 0):
            asyncio.run(go())
        self.assertEqual(store.fulfilled, 1)
        self.assertEqual(consumer.commits, 1)
        self.assertEqual(stats.counters.get("completed"), 1)
        self.assertEqual(stats.counters.get("errors"), 1)

    def test_malformed_event_does_not_advance(self):
        class Consumer:
            def __init__(self):
                self.commits = []
                self.sent = False

            async def getone(self):
                if self.sent:
                    await asyncio.sleep(60)
                self.sent = True
                return Msg("garbage")

            async def commit(self, offsets):
                self.commits.append(offsets)

        class Store:
            def __init__(self):
                self.fulfilled = 0

            async def fulfill(self, e):
                self.fulfilled += 1
                return True

        store, consumer = Store(), Consumer()
        stop = asyncio.Event()
        stats = Stats()

        async def go():
            task = asyncio.create_task(worker.consume(
                store, consumer, Redis(get=lambda k: b"1" if k.startswith("control:") else None),
                stats, stop))
            await asyncio.sleep(0.2)
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(worker, "CPU_WORK_MS", 0):
            asyncio.run(go())
        self.assertEqual(store.fulfilled, 0)
        self.assertEqual(consumer.commits, [])
        self.assertGreater(stats.counters.get("errors", 0), 0)

    def test_stop_cancels_blocked_getone(self):
        class Consumer:
            async def getone(self):
                await asyncio.sleep(60)

        stop = asyncio.Event()

        async def go():
            task = asyncio.create_task(
                worker.consume(object(), Consumer(), Redis(), Stats(), stop))
            await asyncio.sleep(0.05)
            stop.set()
            started = asyncio.get_running_loop().time()
            await asyncio.wait_for(task, timeout=1)
            self.assertLess(asyncio.get_running_loop().time() - started, 1)

        asyncio.run(go())


class AppMiddlewareTest(unittest.TestCase):
    def setUp(self):
        from advanced import app as app_mod
        self.app_mod = app_mod
        self.stats = Stats()
        self._stats = patch.object(app_mod, "stats", self.stats)
        self._role = patch.object(app_mod, "ROLE", "api")
        self._stats.start()
        self._role.start()
        self.addCleanup(self._stats.stop)
        self.addCleanup(self._role.stop)

    def _request(self, path):
        from types import SimpleNamespace
        return SimpleNamespace(url=SimpleNamespace(path=path))

    def test_counters_and_histogram_consistent(self):
        from starlette.responses import Response

        async def ok(request):
            return Response(status_code=200)

        async def failing(request):
            return Response(status_code=500)

        async def raising(request):
            raise RuntimeError("boom")

        asyncio.run(self.app_mod.instrument(self._request("/orders/x"), ok))
        asyncio.run(self.app_mod.instrument(self._request("/catalog/t/1"), failing))
        with self.assertRaises(RuntimeError):
            asyncio.run(self.app_mod.instrument(self._request("/orders"), raising))
        asyncio.run(self.app_mod.instrument(self._request("/healthz"), ok))
        c = self.stats.counters
        self.assertEqual(c["requests"], 3)
        self.assertEqual(c["attempts"], 3)
        self.assertEqual(c["completed"], 1)
        self.assertEqual(c["errors"], 2)
        self.assertEqual(len(self.stats.hists["request"]), 3)


class CliTest(unittest.TestCase):
    def test_plan_dry_run_prints_list(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["plan"]), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["kind"], "List")
        self.assertTrue(any(o["kind"] == "StatefulSet" for o in payload["items"]))

    def test_up_defaults_to_dry_run(self):
        calls = []
        with patch.object(cli, "_run", lambda *a, **k: calls.append(a) or ""):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["up"]), 0)
        self.assertTrue(json.loads(out.getvalue())["dry_run"])
        self.assertEqual(calls, [])

    def test_kubectl_always_private_context(self):
        seen = []
        with patch.object(cli, "_run", lambda argv, **k: seen.append(argv) or "x"):
            cli._kubectl(["get", "pods"])
        argv = seen[0]
        self.assertIn("--kubeconfig", argv)
        self.assertIn(str(cli.KUBECONFIG), argv)
        self.assertIn("--context", argv)
        self.assertIn("kind-faultline-advanced", argv)

    def test_up_refuses_foreign_cluster(self):
        def fake_run(argv, **k):
            if argv[1:3] == ["get", "clusters"]:
                return "faultline-advanced\n"
            return ""

        with patch.object(cli, "KUBECONFIG", Path("/nonexistent/kubeconfig")), \
                patch.object(cli, "_run", fake_run):
            self.assertEqual(cli.main(["up", "--execute"]), 2)

    def test_cluster_errors_propagate_before_mutation(self):
        mutated = []

        def fake_run(argv, **k):
            if argv[1:3] == ["get", "clusters"]:
                raise RuntimeError("connection refused")
            mutated.append(argv)
            return ""

        with patch.object(cli, "_run", fake_run):
            self.assertEqual(cli.main(["up", "--execute"]), 2)
        self.assertEqual(mutated, [])

    def test_existing_cluster_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            kube = Path(tmp) / "kubeconfig"
            kube.write_text("k")
            recorded = []

            def fake_run(argv, **k):
                recorded.append(list(argv))
                if argv[1:3] == ["get", "clusters"]:
                    return "faultline-advanced\n"
                if "get-contexts" in argv:
                    return "kind-faultline-advanced\n"
                if "namespace" in argv and "get" in argv:
                    return ""
                if "secret" in argv and "get" in argv:
                    return ""
                return "{}"

            with patch.object(cli, "_run", fake_run), \
                    patch.object(cli, "RUN_DIR", Path(tmp)), \
                    patch.object(cli, "KUBECONFIG", kube), \
                    patch.object(cli, "KIND", Path("/tmp/kind")), \
                    patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = b"calico"
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["up", "--execute"])
            self.assertEqual(code, 0)
            self.assertFalse(any("create" in a and "cluster" in a for a in recorded))

    def test_no_delete_commands_and_ordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorded = []

            def fake_run(argv, **k):
                recorded.append(list(argv))
                if "clusters" in argv:
                    return ""
                if "create" in argv and "cluster" in argv:
                    (Path(tmp) / "kubeconfig").write_text("k")
                    return ""
                if argv[0] == "kubectl" and "namespace" in argv and "get" in argv:
                    return ""
                if "secret" in argv and "get" in argv:
                    return ""
                return "{}"

            with patch.object(cli, "_run", fake_run), \
                    patch.object(cli, "RUN_DIR", Path(tmp)), \
                    patch.object(cli, "KUBECONFIG", Path(tmp) / "kubeconfig"), \
                    patch.object(cli, "KIND", Path("/tmp/kind")), \
                    patch("urllib.request.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = b"calico"
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["up", "--execute"])
            self.assertEqual(code, 0)
            flat = [a for argv in recorded for a in argv]
            self.assertNotIn("delete", flat)
            self.assertNotIn("down", flat)
            creates = [a for a in recorded if "create" in a and "cluster" in a]
            self.assertEqual(len(creates), 1)
            self.assertIn(str(Path(tmp) / "kubeconfig"), creates[0])
            ns_apply = next(i for i, a in enumerate(recorded)
                            if "apply" in a and "-f" in a and "-" in a)
            secret_create = next(i for i, a in enumerate(recorded)
                                 if "create" in a and "-f" in a)
            self.assertLess(ns_apply, secret_create)
            rollouts = [a for a in recorded if "rollout" in a and "status" in a]
            self.assertTrue(any("daemonset/calico-node" in a for a in rollouts))
            self.assertTrue(any("statefulset/kafka" in a for a in rollouts))
            self.assertTrue(any("deployment/api" in a for a in rollouts))
            docker = [a for a in recorded if a[0] == "docker"]
            self.assertEqual(len(docker), 1)
            self.assertIn(str(cli.WORKTREE), docker[0])
            loads = [a for a in recorded if "load" in a and "docker-image" in a]
            self.assertEqual(len(loads), 1)
            self.assertNotIn("--kubeconfig", loads[0])


if __name__ == "__main__":
    unittest.main()
