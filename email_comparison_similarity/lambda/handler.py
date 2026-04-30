"""TF-IDF + ROUGE-L email similarity Lambda handler.

Stateless microservice. Given a candidate email draft and a list of
reference emails, returns per-reference similarity scores *plus* a
summary that tells the caller which reference is closest and how
confident that assessment is.

Purely informational — consumed by the UI to show the user how close
their draft sits to the source emails retrieved from the knowledge
base. No gating, no rewrite policy.

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
            "confidence":          "high"      # high | medium | low
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
"""

from __future__ import annotations

import json
import logging
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


def _tfidf_cosine(candidate: str, references: list[str]) -> list[float]:
    """Cosine similarity of the candidate vs each reference in TF-IDF space.

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
    return [float(s) for s in sims]


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


def _build_reference_rows(
    valid_refs: list[dict[str, str]],
    tfidf_scores: list[float],
    rouge_scores: list[float],
    shares: list[float],
) -> list[dict[str, Any]]:
    """Combine the per-reference metrics into the response row shape, ranked."""
    rows = [
        {
            "email_id": ref["email_id"],
            "similarity": round(tfidf, 4),
            "rouge_l": round(rouge, 4),
            "tfidf_cosine": round(tfidf, 4),
            "relative_share": round(share, 4),
            # rank filled in below after sorting
        }
        for ref, tfidf, rouge, share in zip(
            valid_refs, tfidf_scores, rouge_scores, shares
        )
    ]
    # Sort descending by similarity, then assign rank 1..N.
    rows.sort(key=lambda r: r["similarity"], reverse=True)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    # Re-order keys so rank appears after email_id when serialised.
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


def _build_summary(ranked_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Produce the candidate_summary block."""
    if not ranked_rows:
        return {"closest_match": None, "closest_similarity": 0.0, "confidence": "low"}

    top = ranked_rows[0]
    second_sim = ranked_rows[1]["similarity"] if len(ranked_rows) > 1 else None
    return {
        "closest_match": top["email_id"],
        "closest_similarity": top["similarity"],
        "confidence": _confidence(top["similarity"], second_sim),
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
                },
            }),
        }

    ref_texts = [r["text"] for r in valid_refs]

    try:
        tfidf_scores = _tfidf_cosine(candidate, ref_texts)
    except Exception as exc:
        _LOG.exception("TF-IDF scoring failed: %s", exc)
        return {"statusCode": 500, "body": json.dumps({"error": "scoring failed"})}

    rouge_scores = [_rouge_l(candidate, t) for t in ref_texts]
    shares = _relative_shares(tfidf_scores)

    ranked_rows = _build_reference_rows(valid_refs, tfidf_scores, rouge_scores, shares)
    summary = _build_summary(ranked_rows)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "references": ranked_rows,
            "candidate_summary": summary,
        }),
    }
