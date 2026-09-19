"""Turn measured phase responses into a contract ``Verdict``."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp
from statistics import fmean

from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.triage import (
    NONE_OF_THE_ABOVE,
    ConfirmExpect,
    Direction,
    HypothesisSupport,
    Observation,
    Phase,
    TriageDraft,
    Verdict,
)

from .noise import NoiseModel


@dataclass(frozen=True)
class PhaseMeasurement:
    """Windows collected in one experimental phase.

    ``reference`` is normally the incident fingerprint immediately before the
    lever was applied.  It lets a "down" prediction during an intervention
    mean a real improvement from the incident.  After release, every result is
    deliberately compared with healthy baseline: that is the counterfactual
    the experiment is meant to expose.
    """

    phase: Phase
    fingerprints: list[Fingerprint]
    reference: Fingerprint | None = None


class Judge:
    def __init__(self, z_threshold: float = 3.0):
        if z_threshold <= 0:
            raise ValueError("z_threshold must be positive")
        self.z_threshold = z_threshold

    def observe(self, noise: NoiseModel, experiment_id: str, measurement: PhaseMeasurement) -> list[Observation]:
        values: dict[str, list[float]] = {}
        for fingerprint in measurement.fingerprints:
            for metric, value in fingerprint.metrics().items():
                if noise.get(metric) is not None:
                    values.setdefault(metric, []).append(value)
        reference = measurement.reference.metrics() if measurement.reference and measurement.phase == Phase.during else {}
        observations: list[Observation] = []
        for metric, samples in sorted(values.items()):
            estimate = noise.require(metric)
            measured = fmean(samples)
            baseline = reference.get(metric, estimate.baseline)
            z = noise.z_score(metric, measured, baseline)
            direction = self._direction(z)
            observations.append(
                Observation(
                    experiment_id=experiment_id,
                    metric=metric,
                    phase=measurement.phase,
                    baseline=baseline,
                    measured=measured,
                    sigma=estimate.sigma,
                    z=z,
                    direction=direction,
                )
            )
        return observations

    def verdict(
        self,
        incident_id: str,
        draft: TriageDraft,
        experiment_id: str,
        noise: NoiseModel,
        measurements: list[PhaseMeasurement],
    ) -> Verdict:
        observations = [observation for measurement in measurements for observation in self.observe(noise, experiment_id, measurement)]
        lookup = {(item.phase, item.metric): item for item in observations}
        raw_scores: dict[str, float] = {}
        confirmations: dict[str, bool] = {}
        for hypothesis in draft.hypotheses:
            prediction = next(
                (item for item in draft.predictions if item.hypothesis_id == hypothesis.id and item.experiment_id == experiment_id),
                None,
            )
            if prediction is None:
                raw_scores[hypothesis.id] = -1.0
                confirmations[hypothesis.id] = False
                continue
            agreements: list[float] = []
            for phase, expected in ((Phase.during, prediction.during), (Phase.after_release, prediction.after_release)):
                for expectation in expected:
                    observed = lookup.get((phase, expectation.metric))
                    if observed is None:
                        continue
                    agreements.append(self._agreement(expectation.direction, observed.direction))
            raw_scores[hypothesis.id] = fmean(agreements) if agreements else -1.0
            confirmation = lookup.get((prediction.confirms_if.phase, prediction.confirms_if.metric))
            confirmations[hypothesis.id] = self._confirms(prediction.confirms_if.expect, confirmation, noise)

            # A confirmation is a hypothesis' explicit positive test, not just
            # one more directional hint.  Give it decisive but transparent
            # weight so an early, shared improvement cannot outrank the
            # hypothesis whose predicted release behaviour actually occurred.
            if confirmations[hypothesis.id]:
                raw_scores[hypothesis.id] += 2.0

        supports = self._normalize(raw_scores, confirmations)
        confirmed_ids = [hypothesis_id for hypothesis_id, ok in confirmations.items() if ok]
        leader = max(supports, key=lambda item: item.support)
        diagnosis = leader.hypothesis_id if leader.hypothesis_id in confirmed_ids else NONE_OF_THE_ABOVE
        return Verdict(
            incident_id=incident_id,
            diagnosis=diagnosis,
            confirmed=diagnosis != NONE_OF_THE_ABOVE,
            support=supports,
            observations=observations,
            summary="Measured response passed a hypothesis confirmation test." if diagnosis != NONE_OF_THE_ABOVE else "No hypothesis passed its confirmation test.",
        )

    def _direction(self, z: float) -> Direction:
        if z >= self.z_threshold:
            return Direction.up
        if z <= -self.z_threshold:
            return Direction.down
        return Direction.flat

    @staticmethod
    def _agreement(expected: Direction, observed: Direction) -> float:
        if expected == observed:
            return 1.0
        if observed == Direction.flat:
            return 0.0
        return -1.0

    def _confirms(self, expected: ConfirmExpect, observed: Observation | None, noise: NoiseModel) -> bool:
        if observed is None:
            return False
        if expected == ConfirmExpect.within_baseline:
            healthy_z = noise.z_score(observed.metric, observed.measured)
            return abs(healthy_z) < self.z_threshold
        return observed.direction == Direction(expected.value)

    @staticmethod
    def _normalize(scores: dict[str, float], confirmations: dict[str, bool]) -> list[HypothesisSupport]:
        maximum = max(scores.values())
        weights = {key: exp(value - maximum) for key, value in scores.items()}
        total = sum(weights.values())
        return [
            HypothesisSupport(hypothesis_id=key, support=weights[key] / total, confirmed=confirmations[key])
            for key in scores
        ]
