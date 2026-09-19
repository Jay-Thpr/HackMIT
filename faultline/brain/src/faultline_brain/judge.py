"""C2 — Judge: the math verdict. No LLM involved.

Turns a triage result (hypotheses + predictions from the LLM) plus the telemetry
recorded during and after an experiment into a `Verdict`: per-metric `Observation`s,
normalized `HypothesisSupport`, and a `diagnosis` that only stands if the leading
hypothesis's own `confirms_if` actually passes.

Two reference `NoiseModel`s are supplied by the caller (not built here), because
"where the healthy prefix ends and the incident begins" is a detection-stage
question, not a judging-stage one:

- `healthy_baseline`: NoiseModel over the healthy prefix windows. Used for
  `after_release` observations and for `within_baseline` confirmation checks
  (the question there is "did it go back to how it was before the incident?").
- `incident_baseline`: NoiseModel over the incident windows immediately before
  `experiment_start`. Used for `during` observations (the question there is
  "did the lever move it off the incident's own level?").
"""

from datetime import datetime

from faultline_contracts.audit import ExperimentWindow
from faultline_contracts.fingerprint import Fingerprint
from faultline_contracts.triage import (
    NONE_OF_THE_ABOVE,
    ConfirmExpect,
    HypothesisSupport,
    MetricExpectation,
    Observation,
    Phase,
    TriageResult,
    Verdict,
)

from .noise import NoiseModel

# Likelihood ratio applied per matching (vs. mismatching) directional expectation
# when accumulating hypothesis support. This is a simple Bayesian-odds update over
# a uniform prior: each independent confirming measurement multiplies a
# hypothesis's relative odds by this factor, each contradicting one divides by it.
# It is a design choice, not a fitted statistic -- it only needs to separate
# hypotheses whose evidence disagrees, which a plain match/mismatch tally does not
# do sharply enough once one hypothesis has any contradicting evidence at all.
AGREEMENT_ODDS = 2.0

# A lever can take several seconds to drain queued work.  During-phase evidence
# should describe the settled response to the lever, rather than averaging that
# response away with the transition immediately after it was applied.
DURING_SETTLED_FRACTION = 0.5


def phase_fingerprints(
    series: list[Fingerprint], start: datetime, end: datetime | None
) -> list[Fingerprint]:
    """Fingerprints whose window_start falls in [start, end).

    `end=None` means open-ended: everything from `start` to the end of `series`.
    Used to slice a telemetry series into the "during" window ([start, release))
    and the "after_release" window ([release, ...)) of an experiment.
    """
    return [
        fp
        for fp in series
        if fp.window_start >= start and (end is None or fp.window_start < end)
    ]


def _measure(fps: list[Fingerprint], metric: str) -> float | None:
    """Mean value of `metric` across `fps`, or None if never present."""
    values = [v for fp in fps for k, v in fp.metrics().items() if k == metric]
    if not values:
        return None
    return sum(values) / len(values)


def _settled_during(fps: list[Fingerprint]) -> list[Fingerprint]:
    """Return the trailing half of a during-phase series.

    Telemetry windows are aggregates, so the first window(s) after applying a
    lever can still contain the pre-action backlog.  We measure the settled end
    of a bounded hold instead.  One-window holds remain measurable.
    """
    if not fps:
        return []
    start = max(0, len(fps) - max(1, round(len(fps) * DURING_SETTLED_FRACTION)))
    return fps[start:]


def _observe(
    experiment_id: str,
    metric: str,
    phase: Phase,
    measured: float,
    baseline_model: NoiseModel,
) -> Observation | None:
    """Build an Observation for `metric` at `phase`, or None if the baseline model
    has no reference for this metric (e.g. it never appeared in the reference
    windows)."""
    baseline = baseline_model.baseline(metric)
    sigma = baseline_model.sigma(metric)
    if baseline is None or sigma is None:
        return None
    return Observation(
        experiment_id=experiment_id,
        metric=metric,
        phase=phase,
        baseline=baseline,
        measured=measured,
        sigma=sigma,
        z=baseline_model.z(metric, measured),
        direction=baseline_model.direction(metric, measured),
    )


def judge(
    triage: TriageResult,
    series: list[Fingerprint],
    windows: list[ExperimentWindow],
    healthy_baseline: NoiseModel,
    incident_baseline: NoiseModel,
) -> Verdict:
    """Produce the math Verdict for one incident's triage result.

    Args:
        triage: LLM-authored hypotheses + predictions to check.
        series: Telemetry fingerprints spanning at least every experiment window
            referenced by `triage.predictions` (during and after_release).
        windows: Experiment phase boundaries, e.g. from
            `faultline_contracts.audit.experiment_windows(events)`, or built
            explicitly. Predictions whose `experiment_id` has no window, or whose
            window never released, are skipped -- there is no telemetry to judge
            them against yet.
        healthy_baseline: NoiseModel built from the healthy prefix of the series.
        incident_baseline: NoiseModel built from the incident windows
            immediately preceding the first experiment's start.

    Returns:
        Verdict with observations for every measurable expectation, normalized
        support per hypothesis, and a diagnosis that is only a hypothesis id if
        that hypothesis both leads on support and passes its own confirms_if.
    """
    window_by_id = {w.experiment_id: w for w in windows}

    observations: list[Observation] = []
    obs_index: dict[tuple[str, Phase, str], Observation] = {}
    # hypothesis_id -> [agreements, mismatches]
    tally: dict[str, list[int]] = {h.id: [0, 0] for h in triage.hypotheses}

    for pred in triage.predictions:
        window = window_by_id.get(pred.experiment_id)
        if window is None or window.release is None:
            continue  # experiment never ran (or never released): nothing to judge

        during_fps = phase_fingerprints(series, window.start, window.release)
        settled_during_fps = _settled_during(during_fps)
        after_fps = phase_fingerprints(series, window.release, None)
        plan: list[tuple[MetricExpectation, Phase, list[Fingerprint], NoiseModel]] = [
            *((e, Phase.during, settled_during_fps, incident_baseline) for e in pred.during),
            *((e, Phase.after_release, after_fps, healthy_baseline) for e in pred.after_release),
        ]

        for expectation, phase, fps, baseline_model in plan:
            measured = _measure(fps, expectation.metric)
            if measured is None:
                continue
            obs = _observe(pred.experiment_id, expectation.metric, phase, measured, baseline_model)
            if obs is None:
                continue

            observations.append(obs)
            obs_index[(pred.experiment_id, phase, expectation.metric)] = obs

            bucket = tally.setdefault(pred.hypothesis_id, [0, 0])
            if obs.direction == expectation.direction:
                bucket[0] += 1
            else:
                bucket[1] += 1

    support_by_id = _score_support(triage, tally)
    diagnosis, leader_id, leader_confirmed = _confirm(
        triage, support_by_id, obs_index, healthy_baseline
    )

    support = [
        HypothesisSupport(
            hypothesis_id=hid,
            support=support_by_id[hid],
            confirmed=leader_confirmed if hid == leader_id else None,
        )
        for hid in support_by_id
    ]

    return Verdict(
        incident_id=triage.incident_id,
        diagnosis=diagnosis,
        confirmed=leader_confirmed,
        support=support,
        observations=observations,
        summary=_summarize(diagnosis, leader_confirmed, observations),
    )


def _score_support(
    triage: TriageResult, tally: dict[str, list[int]]
) -> dict[str, float]:
    """Uniform prior over hypotheses, updated by a Bayesian-odds factor
    (AGREEMENT_ODDS) per agreeing/disagreeing directional expectation, then
    normalized to sum to 1."""
    hypothesis_ids = [h.id for h in triage.hypotheses] or list(tally.keys())
    n = max(len(hypothesis_ids), 1)
    prior = 1.0 / n

    raw = {}
    for hid in hypothesis_ids:
        agreements, mismatches = tally.get(hid, [0, 0])
        raw[hid] = prior * (AGREEMENT_ODDS ** (agreements - mismatches))

    total = sum(raw.values()) or 1.0
    return {hid: value / total for hid, value in raw.items()}


def _confirm(
    triage: TriageResult,
    support_by_id: dict[str, float],
    obs_index: dict[tuple[str, Phase, str], Observation],
    healthy_baseline: NoiseModel,
) -> tuple[str, str | None, bool]:
    """The hypothesis with max support is the diagnosis only if at least one of
    its own predictions has a confirms_if that passes against the measured
    observations. Otherwise diagnosis is NONE_OF_THE_ABOVE and confirmed=False."""
    if not support_by_id:
        return NONE_OF_THE_ABOVE, None, False

    leader_id = max(support_by_id, key=lambda hid: support_by_id[hid])

    for pred in triage.predictions:
        if pred.hypothesis_id != leader_id:
            continue
        confirms = pred.confirms_if
        obs = obs_index.get((pred.experiment_id, confirms.phase, confirms.metric))
        if obs is None:
            continue  # this experiment's confirms_if metric was never measured

        if confirms.expect == ConfirmExpect.within_baseline:
            passed = not healthy_baseline.is_significant(confirms.metric, obs.measured)
        else:
            passed = obs.direction.value == confirms.expect.value

        if passed:
            return leader_id, leader_id, True

    return NONE_OF_THE_ABOVE, leader_id, False


def _summarize(diagnosis: str, confirmed: bool, observations: list[Observation]) -> str:
    if diagnosis == NONE_OF_THE_ABOVE:
        return (
            f"No hypothesis confirmed across {len(observations)} observation(s); "
            "leading hypothesis failed its own confirms_if check."
        )
    verb = "confirmed" if confirmed else "led but was not confirmed"
    return f"{diagnosis} {verb} across {len(observations)} observation(s)."
