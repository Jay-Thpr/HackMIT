"""Choose the smallest experiment whose predictions separate hypotheses."""

from __future__ import annotations

from dataclasses import dataclass

from faultline_contracts.levers import Experiment
from faultline_contracts.triage import Direction, Phase, TriageDraft


@dataclass(frozen=True)
class ExperimentScore:
    experiment_id: str
    separation: int
    blast_radius_pct: float
    score: float
    discriminating_metrics: tuple[str, ...]


@dataclass(frozen=True)
class ExperimentPlan:
    experiment: Experiment
    score: ExperimentScore


class ExperimentPlanner:
    """Ranks only LLM-proposed, contract-valid experiments.

    Separation counts metric/phase pairs for which at least two hypotheses make
    different directional predictions.  This avoids using any hidden-world
    information while favouring an intervention that can actually distinguish
    the candidates.  The small blast penalty breaks ties in favour of gentler
    actions.
    """

    def __init__(self, blast_penalty: float = 0.10):
        if blast_penalty < 0:
            raise ValueError("blast_penalty must be non-negative")
        self.blast_penalty = blast_penalty

    def rank(self, draft: TriageDraft, experiments: list[Experiment]) -> list[ExperimentScore]:
        predictions = {(p.hypothesis_id, p.experiment_id): p for p in draft.predictions}
        scores: list[ExperimentScore] = []
        for experiment in experiments:
            by_signal: dict[tuple[Phase, str], set[Direction]] = {}
            for hypothesis in draft.hypotheses:
                prediction = predictions.get((hypothesis.id, experiment.id))
                if prediction is None:
                    continue
                for phase, expectations in (
                    (Phase.during, prediction.during),
                    (Phase.after_release, prediction.after_release),
                ):
                    for expectation in expectations:
                        by_signal.setdefault((phase, expectation.metric), set()).add(expectation.direction)
            signals = tuple(
                f"{phase.value}:{metric}"
                for (phase, metric), directions in sorted(by_signal.items(), key=lambda item: (item[0][0].value, item[0][1]))
                if len(directions) > 1
            )
            separation = len(signals)
            scores.append(
                ExperimentScore(
                    experiment_id=experiment.id,
                    separation=separation,
                    blast_radius_pct=experiment.blast_radius_pct,
                    score=separation - self.blast_penalty * experiment.blast_radius_pct,
                    discriminating_metrics=signals,
                )
            )
        return sorted(scores, key=lambda item: (-item.score, -item.separation, item.blast_radius_pct, item.experiment_id))

    def choose(self, draft: TriageDraft, experiments: list[Experiment]) -> ExperimentPlan:
        if not experiments:
            raise ValueError("cannot plan without experiments")
        ranked = self.rank(draft, experiments)
        best = ranked[0]
        if best.separation == 0:
            raise ValueError("no candidate experiment separates the proposed hypotheses")
        experiment = next(item for item in experiments if item.id == best.experiment_id)
        return ExperimentPlan(experiment=experiment, score=best)
