# Similarity Evaluator Agent

Strands agent that sits between the Reviewer and the Serializer in
the Copycraft email pipeline. Takes the reviewer's final draft plus
the knowledge-base source emails, asks the similarity Lambda for
deterministic scores + evidence, and composes a one-sentence
*informational* description of the relationship. Returns the full
result as a typed `SimilarityEvaluation` (Pydantic → JSON).

Everything deterministic — the ranked references, the verdict, the
evidence block — is passed through from the Lambda unchanged. The LLM
only writes the `explanation` string, and it can't touch anything
else because its output schema is a single-field `ExplanationOnly`.

```
Reviewer (Final Draft) ─► evaluate_similarity()
                                 │
                                 ├─► invoke_similarity_lambda()
                                 │       └─ TF-IDF + ROUGE-L + evidence
                                 │                 │
                                 │                 ▼
                                 │   (scoring dict passed in prompt)
                                 │                 │
                                 ▼                 ▼
                         Bedrock Agent (Nemotron 3 Super / GLM 5)
                                   └─► writes ONE sentence
                                 │
                                 ▼
                   SimilarityEvaluation (JSON) ─► Serializer ─► UI
```

## Why no Strands `@tool`?

My first version had the Lambda as a Strands `@tool` that the LLM
could call. That broke: Nemotron 3 Super kept calling the tool in a
loop (30+ times per run) instead of producing the final answer. This
is a known weakness of the model — NVIDIA's own docs flag "tool-call
failures that break the execution loop" as one of its dominant
issues, and combining tools with `structured_output` amplifies it.

The deeper fix is architectural: **the Lambda call was never a
decision the LLM needed to make**. It happens exactly once, it's
deterministic, the output always goes in the same place. Making it a
tool was complexity for complexity's sake. So now we call the Lambda
in plain Python *before* the LLM is involved, and feed its output into
the LLM's user prompt as JSON. One Bedrock call, zero tool loops,
same result.

## Layout

```
evaluator_agent/
├── README.md              This file
├── requirements.txt       strands-agents, boto3, pydantic
├── config.py              Model ID, region, system prompt, thresholds
├── schemas.py             Pydantic: SimilarityEvaluation (output contract)
├── similarity_client.py   Plain-function wrapper around the Lambda
├── agent.py               evaluate_similarity() entry point
└── run_local.py           Fixtures-driven runner, JSON-only output
```

Fixtures live at the repo root in `../fixtures/` — shared with
`local_test/` so there's one source of truth for the test emails.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate

# Agent deps
pip install -r requirements.txt

# Also install the Lambda's deps (scikit-learn, rouge-score) so the
# in-process Lambda mode works. These are NOT in requirements.txt on
# purpose — we don't want strands-agents pulling scikit-learn into
# environments that only ever hit the deployed Lambda.
pip install -r ../lambda/requirements.txt
```

## Run

All three modes produce valid JSON to stdout. Library logging goes
to stderr, so piping is always clean.

### Mock mode (default)

Deterministic, offline, no AWS creds. Uses the in-process Lambda
handler and a templated explanation that mirrors the three branches
of the real system prompt (`distinct` / `related` / `near_duplicate`).

```bash
python run_local.py | jq .
```

### Live LLM, local Lambda

Real Bedrock call for the explanation. Lambda stays in-process.
Requires `bedrock:InvokeModel` on `nvidia.nemotron-super-3-120b` in
`eu-west-2`.

```bash
python run_local.py --live-llm | jq .
```

### Full live

Deployed Lambda + real Bedrock. Production shape. Requires
`lambda:InvokeFunction` on `copycraft-similarity-handler` *and*
Bedrock permissions above.

```bash
python run_local.py --live-llm --live-lambda | jq .
```

## Example output

Against the default unrelated-references fixture, live mode:

```json
{
  "references": [
    {
      "email_id": "id_2",
      "rank": 1,
      "similarity": 0.0414,
      "rouge_l": 0.1854,
      "tfidf_cosine": 0.0414,
      "relative_share": 0.5236
    },
    {
      "email_id": "id_3",
      "rank": 2,
      "similarity": 0.0307,
      "rouge_l": 0.132,
      "tfidf_cosine": 0.0307,
      "relative_share": 0.3877
    },
    {
      "email_id": "id_1",
      "rank": 3,
      "similarity": 0.007,
      "rouge_l": 0.0958,
      "tfidf_cosine": 0.007,
      "relative_share": 0.0887
    }
  ],
  "verdict": "distinct",
  "confidence": "low",
  "evidence": {
    "shared_terms": [
      {"term": "15", "weight": 0.0707},
      {"term": "happy", "weight": 0.0707},
      {"term": "share", "weight": 0.0707},
      {"term": "wanted", "weight": 0.0707},
      {"term": "wanted share", "weight": 0.0707}
    ],
    "longest_shared_phrase": {"text": "let me know if", "token_count": 4},
    "candidate_unique_term_ratio": 0.927
  },
  "explanation": "Your draft is distinct from all source emails; the only overlap is generic email filler ('let me know if', 'wanted to share')."
}
```

## Output contract

`evaluate_similarity()` returns `SimilarityEvaluation` (see
`schemas.py` for the full Pydantic definition).

| Field | Type | Source | Description |
|---|---|---|---|
| `references` | `list[Reference]` | Lambda | All scored references, ranked (`references[0]` is the closest match) |
| `references[].email_id` | `str` | Lambda | Passthrough from the request |
| `references[].rank` | `int` | Lambda | 1 = closest, N = furthest |
| `references[].similarity` | `float [0,1]` | Lambda | Headline = TF-IDF cosine |
| `references[].rouge_l` | `float [0,1]` | Lambda | ROUGE-L F-measure |
| `references[].tfidf_cosine` | `float [0,1]` | Lambda | Explicit alias for `similarity` |
| `references[].relative_share` | `float [0,1]` | Lambda | Share of total similarity mass; values across references sum to 1.0 |
| `verdict` | `distinct \| related \| near_duplicate` | Lambda | Absolute-threshold classification of `references[0]` |
| `confidence` | `low \| medium \| high` | Lambda | How decisive the rank-1 vs rank-2 gap is |
| `evidence` | `Evidence \| None` | Lambda | Shared terms, longest shared phrase, originality ratio — for `references[0]` only |
| `explanation` | `str` | **LLM** (or mock) | One sentence, informational, no recommendations |

Evidence is intentionally scoped to the closest match only — see
[`../PLAN.md` § 4.7](../PLAN.md) for the rationale.

## Configuration

Everything tunable lives in `config.py`:

| Setting | Default | Notes |
|---|---|---|
| `EVALUATOR_MODEL_ID` | `"nvidia.nemotron-super-3-120b"` | Swap to `"zai.glm-5"` for GLM |
| `EVALUATOR_REGION` | `"eu-west-2"` | Must have the model available |
| `EVALUATOR_TEMPERATURE` | `0.0` | Reproducible output; do not raise without good reason |
| `EVALUATOR_MAX_TOKENS` | `128` | ~25 words + punctuation |
| `EVALUATOR_SYSTEM_PROMPT` | (long) | The informational-tone guardrails |
| `LAMBDA_FUNCTION_NAME` | `"copycraft-similarity-handler"` | Must match `infra/stacks/similarity_stack.py` |

After editing the prompt, always re-run `--live-llm` against both
the default fixture *and* anything that exercises `related` /
`near_duplicate` verdicts. The failure mode most likely to slip
through is the LLM adding prescriptive language ("consider rewording",
"safe to send") — the current prompt explicitly forbids this, so
watch for regressions there.

## Mock mode as regression tripwire

The mock templater in `agent.py` (`_template_explanation`) implements
the same three-branch logic the system prompt asks the live LLM to
follow, using the same substance heuristics
(`_BOILERPLATE_TERMS` and `_MIN_SUBSTANTIVE_PHRASE_TOKENS`). So:

- Mock output is a reasonable preview of live output — if the mock
  sentence reads wrong for a fixture, the live LLM will probably also
  read wrong for that fixture.
- You can iterate on Lambda-side changes (new evidence fields, new
  thresholds) in mock mode for free, then verify with `--live-llm`.
- When you see the LLM start drifting, check whether the mock
  templater would give you the same sentence with the same drift —
  if yes, the prompt is wrong; if no, the model is wrong.
