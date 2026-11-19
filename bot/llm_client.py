"""Structured LLM calls through a locally authenticated Codex CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ValidationError

from .config import (
    CODEX_COMMAND,
    CODEX_MAX_CONCURRENCY,
    CODEX_TIMEOUT_SECONDS,
    LLM_PROVIDER,
    check_required_env,
)


class CodexUnavailableError(RuntimeError):
    """The configured Codex executable is missing or incompatible."""


class CodexAuthError(RuntimeError):
    """Codex is not authenticated with a ChatGPT subscription login."""


class CodexTimeoutError(RuntimeError):
    """A Codex subprocess exceeded the configured deadline."""


class CodexExecutionError(RuntimeError):
    """Codex exited before returning a valid structured response."""


class SchemaParseError(RuntimeError):
    """Codex output did not validate against the requested Pydantic schema."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]

_REQUIRED_EXEC_OPTIONS = (
    "--ephemeral",
    "--sandbox",
    "--ignore-user-config",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--color",
    "--output-schema",
)
_SAFE_CODEX_ENV_KEYS = (
    "HOME",
    "PATH",
    "TMPDIR",
    "CODEX_HOME",
    "CODEX_CA_CERTIFICATE",
    "SSL_CERT_FILE",
)


def _codex_environment() -> dict[str, str]:
    """Expose only what the CLI needs; never inherit app or API credentials."""
    env = {
        key: os.environ[key]
        for key in _SAFE_CODEX_ENV_KEYS
        if key in os.environ
    }
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    env["RUST_LOG"] = "error"
    return env


def _strict_object_schema(value: object) -> None:
    """Convert a Pydantic schema to the CLI's strict structured-output schema."""
    if not isinstance(value, dict):
        return

    value.pop("default", None)
    properties = value.get("properties", {})
    if value.get("type") == "object" or "properties" in value:
        value["additionalProperties"] = False
        value["required"] = list(properties)

    for key in ("properties", "$defs", "definitions"):
        for child in value.get(key, {}).values():
            _strict_object_schema(child)
    _strict_object_schema(value.get("items"))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for child in value.get(key, ()):
            _strict_object_schema(child)



class CodexCLIClient:
    """Synchronous, injectable adapter around ``codex exec``."""

    def __init__(
        self,
        *,
        command: str = CODEX_COMMAND,
        timeout_seconds: int = CODEX_TIMEOUT_SECONDS,
        max_concurrency: int = CODEX_MAX_CONCURRENCY,
        runner: Runner = subprocess.run,
        which: Which = shutil.which,
    ) -> None:
        if not command:
            raise ValueError("Codex command must not be empty")
        if timeout_seconds <= 0 or max_concurrency <= 0:
            raise ValueError("Codex timeout and concurrency must be positive")
        self.command = command
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self._which = which
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._runtime_lock = threading.Lock()
        self._resolved_command: str | None = None
        self._model_identity: str | None = None

    def _run_control(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(
                args,
                capture_output=True,
                text=True,
                timeout=min(self.timeout_seconds, 30),
                check=False,
                shell=False,
                env=_codex_environment(),
            )
        except FileNotFoundError as exc:
            raise CodexUnavailableError("Codex CLI executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CodexUnavailableError("Codex CLI preflight timed out") from exc
        except OSError as exc:
            raise CodexUnavailableError("Codex CLI preflight could not start") from exc

    def check_runtime(self, *, refresh: bool = False) -> None:
        """Fail closed unless a compatible CLI is logged in through ChatGPT."""
        check_required_env()
        if LLM_PROVIDER != "codex_cli":
            raise CodexUnavailableError("Only the codex_cli provider is supported")

        with self._runtime_lock:
            if self._model_identity is not None and not refresh:
                return
            # A failed refresh must not leave an older successful preflight cached.
            self._resolved_command = None
            self._model_identity = None

            resolved = self._which(self.command)
            if not resolved:
                raise CodexUnavailableError("Codex CLI executable was not found")

            status = self._run_control([resolved, "login", "status"])
            if status.returncode != 0:
                raise CodexAuthError("Codex CLI login status check failed")
            auth_text = f"{status.stdout}\n{status.stderr}".lower()
            if "logged in using chatgpt" not in auth_text:
                raise CodexAuthError(
                    "Codex CLI must be logged in with ChatGPT subscription credentials"
                )

            help_result = self._run_control([resolved, "exec", "--help"])
            if help_result.returncode != 0:
                raise CodexUnavailableError("Codex CLI exec help check failed")
            help_text = f"{help_result.stdout}\n{help_result.stderr}"
            missing = [option for option in _REQUIRED_EXEC_OPTIONS if option not in help_text]
            if missing:
                raise CodexUnavailableError(
                    "Codex CLI lacks required structured execution options"
                )

            version_result = self._run_control([resolved, "--version"])
            if version_result.returncode != 0:
                raise CodexUnavailableError("Codex CLI version check failed")
            version_text = version_result.stdout.strip()
            version = version_text.split()[-1] if version_text else "unknown"
            self._resolved_command = resolved
            self._model_identity = f"codex-cli/{version}:default"

    def get_model_identity(self) -> str:
        self.check_runtime()
        assert self._model_identity is not None
        return self._model_identity

    @staticmethod
    def _prompt(system_prompt: str, user_prompt: str) -> str:
        return (
            "You are a constrained structured-output worker. "
            "Do not use tools, inspect files, or perform external actions. "
            "Return only JSON that conforms to the supplied output schema.\n\n"
            "<system_instructions>\n"
            f"{system_prompt}\n"
            "</system_instructions>\n\n"
            "<user_task>\n"
            f"{user_prompt}\n"
            "</user_task>\n"
        )

    def _invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_model: type[BaseModel],
    ) -> BaseModel:
        self.check_runtime()
        assert self._resolved_command is not None

        schema = schema_model.model_json_schema()
        _strict_object_schema(schema)

        with tempfile.TemporaryDirectory(prefix="hermes-codex-") as temp_dir:
            schema_path = Path(temp_dir) / "output-schema.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False), encoding="utf-8"
            )
            args = [
                self._resolved_command,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "-",
            ]
            try:
                completed = self._runner(
                    args,
                    input=self._prompt(system_prompt, user_prompt),
                    capture_output=True,
                    text=True,
                    cwd=temp_dir,
                    timeout=self.timeout_seconds,
                    check=False,
                    shell=False,
                    env=_codex_environment(),
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexTimeoutError("Codex structured generation timed out") from exc
            except FileNotFoundError as exc:
                raise CodexUnavailableError("Codex CLI executable was not found") from exc
            except OSError as exc:
                raise CodexExecutionError("Codex structured generation could not start") from exc

            if completed.returncode != 0:
                # stderr may contain prompt fragments or provider diagnostics.
                raise CodexExecutionError(
                    f"Codex structured generation failed with exit code {completed.returncode}"
                )
            return schema_model.model_validate_json(completed.stdout)

    def call_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_model: type[BaseModel],
        max_retries: int = 2,
    ) -> BaseModel:
        if max_retries < 0:
            raise ValueError("max_retries must be zero or greater")
        last_error: ValidationError | ValueError | None = None
        with self._slots:
            for _attempt in range(max_retries + 1):
                try:
                    return self._invoke(system_prompt, user_prompt, schema_model)
                except (ValidationError, ValueError) as exc:
                    last_error = exc
        raise SchemaParseError(
            f"Codex output failed schema validation after {max_retries + 1} attempts"
        ) from last_error


_DEFAULT_CLIENT = CodexCLIClient()


def call_structured(
    system_prompt: str,
    user_prompt: str,
    schema_model: type[BaseModel],
    max_retries: int = 2,
) -> BaseModel:
    return _DEFAULT_CLIENT.call_structured(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema_model=schema_model,
        max_retries=max_retries,
    )


def get_model_identity() -> str:
    return _DEFAULT_CLIENT.get_model_identity()


def check_codex_runtime() -> None:
    _DEFAULT_CLIENT.check_runtime(refresh=True)
