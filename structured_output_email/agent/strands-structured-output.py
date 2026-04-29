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

import asyncio
import json
import os
from pathlib import Path

from strands import Agent
from strands.models import BedrockModel

# Import the canonical schema (sibling file)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "schema"))
from email_blocks_schema import EmailDraft


# ---------------------------------------------------------------------------
# Bedrock model config
# ---------------------------------------------------------------------------
#
# Use the EU cross-region inference profile for Claude Sonnet 4.5.
# Override via env vars if you need to swap models or regions.

BEDROCK_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
)
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "eu-west-1")


def _build_model() -> BedrockModel:
    return BedrockModel(model_id=BEDROCK_MODEL_ID, region_name=BEDROCK_REGION)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

# The prompt deliberately does NOT describe field names — Strands sends the
# JSON Schema to the model as a tool spec. The prompt only describes *intent*
# and the block-based authoring philosophy.
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
    """
    Simplest form: one-shot structured output.

    Use this shape when you want the LLM to emit the full EmailDraft in one
    invocation — e.g. after your pipeline has converged and you're binding
    the final pass to the canonical schema.
    """
    agent = Agent(
        model=_build_model(),
        system_prompt=SYSTEM_PROMPT,
        # Agent-level default: every invocation returns an EmailDraft.
        structured_output_model=EmailDraft,
    )
    result = agent(BRIEF)
    # result.structured_output is a fully validated EmailDraft instance.
    return result.structured_output


# ---------------------------------------------------------------------------
# Runner — streaming (closer to the AG-UI pipeline in design.md)
# ---------------------------------------------------------------------------

async def run_streaming() -> EmailDraft:
    """
    Streaming variant: yields intermediate text events during generation,
    then the final validated EmailDraft.

    In your actual AgentCore Runtime container, you'd bridge these events
    to AG-UI SSE events (text_message_chunk, state_delta, run_finished).
    """
    agent = Agent(
        model=_build_model(),
        system_prompt=SYSTEM_PROMPT,
    )

    if not _SUPPORTS_MODERN_STRUCTURED_OUTPUT:
        # Legacy Strands has no streaming path for structured output.
        # Fall back to the async one-shot call.
        return await agent.structured_output_async(EmailDraft, BRIEF)

    final_draft: EmailDraft | None = None

    async for event in agent.stream_async(
        BRIEF,
        structured_output_model=EmailDraft,
    ):
        # Token-level stream — relay to AG-UI text_message_chunk in production.
        if "data" in event:
            print(event["data"], end="", flush=True)
        # Terminal event — contains the validated structured output.
        elif "result" in event:
            final_draft = event["result"].structured_output

    assert final_draft is not None, "stream ended without a result event"
    return final_draft


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _write_output(draft: EmailDraft, out_path: Path) -> None:
    """Serialise using `by_alias=True` because DraftMetadata aliases pass_ -> pass."""
    out_path.write_text(
        json.dumps(draft.model_dump(by_alias=True, mode="json"), indent=2),
        encoding="utf-8",
    )
    print(f"\n\n✅ Wrote validated EmailDraft to {out_path}")
    print(f"   subject     : {draft.subject}")
    print(f"   pre_header  : {draft.pre_header}")
    print(f"   blocks      : {len(draft.blocks)} ({', '.join(b.type for b in draft.blocks)})")
    print(f"   annotations : {len(draft.annotations)}")
    print(f"   pass        : {draft.metadata.pass_}  converged={draft.metadata.converged}")


if __name__ == "__main__":
    out = Path(__file__).parent / "email-draft-generated.json"

    mode = os.getenv("MODE", "sync")  # set MODE=stream to try streaming
    if mode == "stream":
        draft = asyncio.run(run_streaming())
    else:
        draft = run_sync()

    _write_output(draft, out)
