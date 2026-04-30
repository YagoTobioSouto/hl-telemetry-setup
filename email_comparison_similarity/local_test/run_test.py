#!/usr/bin/env python3
"""Local test runner for the email similarity handler (TF-IDF + ROUGE-L).

Assembles a handler payload from per-email text files, so that you can
edit realistic multi-line emails naturally (paragraphs, signatures, line
breaks) without wrestling with JSON escape sequences.

Layout:
    local_test/fixtures/
        candidate.txt                Draft email (stand-in for Writer output)
        references/
            <email_id>.txt           One file per KB source email;
                                      filename (sans .txt) = email_id
            ...

At runtime the files are read, packed into the same JSON payload shape
the Lambda handler receives in production, and passed directly to
lambda_handler(). The handler contract does not change.

Usage:
    python run_test.py                               # default: fixtures/
    python run_test.py --fixtures path/to/dir        # custom fixtures dir
    python run_test.py --payload payload.json        # raw JSON override
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Add the lambda/ directory to the path so we can import handler directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from handler import lambda_handler  # noqa: E402

HERE = Path(__file__).parent
DEFAULT_FIXTURES = HERE.parent / "fixtures"


def load_from_fixtures(fixtures_dir: Path) -> dict:
    """Build the handler payload from a fixtures directory.

    The candidate comes from ``candidate.txt`` and each file in
    ``references/`` becomes one reference entry where the filename
    (without the ``.txt`` suffix) is used as the ``email_id``.
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

    return {
        "candidate": candidate_path.read_text(encoding="utf-8"),
        "references": [
            {"email_id": f.stem, "text": f.read_text(encoding="utf-8")}
            for f in ref_files
        ],
    }


def load_from_json(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURES,
        help="Directory containing candidate.txt and references/*.txt (default: ../fixtures)",
    )
    group.add_argument(
        "--payload",
        type=Path,
        help="Path to a raw JSON payload file (bypasses fixtures loading)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    event = load_from_json(args.payload) if args.payload else load_from_fixtures(args.fixtures)

    candidate_preview = event["candidate"].strip().splitlines()[0][:80]
    print(f"Candidate:  {candidate_preview}...")
    print(f"References: {len(event['references'])} emails "
          f"[{', '.join(r['email_id'] for r in event['references'])}]")
    print("---")
    print("Running handler (TF-IDF + ROUGE-L, should be sub-second)...\n")

    start = time.perf_counter()
    response = lambda_handler(event, None)
    elapsed = time.perf_counter() - start

    print(f"Status: {response['statusCode']}")
    print(f"Time:   {elapsed:.2f}s")
    print(f"\nResponse body:\n{json.dumps(json.loads(response['body']), indent=2)}")


if __name__ == "__main__":
    main()
