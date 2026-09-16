#!/usr/bin/env python3
"""Validate the Phase 3 Slack test foundation and optional live workspace."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import DB_PATH  # noqa: E402
from bot.db import get_conn  # noqa: E402
from bot.slack_adapter.blocks import render_test_card, test_card_text  # noqa: E402
from bot.slack_adapter.config import SlackSettings, load_slack_mapping  # noqa: E402
from bot.slack_adapter.fixtures import load_test_fixture  # noqa: E402
from bot.slack_adapter.service import SlackTestService  # noqa: E402


REQUIRED_FILES = (
    "bot/slack_app.py",
    "bot/slack_adapter/app.py",
    "bot/slack_adapter/config.py",
    "bot/slack_adapter/service.py",
    "bot/slack_adapter/blocks.py",
    "slack/manifest.yaml",
    "slack/mapping.example.yaml",
    "docs/phase3/slack_test_foundation.md",
    "tests/test_phase3_slack.py",
)
REQUIRED_TABLES = {"slack_test_sessions", "slack_interaction_events"}
PROTECTED_TABLES = (
    "work_items",
    "approval_requests",
    "approval_decisions",
    "acknowledgements",
    "handoff_jobs",
    "execution_requests",
)
FORBIDDEN_ADAPTER_REFERENCES = (
    "from ..workflow",
    "from bot.workflow",
    "from ..execution",
    "from bot.execution",
    "from ..llm_client",
    "from bot.llm_client",
    "call_structured(",
    "claim_handoffs(",
    "decide(",
    "acknowledge(",
    "execute_request(",
)


def _git_status() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def automated_errors() -> list[str]:
    errors: list[str] = []
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            errors.append(f"required file missing: {relative}")

    requirements = (ROOT / "bot/requirements.txt").read_text(encoding="utf-8").lower()
    if "slack-bolt>=1.28.0,<2.0" not in requirements:
        errors.append("bot/requirements.txt must pin slack-bolt to the supported major version")

    env_example = (ROOT / "bot/.env.example").read_text(encoding="utf-8")
    for name in (
        "SLACK_BOT_TOKEN=",
        "SLACK_APP_TOKEN=",
        "SLACK_MAPPING_PATH=~/.hermes/slack_mapping.yaml",
        "SLACK_TEST_SESSION_TTL_MINUTES=15",
    ):
        if name not in env_example:
            errors.append(f"bot/.env.example missing Phase 3 setting: {name}")

    try:
        manifest = yaml.safe_load((ROOT / "slack/manifest.yaml").read_text(encoding="utf-8"))
        settings = manifest.get("settings", {})
        if settings.get("socket_mode_enabled") is not True:
            errors.append("Slack manifest must enable Socket Mode")
        if settings.get("interactivity", {}).get("is_enabled") is not True:
            errors.append("Slack manifest must enable interactivity")
        commands = manifest.get("features", {}).get("slash_commands", [])
        if [item.get("command") for item in commands] != ["/hermes-test"]:
            errors.append("Slack manifest must expose only /hermes-test")
        scopes = set(manifest.get("oauth_config", {}).get("scopes", {}).get("bot", []))
        if scopes != {"commands", "chat:write"}:
            errors.append("Slack bot scopes must be exactly commands and chat:write")
        if settings.get("event_subscriptions"):
            errors.append("Phase 3 manifest must not subscribe to Events API events")
    except (OSError, yaml.YAMLError, AttributeError) as exc:
        errors.append(f"Slack manifest is invalid: {exc}")

    for path in (ROOT / "bot/slack_adapter").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_ADAPTER_REFERENCES:
            if forbidden in text:
                errors.append(
                    f"Slack adapter crosses the Phase 3 boundary in {path.relative_to(ROOT)}: {forbidden}"
                )

    try:
        mapping = load_slack_mapping(ROOT / "slack/mapping.example.yaml")
        if len(set(mapping.queue_channels.values())) != 1:
            errors.append("Phase 3 example queues must map to one development channel")
    except Exception as exc:
        errors.append(f"Slack example mapping is invalid: {type(exc).__name__}: {exc}")

    try:
        fixtures = {
            kind: load_test_fixture(kind)
            for kind in ("keyword", "content", "performance", "data")
        }
        if any(not fixture.required_roles for fixture in fixtures.values()):
            errors.append("every Slack UI fixture must have a test role")
    except Exception as exc:
        errors.append(f"Slack fixture contract failed: {type(exc).__name__}: {exc}")
        fixtures = {}

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "phase3-gate.db"
        conn = get_conn(db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in PROTECTED_TABLES
        }
        conn.close()
        missing = REQUIRED_TABLES - tables
        if missing:
            errors.append(f"Slack schema missing tables: {', '.join(sorted(missing))}")
        for trigger in (
            "slack_interaction_events_no_update",
            "slack_interaction_events_no_delete",
        ):
            if trigger not in triggers:
                errors.append(f"Slack append-only trigger missing: {trigger}")

        if "keyword" in fixtures:
            service = SlackTestService(db_path)
            session = service.create_session(
                command_id="gate-command",
                fixture=fixtures["keyword"],
                team_id="TGATE",
                channel_id="CGATE",
                created_by="slack:TGATE:UGATE",
            )
            session = service.bind_message(session["session_id"], message_ts="1.1")
            result = service.apply_action(
                interaction_id="gate-action",
                session_id=session["session_id"],
                action_type="approved",
                team_id="TGATE",
                channel_id="CGATE",
                message_ts="1.1",
                user_id="UGATE",
                actor_roles={"keyword_approver"},
                acting_role="keyword_approver",
            )
            if result["state"] != "approved":
                errors.append("Phase 3 test session smoke approval failed")
        conn = get_conn(db_path)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in PROTECTED_TABLES
        }
        conn.close()
        if before != after:
            errors.append("Phase 3 smoke action modified protected workflow tables")
    return errors


def live_gate_errors() -> list[str]:
    errors: list[str] = []
    before = _git_status()
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        return ["Slack live gate dependency is missing; install bot/requirements.txt"]

    try:
        settings = SlackSettings.from_env()
        settings.validate_runtime()
        mapping = load_slack_mapping(settings.mapping_path)
        client = WebClient(token=settings.bot_token)
        auth = client.auth_test()
        if not auth.get("ok") or auth.get("team_id") != mapping.workspace_id:
            return ["Slack live gate workspace does not match the mapping"]

        fixture = load_test_fixture("keyword")
        service = SlackTestService(DB_PATH, session_ttl_minutes=settings.session_ttl_minutes)
        correlation_id = str(uuid.uuid4())
        session = service.create_session(
            command_id=f"live-gate:{correlation_id}",
            fixture=fixture,
            team_id=mapping.workspace_id,
            channel_id=mapping.development_channel_id,
            created_by="system:phase3-live-gate",
        )
        response = client.chat_postMessage(
            channel=mapping.development_channel_id,
            text=test_card_text(session, fixture, live_gate=True),
            blocks=render_test_card(session, fixture, live_gate=True),
        )
        service.bind_message(session["session_id"], message_ts=response["ts"])
    except SlackApiError as exc:
        code = exc.response.get("error", "unknown_slack_error")
        errors.append(f"Slack live gate API failure: {code}")
    except Exception as exc:
        errors.append(f"Slack live gate failed: {type(exc).__name__}: {exc}")
    after = _git_status()
    if before != after:
        errors.append("Slack live gate changed repository files")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--gate", action="store_true", help="run the offline engineering gate")
    mode.add_argument(
        "--live-gate",
        action="store_true",
        help="verify Slack auth/mapping and post one persistent test card",
    )
    args = parser.parse_args()

    errors = automated_errors()
    if errors:
        print(f"PHASE 3 AUTOMATED CHECK: FAIL ({len(errors)} errors)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("PHASE 3 AUTOMATED CHECK: PASS")
    if args.gate:
        print("PHASE 3 ENGINEERING GATE: PASS")
    if args.live_gate:
        errors = live_gate_errors()
        if errors:
            print(f"PHASE 3 LIVE GATE: FAIL ({len(errors)} errors)")
            for error in errors:
                print(f"- {error}")
            return 1
        print("PHASE 3 LIVE GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
