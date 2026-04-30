# Email Similarity Service — Plan & Architecture

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Strands Agents Service                           │
│                                                                      │
│   Researcher Agent ──► Writer Agent ──► similarity_tool()            │
│        │                    │                 │                      │
│        ▼                    │                 ▼                      │
│    S3 Vectors KB            │          ┌─────────────┐               │
│    (top k emails)           │          │ lambda:     │               │
│                             │          │ Invoke      │               │
│                             │          │ (SigV4/IAM) │               │
│                             │          └──────┬──────┘               │
│                             │                 │                      │
└─────────────────────────────┼─────────────────┼──────────────────────┘
                              │                 │
                              │                 ▼
                              │      ┌──────────────────────┐
                              │      │ Similarity Lambda     │
                              │      │ (zip, Python 3.12)   │
                              │      │                      │
                              │      │  TF-IDF (sklearn)    │
                              │      │  ROUGE-L (rouge-score)│
                              │      │                      │
                              │      │  512 MB, 10s timeout │
                              │      │  ~50ms warm          │
                              │      │  ~1s cold            │
                              │      └──────────────────────┘
                              │
                              ▼
                  UI surfaces similarity scores
                  alongside the draft for the user
                  (informational only — no gating)
```

### Components

| Component | Service | Role |
|---|---|---|
| Strands Agents Service | AgentCore Runtime | Hosts the multi-agent pipeline |
| S3 Vectors KB | Amazon S3 Vectors | Stores email embeddings; Researcher retrieves top k |
| Similarity Lambda | AWS Lambda (zip, Python 3.12) | Computes per-reference TF-IDF + ROUGE-L + summary |

The Lambda is stateless, has no VPC attachment, no persistence, no
environment variables, and no external dependencies beyond standard
AWS Lambda runtime.

---

## 2. End-to-End Request Flow

```
1. User submits query
         │
         ▼
2. Researcher Agent ──► S3 Vectors ──► [{email_id, text}, ...]
         │
         ▼
3. Writer Agent (may run 2–3 internal refinement passes)
         │
         ▼  final_draft_text
4. similarity_tool()     ──► Lambda ──► scores + summary
         │
         ▼
5. UI renders draft + similarity panel:
     "Your draft is 87% aligned with [id_1]
      (high confidence match)."
```

### Step-by-step

1. **User submits a query** (e.g. "Draft a Q3 pricing follow-up").

2. **Researcher Agent** invokes the S3 Vectors retrieval tool.
   It returns the top k most similar emails, each with an
   ``email_id`` and ``text``. The agent passes these downstream.

3. **Writer Agent** receives the source emails as context plus the
   user query. It drafts a new email, potentially running 2–3
   internal refinement passes. Output: ``final_draft_text``.

4. **similarity_tool** (a Strands ``@tool`` function, not a full
   agent) takes the draft and the source emails and makes a
   SigV4-signed ``lambda:Invoke`` call. The Lambda computes per-
   reference TF-IDF cosine + ROUGE-L, ranks them, and adds a
   summary block identifying the closest match and how confident
   that assessment is.

5. **UI** surfaces the scores to the user, alongside the generated
   draft. The score is **informational**: it tells the user how
   similar their draft looks to the source emails that were
   retrieved. The pipeline does not gate on it, does not rewrite
   based on it, and does not flag it for human review.

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
    "confidence":         "high"
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
| ``candidate_summary.confidence`` | enum | ``high`` / ``medium`` / ``low`` — see below |

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

### 4.5 The ``confidence`` field

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

### Tool definition

```python
import json
import boto3
from strands import tool

_lambda = boto3.client("lambda", region_name="eu-west-2")

@tool
def compute_similarity(draft_email: str, source_emails: list[dict]) -> dict:
    """Score how similar a draft email is to each source email.

    Args:
        draft_email: The final draft produced by the Writer agent.
        source_emails: [{email_id, text}, ...] from S3 Vectors retrieval.

    Returns:
        {
          "references": [...],         # ranked per-reference scores
          "candidate_summary": {...},   # closest match + confidence
        }
    """
    resp = _lambda.invoke(
        FunctionName="copycraft-similarity-handler",
        InvocationType="RequestResponse",
        Payload=json.dumps({
            "candidate": draft_email,
            "references": source_emails,
        }).encode(),
    )
    body = json.loads(resp["Payload"].read())
    return json.loads(body["body"])
```

### When it triggers

The tool is invoked **once per pipeline execution, after the Writer
has produced its final draft**. Never during intermediate Writer
refinement passes. The Strands orchestrator controls the sequencing:

```python
# 1. Researcher retrieves source emails
source_emails = researcher(user_query)

# 2. Writer drafts (may iterate internally)
draft = writer(source_emails, user_query)

# 3. Similarity score (single call, after final draft)
scores = compute_similarity(draft_email=draft.text, source_emails=source_emails)

# 4. Response returned to the caller with scores attached
return {"draft": draft.text, "similarity": scores}
```

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
