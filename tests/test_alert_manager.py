"""Tests for alert escalation limits and backoff."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.alert_manager import Alert, AlertManager, AlertPriority, AlertStatus


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
