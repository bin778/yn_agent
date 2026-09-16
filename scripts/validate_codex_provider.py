#!/usr/bin/env python3
"""Validate the local ChatGPT-authenticated Codex CLI provider."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import (  # noqa: E402
    CODEX_COMMAND,
    CODEX_MAX_CONCURRENCY,
    CODEX_TIMEOUT_SECONDS,
    LLM_PROVIDER,
)
from bot.llm_client import call_structured, check_codex_runtime  # noqa: E402


REQUIRED_FILES = (
    "bot/llm_client.py",
    "docs/phase2_5/codex_cli_provider.md",
    "tests/test_codex_provider.py",
)
FORBIDDEN_RUNTIME_TEXT = (
    "GOOGLE_API_KEY",
    "GEMINI_MODEL",
    "google.genai",
    "call_gemini_structured",
)


class SmokeOutput(BaseModel):
    status: Literal["ok"]
    detail: str


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

    if LLM_PROVIDER != "codex_cli":
        errors.append("LLM_PROVIDER must default to codex_cli")
    if not CODEX_COMMAND:
        errors.append("CODEX_COMMAND must not be empty")
    if CODEX_TIMEOUT_SECONDS <= 0:
        errors.append("CODEX_TIMEOUT_SECONDS must be positive")
    if CODEX_MAX_CONCURRENCY != 1:
        errors.append("Phase 2.5 requires CODEX_MAX_CONCURRENCY=1")

    active_paths = list((ROOT / "bot").rglob("*.py"))
    active_paths.extend((ROOT / "bot" / name) for name in ("requirements.txt", ".env.example"))
    for path in active_paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_RUNTIME_TEXT:
            if forbidden in text:
                errors.append(f"active Gemini runtime reference remains in {path.relative_to(ROOT)}: {forbidden}")

    requirements = (ROOT / "bot/requirements.txt").read_text(encoding="utf-8").lower()
    if "google-genai" in requirements:
        errors.append("google-genai dependency remains active")

    env_example = (ROOT / "bot/.env.example").read_text(encoding="utf-8")
    for contract in (
        "LLM_PROVIDER=codex_cli",
        "CODEX_COMMAND=codex",
        "CODEX_TIMEOUT_SECONDS=180",
        "CODEX_MAX_CONCURRENCY=1",
    ):
        if contract not in env_example:
            errors.append(f"bot/.env.example missing contract: {contract}")

    for relative in (
        "bot/agents/keyword_analyst.py",
        "bot/agents/content_editor.py",
        "bot/agents/performance_analyst.py",
        "bot/agents/data_assistant.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        if "call_structured" not in text or "get_model_identity" not in text:
            errors.append(f"agent is not wired to Codex model identity: {relative}")
    return errors


def live_gate_errors() -> list[str]:
    errors: list[str] = []
    before = _git_status()
    try:
        check_codex_runtime()
        result = call_structured(
            system_prompt=(
                "This is a Hermes local runtime health check. Do not use tools or files."
            ),
            user_prompt=(
                'Return status="ok" and a short detail confirming structured output.'
            ),
            schema_model=SmokeOutput,
            max_retries=0,
        )
        if result.status != "ok":
            errors.append("Codex live structured response did not return status=ok")
    except Exception as exc:
        errors.append(f"Codex live gate failed: {type(exc).__name__}: {exc}")
    after = _git_status()
    if before != after:
        errors.append("Codex live gate changed repository files")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate",
        action="store_true",
        help="also verify ChatGPT login and make one live structured Codex call",
    )
    args = parser.parse_args()

    errors = automated_errors()
    if errors:
        print(f"CODEX PROVIDER AUTOMATED CHECK: FAIL ({len(errors)} errors)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("CODEX PROVIDER AUTOMATED CHECK: PASS")

    if args.gate:
        errors = live_gate_errors()
        if errors:
            print(f"CODEX PROVIDER LIVE GATE: FAIL ({len(errors)} errors)")
            for error in errors:
                print(f"- {error}")
            return 1
        print("CODEX PROVIDER LIVE GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
