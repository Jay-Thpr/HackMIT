"""Screenshot the Faultline Kibana evidence dashboard (PRD DoD item 13).

Opens the ``faultline-evidence`` dashboard (created by kibana_setup.py) in the
installed Google Chrome via Playwright — no browser download needed — and
saves a full-page screenshot to
``faultline/telemetry/docs/kibana-traces-production-vs-clone.png``.

    uv run --with playwright python scripts/kibana_screenshot.py

Reads KIBANA_URL / FAULTLINE_ELASTICSEARCH_API_KEY from the environment
(falling back to the repo-root .env). The ApiKey is sent as an HTTP header on
every request; if Kibana still redirects to a cloud login page, the script
prints what it sees and exits non-zero.
"""

import argparse
import os
import sys
from pathlib import Path

from faultline_telemetry import load_repo_dotenv

PNG_PATH = Path(__file__).resolve().parents[1] / "docs" / "kibana-traces-production-vs-clone.png"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kibana-url", default=None, help="Override $KIBANA_URL")
    parser.add_argument(
        "--api-key-env",
        default="FAULTLINE_ELASTICSEARCH_API_KEY",
        help="Name of the env var holding the API key (default: FAULTLINE_ELASTICSEARCH_API_KEY)",
    )
    args = parser.parse_args()

    load_repo_dotenv(Path(__file__))
    kibana_url = (args.kibana_url or os.environ.get("KIBANA_URL", "")).rstrip("/")
    api_key = os.environ.get(args.api_key_env)
    if not kibana_url or not api_key:
        print(f"KIBANA_URL and {args.api_key_env} must be set", file=sys.stderr)
        return 2

    from playwright.sync_api import sync_playwright

    rstate = '_g=(time:(from:now-24h,to:now))'
    dashboard_url = f"{kibana_url}/app/dashboards#/view/faultline-evidence?{rstate}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            extra_http_headers={
                "Authorization": f"ApiKey {api_key}",
                "kbn-xsrf": "true",
            },
        )
        page = context.new_page()
        page.goto(dashboard_url, wait_until="domcontentloaded", timeout=120_000)

        if "login" in page.url:
            print(f"Auth redirect: URL={page.url} TITLE={page.title()}", file=sys.stderr)
            page.screenshot(path=str(PNG_PATH.with_name("kibana-login-failure.png")))
            browser.close()
            return 1

        # Wait for the dashboard grid to mount, then for the search panels to
        # fetch and render their document tables.
        try:
            page.wait_for_selector("[data-test-subj='dashboardGrid'], .dshDashboardGrid", timeout=120_000)
        except Exception:
            print("warning: dashboard grid not detected", file=sys.stderr)
        try:
            page.wait_for_load_state("networkidle", timeout=60_000)
        except Exception:
            pass
        page.wait_for_timeout(5_000)
        try:
            page.wait_for_selector(".euiDataGridRow, .kbnDocTable__row, [data-test-subj='discoverDocTable'] tr, .unifiedDataTable__row", timeout=60_000)
        except Exception:
            print("warning: no table rows detected before screenshot", file=sys.stderr)
        page.wait_for_timeout(5_000)

        PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(PNG_PATH), full_page=True)
        print(f"URL: {page.url}")
        print(f"TITLE: {page.title()}")
        print(f"Saved: {PNG_PATH}")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
