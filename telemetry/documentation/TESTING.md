# Testing

How to exercise the telemetry pipeline before the front-end harness exists.
Three independent layers, smallest to largest. Pick the layer that matches
what you're trying to prove.

---

## Layer 0 — Synth-time verification (always run this first)

Proves the CDK code produces a valid CloudFormation template. Catches
TypeScript/Python syntax errors, missing imports, bad property names, and
CDK deprecation warnings. Runs in seconds, no AWS calls.

```bash
cd infra
source .venv/bin/activate   # if not already in the venv
cdk synth
```

**What success looks like:** the command prints a YAML template and exits 0.

**Quick checks on the output** (useful when auditing contract compliance):

```bash
# Lifecycle rules — should show 365-day retention on both prefixes
cdk synth 2>/dev/null | grep -E '(ExpirationInDays|Prefix:)'

# IAM — should show the Comprehend grant
cdk synth 2>/dev/null | grep -E 'DetectPiiEntities'

# Authorizer — should be NONE when user_pool is not passed (dev default),
# COGNITO_USER_POOLS when it is
cdk synth 2>/dev/null | grep -E 'AuthorizationType'
```

Run this after every code change. It's the cheapest signal.

---

## Layer A — Lambda unit tests (local, fast, no AWS)

Tests the handler logic with mocked `boto3`. Proves route dispatching, key
construction, and validation paths without needing a deployed stack.

### Setup

```bash
# From project root
pip install pytest                 # if not already installed
```

(You can add `pytest` to a `lambdas/requirements-dev.txt` later — keeping it
out of `infra/requirements.txt` avoids bloating the Lambda deployment bundle.)

### Example test file

Create `lambdas/test_feedback_handler.py`:

```python
import json
import os
from unittest.mock import MagicMock, patch

os.environ["BUCKET_NAME"] = "test-bucket"


def _make_event(resource: str, body: dict) -> dict:
    """Synthesise an API Gateway proxy event."""
    return {
        "resource": resource,
        "path": resource,
        "httpMethod": "POST",
        "body": json.dumps(body),
        "headers": {"Content-Type": "application/json"},
    }


def test_edit_decision_writes_correct_key():
    with patch("boto3.client") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3

        # Import AFTER patching so the module-level boto3.client picks up the mock
        from feedback_handler import handler

        event = _make_event(
            "/api/feedback/edit-decision",
            {
                "sessionId": "abc-1234",
                "issueId": "iss-001",
                "action": "accept",
                "severity": "Med",
            },
        )
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_s3.put_object.assert_called_once()
        call = mock_s3.put_object.call_args.kwargs
        assert call["Bucket"] == "test-bucket"
        assert call["Key"].startswith("edit-decisions/year=")
        assert call["Key"].endswith("abc-1234_iss-001.json")


def test_rating_writes_correct_key():
    with patch("boto3.client") as mock_boto:
        mock_s3 = MagicMock()
        mock_boto.return_value = mock_s3

        from feedback_handler import handler

        event = _make_event(
            "/api/feedback/rating",
            {"sessionId": "abc-1234", "score": 1, "userId": "u-1"},
        )
        result = handler(event, None)

        assert result["statusCode"] == 200
        call = mock_s3.put_object.call_args.kwargs
        assert call["Key"].endswith("abc-1234.json")
        assert "ratings/year=" in call["Key"]


def test_missing_session_id_returns_400():
    with patch("boto3.client"):
        from feedback_handler import handler

        event = _make_event("/api/feedback/edit-decision", {})
        result = handler(event, None)
        assert result["statusCode"] == 400


def test_unknown_resource_returns_400():
    with patch("boto3.client"):
        from feedback_handler import handler

        event = _make_event("/api/feedback/bogus", {"sessionId": "x"})
        result = handler(event, None)
        assert result["statusCode"] == 400
```

### Run

```bash
cd lambdas
pytest -v
```

### What this covers / doesn't

Covered:

- Route dispatching via `event["resource"]`
- S3 key format + time partitioning
- Missing-field error paths
- Response envelope shape

Not covered (needs Layer B):

- IAM permissions
- API Gateway request mapping
- Real S3 writes
- CORS preflight

---

## Layer B — Deployed-stack smoke test (proves the whole pipe)

Deploys the real stack and hits it with `curl`. This is the highest-value
test — it catches wiring mistakes that unit tests can't.

### Prerequisites

- AWS credentials in the environment (`aws sts get-caller-identity` works).
- CDK bootstrapped in the target account/region:
  `cdk bootstrap aws://ACCOUNT_ID/REGION`.

### Deploy

```bash
cd infra
source .venv/bin/activate
cdk deploy --require-approval never
```

Note the two `Outputs` the stack prints at the end:

```
TelemetryStack.ApiUrl     = https://xxxxx.execute-api.eu-west-2.amazonaws.com/prod/
TelemetryStack.BucketName = telemetrystack-telemetrybuckETxxxxx-YYYYYYYYY
```

### Smoke-test script

Either run the commands below by hand, or save this as
`scripts/smoke-test.sh` and run it after each deploy.

```bash
#!/usr/bin/env bash
set -euo pipefail

STACK=${STACK:-TelemetryStack}

API_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiUrl`].OutputValue' \
  --output text)

BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' \
  --output text)

SESSION_ID="smoke-$(date +%s)"

echo "--- Testing /api/feedback/edit-decision ---"
curl -s -X POST "${API_URL}api/feedback/edit-decision" \
  -H "Content-Type: application/json" \
  -d "{
    \"sessionId\": \"$SESSION_ID\",
    \"invocationId\": \"inv-1\",
    \"issueId\": \"iss-001\",
    \"action\": \"accept\",
    \"severity\": \"Med\",
    \"category\": \"Clarity\",
    \"issueText\": \"test issue text\",
    \"suggestionText\": \"test suggestion\",
    \"sourceAgent\": \"fin-proms\",
    \"userId\": \"smoke-user\"
  }" | jq .

echo "--- Testing /api/feedback/rating ---"
curl -s -X POST "${API_URL}api/feedback/rating" \
  -H "Content-Type: application/json" \
  -d "{
    \"sessionId\": \"$SESSION_ID\",
    \"score\": 1,
    \"userId\": \"smoke-user\",
    \"comment\": \"smoke test rating\"
  }" | jq .

echo "--- Listing S3 objects ---"
aws s3 ls "s3://${BUCKET}/" --recursive | grep "$SESSION_ID"
```

### What success looks like

Both curl calls return:

```json
{ "message": "ok", "key": "edit-decisions/year=2026/month=04/day=29/smoke-xxxxx_iss-001.json" }
```

And the S3 listing shows two new objects, one per route.

### Inspect an object

```bash
aws s3 cp "s3://${BUCKET}/ratings/year=2026/month=04/day=29/${SESSION_ID}.json" -
```

### CORS preflight check (for when the front-end connects)

```bash
curl -i -X OPTIONS "${API_URL}api/feedback/edit-decision" \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: Content-Type,Authorization"
```

You should see `Access-Control-Allow-Origin: *` and the allowed methods in
the response headers.

---

## Layer C — What to test later (not now)

These are the things that need Layer A+B plus the front-end harness:

| Test                                      | Unlocks when…                          |
| ----------------------------------------- | -------------------------------------- |
| Cognito 401 on unauth request             | `user_pool` is wired in                |
| PII redaction actually removes PII        | Comprehend code is un-stubbed          |
| Validation rejects invalid enums          | Validation layer lands (see `contract-conflicts.md` §6) |
| Session accumulator produces full payload | Front-end `FeedbackProvider` is built  |
| AGUI streaming                            | AgentCore runtime integration is done  |

Track these as they unlock. None of them block the current Phase 1 work.

---

## Recommended workflow

For small changes (handler tweaks, stack edits):

```
Layer 0 (cdk synth)  →  Layer A (pytest)
```

For anything that touches IAM, API Gateway, or S3 wiring:

```
Layer 0  →  Layer A  →  cdk deploy  →  Layer B smoke test
```

Layer B takes ~2 minutes end-to-end once you've deployed once. Worth it
whenever the change could plausibly break the pipe.
