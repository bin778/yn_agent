import json
import os
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from bot.agents import content_editor, data_assistant, keyword_analyst, performance_analyst
from bot.llm_client import (
    CodexAuthError,
    CodexCLIClient,
    CodexExecutionError,
    CodexTimeoutError,
    CodexUnavailableError,
    SchemaParseError,
)
from bot.schemas import (
    ContentEditorOutput,
    DataAssistantOutput,
    KeywordAnalystOutput,
    PerformanceAnalystOutput,
)


REQUIRED_HELP = " ".join(
    (
        "--ephemeral",
        "--sandbox",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--color",
        "--output-schema",
    )
)


class FakeRunner:
    def __init__(self, outputs=None, *, auth="Logged in using ChatGPT"):
        self.outputs = list(outputs or [])
        self.auth = auth
        self.auth_returncode = 0
        self.exec_returncode = 0
        self.exec_stderr = ""
        self.exec_exception = None
        self.delay = 0.0
        self.calls = []
        self.exec_calls = []
        self.schema_paths = []
        self.schemas = []
        self.cwd_paths = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        suffix = args[1:]
        if suffix == ["login", "status"]:
            return subprocess.CompletedProcess(
                args, self.auth_returncode, stdout=self.auth, stderr=""
            )
        if suffix == ["exec", "--help"]:
            return subprocess.CompletedProcess(args, 0, stdout=REQUIRED_HELP, stderr="")
        if suffix == ["--version"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="codex-cli 1.2.3-test", stderr=""
            )
        if len(suffix) > 0 and suffix[0] == "exec":
            with self._lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
            try:
                self.exec_calls.append((list(args), kwargs))
                schema_path = Path(args[args.index("--output-schema") + 1])
                self.schema_paths.append(schema_path)
                self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
                self.cwd_paths.append(Path(kwargs["cwd"]))
                if self.delay:
                    time.sleep(self.delay)
                if self.exec_exception is not None:
                    raise self.exec_exception
                with self._lock:
                    output = self.outputs.pop(0) if self.outputs else "{}"
                return subprocess.CompletedProcess(
                    args,
                    self.exec_returncode,
                    stdout=output,
                    stderr=self.exec_stderr,
                )
            finally:
                with self._lock:
                    self._active -= 1
        raise AssertionError(f"unexpected command: {args}")


def client_for(runner, *, max_concurrency=1):
    return CodexCLIClient(
        command="codex",
        timeout_seconds=5,
        max_concurrency=max_concurrency,
        runner=runner,
        which=lambda _command: "/opt/codex",
    )


class CodexProviderTest(unittest.TestCase):
    def test_four_employee_schemas_parse_without_live_codex(self):
        outputs = [
            json.dumps(
                {
                    "items": [
                        {
                            "keyword": "경찰 조사 준비",
                            "estimated_search_volume": 10,
                            "intent_type": "Information",
                            "urgency_level": 2,
                            "compliance_risk": "low",
                            "reason": "정보 탐색",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "target_keyword": "경찰 조사 준비",
                    "title": "조사 전 확인사항",
                    "hook": "절차를 차분히 확인합니다.",
                    "body": "일반적인 절차 안내입니다.",
                    "flagged_claims": [],
                    "sensitive_flags": [],
                    "reason": "단정적 표현을 피함",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "status_summary": "Normal",
                    "tracking_anomalies": [],
                    "action_items": [],
                    "privacy_flags": [],
                    "suspected_layer": "",
                    "reason": "이상 없음",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "recommendations": [
                        {
                            "keyword": "형사 변호사",
                            "campaign": "검색",
                            "action": "HOLD",
                            "adjustment_percentage": 0,
                            "target_device": "all",
                            "target_hour_range": "",
                            "category": "형사",
                            "sensitive_keyword": False,
                            "caution_flag": False,
                            "rationale": "관찰값 유지",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ]
        runner = FakeRunner(outputs)
        client = client_for(runner)
        models = (
            KeywordAnalystOutput,
            ContentEditorOutput,
            DataAssistantOutput,
            PerformanceAnalystOutput,
        )
        for model in models:
            result = client.call_structured("system", "user", model)
            self.assertIsInstance(result, model)
        self.assertEqual(4, len(runner.exec_calls))

    def test_cli_schema_requires_defaulted_fields_at_top_level_and_in_refs(self):
        runner = FakeRunner([
            '{"target_keyword":"예시","title":"예시","hook":"예시","body":"예시","reason":"예시"}',
            '{"items":[]}',
        ])
        client = client_for(runner)
        draft = client.call_structured("system", "user", ContentEditorOutput)
        self.assertEqual(draft.flagged_claims, [])
        client.call_structured("system", "user", KeywordAnalystOutput)

        content_schema, keyword_schema = runner.schemas
        self.assertEqual(content_schema["required"], list(content_schema["properties"]))
        self.assertIn("flagged_claims", content_schema["required"])
        self.assertIn("sensitive_flags", content_schema["required"])
        nested = keyword_schema["$defs"]["KeywordItem"]
        self.assertEqual(nested["required"], list(nested["properties"]))
        self.assertFalse(nested["additionalProperties"])
        self.assertNotIn("default", nested["properties"]["compliance_note"])

    def test_schema_failure_retries_then_succeeds(self):
        valid = json.dumps({"status_summary": "Normal", "reason": "ok"})
        runner = FakeRunner(["not-json", valid])
        result = client_for(runner).call_structured(
            "system", "user", DataAssistantOutput, max_retries=1
        )
        self.assertEqual("Normal", result.status_summary)
        self.assertEqual(2, len(runner.exec_calls))

    def test_schema_failure_exhaustion_raises_sanitized_error(self):
        runner = FakeRunner(["bad-secret-output", "still-bad"])
        with self.assertRaises(SchemaParseError) as raised:
            client_for(runner).call_structured(
                "system", "user", DataAssistantOutput, max_retries=1
            )
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(2, len(runner.exec_calls))

    def test_missing_cli_fails_closed(self):
        client = CodexCLIClient(
            runner=FakeRunner(), which=lambda _command: None
        )
        with self.assertRaises(CodexUnavailableError):
            client.check_runtime()

    def test_logout_and_api_key_auth_are_rejected(self):
        logged_out = FakeRunner(auth="")
        logged_out.auth_returncode = 1
        with self.assertRaises(CodexAuthError):
            client_for(logged_out).check_runtime()

        api_key = FakeRunner(auth="Logged in using an API key")
        with self.assertRaises(CodexAuthError):
            client_for(api_key).check_runtime()

    def test_timeout_is_not_retried(self):
        runner = FakeRunner()
        runner.exec_exception = subprocess.TimeoutExpired("codex", 5)
        with self.assertRaises(CodexTimeoutError):
            client_for(runner).call_structured(
                "system", "user", DataAssistantOutput, max_retries=2
            )
        self.assertEqual(1, len(runner.exec_calls))

    def test_nonzero_exit_does_not_expose_stderr_or_retry(self):
        runner = FakeRunner()
        runner.exec_returncode = 9
        runner.exec_stderr = "Bearer super-secret prompt-fragment"
        with self.assertRaises(CodexExecutionError) as raised:
            client_for(runner).call_structured(
                "system", "user", DataAssistantOutput, max_retries=2
            )
        message = str(raised.exception)
        self.assertNotIn("super-secret", message)
        self.assertNotIn("prompt-fragment", message)
        self.assertEqual(1, len(runner.exec_calls))

    def test_invocation_is_isolated_and_does_not_inherit_api_keys(self):
        output = json.dumps({"status_summary": "Normal", "reason": "ok"})
        runner = FakeRunner([output])
        hostile = "$(touch should-not-run); `whoami`"
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "api-secret",
                "CODEX_API_KEY": "codex-secret",
                "CODEX_ACCESS_TOKEN": "token-secret",
                "SLACK_BOT_TOKEN": "slack-secret",
            },
        ):
            result = client_for(runner).call_structured(
                "system", hostile, DataAssistantOutput
            )
        self.assertEqual("Normal", result.status_summary)

        args, kwargs = runner.exec_calls[0]
        self.assertFalse(kwargs["shell"])
        self.assertNotIn(hostile, args)
        self.assertIn(hostile, kwargs["input"])
        self.assertEqual("read-only", args[args.index("--sandbox") + 1])
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        for key in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "SLACK_BOT_TOKEN",
        ):
            self.assertNotIn(key, kwargs["env"])
        self.assertFalse(runner.schema_paths[0].exists())
        self.assertFalse(runner.cwd_paths[0].exists())
        self.assertFalse(Path("should-not-run").exists())
        self.assertFalse(runner.schemas[0]["additionalProperties"])

    def test_concurrency_limit_serializes_exec_calls(self):
        output = json.dumps({"status_summary": "Normal", "reason": "ok"})
        runner = FakeRunner([output, output])
        runner.delay = 0.05
        client = client_for(runner, max_concurrency=1)
        client.check_runtime()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    client.call_structured,
                    "system",
                    f"user-{index}",
                    DataAssistantOutput,
                )
                for index in range(2)
            ]
            for future in futures:
                self.assertEqual("Normal", future.result().status_summary)
        self.assertEqual(1, runner.max_active)

    def test_model_identity_records_cli_version_and_default_policy(self):
        runner = FakeRunner()
        client = client_for(runner)
        self.assertEqual("codex-cli/1.2.3-test:default", client.get_model_identity())

    def test_failed_runtime_refresh_clears_previous_success(self):
        runner = FakeRunner()
        client = client_for(runner)
        self.assertEqual("codex-cli/1.2.3-test:default", client.get_model_identity())
        runner.auth = "Logged in using an API key"
        with self.assertRaises(CodexAuthError):
            client.check_runtime(refresh=True)
        with self.assertRaises(CodexAuthError):
            client.get_model_identity()

    def test_each_agent_persists_codex_model_identity(self):
        identity = "codex-cli/test:default"

        keyword_output = KeywordAnalystOutput.model_validate(
            {
                "items": [
                    {
                        "keyword": "경찰 조사 준비",
                        "estimated_search_volume": 10,
                        "intent_type": "Information",
                        "urgency_level": 2,
                        "compliance_risk": "low",
                        "reason": "정보 탐색",
                    }
                ]
            }
        )
        with (
            patch.object(keyword_analyst, "build_system_prompt", return_value="system"),
            patch.object(keyword_analyst, "_format_history", return_value="history"),
            patch.object(keyword_analyst, "call_structured", return_value=keyword_output),
            patch.object(keyword_analyst, "get_model_identity", return_value=identity),
            patch.object(
                keyword_analyst,
                "create_work_item",
                return_value={"work_item_id": "kw", "version": 1, "request_id": "rq"},
            ) as create_keyword,
        ):
            keyword_analyst.run_daily_keyword_scan()
        self.assertEqual(identity, create_keyword.call_args.kwargs["model_version"])

        content_output = ContentEditorOutput.model_validate(
            {
                "target_keyword": "경찰 조사 준비",
                "title": "조사 전 확인사항",
                "hook": "절차를 확인합니다.",
                "body": "일반적인 절차 안내입니다.",
                "reason": "단정적 표현을 피함",
            }
        )
        with (
            patch.object(content_editor, "build_system_prompt", return_value="system"),
            patch.object(content_editor, "_format_rejections", return_value="history"),
            patch.object(content_editor, "call_structured", return_value=content_output),
            patch.object(content_editor, "get_model_identity", return_value=identity),
            patch.object(
                content_editor,
                "create_work_item",
                return_value={"work_item_id": "content", "version": 1, "request_id": "rq"},
            ) as create_content,
        ):
            content_editor._draft_content(keyword_output.items[0])
        self.assertEqual(identity, create_content.call_args.kwargs["model_version"])

        performance_output = PerformanceAnalystOutput.model_validate(
            {
                "recommendations": [
                    {
                        "keyword": "형사 변호사",
                        "action": "HOLD",
                        "adjustment_percentage": 0,
                        "rationale": "관찰값 유지",
                    }
                ]
            }
        )
        with (
            patch.object(performance_analyst, "get_tracking_status", return_value=None),
            patch.object(performance_analyst, "build_system_prompt", return_value="system"),
            patch.object(performance_analyst, "_format_history", return_value="history"),
            patch.object(
                performance_analyst, "call_structured", return_value=performance_output
            ),
            patch.object(performance_analyst, "get_model_identity", return_value=identity),
            patch.object(
                performance_analyst,
                "create_work_item",
                return_value={"work_item_id": "perf", "version": 1, "request_id": "rq"},
            ) as create_performance,
        ):
            performance_analyst.run_bid_optimization(tracking_override="normal")
        self.assertEqual(identity, create_performance.call_args.kwargs["model_version"])

        data_output = DataAssistantOutput.model_validate(
            {"status_summary": "Normal", "reason": "이상 없음"}
        )
        with (
            patch.object(data_assistant, "build_system_prompt", return_value="system"),
            patch.object(data_assistant, "_format_anomaly_history", return_value="history"),
            patch.object(data_assistant, "call_structured", return_value=data_output),
            patch.object(data_assistant, "get_model_identity", return_value=identity),
            patch.object(
                data_assistant,
                "create_work_item",
                return_value={"work_item_id": "data", "version": 1, "request_id": "rq"},
            ) as create_data,
            patch.object(data_assistant, "upsert_tracking_status"),
        ):
            data_assistant.run_anomaly_check(scenario="normal")
        self.assertEqual(identity, create_data.call_args.kwargs["model_version"])

    def test_performance_critical_halt_is_recorded_as_non_llm_policy(self):
        with (
            patch.object(performance_analyst, "get_tracking_status", return_value=None),
            patch.object(performance_analyst, "call_structured") as call_llm,
            patch.object(
                performance_analyst,
                "create_work_item",
                return_value={"work_item_id": "halt", "version": 1, "request_id": "rq"},
            ) as create_item,
        ):
            result = performance_analyst.run_bid_optimization(
                tracking_override="critical"
            )
        self.assertTrue(result["halted"])
        call_llm.assert_not_called()
        self.assertEqual(
            "policy-engine/no-llm", create_item.call_args.kwargs["model_version"]
        )


if __name__ == "__main__":
    unittest.main()
