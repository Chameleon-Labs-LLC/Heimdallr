"""Alert manager for error thresholds, deduplication, and escalation.

Provides:
- Alert creation from errors and health changes
- Alert deduplication to prevent spam
- Severity-based escalation rules
- Alert lifecycle management (open, acknowledged, resolved)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Set
from uuid import uuid4

from app.log_collector import DetectedError, ErrorSeverity
from app.service_monitor import HealthChange, HealthState

log = logging.getLogger("monitor.alert_manager")


class AlertStatus(Enum):
    """Status of an alert."""
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"
    ESCALATION_EXHAUSTED = "escalation_exhausted"


class AlertPriority(Enum):
    """Priority level for alerts."""
    P1 = "P1"  # Critical - immediate action
    P2 = "P2"  # High - action within 1 hour
    P3 = "P3"  # Medium - action within 24 hours
    P4 = "P4"  # Low - informational


@dataclass
class Alert:
    """An alert requiring attention."""
    alert_id: str
    title: str
    message: str
    priority: AlertPriority
    status: AlertStatus
    source_type: str  # "error" or "health_change"
    source_service: str
    created_at: datetime
    updated_at: datetime
    fingerprint: str = ""  # For deduplication
    occurrence_count: int = 1
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    metadata: Dict = field(default_factory=dict)
    escalation_count: int = 0
    last_escalated_at: Optional[datetime] = None

    @classmethod
    def from_error(cls, error: DetectedError) -> "Alert":
        """Create alert from a detected error."""
        # Map error severity to alert priority
        priority_map = {
            ErrorSeverity.CRITICAL: AlertPriority.P1,
            ErrorSeverity.ERROR: AlertPriority.P2,
            ErrorSeverity.WARNING: AlertPriority.P3,
            ErrorSeverity.INFO: AlertPriority.P4,
        }

        now = datetime.now(timezone.utc)
        return cls(
            alert_id=str(uuid4())[:8],
            title=f"{error.severity.value.upper()}: {error.error_type} in {error.source_app}",
            message=error.message[:500],
            priority=priority_map.get(error.severity, AlertPriority.P3),
            status=AlertStatus.OPEN,
            source_type="error",
            source_service=error.source_app,
            created_at=now,
            updated_at=now,
            fingerprint=error.fingerprint,
            occurrence_count=error.count,
            metadata={
                "error_type": error.error_type,
                "log_group": error.log_group,
                "log_stream": error.log_stream,
                "error_timestamp": error.timestamp.isoformat(),
            },
        )

    @classmethod
    def from_health_change(cls, change: HealthChange) -> "Alert":
        """Create alert from a health state change."""
        # Determine priority based on state transition
        if change.new_state == HealthState.UNHEALTHY:
            priority = AlertPriority.P1
        elif change.new_state == HealthState.DEGRADED:
            priority = AlertPriority.P2
        elif change.old_state in {HealthState.UNHEALTHY, HealthState.DEGRADED}:
            priority = AlertPriority.P3  # Recovery
        else:
            priority = AlertPriority.P4

        now = datetime.now(timezone.utc)
        return cls(
            alert_id=str(uuid4())[:8],
            title=f"Health: {change.service_name} {change.old_state.value} → {change.new_state.value}",
            message=change.message,
            priority=priority,
            status=AlertStatus.OPEN,
            source_type="health_change",
            source_service=change.service_name,
            created_at=now,
            updated_at=now,
            fingerprint=f"health:{change.service_id}",
            metadata={
                "service_id": change.service_id,
                "service_type": change.service_type,
                "old_state": change.old_state.value,
                "new_state": change.new_state.value,
            },
        )


@dataclass
class EscalationRule:
    """Rule for escalating alerts."""
    name: str
    priority: AlertPriority
    after_minutes: int  # Escalate if unacknowledged after this time
    action: str  # "notify", "page", "auto_remediate"


class AlertManager:
    """Manages alerts, deduplication, and escalation.

    Features:
    - Creates alerts from errors and health changes
    - Deduplicates alerts by fingerprint
    - Applies escalation rules based on priority and time
    - Caps escalation attempts and backs off between them
    - Tracks alert lifecycle (open -> acknowledged -> resolved)
    """

    # Stop notifying after this many escalation attempts.
    MAX_ESCALATIONS = 10
    # Minutes to wait before the next attempt, indexed by attempts already made.
    # The last interval is reused until the attempt cap is reached.
    ESCALATION_BACKOFF_MINUTES = (1, 2, 5, 10, 30)

    def __init__(
        self,
        alert_callback: Optional[Callable[[Alert], None]] = None,
        escalation_callback: Optional[Callable[[Alert, str], None]] = None,
    ):
        """Initialize the alert manager.

        Args:
            alert_callback: Called when new alert is created
            escalation_callback: Called when alert escalates (alert, reason)
        """
        self.alert_callback = alert_callback
        self.escalation_callback = escalation_callback

        # Active alerts by fingerprint
        self._alerts: Dict[str, Alert] = {}

        # Alert history (resolved alerts)
        self._history: List[Alert] = []
        self._max_history = 500

        # Escalation rules
        self._escalation_rules: List[EscalationRule] = [
            EscalationRule("P1 unack 5min", AlertPriority.P1, 5, "page"),
            EscalationRule("P2 unack 30min", AlertPriority.P2, 30, "notify"),
            EscalationRule("P3 unack 2hr", AlertPriority.P3, 120, "notify"),
        ]

        # Suppression rules (service patterns to ignore)
        self._suppressed_patterns: Set[str] = set()

        # Background task
        self._running = False
        self._escalation_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the alert manager background tasks."""
        if self._running:
            return

        self._running = True
        self._escalation_task = asyncio.create_task(self._escalation_loop())
        log.info("Alert manager started")

    async def stop(self) -> None:
        """Stop the alert manager."""
        self._running = False
        if self._escalation_task:
            self._escalation_task.cancel()
            try:
                await self._escalation_task
            except asyncio.CancelledError:
                pass
        log.info("Alert manager stopped")

    async def _escalation_loop(self) -> None:
        """Check for alerts needing escalation."""
        while self._running:
            try:
                await self._check_escalations()
            except Exception as e:
                log.error(f"Error in escalation check: {e}")

            await asyncio.sleep(60)  # Check every minute

    async def _check_escalations(self) -> None:
        """Check open alerts against escalation rules.

        An alert escalates at most once per backoff interval. When several
        rules for its priority have passed their threshold, the one with the
        longest threshold fires, so a later, stronger rule (for example a
        page after a notify) takes over instead of being shadowed by the
        first. After MAX_ESCALATIONS attempts the alert is marked
        escalation_exhausted and a single error is logged.
        """
        now = datetime.now(timezone.utc)

        for alert in list(self._alerts.values()):
            if alert.status != AlertStatus.OPEN:
                continue

            if alert.escalation_count >= self.MAX_ESCALATIONS:
                self._mark_escalation_exhausted(alert, now)
                continue

            age_minutes = (now - alert.created_at).total_seconds() / 60

            matching = [
                rule for rule in self._escalation_rules
                if rule.priority == alert.priority
                and age_minutes >= rule.after_minutes
            ]
            if not matching or not self._escalation_interval_elapsed(alert, now):
                continue

            rule = max(matching, key=lambda r: r.after_minutes)

            alert.escalation_count += 1
            alert.last_escalated_at = now
            alert.updated_at = now

            log.warning(
                f"Escalating alert {alert.alert_id}: "
                f"{rule.name} ({rule.action})"
            )

            if self.escalation_callback:
                try:
                    self.escalation_callback(alert, rule.action)
                except Exception as e:
                    log.error(f"Escalation callback failed: {e}")

            if alert.escalation_count >= self.MAX_ESCALATIONS:
                self._mark_escalation_exhausted(alert, now)

    def _escalation_interval_elapsed(self, alert: Alert, now: datetime) -> bool:
        """Return True when the backoff since the last escalation has elapsed."""
        if alert.escalation_count == 0 or alert.last_escalated_at is None:
            return True

        index = min(
            alert.escalation_count - 1,
            len(self.ESCALATION_BACKOFF_MINUTES) - 1,
        )
        delay_minutes = self.ESCALATION_BACKOFF_MINUTES[index]
        elapsed_minutes = (now - alert.last_escalated_at).total_seconds() / 60
        return elapsed_minutes >= delay_minutes

    def _mark_escalation_exhausted(self, alert: Alert, now: datetime) -> None:
        """Stop further escalations and log the exhaustion a single time.

        Only an OPEN alert can become exhausted. The escalation callback may
        acknowledge or resolve the alert on its final attempt; that decision
        stands and must not be overwritten here.
        """
        if alert.status != AlertStatus.OPEN:
            return

        alert.status = AlertStatus.ESCALATION_EXHAUSTED
        alert.updated_at = now
        log.error(
            f"Alert {alert.alert_id} escalation exhausted after "
            f"{alert.escalation_count} attempts"
        )

    def process_error(self, error: DetectedError) -> Optional[Alert]:
        """Process a detected error and create/update alert.

        Args:
            error: Detected error from log collector

        Returns:
            Alert if created/updated, None if suppressed
        """
        # Check suppression
        if self._is_suppressed(error.source_app):
            log.debug(f"Suppressed error from {error.source_app}")
            return None

        fingerprint = error.fingerprint

        # Check for existing alert
        if fingerprint in self._alerts:
            alert = self._alerts[fingerprint]
            alert.occurrence_count += error.count
            alert.updated_at = datetime.now(timezone.utc)
            log.debug(f"Updated alert {alert.alert_id}: {alert.occurrence_count} occurrences")
            return alert

        # Create new alert
        alert = Alert.from_error(error)
        self._alerts[fingerprint] = alert

        log.info(f"New alert {alert.alert_id}: {alert.title}")

        if self.alert_callback:
            try:
                self.alert_callback(alert)
            except Exception as e:
                log.error(f"Alert callback failed: {e}")

        return alert

    def process_health_change(self, change: HealthChange) -> Optional[Alert]:
        """Process a health state change and create/update alert.

        Args:
            change: Health state change from service monitor

        Returns:
            Alert if created, None if not alertable
        """
        # Only alert on degraded or unhealthy states
        if change.new_state not in {HealthState.DEGRADED, HealthState.UNHEALTHY}:
            # Check if we should resolve existing alert
            fingerprint = f"health:{change.service_id}"
            if fingerprint in self._alerts:
                self.resolve_alert(
                    fingerprint,
                    resolved_by="auto",
                    message=f"Service recovered: {change.new_state.value}",
                )
            return None

        # Check suppression
        if self._is_suppressed(change.service_name):
            log.debug(f"Suppressed health change for {change.service_name}")
            return None

        fingerprint = f"health:{change.service_id}"

        # Check for existing alert
        if fingerprint in self._alerts:
            alert = self._alerts[fingerprint]
            alert.message = change.message
            alert.updated_at = datetime.now(timezone.utc)
            alert.metadata["old_state"] = change.old_state.value
            alert.metadata["new_state"] = change.new_state.value
            return alert

        # Create new alert
        alert = Alert.from_health_change(change)
        self._alerts[fingerprint] = alert

        log.info(f"New health alert {alert.alert_id}: {alert.title}")

        if self.alert_callback:
            try:
                self.alert_callback(alert)
            except Exception as e:
                log.error(f"Alert callback failed: {e}")

        return alert

    def acknowledge_alert(
        self,
        alert_id_or_fingerprint: str,
        acknowledged_by: str = "operator",
    ) -> bool:
        """Acknowledge an alert.

        Args:
            alert_id_or_fingerprint: Alert ID or fingerprint
            acknowledged_by: Who acknowledged

        Returns:
            True if alert found and acknowledged
        """
        alert = self._find_alert(alert_id_or_fingerprint)
        if not alert:
            return False

        alert.status = AlertStatus.ACKNOWLEDGED
        alert.acknowledged_at = datetime.now(timezone.utc)
        alert.acknowledged_by = acknowledged_by
        alert.updated_at = datetime.now(timezone.utc)

        log.info(f"Alert {alert.alert_id} acknowledged by {acknowledged_by}")
        return True

    def resolve_alert(
        self,
        alert_id_or_fingerprint: str,
        resolved_by: str = "operator",
        message: str = "",
    ) -> bool:
        """Resolve an alert.

        Args:
            alert_id_or_fingerprint: Alert ID or fingerprint
            resolved_by: Who resolved
            message: Resolution message

        Returns:
            True if alert found and resolved
        """
        alert = self._find_alert(alert_id_or_fingerprint)
        if not alert:
            return False

        alert.status = AlertStatus.RESOLVED
        alert.resolved_at = datetime.now(timezone.utc)
        alert.resolved_by = resolved_by
        alert.updated_at = datetime.now(timezone.utc)
        if message:
            alert.message = message

        # Move to history
        if alert.fingerprint in self._alerts:
            del self._alerts[alert.fingerprint]
        self._history.append(alert)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        log.info(f"Alert {alert.alert_id} resolved by {resolved_by}")
        return True

    def _find_alert(self, alert_id_or_fingerprint: str) -> Optional[Alert]:
        """Find alert by ID or fingerprint."""
        # Try fingerprint first
        if alert_id_or_fingerprint in self._alerts:
            return self._alerts[alert_id_or_fingerprint]

        # Try alert ID
        for alert in self._alerts.values():
            if alert.alert_id == alert_id_or_fingerprint:
                return alert

        return None

    def _is_suppressed(self, service_name: str) -> bool:
        """Check if service is suppressed."""
        for pattern in self._suppressed_patterns:
            if pattern in service_name.lower():
                return True
        return False

    def suppress_service(self, pattern: str) -> None:
        """Suppress alerts for services matching pattern.

        Args:
            pattern: Service name pattern to suppress (case-insensitive)
        """
        self._suppressed_patterns.add(pattern.lower())
        log.info(f"Suppressing alerts for: {pattern}")

    def unsuppress_service(self, pattern: str) -> None:
        """Remove suppression for a pattern."""
        self._suppressed_patterns.discard(pattern.lower())
        log.info(f"Unsuppressed alerts for: {pattern}")

    def get_open_alerts(self, priority: Optional[AlertPriority] = None) -> List[Alert]:
        """Get all open alerts.

        Args:
            priority: Optional priority filter

        Returns:
            List of open alerts
        """
        alerts = [
            a for a in self._alerts.values()
            if a.status in {
                AlertStatus.OPEN,
                AlertStatus.ACKNOWLEDGED,
                AlertStatus.ESCALATION_EXHAUSTED,
            }
        ]

        if priority:
            alerts = [a for a in alerts if a.priority == priority]

        # Sort by priority then time
        priority_order = {AlertPriority.P1: 0, AlertPriority.P2: 1, AlertPriority.P3: 2, AlertPriority.P4: 3}
        alerts.sort(key=lambda a: (priority_order.get(a.priority, 99), a.created_at))

        return alerts

    def get_alert_by_id(self, alert_id: str) -> Optional[Alert]:
        """Get specific alert by ID."""
        return self._find_alert(alert_id)

    def get_recent_history(self, limit: int = 20) -> List[Alert]:
        """Get recently resolved alerts.

        Args:
            limit: Maximum alerts to return

        Returns:
            List of resolved alerts, newest first
        """
        return list(reversed(self._history[-limit:]))

    def get_stats(self) -> Dict:
        """Get alert manager statistics."""
        by_priority = {p.value: 0 for p in AlertPriority}
        by_status = {s.value: 0 for s in AlertStatus}

        for alert in self._alerts.values():
            by_priority[alert.priority.value] += 1
            by_status[alert.status.value] += 1

        return {
            "total_open": len(self._alerts),
            "by_priority": by_priority,
            "by_status": by_status,
            "history_size": len(self._history),
            "suppressed_patterns": len(self._suppressed_patterns),
        }

    def clear_old_alerts(self, hours: int = 24) -> int:
        """Auto-resolve old alerts.

        Args:
            hours: Resolve alerts older than this

        Returns:
            Number of alerts resolved
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        to_resolve = [
            a.fingerprint for a in self._alerts.values()
            if a.status in {AlertStatus.OPEN, AlertStatus.ESCALATION_EXHAUSTED}
            and a.created_at < cutoff
        ]

        for fp in to_resolve:
            self.resolve_alert(fp, resolved_by="auto_cleanup", message="Auto-resolved due to age")

        if to_resolve:
            log.info(f"Auto-resolved {len(to_resolve)} old alerts")

        return len(to_resolve)
