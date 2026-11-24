"""Platform-independent approval, acknowledgement, versioning, and handoff service."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urlsplit

from .db import CODEX_TIMEOUT_ERROR, get_conn, utc_iso, utc_now

Decision = Literal["approved", "rejected", "revision_requested"]


class WorkflowError(Exception):
    """Base class for workflow contract failures."""


class AuthorizationError(WorkflowError):
    pass


class InvalidTransitionError(WorkflowError):
    pass


class StaleActionError(WorkflowError):
    pass


class LegalEvidenceError(WorkflowError):
    pass


class NotFoundError(WorkflowError):
    pass


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    roles: frozenset[str]
    verified: bool
    is_admin: bool = False
    platform: str = "internal"

    @classmethod
    def verified_actor(cls, actor_id: str, roles: Iterable[str], *, is_admin: bool = False,
                       platform: str = "internal") -> "ActorContext":
        return cls(actor_id, frozenset(roles), True, is_admin, platform)


@dataclass(frozen=True)
class ApprovalPolicy:
    key: str
    roles: tuple[str, ...]
    ttl: dt.timedelta
    handoff_type: str | None
    handoff_target: str | None
    acknowledgement_roles: tuple[str, ...] = ()
    risk_level: str = "standard"
    initial_status: str = "pending_approval"


def _payload(output: dict | object) -> dict:
    if isinstance(output, dict):
        return output
    if hasattr(output, "model_dump"):
        return output.model_dump()
    raise TypeError("output must be a dict or Pydantic model")


class ContentApprovalMode(str, Enum):
    RISK_BASED = "risk_based"
    SINGLE = "single"


class KeywordApprovalMode(str, Enum):
    LEGAL_BEFORE_DRAFT = "legal_before_draft"
    DRAFT_ADVISORY = "draft_advisory"


def policy_for(
    agent_id: str, output_value: dict | object,
    *, content_approval_mode: ContentApprovalMode = ContentApprovalMode.RISK_BASED,
    keyword_approval_mode: KeywordApprovalMode = KeywordApprovalMode.LEGAL_BEFORE_DRAFT,
) -> ApprovalPolicy:
    output = _payload(output_value)
    if agent_id == "keyword_analyst":
        high = output.get("compliance_risk") == "high"
        keyword_approval_mode = KeywordApprovalMode(keyword_approval_mode)
        legal_required = high and keyword_approval_mode is KeywordApprovalMode.LEGAL_BEFORE_DRAFT
        key = "keyword_standard"
        if high:
            key = "keyword_high" if legal_required else "keyword_high_advisory"
        return ApprovalPolicy(
            key,
            ("keyword_approver", "legal_compliance") if legal_required else ("keyword_approver",),
            dt.timedelta(days=3 if high else 7), "draft_content", "content_editor.inbox",
            risk_level="high" if high else "standard",
        )
    if agent_id == "content_editor":
        source_requires_legal = output.get("requires_legal_before_publish") is True
        high = source_requires_legal or bool(
            output.get("flagged_claims") or output.get("sensitive_flags") or legal_claims(output)
        )
        content_approval_mode = ContentApprovalMode(content_approval_mode)
        needs_legal = source_requires_legal or (high and content_approval_mode is ContentApprovalMode.RISK_BASED)
        key = "content_legal" if needs_legal else "content_standard"
        if content_approval_mode is ContentApprovalMode.SINGLE and not needs_legal:
            key = "content_single"
        return ApprovalPolicy(
            key, ("content_approver", "legal_compliance") if needs_legal else ("content_approver",),
            dt.timedelta(days=3 if high else 7), "queue_for_publish", "publish_queue",
            risk_level="high" if high else "standard",
        )
    if agent_id == "performance_analyst":
        if output.get("halted"):
            return ApprovalPolicy("performance_halted", (), dt.timedelta(days=1), None, None,
                                  risk_level="high", initial_status="halted")
        roles = ["performance_approver"]
        if abs(int(output.get("adjustment_percentage", 0))) > 20 or output.get("action") == "PAUSE":
            roles.append("operations_owner")
        if output.get("sensitive_keyword") or output.get("category") in {"성범죄", "미성년자"}:
            roles.append("legal_compliance")
        return ApprovalPolicy(
            "performance_" + "_".join(roles[1:] or ["standard"]), tuple(roles),
            dt.timedelta(hours=24), "queue_for_execution_review", "performance_execution_pending",
            risk_level="high" if len(roles) > 1 else "standard",
        )
    if agent_id == "data_assistant":
        status = output.get("status_summary", "Normal")
        roles: list[str] = []
        if status in {"Warning", "Critical"}:
            roles.append("data_owner")
        if output.get("privacy_flags"):
            roles.append("privacy_officer")
        roles = list(dict.fromkeys(roles))
        return ApprovalPolicy(
            "data_" + status.lower(), (), dt.timedelta(days=7), None, None, tuple(roles),
            risk_level="high" if status == "Critical" or output.get("privacy_flags") else "standard",
            initial_status="acknowledgement_pending" if roles else "recorded",
        )
    raise ValueError(f"unknown agent_id: {agent_id}")


def _audit(conn, *, event_type: str, work: dict, actor_type: str, actor_id: str,
           platform: str, previous_state: str | None, new_state: str | None,
           reason: str | None, correlation_id: str) -> None:
    conn.execute(
        """INSERT INTO audit_events (
               event_id, event_type, work_item_id, work_item_version, agent_id,
               actor_type, actor_id, platform, model_id, occurred_at,
               previous_state, new_state, reason, correlation_id
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), event_type, work["work_item_id"], work["version"], work["agent_id"],
         actor_type, actor_id, platform, work.get("model_version"), utc_iso(), previous_state,
         new_state, reason, correlation_id),
    )


def create_work_item(*, agent_id: str, output: dict | object, routed_to: str,
                     model_version: str | None, input_summary: str,
                     created_by: str | None = None, correlation_id: str | None = None,
                     work_item_id: str | None = None, version: int = 1,
                     parent_version: int | None = None, creation_reason: str | None = None,
                     now: dt.datetime | None = None,
                     db_path: str | Path | None = None,
                     content_approval_mode: ContentApprovalMode = ContentApprovalMode.RISK_BASED) -> dict:
    with closing(get_conn(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        return _insert_work_item(
            conn, agent_id=agent_id, output=output, routed_to=routed_to,
            model_version=model_version, input_summary=input_summary, created_by=created_by,
            correlation_id=correlation_id, work_item_id=work_item_id, version=version,
            parent_version=parent_version, creation_reason=creation_reason, now=now,
            content_approval_mode=content_approval_mode,
        )


def _insert_work_item(
    conn, *, agent_id: str, output: dict | object, routed_to: str,
    model_version: str | None, input_summary: str, created_by: str | None = None,
    correlation_id: str | None = None, work_item_id: str | None = None, version: int = 1,
    parent_version: int | None = None, creation_reason: str | None = None,
    now: dt.datetime | None = None, editor: ActorContext | None = None,
    content_approval_mode: ContentApprovalMode = ContentApprovalMode.RISK_BASED,
) -> dict:
    payload = _payload(output)
    if agent_id == "content_editor" and parent_version is not None:
        parent = conn.execute(
            "SELECT output_json FROM work_items WHERE work_item_id=? AND version=?",
            (work_item_id, parent_version),
        ).fetchone()
        if parent and json.loads(parent["output_json"]).get("requires_legal_before_publish") is True:
            payload = {**payload, "requires_legal_before_publish": True}
    keyword_mode = KeywordApprovalMode.LEGAL_BEFORE_DRAFT
    if agent_id == "keyword_analyst" and correlation_id:
        brief = conn.execute(
            "SELECT keyword_approval_mode FROM team_briefs WHERE brief_id=?", (correlation_id,),
        ).fetchone()
        if brief:
            keyword_mode = KeywordApprovalMode(brief["keyword_approval_mode"])
    policy = policy_for(
        agent_id, payload, content_approval_mode=content_approval_mode,
        keyword_approval_mode=keyword_mode,
    )
    if agent_id == "content_editor" and payload.get("requires_legal_before_publish") is True:
        routed_to = "human_review.queue"
    if policy.key == "keyword_high_advisory":
        routed_to = "content_editor.inbox"
    current_time = now or utc_now()
    work_id = work_item_id or str(uuid.uuid4())
    correlation = correlation_id or str(uuid.uuid4())
    creator = created_by or f"agent:{agent_id}"
    request_id = str(uuid.uuid4()) if policy.roles else None

    work = {"work_item_id": work_id, "version": version, "agent_id": agent_id,
            "model_version": model_version}
    conn.execute(
        """INSERT INTO work_items (
               work_item_id, version, agent_id, created_at, created_by, output_json,
               routed_to, risk_level, status, model_version, input_summary,
               correlation_id, parent_version
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (work_id, version, agent_id, utc_iso(current_time), creator,
         json.dumps(payload, ensure_ascii=False), routed_to, policy.risk_level,
         policy.initial_status, model_version, input_summary, correlation, parent_version),
    )
    event_type = "work_item.revision_created" if parent_version else "work_item.created"
    _audit(conn, event_type=event_type, work=work, actor_type="human" if editor else "agent",
           actor_id=creator, platform=editor.platform if editor else "internal", previous_state=None,
           new_state=policy.initial_status, reason=creation_reason, correlation_id=correlation)
    if request_id:
        conn.execute(
            """INSERT INTO approval_requests (
                   request_id, work_item_id, work_item_version, policy_key, status,
                   created_at, expires_at, requested_by, correlation_id, distinct_approvers
               ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, 1)""",
            (request_id, work_id, version, policy.key, utc_iso(current_time),
             utc_iso(current_time + policy.ttl), creator, correlation),
        )
        conn.executemany(
            "INSERT INTO approval_requirements (request_id, role_key, approvals_required) VALUES (?, ?, 1)",
            [(request_id, role) for role in policy.roles],
        )
        _audit(conn, event_type="approval.requested", work=work, actor_type="system",
               actor_id="workflow", platform="internal", previous_state=None,
               new_state="pending", reason=policy.key, correlation_id=correlation)
    conn.executemany(
        """INSERT INTO acknowledgement_requirements
               (work_item_id, work_item_version, role_key, acknowledgements_required)
           VALUES (?, ?, ?, 1)""",
        [(work_id, version, role) for role in policy.acknowledgement_roles],
    )
    return {"work_item_id": work_id, "version": version, "request_id": request_id,
            "status": policy.initial_status, "policy_key": policy.key,
            "required_roles": list(policy.roles),
            "acknowledgement_roles": list(policy.acknowledgement_roles),
            "correlation_id": correlation}


def _require_actor(actor: ActorContext) -> None:
    if not actor.verified or not actor.actor_id or not actor.roles:
        raise AuthorizationError("actor identity and roles must be verified")


def get_approval_state(request_id: str, db_path: str | Path | None = None) -> dict:
    conn = get_conn(db_path)
    request = conn.execute("SELECT * FROM approval_requests WHERE request_id=?", (request_id,)).fetchone()
    if request is None:
        conn.close()
        raise NotFoundError(f"approval request not found: {request_id}")
    requirements = conn.execute(
        "SELECT role_key, approvals_required FROM approval_requirements WHERE request_id=? ORDER BY role_key",
        (request_id,),
    ).fetchall()
    counts = {row["actor_role"]: row["count"] for row in conn.execute(
        """SELECT actor_role, COUNT(*) count FROM approval_decisions
           WHERE request_id=? AND decision='approved' GROUP BY actor_role""", (request_id,)
    ).fetchall()}
    decisions = [dict(row) for row in conn.execute(
        "SELECT actor_id, actor_role, decision, reason, decided_at FROM approval_decisions WHERE request_id=? ORDER BY decided_at",
        (request_id,),
    ).fetchall()]
    conn.close()
    return {"request_id": request_id, "work_item_id": request["work_item_id"],
            "version": request["work_item_version"], "status": request["status"],
            "expires_at": request["expires_at"], "policy_key": request["policy_key"],
            "requirements": [{"role": row["role_key"], "required": row["approvals_required"],
                              "approved": counts.get(row["role_key"], 0)} for row in requirements],
            "decisions": decisions}


def legal_claims(output: dict) -> list[str]:
    return list(dict.fromkeys(claim.strip() for claim in output.get("legal_claims", []) if claim.strip()))


def read_legal_reviews(request_id: str, db_path: str | Path | None = None) -> dict[int, dict]:
    with closing(get_conn(db_path)) as conn:
        return {
            row["claim_index"]: dict(row)
            for row in conn.execute("SELECT * FROM legal_evidence_reviews WHERE request_id=?", (request_id,))
        }


def _official_source(url: str) -> bool:
    if len(url) > 1800 or any(
        char.isspace() or not char.isprintable() or char in '\\<>|"'
        for char in url
    ):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https" and parsed.username is None and parsed.port in {None, 443}
            and any(host == domain or host.endswith("." + domain)
                    for domain in ("law.go.kr", "scourt.go.kr", "ccourt.go.kr"))
            and parsed.path not in {"", "/"}
        )
    except ValueError:
        return False


def record_legal_review(
    *, request_id: str, expected_version: int, claim_index: int, actor: ActorContext,
    status: str | None, source_url: str, source_reference: str, note: str, db_path: str | Path | None = None,
) -> None:
    _require_actor(actor)
    source_url, source_reference, note = source_url.strip(), source_reference.strip(), note.strip()
    if status is not None and status not in {"confirmed", "needs_revision"}:
        raise LegalEvidenceError("검토 결과를 다시 선택해 주세요.")
    if status is None and not (source_url or source_reference or note):
        raise LegalEvidenceError("검토 결과 또는 근거를 하나 이상 입력해 주세요.")
    if len(source_reference) > 300 or len(note) > 1200:
        raise LegalEvidenceError("근거 정보 또는 검토 내용이 너무 깁니다.")
    status = status or "noted"
    if source_url and not _official_source(source_url):
        raise LegalEvidenceError("국가법령정보센터·법원·헌법재판소의 HTTPS 원문 URL을 입력해 주세요.")

    with closing(get_conn(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        work = conn.execute(
            """SELECT w.*, a.status AS request_status, a.expires_at, a.policy_key
               FROM approval_requests a JOIN work_items w
               ON w.work_item_id=a.work_item_id AND w.version=a.work_item_version
               WHERE a.request_id=? AND w.agent_id='content_editor' AND w.version=?
                 AND NOT EXISTS (
                     SELECT 1 FROM work_items newer
                     WHERE newer.work_item_id=w.work_item_id AND newer.version>w.version
                 )""",
            (request_id, expected_version),
        ).fetchone()
        if (work is None or work["request_status"] != "pending"
                or utc_now() >= dt.datetime.fromisoformat(work["expires_at"])):
            raise LegalEvidenceError("승인 대기 중인 최신 초안에서만 검토를 저장할 수 있습니다.")
        role = "content_approver" if work["policy_key"] == "content_single" else "legal_compliance"
        if role not in actor.roles:
            raise AuthorizationError("missing legal evidence reviewer role")
        claims = legal_claims(json.loads(work["output_json"]))
        if not 0 <= claim_index < len(claims):
            raise LegalEvidenceError("검토 대상 문장을 찾을 수 없습니다.")
        if conn.execute(
            "SELECT 1 FROM approval_decisions WHERE request_id=? AND decision='approved'", (request_id,)
        ).fetchone():
            raise LegalEvidenceError("승인이 시작된 초안은 새 버전을 만들어 다시 검토해 주세요.")

        current = (status, source_url, source_reference, note, actor.actor_id)
        previous = conn.execute(
            """SELECT status, source_url, source_reference, note, reviewed_by
               FROM legal_evidence_reviews WHERE request_id=? AND claim_index=?""",
            (request_id, claim_index),
        ).fetchone()
        if previous is not None and tuple(previous) == current:
            return
        conn.execute(
            """INSERT INTO legal_evidence_reviews (
                   request_id, claim_index, status, source_url, source_reference, note, reviewed_by, reviewed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(request_id, claim_index) DO UPDATE SET
                   status=excluded.status, source_url=excluded.source_url,
                   source_reference=excluded.source_reference, note=excluded.note,
                   reviewed_by=excluded.reviewed_by, reviewed_at=excluded.reviewed_at""",
            (request_id, claim_index, *current, utc_iso()),
        )
        _audit(
            conn, event_type="legal_evidence.reviewed", work=dict(work), actor_type="human",
            actor_id=actor.actor_id, platform=actor.platform,
            previous_state=previous["status"] if previous is not None else None,
            new_state=status, reason=json.dumps({
                "claim_index": claim_index, "source_url": source_url,
                "source_reference": source_reference, "note": note,
            }, ensure_ascii=False), correlation_id=work["correlation_id"],
        )


def decide(*, request_id: str, expected_version: int, actor: ActorContext,
           acting_role: str, decision: Decision, reason: str | None = None,
           correlation_id: str | None = None, now: dt.datetime | None = None,
           db_path: str | Path | None = None) -> dict:
    _require_actor(actor)
    if acting_role not in actor.roles:
        raise AuthorizationError(f"actor is not verified for role: {acting_role}")
    if decision not in {"approved", "rejected", "revision_requested"}:
        raise ValueError(f"unsupported decision: {decision}")
    if decision in {"rejected", "revision_requested"} and not (reason or "").strip():
        raise ValueError(f"reason is required for {decision}")

    current_time = now or utc_now()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        request = conn.execute("SELECT * FROM approval_requests WHERE request_id=?", (request_id,)).fetchone()
        if request is None:
            raise NotFoundError(f"approval request not found: {request_id}")
        if request["work_item_version"] != expected_version:
            raise StaleActionError("approval action targets an old work item version")
        existing = conn.execute(
            "SELECT * FROM approval_decisions WHERE request_id=? AND actor_id=?",
            (request_id, actor.actor_id),
        ).fetchone()
        if existing is not None:
            if existing["actor_role"] == acting_role and existing["decision"] == decision:
                conn.rollback()
                state = get_approval_state(request_id, db_path)
                state["duplicate"] = True
                return state
            raise InvalidTransitionError("actor has already decided this request")
        if request["status"] != "pending":
            raise StaleActionError(f"approval request is already {request['status']}")
        work = conn.execute(
            "SELECT * FROM work_items WHERE work_item_id=? AND version=?",
            (request["work_item_id"], expected_version),
        ).fetchone()
        if current_time >= dt.datetime.fromisoformat(request["expires_at"]):
            conn.execute("UPDATE approval_requests SET status='expired', resolved_at=? WHERE request_id=?",
                         (utc_iso(current_time), request_id))
            conn.execute("UPDATE work_items SET status='expired' WHERE work_item_id=? AND version=?",
                         (request["work_item_id"], expected_version))
            _audit(conn, event_type="approval.expired", work=dict(work), actor_type="system",
                   actor_id="workflow", platform="internal", previous_state="pending",
                   new_state="expired", reason="approval TTL elapsed",
                   correlation_id=request["correlation_id"])
            conn.commit()
            raise StaleActionError("approval request has expired")
        requirement = conn.execute(
            "SELECT 1 FROM approval_requirements WHERE request_id=? AND role_key=?",
            (request_id, acting_role),
        ).fetchone()
        if requirement is None:
            raise AuthorizationError(f"role is not required for this request: {acting_role}")

        correlation = correlation_id or request["correlation_id"]
        conn.execute(
            """INSERT INTO approval_decisions
                   (decision_id, request_id, actor_id, actor_role, decision, reason, decided_at, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), request_id, actor.actor_id, acting_role, decision,
             reason, utc_iso(current_time), correlation),
        )
        _audit(conn, event_type=f"approval.decision.{decision}", work=dict(work),
               actor_type="human", actor_id=actor.actor_id, platform=actor.platform,
               previous_state="pending", new_state=decision if decision != "approved" else "pending",
               reason=reason, correlation_id=correlation)

        final_status = "pending"
        if decision in {"rejected", "revision_requested"}:
            final_status = decision
        else:
            missing = conn.execute(
                """SELECT COUNT(*) FROM approval_requirements r
                   WHERE r.request_id=? AND (
                       SELECT COUNT(*) FROM approval_decisions d
                       WHERE d.request_id=r.request_id AND d.actor_role=r.role_key AND d.decision='approved'
                   ) < r.approvals_required""", (request_id,),
            ).fetchone()[0]
            if missing == 0:
                final_status = "approved"

        if final_status != "pending":
            conn.execute(
                "UPDATE approval_requests SET status=?, resolved_at=? WHERE request_id=? AND status='pending'",
                (final_status, utc_iso(current_time), request_id),
            )
            conn.execute("UPDATE work_items SET status=? WHERE work_item_id=? AND version=?",
                         (final_status, request["work_item_id"], expected_version))
            _audit(conn, event_type=f"approval.request.{final_status}", work=dict(work),
                   actor_type="system", actor_id="workflow", platform="internal",
                   previous_state="pending", new_state=final_status, reason=reason,
                   correlation_id=correlation)
            if final_status == "approved":
                policy = policy_for(work["agent_id"], json.loads(work["output_json"]))
                if policy.handoff_type and policy.handoff_target:
                    inserted = conn.execute(
                        """INSERT OR IGNORE INTO handoff_jobs
                               (handoff_id, work_item_id, work_item_version, handoff_type,
                                target, status, created_at, correlation_id)
                           VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                        (str(uuid.uuid4()), work["work_item_id"], expected_version,
                         policy.handoff_type, policy.handoff_target, utc_iso(current_time), correlation),
                    ).rowcount
                    if inserted:
                        _audit(conn, event_type="handoff.queued", work=dict(work),
                               actor_type="system", actor_id="workflow", platform="internal",
                               previous_state=None, new_state="pending", reason=policy.handoff_type,
                               correlation_id=correlation)
        conn.commit()
    except StaleActionError:
        if conn.in_transaction:
            conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_approval_state(request_id, db_path)


def acknowledge(*, work_item_id: str, expected_version: int, actor: ActorContext,
                acting_role: str, correlation_id: str | None = None,
                db_path: str | Path | None = None) -> dict:
    _require_actor(actor)
    if acting_role not in actor.roles:
        raise AuthorizationError(f"actor is not verified for role: {acting_role}")
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        work = conn.execute("SELECT * FROM work_items WHERE work_item_id=? AND version=?",
                            (work_item_id, expected_version)).fetchone()
        if work is None:
            raise NotFoundError(f"work item not found: {work_item_id} v{expected_version}")
        if work["status"] not in {"acknowledgement_pending", "acknowledged"}:
            raise StaleActionError(f"work item does not accept acknowledgements: {work['status']}")
        requirement = conn.execute(
            """SELECT 1 FROM acknowledgement_requirements
               WHERE work_item_id=? AND work_item_version=? AND role_key=?""",
            (work_item_id, expected_version, acting_role),
        ).fetchone()
        if requirement is None:
            raise AuthorizationError(f"role is not required to acknowledge: {acting_role}")
        existing = conn.execute(
            "SELECT * FROM acknowledgements WHERE work_item_id=? AND work_item_version=? AND actor_id=?",
            (work_item_id, expected_version, actor.actor_id),
        ).fetchone()
        if existing:
            if existing["actor_role"] != acting_role:
                raise InvalidTransitionError("actor has already acknowledged under another role")
            conn.rollback()
            return {"work_item_id": work_item_id, "version": expected_version,
                    "status": work["status"], "duplicate": True}
        correlation = correlation_id or work["correlation_id"]
        conn.execute(
            """INSERT INTO acknowledgements
                   (acknowledgement_id, work_item_id, work_item_version, actor_id,
                    actor_role, acknowledged_at, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), work_item_id, expected_version, actor.actor_id,
             acting_role, utc_iso(), correlation),
        )
        missing = conn.execute(
            """SELECT COUNT(*) FROM acknowledgement_requirements r
               WHERE r.work_item_id=? AND r.work_item_version=? AND (
                   SELECT COUNT(*) FROM acknowledgements a
                   WHERE a.work_item_id=r.work_item_id AND a.work_item_version=r.work_item_version
                     AND a.actor_role=r.role_key
               ) < r.acknowledgements_required""", (work_item_id, expected_version),
        ).fetchone()[0]
        new_status = "acknowledged" if missing == 0 else "acknowledgement_pending"
        conn.execute("UPDATE work_items SET status=? WHERE work_item_id=? AND version=?",
                     (new_status, work_item_id, expected_version))
        _audit(conn, event_type="data.acknowledged", work=dict(work), actor_type="human",
               actor_id=actor.actor_id, platform=actor.platform, previous_state=work["status"],
               new_state=new_status, reason=acting_role, correlation_id=correlation)
        conn.commit()
        return {"work_item_id": work_item_id, "version": expected_version, "status": new_status}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_request(request_id: str, actor: ActorContext, *, reason: str,
                   db_path: str | Path | None = None) -> dict:
    _require_actor(actor)
    if not reason.strip():
        raise ValueError("reason is required for cancellation")
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        request = conn.execute("SELECT * FROM approval_requests WHERE request_id=?", (request_id,)).fetchone()
        if request is None:
            raise NotFoundError(f"approval request not found: {request_id}")
        if request["status"] != "pending":
            raise InvalidTransitionError(f"cannot cancel {request['status']} request")
        if not actor.is_admin and actor.actor_id != request["requested_by"]:
            raise AuthorizationError("only the requester or an administrator can cancel")
        work = conn.execute("SELECT * FROM work_items WHERE work_item_id=? AND version=?",
                            (request["work_item_id"], request["work_item_version"])).fetchone()
        conn.execute("UPDATE approval_requests SET status='cancelled', resolved_at=? WHERE request_id=?",
                     (utc_iso(), request_id))
        conn.execute("UPDATE work_items SET status='cancelled' WHERE work_item_id=? AND version=?",
                     (request["work_item_id"], request["work_item_version"]))
        _audit(conn, event_type="approval.request.cancelled", work=dict(work), actor_type="human",
               actor_id=actor.actor_id, platform=actor.platform, previous_state="pending",
               new_state="cancelled", reason=reason, correlation_id=request["correlation_id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_approval_state(request_id, db_path)


def expire_pending(*, now: dt.datetime | None = None, db_path: str | Path | None = None) -> int:
    current_time = now or utc_now()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM approval_requests WHERE status='pending' AND expires_at<=?",
            (utc_iso(current_time),),
        ).fetchall()
        for request in rows:
            work = conn.execute("SELECT * FROM work_items WHERE work_item_id=? AND version=?",
                                (request["work_item_id"], request["work_item_version"])).fetchone()
            conn.execute("UPDATE approval_requests SET status='expired', resolved_at=? WHERE request_id=?",
                         (utc_iso(current_time), request["request_id"]))
            conn.execute("UPDATE work_items SET status='expired' WHERE work_item_id=? AND version=?",
                         (request["work_item_id"], request["work_item_version"]))
            _audit(conn, event_type="approval.expired", work=dict(work), actor_type="system",
                   actor_id="workflow", platform="internal", previous_state="pending",
                   new_state="expired", reason="approval TTL elapsed",
                   correlation_id=request["correlation_id"])
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_revision(*, source_request_id: str, revised_output: dict | object,
                    created_by: str, reason: str, model_version: str | None = None,
                    db_path: str | Path | None = None) -> dict:
    if not reason.strip():
        raise ValueError("revision reason is required")
    conn = get_conn(db_path)
    request = conn.execute("SELECT * FROM approval_requests WHERE request_id=?", (source_request_id,)).fetchone()
    if request is None:
        conn.close()
        raise NotFoundError(f"approval request not found: {source_request_id}")
    if request["status"] != "revision_requested":
        conn.close()
        raise InvalidTransitionError("a new version requires revision_requested status")
    old = conn.execute("SELECT * FROM work_items WHERE work_item_id=? AND version=?",
                       (request["work_item_id"], request["work_item_version"])).fetchone()
    latest = conn.execute("SELECT MAX(version) FROM work_items WHERE work_item_id=?",
                          (request["work_item_id"],)).fetchone()[0]
    conn.close()
    if latest != old["version"]:
        raise StaleActionError("a newer work item version already exists")
    return create_work_item(
        agent_id=old["agent_id"], output=revised_output, routed_to=old["routed_to"],
        model_version=model_version or old["model_version"], input_summary=old["input_summary"],
        created_by=created_by, correlation_id=old["correlation_id"],
        work_item_id=old["work_item_id"], version=old["version"] + 1,
        parent_version=old["version"], creation_reason=reason, db_path=db_path,
    )


def revise_pending_content(*, request_id: str, expected_version: int, actor: ActorContext,
                           revised_output: dict, db_path: str | Path | None = None) -> dict:
    """Replace a pending content draft and its approval request in one transaction."""
    _require_actor(actor)
    if "content_approver" not in actor.roles:
        raise AuthorizationError("content editor must be a verified content approver")
    now = utc_now()
    with closing(get_conn(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        original = conn.execute(
            """SELECT w.*, a.status AS approval_status, a.expires_at, a.policy_key
               FROM approval_requests a JOIN work_items w
                 ON w.work_item_id=a.work_item_id AND w.version=a.work_item_version
               WHERE a.request_id=? AND w.agent_id='content_editor'""", (request_id,),
        ).fetchone()
        if original is None:
            raise NotFoundError("content approval request not found")
        latest = conn.execute(
            "SELECT MAX(version) FROM work_items WHERE work_item_id=?",
            (original["work_item_id"],),
        ).fetchone()[0]
        if (original["approval_status"] != "pending" or original["version"] != expected_version
                or latest != expected_version or now >= dt.datetime.fromisoformat(original["expires_at"])):
            raise StaleActionError("only the latest pending, unexpired content can be edited")
        conn.execute(
            "UPDATE approval_requests SET status='revision_requested', resolved_at=? WHERE request_id=?",
            (utc_iso(now), request_id),
        )
        conn.execute(
            "UPDATE work_items SET status='revision_requested' WHERE work_item_id=? AND version=?",
            (original["work_item_id"], expected_version),
        )
        reason = "Content edited in Slack"
        _audit(conn, event_type="approval.request.revision_requested", work=dict(original),
               actor_type="human", actor_id=actor.actor_id, platform=actor.platform,
               previous_state="pending", new_state="revision_requested", reason=reason,
               correlation_id=original["correlation_id"])
        approval_mode = (
            ContentApprovalMode.SINGLE if original["policy_key"] == "content_single"
            else ContentApprovalMode.RISK_BASED
        )
        policy = policy_for("content_editor", revised_output, content_approval_mode=approval_mode)
        return _insert_work_item(
            conn, agent_id="content_editor", output=revised_output,
            routed_to="human_review.queue" if policy.risk_level == "high" else "publish_queue",
            model_version=original["model_version"], input_summary=original["input_summary"],
            created_by=actor.actor_id, correlation_id=original["correlation_id"],
            work_item_id=original["work_item_id"], version=expected_version + 1,
            parent_version=expected_version, creation_reason=reason, now=now, editor=actor,
            content_approval_mode=approval_mode,
        )


def retry_team_draft_timeout(
    *, actor: ActorContext, team_id: str, channel_id: str, message_ts: str,
    handoff_id: str, claim_token: str, db_path: str | Path | None = None,
) -> bool:
    _require_actor(actor)
    if "keyword_approver" not in actor.roles:
        raise AuthorizationError("keyword approver role required")
    with closing(get_conn(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        handoff = conn.execute(
            """SELECT h.*, w.version, w.agent_id, w.model_version
               FROM handoff_jobs h
               JOIN team_briefs b ON b.brief_id=h.correlation_id
               JOIN work_items w ON w.work_item_id=h.work_item_id AND w.version=h.work_item_version
               WHERE b.team_id=? AND b.channel_id=? AND b.message_ts=?
                 AND h.handoff_id=? AND h.claim_token=? AND h.handoff_type='draft_content'
                 AND h.status='failed' AND h.error=? AND w.status='approved'
                 AND NOT EXISTS (
                     SELECT 1 FROM work_items newer
                     WHERE newer.work_item_id=w.work_item_id AND newer.version>w.version
                 )""",
            (team_id, channel_id, message_ts, handoff_id, claim_token, CODEX_TIMEOUT_ERROR),
        ).fetchone()
        if handoff is None:
            return False
        conn.execute(
            """UPDATE handoff_jobs SET status='pending', claimed_at=NULL, completed_at=NULL,
                   claim_token=NULL, error=NULL WHERE handoff_id=?""",
            (handoff_id,),
        )
        _audit(
            conn, event_type="handoff.retry_requested", work=dict(handoff),
            actor_type="human", actor_id=actor.actor_id, platform=actor.platform,
            previous_state="failed", new_state="pending", reason=CODEX_TIMEOUT_ERROR,
            correlation_id=handoff["correlation_id"],
        )
    return True


def claim_handoffs(handoff_type: str, *, limit: int = 10,
                   db_path: str | Path | None = None,
                   team_brief_id: str | None = None) -> list[dict]:
    """Team consumers hold an exclusive worker lock and can resume their own claim."""
    conn = get_conn(db_path)
    claimed: list[dict] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT h.*, w.output_json, w.agent_id, w.routed_to
               FROM handoff_jobs h JOIN work_items w
                 ON w.work_item_id=h.work_item_id AND w.version=h.work_item_version
               WHERE h.handoff_type=?
                 AND (
                   (? IS NULL AND h.status='pending' AND NOT EXISTS (
                       SELECT 1 FROM team_briefs b WHERE b.brief_id=h.correlation_id
                   ))
                   OR (h.correlation_id=? AND h.status IN ('pending', 'claimed'))
                 )
               ORDER BY h.created_at LIMIT ?""", (handoff_type, team_brief_id, team_brief_id, limit),
        ).fetchall()
        for row in rows:
            token = str(uuid.uuid4())
            if conn.execute(
                """UPDATE handoff_jobs SET status='claimed', claimed_at=?, claim_token=?
                   WHERE handoff_id=? AND status=?""",
                (utc_iso(), token, row["handoff_id"], row["status"]),
            ).rowcount:
                item = dict(row)
                item["output"] = json.loads(item.pop("output_json"))
                item["version"] = item["work_item_version"]
                item["claim_token"] = token
                claimed.append(item)
                _audit(
                    conn, event_type="handoff.claimed", work=item,
                    actor_type="service", actor_id=f"consumer:{handoff_type}", platform="internal",
                    previous_state=row["status"], new_state="claimed", reason=handoff_type,
                    correlation_id=item["correlation_id"],
                )
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_handoff(handoff_id: str, claim_token: str, *, error: str | None = None,
                     db_path: str | Path | None = None) -> None:
    conn = get_conn(db_path)
    status = "failed" if error else "completed"
    try:
        conn.execute("BEGIN IMMEDIATE")
        handoff = conn.execute(
            """SELECT h.*, w.agent_id, w.model_version FROM handoff_jobs h
               JOIN work_items w ON w.work_item_id=h.work_item_id AND w.version=h.work_item_version
               WHERE h.handoff_id=?""", (handoff_id,),
        ).fetchone()
        updated = conn.execute(
            """UPDATE handoff_jobs SET status=?, completed_at=?, error=?
               WHERE handoff_id=? AND status='claimed' AND claim_token=?""",
            (status, utc_iso(), error, handoff_id, claim_token),
        ).rowcount
        if updated == 1:
            work = dict(handoff)
            work["version"] = work["work_item_version"]
            _audit(
                conn, event_type=f"handoff.{status}", work=work, actor_type="service",
                actor_id=f"consumer:{handoff['handoff_type']}", platform="internal",
                previous_state="claimed", new_state=status, reason=error,
                correlation_id=handoff["correlation_id"],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if updated != 1:
        raise StaleActionError("handoff is not owned by this claim")
