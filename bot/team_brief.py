"""Persist and advance a weekly content and advertising brief."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import closing
from pathlib import Path

from . import brief_inputs, workflow
from .agents import content_editor, data_assistant, keyword_analyst, performance_analyst
from .db import CODEX_TIMEOUT_ERROR, get_conn, get_work_item, utc_iso
from .schemas import DRAFT_TOPICS

LOGGER = logging.getLogger(__name__)
AGENTS = {
    "keyword": "keyword_analyst",
    "data": "data_assistant",
    "performance": "performance_analyst",
}


class TeamBriefs:
    def __init__(
        self, db_path: Path, team_id: str, channel_id: str, approval_mode: workflow.ContentApprovalMode,
        *, keyword_approval_mode: workflow.KeywordApprovalMode = workflow.KeywordApprovalMode.LEGAL_BEFORE_DRAFT,
    ):
        self.db_path = db_path
        self.team_id = team_id
        self.channel_id = channel_id
        self.approval_mode = approval_mode
        self.keyword_approval_mode = workflow.KeywordApprovalMode(keyword_approval_mode)

    def enqueue(self, *, trigger_id: str, user_id: str, scenario: str) -> str:
        brief_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes:brief:{self.team_id}:{trigger_id}"))
        with closing(get_conn(self.db_path)) as conn, conn:
            self._enqueue(conn, brief_id, user_id, scenario)
        return brief_id

    def _enqueue(self, conn, brief_id: str, user_id: str, scenario: str) -> None:
        created = conn.execute(
            """INSERT INTO team_briefs
               (brief_id, team_id, channel_id, user_id, tracking_scenario, created_at, keyword_approval_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(brief_id) DO NOTHING""",
            (
                brief_id, self.team_id, self.channel_id, user_id, scenario, utc_iso(),
                self.keyword_approval_mode.value,
            ),
        ).rowcount
        if created:
            conn.executemany(
                "INSERT INTO team_brief_jobs (brief_id, stage) VALUES (?, ?)",
                [(brief_id, stage) for stage in AGENTS],
            )

    def save_input(self, input_id: str, user_id: str, data: dict) -> dict:
        now = time.time()
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM team_brief_inputs WHERE brief_id IS NULL AND expires_at < ?", (now,))
            conn.execute(
                """INSERT INTO team_brief_inputs (input_id,team_id,channel_id,user_id,payload_json,expires_at)
                   VALUES (?,?,?,?,?,?) ON CONFLICT(input_id) DO NOTHING""",
                (input_id, self.team_id, self.channel_id, user_id, json.dumps(data), now + 1800),
            )
            row = conn.execute(
                "SELECT payload_json FROM team_brief_inputs WHERE input_id=? AND team_id=? AND channel_id=? AND user_id=?",
                (input_id, self.team_id, self.channel_id, user_id),
            ).fetchone()
            if row is None:
                raise workflow.NotFoundError("input not found")
            return json.loads(row["payload_json"])

    def confirm_input(self, input_id: str, user_id: str, *, allow_ga4: bool = True) -> str:
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT brief_id, payload_json FROM team_brief_inputs WHERE input_id=? AND team_id=? AND channel_id=?
                   AND user_id=? AND (brief_id IS NOT NULL OR expires_at >= ?)""",
                (input_id, self.team_id, self.channel_id, user_id, time.time()),
            ).fetchone()
            if row is None:
                raise workflow.NotFoundError("input not found or expired")
            if row["brief_id"]:
                return row["brief_id"]
            if not allow_ga4 and json.loads(row["payload_json"]).get("mode") == "ga4":
                raise brief_inputs.InputError("GA4 API 입력이 비활성화되어 있습니다.")
            brief_id = str(uuid.uuid4())
            self._enqueue(conn, brief_id, user_id, "imported")
            conn.execute("UPDATE team_brief_inputs SET brief_id=? WHERE input_id=?", (brief_id, input_id))
            return brief_id

    def imported_input(self, brief_id: str) -> dict | None:
        with closing(get_conn(self.db_path)) as conn:
            row = conn.execute(
                "SELECT payload_json FROM team_brief_inputs WHERE brief_id=? AND team_id=? AND channel_id=?",
                (brief_id, self.team_id, self.channel_id),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def get_brief(self, brief_id: str) -> dict | None:
        with closing(get_conn(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM team_briefs WHERE brief_id=? AND team_id=? AND channel_id=?",
                (brief_id, self.team_id, self.channel_id),
            ).fetchone()
        return dict(row) if row else None

    def retry_performance_timeout(
        self, *, brief_id: str, message_ts: str, retry_count: int, actor: workflow.ActorContext,
    ) -> bool:
        if not actor.verified or not actor.actor_id:
            raise workflow.AuthorizationError("verified actor required")
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """UPDATE team_brief_jobs SET status='queued', error=NULL, retry_count=retry_count+1
                   WHERE brief_id=? AND stage='performance' AND status='failed' AND error=? AND retry_count=?
                     AND EXISTS (
                         SELECT 1 FROM team_briefs b WHERE b.brief_id=team_brief_jobs.brief_id
                         AND b.team_id=? AND b.channel_id=? AND b.message_ts=? AND (b.user_id=? OR ?)
                     )""",
                (brief_id, CODEX_TIMEOUT_ERROR, retry_count, self.team_id, self.channel_id, message_ts,
                 actor.actor_id, bool(actor.roles & {"performance_approver", "workflow_admin"})),
            ).rowcount
            if updated:
                self._mark_dirty(conn, brief_id)
        if updated:
            LOGGER.info("Brief performance retry requested brief_id=%s attempt=%s", brief_id, retry_count + 1)
        return bool(updated)

    def snapshot(self, brief_id: str) -> tuple[dict[str, dict], list[dict], list[dict]]:
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN")
            jobs = {row["stage"]: dict(row) for row in conn.execute(
                "SELECT * FROM team_brief_jobs WHERE brief_id=?", (brief_id,)
            )}
            work = [dict(row) for row in conn.execute(
                """SELECT w.*, a.request_id, a.policy_key,
                          EXISTS (SELECT 1 FROM approval_decisions d
                                  WHERE d.request_id=a.request_id AND d.actor_role='keyword_approver'
                                    AND d.decision='approved') AS keyword_approved
                   FROM work_items w LEFT JOIN approval_requests a
                     ON a.work_item_id=w.work_item_id AND a.work_item_version=w.version
                   WHERE w.correlation_id=? AND w.version=(
                       SELECT MAX(v.version) FROM work_items v WHERE v.work_item_id=w.work_item_id
                   ) ORDER BY w.created_at, w.rowid""",
                (brief_id,),
            )]
            handoffs = [dict(row) for row in conn.execute(
                """SELECT * FROM handoff_jobs
                   WHERE correlation_id=? AND handoff_type='draft_content'""",
                (brief_id,),
            )]
        for item in work:
            item["output"] = json.loads(item.pop("output_json"))
        return jobs, work, handoffs

    def next_job(self) -> bool:
        with closing(get_conn(self.db_path)) as conn:
            job = conn.execute(
                """SELECT j.*, b.tracking_scenario FROM team_brief_jobs j
                   JOIN team_briefs b USING (brief_id)
                   JOIN team_brief_jobs d ON d.brief_id=b.brief_id AND d.stage='data'
                   WHERE b.team_id=? AND b.channel_id=? AND j.status='queued'
                     AND (j.stage!='performance' OR d.status!='queued')
                   ORDER BY b.created_at, CASE j.stage WHEN 'data' THEN 0
                       WHEN 'keyword' THEN 1 ELSE 2 END LIMIT 1""",
                (self.team_id, self.channel_id),
            ).fetchone()
            handoff = conn.execute(
                """SELECT b.brief_id FROM handoff_jobs h
                   JOIN team_briefs b ON b.brief_id=h.correlation_id
                   WHERE b.team_id=? AND b.channel_id=? AND h.handoff_type='draft_content'
                     AND h.status IN ('pending', 'claimed') ORDER BY h.created_at LIMIT 1""",
                (self.team_id, self.channel_id),
            ).fetchone()
        if handoff:
            self._draft(handoff["brief_id"])
            return True
        if job:
            self._analyze(dict(job))
            return True
        return False

    def _analyze(self, job: dict) -> None:
        brief_id, stage = job["brief_id"], job["stage"]
        started = time.monotonic()
        pending = []
        error = None

        def buffer_work_item(*, db_path, **fields):
            pending.append(fields)
            return {}

        try:
            args = {"db_path": self.db_path, "record_work_item": buffer_work_item}
            if job["tracking_scenario"] == "imported":
                data = self.imported_input(brief_id)
                if data is None:
                    raise workflow.NotFoundError("confirmed brief input is missing")
                self._analyze_imported(brief_id, stage, data, args)
            elif stage == "data":
                data_assistant.run_anomaly_check(job["tracking_scenario"], **args)
            elif stage == "keyword":
                keyword_analyst.run_daily_keyword_scan(**args)
            else:
                _, items, _ = self.snapshot(brief_id)
                tracking = next(
                    (item["output"] for item in items if item["agent_id"] == AGENTS["data"]), {}
                )
                if tracking.get("privacy_flags"):
                    issue = ("트래킹 점검에 개인정보 신호가 있습니다.", "마스킹 상태를 확인해 주세요.")
                elif tracking.get("status_summary") not in ("Normal", "Warning", "Critical"):
                    issue = ("같은 브리핑의 트래킹 점검 결과가 없습니다.", "새 브리핑으로 트래킹을 다시 점검해 주세요.")
                else:
                    issue = data_assistant.tracking_input_issue(tracking.get("tracking_input"))
                if issue:
                    reason, next_action = issue
                    pending.append({
                        "agent_id": AGENTS["performance"], "routed_to": "dev_team.queue",
                        "output": {"halted": True, "reason": reason, "next_action": next_action},
                        "model_version": "policy-engine/no-llm", "input_summary": "halt:tracking_unusable",
                    })
                else:
                    performance_analyst.run_bid_optimization(tracking["status_summary"], **args)
        except Exception as exc:
            error = type(exc).__name__
            LOGGER.warning("Brief stage failed brief_id=%s stage=%s error_type=%s", brief_id, stage, error)

        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """UPDATE team_brief_jobs SET status=?, error=?
                   WHERE brief_id=? AND stage=? AND status='queued' AND retry_count=?""",
                ("failed" if error else "completed", error, brief_id, stage, job["retry_count"]),
            ).rowcount
            if updated:
                if error is None:
                    for fields in pending:
                        workflow._insert_work_item(conn, correlation_id=brief_id, **fields)
                self._mark_dirty(conn, brief_id)
        if stage == "performance" and updated:
            LOGGER.info(
                "Brief performance finished brief_id=%s attempt=%s elapsed_ms=%d error_type=%s",
                brief_id, job["retry_count"], (time.monotonic() - started) * 1000, error or "-",
            )

    def _analyze_imported(self, brief_id: str, stage: str, data: dict, args: dict) -> None:
        issue = brief_inputs.hold_reason(data)
        if stage == "data":
            args["record_work_item"](
                db_path=self.db_path, agent_id=AGENTS["data"], routed_to="data_assistant.tracking_status",
                output={
                    "status_summary": "Warning" if issue else "Normal",
                    "reason": issue or "제출된 과거 보고서의 기간, 시간대, 일별 지표를 확인했습니다.",
                    "tracking_anomalies": data["warnings"],
                    "action_items": ["원본 보고서의 집계 상태와 누락 행을 확인해 주세요."] if issue else [],
                },
                model_version="policy-engine/no-llm", input_summary="verified_input:tracking",
            )
        elif stage == "keyword":
            if data["keywords"]:
                keyword_analyst.run_daily_keyword_scan(search_data=data["keywords"], **args)
        else:
            stages, _, _ = self.snapshot(brief_id)
            if stages["data"]["status"] != "completed":
                issue = "같은 브리핑의 데이터 점검이 완료되지 않았습니다."
            performance_analyst.record_imported_analysis(data, issue, **args)

    def _draft(self, brief_id: str) -> None:
        handoffs = workflow.claim_handoffs(
            "draft_content", limit=1, db_path=self.db_path, team_brief_id=brief_id,
        )
        if not handoffs:
            return
        row = handoffs[0]
        work_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "hermes:brief:draft:" + row["handoff_id"]))
        error = None
        if get_work_item(work_id, db_path=self.db_path) is None:
            try:
                keyword = row["output"]
                high_risk = keyword.get("compliance_risk") == "high"
                brief = self.get_brief(brief_id)
                if high_risk and brief["keyword_approval_mode"] == workflow.KeywordApprovalMode.DRAFT_ADVISORY.value:
                    category = keyword.get("draft_topic_category", "general")
                    keyword = {"keyword": DRAFT_TOPICS.get(category, DRAFT_TOPICS["general"])}
                content_editor._draft_content(
                    keyword, correlation_id=brief_id, db_path=self.db_path,
                    work_item_id=work_id, content_approval_mode=self.approval_mode,
                    collect_legal_claims=True, requires_legal_before_publish=high_risk,
                )
            except Exception as exc:
                error = type(exc).__name__
                LOGGER.warning("Brief draft failed brief_id=%s error_type=%s", brief_id, error)
        workflow.complete_handoff(
            row["handoff_id"], row["claim_token"], error=error, db_path=self.db_path,
        )

    @staticmethod
    def _mark_dirty(conn, brief_id: str) -> None:
        conn.execute(
            "UPDATE team_briefs SET sync_version=sync_version+1 WHERE brief_id=?", (brief_id,),
        )
