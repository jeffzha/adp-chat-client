"""Low-cardinality Prometheus metrics for the isolated workbench runtime.

The registry intentionally accepts only declared label names and values. This
prevents a future call site from accidentally putting tenant, user, App, Turn,
request, or Conversation identifiers into a time-series label.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict


_DEFINITIONS = {
    "workbench_active_turns": ("gauge", "Live provider Turn tasks in this process.", {}),
    "workbench_active_sse": ("gauge", "Active durable Turn replay SSE connections.", {}),
    "workbench_turns_total": (
        "counter",
        "Durable Turns reaching a terminal state.",
        {"status": frozenset({"completed", "failed_before_accept", "failed_after_accept", "provider_unknown", "cancel_confirmed", "other"})},
    ),
    "workbench_turn_events_persisted_total": (
        "counter",
        "Workbench Turn-event rows durably committed to the database.",
        {},
    ),
    "workbench_sse_reconnect_total": ("counter", "SSE replays resumed after a prior event.", {}),
    "workbench_control_event_lag_seconds": (
        "gauge",
        "Age of the latest applied control event.",
        {"stage": frozenset({"consume"})},
    ),
    "workbench_control_event_failures_total": (
        "counter",
        "Failures consuming or dispatching control events.",
        {"stage": frozenset({"consume", "dispatch"})},
    ),
    "workbench_authz_denied_total": (
        "counter",
        "Denied or failed continuous authorization checks.",
        {"reason": frozenset({"denied", "error"})},
    ),
    "workbench_identity_bind_total": (
        "counter",
        "Shadow-account binding confirmations sent to the control plane.",
        {"result": frozenset({"success", "failure"})},
    ),
    "workbench_file_failures_total": (
        "counter",
        "Secure file pipeline failures.",
        {"stage": frozenset({"scan", "upload"})},
    ),
    "workbench_usage_evidence_capture_failures_total": (
        "counter",
        "Failures persisting response.completed evidence.",
        {"reason": frozenset({"invalid", "configuration", "conflict", "storage", "other"})},
    ),
    "workbench_gate_failures_total": (
        "counter",
        "Trusted App, plan, or local policy gate failures.",
        {"gate": frozenset({"app", "plan", "policy"}), "reason": frozenset({"denied", "error"})},
    ),
    "workbench_limit_denied_total": (
        "counter",
        "Local workbench policy limits that denied an operation.",
        {"limit": frozenset({"capability", "access_mode", "configuration", "web_search", "unbounded", "file_size", "other"})},
    ),
    "workbench_first_event_seconds": (
        "histogram",
        "Seconds from durable Turn creation to first persisted provider event.",
        {},
    ),
    "workbench_agent_provision_seconds": (
        "histogram",
        "Seconds from a durable provisioning attempt to Agent ownership reporting.",
        {},
    ),
    "workbench_action_denied_total": (
        "counter",
        "Unknown or unauthorized generic ADP Actions denied locally.",
        {"action": frozenset({
            "DescribeApp", "CopyAgentFromApp", "DescribeAgentDetail", "ModifyAgent",
            "CreateConversation", "DescribeConversation", "DescribeConversationList",
            "DescribeConversationMessageList", "DescribeModelList",
            "DescribeSkillSummaryList", "DescribeSkillDetail",
            "DescribePluginSummaryList", "DescribePlugin", "other",
        })},
    ),
    "workbench_turn_duration_seconds": (
        "histogram",
        "Seconds from durable Turn creation to terminal persistence.",
        {},
    ),
    "workbench_turn_event_persist_db_seconds": (
        "histogram",
        "Database transaction seconds for each successfully persisted provider Turn event.",
        {},
    ),
    "workbench_scheduled_task_actions_total": (
        "counter",
        "Authorized scheduled-task lifecycle mutations.",
        {"action": frozenset({"create", "update", "pause", "resume", "run_now", "delete"})},
    ),
    "workbench_scheduled_materialized_total": (
        "counter",
        "Durable scheduled occurrences materialized by result.",
        {"result": frozenset({"queued", "skipped"})},
    ),
    "workbench_scheduled_runs_total": (
        "counter",
        "Scheduled runs reaching a terminal state.",
        {"status": frozenset({"completed", "failed_before_accept", "failed_after_accept", "provider_unknown", "cancelled", "other"})},
    ),
    "workbench_scheduled_worker_failures_total": (
        "counter",
        "Failures in the scheduled-task worker loop.",
        {"stage": frozenset({"loop"})},
    ),
}

_HISTOGRAM_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0)
_TURN_EVENT_DB_BUCKETS = (
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class WorkbenchMetrics:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[str, dict[str, object]] = {}
        self.reset_for_test()

    @staticmethod
    def _labels(name: str, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        definition = _DEFINITIONS.get(name)
        if definition is None:
            raise ValueError("undeclared workbench metric")
        expected = definition[2]
        if set(labels) != set(expected):
            raise ValueError("workbench metric labels do not match the declaration")
        normalized = []
        for key in sorted(expected):
            value = str(labels[key])
            if value not in expected[key]:
                raise ValueError("workbench metric label value is not bounded")
            normalized.append((key, value))
        return tuple(normalized)

    def inc(self, name: str, amount: float = 1.0, **labels: str) -> None:
        if _DEFINITIONS.get(name, (None,))[0] != "counter" or not math.isfinite(amount) or amount < 0:
            raise ValueError("invalid workbench counter update")
        key = (name, self._labels(name, labels))
        with self._lock:
            self._values[key] += amount

    def set(self, name: str, value: float, **labels: str) -> None:
        if _DEFINITIONS.get(name, (None,))[0] != "gauge" or not math.isfinite(value):
            raise ValueError("invalid workbench gauge update")
        key = (name, self._labels(name, labels))
        with self._lock:
            self._values[key] = value

    def add(self, name: str, amount: float, **labels: str) -> None:
        if _DEFINITIONS.get(name, (None,))[0] != "gauge" or not math.isfinite(amount):
            raise ValueError("invalid workbench gauge update")
        key = (name, self._labels(name, labels))
        with self._lock:
            self._values[key] = max(0.0, self._values[key] + amount)

    def observe(self, name: str, value: float) -> None:
        if _DEFINITIONS.get(name, (None,))[0] != "histogram" or not math.isfinite(value) or value < 0:
            raise ValueError("invalid workbench histogram observation")
        if name == "workbench_turn_event_persist_db_seconds":
            raise ValueError("Turn-event DB latency must be recorded with its committed row count")
        with self._lock:
            self._observe_locked(name, value)

    def record_turn_event_commit(self, event_rows: int, duration_seconds: float) -> None:
        if (
            not isinstance(event_rows, int)
            or isinstance(event_rows, bool)
            or event_rows <= 0
            or not math.isfinite(duration_seconds)
            or duration_seconds < 0
        ):
            raise ValueError("invalid committed workbench Turn-event transaction")
        with self._lock:
            self._values[("workbench_turn_events_persisted_total", ())] += event_rows
            self._observe_locked("workbench_turn_event_persist_db_seconds", duration_seconds)

    def _observe_locked(self, name: str, value: float) -> None:
        histogram = self._histograms[name]
        histogram["count"] = int(histogram["count"]) + 1
        histogram["sum"] = float(histogram["sum"]) + value
        counts = histogram["buckets"]
        for bucket in self._buckets(name):
            if value <= bucket:
                counts[bucket] += 1

    def record_terminal(self, status: str, duration_seconds: float) -> None:
        allowed = _DEFINITIONS["workbench_turns_total"][2]["status"]
        normalized = status if status in allowed else "other"
        self.inc("workbench_turns_total", status=normalized)
        self.observe("workbench_turn_duration_seconds", max(0.0, duration_seconds))

    def record_action_denied(self, action: str) -> None:
        allowed = _DEFINITIONS["workbench_action_denied_total"][2]["action"]
        normalized = action if action in allowed else "other"
        self.inc("workbench_action_denied_total", action=normalized)

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
            histograms = {
                name: {
                    "count": data["count"],
                    "sum": data["sum"],
                    "buckets": dict(data["buckets"]),
                }
                for name, data in self._histograms.items()
            }
        output: list[str] = []
        for name in sorted(_DEFINITIONS):
            metric_type, help_text, _ = _DEFINITIONS[name]
            output.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))
            if metric_type == "histogram":
                data = histograms[name]
                for bucket in self._buckets(name):
                    output.append(f'{name}_bucket{{le="{bucket:g}"}} {data["buckets"][bucket]}')
                output.append(f'{name}_bucket{{le="+Inf"}} {data["count"]}')
                output.append(f'{name}_sum {float(data["sum"]):.6f}')
                output.append(f'{name}_count {data["count"]}')
                continue
            rows = sorted(
                (labels, value)
                for (metric_name, labels), value in values.items()
                if metric_name == name
            )
            for labels, value in rows:
                suffix = ""
                if labels:
                    suffix = "{" + ",".join(
                        f'{key}="{self._escape(label_value)}"' for key, label_value in labels
                    ) + "}"
                output.append(f"{name}{suffix} {value:g}")
        return "\n".join(output) + "\n"

    def reset_for_test(self) -> None:
        with self._lock:
            self._values.clear()
            self._histograms = {
                name: {"count": 0, "sum": 0.0, "buckets": {bucket: 0 for bucket in self._buckets(name)}}
                for name, definition in _DEFINITIONS.items()
                if definition[0] == "histogram"
            }
            for status in sorted(_DEFINITIONS["workbench_turns_total"][2]["status"]):
                self._values[("workbench_turns_total", (("status", status),))] = 0.0
            for result in sorted(
                _DEFINITIONS["workbench_identity_bind_total"][2]["result"]
            ):
                self._values[
                    ("workbench_identity_bind_total", (("result", result),))
                ] = 0.0
            self._values[("workbench_turn_events_persisted_total", ())] = 0.0
            self._values[("workbench_active_turns", ())] = 0.0
            self._values[("workbench_active_sse", ())] = 0.0
            self._values[("workbench_control_event_lag_seconds", (("stage", "consume"),))] = 0.0

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    @staticmethod
    def _buckets(name: str) -> tuple[float, ...]:
        if name == "workbench_turn_event_persist_db_seconds":
            return _TURN_EVENT_DB_BUCKETS
        return _HISTOGRAM_BUCKETS


WORKBENCH_METRICS = WorkbenchMetrics()


def evidence_failure_reason(error: Exception) -> str:
    message = str(error).lower()
    if "not configured" in message or "encryption" in message:
        return "configuration"
    if "conflicting" in message or "ownership changed" in message:
        return "conflict"
    if "invalid" in message or "exceeds" in message:
        return "invalid"
    if type(error).__name__ in {"IntegrityError", "OperationalError", "OSError"}:
        return "storage"
    return "other"
