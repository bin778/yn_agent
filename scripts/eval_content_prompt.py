"""Generate draft outputs for human scoring without using the live Hermes database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from bot.agents.content_editor import draft_from_topic
from bot.llm_client import get_model_identity

CASES_PATH = Path(__file__).resolve().parents[1] / "evals" / "content_prompt" / "cases.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List the evaluation cases without calling a model.")
    parser.add_argument("--case", action="append", metavar="ID", help="Run this case. Repeat to select more.")
    args = parser.parse_args()
    dataset = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in dataset["cases"]}
    if len(cases) != len(dataset["cases"]):
        parser.error("duplicate case IDs")
    if args.list:
        for case in cases.values():
            print(f"{case['id']}  {case['category']}  {case['topic']}")
        return 0
    selected = args.case or list(cases)
    if unknown := set(selected) - cases.keys():
        parser.error(f"unknown case IDs: {', '.join(sorted(unknown))}")

    failures = 0
    with TemporaryDirectory(prefix="hermes-content-eval-") as directory:
        for case_id in selected:
            case = cases[case_id]
            try:
                result = draft_from_topic(
                    case["topic"], work_item_id=case_id, db_path=Path(directory) / f"{case_id}.db",
                )
            except Exception as exc:
                failures += 1
                print(f"{case_id}: {type(exc).__name__}", file=sys.stderr)
                continue
            print(json.dumps({
                **case, "model": get_model_identity(), "output": result["item"].model_dump(),
            }, ensure_ascii=False), flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
