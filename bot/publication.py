"""Read the approved Slack draft queue and record manual publication results."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from .db import get_conn, utc_iso
from .workflow import ActorContext, _audit

PUBLISHER_ROLE = "content_publisher"
URL_LIMIT = 2000

_QUEUE = """
    SELECT h.handoff_id, h.status, h.correlation_id, w.work_item_id, w.version,
           w.agent_id, w.model_version, w.output_json, a.resolved_at AS approved_at,
           m.publication_url, m.recorded_by, m.recorded_at
    FROM handoff_jobs h
    JOIN work_items w ON w.work_item_id=h.work_item_id AND w.version=h.work_item_version
    JOIN approval_requests a ON a.work_item_id=w.work_item_id AND a.work_item_version=w.version
    JOIN slack_draft_jobs d ON d.job_id=w.work_item_id
    LEFT JOIN manual_publications m ON m.handoff_id=h.handoff_id
    WHERE h.handoff_type='queue_for_publish' AND h.target='publish_queue'
      AND w.agent_id='content_editor' AND w.status='approved' AND a.status='approved'
      AND d.team_id=? AND d.channel_id=? AND d.failure IS NULL
      AND (
          (h.status='pending' AND NOT EXISTS (
              SELECT 1 FROM work_items newer
              WHERE newer.work_item_id=w.work_item_id AND newer.version>w.version
          ))
          OR (h.status='completed' AND m.handoff_id IS NOT NULL)
      )
"""


class PublicationError(Exception):
    """A publication operation the caller can correct."""


def validate_publication_url(value: str) -> str:
    url = value.strip()
    message = "http:// 또는 https://로 시작하는 발행 페이지 주소를 입력해 주세요."
    if len(url) > URL_LIMIT or any(
        char.isspace() or not char.isprintable() or char in "\\<>|\""
        for char in url
    ):
        raise PublicationError(message)
    try:
        parsed = urlsplit(url)
        parsed.port  # Reject malformed or out-of-range ports.
    except ValueError as exc:
        raise PublicationError(message) from exc
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
        raise PublicationError(message)
    return url


def _entry(row) -> dict:
    result = dict(row)
    result["output"] = json.loads(result.pop("output_json"))
    return result


class PublicationQueue:
    def __init__(self, db_path: Path, team_id: str, channel_id: str):
        self.db_path = db_path
        self.scope = (team_id, channel_id)

    def list(self, status: str, *, limit: int, offset: int) -> list[dict]:
        with closing(get_conn(self.db_path)) as conn:
            rows = conn.execute(
                _QUEUE + """ AND h.status=?
                    ORDER BY COALESCE(m.recorded_at, a.resolved_at) DESC, h.handoff_id DESC
                    LIMIT ? OFFSET ?""",
                (*self.scope, status, limit, offset),
            )
            return [_entry(row) for row in rows]

    def _get(self, conn, handoff_id: str) -> dict:
        row = conn.execute(_QUEUE + " AND h.handoff_id=?", (*self.scope, handoff_id)).fetchone()
        if row is None:
            raise PublicationError("이 승인본은 현재 발행 대기열에서 처리할 수 없습니다. 목록을 다시 열어 주세요.")
        return _entry(row)

    def get(self, handoff_id: str) -> dict:
        with closing(get_conn(self.db_path)) as conn:
            return self._get(conn, handoff_id)

    def record(self, handoff_id: str, value: str, actor: ActorContext) -> dict:
        if not (actor.verified and actor.platform == "slack" and PUBLISHER_ROLE in actor.roles
                and actor.actor_id.startswith(f"slack:{self.scope[0]}:")):
            raise PublicationError("발행 담당자 권한이 필요합니다.")
        url = validate_publication_url(value)
        with closing(get_conn(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            entry = self._get(conn, handoff_id)
            if entry["status"] == "completed":
                if entry["publication_url"] == url:
                    return entry
                raise PublicationError("이 승인본의 발행 결과가 이미 저장되었습니다. 발행 기록에서 확인해 주세요.")

            recorded_at = utc_iso()
            conn.execute(
                """INSERT INTO manual_publications (handoff_id, publication_url, recorded_by, recorded_at)
                   VALUES (?, ?, ?, ?)""",
                (handoff_id, url, actor.actor_id, recorded_at),
            )
            conn.execute(
                "UPDATE handoff_jobs SET status='completed', completed_at=? WHERE handoff_id=?",
                (recorded_at, handoff_id),
            )
            _audit(
                conn, event_type="publication.manual_recorded", work=entry, actor_type="human",
                actor_id=actor.actor_id, platform=actor.platform, previous_state="pending",
                new_state="completed", reason=None, correlation_id=entry["correlation_id"],
            )
            return self._get(conn, handoff_id)
