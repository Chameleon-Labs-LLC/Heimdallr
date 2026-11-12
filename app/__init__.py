"""AWS Monitor - LLM-powered AWS service monitoring and remediation."""

__version__ = "0.1.1"

from app.config import AppConfig
from app.llm_client import LLMClient
from app.model_config import get_model_config, get_model_info
from app.aws_client import AWSClient
from app.log_collector import LogCollector, DetectedError, ErrorSeverity
from app.service_monitor import ServiceMonitor, ServiceHealth, HealthState
from app.alert_manager import AlertManager, Alert, AlertPriority, AlertStatus
from app.error_analyzer import ErrorAnalyzer, AnalysisResult, ErrorCategory
from app.llm_orchestrator import LLMOrchestrator, TaskComplexity
from app.action_recommender import ActionRecommender, ActionRecommendation, ActionPlan
from app.action_executor import ActionExecutor, ExecutionResult, ExecutionStatus
from app.safety_guard import SafetyGuard, SafetyCheckResult
from app.audit_logger import AuditLogger, AuditEvent
from app.notifier import Notifier, Notification
from app.scheduler import Scheduler, ScheduledTask, create_monitor_scheduler

__all__ = [
    # Configuration
    "AppConfig",
    # LLM
    "LLMClient",
    "get_model_config",
    "get_model_info",
    "LLMOrchestrator",
    "TaskComplexity",
    # AWS
    "AWSClient",
    # Monitoring
    "LogCollector",
    "DetectedError",
    "ErrorSeverity",
    "ServiceMonitor",
    "ServiceHealth",
    "HealthState",
    # Alerts
    "AlertManager",
    "Alert",
    "AlertPriority",
    "AlertStatus",
    # Analysis
    "ErrorAnalyzer",
    "AnalysisResult",
    "ErrorCategory",
    # Actions
    "ActionRecommender",
    "ActionRecommendation",
    "ActionPlan",
    "ActionExecutor",
    "ExecutionResult",
    "ExecutionStatus",
    # Safety
    "SafetyGuard",
    "SafetyCheckResult",
    # Audit
    "AuditLogger",
    "AuditEvent",
    # Notifications
    "Notifier",
    "Notification",
    # Scheduler
    "Scheduler",
    "ScheduledTask",
    "create_monitor_scheduler",
]
