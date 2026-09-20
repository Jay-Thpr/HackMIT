import argparse
import json
import os
import secrets
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import manifests, runtime

WORKTREE = Path(__file__).resolve().parents[2]
RUN_DIR = WORKTREE / ".faultline" / "advanced"
KUBECONFIG = RUN_DIR / "kubeconfig"
KIND = WORKTREE / ".faultline" / "bin" / "kind"
CLUSTER = runtime.SPEC["cluster"]
CONTEXT = runtime.SPEC["context"]
NAMESPACE = runtime.SPEC["namespace"]
IMAGE = runtime.SPEC["images"]["app"]
MANAGED_VALUE = "distributed-demo"


def _run(argv: list[str], *, input_text: str | None = None, timeout: int = 600,
         cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                            timeout=timeout, cwd=cwd, env=env)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {result.stderr.strip()[-400:]}")
    return result.stdout


def _kubectl(args: list[str], *, input_text: str | None = None, timeout: int = 120) -> str:
    return _run(["kubectl", "--kubeconfig", str(KUBECONFIG), "--context", CONTEXT, *args],
                input_text=input_text, timeout=timeout)


def _cluster_exists() -> bool:
    out = _run([str(KIND), "get", "clusters"], timeout=30)
    return CLUSTER in out.split()


def _create_cluster() -> None:
    if _cluster_exists():
        if not KUBECONFIG.exists():
            raise RuntimeError(
                f"cluster {CLUSTER} exists but private kubeconfig {KUBECONFIG} is missing; refusing")
        out = _kubectl(["config", "get-contexts", CONTEXT, "-o", "name"], timeout=30)
        if CONTEXT not in out.split():
            raise RuntimeError(f"private kubeconfig has no context {CONTEXT}; refusing")
        return
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    config_path = RUN_DIR / "kind.json"
    config_path.write_text(json.dumps(manifests.kind_config(), indent=2))
    _run([str(KIND), "create", "cluster", "--name", CLUSTER,
          "--config", str(config_path), "--kubeconfig", str(KUBECONFIG)], timeout=900)
    KUBECONFIG.chmod(0o600)


def _check_namespace() -> None:
    out = _kubectl(["get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json"])
    if not out.strip():
        return
    labels = json.loads(out).get("metadata", {}).get("labels", {})
    if labels.get("faultline.dev/managed-by") != MANAGED_VALUE:
        raise RuntimeError(f"namespace {NAMESPACE} exists and is not managed by this demo; refusing")


def _apply_namespace() -> None:
    items = [o for o in manifests.render(NAMESPACE) if o["kind"] == "Namespace"]
    _kubectl(["apply", "-f", "-"], input_text=json.dumps(
        {"apiVersion": "v1", "kind": "List", "items": items}))


def _ensure_secrets() -> None:
    out = _kubectl(["get", "secret", "advanced-secrets", "-n", NAMESPACE,
                    "--ignore-not-found", "-o", "name"])
    if out.strip():
        return
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "advanced-secrets", "namespace": NAMESPACE},
        "stringData": {
            "postgres-password": secrets.token_urlsafe(24),
            "replication-password": secrets.token_urlsafe(24),
            "redis-password": secrets.token_urlsafe(24),
            "control-token": secrets.token_urlsafe(24),
        },
    }
    _kubectl(["create", "-f", "-"], input_text=json.dumps(secret))


def _install_calico() -> None:
    dest = RUN_DIR / "calico.yaml"
    with urllib.request.urlopen(runtime.SPEC["calico_manifest"], timeout=60) as response:
        dest.write_bytes(response.read())
    _kubectl(["apply", "-f", str(dest)], timeout=300)
    _kubectl(["-n", "kube-system", "rollout", "status", "daemonset/calico-node",
              "--timeout=300s"], timeout=320)
    _kubectl(["wait", "--for=condition=Ready", "nodes", "--all", "--timeout=300s"], timeout=320)


def _build() -> None:
    _run(["docker", "build", "-f", str(WORKTREE / "sandbox" / "advanced" / "Dockerfile"),
          "-t", IMAGE, str(WORKTREE)], cwd=WORKTREE, timeout=1200)
    env = {**os.environ, "KUBECONFIG": str(KUBECONFIG)}
    _run([str(KIND), "load", "docker-image", IMAGE, "--name", CLUSTER],
         env=env, timeout=900)


def _apply_manifest() -> None:
    payload = {"apiVersion": "v1", "kind": "List",
               "items": manifests.render(NAMESPACE)}
    path = RUN_DIR / "manifest.json"
    path.write_text(json.dumps(payload, indent=2))
    _kubectl(["apply", "-f", str(path)], timeout=300)


def _wait_ready() -> None:
    for obj in manifests.render(NAMESPACE):
        kind, name = obj["kind"], obj["metadata"]["name"]
        if kind in ("Deployment", "StatefulSet"):
            _kubectl(["rollout", "status", f"{kind.lower()}/{name}", "-n", NAMESPACE,
                      "--timeout=240s"], timeout=260)
        elif kind == "Job":
            _kubectl(["wait", "--for=condition=complete", f"job/{name}", "-n", NAMESPACE,
                      "--timeout=240s"], timeout=260)


def cmd_plan(args) -> int:
    print(json.dumps({"apiVersion": "v1", "kind": "List",
                      "items": manifests.render(NAMESPACE, image=args.image)}, indent=2))
    return 0


def cmd_up(args) -> int:
    if not args.execute:
        print(json.dumps({"dry_run": True, "cluster": CLUSTER, "context": CONTEXT,
                          "namespace": NAMESPACE,
                          "steps": ["create or reuse kind cluster", "check namespace ownership",
                                    "apply namespace", "create secrets",
                                    "install calico and wait nodes", "build image",
                                    "kind load image", "apply manifest", "wait ready"]}, indent=2))
        return 0
    _create_cluster()
    _check_namespace()
    _apply_namespace()
    _ensure_secrets()
    _install_calico()
    _build()
    _apply_manifest()
    _wait_ready()
    print(json.dumps({"status": "ready", "namespace": NAMESPACE}))
    return 0


def cmd_build(args) -> int:
    if not args.execute:
        print(json.dumps({"dry_run": True, "image": IMAGE,
                          "steps": ["docker build", "kind load docker-image"]}, indent=2))
        return 0
    if not _cluster_exists() or not KUBECONFIG.exists():
        raise RuntimeError(f"cluster {CLUSTER} not up with private kubeconfig")
    _build()
    return 0


def cmd_status(args) -> int:
    if not KUBECONFIG.exists():
        print(json.dumps({"status": "no kubeconfig", "cluster": CLUSTER}))
        return 1
    out = _kubectl(["get", "all", "-n", NAMESPACE])
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advanced")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--image")
    plan.set_defaults(fn=cmd_plan)
    up = commands.add_parser("up")
    up.add_argument("--execute", action="store_true")
    up.set_defaults(fn=cmd_up)
    build = commands.add_parser("build")
    build.add_argument("--execute", action="store_true")
    build.set_defaults(fn=cmd_build)
    status = commands.add_parser("status")
    status.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"advanced: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
