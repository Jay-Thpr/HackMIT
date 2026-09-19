"""C2 experiment planner: maximize predicted separation for minimal user impact.

The C2 prediction contract records directions, not expected numeric magnitudes.
Accordingly, separation is the count of phase/metric predictions on which two or
more hypotheses disagree. The judge, using the noise model, decides whether a
measured change is large enough to count.
"""

from dataclasses import dataclass

from faultline_contracts.levers import Experiment
from faultline_contracts.triage import Direction, Phase, Prediction, TriageResult


@dataclass(frozen=True)
class ExperimentScore:
    """Auditable score for one candidate experiment."""

    experiment: Experiment
    separation: int
    divergent_metrics: tuple[tuple[Phase, str], ...]
    score: float


@dataclass(frozen=True)
class Plan:
    """Ranked candidate experiments; `selected` is None when none separates."""

    selected: Experiment | None
    scores: tuple[ExperimentScore, ...]


def _directions_by_hypothesis(
    predictions: list[Prediction], experiment_id: str
) -> dict[str, dict[tuple[Phase, str], Direction]]:
    out: dict[str, dict[tuple[Phase, str], Direction]] = {}
    for prediction in predictions:
        if prediction.experiment_id != experiment_id:
            continue
        measures = out.setdefault(prediction.hypothesis_id, {})
        for phase, expectations in (
            (Phase.during, prediction.during),
            (Phase.after_release, prediction.after_release),
        ):
            for expectation in expectations:
                measures[(phase, expectation.metric)] = expectation.direction
    return out


def score_experiment(
    predictions: list[Prediction], experiment: Experiment, *, blast_penalty: float = 0.1
) -> ExperimentScore:
    """Score an experiment as direction separation minus user-impact cost.

    A metric is separating only when it is predicted by at least two hypotheses
    and those hypotheses assign different directions in the same phase.
    """
    if blast_penalty < 0:
        raise ValueError("blast_penalty must be non-negative")
    per_hypothesis = _directions_by_hypothesis(predictions, experiment.id)
    all_metrics = set().union(*(directions.keys() for directions in per_hypothesis.values())) if per_hypothesis else set()
    divergent = []
    for metric in all_metrics:
        directions = {
            values[metric]
            for values in per_hypothesis.values()
            if metric in values
        }
        if len(directions) > 1:
            divergent.append(metric)
    divergent.sort(key=lambda item: (item[0].value, item[1]))
    separation = len(divergent)
    return ExperimentScore(
        experiment=experiment,
        separation=separation,
        divergent_metrics=tuple(divergent),
        score=separation - blast_penalty * experiment.blast_radius_pct,
    )


def plan_experiment(
    triage: TriageResult, candidates: list[Experiment], *, blast_penalty: float = 0.1
) -> Plan:
    """Rank C3 candidates and choose the highest positive-separation experiment.

    Ties favor lower blast radius, then a stable lexical experiment id. C2 only
    proposes the experiment; the orchestrator owns the eventual C3 action.
    """
    scores = [score_experiment(triage.predictions, candidate, blast_penalty=blast_penalty) for candidate in candidates]
    ordered = sorted(
        scores,
        key=lambda item: (-item.score, -item.separation, item.experiment.blast_radius_pct, item.experiment.id),
    )
    selected = ordered[0].experiment if ordered and ordered[0].separation > 0 else None
    return Plan(selected=selected, scores=tuple(ordered))


def confirmation_experiment(
    triage: TriageResult,
    hypothesis_id: str,
    candidates: list[Experiment],
    *,
    excluded_ids: set[str] | None = None,
) -> Experiment | None:
    """Choose the gentlest untried direct confirmation probe for a leading cause.

    A first experiment may be diagnostic-only (for example, a retry cap). Once
    it points to a hypothesis, this selects an experiment whose prediction has
    a non-null positive confirmation. C2 deliberately keeps that distinction in
    the triage contract rather than inferring causality from a lever name.
    """
    excluded_ids = excluded_ids or set()
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    eligible = {
        prediction.experiment_id
        for prediction in triage.predictions
        if prediction.hypothesis_id == hypothesis_id
        and prediction.confirms_if is not None
        and prediction.experiment_id not in excluded_ids
        and prediction.experiment_id in candidate_by_id
    }
    if not eligible:
        return None
    return min(
        (candidate_by_id[experiment_id] for experiment_id in eligible),
        key=lambda item: (item.blast_radius_pct, item.id),
    )
