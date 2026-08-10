from concurrent.futures import ThreadPoolExecutor

import pytest

from core.workbench_metrics import WorkbenchMetrics


def test_load_evidence_counter_families_exist_before_first_turn():
    output = WorkbenchMetrics().render()

    assert "workbench_turn_events_persisted_total 0" in output
    assert 'workbench_turns_total{status="completed"} 0' in output
    assert 'workbench_identity_bind_total{result="success"} 0' in output
    assert 'workbench_identity_bind_total{result="failure"} 0' in output
    assert "workbench_turn_event_persist_db_seconds_count 0" in output


def test_metrics_are_concurrent_safe_and_prometheus_histograms_are_cumulative():
    metrics = WorkbenchMetrics()

    def update(_index):
        metrics.inc("workbench_turns_total", status="completed")
        metrics.inc("workbench_identity_bind_total", result="success")
        metrics.add("workbench_active_turns", 1)
        metrics.add("workbench_active_turns", -1)
        metrics.record_turn_event_commit(2, 0.002)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(100)))
    metrics.observe("workbench_turn_duration_seconds", 0.2)
    metrics.observe("workbench_turn_duration_seconds", 3.0)
    metrics.observe("workbench_agent_provision_seconds", 1.2)
    metrics.record_action_denied("DescribeApp")
    metrics.record_action_denied("attacker-supplied-action")

    output = metrics.render()
    assert 'workbench_turns_total{status="completed"} 100' in output
    assert 'workbench_identity_bind_total{result="success"} 100' in output
    assert "workbench_active_turns 0" in output
    assert 'workbench_turn_duration_seconds_bucket{le="0.25"} 1' in output
    assert 'workbench_turn_duration_seconds_bucket{le="5"} 2' in output
    assert "workbench_turn_duration_seconds_count 2" in output
    assert "workbench_turn_events_persisted_total 200" in output
    assert 'workbench_turn_event_persist_db_seconds_bucket{le="0.0025"} 100' in output
    assert "workbench_turn_event_persist_db_seconds_count 100" in output
    assert "workbench_agent_provision_seconds_count 1" in output
    assert 'workbench_action_denied_total{action="DescribeApp"} 1' in output
    assert 'workbench_action_denied_total{action="other"} 1' in output
    assert "customer_id" not in output
    assert "turn_id" not in output


def test_metrics_reject_undeclared_or_high_cardinality_labels():
    metrics = WorkbenchMetrics()

    with pytest.raises(ValueError):
        metrics.inc(
            "workbench_turns_total",
            status="completed",
            customer_id="customer-42",
        )
    with pytest.raises(ValueError):
        metrics.inc("workbench_turns_total", status="wt_arbitrary")
    with pytest.raises(ValueError):
        metrics.inc("unknown_metric")
    with pytest.raises(ValueError):
        metrics.record_turn_event_commit(0, 0.001)
    with pytest.raises(ValueError):
        metrics.record_turn_event_commit(1, float("nan"))
    with pytest.raises(ValueError, match="committed row count"):
        metrics.observe("workbench_turn_event_persist_db_seconds", 0.001)
