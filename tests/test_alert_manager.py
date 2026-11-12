"""Tests for alert escalation limits and backoff."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.alert_manager import (
    Alert,
    AlertManager,
    AlertPriority,
    AlertStatus,
    EscalationRule,
)


def _make_alert(**overrides) -> Alert:
    created = overrides.pop(
        "created_at",
        datetime.now(timezone.utc) - timedelta(minutes=31),
    )
    data = dict(
        alert_id="581b096d",
        title="P2 error",
        message="failed",
        priority=AlertPriority.P2,
        status=AlertStatus.OPEN,
        source_type="error",
        source_service="api",
        created_at=created,
        updated_at=created,
        fingerprint="fp-581b096d",
    )
    data.update(overrides)
    return Alert(**data)


def _manager_with(alert: Alert, callback=None) -> AlertManager:
    manager = AlertManager(escalation_callback=callback)
    manager._alerts[alert.fingerprint] = alert
    return manager


def test_escalates_once_until_backoff_elapses(caplog):
    calls = []
    alert = _make_alert()
    manager = _manager_with(alert, lambda _alert, action: calls.append(action))

    with caplog.at_level(logging.WARNING, logger="monitor.alert_manager"):
        asyncio.run(manager._check_escalations())
        asyncio.run(manager._check_escalations())

    assert calls == ["notify"]
    assert alert.escalation_count == 1
    assert alert.status == AlertStatus.OPEN
    warnings = [r for r in caplog.records if "Escalating alert" in r.message]
    assert len(warnings) == 1


def test_backoff_schedule_gates_repeat_escalations():
    alert = _make_alert()
    alert.escalation_count = 2
    alert.last_escalated_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    calls = []
    manager = _manager_with(alert, lambda _alert, action: calls.append(action))

    asyncio.run(manager._check_escalations())
    assert calls == []
    assert alert.escalation_count == 2

    alert.last_escalated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    asyncio.run(manager._check_escalations())
    assert calls == ["notify"]
    assert alert.escalation_count == 3


def test_below_threshold_does_not_escalate():
    alert = _make_alert(created_at=datetime.now(timezone.utc) - timedelta(minutes=10))
    calls = []
    manager = _manager_with(alert, lambda _alert, action: calls.append(action))

    asyncio.run(manager._check_escalations())

    assert calls == []
    assert alert.escalation_count == 0
    assert alert.status == AlertStatus.OPEN


def test_exhaustion_logs_once_and_stops(caplog):
    calls = []
    alert = _make_alert()
    alert.escalation_count = AlertManager.MAX_ESCALATIONS - 1
    alert.last_escalated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    manager = _manager_with(alert, lambda _alert, action: calls.append(action))

    with caplog.at_level(logging.ERROR, logger="monitor.alert_manager"):
        asyncio.run(manager._check_escalations())
        asyncio.run(manager._check_escalations())
        asyncio.run(manager._check_escalations())

    assert calls == ["notify"]
    assert alert.escalation_count == AlertManager.MAX_ESCALATIONS
    assert alert.status == AlertStatus.ESCALATION_EXHAUSTED
    errors = [r for r in caplog.records if "escalation exhausted" in r.message]
    assert len(errors) == 1
    assert errors[0].levelno == logging.ERROR


def test_already_at_cap_logs_error_without_another_notify(caplog):
    calls = []
    alert = _make_alert()
    alert.escalation_count = AlertManager.MAX_ESCALATIONS
    manager = _manager_with(alert, lambda _alert, action: calls.append(action))

    with caplog.at_level(logging.ERROR, logger="monitor.alert_manager"):
        asyncio.run(manager._check_escalations())
        asyncio.run(manager._check_escalations())

    assert calls == []
    assert alert.status == AlertStatus.ESCALATION_EXHAUSTED
    errors = [r for r in caplog.records if "escalation exhausted" in r.message]
    assert len(errors) == 1


class _Clock:
    """Controllable replacement for ``datetime`` inside the alert manager."""

    def __init__(self, start: datetime):
        self.current = start

    def now(self, tz=None):
        """Return the frozen current time, like ``datetime.now``."""
        return self.current


def _patch_clock(monkeypatch, start: datetime) -> _Clock:
    clock = _Clock(start)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now(tz)

    monkeypatch.setattr("app.alert_manager.datetime", _FrozenDatetime)
    return clock


def test_issue_1_scenario_caps_a_two_hour_storm(monkeypatch, caplog):
    """Regression for #1: an unacknowledged P2 checked every minute for hours
    escalates exactly MAX_ESCALATIONS times on the backoff schedule, then logs
    one ERROR and stays quiet."""
    start = datetime(2026, 3, 11, 22, 25, tzinfo=timezone.utc)
    clock = _patch_clock(monkeypatch, start)
    alert = _make_alert(created_at=start - timedelta(minutes=30))
    escalated_at = []
    manager = _manager_with(
        alert, lambda a, action: escalated_at.append(clock.current)
    )

    with caplog.at_level(logging.WARNING, logger="monitor.alert_manager"):
        for _ in range(4 * 60):  # four hours of once-a-minute checks
            asyncio.run(manager._check_escalations())
            clock.current += timedelta(minutes=1)

    assert len(escalated_at) == AlertManager.MAX_ESCALATIONS
    gaps = [
        int((b - a).total_seconds() // 60)
        for a, b in zip(escalated_at, escalated_at[1:])
    ]
    assert gaps == [1, 2, 5, 10, 30, 30, 30, 30, 30]
    assert alert.status == AlertStatus.ESCALATION_EXHAUSTED
    warnings = [r for r in caplog.records if "Escalating alert" in r.message]
    errors = [r for r in caplog.records if "escalation exhausted" in r.message]
    assert len(warnings) == AlertManager.MAX_ESCALATIONS
    assert len(errors) == 1


def test_callback_acknowledging_on_final_attempt_is_preserved(caplog):
    """The callback's acknowledgment on attempt 10 must not be overwritten."""
    alert = _make_alert()
    alert.escalation_count = AlertManager.MAX_ESCALATIONS - 1
    alert.last_escalated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    manager = AlertManager()
    manager.escalation_callback = (
        lambda a, action: manager.acknowledge_alert(a.alert_id, "on-call")
    )
    manager._alerts[alert.fingerprint] = alert

    with caplog.at_level(logging.ERROR, logger="monitor.alert_manager"):
        asyncio.run(manager._check_escalations())

    assert alert.status == AlertStatus.ACKNOWLEDGED
    assert alert.acknowledged_by == "on-call"
    assert not [r for r in caplog.records if "escalation exhausted" in r.message]


def test_callback_resolving_on_final_attempt_is_preserved():
    """A resolution made by the callback on attempt 10 stays resolved."""
    alert = _make_alert()
    alert.escalation_count = AlertManager.MAX_ESCALATIONS - 1
    alert.last_escalated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    manager = AlertManager()
    manager.escalation_callback = (
        lambda a, action: manager.resolve_alert(a.alert_id, "auto")
    )
    manager._alerts[alert.fingerprint] = alert

    asyncio.run(manager._check_escalations())

    assert alert.status == AlertStatus.RESOLVED
    assert manager.get_recent_history()[0] is alert
    assert alert.fingerprint not in manager._alerts


def test_exhausted_alert_can_be_acknowledged_and_resolved():
    """Exhaustion is not terminal: an operator can still ack, then resolve."""
    alert = _make_alert(status=AlertStatus.ESCALATION_EXHAUSTED)
    alert.escalation_count = AlertManager.MAX_ESCALATIONS
    manager = _manager_with(alert)

    assert manager.acknowledge_alert(alert.alert_id, "operator")
    assert alert.status == AlertStatus.ACKNOWLEDGED
    assert manager.resolve_alert(alert.alert_id, "operator")
    assert alert.status == AlertStatus.RESOLVED
    assert manager.get_open_alerts() == []


def test_later_rule_for_same_priority_is_not_shadowed():
    """With two P1 rules past threshold, the longer-threshold one fires."""
    alert = _make_alert(
        priority=AlertPriority.P1,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    calls = []
    manager = _manager_with(alert, lambda _alert, action: calls.append(action))
    manager._escalation_rules.append(
        EscalationRule("P1 unack 15min", AlertPriority.P1, 15, "auto_remediate")
    )

    asyncio.run(manager._check_escalations())

    assert calls == ["auto_remediate"]
    assert alert.escalation_count == 1
