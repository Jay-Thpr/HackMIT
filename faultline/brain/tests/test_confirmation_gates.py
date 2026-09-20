"""What a confirms_if is allowed to confirm.

These cover the four ways the storm case in the first comparison pilot reached
`H_db confirmed` on a retry storm: a support tie broken by the order the model
listed its hypotheses, a passing confirmation on a non-leading hypothesis that
was never consulted, a confirmation probe that separated nothing, and a
confirmation that stood while the same experiment's release refuted it.
"""

from datetime import datetime, timedelta, timezone

import pytest
from faultline_contracts.audit import ExperimentWindow
from faultline_contracts.fingerprint import Fingerprint, ServiceStats
from faultline_contracts.levers import Experiment
from faultline_contracts.triage import (
    NONE_OF_THE_ABOVE,
    Confirmation,
    ConfirmExpect,
    Direction,
    Hypothesis,
    MetricExpectation,
    Phase,
    Prediction,
    TriageResult,
)

from faultline_brain.judge import judge
from faultline_brain.noise import NoiseModel
from faultline_brain.planner import confirmation_experiment, separates

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
METRIC = "svc.gateway.p99_ms"
OTHER = "svc.orders.retry_ratio"
HEALTHY, INCIDENT = 100.0, 1000.0
EXPERIMENT_START = START + timedelta(seconds=60)
RELEASE = EXPERIMENT_START + timedelta(seconds=20)


def _window(index: int, p99: float, retry: float) -> Fingerprint:
    start = START + timedelta(seconds=5 * index)
    return Fingerprint(
        window_start=start,
        window_end=start + timedelta(seconds=5),
        services={"gateway": ServiceStats(p99_ms=p99), "orders": ServiceStats(retry_ratio=retry)},
    )


def _series(during_p99: float, after_p99: float, after_retry: float = 1.0) -> list[Fingerprint]:
    healthy = [_window(i, HEALTHY, 1.0) for i in range(6)]
    incident = [_window(6 + i, INCIDENT, 4.0) for i in range(6)]
    during = [_window(12 + i, during_p99, 1.0) for i in range(4)]
    after = [_window(16 + i, after_p99, after_retry) for i in range(4)]
    return healthy + incident + during + after


def _prediction(hypothesis: str, experiment: str, during: list[tuple[str, Direction]],
                after: list[tuple[str, Direction]], confirms: Confirmation | None) -> Prediction:
    return Prediction(
        hypothesis_id=hypothesis,
        experiment_id=experiment,
        during=[MetricExpectation(metric=m, direction=d) for m, d in during],
        after_release=[MetricExpectation(metric=m, direction=d) for m, d in after],
        confirms_if=confirms,
    )


def _triage(predictions: list[Prediction], order: list[str]) -> TriageResult:
    return TriageResult(
        incident_id="inc-gate",
        ambiguous=True,
        reasoning="fixture",
        hypotheses=[Hypothesis(id=h, label=h, description=h, evidence=[]) for h in order],
        predictions=predictions,
    )


def _judge(triage: TriageResult, series: list[Fingerprint], experiment_ids=("probe",)):
    return judge(
        triage=triage,
        series=series,
        windows=[ExperimentWindow(experiment_id=e, start=EXPERIMENT_START, release=RELEASE) for e in experiment_ids],
        healthy_baseline=NoiseModel.from_windows(series[:6]),
        incident_baseline=NoiseModel.from_windows(series[6:12]),
    )


WITHIN_AFTER = Confirmation(phase=Phase.after_release, metric=METRIC, expect=ConfirmExpect.within_baseline)
WITHIN_DURING = Confirmation(phase=Phase.during, metric=METRIC, expect=ConfirmExpect.within_baseline)


def _tied_pair(order: list[str], db_confirms: Confirmation | None = None) -> TriageResult:
    """Both hypotheses land three matches and one mismatch, so support ties.

    They still disagree about the retry ratio, so the probe separates them; the
    measured series then decides only through the confirmation test.
    """
    return _triage(
        [
            _prediction("H_meta", "probe", [(METRIC, Direction.down), (OTHER, Direction.down)],
                        [(METRIC, Direction.flat), (OTHER, Direction.up)], WITHIN_AFTER),
            _prediction("H_db", "probe", [(METRIC, Direction.down), (OTHER, Direction.flat)],
                        [(METRIC, Direction.flat), (OTHER, Direction.flat)], db_confirms),
        ],
        order,
    )


@pytest.mark.parametrize("order", [["H_db", "H_meta"], ["H_meta", "H_db"]])
def test_tied_support_is_decided_by_confirmation_not_hypothesis_order(order):
    verdict = _judge(_tied_pair(order), _series(during_p99=HEALTHY, after_p99=HEALTHY))
    support = {s.hypothesis_id: s.support for s in verdict.support}

    assert support["H_meta"] == pytest.approx(support["H_db"])
    assert verdict.diagnosis == "H_meta"
    assert verdict.confirmed is True


def test_every_tied_leader_is_tested_and_reported():
    verdict = _judge(_tied_pair(["H_db", "H_meta"]), _series(during_p99=HEALTHY, after_p99=HEALTHY))
    confirmed = {s.hypothesis_id: s.confirmed for s in verdict.support}

    assert confirmed == {"H_meta": True, "H_db": False}


def test_two_tied_leaders_that_both_pass_confirm_neither():
    verdict = _judge(_tied_pair(["H_db", "H_meta"], db_confirms=WITHIN_AFTER),
                     _series(during_p99=HEALTHY, after_p99=HEALTHY))

    assert verdict.diagnosis == NONE_OF_THE_ABOVE
    assert verdict.confirmed is False


def test_experiment_that_separates_nothing_confirms_nothing():
    """Both stories predict the same response, so the measurement is not evidence."""
    triage = _triage(
        [
            _prediction("H_meta", "probe", [(METRIC, Direction.down)], [(METRIC, Direction.flat)], WITHIN_DURING),
            _prediction("H_db", "probe", [(METRIC, Direction.down)], [(METRIC, Direction.flat)], WITHIN_DURING),
        ],
        ["H_db", "H_meta"],
    )
    verdict = _judge(triage, _series(during_p99=HEALTHY, after_p99=HEALTHY))

    assert separates(triage.predictions, "probe") is False
    assert verdict.diagnosis == NONE_OF_THE_ABOVE
    assert verdict.confirmed is False


def _relief_claim(order: list[str]) -> TriageResult:
    """H_db claims the lever held its cause down while applied."""
    return _triage(
        [_prediction("H_db", "probe", [(METRIC, Direction.down)],
                     [(METRIC, Direction.up), (OTHER, Direction.up)], WITHIN_DURING)],
        order,
    )


def test_healthy_after_release_voids_a_relief_confirmation():
    """Healthy while held and still healthy once released: the lever proved nothing."""
    verdict = _judge(_relief_claim(["H_db"]), _series(during_p99=HEALTHY, after_p99=HEALTHY))

    assert verdict.diagnosis == NONE_OF_THE_ABOVE
    assert verdict.confirmed is False


def test_incident_returning_after_release_confirms_the_relief_hypothesis():
    """The same claim stands when removing the lever brings the incident back.

    The hypothesis's own after-release prediction for OTHER is contradicted here;
    that must not veto measured evidence this strong.
    """
    verdict = _judge(_relief_claim(["H_db"]), _series(during_p99=HEALTHY, after_p99=INCIDENT))

    assert verdict.diagnosis == "H_db"
    assert verdict.confirmed is True


def test_a_sole_hypothesis_still_confirms_without_a_rival_to_separate_from():
    triage = _triage(
        [_prediction("H_meta", "probe", [(METRIC, Direction.down)], [(METRIC, Direction.flat)], WITHIN_AFTER)],
        ["H_meta"],
    )
    verdict = _judge(triage, _series(during_p99=HEALTHY, after_p99=HEALTHY))

    assert separates(triage.predictions, "probe") is True
    assert verdict.diagnosis == "H_meta"
    assert verdict.confirmed is True


def test_confirmation_probe_must_separate_the_surviving_hypotheses():
    triage = _triage(
        [
            _prediction("H_db", "flat_probe", [(METRIC, Direction.down)], [(METRIC, Direction.up)], WITHIN_DURING),
            _prediction("H_meta", "flat_probe", [(METRIC, Direction.down)], [(METRIC, Direction.up)], None),
            _prediction("H_db", "sharp_probe", [(METRIC, Direction.down)], [(METRIC, Direction.up)], WITHIN_DURING),
            _prediction("H_meta", "sharp_probe", [(METRIC, Direction.flat)], [(METRIC, Direction.flat)], None),
        ],
        ["H_db", "H_meta"],
    )
    candidates = [
        Experiment(id="flat_probe", lever_id="db_failover", params={}, hold_s=30, blast_radius_pct=1.0),
        Experiment(id="sharp_probe", lever_id="shed", params={"fraction": 0.1}, hold_s=20, blast_radius_pct=10.0),
    ]

    chosen = confirmation_experiment(triage, "H_db", candidates, excluded_ids=set())

    assert chosen is not None and chosen.id == "sharp_probe"
    assert confirmation_experiment(triage, "H_db", candidates[:1], excluded_ids=set()) is None
