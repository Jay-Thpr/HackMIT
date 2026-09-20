"""Clone-investigation evidence for C6.

This is deliberately adapter-driven: Brain receives a public ``CloneLab`` plus
clone observations/probes supplied by the product layer. It never sees C5,
hidden world labels, Docker, or a fault controller.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from math import ceil

from faultline_contracts import (
    CloneInfo,
    CloneLab,
    CloneSpec,
    CloneStatus,
    Experiment,
    Fingerprint,
    LabActionHandle,
    Phase,
    TriageResult,
)

from .noise import NoiseModel

DEFAULT_MATCH_Z = 3.0

# The metrics an incident is judged on: db health, the orders->payments retry path,
# orders latency, and the checkout SLO. Edge/gateway counters are too noisy to compare.
KEY_METRICS = frozenset(
    {
        "db.qps",
        "db.query_p50_ms",
        "db.query_p99_ms",
        "db.pool_busy_ratio",
        "svc.orders.retry_ratio",
        "svc.orders.error_rate",
        "svc.orders.p99_ms",
        "slo.checkout.value",
    }
)


def sigma_floor(metric: str) -> float:
    """Absolute lower bound for a metric's sigma in clone-vs-production comparisons.

    Clone windows are single samples and production baselines can be near-degenerate
    (an error_rate pinned at 0 has measured std 0), so the relative noise floor alone
    makes healthy jitter look like a breach. These floors are in canonical units.
    """
    if metric.endswith("_ms"):
        return 10.0
    if metric.endswith(".qps"):
        return 5.0
    if metric.endswith("retry_ratio"):
        return 0.25
    if metric.endswith("_rate") or metric.endswith("_ratio"):
        return 0.05
    if metric.startswith("slo.") and metric.endswith(".value"):
        return 10.0
    return 0.0


@dataclass(frozen=True)
class LabExperiment:
    """A clone-only intervention selected by one hypothesis investigator."""

    action: str
    params: dict
    ttl_s: int
    observe_after_s: float = 0.0
    recovery_wait_s: float = 0.0

    def recipe(self) -> dict:
        """C6 patch-verifier recipe built from an investigator's measured action."""
        return {"action": self.action, "params": dict(self.params), "ttl_s": self.ttl_s}


@dataclass(frozen=True)
class SimilarityEvidence:
    """Noise-normalized comparison of an observed clone window to a reference."""

    shared_metrics: int
    matching_metrics: int
    mean_abs_z: float
    matches: bool
    z_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ReproductionEvidence:
    hypothesis_id: str
    clone_id: str
    action: LabExperiment
    similarity: SimilarityEvidence
    reproduced: bool


@dataclass(frozen=True)
class RecoveryEvidence:
    clone_id: str
    similarity: SimilarityEvidence
    recovered: bool


@dataclass(frozen=True)
class CloneProbe:
    """Measured C3 experiment windows from a clone; Product owns executing it."""

    healthy_baseline: list[Fingerprint]
    incident_baseline: list[Fingerprint]
    during: list[Fingerprint]
    after_release: list[Fingerprint]


@dataclass(frozen=True)
class PredictionEvidence:
    hypothesis_id: str
    experiment_id: str
    measured_expectations: int
    matched_expectations: int

    @property
    def predicts(self) -> bool:
        return (
            self.measured_expectations > 0
            and self.matched_expectations >= ceil(0.75 * self.measured_expectations)
        )


@dataclass(frozen=True)
class InvestigationEvidence:
    reproduction: ReproductionEvidence
    recovery: RecoveryEvidence
    prediction: PredictionEvidence | None

    @property
    def survives_falsification(self) -> bool:
        return (
            self.reproduction.reproduced
            and self.recovery.recovered
            and (self.prediction is None or self.prediction.predicts)
        )


CloneObserver = Callable[[CloneInfo], Fingerprint]
CloneProbeRunner = Callable[[CloneInfo, Experiment], CloneProbe]


def floored_sigma(model: NoiseModel, metric: str) -> float | None:
    """The model's sigma widened to the absolute floor for this metric's units."""
    sigma = model.sigma(metric)
    return None if sigma is None else max(sigma, sigma_floor(metric))


def _z(model: NoiseModel, metric: str, measured: float) -> float:
    sigma = floored_sigma(model, metric)
    baseline = model.baseline(metric)
    if sigma is None or baseline is None:
        raise KeyError(metric)
    if sigma == 0:
        return float("inf") if measured != baseline else 0.0
    return (measured - baseline) / sigma


def similarity(
    reference: Fingerprint | list[Fingerprint],
    observed: Fingerprint,
    *,
    threshold_z: float = DEFAULT_MATCH_Z,
    metrics: frozenset[str] | None = KEY_METRICS,
) -> SimilarityEvidence:
    """Compare shared C1 metrics using the reference's measured noise floor.

    ``reference`` may be one fingerprint or a list of windows (a multi-window
    baseline measures real variance). Only ``metrics`` keys present in both the
    reference model and ``observed`` are compared (``metrics=None`` compares all
    shared metrics). A clone matches when at least three quarters of the shared
    metrics are within ``threshold_z`` of the floored-sigma baseline.
    """
    references = [reference] if isinstance(reference, Fingerprint) else list(reference)
    model = NoiseModel.from_windows(references)
    z_scores: dict[str, float] = {}
    for metric, value in observed.metrics().items():
        if value is None or model.baseline(metric) is None:
            continue
        if metrics is not None and metric not in metrics:
            continue
        z_scores[metric] = abs(_z(model, metric, value))
    matching = sum(score < threshold_z for score in z_scores.values())
    shared = len(z_scores)
    return SimilarityEvidence(
        shared_metrics=shared,
        matching_metrics=matching,
        mean_abs_z=sum(z_scores.values()) / shared if shared else float("inf"),
        matches=shared >= 1 and matching >= ceil(0.75 * shared),
        z_scores=z_scores,
    )


def score_clone_prediction(
    triage: TriageResult,
    hypothesis_id: str,
    experiment: Experiment,
    probe: CloneProbe,
) -> PredictionEvidence:
    """Check one hypothesis's C2 directions against measured clone windows."""
    prediction = next(
        (
            item
            for item in triage.predictions
            if item.hypothesis_id == hypothesis_id and item.experiment_id == experiment.id
        ),
        None,
    )
    if prediction is None:
        raise ValueError(f"no prediction for {hypothesis_id}/{experiment.id}")
    healthy_baseline = NoiseModel.from_windows(probe.healthy_baseline)
    incident_baseline = NoiseModel.from_windows(probe.incident_baseline)
    measured = matched = 0
    for _phase, expectations, windows, baseline in (
        (Phase.during, prediction.during, probe.during, incident_baseline),
        (Phase.after_release, prediction.after_release, probe.after_release, healthy_baseline),
    ):
        for expectation in expectations:
            values = [fp.metrics().get(expectation.metric) for fp in windows]
            values = [value for value in values if value is not None]
            if not values or baseline.baseline(expectation.metric) is None:
                continue
            observed = sum(values) / len(values)
            measured += 1
            if baseline.direction(expectation.metric, observed) == expectation.direction:
                matched += 1
    return PredictionEvidence(hypothesis_id, experiment.id, measured, matched)


class CloneInvestigator:
    """Execute one hypothesis's reproduce → recover → predict evidence loop.

    A caller may run one instance per hypothesis concurrently. Cleanup is in a
    ``finally`` block so an investigator never leaves an aggressive clone action
    running or consumes a clone slot after an observation/probe failure.
    """

    def __init__(
        self,
        lab: CloneLab,
        observe: CloneObserver,
        wait: Callable[[float], None] | None = None,
    ):
        self._lab = lab
        self._observe = observe
        self._wait = wait or (lambda _seconds: None)

    def investigate(
        self,
        hypothesis_id: str,
        spec: CloneSpec,
        production_incident: Fingerprint | list[Fingerprint],
        healthy_reference: Fingerprint | list[Fingerprint],
        experiment: LabExperiment,
        *,
        triage: TriageResult | None = None,
        production_probe: Experiment | None = None,
        run_probe: CloneProbeRunner | None = None,
    ) -> InvestigationEvidence:
        clone = self._lab.create(spec)
        if clone.status != CloneStatus.ready or clone.endpoints is None:
            raise RuntimeError(f"clone {clone.clone_id} was not ready for investigation")
        handle: LabActionHandle | None = None
        try:
            handle = self._lab.apply(clone.clone_id, experiment.action, experiment.params, experiment.ttl_s)
            self._wait(experiment.observe_after_s)
            reproduced = similarity(production_incident, self._observe(clone))
            reproduction = ReproductionEvidence(
                hypothesis_id, clone.clone_id, experiment, reproduced, reproduced.matches
            )
            self._lab.undo(handle)
            handle = None
            self._wait(experiment.recovery_wait_s)
            recovery = similarity(healthy_reference, self._observe(clone))
            recovery_evidence = RecoveryEvidence(clone.clone_id, recovery, recovery.matches)
            probe_evidence = None
            if triage is not None or production_probe is not None or run_probe is not None:
                if triage is None or production_probe is None or run_probe is None:
                    raise ValueError("triage, production_probe, and run_probe must be supplied together")
                probe_evidence = score_clone_prediction(
                    triage,
                    hypothesis_id,
                    production_probe,
                    run_probe(clone, production_probe),
                )
            return InvestigationEvidence(reproduction, recovery_evidence, probe_evidence)
        finally:
            if handle is not None:
                self._lab.undo(handle)
            self._lab.reset(clone.clone_id)
            self._lab.destroy(clone.clone_id)
