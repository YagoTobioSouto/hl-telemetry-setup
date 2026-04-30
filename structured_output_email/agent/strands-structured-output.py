"""
strands-structured-output.py

Proof of concept: use Strands Agents to generate a structured EmailDraft
matching the Copycraft email_blocks_schema.

Methodology:
    1. The LLM does NOT emit raw HTML for the email.
    2. It emits a structured tree of AuthoredBlocks (hero, heading, paragraph,
       cta, ...) conforming to EmailDraft.
    3. The renderer (downstream, not in this file) turns those blocks + the
       brand config into final HTML at compile time.
    4. Inline Liquid (salutation, merge tags) lives inside paragraph HTML as
       <span data-liquid-tag="..."> nodes — sanitised TipTap output.

Key Strands calls used (from Strands docs):
    - Agent(..., structured_output_model=EmailDraft)   # agent-level default
    - agent(prompt, structured_output_model=EmailDraft) # per-invocation
    - result.structured_output -> validated EmailDraft instance

Pipeline fit (from design.md):
    In your 3-agent pipeline (Copywriter -> Editor -> PR Review), structured
    output is most valuable on the FINAL PASS — i.e. after convergence or at
    max_passes — when you serialise the agreed draft into the canonical
    EmailDraft envelope that the editor/renderer consumes.

    Interior agent steps can still exchange free-form text (streamed via
    AG-UI), and only the last step binds the EmailDraft schema. That avoids
    the overhead of re-validating a large nested schema on every hop and
    keeps the streaming UX clean.

Prerequisites:
    pip install strands-agents pydantic
    # Bedrock credentials configured via the usual AWS_* env vars.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import botocore.config
from strands import Agent
from strands.models import BedrockModel

# Import the canonical schema (sibling file)
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "schema"))
from email_blocks_schema import EmailDraft

# ---------------------------------------------------------------------------
# Bedrock model config
# ---------------------------------------------------------------------------

BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "nvidia.nemotron-super-3-120b",
    # "zai.glm-5",  # Z.AI GLM 5 — also available in eu-west-2
)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "eu-west-2")


def _build_model() -> BedrockModel:
    return BedrockModel(
        model_id=BEDROCK_MODEL_ID,
        region_name=BEDROCK_REGION,
        streaming=False,
        boto_client_config=botocore.config.Config(
            read_timeout=300,
            retries={"max_attempts": 1},
        ),
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are Copycraft, Hargreaves Lansdown's email copywriter.

You author emails as a STRUCTURED TREE OF BLOCKS, not as raw HTML. The
renderer adds brand chrome (logo, footer, colours, spacing) at compile time.

Authoring rules:
  - Choose the right block type for each section: hero, heading, paragraph,
    cta, feature_row, column_layout, disclaimer, divider, spacer,
    liquid_macro.
  - Put narrative copy in `paragraph` blocks. Put rich text in `html` as
    sanitised TipTap output — <p>, <strong>, <em>, <a>, <ul>, <ol>, <li>.
  - For salutations and merge tags, embed Liquid as:
        <span data-liquid-tag="<tag_id>">[Label]</span>
    where tag_id refers to a LiquidTag in the brand's registry.
  - For HL's central regulatory footer, emit a `liquid_macro` block with
    expression `{{${email_footer}}}` and preview_label `[HL footer]`.
  - Add campaign-specific regulatory body text as a `disclaimer` block.
  - Assign every block a unique, human-readable id (e.g. `blk_hero_01`,
    `blk_para_deadline`).
  - Keep subject <= 150 chars and pre_header <= 200 chars.
  - Populate `annotations` with any concerns you have about your own draft
    (source='editor', severity=advisory|med|high). Leave empty if none.

Metadata rules:
  - `pass` = 1 for the first structured emit.
  - `converged` = True only if you believe no further revisions are needed.
  - `word_count` is the total word count across all block prose.

Never output free-form text. Only emit the structured EmailDraft object.
"""


# ---------------------------------------------------------------------------
# Example brief
# ---------------------------------------------------------------------------

BRIEF = """\
Generate an ISA deadline reminder email.

draft_id: draft_test_01
campaign_id: campaign_isa_deadline_2026
brand_id: hargreaves-lansdown
email_type: isa-deadline
persona: ellie

Hero template available: hl_isa_deadline_banner_v1

Key facts:
  - Tax year ends 23:59 on 5 April.
  - The recipient has used £16,800 of their £20,000 ISA allowance.
  - Unused allowance does not carry forward.
  - CTA: top up their Stocks & Shares ISA at
    https://www.hl.co.uk/investment-services/stocks-and-shares-isa/top-up

Tone: action-oriented but not alarmist. Warm and professional.

Must include:
  - Personalised salutation using the `hl_salutation` Liquid tag.
  - A standard HL investment risk disclaimer (not personal advice,
    tax rules can change, value of investments can fall).
  - The HL central footer as a liquid_macro block.
"""


# ---------------------------------------------------------------------------
# Runner — sync
# ---------------------------------------------------------------------------


def run_sync() -> EmailDraft:
    """One-shot structured output."""
    print(f"🚀 model={BEDROCK_MODEL_ID}  region={BEDROCK_REGION}")

    agent = Agent(
        model=_build_model(),
        system_prompt=SYSTEM_PROMPT,
        structured_output_model=EmailDraft,
    )

    print("📡 Calling agent...")
    t0 = time.time()
    result = agent(BRIEF)
    print(f"⏱️  Completed in {time.time() - t0:.1f}s")

    return result.structured_output


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _write_output(draft: EmailDraft, out_path: Path) -> None:
    """Serialise using by_alias=True because DraftMetadata aliases pass_ -> pass."""
    out_path.write_text(
        json.dumps(draft.model_dump(by_alias=True, mode="json"), indent=2),
        encoding="utf-8",
    )
    print(f"\n✅ Wrote validated EmailDraft to {out_path}")
    print(f"   subject     : {draft.subject}")
    print(f"   pre_header  : {draft.pre_header}")
    print(
        f"   blocks      : {len(draft.blocks)} ({', '.join(b.type for b in draft.blocks)})"
    )
    print(f"   annotations : {len(draft.annotations)}")
    print(
        f"   pass        : {draft.metadata.pass_}  converged={draft.metadata.converged}"
    )


if __name__ == "__main__":
    out = Path(__file__).parent / "email-draft-generated.json"
    draft = run_sync()
    _write_output(draft, out)
