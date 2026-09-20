"""Minimal repo-root .env loader shared by the smoke script and the product CLI."""

import os
from pathlib import Path


def load_repo_dotenv(start: Path) -> Path | None:
    """Load ``KEY=VALUE`` lines from the repo-root .env without overriding real env vars.

    Walks up from ``start`` until a directory containing ``.git`` marks the repo
    root (stops at the filesystem root). Returns the loaded .env path, or None.
    """
    directory = start.resolve()
    if directory.is_file():
        directory = directory.parent
    while True:
        if (directory / ".git").exists():
            env_path = directory / ".env"
            if not env_path.exists():
                return None
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key.strip(), value)
            return env_path
        if directory.parent == directory:
            return None
        directory = directory.parent
