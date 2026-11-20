"""Slack Bolt handlers and Socket Mode entrypoint."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from ..config import DB_PATH
from ..slack_approval_demo import COMMANDS as DEMO_COMMANDS
from ..slack_approval_demo import ApprovalDemo, SlackApprovalCards
from .blocks import render_action_modal, render_test_card, test_card_text
from .config import (
    SlackConfigurationError,
    SlackIdentityProvider,
    SlackMapping,
    SlackSettings,
    load_slack_mapping,
)
from .drafts import COMMAND as DRAFT_COMMAND
from .drafts import EDIT_ACTION, EDIT_MODAL
from .drafts import SlackDrafts, approval_mode_from_env
from .fixtures import SlackTestFixture, load_test_fixture
from .publish import register_publish_handlers
from .service import SlackTestError, SlackTestService, SlackTestValidationError


LOGGER = logging.getLogger("hermes.slack")
COMMAND = "/hermes-test"
COMMANDS = frozenset({"keyword", "content", "performance", "data"})

ACTION_TYPES = {
    "phase3_approve": "approved",
    "phase3_reject": "rejected",
    "phase3_revision": "revision_requested",
    "phase3_acknowledge": "acknowledged",
}

VIEW_TYPES = {
    "phase3_approved_modal": "approved",
    "phase3_rejected_modal": "rejected",
    "phase3_revision_requested_modal": "revision_requested",
    "phase3_acknowledged_modal": "acknowledged",
}


@dataclass(frozen=True)
class SlackDependencies:
    mapping: SlackMapping
    identity: SlackIdentityProvider
    service: SlackTestService
    fixtures: dict[str, SlackTestFixture]


def build_dependencies(settings: SlackSettings, *, db_path=DB_PATH) -> SlackDependencies:
    mapping = load_slack_mapping(settings.mapping_path)
    return SlackDependencies(
        mapping=mapping,
        identity=SlackIdentityProvider(mapping),
        service=SlackTestService(
            db_path, session_ttl_minutes=settings.session_ttl_minutes
        ),
        fixtures={name: load_test_fixture(name) for name in sorted(COMMANDS)},
    )


def _team_id(body: dict) -> str:
    team = body.get("team")
    if isinstance(team, dict):
        return str(team.get("id", ""))
    return str(body.get("team_id", ""))


def _user_id(body: dict) -> str:
    user = body.get("user")
    if isinstance(user, dict):
        return str(user.get("id", ""))
    return str(body.get("user_id", ""))


def command_interaction_id(command: dict) -> str:
    return f"cmd:{command.get('team_id', '')}:{command.get('trigger_id', '')}"


def action_interaction_id(body: dict, action: dict, session_id: str) -> str:
    return ":".join(
        (
            "act",
            _team_id(body),
            _user_id(body),
            str(action.get("action_ts", "")),
            str(action.get("action_id", "")),
            session_id,
        )
    )


def view_interaction_id(body: dict, callback_id: str, session_id: str) -> str:
    view = body.get("view", {})
    return ":".join(
        (
            "view",
            _team_id(body),
            _user_id(body),
            str(view.get("id", "")),
            callback_id,
            session_id,
        )
    )


def _ephemeral(respond: Callable[..., Any], text: str) -> None:
    respond(response_type="ephemeral", text=text)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SlackConfigurationError):
        return "Slack 워크스페이스·사용자·채널 매핑을 확인해 주세요."
    if isinstance(exc, SlackTestError):
        return str(exc)
    return "Slack 테스트 요청을 처리하지 못했습니다. 세션 ID와 로컬 로그를 확인해 주세요."


def _update_message(client: Any, deps: SlackDependencies, session: dict) -> None:
    fixture = deps.fixtures[session["fixture_type"]]
    client.chat_update(
        channel=session["channel_id"],
        ts=session["message_ts"],
        text=test_card_text(session, fixture),
        blocks=render_test_card(session, fixture),
    )


def handle_command(
    *,
    ack: Callable[..., Any],
    command: dict,
    respond: Callable[..., Any],
    client: Any,
    deps: SlackDependencies,
) -> None:
    ack()
    subcommand = str(command.get("text", "")).strip().lower()
    try:
        team_id = str(command.get("team_id", ""))
        channel_id = str(command.get("channel_id", ""))
        user_id = str(command.get("user_id", ""))
        actor = deps.identity.actor_for(team_id, user_id)
        deps.identity.require_development_channel(channel_id)

        if subcommand in {"", "help"}:
            _ephemeral(
                respond,
                "사용법: `/hermes-test status|keyword|content|performance|data`\n"
                "모든 결과는 Phase 3 테스트 전용이며 실제 승인이 아닙니다.",
            )
            return
        if subcommand == "status":
            _ephemeral(
                respond,
                "PHASE 3 TEST 연결 정상\n"
                f"Workspace `{team_id}` · Channel `{channel_id}`\n"
                f"Actor `{actor.actor_id}`\nRoles: "
                + ", ".join(f"`{role}`" for role in sorted(actor.roles)),
            )
            return
        if subcommand not in COMMANDS:
            _ephemeral(respond, "지원하지 않는 명령입니다. `/hermes-test`로 사용법을 확인하세요.")
            return

        command_id = command_interaction_id(command)
        if command_id.endswith(":"):
            raise SlackTestValidationError("Slack trigger id is required")
        fixture = deps.fixtures[subcommand]
        session = deps.service.create_session(
            command_id=command_id,
            fixture=fixture,
            team_id=team_id,
            channel_id=channel_id,
            created_by=actor.actor_id,
        )
        if session.get("duplicate"):
            state = "이미 생성된" if session.get("message_ts") else "현재 처리 중인"
            _ephemeral(
                respond,
                f"{state} 테스트 세션입니다: `{session['session_id']}`",
            )
            return
        response = client.chat_postMessage(
            channel=channel_id,
            text=test_card_text(session, fixture),
            blocks=render_test_card(session, fixture),
        )
        bound = deps.service.bind_message(session["session_id"], message_ts=response["ts"])
        LOGGER.info("phase3 session created session_id=%s", bound["session_id"])
    except Exception as exc:
        LOGGER.warning("phase3 command failed error_type=%s", type(exc).__name__)
        _ephemeral(respond, _safe_error(exc))


def handle_action(
    *,
    ack: Callable[..., Any],
    body: dict,
    action: dict,
    respond: Callable[..., Any],
    client: Any,
    deps: SlackDependencies,
) -> None:
    ack()
    try:
        action_type = ACTION_TYPES[str(action.get("action_id", ""))]
        session_id = str(action.get("value", ""))
        team_id = _team_id(body)
        user_id = _user_id(body)
        channel_id = str(body.get("channel", {}).get("id", ""))
        message_ts = str(body.get("message", {}).get("ts", ""))
        actor = deps.identity.actor_for(team_id, user_id)
        deps.identity.require_development_channel(channel_id)
        session = deps.service.validate_action_target(
            session_id=session_id,
            team_id=team_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )
        eligible = sorted(set(session["remaining_roles"]) & set(actor.roles))
        if not eligible:
            raise SlackConfigurationError("Slack user has no remaining role for this session")

        needs_modal = action_type in {"rejected", "revision_requested"} or len(eligible) > 1
        if needs_modal:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=render_action_modal(
                    session_id=session_id,
                    action_type=action_type,
                    eligible_roles=eligible,
                ),
            )
            return
        result = deps.service.apply_action(
            interaction_id=action_interaction_id(body, action, session_id),
            session_id=session_id,
            action_type=action_type,
            team_id=team_id,
            channel_id=channel_id,
            message_ts=message_ts,
            user_id=user_id,
            actor_roles=actor.roles,
            acting_role=eligible[0],
        )
        _update_message(client, deps, result)
        LOGGER.info(
            "phase3 action applied session_id=%s state=%s",
            result["session_id"],
            result["state"],
        )
    except Exception as exc:
        LOGGER.warning("phase3 action failed error_type=%s", type(exc).__name__)
        _ephemeral(respond, _safe_error(exc))


def _modal_values(view: dict) -> tuple[str, str]:
    values = view.get("state", {}).get("values", {})
    role = (
        values.get("acting_role_block", {})
        .get("acting_role", {})
        .get("selected_option", {})
        .get("value", "")
    )
    reason = values.get("reason_block", {}).get("reason", {}).get("value", "")
    return str(role), str(reason or "").strip()


def handle_view_submission(
    *,
    ack: Callable[..., Any],
    body: dict,
    client: Any,
    deps: SlackDependencies,
) -> None:
    view = body.get("view", {})
    callback_id = str(view.get("callback_id", ""))
    action_type = VIEW_TYPES.get(callback_id)
    acting_role, reason = _modal_values(view)
    errors: dict[str, str] = {}
    if not acting_role:
        errors["acting_role_block"] = "담당 역할을 선택해 주세요."
    if action_type in {"rejected", "revision_requested"} and not reason:
        errors["reason_block"] = "사유를 입력해 주세요."
    if action_type is None:
        errors["acting_role_block"] = "지원하지 않는 요청입니다."
    if errors:
        ack(response_action="errors", errors=errors)
        return
    ack()

    session_id = ""
    try:
        metadata = json.loads(view.get("private_metadata", "{}"))
        session_id = str(metadata.get("session_id", ""))
        if not session_id:
            raise SlackTestValidationError("Slack test session id is required")
        team_id = _team_id(body)
        user_id = _user_id(body)
        actor = deps.identity.actor_for(team_id, user_id)
        session = deps.service.get_session(session_id)
        deps.identity.require_development_channel(session["channel_id"])
        result = deps.service.apply_action(
            interaction_id=view_interaction_id(body, callback_id, session_id),
            session_id=session_id,
            action_type=action_type,
            team_id=team_id,
            channel_id=session["channel_id"],
            message_ts=session["message_ts"],
            user_id=user_id,
            actor_roles=actor.roles,
            acting_role=acting_role,
            reason=reason,
        )
        _update_message(client, deps, result)
        LOGGER.info(
            "phase3 modal applied session_id=%s state=%s",
            result["session_id"],
            result["state"],
        )
    except Exception as exc:
        LOGGER.warning(
            "phase3 modal failed session_id=%s error_type=%s",
            session_id or "unknown",
            type(exc).__name__,
        )
        if session_id:
            try:
                session = deps.service.get_session(session_id)
                client.chat_postEphemeral(
                    channel=session["channel_id"],
                    user=_user_id(body),
                    text=_safe_error(exc),
                )
            except Exception:
                LOGGER.warning(
                    "phase3 modal error notification failed session_id=%s", session_id
                )


def _register_approval_cards(app: App, cards: SlackApprovalCards) -> None:
    def on_action(ack, body, action, respond, client):
        cards.handle_action(ack=ack, body=body, action=action, respond=respond, client=client)

    for action_id in cards.actions:
        app.action(action_id)(on_action)

    @app.view(cards.reason_modal)
    def on_view(ack, body, client):
        cards.handle_view(ack=ack, body=body, client=client)


def register_handlers(
    app: App, deps: SlackDependencies, approval_demo: ApprovalDemo | None = None,
    *, drafts: SlackDrafts | None = None,
) -> None:
    @app.command(COMMAND)
    def _command(ack, command, respond, client):
        words = str(command.get("text", "")).lower().split(maxsplit=1)
        if approval_demo is not None and words and words[0] in DEMO_COMMANDS:
            approval_demo.handle_command(ack=ack, command=command, respond=respond, client=client)
            return
        handle_command(ack=ack, command=command, respond=respond, client=client, deps=deps)

    if approval_demo is not None:
        _register_approval_cards(app, approval_demo)

    if drafts is not None:
        _register_approval_cards(app, drafts)
        register_publish_handlers(app, deps.mapping, drafts.db_path, drafts.channel_id)

        @app.action(EDIT_ACTION)
        def _draft_edit(ack, body, action, respond, client):
            drafts.open_edit(ack=ack, body=body, action=action, respond=respond, client=client)

        @app.view(EDIT_MODAL)
        def _draft_edit_save(ack, body):
            drafts.submit_edit(ack=ack, body=body)

        @app.command(DRAFT_COMMAND)
        def _draft_command(ack, command, respond):
            drafts.handle_command(ack=ack, command=command, respond=respond)

    def _action(ack, body, action, respond, client):
        handle_action(
            ack=ack,
            body=body,
            action=action,
            respond=respond,
            client=client,
            deps=deps,
        )

    for action_id in ACTION_TYPES:
        app.action(action_id)(_action)

    def _view(ack, body, client):
        handle_view_submission(ack=ack, body=body, client=client, deps=deps)

    for callback_id in VIEW_TYPES:
        app.view(callback_id)(_view)


def verify_slack_workspace(client: Any, mapping: SlackMapping) -> dict:
    response = client.auth_test()
    if not response.get("ok") or response.get("team_id") != mapping.workspace_id:
        raise SlackConfigurationError("Slack token workspace does not match the mapping")
    # slack_sdk.web.SlackResponse exposes mapping-style ``get`` but is not a
    # Mapping accepted by dict(response). Return only the verified identifiers
    # needed by the caller instead of trying to copy the SDK response object.
    return {
        "ok": True,
        "team_id": response.get("team_id"),
        "user_id": response.get("user_id"),
    }


def create_app(
    settings: SlackSettings, deps: SlackDependencies, approval_demo: ApprovalDemo | None = None,
    *, drafts: SlackDrafts | None = None,
) -> App:
    settings.validate_runtime()
    app = App(token=settings.bot_token)
    verify_slack_workspace(app.client, deps.mapping)
    register_handlers(app, deps, approval_demo, drafts=drafts)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Hermes Slack Socket Mode adapter.")
    parser.add_argument(
        "--approval-demo-db", type=Path,
        help="enable the development approval demo with its own SQLite file",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = SlackSettings.from_env()
    deps = build_dependencies(settings)
    approval_demo = None
    if args.approval_demo_db is not None:
        demo_path = args.approval_demo_db.expanduser().resolve()
        phase3_path = DB_PATH.expanduser().resolve()
        if demo_path == phase3_path or (
            demo_path.exists() and phase3_path.exists() and demo_path.samefile(phase3_path)
        ):
            parser.error("--approval-demo-db must use a separate file from HERMES_DB_PATH")
        approval_demo = ApprovalDemo(deps.mapping, demo_path)
    draft_channel = os.environ.get("SLACK_DRAFT_CHANNEL_ID", "").strip()
    drafts = None
    if draft_channel:
        try:
            approval_mode = approval_mode_from_env(os.environ)
        except SlackConfigurationError as exc:
            parser.error(str(exc))
        drafts = SlackDrafts(deps.mapping, DB_PATH, draft_channel, approval_mode=approval_mode)
    app = create_app(settings, deps, approval_demo, drafts=drafts)
    handler = SocketModeHandler(app, settings.app_token)
    stop = Event()
    if drafts is not None:
        Thread(target=drafts.run, args=(app.client, stop), name="slack-drafts", daemon=True).start()
    LOGGER.info(
        "Slack Socket Mode starting workspace=%s draft_requests=%s content_approval_mode=%s",
        deps.mapping.workspace_id, drafts is not None, drafts.approval_mode.value if drafts else "off",
    )
    try:
        handler.start()
    except KeyboardInterrupt:
        LOGGER.info("Slack Socket Mode stopped")
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
