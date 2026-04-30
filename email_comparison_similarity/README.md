# Email Similarity Service

Informational "how derivative is this draft?" signal for the Copycraft
email pipeline. Surfaces, alongside each generated email, a typed
evaluation of how closely the draft resembles the source emails it
was written from.

**Informational only.** The pipeline does not gate on it, does not
trigger rewrites, does not flag for human review. The copywriter
sees the result and decides what to do.

---

## 1. Deliverables

This repo is three separate but coupled deliverables. Each owns a
different slice of the problem and can be understood on its own.

### 1.1 Similarity Lambda (`lambda/`)

Stateless AWS Lambda. Given a draft email and a list of reference
emails, returns deterministic scores plus a structured evidence
block:

- Per-reference **TF-IDF cosine + ROUGE-L** scores, ranked.
- Absolute **`verdict`** (`distinct` / `related` / `near_duplicate`)
  from the rank-1 similarity.
- Relative **`confidence`** (`low` / `medium` / `high`) from the
  gap between rank-1 and rank-2.
- **`evidence`** for the closest match: shared TF-IDF terms, longest
  contiguous shared phrase, candidate originality ratio.

No model weights, no env vars, no network calls beyond the Lambda
service plane itself. ~5 MB zip, 512 MB memory, ~50 ms warm,
~1 s cold. See [`PLAN.md § 4`](./PLAN.md) for the full response schema.

### 1.2 Similarity Evaluator agent (`evaluator_agent/`)

Python module — a Strands agent that sits between the Reviewer and
Serializer in the Copycraft pipeline. Entry point:

```python
from evaluator_agent.agent import evaluate_similarity

evaluation = evaluate_similarity(
    draft_email=final_draft_text,
    source_emails=researcher_output,   # [{"email_id": ..., "text": ...}]
    llm="live",                        # or "mock"
    lambda_mode="live",                # or "local"
)
```

Returns a typed `SimilarityEvaluation` (Pydantic → JSON) that merges
the Lambda's deterministic output with a single LLM-generated
sentence describing the relationship between the draft and its
closest source.

Uses **Nemotron 3 Super** (or **GLM 5**, swappable in one config
line) via Amazon Bedrock in `eu-west-2`. The LLM's output schema is
a single `explanation: str` field — it structurally cannot modify
the deterministic scores or verdict. See
[`evaluator_agent/README.md`](./evaluator_agent/README.md) for
internals and [`PLAN.md § 7`](./PLAN.md) for the integration
contract.

**Does not use a Strands `@tool` for the Lambda call** — see
[`evaluator_agent/README.md § Why no Strands tool?`](./evaluator_agent/README.md)
for why this matters.

### 1.3 CDK stack (`infra/`)

Packages and deploys the Lambda to `eu-west-2`. Pure
infrastructure-as-code:

- Zip-packaged (not container image).
- Bundled inside `public.ecr.aws/sam/build-python3.12:latest` so the
  compiled wheels match the Lambda runtime's Amazon Linux glibc.
- IAM-authed invocation only — no Function URL, no VPC attachment.
- CloudWatch Logs retention 1 month.
- No parameters to configure at deploy time; behaviour is hard-coded
  in the handler by design.

```bash
cd infra
cdk deploy
```

Stack outputs: Lambda function name and ARN.

---

## 2. How it fits into the pipeline

```
                    ┌───────────────────────────────────────────────┐
                    │           Strands Agents Service               │
                    │                                                │
    User query  ──► │  Researcher ─► Copywriter ─► Editor ─► Reviewer│
                    │      │                                    │   │
                    │      ▼                                    ▼   │
                    │  S3 Vectors                        final draft │
                    │  (top 3 emails)                            │   │
                    │                                            ▼   │
                    │                                   ┌────────────┤
                    │                                   │ Evaluator  │
                    │                                   │ agent      │ ───────────┐
                    │                                   └────────────┤            │
                    │                                            │   │            │
                    │                                            ▼   │            ▼
                    │                                      Serializer │   ┌──────────────────┐
                    │                                            │   │   │ Similarity       │
                    └────────────────────────────────────────────┼───┘   │ Lambda (eu-west-2)│
                                                                 │       │                  │
                                                                 ▼       │  TF-IDF + ROUGE-L│
                                                            UI payload  │  + verdict       │
                                                                        │  + evidence      │
                                                                        └──────────────────┘
                                                                                 ▲
                                                                                 │
                                                                           boto3 lambda:Invoke
                                                                           (from the Evaluator)
                                                                                 │
                                                                                 └── Bedrock
                                                                                     (Nemotron/GLM)
```

- **Researcher** pulls the top 3 source emails from the S3 Vectors KB.
- **Copywriter → Editor → Reviewer** produce the final draft.
- **Evaluator** (this repo) is invoked exactly once on the final
  draft. It calls the Lambda and Bedrock, returns a typed
  `SimilarityEvaluation`.
- **Serializer** forwards the evaluation JSON to the UI.

### Integration checklist for the orchestrator team

The team owning the Strands orchestrator needs:

1. **Add the Evaluator as a dependency** — either vendor the
   `evaluator_agent/` directory into your service, or install it from
   a git reference. Three Python deps (see
   [`evaluator_agent/requirements.txt`](./evaluator_agent/requirements.txt)).

2. **Call `evaluate_similarity()` once**, on the Reviewer's final
   output. Pass the source emails from the Researcher through
   unchanged.

3. **Grant the orchestrator's execution role two Bedrock +
   Lambda permissions**:

   ```json
   {
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "lambda:InvokeFunction",
         "Resource": "arn:aws:lambda:eu-west-2:ACCOUNT:function:copycraft-similarity-handler"
       },
       {
         "Effect": "Allow",
         "Action": "bedrock:InvokeModel",
         "Resource": [
           "arn:aws:bedrock:eu-west-2::foundation-model/nvidia.nemotron-super-3-120b",
           "arn:aws:bedrock:eu-west-2::foundation-model/zai.glm-5"
         ]
       }
     ]
   }
   ```

   Only grant the model ARN you actually use — both are listed for
   the swap case.

4. **Handle failure modes** (see
   [`PLAN.md § 7 — Degraded-mode behaviour`](./PLAN.md)):
   - Bedrock unavailable → fall back to `llm="mock"` (deterministic
     templated explanation, same shape).
   - Lambda invocation raises → catch the `RuntimeError`, attach
     `similarity_status: "unavailable"` to the response, return the
     draft anyway. Similarity is informational and must not block
     the draft.

5. **Serializer contract** — the `SimilarityEvaluation.model_dump()`
   JSON shape is stable; see
   [`evaluator_agent/README.md`](./evaluator_agent/README.md) for
   field-by-field. The UI should render `explanation` as the
   headline and expose `references[]` / `evidence` behind a
   "show details" interaction.

---

## 3. Layout

```
email_comparison_similarity/
├── PLAN.md                       Architecture & design decisions
├── SYNTHESIS.md                  How we got here (decision trail)
├── README.md                     This file
├── fixtures/                     Shared test emails, one source of truth
│   ├── candidate.txt
│   └── references/{id_1,id_2,id_3}.txt
├── lambda/                       DELIVERABLE 1 — scoring Lambda
│   ├── handler.py
│   └── requirements.txt
├── infra/                        DELIVERABLE 3 — CDK stack
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/similarity_stack.py
├── local_test/                   Lambda-only smoke test (no agent)
│   ├── run_test.py
│   └── requirements.txt
└── evaluator_agent/              DELIVERABLE 2 — Strands agent
    ├── README.md                 Agent-specific docs
    ├── agent.py                  evaluate_similarity() entry point
    ├── config.py                 Model, region, prompt
    ├── schemas.py                Pydantic: SimilarityEvaluation
    ├── similarity_client.py      Lambda invoker (local or live)
    ├── run_local.py              JSON-only runner
    └── requirements.txt
```

---

## 4. Demo from a clean checkout

There are two runners, answering different questions:

| Runner | Answers | What it exercises |
|---|---|---|
| `local_test/run_test.py` | "Are the scoring numbers right?" | Lambda handler alone, no agent |
| `evaluator_agent/run_local.py` | "Does the agent produce the right JSON?" | Full pipeline: Lambda + (optional) Bedrock + Pydantic |

Most of the time you want the agent runner. The Lambda runner is
there for when something looks wrong and you need to bisect *where*.

### 4.1 Agent — mock mode (default, no AWS creds)

Deterministic templated explanation. Good for fast iteration.

```bash
cd evaluator_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r ../lambda/requirements.txt   # scikit-learn, rouge-score

python run_local.py | jq .
```

Outputs valid JSON straight to stdout. Library logs go to stderr
so piping is always clean.

### 4.2 Agent — live LLM (Bedrock Nemotron 3 Super in eu-west-2)

Needs AWS creds with `bedrock:InvokeModel` for
`nvidia.nemotron-super-3-120b` in `eu-west-2`. Still uses the Lambda
in-process, so no Lambda deployment needed.

```bash
python run_local.py --live-llm | jq .
```

To compare the LLM against the deterministic mock explanation:

```bash
python run_local.py        | jq -r .explanation    # templated
python run_local.py --live-llm | jq -r .explanation   # Nemotron
```

### 4.3 Agent — full live (Bedrock + deployed Lambda)

Production shape. Needs `lambda:InvokeFunction` on
`copycraft-similarity-handler` *and* Bedrock. Deploy the Lambda
first (see § 5 Deploy below).

```bash
python run_local.py --live-llm --live-lambda | jq .
```

### 4.4 Lambda only

If the agent output looks wrong and you want to check whether the
Lambda itself is to blame:

```bash
cd local_test
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python run_test.py
```

This prints the raw Lambda response — no agent, no LLM, no JSON
cleanup. Same fixtures, same candidate, same references.

### Customising the fixtures

`fixtures/` is the single source of truth. Edit
`fixtures/candidate.txt` and the files under `fixtures/references/`
in your normal editor. Both runners pick up changes immediately —
no JSON escaping, no config files.

Filename (sans `.txt`) in `references/` becomes the `email_id`.

---

## 5. Deploying the Lambda

### Prerequisites

- Python 3.12+
- Docker (CDK bundles the Lambda zip inside a Lambda-compatible Linux
  container to get the right compiled wheels for numpy/scipy)
- AWS CLI configured with credentials for the target account
- AWS CDK v2 CLI: `npm install -g aws-cdk`
- CDK bootstrapped in the target account/region:
  `cdk bootstrap aws://ACCOUNT/eu-west-2`

### Deploy

```bash
cd infra
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# First deploy pulls the AWS Lambda Python 3.12 build image (~500 MB)
# for bundling. Subsequent deploys reuse it.
cdk synth
cdk deploy
```

On success the stack outputs the Lambda function name and ARN. Build
artefacts land in `infra/cdk.out/`.

### Invoking the Lambda directly (debug only)

```bash
aws lambda invoke \
  --function-name copycraft-similarity-handler \
  --region eu-west-2 \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "candidate": "Hi team, following up on the Q3 pricing discussion from last week.",
    "references": [
      {"email_id": "id_1", "text": "All, quick recap of our Q3 pricing review last Thursday."},
      {"email_id": "id_2", "text": "Team, I wanted to share a Q4 product roadmap update."},
      {"email_id": "id_3", "text": "Please find attached the vendor contract for your signature."}
    ]
  }' \
  response.json && cat response.json
```

The orchestrator does not call the Lambda this way — it goes through
the Evaluator agent. This is a debugging path only.

---

## 6. Operations

### Cost

At rest: **$0** (no provisioned concurrency, no always-on
infrastructure, no ECR repo).

Per invocation:

- Lambda (warm, 3 references): ~$0.000002 at 512 MB × 50 ms.
- Bedrock (Nemotron 3 Super, ≤128 output tokens): ~$0.0001.

| Usage | Monthly cost (Lambda + Bedrock) |
|---|---|
| 100 invocations | ~$0.01 |
| 1,000 invocations | ~$0.10 |
| 10,000 invocations | ~$1.00 |

The agent layer is the cost driver, not the Lambda. If you skip the
LLM explanation (use `llm="mock"`) the cost collapses back to
~$0.00005 per 1k invocations.

See [`SYNTHESIS.md`](./SYNTHESIS.md) for the cost comparison against
the BERTScore container design this replaced.

### Tuning

Neither component has runtime config (no env vars). Tuning is a code
change by design:

| What | Where |
|---|---|
| Confidence gap thresholds | `lambda/handler.py` → `_CONFIDENCE_HIGH_GAP`, `_CONFIDENCE_MEDIUM_GAP` |
| Verdict thresholds | `lambda/handler.py` → `_VERDICT_NEAR_DUPLICATE`, `_VERDICT_RELATED` |
| TF-IDF n-gram range, stopwords, tokeniser | `lambda/handler.py` → `_tfidf_cosine()` |
| Lambda memory/timeout | `infra/stacks/similarity_stack.py` |
| Evaluator LLM model | `evaluator_agent/config.py` → `EVALUATOR_MODEL_ID` (swap `nvidia.nemotron-super-3-120b` ↔ `zai.glm-5`) |
| System prompt | `evaluator_agent/config.py` → `EVALUATOR_SYSTEM_PROMPT` |
| Mock boilerplate detector | `evaluator_agent/agent.py` → `_BOILERPLATE_TERMS` |

After changing any Lambda-side parameter, re-run `local_test/run_test.py`
to sanity-check the scores. After changing any agent-side parameter,
re-run `evaluator_agent/run_local.py` (mock *and* `--live-llm`) to
confirm both explanations still read right.

---

## 7. Further reading

- [`PLAN.md`](./PLAN.md) — architecture decisions, response schema,
  integration contract.
- [`SYNTHESIS.md`](./SYNTHESIS.md) — decision trail from the original
  BERTScore proposal to the current TF-IDF design.
- [`evaluator_agent/README.md`](./evaluator_agent/README.md) — agent
  internals, prompt design, output contract field-by-field.
