import pytest

from faultline_contracts.common import WINDOW_S

from faultline_telemetry.config import TelemetrySettings


def test_defaults_keep_envoy_admin_in_the_compose_network():
    settings = TelemetrySettings()

    assert settings.envoy_stats_url == "http://envoy:9902/stats/prometheus"
    assert settings.window_s == WINDOW_S


def test_non_contract_window_is_refused():
    with pytest.raises(ValueError, match="fixed"):
        TelemetrySettings(window_s=10)
