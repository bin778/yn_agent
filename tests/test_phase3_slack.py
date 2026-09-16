from __future__ import annotations

import datetime as dt
import inspect
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from bot.db import get_conn
from bot.slack_adapter.app import (
    SlackDependencies,
    _safe_error,
    handle_action,
    handle_command,
    handle_view_submission,
    register_handlers,
    verify_slack_workspace,
)
from bot.slack_adapter.blocks import render_action_modal, render_test_card, test_card_text
from bot.slack_adapter.config import (
    PHASE3_QUEUE_KEYS,
    SlackConfigurationError,
    SlackIdentityProvider,
    SlackMapping,
    SlackSettings,
    load_slack_mapping,
)
from bot.slack_adapter.fixtures import load_test_fixture
from bot.slack_adapter.service import (
    SlackTestAuthorizationError,
    SlackTestService,
    SlackTestStaleActionError,
    SlackTestValidationError,
)


TEAM = "TTEST123"
CHANNEL = "CTEST123"
USER_A = "UTESTA"
USER_B = "UTESTB"
ALL_ROLES = frozenset(
    {
        "keyword_approver",
        "content_approver",
        "performance_approver",
        "data_owner",
        "legal_compliance",
        "operations_owner",
        "privacy_officer",
        "workflow_admin",
    }
)


class FakeClient:
    def __init__(self, calls: list | None = None):
        self.calls = calls if calls is not None else []
        self.posted = []
        self.updated = []
        self.views = []

    def chat_postMessage(self, **kwargs):
        self.calls.append("post")
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1000.{len(self.posted)}"}

    def chat_update(self, **kwargs):
        self.calls.append("update")
        self.updated.append(kwargs)
        return {"ok": True}

    def views_open(self, **kwargs):
        self.calls.append("modal")
        self.views.append(kwargs)
        return {"ok": True}

    def chat_postEphemeral(self, **kwargs):
        self.calls.append("ephemeral")
        return {"ok": True}


class SlackResponseLike:
    """Mimic SlackResponse: supports get(), but not dict(response)."""

    def __init__(self, data):
        self.data = data

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __iter__(self):
        return iter("not-a-mapping")


class AuthClient:
    def __init__(self, response):
        self.response = response

    def auth_test(self):
        return self.response


class FakeBoltApp:
    def __init__(self):
        self.commands = {}
        self.actions = {}
        self.views = {}

    @staticmethod
    def _decorator(registry, key):
        def register(listener):
            registry[key] = listener
            return listener

        return register

    def command(self, key):
        return self._decorator(self.commands, key)

    def action(self, key):
        return self._decorator(self.actions, key)

    def view(self, key):
        return self._decorator(self.views, key)


class Phase3SlackTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "phase3.db"
        self.mapping = SlackMapping(
            workspace_id=TEAM,
            allowed_users={USER_A: ALL_ROLES, USER_B: ALL_ROLES},
            queue_channels={key: CHANNEL for key in PHASE3_QUEUE_KEYS},
        )
        self.service = SlackTestService(self.db, session_ttl_minutes=15)
        self.fixtures = {
            name: load_test_fixture(name)
            for name in ("keyword", "content", "performance", "data")
        }
        self.deps = SlackDependencies(
            mapping=self.mapping,
            identity=SlackIdentityProvider(self.mapping),
            service=self.service,
            fixtures=self.fixtures,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def session(self, kind="keyword", *, now=None):
        session = self.service.create_session(
            command_id=f"cmd:{kind}:{now or 'now'}",
            fixture=self.fixtures[kind],
            team_id=TEAM,
            channel_id=CHANNEL,
            created_by=f"slack:{TEAM}:{USER_A}",
            now=now,
        )
        return self.service.bind_message(session["session_id"], message_ts="1000.1")

    def apply(self, session, *, interaction="a1", action="approved", user=USER_A,
              role="keyword_approver", reason=None, now=None, **overrides):
        values = {
            "interaction_id": interaction,
            "session_id": session["session_id"],
            "action_type": action,
            "team_id": TEAM,
            "channel_id": CHANNEL,
            "message_ts": "1000.1",
            "user_id": user,
            "actor_roles": ALL_ROLES,
            "acting_role": role,
            "reason": reason,
            "now": now,
        }
        values.update(overrides)
        return self.service.apply_action(**values)

    def test_schema_and_interaction_events_are_append_only(self):
        conn = get_conn(self.db)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        conn.close()
        self.assertIn("slack_test_sessions", tables)
        self.assertIn("slack_interaction_events", tables)
        self.assertIn("slack_interaction_events_no_update", triggers)
        self.assertIn("slack_interaction_events_no_delete", triggers)

        session = self.session()
        self.apply(session)
        conn = get_conn(self.db)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE slack_interaction_events SET reason='changed'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM slack_interaction_events")
        conn.close()

    def test_mapping_is_exact_and_fail_closed(self):
        path = self.root / "mapping.yaml"
        content = {
            "workspace_id": TEAM,
            "allowed_users": {USER_A: {"roles": ["keyword_approver"]}},
            "queue_channels": {key: CHANNEL for key in PHASE3_QUEUE_KEYS},
        }
        path.write_text(yaml.safe_dump(content), encoding="utf-8")
        mapping = load_slack_mapping(path)
        actor = SlackIdentityProvider(mapping).actor_for(TEAM, USER_A)
        self.assertEqual(f"slack:{TEAM}:{USER_A}", actor.actor_id)
        with self.assertRaises(SlackConfigurationError):
            SlackIdentityProvider(mapping).actor_for("TOTHER", USER_A)
        with self.assertRaises(SlackConfigurationError):
            SlackIdentityProvider(mapping).actor_for(TEAM, "UUNKNOWN")

        content["allowed_users"][USER_A]["roles"] = ["forged_role"]
        path.write_text(yaml.safe_dump(content), encoding="utf-8")
        with self.assertRaises(SlackConfigurationError):
            load_slack_mapping(path)

        content["allowed_users"][USER_A]["roles"] = ["keyword_approver"]
        del content["queue_channels"]["dev_team.queue"]
        path.write_text(yaml.safe_dump(content), encoding="utf-8")
        with self.assertRaises(SlackConfigurationError):
            load_slack_mapping(path)

    def test_settings_require_bot_and_app_tokens(self):
        settings = SlackSettings.from_env(
            {
                "SLACK_BOT_TOKEN": "xoxb-test",
                "SLACK_APP_TOKEN": "xapp-test",
                "SLACK_MAPPING_PATH": str(self.root / "mapping.yaml"),
                "SLACK_TEST_SESSION_TTL_MINUTES": "15",
            }
        )
        settings.validate_runtime()
        with self.assertRaises(SlackConfigurationError):
            SlackSettings.from_env(
                {"SLACK_TEST_SESSION_TTL_MINUTES": "0"}
            )
        with self.assertRaises(SlackConfigurationError):
            SlackSettings("secret", "xapp-test", self.root / "x").validate_runtime()
        secret = "xoxb-must-never-appear"
        self.assertNotIn(secret, _safe_error(RuntimeError(secret)))

    def test_workspace_verification_accepts_slack_response_object(self):
        response = SlackResponseLike(
            {"ok": True, "team_id": TEAM, "user_id": "UBOT123"}
        )
        result = verify_slack_workspace(AuthClient(response), self.mapping)
        self.assertEqual(
            {"ok": True, "team_id": TEAM, "user_id": "UBOT123"}, result
        )
        with self.assertRaises(SlackConfigurationError):
            verify_slack_workspace(
                AuthClient(SlackResponseLike({"ok": True, "team_id": "TOTHER"})),
                self.mapping,
            )

    def test_registered_bolt_listeners_only_request_supported_arguments(self):
        app = FakeBoltApp()
        register_handlers(app, self.deps)
        self.assertEqual({"/hermes-test"}, set(app.commands))
        self.assertEqual(
            {"ack", "command", "respond", "client"},
            set(inspect.signature(app.commands["/hermes-test"]).parameters),
        )
        for listener in app.actions.values():
            self.assertEqual(
                {"ack", "body", "action", "respond", "client"},
                set(inspect.signature(listener).parameters),
            )
        for listener in app.views.values():
            self.assertEqual(
                {"ack", "body", "client"},
                set(inspect.signature(listener).parameters),
            )

    def test_fixed_fixtures_and_blocks_are_safe_and_bounded(self):
        expected = {
            "keyword": ("ROUTE-KW-001", ("keyword_approver",)),
            "content": ("ROUTE-CE-002", ("content_approver", "legal_compliance")),
            "performance": (
                "ROUTE-PA-003",
                ("performance_approver", "operations_owner"),
            ),
            "data": ("ROUTE-DA-004", ("data_owner", "privacy_officer")),
        }
        for kind, (fixture_id, roles) in expected.items():
            fixture = self.fixtures[kind]
            self.assertEqual(fixture_id, fixture.fixture_id)
            self.assertEqual(roles, fixture.required_roles)
            session = self.session(kind)
            blocks = render_test_card(session, fixture)
            self.assertLessEqual(len(blocks), 50)
            self.assertIn("실제 승인 아님", test_card_text(session, fixture))
            action_values = [
                element["value"]
                for block in blocks
                if block["type"] == "actions"
                for element in block["elements"]
            ]
            self.assertTrue(action_values)
            self.assertEqual({session["session_id"]}, set(action_values))

    def test_command_and_action_idempotency(self):
        fixture = self.fixtures["keyword"]
        first = self.service.create_session(
            command_id="cmd:duplicate",
            fixture=fixture,
            team_id=TEAM,
            channel_id=CHANNEL,
            created_by=USER_A,
        )
        second = self.service.create_session(
            command_id="cmd:duplicate",
            fixture=fixture,
            team_id=TEAM,
            channel_id=CHANNEL,
            created_by=USER_A,
        )
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertTrue(second["duplicate"])
        session = self.service.bind_message(first["session_id"], message_ts="1000.1")
        applied = self.apply(session, interaction="same")
        duplicate = self.apply(session, interaction="same")
        self.assertEqual("approved", applied["state"])
        self.assertTrue(duplicate["duplicate"])
        conn = get_conn(self.db)
        count = conn.execute("SELECT COUNT(*) FROM slack_interaction_events").fetchone()[0]
        conn.close()
        self.assertEqual(1, count)

    def test_concurrent_duplicate_action_is_recorded_once(self):
        session = self.session("keyword")

        def approve():
            return self.apply(session, interaction="concurrent-same")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: approve(), range(2)))
        self.assertTrue(any(result.get("duplicate") for result in results))
        conn = get_conn(self.db)
        count = conn.execute("SELECT COUNT(*) FROM slack_interaction_events").fetchone()[0]
        conn.close()
        self.assertEqual(1, count)

    def test_single_role_approval_does_not_touch_phase1_or_handoffs(self):
        session = self.session("keyword")
        protected = (
            "work_items",
            "approval_requests",
            "approval_decisions",
            "acknowledgements",
            "handoff_jobs",
            "execution_requests",
        )
        conn = get_conn(self.db)
        before = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected
        }
        conn.close()
        result = self.apply(session)
        self.assertEqual("approved", result["state"])
        conn = get_conn(self.db)
        after = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in protected
        }
        conn.close()
        self.assertEqual(before, after)

    def test_multi_role_requires_distinct_users(self):
        session = self.session("content")
        first = self.apply(
            session,
            interaction="content-1",
            role="content_approver",
        )
        self.assertEqual("pending", first["state"])
        with self.assertRaises(SlackTestAuthorizationError):
            self.apply(
                first,
                interaction="content-2",
                user=USER_A,
                role="legal_compliance",
            )
        final = self.apply(
            first,
            interaction="content-3",
            user=USER_B,
            role="legal_compliance",
        )
        self.assertEqual("approved", final["state"])

    def test_completed_role_cannot_be_counted_twice(self):
        session = self.session("content")
        first = self.apply(
            session,
            interaction="covered-1",
            role="content_approver",
        )
        with self.assertRaises(SlackTestStaleActionError):
            self.apply(
                first,
                interaction="covered-2",
                user=USER_B,
                role="content_approver",
            )

    def test_role_forgery_and_nonrequired_role_are_denied(self):
        session = self.session("keyword")
        with self.assertRaises(SlackTestAuthorizationError):
            self.apply(
                session,
                actor_roles={"legal_compliance"},
                role="keyword_approver",
            )
        with self.assertRaises(SlackTestAuthorizationError):
            self.apply(session, role="legal_compliance")

    def test_rejection_and_revision_require_reason_and_are_terminal(self):
        session = self.session("content")
        with self.assertRaises(SlackTestValidationError):
            self.apply(
                session,
                action="rejected",
                role="content_approver",
            )
        rejected = self.apply(
            session,
            interaction="reject",
            action="rejected",
            role="content_approver",
            reason="근거 부족",
        )
        self.assertEqual("rejected", rejected["state"])
        with self.assertRaises(SlackTestStaleActionError):
            self.apply(rejected, interaction="after-reject")

        revision = self.session("performance")
        revised = self.apply(
            revision,
            interaction="revision",
            action="revision_requested",
            role="performance_approver",
            reason="수치 재검토",
        )
        self.assertEqual("revision_requested", revised["state"])

    def test_data_acknowledgement_never_becomes_approval(self):
        session = self.session("data")
        first = self.apply(
            session,
            interaction="data-1",
            action="acknowledged",
            role="data_owner",
        )
        self.assertEqual("pending", first["state"])
        final = self.apply(
            first,
            interaction="data-2",
            action="acknowledged",
            user=USER_B,
            role="privacy_officer",
        )
        self.assertEqual("acknowledged", final["state"])
        self.assertNotEqual("approved", final["state"])

    def test_wrong_binding_and_expiration_are_stale(self):
        session = self.session("keyword")
        for override in (
            {"team_id": "TOTHER"},
            {"channel_id": "COTHER"},
            {"message_ts": "9999.9"},
        ):
            with self.assertRaises(SlackTestStaleActionError):
                self.apply(session, interaction=str(override), **override)

        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        expired = self.session("keyword", now=start)
        with self.assertRaises(SlackTestStaleActionError):
            self.apply(
                expired,
                interaction="expired",
                now=start + dt.timedelta(minutes=16),
            )
        self.assertEqual("expired", self.service.get_session(expired["session_id"])["state"])

    def test_command_acknowledges_before_post_and_retries_do_not_repost(self):
        calls: list = []
        responses: list = []
        client = FakeClient(calls)
        command = {
            "team_id": TEAM,
            "channel_id": CHANNEL,
            "user_id": USER_A,
            "trigger_id": "trigger-1",
            "text": "keyword",
        }

        def ack(**kwargs):
            calls.append(("ack", kwargs))

        def respond(**kwargs):
            calls.append("respond")
            responses.append(kwargs)

        handle_command(
            ack=ack, command=command, respond=respond, client=client, deps=self.deps
        )
        self.assertEqual("ack", calls[0][0])
        self.assertEqual(1, len(client.posted))
        handle_command(
            ack=ack, command=command, respond=respond, client=client, deps=self.deps
        )
        self.assertEqual(1, len(client.posted))
        self.assertIn("이미 생성", responses[-1]["text"])

    def test_status_is_ephemeral_and_does_not_create_a_session(self):
        calls: list = []
        responses: list = []
        client = FakeClient(calls)

        def ack(**kwargs):
            calls.append(("ack", kwargs))

        def respond(**kwargs):
            responses.append(kwargs)

        handle_command(
            ack=ack,
            command={
                "team_id": TEAM,
                "channel_id": CHANNEL,
                "user_id": USER_A,
                "trigger_id": "status-trigger",
                "text": "status",
            },
            respond=respond,
            client=client,
            deps=self.deps,
        )
        self.assertEqual("ack", calls[0][0])
        self.assertEqual("ephemeral", responses[0]["response_type"])
        self.assertEqual([], client.posted)
        conn = get_conn(self.db)
        count = conn.execute("SELECT COUNT(*) FROM slack_test_sessions").fetchone()[0]
        conn.close()
        self.assertEqual(0, count)

    def test_action_acknowledges_then_updates_or_opens_modal(self):
        calls: list = []
        client = FakeClient(calls)
        responses: list = []

        def ack(**kwargs):
            calls.append(("ack", kwargs))

        def respond(**kwargs):
            responses.append(kwargs)

        keyword = self.session("keyword")
        body = {
            "team": {"id": TEAM},
            "user": {"id": USER_A},
            "channel": {"id": CHANNEL},
            "message": {"ts": "1000.1"},
            "trigger_id": "button-trigger",
        }
        action = {
            "action_id": "phase3_approve",
            "action_ts": "2000.1",
            "value": keyword["session_id"],
        }
        handle_action(
            ack=ack,
            body=body,
            action=action,
            respond=respond,
            client=client,
            deps=self.deps,
        )
        self.assertEqual("ack", calls[0][0])
        self.assertEqual(1, len(client.updated))
        self.assertEqual("approved", self.service.get_session(keyword["session_id"])["state"])

        content = self.session("content")
        action.update(value=content["session_id"], action_ts="2000.2")
        handle_action(
            ack=ack,
            body=body,
            action=action,
            respond=respond,
            client=client,
            deps=self.deps,
        )
        self.assertEqual(1, len(client.views))
        self.assertEqual(
            {"content_approver", "legal_compliance"},
            {
                option["value"]
                for option in client.views[0]["view"]["blocks"][0]["element"]["options"]
            },
        )

    def test_modal_validation_happens_before_ack_and_submission_revalidates_role(self):
        session = self.session("content")
        client = FakeClient()
        acknowledgements: list = []
        body = {
            "team": {"id": TEAM},
            "user": {"id": USER_A},
            "view": {
                "id": "V123",
                "callback_id": "phase3_rejected_modal",
                "private_metadata": '{"session_id":"' + session["session_id"] + '"}',
                "state": {
                    "values": {
                        "acting_role_block": {
                            "acting_role": {
                                "selected_option": {"value": "content_approver"}
                            }
                        },
                        "reason_block": {"reason": {"value": ""}},
                    }
                },
            },
        }

        def ack(**kwargs):
            acknowledgements.append(kwargs)

        handle_view_submission(ack=ack, body=body, client=client, deps=self.deps)
        self.assertEqual("errors", acknowledgements[-1]["response_action"])
        self.assertEqual("pending", self.service.get_session(session["session_id"])["state"])

        body["view"]["state"]["values"]["reason_block"]["reason"]["value"] = "수정 필요"
        handle_view_submission(ack=ack, body=body, client=client, deps=self.deps)
        self.assertEqual({}, acknowledgements[-1])
        self.assertEqual("rejected", self.service.get_session(session["session_id"])["state"])

    def test_modal_contains_only_session_metadata_and_server_roles(self):
        modal = render_action_modal(
            session_id="00000000-0000-0000-0000-000000000001",
            action_type="revision_requested",
            eligible_roles={"content_approver", "legal_compliance"},
        )
        self.assertEqual(
            '{"session_id": "00000000-0000-0000-0000-000000000001"}',
            modal["private_metadata"],
        )
        self.assertNotIn("token", str(modal).lower())


if __name__ == "__main__":
    unittest.main()
