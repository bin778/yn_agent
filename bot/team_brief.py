"""Persist and advance a weekly content and advertising brief."""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import closing
from pathlib import Path

from . import workflow
from .agents import content_editor, data_assistant, keyword_analyst, performance_analyst
from .db import get_conn, get_work_item, utc_iso

LOGGER = logging.getLogger(__name__)
AGENTS = {
    "keyword": "keyword_analyst",
    "data": "data_assistant",
    "performance": "performance_analyst",
}


class TeamBriefs:
    def __init__(self, db_path: Path, team_id: str, channel_id: str, approval_mode):
        self.db_path = db_path
        self.team_id = team_id
        self.channel_id = channel_id
        self.approval_mode = approval_mode

    def enqueue(self, *, trigger_id: str, user_id: str, scenario: str) -> str:
        brief_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes:brief:{self.team_id}:{trigger_id}"))
        with closing(get_conn(self.db_path)) as conn, conn:
            created = conn.execute(
                """INSERT INTO team_briefs
                   (brief_id, team_id, channel_id, user_id, tracking_scenario, created_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(brief_id) DO NOTHING""",
                (brief_id, self.team_id, self.channel_id, user_id, scenario, utc_iso()),
            ).rowcount
            if created:
                conn.executemany(
                    "INSERT INTO team_brief_jobs (brief_id, stage) VALUES (?, ?)",
                    [(brief_id, stage) for stage in AGENTS],
                )
        return brief_id

    def get_brief(self, brief_id: str) -> dict | None:
        with closing(get_conn(self.db_path)) as conn:
            row = conn.execute(
                "SELECT * FROM team_briefs WHERE brief_id=? AND team_id=? AND channel_id=?",
                (brief_id, self.team_id, self.channel_id),
            ).fetchone()
        return dict(row) if row else None

    def snapshot(self, brief_id: str) -> tuple[dict[str, dict], list[dict], list[dict]]:
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN")
            jobs = {row["stage"]: dict(row) for row in conn.execute(
                "SELECT * FROM team_brief_jobs WHERE brief_id=?", (brief_id,)
            )}
            work = [dict(row) for row in conn.execute(
                """SELECT w.*, a.request_id FROM work_items w LEFT JOIN approval_requests a
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
        pending = []
        error = None

        def buffer_work_item(*, db_path, **fields):
            pending.append(fields)
            return {}

        try:
            args = {"db_path": self.db_path, "record_work_item": buffer_work_item}
            if stage == "data":
                data_assistant.run_anomaly_check(job["tracking_scenario"], **args)
            elif stage == "keyword":
                keyword_analyst.run_daily_keyword_scan(**args)
            else:
                _, items, _ = self.snapshot(brief_id)
                tracking = next(
                    (item["output"] for item in items if item["agent_id"] == AGENTS["data"]), {}
                )
                if tracking.get("status_summary") and not tracking.get("privacy_flags"):
                    performance_analyst.run_bid_optimization(tracking["status_summary"], **args)
                else:
                    reason = (
                        "데이터 점검에 개인정보 신호가 있어 광고 제안을 보류했습니다."
                        if tracking.get("privacy_flags") else
                        "같은 브리핑의 트래킹 점검 결과가 없어 광고 제안을 보류했습니다."
                    )
                    pending.append({
                        "agent_id": AGENTS["performance"], "routed_to": "dev_team.queue",
                        "output": {"halted": True, "reason": reason},
                        "model_version": "policy-engine/no-llm", "input_summary": "halt:tracking_unusable",
                    })
        except Exception as exc:
            error = type(exc).__name__
            LOGGER.warning("Brief stage failed brief_id=%s stage=%s error_type=%s", brief_id, stage, error)

        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """UPDATE team_brief_jobs SET status=?, error=?
                   WHERE brief_id=? AND stage=? AND status='queued'""",
                ("failed" if error else "completed", error, brief_id, stage),
            ).rowcount
            if updated:
                if error is None:
                    for fields in pending:
                        workflow._insert_work_item(conn, correlation_id=brief_id, **fields)
                self._mark_dirty(conn, brief_id)

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
                content_editor._draft_content(
                    row["output"], correlation_id=brief_id, db_path=self.db_path,
                    work_item_id=work_id, content_approval_mode=self.approval_mode,
                    collect_legal_claims=True,
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
