"""Preview or delete recorded Hermes Slack messages for selected team briefings."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler


SEOUL = ZoneInfo("Asia/Seoul")
SLACK_TS = re.compile(r"[0-9]+\.[0-9]{6}")


class CleanupError(RuntimeError):
    pass


def build_plan(args: argparse.Namespace) -> list[dict]:
    with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        where, values = "team_id=? AND channel_id=?", [args.workspace, args.channel]
        if args.root_ts:
            placeholders = ",".join("?" for _ in args.root_ts)
            where += f" AND message_ts IN ({placeholders})"
            values.extend(args.root_ts)
        else:
            start = dt.datetime.combine(args.from_date, dt.time(), tzinfo=SEOUL)
            end = dt.datetime.combine(args.through_date + dt.timedelta(days=1), dt.time(), tzinfo=SEOUL)
            where += " AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)"
            values.extend((start.isoformat(), end.isoformat()))
        briefs = conn.execute(f"""
            SELECT brief_id, message_ts, created_at, tracking_scenario,
                    sync_version != delivered_version
                    OR EXISTS (SELECT 1 FROM team_brief_jobs j
                            WHERE j.brief_id=b.brief_id AND j.status='queued')
                    OR EXISTS (SELECT 1 FROM handoff_jobs h
                            WHERE h.correlation_id=b.brief_id AND h.handoff_type='draft_content'
                            AND h.status IN ('pending', 'claimed')) AS busy
            FROM team_briefs b WHERE {where} ORDER BY created_at
        """, values).fetchall()
        if args.root_ts and set(args.root_ts) != {brief["message_ts"] for brief in briefs}:
            raise CleanupError("One or more parent timestamps do not match this Workspace and channel.")
        plan = []
        for brief in briefs:
            brief_id, root_ts = brief["brief_id"], brief["message_ts"]
            if brief["busy"] or not root_ts:
                raise CleanupError(f"Briefing {brief_id} is still running, unsynced, or has no Slack parent.")
            replies = {
                row[0] for row in conn.execute("""
                    SELECT message_ts FROM team_brief_replies WHERE brief_id=?
                    UNION
                    SELECT c.message_ts FROM slack_approval_demo_cards c
                    JOIN approval_requests a USING (request_id)
                    JOIN work_items w ON w.work_item_id=a.work_item_id AND w.version=a.work_item_version
                    WHERE a.correlation_id=? AND w.agent_id='keyword_analyst'
                    AND c.team_id=? AND c.channel_id=?
                """, (brief_id, brief_id, args.workspace, args.channel))
            }
            if not all(SLACK_TS.fullmatch(ts) for ts in [root_ts, *replies]) or root_ts in replies:
                raise CleanupError(f"Invalid parent or reply timestamp for briefing {brief_id}.")
            plan.append({
                "brief_id": brief_id, "root_ts": root_ts, "replies": sorted(replies),
                "created_at": brief["created_at"], "scenario": brief["tracking_scenario"],
            })
    return plan


def plan_code(args: argparse.Namespace, plan: list[dict]) -> str:
    payload = json.dumps([args.workspace, args.channel, plan], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def print_plan(args: argparse.Namespace, plan: list[dict], code: str) -> None:
    print(f"Workspace={args.workspace} Channel={args.channel}")
    for item in plan:
        day = dt.datetime.fromisoformat(item["created_at"]).astimezone(SEOUL).isoformat(timespec="seconds")
        print(f"{day} {item['scenario']} parent={item['root_ts']} replies={len(item['replies'])}")
    print(f"Parents={len(plan)} bot replies={sum(len(item['replies']) for item in plan)}")
    if plan:
        print(f"Apply code: {code}")
        print("This list covers recorded Hermes messages. Check the threads for any human or unrecorded replies.")


def delete_plan(client: WebClient, args: argparse.Namespace, plan: list[dict]) -> None:
    identity = client.auth_test()
    if identity.get("team_id") != args.workspace or not identity.get("bot_id"):
        raise CleanupError("The bot token must belong to the selected Workspace.")
    next_call = 0.0
    for item in plan:
        for ts in [*item["replies"], item["root_ts"]]:
            time.sleep(max(0, next_call - time.monotonic()))
            try:
                client.chat_delete(channel=args.channel, ts=ts)
                result = "deleted"
            except SlackApiError as exc:
                error = exc.response.get("error", "unknown_error")
                if error != "message_not_found":
                    raise CleanupError(f"Stopped at {ts}: Slack returned {error}.") from None
                result = "already missing"
            next_call = time.monotonic() + 1.5
            print(f"{ts} {result}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/var/lib/hermes/hermes.db"))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--root-ts", action="append", help="Exact parent timestamp; repeat to select multiple.")
    parser.add_argument("--from-date", type=dt.date.fromisoformat, help="First date in Asia/Seoul.")
    parser.add_argument("--through-date", type=dt.date.fromisoformat, help="Last date in Asia/Seoul, inclusive.")
    parser.add_argument("--apply", metavar="CODE", help="Delete after verifying the preview code.")
    args = parser.parse_args()
    if args.root_ts:
        if args.from_date or args.through_date or not all(SLACK_TS.fullmatch(ts) for ts in args.root_ts):
            parser.error("Use exact parent timestamps or one date range.")
    elif not args.from_date or not args.through_date or args.from_date > args.through_date:
        parser.error("Provide --root-ts or both ordered dates.")

    try:
        if not args.apply:
            plan = build_plan(args)
            print_plan(args, plan, plan_code(args, plan))
            return 0

        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token.startswith("xoxb-"):
            raise CleanupError("Set SLACK_BOT_TOKEN using the existing Hermes bot configuration.")
        args.db = args.db.resolve()
        lock_path = args.db.with_name(f"{args.db.name}.slack-team.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CleanupError("The team briefing worker is busy. Retry after its current job.") from None
            plan = build_plan(args)
            if not plan or args.apply != plan_code(args, plan):
                raise CleanupError("The apply code no longer matches. Generate a fresh preview.")
            client = WebClient(
                token=token, timeout=15, retry_handlers=[RateLimitErrorRetryHandler(max_retry_count=4)],
            )
            delete_plan(client, args, plan)
    except (CleanupError, OSError, sqlite3.Error) as exc:
        print(exc, file=sys.stderr)
        return 1
    except SlackClientError as exc:
        print(f"Slack request failed: {type(exc).__name__}.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
