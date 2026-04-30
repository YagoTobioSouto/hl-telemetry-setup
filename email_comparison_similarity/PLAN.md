# Email Similarity Service — Plan & Architecture

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Strands Agents Service                           │
│                                                                      │
│   Researcher ─► Copywriter ─► Editor ─► Reviewer ─► Evaluator ─►     │
│      │                                                │     Serializer│
│      ▼                                                ▼              │
│   S3 Vectors KB                                 evaluate_similarity()│
│   (top 3 emails)                                      │              │
│                                                       │ boto3        │
│                                                       ▼ lambda:Invoke│
│                                              ┌─────────────────────┐│
│                                              │ Similarity Lambda   ││
│                                              │ (zip, Python 3.12)  ││
│                                              │                     ││
│                                              │  TF-IDF (sklearn)   ││
│                                              │  ROUGE-L            ││
│                                              │  + verdict/evidence ││
│                                              │                     ││
│                                              │  512 MB, 10s timeout││
│                                              │  ~50ms warm         ││
│                                              │  ~1s cold           ││
│                                              └─────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
                                                        │
                                                        ▼
                                             SimilarityEvaluation JSON
                                             (references + verdict +
                                              evidence + explanation)
                                             ──► UI
```

### Components

| Component | Service | Role |
|---|---|---|
| Strands Agents Service | AgentCore Runtime | Hosts the multi-agent pipeline |
| S3 Vectors KB | Amazon S3 Vectors | Stores email embeddings; Researcher retrieves top k |
| Similarity Evaluator agent | Strands agent (Python module) | Sits between Reviewer and Serializer; calls the Lambda and a Bedrock LLM to produce `SimilarityEvaluation` |
| Similarity Lambda | AWS Lambda (zip, Python 3.12) | Deterministic scoring: per-reference TF-IDF + ROUGE-L + verdict + evidence |
| Bedrock (Nemotron 3 Super or GLM 5) | Amazon Bedrock | Writes the one-sentence `explanation` inside the Evaluator agent |

The Lambda is stateless, has no VPC attachment, no persistence, no
environment variables, and no external dependencies beyond standard
AWS Lambda runtime. The Evaluator agent runs inside the Strands
orchestrator's process — it is not a separately-deployed service.

---

## 2. End-to-End Request Flow

```
1. User submits query
         │
         ▼
2. Researcher ──► S3 Vectors ──► [{email_id, text}, ...]  (top 3)
         │
         ▼
3. Copywriter ──► Editor ──► Reviewer
         │           (refinement passes within this triad)
         ▼  final_draft_text
4. Evaluator agent: evaluate_similarity(draft, sources)
         │
         ├──► invoke_similarity_lambda()   # plain function call
         │        └─ Lambda computes scores + verdict + evidence
         │
         ├──► Bedrock (Nemotron/GLM) writes 1-sentence explanation
         │
         └──► assembles SimilarityEvaluation (Pydantic)
                   │
                   ▼
5. Serializer forwards SimilarityEvaluation JSON to the UI:
     references[] + verdict + confidence + evidence + explanation
```

### Step-by-step

1. **User submits a query** (e.g. "Draft a Q3 pricing follow-up").

2. **Researcher Agent** invokes the S3 Vectors retrieval tool.
   It returns the top 3 most similar emails, each with an
   ``email_id`` and ``text``. These are passed downstream.

3. **Copywriter → Editor → Reviewer triad** receives the source
   emails plus the user query. The Copywriter drafts, the Editor
   refines, and the Reviewer checks against the editorial
   guidelines, potentially sending the draft back for another
   pass. Final output: ``final_draft_text``.

4. **Evaluator agent** is invoked exactly once, after the
   Reviewer's final draft. Its entry point is
   ``evaluate_similarity(draft, sources)`` in
   ``evaluator_agent/agent.py``. It:
   1. Calls the Lambda in plain Python (not a Strands tool — see
      §7 for why) to get deterministic scores + evidence.
   2. Calls Bedrock once with the scoring output inlined in the
      prompt, asking for a one-sentence informational
      description of the relationship.
   3. Assembles a typed ``SimilarityEvaluation`` Pydantic that
      passes the deterministic fields through unchanged and adds
      only the LLM-generated ``explanation`` string.

5. **Serializer** receives the ``SimilarityEvaluation``, serialises
   it to JSON, and forwards it to the UI alongside the draft.
   The UI renders the ``explanation`` as the headline and exposes
   ``references[]`` / ``evidence`` in an expandable detail view.

The score is **informational**: it tells the user how similar their
draft looks to the source emails. The pipeline does not gate on it,
does not rewrite based on it, and does not flag it for human review.

---

## 3. Scoring Approach

### 3.1 Why TF-IDF + ROUGE-L instead of BERTScore

The earlier design used BERTScore (DistilBERT embeddings → cosine
similarity) as the primary metric. That was replaced with TF-IDF
cosine for three reasons:

1. **The use case doesn't need semantic paraphrase detection.**
   The score is shown to the user, not used to gate a rewrite
   decision. For informational "how derivative is this?" signal,
   surface-level vocabulary overlap is more than sufficient — and
   is in fact easier to interpret.

2. **BERTScore's noise floor was too high.** Any two pieces of
   natural English text scored 0.70–0.85 in DistilBERT cosine
   space, because BERT embeddings cluster all valid English in a
   narrow region. The decision boundary between "related" and
   "unrelated" was compressed into a ~0.1 range. TF-IDF naturally
   sits in a much wider range (0.0–1.0) with unrelated emails
   near zero.

3. **The operational cost was disproportionate to the value.**
   BERTScore required a ~800 MB container image with PyTorch,
   4 GB Lambda memory, 15–20s cold starts, and a baked-in
   neural model. TF-IDF ships as a ~5 MB zip, runs in 512 MB
   RAM, cold-starts in ~1s, and needs no model weights.

### 3.2 What the two metrics measure

| Metric | What it catches | What it misses |
|---|---|---|
| **TF-IDF cosine** | Shared topical vocabulary, weighted by rarity (rare shared words → high score, common "hi team" words → low) | Heavy synonym-swap paraphrase (score ↔ grade) |
| **ROUGE-L** | Longest common word subsequence (verbatim or near-verbatim phrasing) | Reordering, synonyms, semantic paraphrase |

Both metrics are in ``[0, 1]`` by construction. They are complementary
signals and we return both — TF-IDF as the headline ``similarity``
field (it's the more discriminative signal for this use case) and
ROUGE-L as a secondary component kept alongside for auditing.

### 3.3 TF-IDF configuration

```python
TfidfVectorizer(
    lowercase=True,
    stop_words="english",
    ngram_range=(1, 2),   # unigrams + bigrams
)
```

The vectoriser is fit **per request** on the candidate plus all its
references. This means IDF weights are derived from the corpus
actually under comparison, not from some pre-trained vocabulary —
which matters because we're scoring emails, not a general-purpose
document set. With the small reference counts per request (typically
3–10), the per-request fit cost is negligible (~5 ms).

Bigrams are included so short shared phrases like "q3 pricing" or
"rate card" register stronger than just the unigrams "q3 + pricing +
rate + card", which helps the signal rise above boilerplate.

---

## 4. Response Schema

### 4.1 Request

```json
{
  "candidate": "full text of the drafted email",
  "references": [
    {"email_id": "id_1", "text": "full text of source email 1"},
    {"email_id": "id_2", "text": "..."}
  ]
}
```

### 4.2 Response body

```json
{
  "references": [
    {
      "email_id":       "id_1",
      "rank":           1,
      "similarity":     0.5321,
      "rouge_l":        0.6381,
      "tfidf_cosine":   0.5321,
      "relative_share": 0.8706
    },
    {
      "email_id":       "id_2",
      "rank":           2,
      "similarity":     0.0446,
      "rouge_l":        0.1854,
      "tfidf_cosine":   0.0446,
      "relative_share": 0.0730
    }
  ],
  "candidate_summary": {
    "closest_match":      "id_1",
    "closest_similarity": 0.5321,
    "confidence":         "high",
    "verdict":            "near_duplicate",
    "evidence": {
      "shared_terms": [
        {"term": "tier skus",  "weight": 0.1674},
        {"term": "customer",   "weight": 0.1184},
        {"term": "discussion", "weight": 0.1184}
      ],
      "longest_shared_phrase": {
        "text":        "quick summary of where we landed so everyone has the same picture",
        "token_count": 12
      },
      "candidate_unique_term_ratio": 0.5547
    }
  }
}
```

### 4.3 Field-by-field

| Field | Type | Meaning |
|---|---|---|
| ``references[].email_id`` | string | Passthrough from the request |
| ``references[].rank`` | int | 1 = closest, N = furthest, inside this batch |
| ``references[].similarity`` | float [0,1] | Headline score = ``tfidf_cosine`` |
| ``references[].rouge_l`` | float [0,1] | ROUGE-L F-measure (surface overlap) |
| ``references[].tfidf_cosine`` | float [0,1] | Cosine in TF-IDF vector space |
| ``references[].relative_share`` | float [0,1] | This ref's share of total similarity mass across the batch; rows sum to 1.0 |
| ``candidate_summary.closest_match`` | string | ``email_id`` of the rank-1 reference |
| ``candidate_summary.closest_similarity`` | float | Headline score of the closest match |
| ``candidate_summary.confidence`` | enum | ``high`` / ``medium`` / ``low`` — how decisive the ranking is, see §4.5 |
| ``candidate_summary.verdict`` | enum | ``distinct`` / ``related`` / ``near_duplicate`` — how close the top match is, see §4.6 |
| ``candidate_summary.evidence`` | object \| null | Deterministic "why" for the closest match only, see §4.7. ``null`` when no valid references. |
| ``evidence.shared_terms`` | list of {term, weight} | Top 5 TF-IDF terms shared with the closest match, ranked by geometric-mean weight |
| ``evidence.longest_shared_phrase`` | {text, token_count} | Longest *contiguous* run of matching tokens between candidate and closest match |
| ``evidence.candidate_unique_term_ratio`` | float [0,1] | Fraction of the candidate's vocabulary that doesn't appear in the closest match (1.0 = fully original) |

### 4.4 The ``relative_share`` field — batch-relative normalisation

Raw TF-IDF cosine values look small in isolation (a 0.53 feels
unimpressive even when it's a genuine match) because they're not
compared against a ceiling. ``relative_share`` normalises each
reference against the others in the same request:

```
relative_share[i] = tfidf_cosine[i] / sum(tfidf_cosine)
```

This answers the "within this batch, which is closest?" question
directly. The closest match in a copycat batch will dominate (e.g.
87% share), while in a batch of all-unrelated references the top
reference will only hold a mild plurality (e.g. 52%).

**Not min-max normalisation.** Min-max would stretch the batch to
``[0, 1]`` regardless of absolute scores, making the best of a bad
bunch look like a perfect match. Share-of-total preserves the
absolute-vs-relative distinction, and ``confidence`` picks up the
"everything is noise" case explicitly.

### 4.5 The ``confidence`` field — how decisive is the ranking?

Computed from the gap between rank-1 and rank-2 similarity:

```
gap = rank1.similarity - rank2.similarity

confidence = "high"   if gap >= 0.15
            | "medium" if gap >= 0.05
            | "low"    otherwise
```

The point is to flag "there's no genuine winner here, just the
least-dissimilar of a noisy set". When the UI sees
``confidence=low`` it should render the closest match with a soft
phrasing ("mildly resembles …") rather than a confident one
("aligned with …").

Thresholds are empirical from the fixture set; they can be tuned
without shape changes.

### 4.6 The ``verdict`` field — how close is the closest match?

Computed from the absolute rank-1 similarity:

```
verdict = "near_duplicate" if closest_similarity >= 0.50
         | "related"        if closest_similarity >= 0.15
         | "distinct"       otherwise
```

``confidence`` and ``verdict`` answer different questions and can
legitimately disagree. The copywriter use case needs both:

| Case | similarity | gap | confidence | verdict | What it means |
|---|---|---|---|---|---|
| All references unrelated | 0.04 | 0.01 | low | distinct | Noise — no match at all, and no clear pick even within the noise |
| One decisive match | 0.53 | 0.49 | high | near_duplicate | Genuine reuse of a source email |
| Two lukewarm near-misses | 0.22 | 0.03 | low | related | Some topical overlap with multiple sources, no clear winner |
| One clear but moderate match | 0.30 | 0.25 | high | related | Source was referenced but not copied — fine |

``verdict`` is the one-word headline the UI can render without waiting
for a downstream LLM. ``confidence`` tells the evaluator whether the
top pick is *stable* or a noisy tie.

### 4.7 The ``evidence`` block — why did this score come out this way?

Three deterministic, zero-LLM fields that explain the rank-1 score.
Scoped to the closest match only — lower-ranked references would be
noise for the copywriter use case and would triple the response size.

**``shared_terms``** — Top 5 TF-IDF terms (unigrams + bigrams, after
English stopword removal) present in both the candidate and the closest
reference. Each term's weight is the geometric mean of its TF-IDF
weights in the two documents, ``sqrt(cand_w * ref_w)``. Geometric mean
stays in the same [0, 1] range as the headline score, so a term with
weight 0.17 "feels" like a 0.17-level contribution.

These are literally the vocabulary driving the cosine score. Ranking
by the product of their weights surfaces the terms most responsible
for the similarity — boilerplate "wanted share happy" for unrelated
cases, substantive "tier skus customer discussion" for actual matches.

**``longest_shared_phrase``** — Longest contiguous run of matching
tokens between candidate and closest match. ROUGE-L's own LCS is
non-contiguous (matches "a b c" against "a x b x c") which is fine
for scoring but terrible evidence — nobody reading the output would
recognise it as overlap. So we compute the contiguous version directly
using the same tokeniser sklearn's TF-IDF uses, which guarantees the
phrase we surface is literally part of what drove the cosine score.

Comes with ``token_count`` so the downstream agent can judge
substance: short phrases ("let me know if", 4 tokens) are filler;
long phrases (12+ tokens) are damning evidence of verbatim reuse.

**``candidate_unique_term_ratio``** — Fraction of the candidate's
TF-IDF vocabulary that does *not* appear in the closest match. A
simple originality proxy: 1.0 means nothing overlaps (fully original),
0.0 means every term is shared (heavy copy). In practice a genuine
near-duplicate halves originality (~0.55), while unrelated references
leave it near 1.0 (~0.93).

### 4.8 How the evaluator agent uses this

The ``evidence`` block is the handoff to the Similarity Evaluator
agent (shown in the architecture diagram as the node between the
Reviewer's Final Draft and the Serializer). The agent's job is to
take the full Lambda response + both email texts and produce a
single user-facing sentence.

The agent is expected to:

* Treat ``verdict`` as the structural answer and ``evidence`` as the
  supporting detail. It should not invent a new verdict from the raw
  scores — the Lambda already did that work deterministically.
* Filter ``shared_terms`` and ``longest_shared_phrase`` by substance.
  A 4-token phrase of pure boilerplate should be omitted from the
  sentence; a 12-token substantive match should be quoted verbatim.
* Degrade gracefully. If the agent is unavailable, the UI should
  render ``verdict`` + ``shared_terms`` directly as a fallback.

The Lambda itself makes no LLM calls and produces no prose. This
keeps scoring deterministic, fast, and auditable — the prose layer
is strictly additive.

---

## 5. Lambda Compute, Memory & Scaling

### Sizing

| Setting | Value | Rationale |
|---|---|---|
| Runtime | Python 3.12 | Lambda-native, no container indirection |
| Memory | 512 MB | sklearn peaks ~150 MB on short emails; 512 MB gives 3× headroom |
| Timeout | 10 s | Warm invocations ~50 ms; cold ~1 s; 10 s is defensive |
| Ephemeral storage | 512 MB (default) | Unused |
| Provisioned concurrency | None | Cold start is already ~1 s |
| VPC | None | No internal services to reach |
| Environment variables | None | No runtime configuration |

### Cold start profile

Zip package is ~5 MB (mostly sklearn + scipy + numpy compiled wheels).
Python 3.12 interpreter boots in ~200 ms, the zip unpacks and
``handler.py`` imports in ~600 ms, first scoring call adds another
~100 ms for sklearn initialisation. Total cold start: roughly 1 s.

Warm invocations bypass all of that and spend ~50 ms in the
TF-IDF fit + ROUGE-L loop for a typical 3-reference batch.

### Cost

| Usage | Monthly cost |
|---|---|
| 100 invocations / month (all warm) | ~$0.000005 |
| 1,000 invocations / month | ~$0.00005 |
| 10,000 invocations / month | ~$0.0005 |
| At rest | $0 |

Round-trip cost is dominated by the ``lambda:Invoke`` API calls at
this traffic level, not by execution time. The previous BERTScore
container design cost ~$0.0002 *per invocation*; the zip version
is ~100× cheaper.

---

## 6. Packaging & Deployment

### Zip packaging via CDK bundling

```python
fn = _lambda.Function(
    self, "SimilarityHandler",
    runtime=_lambda.Runtime.PYTHON_3_12,
    handler="handler.lambda_handler",
    code=_lambda.Code.from_asset(
        lambda_asset_path,
        bundling=BundlingOptions(
            image=DockerImage.from_registry(
                "public.ecr.aws/sam/build-python3.12:latest"
            ),
            command=[
                "bash", "-c",
                "pip install --no-cache-dir -r requirements.txt -t /asset-output "
                "&& cp -au handler.py /asset-output/",
            ],
        ),
    ),
    memory_size=512,
    timeout=Duration.seconds(10),
)
```

CDK shells out to the public AWS Lambda Python 3.12 build image to
``pip install`` the dependencies into a staging directory, which is
then zipped and uploaded to Lambda.

**Why the public SAM image**: numpy/scipy publish platform-specific
compiled wheels. Installing on a macOS dev machine and uploading
straight up would leave ARM or Darwin binaries in the zip, which
Lambda's x86_64 Amazon Linux runtime can't load. Building inside
the same base image Lambda actually uses guarantees the wheels
match.

**Why ``public.ecr.aws`` and not ``docker.io``**: ECR Public doesn't
rate-limit anonymous pulls the way Docker Hub does, so CI and
developer machines don't randomly fail on ``docker pull``.

### Network & security

- **No VPC.** The Lambda doesn't call any private services. Adding
  VPC attachment here would only add ENI-provisioning cold-start
  latency for zero benefit.
- **No Function URL.** Invoked via the ``lambda:InvokeFunction``
  API directly from the Strands Agents execution role. IAM auth is
  built in.
- **IAM policy on the Strands execution role**:

  ```json
  {
    "Effect": "Allow",
    "Action": "lambda:InvokeFunction",
    "Resource": "arn:aws:lambda:eu-west-2:ACCOUNT:function:copycraft-similarity-handler"
  }
  ```

- **CloudWatch Logs** retention is 1 month (covers debugging without
  accumulating cost).

---

## 7. Integration with Strands Agents

### The Evaluator agent entry point

The similarity Lambda is not exposed to the orchestrator directly.
It is wrapped by the Similarity Evaluator agent
(``evaluator_agent/``), which is the single public API for this
capability:

```python
from evaluator_agent.agent import evaluate_similarity
from evaluator_agent.schemas import SimilarityEvaluation

# After the Reviewer has produced its final draft:
evaluation: SimilarityEvaluation = evaluate_similarity(
    draft_email=final_draft_text,
    source_emails=researcher_output,   # [{"email_id": ..., "text": ...}]
    llm="live",                        # "mock" for offline / CI runs
    lambda_mode="live",                # "local" to run the handler in-process
)

# evaluation is a typed Pydantic model with:
#   - references: list[Reference]      full ranked scores for all sources
#   - verdict, confidence              deterministic from the Lambda
#   - evidence                         shared terms / phrase / originality ratio
#   - explanation                      one informational sentence
serializer_payload = evaluation.model_dump()
```

Internally, ``evaluate_similarity`` does three things in sequence:

1. Calls the Lambda (``invoke_similarity_lambda``) to get the
   deterministic scoring output.
2. Calls Bedrock once with the scoring dict inlined in the prompt,
   with ``structured_output_model=ExplanationOnly`` so the model
   can only return the single ``explanation`` string.
3. Assembles the full ``SimilarityEvaluation`` from the Lambda's
   deterministic fields and the LLM's explanation.

### Why the Lambda is NOT a Strands ``@tool``

An earlier design registered the Lambda as a Strands ``@tool`` that
the LLM could call. That pattern broke in practice: the model
(Nemotron 3 Super) entered infinite tool-call loops — 30+
invocations per request — instead of producing the final structured
output. NVIDIA's own Nemotron documentation flags tool-call loop
failures as one of its two dominant failure modes, and combining
tools with ``structured_output`` amplifies it.

The deeper reason to avoid ``@tool`` here is architectural: **the
Lambda call is not a decision the LLM needs to reason about**. It
happens exactly once per evaluation, unconditionally, with known
inputs. Making it a tool lets the LLM second-guess a step that
isn't a choice. Calling it in plain Python and feeding the result
into the prompt removes the failure mode entirely and is faster.

The same reasoning applies to other deterministic preprocessing
steps. A good rule of thumb: if you know in advance that a call
will happen exactly once and what its inputs will be, don't make
it a tool.

### When the Evaluator is invoked

```python
# 1. Researcher retrieves source emails
source_emails = researcher(user_query)

# 2. Copywriter/Editor/Reviewer produce the final draft
draft = reviewer(editor(copywriter(source_emails, user_query)))

# 3. Similarity evaluation — single call, after the final draft
evaluation = evaluate_similarity(
    draft_email=draft.text,
    source_emails=source_emails,
    llm="live",
    lambda_mode="live",
)

# 4. Serializer forwards both to the UI
return {"draft": draft.text, "similarity": evaluation.model_dump()}
```

The Evaluator runs exactly once per pipeline execution, after the
Reviewer's final output. It does not run on intermediate
refinement passes.

### Degraded-mode behaviour

If Bedrock is unavailable, set ``llm="mock"`` and the Evaluator
will return a deterministic templated explanation that mirrors the
three verdict branches of the live prompt. The rest of the
``SimilarityEvaluation`` is unchanged. The Serializer needs no
code changes — the response shape is identical.

If the Lambda invocation fails, ``evaluate_similarity`` raises a
``RuntimeError``. The orchestrator should catch this and attach
``similarity_status: "unavailable"`` to the response rather than
propagating the error to the user. Similarity scoring is
informational and must not block the draft.

---

## 8. Correctness Properties

### CP-1: Similarity scoring is post-final-draft only

``compute_similarity`` is invoked exactly once per pipeline execution,
after the Writer's final output. Intermediate refinement passes do
not score.

### CP-2: Scoring failure does not block the pipeline

If the Lambda invocation errors or times out, the orchestrator
attaches ``similarity_status: "unavailable"`` to the response and
returns the draft anyway. Similarity scoring is informational —
the user still gets their draft.

### CP-3: Response shape is stable regardless of reference count

Even with zero valid references (all malformed), the handler
returns ``{references: [], candidate_summary: {...confidence: "low"}}``
rather than erroring. Callers always see the same top-level shape.

### CP-4: Ranks are dense and 1-indexed

The ``references`` array is returned already sorted by
descending ``similarity``, and ``rank`` values are ``1..N``
contiguous. Callers do not need to sort.

### CP-5: ``relative_share`` sums to 1.0 across references

(Or, if all similarities are zero, equal shares are assigned so the
invariant holds and the response isn't NaN-poisoned.)
