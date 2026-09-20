import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = "prepared-patch.json"
SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache"}
COPY_FILES = ["sandbox/Dockerfile", "sandbox/requirements.txt", "contracts/pyproject.toml",
              "contracts/README.md"]
COPY_TREES = ["sandbox/services", "contracts/src"]

ORDERS_PATCH = [
    (
        "import asyncio\nimport logging",
        "import asyncio\nimport json\nimport logging",
    ),
    (
        "import os\nimport time",
        "import os\nimport random\nimport time",
    ),
    (
        'DEFAULT_MAX_RETRIES = int(os.environ.get("ORDERS_MAX_RETRIES", "3"))',
        'DEFAULT_MAX_RETRIES = min(int(os.environ.get("ORDERS_MAX_RETRIES", "1")), 1)',
    ),
    (
        "_in_flight = 0\n",
        "_in_flight = 0\n_retry_tokens = 10.0\n\n\ndef _budget_credit() -> None:\n"
        "    global _retry_tokens\n    _retry_tokens = min(10.0, _retry_tokens + 0.1)\n\n\n"
        "def _budget_take() -> bool:\n    global _retry_tokens\n    if _retry_tokens < 1.0:\n"
        "        return False\n    _retry_tokens -= 1.0\n    return True\n",
    ),
    (
        '    stats.inc("requests")\n',
        '    stats.inc("requests")\n    _budget_credit()\n',
    ),
    (
        '                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)\n',
        '                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)\n'
        "            if not _budget_take():\n"
        '                stats.inc("errors")\n'
        '                stats.inc("retries_budget_denied")\n'
        '                return JSONResponse({"error": "payments unavailable", "attempts": attempt}, status_code=503)\n'
        "            await asyncio.sleep(random.uniform(0.0, min(0.4, 0.05 * 2 ** (attempt - 1))))\n",
    ),
    (
        '    return {"ok": True, "version": VERSION}\n',
        '    return {"ok": True, "version": VERSION, "prepared_sha256": '
        'json.loads(open("/app/prepared-patch.json").read())["content_sha256"]}\n',
    ),
]


def _walk_files(root: Path):
    if root.is_symlink():
        raise ValueError(f"refusing symlinked tree root: {root}")
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                raise ValueError(f"refusing symlink in tree: {Path(dirpath) / name}")
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                raise ValueError(f"refusing symlink in tree: {path}")
            yield path


def _env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def _copy_tree(source: Path, destination: Path, relative: str) -> None:
    src_root = source / relative
    for path in _walk_files(src_root):
        rel = path.relative_to(src_root)
        if any(part in SKIP_DIRS for part in rel.parts) or _env_file(rel.name):
            continue
        target = destination / relative / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


def patch_digest(context: Path) -> str:
    context = Path(context).expanduser().resolve()
    digest = hashlib.sha256()
    files = sorted(
        p for p in _walk_files(context) if p.is_file() and p.name != MANIFEST
    )
    for path in files:
        rel = path.relative_to(context).as_posix()
        digest.update(rel.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _apply_patch(orders: Path) -> None:
    text = orders.read_text()
    for old, new in ORDERS_PATCH:
        if text.count(old) != 1:
            raise ValueError(f"orders/app.py: expected exactly one match for {old[:60]!r}")
        text = text.replace(old, new)
    orders.write_text(text)


def _source_revision(source: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def prepare(source: Path, destination: Path) -> dict:
    source_raw = Path(source).expanduser()
    destination_raw = Path(destination).expanduser()
    if source_raw.is_symlink() or destination_raw.is_symlink():
        raise ValueError("refusing symlinked source or destination root")
    source = source_raw.resolve()
    destination = destination_raw.resolve()
    if destination.exists():
        raise ValueError(f"destination already exists: {destination}")
    if not (source / "sandbox" / "Dockerfile").is_file():
        raise ValueError(f"{source} is not a checkout root (no sandbox/Dockerfile)")
    destination.mkdir(parents=True)
    for relative in COPY_FILES:
        src = source / relative
        if src.is_symlink():
            raise ValueError(f"refusing symlink in source: {src}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
    for relative in COPY_TREES:
        _copy_tree(source, destination, relative)
    _apply_patch(destination / "sandbox" / "services" / "orders" / "app.py")
    dockerfile = destination / "sandbox" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "\nCOPY prepared-patch.json /app/prepared-patch.json\n")
    manifest = {
        "schema_version": "faultline-prepared-patch/1",
        "provider": "prepared",
        "label": "Prepared bounded-retry patch; not generated during this run",
        "content_sha256": patch_digest(destination),
        "source_revision": _source_revision(source),
        "replay_profile": "retry-storm-v1",
    }
    (destination / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="fresh destination directory (must not exist)")
    parser.add_argument("--source", type=Path, default=REPO,
                        help="checkout root to copy build inputs from")
    args = parser.parse_args(argv)
    try:
        manifest = prepare(args.source, args.output)
    except ValueError as exc:
        print(f"prepare_demo_patch: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
