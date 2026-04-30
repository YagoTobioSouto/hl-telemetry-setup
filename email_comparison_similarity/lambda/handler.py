"""TF-IDF + ROUGE-L email similarity Lambda handler.

Stateless microservice. Given a candidate email draft and a list of
reference emails, returns per-reference similarity scores *plus* a
summary that tells the caller which reference is closest, how confident
that assessment is, and — for the closest match only — a deterministic
``evidence`` block that explains *why* the score came out the way it did.

Purely informational — consumed downstream by a Similarity Evaluator
agent that turns this structured output into a one-sentence, copywriter-
facing explanation. The Lambda itself makes no model calls and produces
no prose.

--------------------------------------------------------------------
Event payload
--------------------------------------------------------------------
    {
        "candidate": "full text of the drafted email",
        "references": [
            {"email_id": "id_1", "text": "full text of source email 1"},
            {"email_id": "id_2", "text": "full text of source email 2"},
            ...
        ]
    }

--------------------------------------------------------------------
Response body
--------------------------------------------------------------------
    {
        "references": [
            {
                "email_id":      "id_1",
                "rank":          1,            # 1 = closest match in batch
                "similarity":    0.5321,       # headline = tfidf_cosine
                "rouge_l":       0.6381,       # secondary: surface overlap
                "tfidf_cosine":  0.5321,       # primary: weighted vocabulary
                "relative_share":0.8723        # this ref's share of total
                                                #   similarity across the batch
            },
            ...
        ],
        "candidate_summary": {
            "closest_match":       "id_1",
            "closest_similarity":  0.5321,
            "confidence":          "high",     # high | medium | low
            "verdict":             "near_duplicate",
                                                # distinct | related | near_duplicate
            "evidence": {
                "shared_terms": [
                    {"term": "q3 pricing", "weight": 0.41},
                    {"term": "rate card",  "weight": 0.28},
                    ...
                ],
                "longest_shared_phrase": {
                    "text":        "q3 pricing discussion from last thursday",
                    "token_count": 7
                },
                "candidate_unique_term_ratio": 0.22
            }
        }
    }

--------------------------------------------------------------------
Why this shape
--------------------------------------------------------------------
* TF-IDF cosine is the headline because it weights rare shared words
  above common English boilerplate, so topical overlap stands out far
  more cleanly than with ROUGE.
* ROUGE-L is kept alongside as a secondary component — anyone auditing
  a score can see both "shared vocabulary" (tfidf) and "shared phrasing"
  (rouge) independently.
* ``relative_share`` normalises each reference's score against the
  other references in the same request. A raw 0.53 is hard to interpret
  in isolation; an 87% share of the total similarity mass immediately
  says "this one dominates".
* ``candidate_summary.confidence`` derives from the gap between the
  top-ranked and second-ranked references. A wide gap = genuine match;
  a narrow gap = noisy tie and the "closest" ref isn't meaningful.
* ``candidate_summary.verdict`` is the absolute-threshold answer to
  "how close is the closest match?". ``confidence`` and ``verdict``
  answer different questions and can legitimately disagree — e.g.
  ``similarity=0.04, confidence=low, verdict=distinct`` means "the
  top pick is not a clear winner and also not close to the candidate",
  which is the correct interpretation when all references are noise.
* ``candidate_summary.evidence`` makes the score auditable without an
  LLM. Every field is extracted from the same TF-IDF matrix and token
  streams that produced the headline score, so it can never disagree
  with the numbers it explains. The downstream evaluator agent uses
  this block — plus both email texts — to compose the user-facing
  explanation. When the agent is unavailable, the UI can still render
  ``verdict`` + ``shared_terms`` directly.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

from rouge_score import rouge_scorer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# --- Module-level init (runs once per execution environment) ---
_LOG = logging.getLogger()
_LOG.setLevel(logging.INFO)

_ROUGE = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)

# Confidence thresholds for candidate_summary.confidence.
# Measured as (top_similarity - second_similarity).
_CONFIDENCE_HIGH_GAP = 0.15
_CONFIDENCE_MEDIUM_GAP = 0.05

# Verdict thresholds for candidate_summary.verdict.
# Measured against the absolute top-ranked similarity.
_VERDICT_NEAR_DUPLICATE = 0.50
_VERDICT_RELATED = 0.15

# How many shared terms to surface in evidence.shared_terms.
# Five is enough for the evaluator agent to pick a representative
# phrase without drowning it in noise.
_EVIDENCE_TOP_TERMS = 5

# Token pattern matching sklearn's default TfidfVectorizer token_pattern.
# Reused so _longest_common_phrase operates on the same token stream the
# TF-IDF matrix was built from — any shared phrase we surface will
# literally be one of the things driving the cosine score.
_TOKEN_RE = re.compile(r"(?u)\b\w\w+\b")


# --- Small, single-purpose helpers -------------------------------------

def _extract_body(event: dict[str, Any]) -> dict[str, Any]:
    """Support both direct invocation and Function-URL / API-Gateway envelopes."""
    if isinstance(event.get("body"), str):
        return json.loads(event["body"])
    return event


def _validate_references(references: Any) -> list[dict[str, str]]:
    """Filter out malformed reference entries, logging each skip."""
    if not isinstance(references, list):
        return []

    valid: list[dict[str, str]] = []
    for ref in references:
        if not isinstance(ref, dict):
            _LOG.warning("Skipping non-dict reference: %r", ref)
            continue
        email_id = ref.get("email_id")
        text = ref.get("text")
        if (
            not isinstance(email_id, str)
            or not isinstance(text, str)
            or not text.strip()
        ):
            _LOG.warning("Skipping malformed reference: %r", ref)
            continue
        valid.append({"email_id": email_id, "text": text})
    return valid


def _tfidf_cosine(
    candidate: str,
    references: list[str],
) -> tuple[list[float], TfidfVectorizer, Any]:
    """Score the candidate against each reference in TF-IDF space.

    Returns the per-reference cosine similarities *plus* the fitted
    vectoriser and the full TF-IDF matrix. The matrix and vectoriser
    are reused by the evidence helpers below so we never re-tokenise
    or re-fit — everything interpretability-related is a read-only
    view of the same vector space that produced the headline score.

    The vectoriser is fit on the candidate + all references together so
    IDF weights are derived from the actual corpus under comparison,
    which is the standard per-request approach for small reference sets.
    Unigrams + bigrams catches short shared phrases without exploding
    dimensionality on the small texts we handle.
    """
    corpus = [candidate, *references]
    vectoriser = TfidfVectorizer(
        lowercase=True,
        stop_words="english",
        ngram_range=(1, 2),
    )
    matrix = vectoriser.fit_transform(corpus)
    sims = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
    return [float(s) for s in sims], vectoriser, matrix


def _rouge_l(candidate: str, reference: str) -> float:
    """ROUGE-L F-measure between candidate and a single reference."""
    return float(_ROUGE.score(candidate, reference)["rougeL"].fmeasure)


def _relative_shares(scores: list[float]) -> list[float]:
    """Each score's share of the total similarity mass.

    Returns equal shares if all scores are zero (defensive — avoids
    division by zero when no reference has any vocabulary overlap with
    the candidate, e.g. if the candidate is entirely stopwords).
    """
    total = sum(scores)
    if total <= 0:
        n = len(scores) or 1
        return [1.0 / n] * len(scores)
    return [s / total for s in scores]


def _confidence(top: float, second: float | None) -> str:
    """Classify how decisive the top match is, based on the gap to #2.

    If there is only one reference there is no meaningful gap, so we
    report ``high`` when it has any non-trivial score and ``low``
    otherwise — the caller can treat single-reference batches as a
    special case if they want to.
    """
    if second is None:
        return "high" if top > _CONFIDENCE_MEDIUM_GAP else "low"
    gap = top - second
    if gap >= _CONFIDENCE_HIGH_GAP:
        return "high"
    if gap >= _CONFIDENCE_MEDIUM_GAP:
        return "medium"
    return "low"


def _verdict(top: float) -> str:
    """Classify the absolute similarity of the closest match.

    Answers "how close is it?", not "how decisive is the ranking?".
    Copywriter-facing one-word headline that the UI can render directly
    without waiting for a downstream LLM explanation.
    """
    if top >= _VERDICT_NEAR_DUPLICATE:
        return "near_duplicate"
    if top >= _VERDICT_RELATED:
        return "related"
    return "distinct"


def _top_shared_terms(
    vectoriser: TfidfVectorizer,
    matrix: Any,
    cand_row: int,
    ref_row: int,
    top_n: int = _EVIDENCE_TOP_TERMS,
) -> list[dict[str, Any]]:
    """Top-N TF-IDF terms (unigrams + bigrams) present in both texts.

    These are literally the vocabulary driving the cosine score: each
    term's contribution to the dot product is ``cand_weight * ref_weight``,
    so ranking by that product surfaces the terms most responsible for
    the similarity.

    We report the geometric mean ``sqrt(cand_w * ref_w)`` as the term's
    weight rather than the raw product — the geometric mean stays in
    the same [0, 1] range as the inputs, which makes the numbers
    comparable to the headline score. A term with weight 0.4 "feels"
    like a 0.4-level contribution, which matches intuition.
    """
    cand_vec = matrix[cand_row].toarray().flatten()
    ref_vec = matrix[ref_row].toarray().flatten()

    # Element-wise presence in both vectors. Zero in either = not shared.
    shared_mask = (cand_vec > 0) & (ref_vec > 0)
    if not shared_mask.any():
        return []

    feature_names = vectoriser.get_feature_names_out()
    shared_indices = shared_mask.nonzero()[0]

    scored = [
        (
            feature_names[i],
            math.sqrt(float(cand_vec[i]) * float(ref_vec[i])),
        )
        for i in shared_indices
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    return [
        {"term": term, "weight": round(weight, 4)}
        for term, weight in scored[:top_n]
    ]


def _candidate_unique_term_ratio(
    matrix: Any,
    cand_row: int,
    ref_row: int,
) -> float:
    """Fraction of the candidate's vocabulary that is NOT in the closest match.

    Simple originality proxy. ``0.0`` means every term the candidate
    uses also appears in the reference (heavy copy); ``1.0`` means no
    overlap at all. Computed over the TF-IDF feature space, so stopwords
    are already filtered out.

    Defensive on the empty-candidate edge case (returns 1.0 — "fully
    original", which is the safest interpretation if the candidate has
    no content to compare).
    """
    cand_vec = matrix[cand_row].toarray().flatten()
    ref_vec = matrix[ref_row].toarray().flatten()

    cand_terms = (cand_vec > 0)
    shared_terms = cand_terms & (ref_vec > 0)

    total = int(cand_terms.sum())
    if total == 0:
        return 1.0
    shared = int(shared_terms.sum())
    return round((total - shared) / total, 4)


def _longest_shared_phrase(
    candidate: str,
    reference: str,
) -> dict[str, Any]:
    """Longest contiguous run of matching tokens between candidate and reference.

    ROUGE-L's own LCS is non-contiguous ("a b c" and "a x b x c" share
    a length-3 subsequence), which is fine for scoring but terrible
    evidence — no copywriter looking at the output would recognise
    "a b c" as overlap with "a x b x c". So we compute the *contiguous*
    longest match directly with a small DP, walking token streams
    produced by the same regex sklearn uses to build the TF-IDF matrix.
    That guarantees the phrase we return is literally part of what
    drove the cosine score.

    Returns the phrase text and its token count so the downstream agent
    can decide whether the match is damning (long) or trivial (short).
    In the copywriter use case most emails share topic vocabulary more
    than verbatim phrasing, so a 2-token shared phrase is noise; a
    7+ token match is a strong signal of paraphrase/reuse.

    Ties (multiple runs of equal length) resolve to the first match in
    the candidate — stable and good enough for evidence purposes.
    """
    cand_tokens = _TOKEN_RE.findall(candidate.lower())
    ref_tokens = _TOKEN_RE.findall(reference.lower())

    if not cand_tokens or not ref_tokens:
        return {"text": "", "token_count": 0}

    # Classic O(n*m) contiguous-LCS DP. Emails are short (hundreds of
    # tokens at most), so the matrix is tiny and cache-friendly.
    m, n = len(cand_tokens), len(ref_tokens)
    # Row-rolling to keep memory at O(n) rather than O(n*m).
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    best_len = 0
    best_end_in_cand = 0  # exclusive index in cand_tokens

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if cand_tokens[i - 1] == ref_tokens[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > best_len:
                    best_len = curr[j]
                    best_end_in_cand = i
            else:
                curr[j] = 0
        prev, curr = curr, [0] * (n + 1)

    if best_len == 0:
        return {"text": "", "token_count": 0}

    phrase_tokens = cand_tokens[best_end_in_cand - best_len:best_end_in_cand]
    return {
        "text": " ".join(phrase_tokens),
        "token_count": best_len,
    }


def _build_reference_rows(
    valid_refs: list[dict[str, str]],
    tfidf_scores: list[float],
    rouge_scores: list[float],
    shares: list[float],
) -> list[dict[str, Any]]:
    """Combine the per-reference metrics into the response row shape, ranked.

    Returns rows in ranked order (rank 1 first) and, critically, also
    threads the original-corpus index through each row so the caller can
    find the rank-1 reference's row in the TF-IDF matrix for evidence
    extraction. The ``_corpus_index`` key is stripped before serialisation.
    """
    rows = [
        {
            "email_id": ref["email_id"],
            "similarity": round(tfidf, 4),
            "rouge_l": round(rouge, 4),
            "tfidf_cosine": round(tfidf, 4),
            "relative_share": round(share, 4),
            # +1 because corpus row 0 is the candidate
            "_corpus_index": i + 1,
        }
        for i, (ref, tfidf, rouge, share) in enumerate(
            zip(valid_refs, tfidf_scores, rouge_scores, shares)
        )
    ]
    # Sort descending by similarity, then assign rank 1..N.
    rows.sort(key=lambda r: r["similarity"], reverse=True)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _serialise_reference_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip internal bookkeeping and re-order keys for the response."""
    return [
        {
            "email_id": r["email_id"],
            "rank": r["rank"],
            "similarity": r["similarity"],
            "rouge_l": r["rouge_l"],
            "tfidf_cosine": r["tfidf_cosine"],
            "relative_share": r["relative_share"],
        }
        for r in rows
    ]


def _build_summary(
    ranked_rows: list[dict[str, Any]],
    valid_refs: list[dict[str, str]],
    candidate: str,
    vectoriser: TfidfVectorizer | None,
    matrix: Any,
) -> dict[str, Any]:
    """Produce the candidate_summary block, including verdict + evidence.

    Evidence is scoped to the rank-1 reference only. The lower-ranked
    references are intentionally not given evidence blocks — they would
    be noise for the copywriter use case (who only cares about the
    closest match) and would roughly triple the response size for no
    added signal.
    """
    if not ranked_rows or vectoriser is None or matrix is None:
        return {
            "closest_match": None,
            "closest_similarity": 0.0,
            "confidence": "low",
            "verdict": "distinct",
            "evidence": None,
        }

    top = ranked_rows[0]
    second_sim = ranked_rows[1]["similarity"] if len(ranked_rows) > 1 else None

    # Find the original reference text for the rank-1 row so we can run
    # the token-level LCS against it. The `_corpus_index` we stamped in
    # earlier points into `[candidate, *refs]`, so subtract 1 to get
    # the index into valid_refs.
    top_ref = valid_refs[top["_corpus_index"] - 1]

    evidence = {
        "shared_terms": _top_shared_terms(
            vectoriser=vectoriser,
            matrix=matrix,
            cand_row=0,
            ref_row=top["_corpus_index"],
        ),
        "longest_shared_phrase": _longest_shared_phrase(
            candidate=candidate,
            reference=top_ref["text"],
        ),
        "candidate_unique_term_ratio": _candidate_unique_term_ratio(
            matrix=matrix,
            cand_row=0,
            ref_row=top["_corpus_index"],
        ),
    }

    return {
        "closest_match": top["email_id"],
        "closest_similarity": top["similarity"],
        "confidence": _confidence(top["similarity"], second_sim),
        "verdict": _verdict(top["similarity"]),
        "evidence": evidence,
    }


# --- Lambda entry point ------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        body = _extract_body(event)
    except (json.JSONDecodeError, TypeError) as exc:
        _LOG.error("Failed to parse event body: %s", exc)
        return {"statusCode": 400, "body": json.dumps({"error": "invalid json"})}

    candidate = body.get("candidate")

    if not isinstance(candidate, str) or not candidate.strip():
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "candidate must be a non-empty string"}),
        }

    if not isinstance(body.get("references"), list) or not body["references"]:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "references must be a non-empty list"}),
        }

    valid_refs = _validate_references(body["references"])
    if not valid_refs:
        # All references were malformed but we were given a list — empty-but-OK.
        return {
            "statusCode": 200,
            "body": json.dumps({
                "references": [],
                "candidate_summary": {
                    "closest_match": None,
                    "closest_similarity": 0.0,
                    "confidence": "low",
                    "verdict": "distinct",
                    "evidence": None,
                },
            }),
        }

    ref_texts = [r["text"] for r in valid_refs]

    try:
        tfidf_scores, vectoriser, matrix = _tfidf_cosine(candidate, ref_texts)
    except Exception as exc:
        _LOG.exception("TF-IDF scoring failed: %s", exc)
        return {"statusCode": 500, "body": json.dumps({"error": "scoring failed"})}

    rouge_scores = [_rouge_l(candidate, t) for t in ref_texts]
    shares = _relative_shares(tfidf_scores)

    ranked_rows = _build_reference_rows(valid_refs, tfidf_scores, rouge_scores, shares)
    summary = _build_summary(
        ranked_rows=ranked_rows,
        valid_refs=valid_refs,
        candidate=candidate,
        vectoriser=vectoriser,
        matrix=matrix,
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "references": _serialise_reference_rows(ranked_rows),
            "candidate_summary": summary,
        }),
    }
