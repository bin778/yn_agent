"""
agent_memory, audit_log를 SQLite로 관리.
queues.md 10, 11번 항목 참고: "Discord 채널이 아니라 DB가 진실의 원천"
"""
import sqlite3
import json
import datetime
from .config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            output_json TEXT NOT NULL,
            routed_to TEXT NOT NULL,
            human_decision TEXT DEFAULT NULL,   -- approved / rejected / NULL(대기중)
            decided_by TEXT DEFAULT NULL,
            decided_at TEXT DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            model_version TEXT,
            input_summary TEXT,
            output_summary TEXT,
            routed_to TEXT
        )
    """)
    return conn


def record_run(agent_id: str, output: dict, routed_to: str, model_version: str, input_summary: str) -> int:
    """작업 결과를 agent_memory + audit_log에 동시 기록하고 agent_memory의 record id를 반환."""
    conn = get_conn()
    now = datetime.datetime.utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO agent_memory (agent_id, created_at, output_json, routed_to) VALUES (?, ?, ?, ?)",
        (agent_id, now, json.dumps(output, ensure_ascii=False), routed_to),
    )
    record_id = cur.lastrowid
    conn.execute(
        """INSERT INTO audit_log (agent_id, created_at, model_version, input_summary, output_summary, routed_to)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_id, now, model_version, input_summary, json.dumps(output, ensure_ascii=False)[:500], routed_to),
    )
    conn.commit()
    conn.close()
    return record_id


def record_decision(record_id: int, decision: str, decided_by: str):
    """Discord에서 승인/반려 버튼을 눌렀을 때 agent_memory에 반영."""
    conn = get_conn()
    now = datetime.datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE agent_memory SET human_decision=?, decided_by=?, decided_at=? WHERE id=?",
        (decision, decided_by, now, record_id),
    )
    conn.commit()
    conn.close()


def recent_history(agent_id: str, limit: int = 5) -> list[tuple]:
    """서연우 SOUL.md 원칙: '최근 반려 이력을 먼저 확인하고 패턴을 인지한다' 를 위한 조회."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT created_at, output_json, routed_to, human_decision
           FROM agent_memory WHERE agent_id=? ORDER BY id DESC LIMIT ?""",
        (agent_id, limit),
    ).fetchall()
    conn.close()
    return rows


def get_record(record_id: int) -> dict | None:
    """agent_memory 단건 조회. 없으면 None."""
    conn = get_conn()
    row = conn.execute(
        """SELECT id, agent_id, created_at, output_json, routed_to, human_decision
           FROM agent_memory WHERE id=?""",
        (record_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "agent_id": row[1],
        "created_at": row[2],
        "output": json.loads(row[3]),
        "routed_to": row[4],
        "human_decision": row[5],
    }


def recent_rejections(agent_id: str, limit: int = 5) -> list[tuple]:
    """한도윤 pre_task: 최근 반려된 콘텐츠 및 사유."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT created_at, output_json, routed_to, human_decision
           FROM agent_memory
           WHERE agent_id=? AND human_decision='rejected'
           ORDER BY id DESC LIMIT ?""",
        (agent_id, limit),
    ).fetchall()
    conn.close()
    return rows


def list_approved_keywords_awaiting_draft(limit: int = 10) -> list[dict]:
    """
    content_editor.inbox로 라우팅되어 승인됐지만,
    아직 한도윤이 같은 키워드로 초안을 만들지 않은 항목.
    """
    conn = get_conn()
    drafted_rows = conn.execute(
        """SELECT output_json FROM agent_memory WHERE agent_id='content_editor'"""
    ).fetchall()
    drafted_keywords = set()
    for (output_json,) in drafted_rows:
        try:
            drafted_keywords.add(json.loads(output_json).get("target_keyword", ""))
        except (json.JSONDecodeError, AttributeError):
            pass

    rows = conn.execute(
        """SELECT id, output_json, routed_to
           FROM agent_memory
           WHERE agent_id='keyword_analyst'
             AND routed_to='content_editor.inbox'
             AND human_decision='approved'
           ORDER BY id ASC""",
    ).fetchall()
    conn.close()

    pending = []
    for record_id, output_json, routed_to in rows:
        output = json.loads(output_json)
        keyword = output.get("keyword", "")
        if keyword and keyword not in drafted_keywords:
            pending.append(
                {
                    "id": record_id,
                    "output": output,
                    "routed_to": routed_to,
                }
            )
            if len(pending) >= limit:
                break
    return pending
