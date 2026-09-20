"""C2 — Hypotheses, predictions (LLM output) and verdicts (math output).

`TriageDraft` is the exact shape requested from the OpenAI API with strict structured
outputs: no free-form dicts, no defaults, every field required.
"""

from datetime import datetime
from enum import Enum

from pydantic import Field

from .common import SCHEMA_VERSION, Model, utcnow
from .metrics import unknown_metrics

NONE_OF_THE_ABOVE = "none_of_the_above"


class Direction(str, Enum):
    up = "up"
    down = "down"
    flat = "flat"


class Phase(str, Enum):
    during = "during"  # while the lever is applied
    after_release = "after_release"  # after the lever is undone


class ConfirmExpect(str, Enum):
    within_baseline = "within_baseline"  # back to healthy-baseline levels (within ~3 sigma)
    up = "up"
    down = "down"
    flat = "flat"


# ---- LLM output (strict) -------------------------------------------------------------


class MetricExpectation(Model):
    metric: str
    direction: Direction


class Confirmation(Model):
    phase: Phase
    metric: str
    expect: ConfirmExpect


class Hypothesis(Model):
    id: str  # e.g. "H_meta"
    label: str  # short name, e.g. "Self-sustaining retry storm"
    description: str  # names trigger and sustaining cause
    evidence: list[str]


class Prediction(Model):
    hypothesis_id: str
    experiment_id: str
    during: list[MetricExpectation]
    after_release: list[MetricExpectation]
    # Diagnostic experiments can separate hypotheses without being a direct,
    # causal confirmation of either one. Every ambiguous hypothesis must still
    # have at least one non-null confirmation across its predictions.
    confirms_if: Confirmation | None


class TriageDraft(Model):
    ambiguous: bool  # False => obvious, skip experiments
    reasoning: str
    hypotheses: list[Hypothesis]
    predictions: list[Prediction]

    def problems(self, known_metrics: set[str] | None = None, experiment_ids: set[str] | None = None) -> list[str]:
        """Validation beyond the schema. Empty list = OK."""
        errs: list[str] = []
        hyp_ids = {h.id for h in self.hypotheses}
        if len(hyp_ids) != len(self.hypotheses):
            errs.append("duplicate hypothesis ids")
        if NONE_OF_THE_ABOVE in hyp_ids:
            errs.append(f"'{NONE_OF_THE_ABOVE}' is reserved")
        for p in self.predictions:
            if p.hypothesis_id not in hyp_ids:
                errs.append(f"prediction for unknown hypothesis {p.hypothesis_id!r}")
            if experiment_ids is not None and p.experiment_id not in experiment_ids:
                errs.append(f"prediction for unknown experiment {p.experiment_id!r}")
            keys = [m.metric for m in p.during + p.after_release]
            if p.confirms_if is not None:
                keys.append(p.confirms_if.metric)
            for k in unknown_metrics(keys, known_metrics):
                errs.append(f"unknown metric {k!r} in prediction {p.hypothesis_id}/{p.experiment_id}")
        if self.ambiguous:
            confirmed_hypotheses = {
                prediction.hypothesis_id
                for prediction in self.predictions
                if prediction.confirms_if is not None
            }
            for hypothesis_id in sorted(hyp_ids - confirmed_hypotheses):
                errs.append(f"ambiguous hypothesis {hypothesis_id!r} has no positive confirmation test")
        return errs


class TriageResult(TriageDraft):
    schema_version: str = SCHEMA_VERSION
    incident_id: str
    created_at: datetime = Field(default_factory=utcnow)


# ---- Math output ---------------------------------------------------------------------


class Observation(Model):
    experiment_id: str
    metric: str
    phase: Phase
    baseline: float
    measured: float
    sigma: float
    z: float  # (measured - baseline) / sigma
    direction: Direction  # measured direction; flat if |z| < threshold


class HypothesisSupport(Model):
    hypothesis_id: str
    support: float  # normalized, sums to 1 across hypotheses
    confirmed: bool | None = None  # None = confirmation test not run yet


class Verdict(Model):
    schema_version: str = SCHEMA_VERSION
    incident_id: str
    diagnosis: str  # a hypothesis id, or NONE_OF_THE_ABOVE
    confirmed: bool
    support: list[HypothesisSupport]
    observations: list[Observation]
    summary: str = ""
    created_at: datetime = Field(default_factory=utcnow)
