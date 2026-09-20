from datetime import datetime, timedelta, timezone

import pytest
from faultline_contracts import Fingerprint
from faultline_contracts.fingerprint import ServiceStats, SloStatus

from faultline_product.prepared_evidence import canary_split_evidence, canary_window_issue, healthy_window_issue


T0 = datetime(2026, 9, 20, tzinfo=timezone.utc)


def samples(n=4):
    return [Fingerprint(window_start=T0 + timedelta(seconds=5 * i),
                        window_end=T0 + timedelta(seconds=5 * (i + 1)),
                        services={name: ServiceStats(qps=80, p99_ms=100, error_rate=0)
                                  for name in ("gateway", "orders_v2")},
                        slos=[SloStatus(name="checkout", metric="svc.gateway.p99_ms", threshold=1000,
                                        value=100, breached=False)]) for i in range(n)]


def test_complete_healthy_evidence_passes():
    assert healthy_window_issue(samples(), minimum_windows=4) is None


@pytest.mark.parametrize("field,value", [("qps", None), ("qps", 0), ("qps", float("nan")),
                                        ("p99_ms", None), ("p99_ms", float("inf")),
                                        ("error_rate", None), ("error_rate", 0.1)])
def test_incomplete_or_unhealthy_version_data_fails(field, value):
    windows = samples()
    setattr(windows[1].services["orders_v2"], field, value)
    assert healthy_window_issue(windows, minimum_windows=4) is not None


def test_missing_window_is_not_zero():
    assert healthy_window_issue(samples(3), minimum_windows=4) is not None


def test_duplicate_windows_do_not_supply_coverage():
    windows = samples()
    windows[1] = windows[0]
    assert healthy_window_issue(windows, minimum_windows=4) is not None


def test_missing_slo_does_not_mean_healthy():
    windows = samples()
    windows[2].slos = []
    assert healthy_window_issue(windows, minimum_windows=4) is not None


def test_missing_service_is_not_treated_as_no_errors():
    windows = samples()
    del windows[0].services["orders_v2"]
    assert healthy_window_issue(windows, minimum_windows=4) is not None


def test_declared_healthy_slo_cannot_contradict_its_measurement():
    windows = samples()
    windows[0].slos[0].value = 1100
    assert healthy_window_issue(windows, minimum_windows=4) is not None


def test_canary_needs_full_observation_window():
    assert healthy_window_issue(samples(23), minimum_windows=24) is not None
    assert healthy_window_issue(samples(24), minimum_windows=24) is None


def split_samples(share=0.05):
    windows = samples(24)
    for fp in windows:
        fp.services["orders"] = ServiceStats(qps=80 * (1 - share), p99_ms=100, error_rate=0)
        fp.services["orders_v2"].qps = 80 * share
    return windows


def test_canary_requires_both_healthy_versions_and_requested_traffic_share():
    windows = split_samples()
    assert canary_window_issue(windows, minimum_windows=24) is None
    evidence = canary_split_evidence(windows)
    assert evidence["canary_split_status"] == "compatible"
    assert evidence["canary_observed_share_estimate"] == pytest.approx(0.05)
    assert evidence["canary_total_request_estimate"] == pytest.approx(9600)
    assert evidence["canary_v2_request_estimate"] == pytest.approx(480)


@pytest.mark.parametrize("share", [0, 0.5, 1])
def test_healthy_but_misrouted_canary_fails(share):
    assert canary_window_issue(split_samples(share), minimum_windows=24) is not None
    assert canary_split_evidence(split_samples(share))["canary_split_status"] == "mismatched"


def test_ordinary_sampling_variation_does_not_require_exactly_five_percent():
    assert canary_window_issue(split_samples(0.052), minimum_windows=24) is None


def test_missing_control_version_does_not_allow_a_canary_comparison():
    windows = split_samples()
    del windows[2].services["orders"]
    assert canary_window_issue(windows, minimum_windows=24) is not None
    assert canary_split_evidence(windows)["canary_split_status"] == "missing"


def test_incomplete_transition_window_is_still_rejected():
    windows = split_samples()
    windows[0].services["orders_v2"].p99_ms = None
    windows[0].services["orders_v2"].error_rate = None
    assert canary_window_issue(windows, minimum_windows=24) is not None
