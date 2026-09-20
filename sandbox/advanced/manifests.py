import base64
import hashlib
import re

from . import runtime

NS_PATTERN = re.compile(r"^faultline-advanced(-clone-[a-z0-9]{8})?$")
MANAGED = {"faultline.dev/managed-by": "distributed-demo"}


def _check_namespace(namespace: str) -> dict:
    if not NS_PATTERN.fullmatch(namespace):
        raise ValueError(f"namespace {namespace!r} is not faultline-advanced or a -clone-<8hex> namespace")
    return {}


def _cluster_id(namespace: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(namespace.encode()).digest()[:16]).decode().rstrip("=")


def _secret_key(name: str, key: str) -> dict:
    return {"name": "advanced-secrets", "key": key}


def _env_secret(name: str, key: str) -> dict:
    return {"name": name, "valueFrom": {"secretKeyRef": _secret_key("advanced-secrets", key)}}


def _pod_spec(**kw) -> dict:
    spec = {
        "serviceAccountName": kw.pop("service_account", "app"),
        "automountServiceAccountToken": kw.pop("automount", False),
        "securityContext": {"fsGroup": 1000},
        "affinity": {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [{
                    "weight": 100,
                    "podAffinityTerm": {
                        "labelSelector": {"matchLabels": kw.pop("affinity_labels", {"app": "advanced"})},
                        "topologyKey": "kubernetes.io/hostname",
                    },
                }],
            },
        },
    }
    spec.update(kw)
    return spec


def _app_container(name: str, role: str, image: str, env_extra: list | None = None) -> dict:
    env = [
        {"name": "NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        {"name": "ROLE", "value": role},
        {"name": "KAFKA_BOOTSTRAP", "value": "kafka:9092"},
        _env_secret("DB_PASSWORD", "postgres-password"),
        _env_secret("REDIS_PASSWORD", "redis-password"),
    ]
    env.extend(env_extra or [])
    return {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "env": env,
        "ports": [{"containerPort": 8000, "name": "stats"}],
        "resources": runtime.SPEC["resources"]["app"],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "allowPrivilegeEscalation": False},
        "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8000},
                           "periodSeconds": 5, "failureThreshold": 12},
    }


def _deployment(name: str, role: str, replicas: int, image: str, recreate: bool = False,
                env_extra: list | None = None) -> dict:
    template_spec = _pod_spec(
        affinity_labels={"app": name},
        containers=[_app_container(name, role, image, env_extra)],
    )
    deploy = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": template_spec},
        },
    }
    if recreate:
        deploy["spec"]["strategy"] = {"type": "Recreate"}
    return deploy


def _service(name: str, port: int, target: int, headless: bool = False) -> dict:
    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "selector": {"app": name.split("-")[0] if name.startswith("shard") else name},
            "ports": [{"port": port, "targetPort": target}],
        },
    }
    svc["spec"]["selector"] = {"app": name}
    if headless:
        svc["spec"]["clusterIP"] = "None"
        svc["spec"]["publishNotReadyAddresses"] = True
    return svc


def _control_deployment(image: str) -> dict:
    container = _app_container("control", "control", image,
                               env_extra=[_env_secret("CONTROL_TOKEN", "control-token")])
    container["command"] = ["uvicorn", "advanced.control:app",
                            "--host", "0.0.0.0", "--port", "8000"]
    template_spec = _pod_spec(
        service_account="control",
        automount=True,
        containers=[container],
    )
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "control", "labels": {"app": "control"}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "control"}},
            "template": {"metadata": {"labels": {"app": "control", "role": "control"}},
                       "spec": template_spec},
        },
    }


def _pg_statefulset(name: str, primary_of: int | None, replica_of: int | None,
                    resources: dict) -> list[dict]:
    shard = primary_of if primary_of is not None else replica_of
    is_primary = primary_of is not None
    pg = runtime.SPEC["postgres"]
    data_mount = {"name": "data", "mountPath": "/var/lib/postgresql/data"}
    env = [
        {"name": "POSTGRES_USER", "value": pg["user"]},
        {"name": "POSTGRES_DB", "value": pg["database"]},
        _env_secret("POSTGRES_PASSWORD", "postgres-password"),
        _env_secret("REPLICATION_PASSWORD", "replication-password"),
        {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
    ]
    if is_primary:
        args = [
            "postgres",
            "-c", "wal_level=replica",
            "-c", "max_wal_senders=10",
            "-c", "max_replication_slots=10",
            "-c", "hot_standby=on",
            "-c", f"synchronous_commit={pg['synchronous_commit']}",
        ]
        readiness = {"exec": {"command": ["pg_isready", "-U", pg["user"], "-d", pg["database"]]},
                     "periodSeconds": 5, "failureThreshold": 30}
        volumes = [{
            "name": "initdb",
            "configMap": {"name": "advanced-init", "defaultMode": 0o755},
        }]
        mounts = [data_mount, {"name": "initdb", "mountPath": "/docker-entrypoint-initdb.d"}]
        init_containers = []
    else:
        peer = f"shard-{shard}-primary"
        env = [*env, _env_secret("PGPASSWORD", "replication-password")]
        args = ["postgres", "-c", "hot_standby=on", "-c", f"synchronous_commit={pg['synchronous_commit']}"]
        readiness = {"exec": {"command": [
            "sh", "-c",
            f"[ \"$(psql -U {pg['user']} -d {pg['database']} -tAc 'SELECT pg_is_in_recovery()')\" = t ]",
        ]}, "periodSeconds": 5, "failureThreshold": 30}
        volumes = []
        mounts = [data_mount]
        init_containers = [{
            "name": "basebackup",
            "image": runtime.SPEC["images"]["postgres"],
            "env": env,
            "command": ["sh", "-c",
                        "set -e; "
                        "if [ ! -f \"$PGDATA/PG_VERSION\" ]; then "
                        "attempt=0; "
                        f"until pg_isready -h {peer} -U {pg['user']} -d {pg['database']}; do "
                        "attempt=$((attempt + 1)); [ \"$attempt\" -ge 120 ] && exit 1; sleep 1; done; "
                        f"pg_basebackup -h {peer} -U replicator "
                        "-D \"$PGDATA\" -R -X stream --checkpoint=fast; "
                        "chown -R postgres:postgres /var/lib/postgresql/data; fi"],
            "volumeMounts": [data_mount],
        }]
    sts = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "serviceName": name,
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name, "shard": f"shard-{shard}"}},
                "spec": _pod_spec(
                    affinity_labels={"shard": f"shard-{shard}"},
                    initContainers=init_containers or None,
                    containers=[{
                        "name": "postgres",
                        "image": runtime.SPEC["images"]["postgres"],
                        "args": args,
                        "env": env,
                        "ports": [{"containerPort": 5432}],
                        "resources": resources,
                        "readinessProbe": readiness,
                        "volumeMounts": mounts,
                    }],
                    volumes=volumes or None,
                ),
            },
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {"accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": pg["storage"]}}},
            }],
        },
    }
    for key in ("initContainers", "volumes"):
        if sts["spec"]["template"]["spec"].get(key) is None:
            del sts["spec"]["template"]["spec"][key]
    return [_service(name, 5432, 5432), sts]


def _kafka_objects(namespace: str) -> list[dict]:
    kafka = runtime.SPEC["kafka"]
    image = runtime.SPEC["images"]["kafka"]
    voters = ",".join(f"{i}@kafka-{i}.kafka:9093" for i in range(kafka["brokers"]))
    env = [
        {"name": "KAFKA_PROCESS_ROLES", "value": "broker,controller"},
        {"name": "KAFKA_LISTENERS", "value": "PLAINTEXT://:9092,CONTROLLER://:9093"},
        {"name": "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP",
         "value": "PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT"},
        {"name": "KAFKA_CONTROLLER_LISTENER_NAMES", "value": "CONTROLLER"},
        {"name": "KAFKA_CONTROLLER_QUORUM_VOTERS", "value": voters},
        {"name": "CLUSTER_ID", "value": _cluster_id(namespace)},
        {"name": "KAFKA_INTER_BROKER_LISTENER_NAME", "value": "PLAINTEXT"},
        {"name": "KAFKA_AUTO_CREATE_TOPICS_ENABLE", "value": "false"},
        {"name": "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "value": str(kafka["replication_factor"])},
        {"name": "KAFKA_MIN_INSYNC_REPLICAS", "value": str(kafka["min_insync_replicas"])},
        {"name": "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "value": str(kafka["replication_factor"])},
        {"name": "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "value": str(kafka["min_insync_replicas"])},
        {"name": "KAFKA_LOG_DIRS", "value": "/var/lib/kafka/data"},
        {"name": "KAFKA_HEAP_OPTS", "value": kafka["heap"]},
    ]
    sts = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "kafka", "labels": {"app": "kafka"}},
        "spec": {
            "serviceName": "kafka",
            "podManagementPolicy": "Parallel",
            "replicas": kafka["brokers"],
            "selector": {"matchLabels": {"app": "kafka"}},
            "template": {
                "metadata": {"labels": {"app": "kafka"}},
                "spec": _pod_spec(
                    affinity_labels={"app": "kafka"},
                    containers=[{
                        "name": "kafka",
                        "image": image,
                        "command": ["sh", "-c",
                                    "export KAFKA_NODE_ID=${HOSTNAME##*-}; "
                                    "export KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://${HOSTNAME}.kafka:9092; "
                                    "exec /etc/kafka/docker/run"],
                        "env": env,
                        "ports": [{"containerPort": 9092}, {"containerPort": 9093}],
                        "resources": runtime.SPEC["resources"]["kafka"],
                        "readinessProbe": {"tcpSocket": {"port": 9092},
                                           "periodSeconds": 5, "failureThreshold": 60},
                        "volumeMounts": [{"name": "data", "mountPath": "/var/lib/kafka/data"}],
                    }],
                ),
            },
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {"accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": kafka["storage"]}}},
            }],
        },
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "kafka-topic-init", "labels": {"app": "kafka-topic-init"}},
        "spec": {
            "template": {
                "metadata": {"labels": {"app": "kafka-topic-init"}},
                "spec": {
                    "serviceAccountName": "app",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "OnFailure",
                    "containers": [{
                        "name": "init",
                        "image": image,
                        "command": ["sh", "-c",
                                    "until /opt/kafka/bin/kafka-broker-api-versions.sh "
                                    "--bootstrap-server kafka:9092 >/dev/null 2>&1; do sleep 2; done; "
                                    "/opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 "
                                    f"--create --if-not-exists --topic {kafka['topic']} "
                                    f"--partitions {kafka['partitions']} "
                                    f"--replication-factor {kafka['replication_factor']} "
                                    f"--config min.insync.replicas={kafka['min_insync_replicas']}"],
                        "env": [{"name": "KAFKA_HEAP_OPTS", "value": "-Xms64m -Xmx128m"}],
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"},
                            "limits": {"cpu": "500m", "memory": "384Mi"},
                        },
                    }],
                },
            },
            "backoffLimit": 10,
        },
    }
    return [_service("kafka", 9092, 9092, headless=True), sts, job]


def _redis_objects() -> list[dict]:
    res = runtime.SPEC["resources"]["redis"]
    sts = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": "redis", "labels": {"app": "redis"}},
        "spec": {
            "serviceName": "redis",
            "replicas": 1,
            "selector": {"matchLabels": {"app": "redis"}},
            "template": {
                "metadata": {"labels": {"app": "redis"}},
                "spec": _pod_spec(
                    affinity_labels={"app": "redis"},
                    containers=[{
                        "name": "redis",
                        "image": runtime.SPEC["images"]["redis"],
                        "command": ["sh", "-c",
                                    "exec redis-server --requirepass \"$REDIS_PASSWORD\" --appendonly yes"],
                        "env": [
                            _env_secret("REDIS_PASSWORD", "redis-password"),
                            _env_secret("REDISCLI_AUTH", "redis-password"),
                        ],
                        "ports": [{"containerPort": 6379}],
                        "resources": res,
                        "readinessProbe": {"exec": {"command": [
                            "sh", "-c", "redis-cli ping | grep -q PONG"]},
                            "periodSeconds": 5, "failureThreshold": 30},
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                    }],
                ),
            },
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {"accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": "1Gi"}}},
            }],
        },
    }
    return [_service("redis", 6379, 6379), sts]


_INIT_SCRIPT = """#!/bin/sh
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v repl_pass="$REPLICATION_PASSWORD" <<'SQL'
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'repl_pass';
SQL
echo "host replication replicator 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"
"""


def _foundation_objects(namespace: str) -> list[dict]:
    schema = (runtime.SPEC_PATH.parent / "schema.sql").read_text()
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": dict(MANAGED)},
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "advanced-quota"},
            "spec": {"hard": {
                "pods": "40",
                "requests.cpu": "8",
                "requests.memory": "10Gi",
                "limits.memory": "20Gi",
                "persistentvolumeclaims": "16",
            }},
        },
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {"name": "advanced-limits"},
            "spec": {"limits": [{
                "type": "Container",
                "defaultRequest": {"cpu": "50m", "memory": "64Mi"},
                "default": {"cpu": "500m", "memory": "512Mi"},
            }]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "default-deny"},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-same-namespace"},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [{"from": [{"podSelector": {}}]}],
                "egress": [{"to": [{"podSelector": {}}]}],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-dns"},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [{
                    "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                    "ports": [{"port": 53, "protocol": "TCP"}, {"port": 53, "protocol": "UDP"}],
                }],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "app"},
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "control"},
            "automountServiceAccountToken": True,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "control"},
            "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                {"apiGroups": ["apps"], "resources": ["deployments"],
                 "resourceNames": [f"worker-{i}" for i in range(runtime.SPEC["replicas"]["worker"])],
                 "verbs": ["get", "patch"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "control"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "control"},
            "subjects": [{"kind": "ServiceAccount", "name": "control"}],
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-control-apiserver"},
            "spec": {
                "podSelector": {"matchLabels": {"role": "control"}},
                "policyTypes": ["Egress"],
                "egress": [{
                    "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
                    "ports": [{"port": 443, "protocol": "TCP"}, {"port": 6443, "protocol": "TCP"}],
                }],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "advanced-init"},
            "data": {"00-schema.sql": schema, "01-init-replication.sh": _INIT_SCRIPT},
        },
    ]


def render(namespace: str, image: str | None = None) -> list[dict]:
    _check_namespace(namespace)
    app_image = image or runtime.SPEC["images"]["app"]
    pg_res = runtime.SPEC["resources"]["postgres"]
    rep = runtime.SPEC["replicas"]
    objects = _foundation_objects(namespace)
    for shard in range(runtime.SPEC["postgres"]["shards"]):
        objects += _pg_statefulset(f"shard-{shard}-primary", primary_of=shard, replica_of=None,
                                   resources=pg_res)
    for shard in range(runtime.SPEC["postgres"]["shards"]):
        objects += _pg_statefulset(f"shard-{shard}-replica", primary_of=None, replica_of=shard,
                                   resources=pg_res)
    objects += _kafka_objects(namespace)
    objects += _redis_objects()
    objects += [
        _deployment("api", "api", rep["api"], app_image),
        _service("api", 8080, 8000),
        _deployment("relay", "relay", rep["relay"], app_image, recreate=True),
        _service("relay", 8000, 8000),
        _deployment("loadgen", "loadgen", rep["loadgen"], app_image),
        _service("loadgen", 8000, 8000),
        _control_deployment(app_image),
        _service("control", 8000, 8000),
    ]
    for index in range(rep["worker"]):
        objects += [
            _deployment(f"worker-{index}", "worker", 1, app_image),
            _service(f"worker-{index}", 8000, 8000),
        ]
    for obj in objects:
        meta = obj.setdefault("metadata", {})
        labels = dict(MANAGED)
        labels.update(meta.get("labels", {}))
        meta["labels"] = labels
        if obj["kind"] != "Namespace":
            meta["namespace"] = namespace
        template = obj.get("spec", {}).get("template")
        if isinstance(template, dict):
            template_meta = template.setdefault("metadata", {})
            template_labels = dict(MANAGED)
            template_labels.update(template_meta.get("labels", {}))
            template_meta["labels"] = template_labels
    return objects


def kind_config() -> dict:
    spec = runtime.SPEC
    nodes = [{"role": "control-plane" if i == 0 else "worker", "image": spec["kind_image"]}
             for i, _ in enumerate(spec["nodes"])]
    return {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": spec["cluster"],
        "networking": {"disableDefaultCNI": True, "podSubnet": "192.168.0.0/16"},
        "nodes": nodes,
    }


__all__ = ["kind_config", "render"]
