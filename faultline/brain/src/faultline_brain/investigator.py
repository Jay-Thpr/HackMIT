"""Clone-investigation evidence for C6.

This is deliberately adapter-driven: Brain receives a public ``CloneLab`` plus
clone observations/probes supplied by the product layer. It never sees C5,
hidden world labels, Docker, or a fault controller.
"""

from collections.abc import Callable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class LabExperiment:
    """A clone-only intervention selected by one hypothesis investigator."""

    action: str
    params: dict
    ttl_s: int


@dataclass(frozen=True)
class SimilarityEvidence:
    """Noise-normalized comparison of an observed clone window to a reference."""

    shared_metrics: int
    matching_metrics: int
    mean_abs_z: float
    matches: bool


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
        return self.measured_expectations > 0 and self.matched_expectations == self.measured_expectations


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


def similarity(reference: Fingerprint, observed: Fingerprint, *, threshold_z: float = DEFAULT_MATCH_Z) -> SimilarityEvidence:
    """Compare shared C1 metrics using the reference's measured noise floor.

    Missing telemetry remains omitted. A clone matches when every shared metric
    is within ``threshold_z`` and at least one metric is available; callers can
    choose a narrower fingerprint before invoking this function if desired.
    """
    model = NoiseModel.from_windows([reference])
    z_scores = []
    for metric, value in observed.metrics().items():
        if value is None or model.baseline(metric) is None:
            continue
        z_scores.append(abs(model.z(metric, value)))
    matching = sum(score < threshold_z for score in z_scores)
    return SimilarityEvidence(
        shared_metrics=len(z_scores),
        matching_metrics=matching,
        mean_abs_z=sum(z_scores) / len(z_scores) if z_scores else float("inf"),
        matches=bool(z_scores) and matching == len(z_scores),
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

    def __init__(self, lab: CloneLab, observe: CloneObserver):
        self._lab = lab
        self._observe = observe

    def investigate(
        self,
        hypothesis_id: str,
        spec: CloneSpec,
        production_incident: Fingerprint,
        healthy_reference: Fingerprint,
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
            reproduced = similarity(production_incident, self._observe(clone))
            reproduction = ReproductionEvidence(
                hypothesis_id, clone.clone_id, experiment, reproduced, reproduced.matches
            )
            self._lab.undo(handle)
            handle = None
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
