"""Write JSON Schema for the contract models into contracts/schema/<Name>.json.

Run: uv run python scripts/export_schemas.py
"""

import json
from pathlib import Path

from faultline_contracts import ActionHandle, AuditEvent, Experiment, Fingerprint, LeverSpec, TriageDraft, TriageResult, Verdict
from faultline_contracts.fault import CpuStarveFault, DegradeDbFault, FaultState, StormFault

MODELS = [Fingerprint, TriageDraft, TriageResult, Verdict, LeverSpec, Experiment, ActionHandle, AuditEvent,
          FaultState, StormFault, DegradeDbFault, CpuStarveFault]
OUT = Path(__file__).resolve().parent.parent / "schema"


def render(model) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def export_all(out_dir: Path = OUT) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for m in MODELS:
        p = out_dir / f"{m.__name__}.json"
        p.write_text(render(m))
        paths.append(p)
    return paths


if __name__ == "__main__":
    for p in export_all():
        print(p)
