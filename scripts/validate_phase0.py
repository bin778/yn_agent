#!/usr/bin/env python3
"""Phase 0 artifacts and human approval gate validator."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

SOURCE_FILE = ROOT / "_shared/knowledge/sources.yaml"
REVIEW_FILE = ROOT / "docs/phase0/reviews.yaml"
RUBRIC_FILE = ROOT / "evals/rubric.yaml"
CASE_FILES = (
    ROOT / "evals/fixtures/routing_cases.yaml",
    ROOT / "evals/legal_golden/cases.yaml",
    ROOT / "evals/adversarial/cases.yaml",
)
HUMAN_REVIEW_CASE_FILES = CASE_FILES[1:]

VALID_AGENT_IDS = {
    "keyword_analyst",
    "content_editor",
    "performance_analyst",
    "data_assistant",
}

PII_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    "korean_mobile": re.compile(r"(?<!\d)01[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
    "resident_registration_number": re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)"),
    "ipv4": re.compile(
        r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
        r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"
    ),
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.relative_to(ROOT)}: YAML root must be a mapping")
    return data


def automated_errors() -> list[str]:
    errors: list[str] = []

    try:
        rubric = load_yaml(RUBRIC_FILE)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [str(exc)]

    required_files = rubric.get("automated_gate", {}).get("required_files", [])
    for relative in required_files:
        if not (ROOT / relative).is_file():
            errors.append(f"required file missing: {relative}")

    try:
        source_data = load_yaml(SOURCE_FILE)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [*errors, str(exc)]

    sources = source_data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources.yaml: sources must be a non-empty list")
        sources = []

    source_ids: set[str] = set()
    for source in sources:
        source_id = source.get("id") if isinstance(source, dict) else None
        if not source_id:
            errors.append("sources.yaml: every source needs an id")
            continue
        if source_id in source_ids:
            errors.append(f"duplicate source id: {source_id}")
        source_ids.add(source_id)
        if not source.get("title") or not source.get("authority") or not source.get("source_type"):
            errors.append(f"source {source_id}: title, authority and source_type are required")
        if source.get("source_type") != "internal_policy" and not source.get("url"):
            errors.append(f"source {source_id}: official source URL is required")
        artifact = source.get("artifact")
        if artifact:
            relative_artifact = artifact.get("path")
            expected_hash = artifact.get("sha256")
            if not relative_artifact or not expected_hash:
                errors.append(f"source {source_id}: artifact path and sha256 are required")
            else:
                artifact_path = (ROOT / relative_artifact).resolve()
                if ROOT.resolve() not in artifact_path.parents:
                    errors.append(f"source {source_id}: artifact must be stored inside repository")
                elif not artifact_path.is_file():
                    errors.append(f"source {source_id}: artifact file missing: {relative_artifact}")
                else:
                    actual_hash = file_sha256(artifact_path)
                    if actual_hash != expected_hash:
                        errors.append(
                            f"source {source_id}: artifact sha256 mismatch "
                            f"(expected {expected_hash}, got {actual_hash})"
                        )
                    if artifact.get("media_type") == "application/pdf":
                        with artifact_path.open("rb") as handle:
                            if handle.read(5) != b"%PDF-":
                                errors.append(f"source {source_id}: artifact is not a PDF")
        review = source.get("review") or {}
        if not review.get("status") or not review.get("reviewer_role") or "reviewer" not in review:
            errors.append(
                f"source {source_id}: review status, reviewer_role and reviewer fields are required"
            )
        if review.get("status") == "approved" and not approval_is_valid(review):
            errors.append(f"source {source_id}: approved review needs reviewer and reviewed_at")

    case_ids: set[str] = set()
    for case_file in CASE_FILES:
        try:
            case_data = load_yaml(case_file)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(str(exc))
            continue
        cases = case_data.get("cases")
        if not isinstance(cases, list) or not cases:
            errors.append(f"{case_file.relative_to(ROOT)}: cases must be a non-empty list")
            continue
        for case in cases:
            case_id = case.get("id") if isinstance(case, dict) else None
            if not case_id:
                errors.append(f"{case_file.relative_to(ROOT)}: every case needs an id")
                continue
            if case_id in case_ids:
                errors.append(f"duplicate case id: {case_id}")
            case_ids.add(case_id)
            if case.get("agent_id") not in VALID_AGENT_IDS:
                errors.append(f"case {case_id}: invalid agent_id")
            for field in ("input", "expected", "basis_refs", "provenance"):
                if field not in case:
                    errors.append(f"case {case_id}: {field} is required")
            if case.get("contains_personal_data") is not False:
                errors.append(f"case {case_id}: contains_personal_data must be false in repository")
            for basis_ref in case.get("basis_refs") or []:
                if basis_ref not in source_ids:
                    errors.append(f"case {case_id}: unknown basis_ref {basis_ref}")

            serialized_input = yaml.safe_dump(case.get("input"), allow_unicode=True)
            for pattern_name, pattern in PII_PATTERNS.items():
                if pattern.search(serialized_input):
                    errors.append(f"case {case_id}: possible {pattern_name} in input")

            if case_file in HUMAN_REVIEW_CASE_FILES:
                review = case.get("review") or {}
                if (
                    not review.get("status")
                    or not review.get("reviewer_role")
                    or "reviewer" not in review
                ):
                    errors.append(f"case {case_id}: human review metadata is required")
                if review.get("status") == "approved" and not approval_is_valid(review):
                    errors.append(f"case {case_id}: approved review needs reviewer and reviewed_at")

    for required_phrase, relative_path in (
        ("승인 전에는", "docs/phase0/workflow_contract.md"),
        ("fail-closed", "docs/phase0/workflow_contract.md"),
        ("EXECUTION_ENABLED=false", "docs/phase0/execution_policy.md"),
        ("default deny", "docs/phase0/execution_policy.md"),
    ):
        path = ROOT / relative_path
        if path.is_file() and required_phrase not in path.read_text(encoding="utf-8"):
            errors.append(f"{relative_path}: required policy phrase missing: {required_phrase}")

    return errors


def approval_is_valid(approval: dict[str, Any]) -> bool:
    return (
        approval.get("status") == "approved"
        and bool(approval.get("reviewer"))
        and bool(approval.get("reviewed_at"))
    )


def human_gate_blockers() -> list[str]:
    blockers: list[str] = []

    reviews = load_yaml(REVIEW_FILE)
    for artifact in reviews.get("artifacts") or []:
        artifact_id = artifact.get("id", "unknown")
        for approval in artifact.get("approvals") or []:
            role = approval.get("role", "unknown")
            if approval.get("status") != "approved":
                blockers.append(f"artifact {artifact_id}: {role} approval pending")
            elif not approval_is_valid(approval):
                blockers.append(f"artifact {artifact_id}: {role} reviewer/reviewed_at missing")

    sources = load_yaml(SOURCE_FILE).get("sources") or []
    for source in sources:
        review = source.get("review") or {}
        if review.get("status") != "approved":
            blockers.append(f"source {source.get('id')}: review pending")
        elif not approval_is_valid(review):
            blockers.append(f"source {source.get('id')}: reviewer/reviewed_at missing")
        if source.get("source_type") in {
            "statute",
            "professional_rule",
            "enforcement_decree",
        } and not source.get("effective_date"):
            blockers.append(f"source {source.get('id')}: effective_date not confirmed")

    for case_file in HUMAN_REVIEW_CASE_FILES:
        cases = load_yaml(case_file).get("cases") or []
        for case in cases:
            review = case.get("review") or {}
            if review.get("status") != "approved":
                blockers.append(f"case {case.get('id')}: review pending")
            elif not approval_is_valid(review):
                blockers.append(f"case {case.get('id')}: reviewer/reviewed_at missing")

    return blockers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    gate_group = parser.add_mutually_exclusive_group()
    gate_group.add_argument(
        "--gate",
        action="store_true",
        help="enforce the Phase 0 engineering gate for starting Phase 1",
    )
    gate_group.add_argument(
        "--release-gate",
        action="store_true",
        help="enforce deferred human approvals before a limited pilot or production",
    )
    args = parser.parse_args()

    errors = automated_errors()
    if errors:
        print(f"PHASE 0 AUTOMATED CHECK: FAIL ({len(errors)} errors)")
        for error in errors:
            print(f"- {error}")
        return 1

    print("PHASE 0 AUTOMATED CHECK: PASS")

    if args.gate:
        print("PHASE 0 ENGINEERING GATE: PASS")

    if args.release_gate:
        blockers = human_gate_blockers()
        if blockers:
            print(f"RELEASE HUMAN GATE: BLOCKED ({len(blockers)} pending items)")
            for blocker in blockers:
                print(f"- {blocker}")
            return 2
        print("RELEASE HUMAN GATE: PASS")

    return 0

if __name__ == "__main__":
    sys.exit(main())
