"""Pydantic schemas for the Similarity Evaluator agent.

These define the contract the agent exposes to the Serializer agent
downstream. The key design rule:

    The LLM writes ONLY ``SimilarityEvaluation.explanation``.
    Every other field in ``SimilarityEvaluation`` is a passthrough
    from the scoring Lambda's deterministic output.

This is enforced structurally, not by prompt discipline: the LLM is
asked to produce a single-field ``ExplanationOnly`` object, and the
agent wrapper merges that string with the Lambda's response into the
full ``SimilarityEvaluation``. The LLM never sees the verdict, score,
or evidence fields as writable slots, so it cannot accidentally
overwrite them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# --- Lambda response passthroughs --------------------------------------
#
# These mirror the shape of the similarity Lambda's response body.
# We redefine them as Pydantic models (rather than accepting raw dicts)
# so the Serializer downstream gets a typed, validated contract rather
# than having to trust our dict structure by convention.

class Reference(BaseModel):
    """One reference email's scores, as returned by the Lambda.

    Per-reference evidence (shared terms, phrase, uniqueness) is NOT
    included here — by design, evidence is scoped to the closest
    match only (see PLAN.md §4.7). Scoring every reference against
    the candidate is cheap; generating evidence for every reference
    would triple the response size for what is, in the end, noise for
    the copywriter looking at a 0.01-similarity match.
    """

    email_id: str = Field(description="Passthrough from the request.")
    rank: int = Field(
        ge=1,
        description="1 = closest match, N = furthest, within this batch.",
    )
    similarity: float = Field(
        ge=0.0,
        le=1.0,
        description="Headline similarity score (= tfidf_cosine).",
    )
    rouge_l: float = Field(
        ge=0.0,
        le=1.0,
        description="ROUGE-L F-measure — surface phrase overlap.",
    )
    tfidf_cosine: float = Field(
        ge=0.0,
        le=1.0,
        description="Cosine similarity in TF-IDF vector space.",
    )
    relative_share: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "This reference's share of total similarity mass across "
            "the batch. The references' relative_share values sum to 1.0."
        ),
    )


class SharedTerm(BaseModel):
    """One TF-IDF term present in both candidate and closest match."""

    term: str = Field(description="The term (unigram or bigram).")
    weight: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Geometric mean of the term's TF-IDF weights in the "
            "candidate and the closest reference. Higher = stronger "
            "contribution to the cosine score."
        ),
    )


class LongestSharedPhrase(BaseModel):
    """The longest contiguous run of matching tokens."""

    text: str = Field(
        description="The shared phrase, lowercased and whitespace-normalised."
    )
    token_count: int = Field(
        ge=0,
        description=(
            "Number of tokens in the phrase. Short phrases (≤4) are "
            "typically filler; long phrases (≥10) are strong evidence "
            "of verbatim reuse."
        ),
    )


class Evidence(BaseModel):
    """Deterministic 'why' for the closest-match ranking."""

    shared_terms: list[SharedTerm] = Field(
        description="Top-N TF-IDF terms shared with the closest match."
    )
    longest_shared_phrase: LongestSharedPhrase
    candidate_unique_term_ratio: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of the candidate's vocabulary NOT present in the "
            "closest match. 1.0 = fully original, 0.0 = heavy copy."
        ),
    )


# --- LLM-only output --------------------------------------------------
#
# This is what the LLM is asked to produce. Single field, string,
# constrained by the system prompt to a one-sentence explanation.
# The tight schema + temperature=0 + max_tokens=128 is what keeps
# the LLM inside its lane.

class ExplanationOnly(BaseModel):
    """Just the one-sentence explanation. The LLM's entire remit."""

    explanation: str = Field(
        description=(
            "A single sentence (≤25 words) explaining how the drafted "
            "email relates to the closest source email. Must respect "
            "the verdict from the scoring tool."
        ),
    )


# --- Full evaluator output --------------------------------------------
#
# The agent wrapper constructs this by merging ExplanationOnly (from
# the LLM) with the Lambda's response. This is what the Serializer
# consumes.

class SimilarityEvaluation(BaseModel):
    """Complete similarity evaluation for a drafted email.

    ``references``, ``verdict``, ``confidence`` and ``evidence`` are
    passthroughs from the scoring Lambda — deterministic, auditable,
    not LLM-generated.

    ``explanation`` is the LLM-generated one-sentence copywriter-facing
    read of the evidence.

    The closest match is ``references[0]`` (references are returned
    ranked, rank=1 first). We intentionally don't duplicate its
    email_id/similarity at the top level — one source of truth.
    """

    references: list[Reference] = Field(
        description=(
            "All reference emails scored, ordered by rank. "
            "references[0] is the closest match. Each reference carries "
            "its own scores; evidence is only computed for the closest "
            "match (see `evidence` below)."
        ),
    )
    verdict: Literal["distinct", "related", "near_duplicate"] = Field(
        description=(
            "Absolute-threshold classification of the closest match. "
            "Answers 'how close is it?'."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "How decisive the top-ranked match is vs the runner-up. "
            "Answers 'how clearly does one reference win?'. Can "
            "legitimately disagree with verdict (e.g. verdict=distinct + "
            "confidence=low when all references are noise)."
        ),
    )
    evidence: Evidence | None = Field(
        description=(
            "Deterministic evidence for the closest match only "
            "(references[0]). None when there were no valid references."
        ),
    )
    explanation: str = Field(
        description=(
            "One-sentence human-readable description of the "
            "relationship between draft and closest source. "
            "Informational only, no recommendations."
        ),
    )
