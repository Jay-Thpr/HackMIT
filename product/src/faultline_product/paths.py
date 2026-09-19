from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = PRODUCT_ROOT.parent
CONTRACT_FIXTURES = REPOSITORY_ROOT / "contracts" / "fixtures"
DEFAULT_AUDIT_LOG = PRODUCT_ROOT / "state" / "faultline-audit.jsonl"
