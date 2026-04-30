# Synthesis — From BERTScore to TF-IDF: The Decision Trail

This document captures **why** the email similarity service looks the
way it does. It exists separately from `PLAN.md` (which describes
*what* we built) and `README.md` (which describes *how to use it*)
because the rationale for the current design is only legible against
the design it replaced.

Read this if you're:
- Picking up this service cold and want to understand why it's so
  simple (when the folder name and git history suggest it should be
  much more complex).
- Considering adding back semantic / neural scoring — this is the
  "what we learned when we tried" record.
- Evaluating a similar "score the AI output against its sources"
  problem elsewhere and want to skip the same detours.

---

## 1. Starting Point: The BERTScore Proposal

The original brief was:

> Score a drafted email for "over-derivativeness" against the 3 source
> emails retrieved from the knowledge base. Use BERTScore (a BERT-based
> semantic similarity metric) plus ROUGE-L. If the score is too high,
> the Reviewer agent should decide to REWRITE the draft, or FLAG it
> for human review.

That drove the v1 architecture:

- **Container-image Lambda** with PyTorch, transformers, bert-score,
  `distilbert-base-uncased` baked into the image.
- **4 GB memory** because Lambda ties CPU to memory and BERTScore
  inference is CPU-bound on 66M parameters.
- **~15–20s cold start** (pulling ~800 MB image + loading model).
- **Lambda Function URL with IAM auth**, VPC-attached for defence in
  depth.
- **Reviewer agent** applying threshold rules:
  `bertscore_f1 > 0.80 AND rouge_l > 0.60 → REWRITE`,
  `bertscore_f1 > 0.85 → FLAG_FOR_HUMAN_REVIEW`, else `ACCEPT`.
- **Provisioned concurrency** considered but rejected on cost (~$86/mo).

This was a defensible design **given the assumed requirements**. The
rationale was sound. The trouble was that the requirements weren't
quite what we thought.

---

## 2. Local Testing: What Actually Surfaced

### 2.1 The first symptom — flat scores

We built a local test harness (`local_test/run_test.py`) so we could
score realistic multi-line emails directly through the handler
without Docker. Early runs against hand-written fixtures produced
output like:

```
id_1 (investment newsletter, genuinely unrelated)   bertscore_f1=0.72
id_2 (roadmap, unrelated)                            bertscore_f1=0.78
id_3 (vendor contract, unrelated)                    bertscore_f1=0.76
```

Three emails, completely unrelated, scoring `0.72 / 0.76 / 0.78`.
That's within a 0.06 window.

Reasonable reaction: *"Something is wrong with the metric."*

### 2.2 Why it was actually happening

BERTScore is the cosine similarity of contextual BERT embeddings.
Two properties fall out of that:

1. Any two pieces of natural English text produce high cosine
   similarity in BERT embedding space, because BERT embeddings
   cluster all valid English in a narrow region of the vector space.
   Two random English paragraphs typically score 0.70–0.80. That's
   the **baseline noise floor**.
2. The ceiling is ~0.95 even for near-identical text. So the
   operational range is compressed to roughly `[0.70, 0.95]`.

The metric wasn't broken. It was working as documented. But the
*absolute* numbers were misleading — the signal was entirely in the
narrow top slice of the range.

### 2.3 The standard fix didn't work for us

BERTScore papers address this with `rescale_with_baseline=True`,
which subtracts a precomputed noise floor derived from random
sentence pairs and re-stretches the range so unrelated text scores
near 0 and near-duplicates near 1. Except:

- The `bert-score` library only ships baseline files for a small
  set of models (RoBERTa variants, full BERT, DeBERTa).
- **DistilBERT is not in that set.** Passing
  `rescale_with_baseline=True` with DistilBERT raises.

So we were stuck with raw scores in a compressed range on the specific
model we'd chosen for its size.

### 2.4 Calibration — did the gap still exist?

Just because the numbers looked flat didn't mean the metric couldn't
discriminate. We built a labelled set (5 buckets × 2 pairs each:
near-duplicate, same-topic-different-wording, paraphrase,
same-domain-unrelated, different-domain) and measured the actual
distribution:

| Bucket | BERTScore F1 range | ROUGE-L range |
|---|---|---|
| near_duplicate | 0.936–0.972 | 0.718–0.865 |
| same_topic_different_wording | 0.795–0.839 | 0.262–0.333 |
| paraphrase | 0.831–0.840 | 0.235–0.361 |
| same_domain_unrelated | 0.762–0.810 | 0.141–0.156 |
| different_domain | 0.660–0.703 | 0.058–0.059 |

Verdict: the gap we cared about — "is this a near-duplicate, yes or
no?" — was clean on both metrics. BERTScore had a +0.10 gap between
near-duplicate min (0.936) and the next bucket's max (0.840).
ROUGE-L had a much wider +0.38 gap (0.718 vs 0.333).

The middle buckets overlapped each other heavily on BERTScore, but
that overlap didn't matter for the question we needed to answer.

**So BERTScore technically worked. We could have shipped it.**

---

## 3. The Reframing That Changed Everything

Mid-build, a single clarification came through:

> Bear in mind that this is just to inform the user of content
> likeliness, no need for human review.

That one sentence deleted the entire Reviewer agent, the
REWRITE/FLAG/ACCEPT policy, and the threshold calibration work that
existed to support them. The Lambda's job became:

- Show the user a number per source email.
- That's it.

With gating removed, we no longer needed:

- A clean decision boundary between "paraphrase" and "same topic".
  (We only need "is this a copy, roughly?").
- Robust paraphrase detection via neural semantics.
  (The user sees the score and judges for themselves.)
- Carefully calibrated thresholds on a specific model.
  (No threshold = no calibration.)

What we *still* needed:
- A signal that rises when the draft is derivative.
- A signal that stays low when it isn't.
- Something cheap enough to run per-draft without thinking.

---

## 4. The Over-Engineering Audit

With the simplified requirement in hand, we walked the stack and
asked "what is each expensive piece paying for, now?"

| Piece | Reason it existed | Still needed? |
|---|---|---|
| PyTorch (400 MB wheel) | BERTScore backbone | ❌ Only if we kept BERTScore |
| DistilBERT model (250 MB) | Semantic embeddings | ❌ Only if we kept BERTScore |
| `bert-score` library | BERTScore calc | ❌ Only if we kept BERTScore |
| 4 GB Lambda memory | CPU for PyTorch | ❌ Only if we kept BERTScore |
| 15s cold start | PyTorch + model load | ❌ Only if we kept BERTScore |
| Container image (~800 MB) | PyTorch dependency | ❌ Only if we kept BERTScore |
| VPC attachment + Function URL | Defensive posture | ❌ Nothing to reach in VPC |
| Provisioned concurrency discussion | BERTScore cold starts | ❌ Moot without BERTScore |
| Reviewer agent | Threshold policy | ❌ No gate = no policy |
| Threshold calibration scaffolding | Reviewer policy | ❌ No policy = no calibration |
| ROUGE-L | Surface overlap signal | ✅ Cheap, keep |

Almost every expensive piece existed to support BERTScore. BERTScore
existed to support the Reviewer. The Reviewer existed to support a
gating/rewrite policy. **The gating policy no longer existed.**

---

## 5. The Replacement: TF-IDF Cosine + ROUGE-L

### 5.1 Why TF-IDF

TF-IDF cosine similarity is the textbook "how similar are these two
documents?" metric. It works as follows:

1. Build a vocabulary from all the texts in the batch.
2. Each document becomes a vector where position `i` is the
   *term-frequency × inverse-document-frequency* weight of vocabulary
   word `i`.
3. Similarity = cosine of the angle between two vectors.

The **inverse-document-frequency** term is doing the work we wanted
from BERTScore's embeddings: rare shared words (e.g. "tier 2 uplift",
"Q3 pricing") count more than common English ones (e.g. "hi team").

Running the same fixtures through TF-IDF + ROUGE-L:

```
fixtures/ (unrelated)                   fixtures_copycat/ (one copy)
id_1 similarity=0.10   share=9%         copycat_source sim=0.53 share=87%
id_2 similarity=0.19   share=52%        roadmap        sim=0.04 share=7%
id_3 similarity=0.13   share=39%        vendor         sim=0.03 share=6%
```

The signal is now:
- **Clean separation**: 12× ratio between copycat and noise on
  TF-IDF (vs 1.1× on BERTScore F1).
- **Intuitive numbers**: `0.03` clearly says "not related", `0.53`
  clearly says "very related". No noise floor to subtract.
- **Sub-second execution**: ~10 ms warm, no neural model.

### 5.2 Why keep ROUGE-L alongside

Two independent signals are more robust than one. ROUGE-L catches
verbatim phrase copying even when the vocabulary overlap is thin
(imagine a draft that quotes a single distinctive sentence from a
source verbatim but is otherwise unrelated — ROUGE would flag that,
TF-IDF might not).

Keeping both also makes the score *auditable*: a user or operator
looking at the response can see "high TF-IDF, low ROUGE" = paraphrase
with shared vocabulary, vs "high ROUGE, high TF-IDF" = genuine
copy-paste. Different failure modes, same response surface.

The headline `similarity` field is TF-IDF because for our data it's
the more discriminative signal.

---

## 6. The Post-Processing Design

Raw similarity numbers look small in isolation. `0.53` feels
unimpressive even when it's a genuine match, because there's no
natural ceiling.

We added two pieces of post-processing to make the scores more
useful to the UI layer:

### 6.1 `relative_share` — batch-relative normalisation

Each reference's share of the total similarity mass:
```
share[i] = similarity[i] / sum(similarity)
```

Shares across the batch sum to 1.0. This answers *"within this
batch, which is closest?"* directly.

Copycat batch: the copycat holds 87% of the mass. Unambiguous.
Unrelated batch: the top reference holds 52%, only marginally ahead
of the runner-up at 39%. That's a three-way tie among nothing,
not a match.

**Why not min-max normalisation?** Min-max would rescale every batch
so rank-1 is always `1.0` and rank-N is always `0.0`. That's
superficially nice but loses the absolute-vs-relative distinction.
A "best of a bad bunch" would look identical to a genuine match.
Share-of-total preserves the truth: in a bad bunch, nothing
dominates.

### 6.2 `candidate_summary.confidence` — decisiveness of the top match

Computed from the gap between rank-1 and rank-2 similarity:

```
gap = rank1.similarity - rank2.similarity

high   if gap ≥ 0.15
medium if gap ≥ 0.05
low    otherwise
```

This is the "did we genuinely find something, or is this just the
least-dissimilar in a noisy batch?" signal. Fixture results:

| Scenario | top | second | gap | confidence |
|---|---|---|---|---|
| fixtures_copycat/ | 0.53 | 0.045 | 0.49 | **high** |
| fixtures/ (all unrelated) | 0.04 | 0.031 | 0.009 | **low** |

When the UI sees `confidence=low` it should soften the phrasing
("mildly resembles…") rather than assert a match ("aligned with…").

---

## 7. Packaging Simplification

With no neural model and no GPU-coupled dependencies, the container
image stopped paying rent.

| Concern | Container (before) | Zip (now) |
|---|---|---|
| Artifact size | ~800 MB | ~5 MB |
| Packaging | Dockerfile + ECR push | CDK `BundlingOptions` + zip |
| Cold start | ~15 s | ~1 s |
| Warm latency | ~1–3 s | ~50 ms |
| Memory | 4 GB (for CPU allocation) | 512 MB (actual need + headroom) |
| Timeout | 60 s | 10 s |
| Per-invocation cost | ~$0.0002 | ~$0.000002 |
| Attack surface | PyTorch + HF hub calls | sklearn + numpy + scipy |
| Ops overhead | ECR image lifecycle | None |

Bundling inside `public.ecr.aws/sam/build-python3.12:latest` (not
Docker Hub) guarantees numpy/scipy wheels match the Lambda runtime's
Amazon Linux glibc and avoids Docker Hub anonymous rate limits.

---

## 8. What We Kept From the Original Effort

Not all of the early work was wasted:

- **The modular fixtures layout** (`candidate.txt` + `references/`)
  survived the metric change unchanged. The handler contract is
  stable — `{candidate, references: [{email_id, text}]}` in,
  scored JSON out — so the test harness didn't need rewriting.
- **The `lambda/` vs `infra/` vs `local_test/` separation.** The
  new stack slotted straight into the same layout.
- **The calibration data + methodology** informed the choice to
  switch. The buckets (`near_duplicate`, `same_topic_different_wording`,
  etc.) aren't a live part of the repo any more, but the underlying
  insight — "absolute numbers are noise floor; bucket separation is
  what matters" — is why we landed on `relative_share` and
  `confidence` instead of just returning raw scores.

---

## 9. What We'd Reconsider Later

If real traffic shows issues:

- **Heavy synonym-swap paraphrase going undetected.** TF-IDF can't
  tell that "commence" ↔ "start". If the Writer agent paraphrases
  aggressively and users complain that genuine derivations are
  scoring low, we'd re-introduce semantic scoring — but this time
  with a smaller, rescaling-compatible model like `roberta-base`,
  still as a zip, or via sentence-transformers with a pre-baked
  embedding model.
- **The confidence thresholds (0.15 / 0.05).** Currently empirical
  from the fixture set. If real-world gaps cluster differently,
  tune them — they're hard-coded constants at the top of
  `handler.py`, no stack update needed for the threshold change
  itself, just a Lambda redeploy.
- **Bigger reference batches.** Current design handles 3–10
  references comfortably. At 50+ the per-request TF-IDF fit cost
  rises linearly; at 500+ you'd want to pre-compute a persistent
  IDF over the whole corpus instead of fitting per request.
  Not a current concern.

---

## 10. One-Line Summary

**A 4 GB PyTorch container with a neural model was justified for a
rewrite-gating decision that no longer exists; a 512 MB zip with
sklearn is justified for the informational-display decision that
does.**

The two designs aren't reflections of different engineering skill.
They're reflections of different requirements. The change in
requirements made one right and the other wrong.
