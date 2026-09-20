from faultline_bench.report import analyze, markdown, svg


def test_report_has_rates_intervals_and_honestly_missing_live_values():
    payload = {"runs": [
        {"arm": "active", "scenario": "storm", "expected_diagnosis": "H_meta", "correct": True,
         "result": {"diagnosis": "H_meta", "confirmed": True, "time_to_verdict_seconds": 170, "actions_applied": 1}},
        {"arm": "active", "scenario": "no_fault", "expected_diagnosis": "no_incident", "correct": False,
         "result": {"diagnosis": "H_meta", "confirmed": True, "time_to_verdict_seconds": 120, "actions_applied": 0}},
    ]}
    result = analyze(payload, bootstrap_samples=20)
    arm = result["arms"][0]
    assert arm["balanced_accuracy"] == 0.5
    assert arm["false_confirmation_rate"] == 0.5
    assert arm["no_fault_false_positive_rate"] == 1.0
    assert arm["mean_time_to_verdict_s"] == 145
    assert arm["mean_tokens"] is None
    assert "Balanced accuracy" in markdown(result)
    assert "<svg" in svg(result)
