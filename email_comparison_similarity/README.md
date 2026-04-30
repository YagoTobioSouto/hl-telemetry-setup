# Email Similarity Service

Two components working together:

1. **Similarity Lambda** (`lambda/`) — stateless, zip-packaged AWS Lambda.
   Computes TF-IDF cosine + ROUGE-L scores between a drafted email and a
   set of reference emails retrieved from the knowledge base. Returns a
   ranked list of per-reference scores plus a deterministic `evidence`
   block explaining the closest match (top TF-IDF shared terms, longest
   contiguous shared phrase, originality ratio).

2. **Similarity Evaluator agent** (`evaluator_agent/`) — Strands agent that
   sits between the Reviewer and Serializer in the Copycraft pipeline. Calls
   the Lambda (directly, not as a Strands tool — see
   `evaluator_agent/README.md § Why no tool?` for why), then uses Nemotron 3
   Super or GLM 5 via Bedrock to write a one-sentence *informational*
   description of the similarity. Returns the full structured evaluation as
   Pydantic (and therefore as JSON).

Intended usage: the Strands pipeline invokes the evaluator agent after the
Reviewer produces a final draft. The evaluator returns
`SimilarityEvaluation` (ranked references + verdict + evidence +
explanation) which the Serializer forwards to the UI. Scores are
**informational only** — they surface alongside the draft so the
copywriter can see how close their output sits to the retrieved sources.
They do not gate the response, do not trigger rewrite, and do not flag
for human review.

See [`PLAN.md`](./PLAN.md) for the architecture rationale and
[`SYNTHESIS.md`](./SYNTHESIS.md) for the full decision trail from the
original BERTScore proposal to the current TF-IDF design.

## Layout

```
email_comparison_similarity/
├── PLAN.md                       Architecture & design decisions
├── SYNTHESIS.md                  How we got here (decision trail)
├── README.md                     This file
├── fixtures/                     Shared test emails, one source of truth
│   ├── candidate.txt
│   └── references/
│       ├── id_1.txt
│       ├── id_2.txt
│       └── id_3.txt
├── lambda/                       AWS Lambda — TF-IDF + ROUGE-L scoring
│   ├── handler.py
│   └── requirements.txt
├── infra/                        CDK stack — eu-west-2, zip packaging
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/similarity_stack.py
├── local_test/                   Lambda-only smoke test (no agent)
│   ├── run_test.py
│   └── requirements.txt
└── evaluator_agent/              Strands agent — calls Lambda + LLM
    ├── README.md                 Agent-specific docs
    ├── agent.py                  evaluate_similarity() entry point
    ├── config.py                 Model, region, prompt (Nemotron/GLM)
    ├── schemas.py                Pydantic: SimilarityEvaluation
    ├── similarity_client.py      Lambda invoker (local or live)
    ├── run_local.py              JSON-only runner
    └── requirements.txt
```

## Demo from a clean checkout

There are two things you can run, and they answer different questions:

| Runner | Answers | What it exercises |
|---|---|---|
| `local_test/run_test.py` | "Are the scoring numbers right?" | Lambda handler alone, no agent |
| `evaluator_agent/run_local.py` | "Does the agent produce the right JSON?" | Full pipeline: Lambda call + LLM (or mock) + Pydantic |

Most of the time you want the agent runner. The Lambda runner is there
for when something looks wrong and you need to bisect *where*.

### 1. Agent — mock mode (default, no AWS creds)

Deterministic templated explanation. Good for fast iteration.

```bash
cd evaluator_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r ../lambda/requirements.txt   # scikit-learn, rouge-score

python run_local.py
```

Outputs valid JSON straight to stdout. Pipe it through `jq` if you like:

```bash
python run_local.py | jq .
```

### 2. Agent — live LLM (Bedrock Nemotron 3 Super in eu-west-2)

Needs AWS creds with `bedrock:InvokeModel` for
`nvidia.nemotron-super-3-120b` in `eu-west-2`. Still uses the Lambda
in-process, so no Lambda deployment needed.

```bash
python run_local.py --live-llm
```

To compare the LLM against the deterministic mock explanation:

```bash
python run_local.py        | jq -r .explanation    # templated
python run_local.py --live-llm | jq -r .explanation   # Nemotron
```

### 3. Agent — full live (Bedrock + deployed Lambda)

Production shape. Needs `lambda:InvokeFunction` on
`copycraft-similarity-handler` *and* Bedrock. Deploy the Lambda first
(see § Deploy below).

```bash
python run_local.py --live-llm --live-lambda
```

### 4. Lambda only

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
`fixtures/candidate.txt` and the files under `fixtures/references/` in
your normal editor. Both runners pick up changes immediately — no JSON
escaping, no config files.

Filename (sans `.txt`) in `references/` becomes the `email_id`.

## Prerequisites

- Python 3.12+
- Docker (CDK bundles the Lambda zip inside a Lambda-compatible Linux
  container to get the right compiled wheels for numpy/scipy)
- AWS CLI configured with credentials for the target account
- AWS CDK v2 CLI: `npm install -g aws-cdk`
- CDK bootstrapped in the target account/region:
  `cdk bootstrap aws://ACCOUNT/eu-west-2`

## Deploy (Lambda)

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

## Invoke the Lambda directly

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

From the Strands agent layer, you'd use the evaluator agent rather
than raw Lambda invocation — see `evaluator_agent/README.md`.

## Cost

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
LLM explanation (use mock mode) the cost collapses back to ~$0.00005
per 1k invocations.

See [`SYNTHESIS.md`](./SYNTHESIS.md) for the full cost comparison
against the BERTScore container design this replaced.

## Tuning

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
