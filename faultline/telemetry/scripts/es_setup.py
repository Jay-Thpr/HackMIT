import argparse
import os
from pathlib import Path

from faultline_telemetry import HttpElasticsearchClient, ensure_index_templates, load_repo_dotenv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observability", "--mirror", action="store_true")
    args = parser.parse_args()
    load_repo_dotenv(Path(__file__))
    prefix = "FAULTLINE_OBSERVABILITY_ELASTICSEARCH" if args.observability else "FAULTLINE_ELASTICSEARCH"
    if args.observability and not any(os.environ.get(f"{prefix}_{suffix}") for suffix in ("URL", "API_KEY", "SETUP_API_KEY")):
        prefix = "FAULTLINE_ELASTICSEARCH_MIRROR"
    url = os.environ.get(f"{prefix}_URL")
    key = os.environ.get(f"{prefix}_SETUP_API_KEY")
    if not url or not key:
        print(f"Set {prefix}_URL and {prefix}_SETUP_API_KEY for custom-index setup")
        return 2
    client = HttpElasticsearchClient(url, api_key=key)
    try:
        ensure_index_templates(client)
        print("Custom C1/C4 templates installed; managed telemetry templates unchanged")
        return 0
    except Exception as exc:
        print(f"Custom-index setup failed ({type(exc).__name__})")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
