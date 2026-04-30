# Email Similarity Service

Stateless, zip-packaged AWS Lambda that scores how similar a drafted
email is to a small set of source emails retrieved from the knowledge
base. Returns per-reference **TF-IDF cosine** and **ROUGE-L** scores
plus a summary block identifying the closest match and how confident
that assessment is.

Intended usage: the Strands agent pipeline invokes this Lambda after
the Writer produces a final draft. Scores are **informational only** —
they are surfaced to the user alongside the draft so they can see how
close their output sits to the retrieved source emails. They do not
gate the response, do not trigger rewrite, and do not flag for human
review.

See [`PLAN.md`](./PLAN.md) for the architecture rationale and
[`SYNTHESIS.md`](./SYNTHESIS.md) for the full decision trail from the
original BERTScore proposal to the current TF-IDF design.

## Layout

```
email_comparison_similarity/
├── PLAN.md                       Architecture & design decisions
├── SYNTHESIS.md                  How we got here (decision trail)
├── README.md                     This file
├── lambda/
│   ├── handler.py                Lambda entry point, TF-IDF + ROUGE-L
│   └── requirements.txt          scikit-learn, rouge-score
├── infra/
│   ├── app.py                    CDK entry point (pinned to eu-west-2)
│   ├── cdk.json
│   ├── requirements.txt          aws-cdk-lib, constructs
│   └── stacks/
│       ├── __init__.py
│       └── similarity_stack.py   Zip-packaged Function with bundling
└── local_test/
    ├── run_test.py               Fixtures-driven local runner
    ├── requirements.txt          scikit-learn, rouge-score
    ├── fixtures/                 Default smoke test (no near-duplicate)
    │   ├── candidate.txt
    │   └── references/
    │       ├── id_1.txt
    │       ├── id_2.txt
    │       └── id_3.txt
    └── fixtures_copycat/         Smoke test with one near-duplicate
        ├── candidate.txt
        └── references/
            ├── copycat_source.txt
            ├── roadmap.txt
            └── vendor.txt
```

## Prerequisites

- Python 3.12+
- Docker (CDK bundles the zip inside a Lambda-compatible Linux
  container to get the right compiled wheels for numpy/scipy)
- AWS CLI configured with credentials for the target account
- AWS CDK v2 CLI: `npm install -g aws-cdk`
- CDK bootstrapped in the target account/region:
  `cdk bootstrap aws://ACCOUNT/eu-west-2`

## Local testing

The handler is importable directly from the repo — no container, no
AWS, no Lambda runtime emulator. Everything runs in ~10 ms per call
against hand-edited text fixtures.

```bash
cd local_test
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Score the default fixture set (all unrelated references)
python run_test.py

# Score the copycat fixture set (one near-duplicate)
python run_test.py --fixtures fixtures_copycat

# Bring your own fixtures directory
python run_test.py --fixtures /path/to/your/emails

# Or bypass fixtures entirely and pass raw JSON
python run_test.py --payload my_payload.json
```

The fixtures layout is:

```
<dir>/
  candidate.txt                Draft email (stand-in for Writer output)
  references/
    <email_id>.txt             One file per source email;
                                filename (sans .txt) = email_id
```

Edit the text files in your normal editor — full multi-line emails,
signatures, line breaks, whatever — and re-run `run_test.py`. No
JSON escaping, no YAML, no config files.

**Expected output shape:**

```
Running handler (TF-IDF + ROUGE-L, should be sub-second)...

Status: 200
Time:   0.01s

Response body:
{
  "references": [
    {
      "email_id":      "copycat_source",
      "rank":          1,
      "similarity":    0.5321,
      "rouge_l":       0.6381,
      "tfidf_cosine":  0.5321,
      "relative_share":0.8706
    },
    {...}, {...}
  ],
  "candidate_summary": {
    "closest_match":      "copycat_source",
    "closest_similarity": 0.5321,
    "confidence":         "high"
  }
}
```

Unrelated references will sit in `similarity=0.01-0.05` with
`confidence=low`. A near-duplicate will jump to `similarity>0.3` with
`confidence=high` and typically >70% `relative_share`. See
[`PLAN.md § 4`](./PLAN.md) for the full field-by-field schema.

## Deploy

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

On success the stack outputs the Lambda function name and ARN.
Build artefacts land in `infra/cdk.out/`.

## Invoke

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

From the Strands agent service (Python):

```python
import json
import boto3

lambda_client = boto3.client("lambda", region_name="eu-west-2")

resp = lambda_client.invoke(
    FunctionName="copycraft-similarity-handler",
    InvocationType="RequestResponse",
    Payload=json.dumps({
        "candidate": draft_text,
        "references": source_emails,
    }).encode(),
)

body = json.loads(resp["Payload"].read())
scores = json.loads(body["body"])
# scores["references"] is already ranked
# scores["candidate_summary"]["closest_match"] is the top email_id
# scores["candidate_summary"]["confidence"] is "high" | "medium" | "low"
```

## Cost

At rest: **$0** (no provisioned concurrency, no always-on
infrastructure, no ECR repo).

Per invocation (warm, 3 references): ~$0.000002 at 512 MB × 50 ms.

| Usage | Monthly cost |
|---|---|
| 100 invocations | ~$0.000005 |
| 1,000 invocations | ~$0.00005 |
| 10,000 invocations | ~$0.0005 |

Roughly **100× cheaper** than the BERTScore container the service
previously ran on. See [`SYNTHESIS.md`](./SYNTHESIS.md) for the full
cost comparison and why we switched.

## Tuning

The handler is deliberately parameter-free — no env vars, no runtime
config. If you want to change something, it's a code change:

| What | Where |
|---|---|
| Confidence gap thresholds (high=0.15, medium=0.05) | `handler.py` → `_CONFIDENCE_HIGH_GAP`, `_CONFIDENCE_MEDIUM_GAP` |
| TF-IDF n-gram range, stopwords, tokeniser | `handler.py` → `_tfidf_cosine()` |
| Lambda memory/timeout | `infra/stacks/similarity_stack.py` → `memory_size`, `timeout` |

After changing any of these, re-run `local_test/run_test.py` against
both fixture sets before deploying to sanity-check the scores still
behave.
