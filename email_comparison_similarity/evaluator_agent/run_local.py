#!/usr/bin/env python3
"""Local runner for the Similarity Evaluator agent.

Reads a draft + reference emails from a fixtures directory, runs the
evaluator agent, and prints the resulting ``SimilarityEvaluation`` as
JSON to stdout. Nothing else — the output is the contract the
Serializer downstream will consume, so the runner prints it verbatim.

Three modes:

    python run_local.py
        Default: mock LLM, in-process Lambda. Offline, no AWS creds,
        ~20 ms per run after the first.

    python run_local.py --live-llm
        Real Bedrock call. Requires AWS creds with bedrock:InvokeModel.

    python run_local.py --live-llm --live-lambda
        Full production shape. Requires creds for Bedrock and
        lambda:InvokeFunction on the deployed handler.

Pick a fixture set with --fixtures (default: ../fixtures):

    python run_local.py --fixtures /path/to/your/fixtures
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Send all library logging to stderr so stdout carries only JSON.
# Without this, absl / botocore / strands INFO lines pollute the
# Serializer-facing output.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

from agent import evaluate_similarity  # noqa: E402  (import after logging config)

HERE = Path(__file__).parent
DEFAULT_FIXTURES = HERE.parent / "fixtures"


def load_from_fixtures(fixtures_dir: Path) -> tuple[str, list[dict]]:
    """Assemble (draft, sources) from a fixtures directory.

    Layout:
        <dir>/candidate.txt
        <dir>/references/<email_id>.txt
    """
    candidate_path = fixtures_dir / "candidate.txt"
    if not candidate_path.is_file():
        raise FileNotFoundError(f"Missing candidate file: {candidate_path}")

    refs_dir = fixtures_dir / "references"
    if not refs_dir.is_dir():
        raise FileNotFoundError(f"Missing references directory: {refs_dir}")

    ref_files = sorted(refs_dir.glob("*.txt"))
    if not ref_files:
        raise FileNotFoundError(f"No .txt reference files in {refs_dir}")

    draft = candidate_path.read_text(encoding="utf-8")
    sources = [
        {"email_id": f.stem, "text": f.read_text(encoding="utf-8")}
        for f in ref_files
    ]
    return draft, sources


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="Fixtures directory (default: ../fixtures).",
    )
    p.add_argument(
        "--live-llm",
        action="store_true",
        help="Call Bedrock for real instead of the deterministic mock templater.",
    )
    p.add_argument(
        "--live-lambda",
        action="store_true",
        help="Invoke the deployed Lambda instead of importing the handler in-process.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    draft, sources = load_from_fixtures(args.fixtures)
    evaluation = evaluate_similarity(
        draft_email=draft,
        source_emails=sources,
        llm="live" if args.live_llm else "mock",
        lambda_mode="live" if args.live_lambda else "local",
    )

    # The agent's output contract is the SimilarityEvaluation JSON.
    # Print it verbatim — any framing we add here is noise the
    # Serializer downstream would have to strip back out.
    json.dump(evaluation.model_dump(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
