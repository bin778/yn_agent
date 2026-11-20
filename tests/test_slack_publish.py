import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

from bot.db import get_conn
from bot.publication import PUBLISHER_ROLE, PublicationError, PublicationQueue
from bot.slack_adapter.config import SlackMapping
from bot.slack_adapter.publish import CHANGE_PAGE, COMMAND, OPEN_ENTRY, PAGE_SIZE, RECORD_VIEW, URL_FIELD
from bot.slack_adapter.publish import register_publish_handlers
from bot.workflow import ActorContext, create_work_item, decide, revise_pending_content


class SlackPublishTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Path(directory.name) / "hermes.db"
        self.output = {"title": "검토한 제목", "hook": "검토한 도입부", "body": "## 검토한 본문"}
        self.approver = ActorContext.verified_actor(
            "slack:TDEMO:UREVIEWER", {"content_approver"}, platform="slack",
        )
        self.publisher = ActorContext.verified_actor(
            "slack:TDEMO:UPUBLISHER", {PUBLISHER_ROLE}, platform="slack",
        )
        self.queue = PublicationQueue(self.db, "TDEMO", "CDRAFT")
        mapping = SlackMapping("TDEMO", {
            "UPUBLISHER": self.publisher.roles, "UREVIEWER": self.approver.roles,
        }, {})
        self.slash, self.actions, self.views = {}, {}, {}
        app = Mock()
        app.command.side_effect = lambda key: lambda handler: self.slash.setdefault(key, handler)
        app.action.side_effect = lambda key: lambda handler: self.actions.setdefault(key, handler)
        app.view.side_effect = lambda key: lambda handler: self.views.setdefault(key, handler)
        register_publish_handlers(app, mapping, self.db, "CDRAFT")
        self.ack, self.client, self.respond = Mock(), Mock(), Mock()

    def make_draft(self, key, *, team="TDEMO", channel="CDRAFT"):
        work = create_work_item(
            agent_id="content_editor", output=self.output, routed_to="human_review.queue",
            model_version=None, input_summary="", work_item_id=key, db_path=self.db,
        )
        with closing(get_conn(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO slack_draft_jobs (job_id, team_id, channel_id, user_id, topic) VALUES (?, ?, ?, ?, '')",
                (key, team, channel, "UREVIEWER"),
            )
        return work

    def approve(self, work, *, version=1):
        decide(
            request_id=work["request_id"], actor=self.approver, acting_role="content_approver",
            decision="approved", expected_version=version, db_path=self.db,
        )

    def open_queue(self, **overrides):
        self.slash[COMMAND](ack=self.ack, client=self.client, respond=self.respond, command={
            "team_id": "TDEMO", "channel_id": "CDRAFT", "user_id": "UPUBLISHER",
            "trigger_id": "trigger", **overrides,
        })

    def act(self, action_id, value, *, scope="TDEMO:CDRAFT", user="UPUBLISHER"):
        self.actions[action_id](ack=self.ack, client=self.client, body={
            "team": {"id": "TDEMO"}, "user": {"id": user}, "trigger_id": "trigger",
            "view": {"private_metadata": scope, "id": "VLIST", "hash": "revision"},
        }, action={"action_id": action_id, "value": value})

    def record(self, view, value, *, user="UPUBLISHER"):
        self.views[RECORD_VIEW](ack=self.ack, body={
            "team": {"id": "TDEMO"}, "user": {"id": user}, "view": {
                **view, "state": {"values": {URL_FIELD: {"value": {"value": value}}}},
            },
        })

    def test_slack_approved_queue_copy_record_and_history(self):
        self.make_draft("pending-approval")
        self.approve(self.make_draft("other-workspace", team="TOTHER"))
        self.approve(self.make_draft("other-channel", channel="COTHER"))
        work = self.make_draft("revised")
        output = {**self.output, "title": "승인된 최신 제목", "body": "## 본문\n" + "가" * 3500}
        revision = revise_pending_content(
            request_id=work["request_id"], actor=self.approver, expected_version=1,
            revised_output=output, db_path=self.db,
        )
        self.approve(revision, version=2)

        self.open_queue()
        view = self.client.views_open.call_args.kwargs["view"]
        entries = [block for block in view["blocks"] if "accessory" in block]
        self.assertEqual(len(entries), 1)
        self.assertIn("승인된 최신 제목 · v2", entries[0]["text"]["text"])
        self.assertIn("한국 시간", entries[0]["text"]["text"])
        handoff_id = entries[0]["accessory"]["value"]

        self.act(OPEN_ENTRY, handoff_id)
        detail = self.client.views_push.call_args.kwargs["view"]
        copied = "".join(block["text"]["text"] for block in detail["blocks"][3:-1])
        self.assertEqual(copied, f"# {output['title']}\n\n{output['hook']}\n\n{output['body']}")

        self.record(detail, "javascript:alert(1)")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        self.record(detail, "https://example.com/articles/123", user="UREVIEWER")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        self.assertEqual(self.queue.get(handoff_id)["status"], "pending")

        url = "https://example.com/articles/123"
        self.record(detail, url)
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "update")
        self.assertEqual(self.queue.list("pending", limit=10, offset=0), [])
        self.act(CHANGE_PAGE, "completed:0")
        history = self.client.views_update.call_args.kwargs["view"]
        self.assertEqual(history["blocks"][2]["accessory"]["value"], handoff_id)
        self.act(OPEN_ENTRY, handoff_id)
        published = self.client.views_push.call_args.kwargs["view"]
        self.assertIn("UPUBLISHER", published["blocks"][1]["text"]["text"])
        self.assertIn(url, published["blocks"][1]["text"]["text"])
        self.assertNotIn("submit", published)

        self.record(detail, url)
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "update")
        self.record(detail, "https://example.com/another")
        self.assertEqual(self.ack.call_args.kwargs["response_action"], "errors")
        with closing(get_conn(self.db)) as conn:
            (audit,) = conn.execute(
                """SELECT actor_id, work_item_version, new_state
                   FROM audit_events WHERE event_type='publication.manual_recorded'"""
            ).fetchall()
            self.assertEqual(tuple(audit), (self.publisher.actor_id, 2, "completed"))
            record = conn.execute("SELECT * FROM manual_publications").fetchone()
            self.assertEqual(record["publication_url"], url)
            handoff = conn.execute("SELECT completed_at FROM handoff_jobs WHERE handoff_id=?",
                                   (handoff_id,)).fetchone()
            self.assertEqual(handoff["completed_at"], record["recorded_at"])

    def test_queue_access_is_scoped_on_open_and_after_opening(self):
        self.open_queue(user_id="UREVIEWER")
        self.open_queue(channel_id="COTHER")
        self.open_queue(team_id="TOTHER")
        self.client.views_open.assert_not_called()
        self.assertEqual(self.respond.call_count, 3)
        self.act(CHANGE_PAGE, "completed:0", scope="TDEMO:COTHER")
        self.act(CHANGE_PAGE, "completed:0", user="UREVIEWER")
        self.client.views_update.assert_not_called()
        self.approve(self.make_draft("other-channel", channel="COTHER"))
        with closing(get_conn(self.db)) as conn:
            handoff_id = conn.execute("SELECT handoff_id FROM handoff_jobs").fetchone()[0]
        with self.assertRaises(PublicationError):
            self.queue.record(handoff_id, "https://example.com/article", self.publisher)

    def test_pending_queue_paginates_without_hiding_older_approvals(self):
        for number in range(PAGE_SIZE + 1):
            self.approve(self.make_draft(f"draft-{number}"))

        self.open_queue()
        first = self.client.views_open.call_args.kwargs["view"]["blocks"]
        first_ids = {block["accessory"]["value"] for block in first if "accessory" in block}
        self.assertEqual(len(first_ids), PAGE_SIZE)
        next_page = first[-1]["elements"][0]
        self.assertEqual(next_page["text"]["text"], "다음")

        self.act(CHANGE_PAGE, next_page["value"])
        second = self.client.views_update.call_args.kwargs["view"]["blocks"]
        second_ids = [block["accessory"]["value"] for block in second if "accessory" in block]
        self.assertEqual(len(second_ids), 1)
        self.assertNotIn(second_ids[0], first_ids)
        self.assertEqual(second[-1]["elements"][0]["value"], "pending:0")
