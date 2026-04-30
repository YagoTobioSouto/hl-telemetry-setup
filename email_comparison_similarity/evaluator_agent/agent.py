"""Similarity Evaluator agent — main entry point.

Architecture:

    evaluate_similarity(draft, sources, ...)
        │
        ├── invoke_similarity_lambda(...)  ← plain function call
        │       └── TF-IDF + ROUGE-L + verdict + evidence
        │                    │
        │                    ▼
        │   (scoring dict passed directly into the prompt)
        │                    │
        │                    ▼
        ├── Agent(Bedrock, structured_output=ExplanationOnly)
        │       └── Writes ONE sentence from the dict
        │
        └── merge(LLM-written explanation, Lambda's deterministic fields)
                    │
                    └──► SimilarityEvaluation

The LLM does NOT have tools. We call the Lambda in code and hand its
full JSON output to the LLM as part of the user prompt. This is
simpler, faster, and avoids tool-call loops that some models
(including Nemotron 3 Super) fall into when combining tools with
structured output.

Three execution modes, selected by two independent flags:

* ``llm="mock"``: deterministic templated explanation from verdict +
  evidence. No Bedrock call.
* ``llm="live"``: one Bedrock invocation for the sentence. No tools.
* ``lambda_mode="local"`` vs ``"live"``: in-process handler import vs
  deployed Lambda via boto3. Orthogonal to the LLM mode.

Typical combinations:

* ``llm="mock" + lambda_mode="local"``: offline iteration, no creds.
* ``llm="live" + lambda_mode="local"``: prompt iteration with real LLM
  but no Lambda deployment needed.
* ``llm="live" + lambda_mode="live"``: production shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from strands import Agent
from strands.models import BedrockModel

from config import (
    EVALUATOR_MAX_TOKENS,
    EVALUATOR_MODEL_ID,
    EVALUATOR_REGION,
    EVALUATOR_SYSTEM_PROMPT,
    EVALUATOR_TEMPERATURE,
)
from schemas import (
    Evidence,
    ExplanationOnly,
    LongestSharedPhrase,
    Reference,
    SharedTerm,
    SimilarityEvaluation,
)
from similarity_client import invoke_similarity_lambda

_LOG = logging.getLogger(__name__)


LlmMode = Literal["mock", "live"]
LambdaMode = Literal["local", "live"]


# --- Public entry point -----------------------------------------------


def evaluate_similarity(
    draft_email: str,
    source_emails: list[dict],
    *,
    llm: LlmMode = "mock",
    lambda_mode: LambdaMode = "local",
) -> SimilarityEvaluation:
    """Evaluate how closely a draft resembles its source emails.

    Args:
        draft_email: The final drafted email, as plain text.
        source_emails: ``[{"email_id": ..., "text": ...}, ...]`` from
            the knowledge base retrieval step.
        llm: ``"live"`` to call Bedrock; ``"mock"`` to template a
            deterministic explanation from the Lambda's evidence
            without any LLM call. Default ``"mock"``.
        lambda_mode: ``"live"`` to call the deployed Lambda; ``"local"``
            to import the handler in-process. Default ``"local"``.

    Returns:
        Fully-populated ``SimilarityEvaluation``. Deterministic fields
        are passed through from the Lambda; ``explanation`` is either
        LLM-generated (live) or templated (mock).
    """
    # Step 1: call the Lambda ONCE, in code. No agent-loop involvement.
    scoring = invoke_similarity_lambda(
        draft_email=draft_email,
        source_emails=source_emails,
        mode=lambda_mode,
    )

    # Step 2: get the explanation (live LLM or templated mock).
    if llm == "mock":
        explanation = _template_explanation(scoring)
    else:
        explanation = _generate_explanation_live(draft_email, scoring)

    # Step 3: assemble the full evaluation.
    return _assemble_evaluation(scoring, explanation)


# --- Live LLM path ----------------------------------------------------


def _generate_explanation_live(
    draft_email: str,
    scoring: dict[str, Any],
) -> str:
    """Ask Bedrock for a one-sentence explanation of the scoring output.

    The agent has NO tools. The scoring dict is handed to the LLM as
    part of the user prompt — the LLM's only job is to read it and
    write one sentence in the ``ExplanationOnly`` schema.
    """
    bedrock_model = BedrockModel(
        model_id=EVALUATOR_MODEL_ID,
        region_name=EVALUATOR_REGION,
        temperature=EVALUATOR_TEMPERATURE,
        max_tokens=EVALUATOR_MAX_TOKENS,
    )

    agent = Agent(
        model=bedrock_model,
        system_prompt=EVALUATOR_SYSTEM_PROMPT,
        # Silence Strands' default stdout printing (the assistant's raw
        # text, "Tool #N" banners, etc.). The caller gets everything it
        # needs from the structured_output return value — anything
        # printed to stdout would collide with the JSON contract this
        # agent is expected to produce.
        callback_handler=None,
    )

    user_prompt = _build_user_prompt(draft_email, scoring)
    result = agent(user_prompt, structured_output_model=ExplanationOnly)
    explanation_obj: ExplanationOnly = result.structured_output
    return explanation_obj.explanation


def _build_user_prompt(draft_email: str, scoring: dict[str, Any]) -> str:
    """Format draft + scoring output for the LLM.

    The scoring dict is inlined as compact JSON rather than re-described
    in prose. The system prompt already explains how to read it.
    """
    summary = scoring.get("candidate_summary", {})

    # Keep the prompt focused on the decision-relevant fields.
    # The full ``references`` list is interesting for auditing but
    # noise for the one-sentence task.
    compact = {
        "verdict": summary.get("verdict"),
        "confidence": summary.get("confidence"),
        "closest_match": summary.get("closest_match"),
        "closest_similarity": summary.get("closest_similarity"),
        "evidence": summary.get("evidence"),
    }

    return (
        "Here is a drafted email and the scoring output produced for it.\n\n"
        f"--- DRAFT ---\n{draft_email.strip()}\n\n"
        f"--- SCORING OUTPUT (JSON) ---\n{json.dumps(compact, indent=2)}\n\n"
        "Write one sentence describing the relationship between the draft "
        "and its closest source email, as specified in your instructions."
    )


# --- Mock LLM path ----------------------------------------------------
#
# Deterministic templated explanation derived from verdict + evidence.
# The templates mirror the three branches the system prompt gives the
# real LLM, so mock output is a reasonable preview of live output.

_BOILERPLATE_TERMS = frozenset({
    "wanted", "share", "wanted share", "happy", "happy to",
    "let", "let know", "know", "thanks", "hi", "hi team",
    "following", "follow up", "following up",
    "15", "14", "16",
})
_MIN_SUBSTANTIVE_PHRASE_TOKENS = 8


def _template_explanation(scoring: dict[str, Any]) -> str:
    """Produce a deterministic one-sentence explanation (informational only).

    Branches on ``verdict`` exactly like the system prompt asks the LLM
    to, and applies the same substance check on shared_terms /
    longest_shared_phrase to decide whether the evidence is meaningful
    or boilerplate. Never advises the user — only describes.
    """
    summary = scoring.get("candidate_summary", {})
    evidence = summary.get("evidence")
    verdict = summary.get("verdict", "distinct")
    closest = summary.get("closest_match")

    if evidence is None or closest is None:
        return "No valid source emails to compare against; no similarity signal available."

    shared_terms = evidence.get("shared_terms", [])
    phrase = evidence.get("longest_shared_phrase", {})
    phrase_text = phrase.get("text", "")
    phrase_tokens = phrase.get("token_count", 0)

    substantive_terms = [
        t["term"] for t in shared_terms
        if t["term"] not in _BOILERPLATE_TERMS
    ]
    phrase_is_substantive = phrase_tokens >= _MIN_SUBSTANTIVE_PHRASE_TOKENS

    if verdict == "distinct":
        # The "blegh" case: low score AND filler evidence. Describe it
        # plainly — no invented relationship, no recommendation.
        sample_filler = _sample_filler(shared_terms, phrase_text)
        return (
            f"Your draft is distinct from all source emails; the only "
            f"overlap is generic email filler ({sample_filler})."
        )

    if verdict == "related":
        if substantive_terms:
            topic = substantive_terms[0]
            return (
                f"Your draft shares the '{topic}' topic with {closest} "
                f"but uses different phrasing and structure."
            )
        return (
            f"Your draft shares some vocabulary with {closest} but no "
            f"substantive phrasing."
        )

    # verdict == "near_duplicate"
    if phrase_is_substantive:
        return (
            f"Your draft contains a {phrase_tokens}-word phrase nearly "
            f"verbatim from {closest} ('{phrase_text}…')."
        )
    if substantive_terms:
        topics = ", ".join(f"'{t}'" for t in substantive_terms[:2])
        return (
            f"Your draft closely mirrors {closest} on key topics ({topics}), "
            f"with high vocabulary overlap across the body."
        )
    return (
        f"Your draft scores as a near-duplicate of {closest} with diffuse "
        f"vocabulary overlap across the body."
    )


def _sample_filler(
    shared_terms: list[dict[str, Any]],
    phrase_text: str,
) -> str:
    """Pick a representative filler example to show the copywriter."""
    pieces: list[str] = []
    if phrase_text:
        pieces.append(f"'{phrase_text}'")
    for term in shared_terms[:2]:
        t = term["term"]
        if t not in _BOILERPLATE_TERMS:
            continue
        pieces.append(f"'{t}'")
        if len(pieces) >= 2:
            break
    if not pieces and shared_terms:
        pieces.append(f"'{shared_terms[0]['term']}'")
    return ", ".join(pieces) if pieces else "none detected"


# --- Assembly ---------------------------------------------------------


def _assemble_evaluation(
    scoring: dict[str, Any],
    explanation: str,
) -> SimilarityEvaluation:
    """Merge the Lambda's deterministic output with the generated explanation."""
    summary = scoring["candidate_summary"]
    evidence_dict = summary.get("evidence")

    references = [
        Reference(
            email_id=r["email_id"],
            rank=r["rank"],
            similarity=r["similarity"],
            rouge_l=r["rouge_l"],
            tfidf_cosine=r["tfidf_cosine"],
            relative_share=r["relative_share"],
        )
        for r in scoring.get("references", [])
    ]

    evidence: Evidence | None = None
    if evidence_dict is not None:
        evidence = Evidence(
            shared_terms=[
                SharedTerm(term=t["term"], weight=t["weight"])
                for t in evidence_dict["shared_terms"]
            ],
            longest_shared_phrase=LongestSharedPhrase(
                text=evidence_dict["longest_shared_phrase"]["text"],
                token_count=evidence_dict["longest_shared_phrase"]["token_count"],
            ),
            candidate_unique_term_ratio=evidence_dict["candidate_unique_term_ratio"],
        )

    return SimilarityEvaluation(
        references=references,
        verdict=summary["verdict"],
        confidence=summary["confidence"],
        evidence=evidence,
        explanation=explanation,
    )
