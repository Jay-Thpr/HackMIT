import asyncio
import json
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from advanced import control, runtime  # noqa: E402


def _request(token="tok"):
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    return SimpleNamespace(headers=headers)


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    async def get(self, key):
        return self.data.get(key)

    async def pttl(self, key):
        return self.ttls.get(key, -1)

    async def set(self, key, value, nx=False, px=None, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        if px:
            self.ttls[key] = px
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    async def decr(self, key):
        self.data[key] = str(int(self.data.get(key, "0")) - 1)
        return int(self.data[key])

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key, "0")) + 1)
        return int(self.data[key])

    async def expire(self, key, seconds):
        return True

    async def eval(self, script, n, key, *args):
        if "incr" in script:
            current = int(self.data.get(key, "0"))
            if current >= int(args[0]):
                return -1
            self.data[key] = str(current + 1)
            return current + 1
        if self.data.get(key) == args[0]:
            del self.data[key]
            return 1
        return 0

    async def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key


def _deployment(rv="1", cpu_req="100m", cpu_lim="500m", action_id=None):
    annotations = {}
    if action_id:
        annotations["faultline.dev/action-id"] = action_id
    return {
        "metadata": {"resourceVersion": rv, "annotations": annotations},
        "spec": {"replicas": 1, "template": {"spec": {"containers": [
            {"resources": {"requests": {"cpu": cpu_req}, "limits": {"cpu": cpu_lim}}}]}},
        },
        "status": {"readyReplicas": 1},
    }


def _apply_patch(deployment, ops):
    for op in ops:
        if op["op"] == "test":
            assert deployment["metadata"]["resourceVersion"] == op["value"]
        elif op["op"] == "add" and op["path"] == "/metadata/annotations":
            deployment["metadata"].setdefault("annotations", {}).update(op["value"])
        elif op["op"] == "add" and "action-id" in op["path"]:
            deployment["metadata"].setdefault("annotations", {})["faultline.dev/action-id"] = op["value"]
        elif op["op"] == "remove" and "action-id" in op["path"]:
            deployment["metadata"].get("annotations", {}).pop("faultline.dev/action-id", None)
        elif op["op"] in ("add", "replace") and "requests/cpu" in op["path"]:
            deployment["spec"]["template"]["spec"]["containers"][0] \
                ["resources"].setdefault("requests", {})["cpu"] = op["value"]
        elif op["op"] in ("add", "replace") and "limits/cpu" in op["path"]:
            deployment["spec"]["template"]["spec"]["containers"][0] \
                ["resources"].setdefault("limits", {})["cpu"] = op["value"]
        elif op["op"] == "remove" and "requests/cpu" in op["path"]:
            deployment["spec"]["template"]["spec"]["containers"][0] \
                ["resources"]["requests"].pop("cpu", None)
        elif op["op"] == "remove" and "limits/cpu" in op["path"]:
            deployment["spec"]["template"]["spec"]["containers"][0] \
                ["resources"]["limits"].pop("cpu", None)
    deployment["metadata"]["resourceVersion"] = str(
        int(deployment["metadata"]["resourceVersion"]) + 1)


class FakeKube:
    def __init__(self):
        self.deployments = {f"worker-{i}": _deployment() for i in range(3)}
        self.patches = []

    def get_deployment(self, name):
        return self.deployments[name]

    def patch_deployment(self, name, resource_version, patch):
        if self.deployments[name]["metadata"]["resourceVersion"] != resource_version:
            raise RuntimeError("conflict")
        self.patches.append((name, resource_version, patch))
        _apply_patch(self.deployments[name], patch)
        return self.deployments[name]


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.kube = FakeKube()
        self._state = dict(control._state)
        control._state.clear()
        control._state.update({"redis": self.redis, "kube": self.kube})
        self.addCleanup(lambda: (control._state.clear(), control._state.update(self._state)))
        self._token = patch.object(runtime, "CONTROL_TOKEN", "tok")
        self._token.start()
        self.addCleanup(self._token.stop)

    def _body(self, lever_id, params, ttl=30, incident="inc-1"):
        return control.ActionBody(lever_id=lever_id, params=params,
                                  ttl_s=ttl, incident_id=incident)

    def test_auth_required(self):
        from fastapi import HTTPException
        for token in (None, "wrong", ""):
            req = SimpleNamespace(
                headers={} if token is None else {"authorization": f"Bearer {token}"})
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(control.catalog(req))
            self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(len(asyncio.run(control.catalog(_request()))), 5)

    def test_apply_writes_ttl_key_and_ledger(self):
        result = asyncio.run(control.apply(
            _request(), self._body("tenant_admission",
                                   {"tenant": "tenant-a", "max_rps": 5}, ttl=30)))
        key = "control:tenant_admission:tenant-a"
        payload = json.loads(self.redis.data[key])
        self.assertEqual(payload["value"], 5)
        self.assertEqual(payload["action_id"], result["action_id"])
        self.assertEqual(self.redis.ttls[key], 30000)
        self.assertIn(f"action:{result['action_id']}", self.redis.data)
        self.assertEqual(self.redis.data["action_budget:inc-1"], "1")

    def test_budget_enforced_and_conflict_rolls_back(self):
        for c in "abcdf":
            asyncio.run(control.apply(
                _request(), self._body("tenant_admission",
                                       {"tenant": f"tenant-{c}", "max_rps": 5},
                                       incident="inc-1")))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.apply(
                _request(), self._body("cache_coalescing",
                                       {"tenant": "tenant-e", "enabled": True},
                                       incident="inc-1")))
        self.assertEqual(ctx.exception.status_code, 429)
        self.redis.data["control:tenant_admission:tenant-a"] = \
            json.dumps({"action_id": "other", "value": 1})
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.apply(
                _request(), self._body("tenant_admission",
                                       {"tenant": "tenant-a", "max_rps": 5},
                                       incident="inc-2")))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.redis.data["action_budget:inc-2"], "0")

    def test_invalid_params_and_ttl_rejected(self):
        from fastapi import HTTPException
        cases = [
            self._body("tenant_admission", {"tenant": "nope", "max_rps": 5}),
            self._body("tenant_admission", {"tenant": "tenant-a", "max_rps": 0}),
            self._body("tenant_admission", {"tenant": "tenant-a", "max_rps": True}),
            self._body("tenant_admission", {"tenant": "tenant-a", "max_rps": 5, "x": 1}),
            self._body("consumer_backoff", {"partition": True, "backoff_ms": 100}),
            self._body("consumer_backoff", {"partition": 9, "backoff_ms": 100}),
            self._body("read_route", {"shard": 0, "target": "both"}),
            self._body("cache_coalescing", {"tenant": "tenant-a", "enabled": 1}),
            self._body("worker_cpu", {"worker": "api", "millicores": 100}),
            self._body("worker_cpu", {"worker": "worker-0", "millicores": 2000}),
            self._body("tenant_admission", {"tenant": "tenant-a", "max_rps": 5}, ttl=121),
        ]
        for body in cases:
            with self.assertRaises(HTTPException, msg=str(body)):
                asyncio.run(control.apply(_request(), body))
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.apply(
                _request(), control.ActionBody(
                    lever_id="nope", params={}, ttl_s=10, incident_id="i")))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_status_and_undo_compare_and_delete(self):
        result = asyncio.run(control.apply(
            _request(), self._body("read_route", {"shard": 1, "target": "primary"})))
        action_id = result["action_id"]
        got = asyncio.run(control.status(_request(), action_id))
        self.assertEqual(got["status"], "active")
        self.redis.data.pop("control:read_route:shard-1")
        got = asyncio.run(control.status(_request(), action_id))
        self.assertEqual(got["status"], "expired")

        result2 = asyncio.run(control.apply(
            _request(), self._body("read_route", {"shard": 2, "target": "replica"})))
        self.redis.data["control:read_route:shard-2"] = \
            json.dumps({"action_id": "intruder", "value": "replica"})
        undone = asyncio.run(control.undo(_request(), result2["action_id"]))
        self.assertEqual(undone["status"], "failed")
        self.assertEqual(json.loads(
            self.redis.data["control:read_route:shard-2"])["action_id"], "intruder")

        result3 = asyncio.run(control.apply(
            _request(), self._body("cache_coalescing",
                                   {"tenant": "tenant-b", "enabled": True})))
        undone3 = asyncio.run(control.undo(_request(), result3["action_id"]))
        self.assertEqual(undone3["status"], "undone")
        self.assertNotIn("control:cache_coalescing:tenant-b", self.redis.data)

    def test_worker_cpu_lease_and_restore(self):
        result = asyncio.run(control.apply(
            _request(), self._body("worker_cpu", {"worker": "worker-1", "millicores": 250})))
        action_id = result["action_id"]
        dep = self.kube.deployments["worker-1"]
        self.assertEqual(dep["metadata"]["annotations"]["faultline.dev/action-id"], action_id)
        self.assertEqual(dep["spec"]["template"]["spec"]["containers"][0]
                         ["resources"]["limits"]["cpu"], "250m")
        lease = json.loads(self.redis.data["cpu_lease:worker-1"])
        self.assertEqual(lease["original"], {"requests": "100m", "limits": "500m"})
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.apply(
                _request(), self._body("worker_cpu", {"worker": "worker-1", "millicores": 300})))
        self.assertEqual(ctx.exception.status_code, 409)
        undone = asyncio.run(control.undo(_request(), action_id))
        self.assertEqual(undone["status"], "undone")
        dep = self.kube.deployments["worker-1"]
        self.assertEqual(dep["spec"]["template"]["spec"]["containers"][0]
                         ["resources"]["limits"]["cpu"], "500m")
        self.assertNotIn("cpu_lease:worker-1", self.redis.data)

    def test_lab_actions_only_on_clone_namespace(self):
        from fastapi import HTTPException
        body = control.LabActionBody(action="worker_delay",
                                     params={"partition": 0, "delay_ms": 250}, ttl_s=30)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.lab_apply(_request(), body))
        self.assertEqual(ctx.exception.status_code, 404)
        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"):
            result = asyncio.run(control.lab_apply(_request(), body))
            key = "bench:worker_delay:0"
            payload = json.loads(self.redis.data[key])
            self.assertEqual(payload["value"], 0.25)
            self.assertEqual(self.redis.ttls[key], 30000)
            undone = asyncio.run(control.lab_undo(_request(), result["action_id"]))
            self.assertEqual(undone["status"], "undone")
            self.assertNotIn(key, self.redis.data)

    def test_lab_workload_cache_and_cpu(self):
        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"):
            r = asyncio.run(control.lab_apply(
                _request(), control.LabActionBody(
                    action="workload",
                    params={"extra_rps": 50.0, "tenant": "tenant-c"}, ttl_s=30)))
            payload = json.loads(self.redis.data["bench:workload:global"])
            self.assertEqual(payload["value"], {"extra_rps": 50.0, "tenant": "tenant-c"})
            self.assertEqual(asyncio.run(
                control.lab_undo(_request(), r["action_id"]))["status"], "undone")
            asyncio.run(control.lab_apply(
                _request(), control.LabActionBody(
                    action="cache_policy",
                    params={"tenant": "tenant-a", "ttl_s": 15}, ttl_s=30)))
            self.assertEqual(json.loads(
                self.redis.data["bench:cache_ttl:tenant-a"])["value"], 15)
            r = asyncio.run(control.lab_apply(
                _request(), control.LabActionBody(
                    action="cpu_limit", params={"worker": "worker-0", "cpus": 0.5}, ttl_s=30)))
            self.assertEqual(self.kube.deployments["worker-0"]["spec"]["template"]
                             ["spec"]["containers"][0]["resources"]["limits"]["cpu"], "500m")
            self.assertEqual(asyncio.run(
                control.lab_undo(_request(), r["action_id"]))["status"], "undone")
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                asyncio.run(control.lab_apply(
                    _request(), control.LabActionBody(
                        action="cpu_limit", params={"worker": "worker-0", "cpus": 0.01},
                        ttl_s=30)))
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(control.lab_apply(
                    _request(), control.LabActionBody(
                        action="workload",
                        params={"extra_rps": 500, "tenant": "tenant-a"}, ttl_s=30)))
            self.assertEqual(ctx.exception.status_code, 422)

    def test_replica_pause_lease_and_expiry(self):
        from advanced import sql

        executed = []

        class Conn:
            async def execute(self, statement):
                executed.append(statement)

            async def fetchval(self, statement):
                executed.append(statement)
                return False

            async def close(self):
                pass

        conn = Conn()

        async def fake_connect(dsn, timeout):
            return conn

        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"), \
                patch("asyncpg.connect", fake_connect):
            r = asyncio.run(control.lab_apply(
                _request(), control.LabActionBody(
                    action="replica_pause", params={"shard": 1}, ttl_s=30)))
            self.assertEqual(executed, [sql.REPLAY_PAUSED, sql.PAUSE_REPLAY])
            lease = json.loads(self.redis.data["pause_lease:1"])
            lease["expires_at"] = time.time() - 1
            self.redis.data["pause_lease:1"] = json.dumps(lease)
            asyncio.run(control._restore_pauses(self.redis))
            self.assertEqual(executed, [sql.REPLAY_PAUSED, sql.PAUSE_REPLAY,
                                        sql.RESUME_REPLAY, sql.REPLAY_PAUSED])
            self.assertNotIn("pause_lease:1", self.redis.data)

    def test_replica_pause_write_ahead_and_overlap(self):
        from advanced import sql

        executed = []

        class Conn:
            def __init__(self):
                self.paused = False

            async def execute(self, statement):
                executed.append(statement)
                if statement == sql.PAUSE_REPLAY:
                    self.paused = True

            async def fetchval(self, statement):
                return self.paused

            async def close(self):
                pass

        conn = Conn()

        async def fake_connect(dsn, timeout):
            return conn

        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"), \
                patch("asyncpg.connect", fake_connect):
            self.redis.data["pause_lease:0"] = json.dumps(
                {"action_id": "other", "shard": 0, "expires_at": time.time() + 60})
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(control.lab_apply(
                    _request(), control.LabActionBody(
                        action="replica_pause", params={"shard": 0}, ttl_s=30)))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(executed, [])
            r = asyncio.run(control.lab_apply(
                _request(), control.LabActionBody(
                    action="replica_pause", params={"shard": 1}, ttl_s=30)))
            self.assertIn("pause_lease:1", self.redis.data)
            undone = asyncio.run(control.lab_undo(_request(), r["action_id"]))
            self.assertEqual(undone["status"], "failed")
            lease = json.loads(self.redis.data["pause_lease:1"])
            self.assertEqual(lease["status"], "failed")

    def test_cpu_lease_ambiguous_patch_keeps_lease(self):
        class FlakyKube(FakeKube):
            def patch_deployment(self, name, resource_version, patch):
                _apply_patch(self.deployments[name], patch)
                raise TimeoutError("timed out")

        self.kube = FlakyKube()
        control._state["kube"] = self.kube
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.apply(
                _request(), self._body("worker_cpu",
                                       {"worker": "worker-0", "millicores": 200})))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("cpu_lease:worker-0", self.redis.data)

    def test_pause_ambiguous_and_already_paused(self):
        class Conn:
            def __init__(self, paused, apply_on_execute=False, fail_execute=False):
                self.paused = paused
                self.apply_on_execute = apply_on_execute
                self.fail_execute = fail_execute

            async def execute(self, statement):
                if self.apply_on_execute:
                    self.paused = True
                if self.fail_execute:
                    raise TimeoutError("write timed out")

            async def fetchval(self, statement):
                return self.paused

            async def close(self):
                pass

        def connect_to(conn):
            async def fake(dsn, timeout):
                return conn
            return fake

        from fastapi import HTTPException
        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"), \
                patch("asyncpg.connect", connect_to(Conn(paused=True))):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(control.lab_apply(
                    _request(), control.LabActionBody(
                        action="replica_pause", params={"shard": 0}, ttl_s=30)))
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertNotIn("pause_lease:0", self.redis.data)

        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"), \
                patch("asyncpg.connect", connect_to(Conn(paused=False, fail_execute=True))):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(control.lab_apply(
                    _request(), control.LabActionBody(
                        action="replica_pause", params={"shard": 1}, ttl_s=30)))
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertNotIn("pause_lease:1", self.redis.data)

        with patch.object(runtime, "NAMESPACE", "faultline-advanced-clone-a1b2c3d4"), \
                patch("asyncpg.connect", connect_to(
                    Conn(paused=False, apply_on_execute=True, fail_execute=True))):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(control.lab_apply(
                    _request(), control.LabActionBody(
                        action="replica_pause", params={"shard": 2}, ttl_s=30)))
            self.assertEqual(ctx.exception.status_code, 502)
            lease = json.loads(self.redis.data["pause_lease:2"])
            self.assertTrue(lease["undo"])

    def test_old_handle_cannot_undo_new_lease(self):
        old = asyncio.run(control.apply(
            _request(), self._body("worker_cpu",
                                   {"worker": "worker-2", "millicores": 200})))
        self.redis.data.pop("cpu_lease:worker-2")
        dep = self.kube.deployments["worker-2"]
        dep["metadata"]["annotations"].pop("faultline.dev/action-id")
        dep["spec"]["template"]["spec"]["containers"][0]["resources"] = \
            {"requests": {"cpu": "100m"}, "limits": {"cpu": "500m"}}
        new = asyncio.run(control.apply(
            _request(), self._body("worker_cpu",
                                   {"worker": "worker-2", "millicores": 300})))
        result = asyncio.run(control.undo(_request(), old["action_id"]))
        self.assertEqual(result["status"], "failed")
        lease = json.loads(self.redis.data["cpu_lease:worker-2"])
        self.assertEqual(lease["action_id"], new["action_id"])
        self.assertFalse(lease.get("undo", False))
        self.assertEqual(self.kube.deployments["worker-2"]["spec"]["template"]
                         ["spec"]["containers"][0]["resources"]["limits"]["cpu"], "300m")

    def test_snapshot_endpoint_wires_real_sources(self):
        control._state.update({"store": object(), "consumer": object(),
                               "http": lambda url, timeout: {}})
        captured = {}

        async def fake_snapshot(**kw):
            captured.update(kw)
            return {"ok": True}

        with patch("advanced.observe.snapshot", fake_snapshot):
            result = asyncio.run(control.snapshot(_request()))
        self.assertEqual(result, {"ok": True})
        self.assertIs(captured["store"], control._state["store"])
        self.assertIs(captured["consumer"], control._state["consumer"])
        self.assertIs(captured["http"], control._state["http"])
        self.assertIs(captured["kube"], self.kube)

    def test_healthz_503_when_unhealthy(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(control.healthz())
        self.assertEqual(ctx.exception.status_code, 503)


class ObserveTest(unittest.TestCase):
    def test_snapshot_shape_and_lag(self):
        from advanced import observe, sql

        class FakePool:
            def __init__(self, conn):
                self.conn = conn

            def acquire(self, *a, **k):
                conn = self.conn

                class AC:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *a):
                        return False

                return AC()

        class FakeConn:
            def transaction(self, **k):
                class AC:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *a):
                        return False

                return AC()

            async def fetchrow(self, query, **k):
                if query == sql.PRIMARY_POSITION:
                    return {"lsn": "0/200", "in_recovery": False}
                if query == sql.REPLICA_POSITION:
                    return {"lsn": "0/100", "in_recovery": True}
                return None

            async def fetch(self, query, **k):
                if query == sql.TENANT_SNAPSHOT:
                    return [{"tenant_id": "tenant-a", "accepted": 3, "paid": 3,
                             "fulfilled": 3, "outbox_pending": 0, "outstanding": 0,
                             "oldest_outstanding_ms": None, "mismatched_effects": 0,
                             "inconsistent_fulfillment": 0}]
                return []

        conn = FakeConn()
        store = SimpleNamespace(
            primaries=[FakePool(conn)] * 3, replicas=[FakePool(conn)] * 3)

        class Kube:
            def list_pods(self, selector):
                return {"items": [{
                    "metadata": {"name": "api-abc", "uid": "u1",
                                 "labels": {"faultline.dev/managed-by": "distributed-demo"}},
                    "status": {"podIP": "10.0.0.5"}}]}

            def get_deployment(self, name):
                return {"spec": {"replicas": 1}, "status": {"readyReplicas": 1}}

        def http(url, timeout):
            return {"instance_id": "boot1", "counters": {"requests": 5}}

        class Consumer:
            def partitions_for_topic(self, topic):
                return {0, 1}

            async def end_offsets(self, tps):
                return {tps[0]: 10}

            async def committed(self, tp):
                return 7 if tp.partition == 0 else None

            async def beginning_offsets(self, tps):
                return {tps[0]: 0}

        snap = asyncio.run(observe.snapshot(store, None, Kube(), Consumer(), http))
        self.assertEqual(snap["schema_version"], "faultline-distributed-stats/1")
        identity = "u1-boot1"
        self.assertEqual(snap["instances"][identity]["service"], "api")
        self.assertEqual(snap["resources"]["shard_0_replica"]["replication_lag_bytes"],
                         256)
        self.assertEqual(snap["resources"]["kafka_partition_0"]["lag_messages"], 3)
        self.assertNotIn("kafka_partition_1", snap["resources"])
        self.assertEqual(snap["resources"]["worker_0"]["ready_replicas"], 1)
        self.assertEqual(snap["resources"]["tenant_tenant_a"]["accepted_total"], 3)
        self.assertTrue(all(isinstance(e["src"], str) for e in snap["edges"]))
        self.assertEqual(snap["business_snapshot"]["shards"][0]["complete"], True)


if __name__ == "__main__":
    unittest.main()
