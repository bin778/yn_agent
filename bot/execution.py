"""Phase 2 execution safety boundary.

Only simulated read-only and dry-run adapters exist in this phase.  This module
must not contain an HTTP client or credentials for an external platform.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .db import get_conn, utc_iso, utc_now
from .redaction import redact_data, summarize_for_log


EXECUTION_MODES = frozenset({"disabled", "read_only", "dry_run"})
READ_ONLY_TYPES = frozenset({"cms_read", "ads_report_read"})
WRITE_TYPES = frozenset(
    {
        "cms_publish",
        "ads_bid_update",
        "ads_budget_update",
        "ads_campaign_pause",
        "external_message",
        "public_post",
        "source_data_mutation",
    }
)
SUPPORTED_TYPES = READ_ONLY_TYPES | WRITE_TYPES
ADS_TYPES = frozenset(item for item in SUPPORTED_TYPES if item.startswith("ads_"))
ALLOWED_AGENTS_BY_TYPE = {
    "cms_read": frozenset({"content_editor"}),
    "cms_publish": frozenset({"content_editor"}),
    "public_post": frozenset({"content_editor"}),
    "external_message": frozenset({"content_editor"}),
    "ads_report_read": frozenset({"performance_analyst"}),
    "ads_bid_update": frozenset({"performance_analyst"}),
    "ads_budget_update": frozenset({"performance_analyst"}),
    "ads_campaign_pause": frozenset({"performance_analyst"}),
    "source_data_mutation": frozenset({"data_assistant"}),
}


def _strict_bool(raw: str | None, default: bool, name: str, errors: list[str]) -> bool:
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    errors.append(f"{name} must be true or false")
    return default


def _positive_float(raw: str | None, default: float, name: str, errors: list[str]) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name} must be a positive number")
        return default
    if value <= 0:
        errors.append(f"{name} must be a positive number")
        return default
    return value


@dataclass(frozen=True)
class ExecutionSettings:
    """Fail-closed settings for the Phase 2 safety boundary."""

    app_env: str = "development"
    enabled: bool = False
    mode: str = "disabled"
    kill_switch: bool = True
    allowed_targets: frozenset[str] = field(default_factory=frozenset)
    max_bid_change_percent: float = 20.0
    max_budget_change_percent: float = 20.0
    tracking_max_age_minutes: float = 180.0
    allow_campaign_pause: bool = False
    compliance_policy_version: str = ""
    configuration_errors: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExecutionSettings":
        source = os.environ if env is None else env
        errors: list[str] = []
        app_env = source.get("APP_ENV", "development").strip().lower()
        if app_env not in {"development", "test", "staging", "production"}:
            errors.append("APP_ENV is not recognized")
            app_env = "development"

        mode = source.get("EXECUTION_MODE", "disabled").strip().lower()
        if mode not in EXECUTION_MODES:
            errors.append("EXECUTION_MODE is not recognized")
            mode = "disabled"

        targets = frozenset(
            item.strip()
            for item in source.get("EXECUTION_ALLOWED_TARGETS", "").split(",")
            if item.strip()
        )
        return cls(
            app_env=app_env,
            enabled=_strict_bool(source.get("EXECUTION_ENABLED"), False, "EXECUTION_ENABLED", errors),
            mode=mode,
            kill_switch=_strict_bool(source.get("EXECUTION_KILL_SWITCH"), True, "EXECUTION_KILL_SWITCH", errors),
            allowed_targets=targets,
            max_bid_change_percent=_positive_float(
                source.get("EXECUTION_MAX_BID_CHANGE_PERCENT"), 20.0,
                "EXECUTION_MAX_BID_CHANGE_PERCENT", errors,
            ),
            max_budget_change_percent=_positive_float(
                source.get("EXECUTION_MAX_BUDGET_CHANGE_PERCENT"), 20.0,
                "EXECUTION_MAX_BUDGET_CHANGE_PERCENT", errors,
            ),
            tracking_max_age_minutes=_positive_float(
                source.get("EXECUTION_TRACKING_MAX_AGE_MINUTES"), 180.0,
                "EXECUTION_TRACKING_MAX_AGE_MINUTES", errors,
            ),
            allow_campaign_pause=_strict_bool(
                source.get("EXECUTION_ALLOW_CAMPAIGN_PAUSE"), False,
                "EXECUTION_ALLOW_CAMPAIGN_PAUSE", errors,
            ),
            compliance_policy_version=source.get("COMPLIANCE_POLICY_VERSION", "").strip(),
            configuration_errors=tuple(errors),
        )


@dataclass(frozen=True)
class ServiceContext:
    service_id: str
    permissions: frozenset[str]
    verified: bool = False

    @classmethod
    def verified_service(cls, service_id: str, permissions: list[str] | tuple[str, ...]) -> "ServiceContext":
        return cls(service_id=service_id, permissions=frozenset(permissions), verified=True)


@dataclass(frozen=True)
class ExecutionRequestSpec:
    work_item_id: str
    expected_version: int
    approval_request_id: str
    execution_type: str
    target: str
    payload: Mapping[str, Any]
    compliance_policy_version: str
    correlation_id: str
    execution_request_id: str = ""


@dataclass(frozen=True)
class AdapterResult:
    status: str
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class ExecutionResult:
    execution_request_id: str
    status: str
    reason: str | None
    duplicate: bool
    payload_hash: str
    response_summary: Mapping[str, Any] | None = None


class AdapterTimeout(TimeoutError):
    """A timeout whose external outcome must be treated as unknown."""


class SafeAdapter(Protocol):
    name: str
    mode: str
    external_network: bool

    def execute(self, spec: ExecutionRequestSpec) -> AdapterResult: ...


class DisabledAdapter:
    name = "disabled"
    mode = "disabled"
    external_network = False

    def execute(self, spec: ExecutionRequestSpec) -> AdapterResult:
        raise RuntimeError("disabled adapter cannot execute")


class DryRunAdapter:
    """Build a redacted preview without contacting an external service."""

    name = "phase2_dry_run"
    mode = "dry_run"
    external_network = False

    def __init__(self, *, simulate_timeout: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.simulate_timeout = simulate_timeout

    def execute(self, spec: ExecutionRequestSpec) -> AdapterResult:
        preview = {
            "execution_type": spec.execution_type,
            "target": spec.target,
            "payload": redact_data(dict(spec.payload)),
            "transmitted": False,
        }
        self.calls.append(preview)
        if self.simulate_timeout:
            raise AdapterTimeout("simulated Phase 2 timeout")
        return AdapterResult(status="dry_run_succeeded", summary=preview)


class FakeReadOnlyAdapter:
    """Return a deterministic simulated read without network access."""

    name = "phase2_fake_read_only"
    mode = "read_only"
    external_network = False

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(self, spec: ExecutionRequestSpec) -> AdapterResult:
        preview = {
            "execution_type": spec.execution_type,
            "target": spec.target,
            "result": "simulated",
            "transmitted": False,
        }
        self.calls.append(preview)
        return AdapterResult(status="read_only_succeeded", summary=preview)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _idempotency_key(spec: ExecutionRequestSpec, payload_hash: str) -> str:
    raw = "|".join(
        [spec.work_item_id, str(spec.expected_version), spec.execution_type, spec.target, payload_hash]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _direct_settings_errors(settings: ExecutionSettings) -> tuple[str, ...]:
    errors = list(settings.configuration_errors)
    if settings.app_env not in {"development", "test", "staging", "production"}:
        errors.append("APP_ENV is not recognized")
    if settings.mode not in EXECUTION_MODES:
        errors.append("EXECUTION_MODE is not recognized")
    if settings.max_bid_change_percent <= 0:
        errors.append("EXECUTION_MAX_BID_CHANGE_PERCENT must be positive")
    if settings.max_budget_change_percent <= 0:
        errors.append("EXECUTION_MAX_BUDGET_CHANGE_PERCENT must be positive")
    if settings.tracking_max_age_minutes <= 0:
        errors.append("EXECUTION_TRACKING_MAX_AGE_MINUTES must be positive")
    return tuple(dict.fromkeys(errors))


def _guard_reason(
    conn: sqlite3.Connection,
    spec: ExecutionRequestSpec,
    service: ServiceContext,
    settings: ExecutionSettings,
    adapter: SafeAdapter,
) -> str | None:
    if _direct_settings_errors(settings):
        return "configuration_invalid"
    if not service.verified or not service.service_id:
        return "service_identity_unverified"
    if "execution:request" not in service.permissions:
        return "service_permission_denied"
    if not settings.enabled:
        return "execution_disabled"
    if settings.kill_switch:
        return "kill_switch_active"
    if settings.mode == "disabled":
        return "execution_mode_disabled"
    if spec.execution_type not in SUPPORTED_TYPES:
        return "unsupported_execution_type"
    if settings.mode == "read_only" and spec.execution_type not in READ_ONLY_TYPES:
        return "mode_disallows_operation"
    if type(adapter) not in {DisabledAdapter, DryRunAdapter, FakeReadOnlyAdapter}:
        return "untrusted_adapter_forbidden_phase2"
    if adapter.external_network:
        return "external_adapter_forbidden_phase2"
    if adapter.mode != settings.mode:
        return "adapter_mode_mismatch"
    if spec.target not in settings.allowed_targets:
        return "target_not_allowed"
    if not settings.compliance_policy_version or (
        spec.compliance_policy_version != settings.compliance_policy_version
    ):
        return "compliance_policy_version_mismatch"

    latest = conn.execute(
        "SELECT MAX(version) AS version FROM work_items WHERE work_item_id=?",
        (spec.work_item_id,),
    ).fetchone()
    if latest is None or latest["version"] is None:
        return "work_item_not_found"
    if int(latest["version"]) != spec.expected_version:
        return "stale_work_item_version"
    work = conn.execute(
        """SELECT status, agent_id, correlation_id, output_json FROM work_items
           WHERE work_item_id=? AND version=?""",
        (spec.work_item_id, spec.expected_version),
    ).fetchone()
    if work is None:
        return "work_item_not_found"
    if work["status"] != "approved":
        return "work_item_not_approved"
    if work["agent_id"] not in ALLOWED_AGENTS_BY_TYPE[spec.execution_type]:
        return "agent_not_authorized_for_execution_type"
    if not spec.correlation_id or spec.correlation_id != work["correlation_id"]:
        return "correlation_id_mismatch"
    approved_output = json.loads(work["output_json"])
    if spec.execution_type in WRITE_TYPES and (
        _payload_hash(spec.payload) != _payload_hash(approved_output)
    ):
        return "payload_not_approved"
    approval = conn.execute(
        """SELECT work_item_id, work_item_version, status
           FROM approval_requests WHERE request_id=?""",
        (spec.approval_request_id,),
    ).fetchone()
    if approval is None or approval["work_item_id"] != spec.work_item_id or (
        int(approval["work_item_version"]) != spec.expected_version
    ):
        return "approval_request_mismatch"
    if approval["status"] != "approved":
        return "approval_not_completed"

    if spec.execution_type in ADS_TYPES:
        tracking = conn.execute(
            "SELECT status, updated_at FROM tracking_status WHERE id=1"
        ).fetchone()
        if tracking is None:
            return "tracking_status_missing"
        try:
            tracked_at = dt.datetime.fromisoformat(tracking["updated_at"])
            if tracked_at.tzinfo is None:
                return "tracking_status_invalid"
            age = utc_now() - tracked_at.astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            return "tracking_status_invalid"
        if age < dt.timedelta(minutes=-5) or age > dt.timedelta(
            minutes=settings.tracking_max_age_minutes
        ):
            return "tracking_status_stale"
        if str(tracking["status"]).lower() == "critical":
            return "tracking_status_critical"

    if spec.execution_type == "ads_bid_update":
        if approved_output.get("action") not in {"INCREASE", "DECREASE"}:
            return "approved_action_mismatch"
        reason = _limit_reason(
            approved_output, "adjustment_percentage", settings.max_bid_change_percent
        )
        if reason:
            return "bid_change_" + reason
    if spec.execution_type == "ads_budget_update":
        if approved_output.get("action") not in {"INCREASE", "DECREASE"}:
            return "approved_action_mismatch"
        reason = _limit_reason(
            approved_output, "adjustment_percentage", settings.max_budget_change_percent
        )
        if reason:
            return "budget_change_" + reason
    if spec.execution_type == "ads_campaign_pause":
        if approved_output.get("action") != "PAUSE":
            return "approved_action_mismatch"
        if not settings.allow_campaign_pause:
            return "campaign_pause_not_allowed"
    return None


def _limit_reason(payload: Mapping[str, Any], field_name: str, limit: float) -> str | None:
    try:
        value = float(payload[field_name])
    except (KeyError, TypeError, ValueError):
        return "invalid"
    if abs(value) > limit:
        return "limit_exceeded"
    return None


def _json_preview(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(summarize_for_log(dict(value)), ensure_ascii=False, sort_keys=True)


def _write_final_state(
    conn: sqlite3.Connection,
    *,
    execution_request_id: str,
    status: str,
    reason: str | None,
    response_summary: Mapping[str, Any] | None,
    correlation_id: str,
    work_item_id: str,
    work_item_version: int,
    agent_id: str,
    service_id: str,
    previous_state: str = "requested",
) -> None:
    completed_at = utc_iso()
    response_json = _json_preview(response_summary)
    conn.execute(
        """UPDATE execution_requests
           SET status=?, blocked_reason=?, response_summary_json=?, completed_at=?
           WHERE execution_request_id=? AND status='requested'""",
        (status, reason, response_json, completed_at, execution_request_id),
    )
    conn.execute(
        """INSERT INTO execution_attempts (
               attempt_id, execution_request_id, status, reason,
               response_summary_json, correlation_id, started_at, completed_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()), execution_request_id, status, reason,
            response_json, correlation_id, completed_at, completed_at,
        ),
    )
    conn.execute(
        """INSERT INTO audit_events (
               event_id, event_type, work_item_id, work_item_version, agent_id,
               actor_type, actor_id, platform, occurred_at, previous_state,
               new_state, reason, correlation_id
           ) VALUES (?, ?, ?, ?, ?, 'service', ?, 'internal', ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()), f"execution.{status}", work_item_id,
            work_item_version, agent_id, service_id, completed_at,
            previous_state, status, reason, correlation_id,
        ),
    )


def execute_request(
    spec: ExecutionRequestSpec,
    service: ServiceContext,
    settings: ExecutionSettings,
    *,
    adapter: SafeAdapter | None = None,
    db_path: str | Path | None = None,
) -> ExecutionResult:
    """Validate, record and safely simulate one execution request.

    Duplicate requests return the original result and never call the adapter a
    second time.  A timeout is terminal ``unknown`` and is also never retried.
    """

    payload_hash = _payload_hash(spec.payload)
    idempotency_key = _idempotency_key(spec, payload_hash)
    execution_request_id = spec.execution_request_id or str(uuid.uuid4())
    safe_mode = settings.mode if settings.mode in EXECUTION_MODES else "disabled"
    selected_adapter: SafeAdapter
    if adapter is not None:
        selected_adapter = adapter
    elif safe_mode == "dry_run":
        selected_adapter = DryRunAdapter()
    elif safe_mode == "read_only":
        selected_adapter = FakeReadOnlyAdapter()
    else:
        selected_adapter = DisabledAdapter()

    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            """SELECT execution_request_id, status, blocked_reason,
                      response_summary_json
               FROM execution_requests WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if duplicate is not None:
            conn.commit()
            summary = (
                json.loads(duplicate["response_summary_json"])
                if duplicate["response_summary_json"] else None
            )
            return ExecutionResult(
                execution_request_id=duplicate["execution_request_id"],
                status=duplicate["status"],
                reason=duplicate["blocked_reason"],
                duplicate=True,
                payload_hash=payload_hash,
                response_summary=summary,
            )

        work = conn.execute(
            """SELECT agent_id, correlation_id FROM work_items
               WHERE work_item_id=? AND version=?""",
            (spec.work_item_id, spec.expected_version),
        ).fetchone()
        if work is None:
            conn.rollback()
            return ExecutionResult(
                execution_request_id=execution_request_id,
                status="blocked",
                reason="work_item_not_found",
                duplicate=False,
                payload_hash=payload_hash,
            )

        correlation_id = work["correlation_id"]
        created_at = utc_iso()
        conn.execute(
            """INSERT INTO execution_requests (
                   execution_request_id, work_item_id, work_item_version,
                   approval_request_id, execution_type, target,
                   payload_summary_json, payload_hash, idempotency_key,
                   compliance_policy_version, mode, status, requested_by,
                   adapter_name, correlation_id, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?)""",
            (
                execution_request_id, spec.work_item_id, spec.expected_version,
                spec.approval_request_id, spec.execution_type, spec.target,
                _json_preview(dict(spec.payload)), payload_hash, idempotency_key,
                spec.compliance_policy_version, safe_mode, service.service_id or "unverified",
                selected_adapter.name, correlation_id, created_at,
            ),
        )
        conn.execute(
            """INSERT INTO audit_events (
                   event_id, event_type, work_item_id, work_item_version, agent_id,
                   actor_type, actor_id, platform, occurred_at, previous_state,
                   new_state, reason, correlation_id
               ) VALUES (?, 'execution.requested', ?, ?, ?, 'service', ?,
                         'internal', ?, NULL, 'requested', ?, ?)""",
            (
                str(uuid.uuid4()), spec.work_item_id, spec.expected_version,
                work["agent_id"], service.service_id or "unverified", created_at,
                f"{spec.execution_type}:{spec.target}", correlation_id,
            ),
        )

        reason = _guard_reason(conn, spec, service, settings, selected_adapter)
        if reason:
            _write_final_state(
                conn,
                execution_request_id=execution_request_id,
                status="blocked",
                reason=reason,
                response_summary=None,
                correlation_id=correlation_id,
                work_item_id=spec.work_item_id,
                work_item_version=spec.expected_version,
                agent_id=work["agent_id"],
                service_id=service.service_id or "unverified",
            )
            conn.commit()
            return ExecutionResult(
                execution_request_id=execution_request_id,
                status="blocked",
                reason=reason,
                duplicate=False,
                payload_hash=payload_hash,
            )

        try:
            adapter_result = selected_adapter.execute(spec)
            expected_status = (
                "dry_run_succeeded" if settings.mode == "dry_run" else "read_only_succeeded"
            )
            if adapter_result.status != expected_status:
                raise RuntimeError("adapter returned an invalid Phase 2 status")
            status = adapter_result.status
            reason = None
            response_summary = adapter_result.summary
        except (AdapterTimeout, TimeoutError):
            status = "unknown"
            reason = "adapter_timeout_no_retry"
            response_summary = None
        except Exception:
            status = "failed"
            reason = "adapter_failed"
            response_summary = None

        _write_final_state(
            conn,
            execution_request_id=execution_request_id,
            status=status,
            reason=reason,
            response_summary=response_summary,
            correlation_id=correlation_id,
            work_item_id=spec.work_item_id,
            work_item_version=spec.expected_version,
            agent_id=work["agent_id"],
            service_id=service.service_id,
        )
        conn.commit()
        return ExecutionResult(
            execution_request_id=execution_request_id,
            status=status,
            reason=reason,
            duplicate=False,
            payload_hash=payload_hash,
            response_summary=(
                summarize_for_log(dict(response_summary)) if response_summary else None
            ),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        duplicate = conn.execute(
            """SELECT execution_request_id, status, blocked_reason,
                      response_summary_json FROM execution_requests
               WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if duplicate is None:
            raise
        return ExecutionResult(
            execution_request_id=duplicate["execution_request_id"],
            status=duplicate["status"],
            reason=duplicate["blocked_reason"],
            duplicate=True,
            payload_hash=payload_hash,
            response_summary=(
                json.loads(duplicate["response_summary_json"])
                if duplicate["response_summary_json"] else None
            ),
        )
    finally:
        conn.close()
