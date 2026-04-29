# Telemetry Pipeline — Implementation Plan

## Goal

Build Phase 1 infrastructure for the telemetry pipeline: an S3 bucket for feedback data, a single Lambda that handles both feedback endpoints, and an API Gateway to expose them.

---

## What We're Building

```
POST /api/feedback/edit-decision ─┐
                                  ├─→ Single Lambda ─→ PII redact (later) ─→ S3 JSON
POST /api/feedback/rating ────────┘
```

Three AWS resources in one CDK stack:

1. **S3 Bucket** — `telemetry-bucket`, SSE-KMS, lifecycle rules, two prefixes
2. **Lambda** — single Python function, routes by API Gateway `resource` field, writes time-partitioned JSON to S3
3. **API Gateway** — REST API, two POST routes, both proxy-integrated to the same Lambda

---

## Project Structure

```
hl-telemetry-setup/
├── infra/                        # CDK app (TypeScript)
│   ├── bin/
│   │   └── app.ts                # CDK entry point
│   ├── lib/
│   │   └── telemetry-stack.ts    # Single stack: S3 + Lambda + API GW
│   ├── cdk.json
│   ├── tsconfig.json
│   └── package.json
├── lambdas/
│   └── feedback_handler.py       # Single handler for both routes
├── PLAN.md
├── README.md
└── Architecture-Reference.png
```

---

## Component Details

### 1. S3 Telemetry Bucket

**CDK construct:** `aws_s3.Bucket`

| Property                | Value                                |
|-------------------------|--------------------------------------|
| `encryption`            | `BucketEncryption.KMS_MANAGED`       |
| `blockPublicAccess`     | `BlockPublicAccess.BLOCK_ALL`        |
| `enforceSSL`            | `true`                               |
| `removalPolicy`         | `RemovalPolicy.DESTROY` (dev)        |
| `autoDeleteObjects`     | `true` (dev)                         |
| `lifecycleRules`        | Expire after 90 days (both prefixes) |

Two logical prefixes (not enforced at bucket level — the Lambda writes to them):
- `edit-decisions/year=YYYY/month=MM/day=DD/`
- `ratings/year=YYYY/month=MM/day=DD/`

### 2. Feedback Lambda (Python 3.12)

**CDK construct:** `aws_lambda.Function`

Single handler, routes by `event["resource"]`:

| Route                              | S3 Prefix         | S3 Key Pattern                          |
|------------------------------------|-------------------|-----------------------------------------|
| `/api/feedback/edit-decision`      | `edit-decisions/` | `year=Y/month=M/day=D/{sessionId}_{issueId}.json` |
| `/api/feedback/rating`             | `ratings/`        | `year=Y/month=M/day=D/{sessionId}.json` |

**How routing works:** API Gateway proxy integration passes the full request as the Lambda event. The `resource` field contains the matched API Gateway resource path (e.g. `/api/feedback/edit-decision`). The Lambda reads this to decide prefix and key shape.

Confirmed from [AWS docs](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html#api-gateway-simple-proxy-for-lambda-input-format) — proxy event structure:
```json
{
  "resource": "/api/feedback/edit-decision",
  "path": "/api/feedback/edit-decision",
  "httpMethod": "POST",
  "headers": { ... },
  "body": "{ ... }",
  ...
}
```

**Response format** (required by proxy integration):
```json
{
  "statusCode": 200,
  "headers": { "Content-Type": "application/json" },
  "body": "{\"message\": \"ok\"}"
}
```

**Environment variables** (set by CDK):
- `BUCKET_NAME` — the telemetry bucket name

**IAM permissions** (granted by CDK):
- `s3:PutObject` on the telemetry bucket

**Comprehend:** Not wired yet. When added later, the Lambda will call `comprehend:DetectPiiEntities` on text fields before writing to S3. The field list per route:
- `edit-decision`: redact `issueText`, `suggestionText`
- `rating`: redact `comment`

### 3. API Gateway (REST API)

**CDK construct:** `aws_apigateway.RestApi` + `LambdaIntegration`

| Property                       | Value                                    |
|--------------------------------|------------------------------------------|
| `restApiName`                  | `telemetry-api`                          |
| `defaultCorsPreflightOptions`  | Allow all origins (dev), POST method     |

**Resource tree:**
```
/api
  /feedback
    /edit-decision  → POST → LambdaIntegration(feedbackHandler)
    /rating         → POST → LambdaIntegration(feedbackHandler)
```

Both POST methods point to the same Lambda. CDK's `LambdaIntegration` defaults to proxy integration, which is what we want — the full request gets passed through.

---

## S3 Data Schemas (from README)

### Edit Decision — one file per issue action

```json
{
  "sessionId": "abc-1234",
  "invocationId": "inv-1",
  "issueId": "iss-001",
  "action": "accept",
  "severity": "Med",
  "category": "Clarity",
  "issueText": "The phrase 'draws to a close' is slightly formal...",
  "suggestionText": "Change to 'Your ISA allowance for this year ends on 5 April.'",
  "sourceAgent": "fin-proms",
  "userId": "user-123",
  "traceId": "otel-trace-abc",
  "timestamp": "2026-04-28T13:00:05Z"
}
```

### Session Rating — one file per session

```json
{
  "sessionId": "abc-1234",
  "timestamp": "2026-04-28T13:02:00Z",
  "score": -1,
  "comment": "Tone was too formal",
  "userId": "user-123",
  "emailType": "ISA Deadline",
  "tone": "Action-Oriented",
  "subjectAngle": "Your ISA allowance expires on 5 April",
  "cta": "Top up your ISA",
  "passes": 3,
  "convergenceResult": "Converged (delta)",
  "wordCount": 195,
  "issuesResolved": 1,
  "issuesAdvisory": 0,
  "issuesUnresolved": 0,
  "totalAccepts": 1,
  "totalDismissals": 0,
  "sourceCount": 3,
  "sourceIds": ["src-001", "src-002", "src-003"],
  "segmentsSelected": ["Brian", "Ellie", "The Harrisons", "High Net Worth"],
  "segmentsPersonalised": 4,
  "exportFormat": "markdown",
  "exportedAt": "2026-04-28T13:01:50Z"
}
```

---

## Build Steps

### Step 1 — Scaffold CDK project

Create `infra/` with:
- `package.json` — dependencies: `aws-cdk-lib`, `constructs`
- `tsconfig.json` — standard CDK TypeScript config
- `cdk.json` — points to `bin/app.ts`
- `bin/app.ts` — instantiates the stack

### Step 2 — Write the Lambda handler

Create `lambdas/feedback_handler.py`:
- Parse `event["resource"]` to determine route
- Parse `event["body"]` (JSON string) for the payload
- Validate required fields per route (`sessionId` always required; `issueId` for edit-decision)
- Build time-partitioned S3 key from current UTC timestamp
- Write JSON to S3 via `boto3` `s3.put_object()`
- Return proxy-compatible response `{ statusCode, headers, body }`

### Step 3 — Define the CDK stack

Create `infra/lib/telemetry-stack.ts`:
1. S3 Bucket with KMS encryption + lifecycle rules
2. Lambda Function pointing at `../lambdas`, handler `feedback_handler.handler`, Python 3.12 runtime
3. Grant the Lambda `PutObject` on the bucket via `bucket.grantPut(fn)`
4. Pass `BUCKET_NAME` as environment variable
5. REST API with resource tree `/api/feedback/edit-decision` and `/api/feedback/rating`
6. Both POST methods use `new LambdaIntegration(fn)` (proxy mode by default)
7. CORS preflight on the `/api/feedback` resource

### Step 4 — Synth and verify

- `cd infra && npm install && npx cdk synth`
- Confirm CloudFormation template has the three resources wired correctly
- Check IAM policy on the Lambda role includes `s3:PutObject`
- Check API Gateway has two POST methods pointing to the same Lambda ARN

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Single Lambda, not two | Both routes do the same thing (validate → write JSON to S3). Only the S3 prefix and key pattern differ. Keeps code DRY, one deployment unit, one log group. |
| REST API, not HTTP API | REST API gives us `resource` field in the proxy event for clean routing. Also aligns with the README architecture (CloudFront → API Gateway → Lambda). |
| `KMS_MANAGED` not custom KMS key | CDK creates and manages the key. No cross-account requirement. Simpler. |
| Python Lambda | Matches the Strands agent code. Comprehend SDK is natural in boto3. |
| No DynamoDB | Write-and-forget analytics data. S3 JSON with time-partitioned keys. Query later via Athena if needed. |
| Comprehend deferred | Optional for now. Lambda structure supports adding it later — just a function call on text fields before the S3 write. |

---

## References

- [CDK v2 `aws_s3.Bucket`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_s3.Bucket.html) — `encryption`, `lifecycleRules`, `blockPublicAccess`
- [CDK v2 `aws_apigateway.RestApi`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_apigateway.RestApi.html) — `addResource`, `addMethod`, `defaultCorsPreflightOptions`
- [CDK v2 `aws_apigateway.LambdaIntegration`](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_apigateway.LambdaIntegration.html) — proxy integration by default
- [API Gateway proxy event format](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html#api-gateway-simple-proxy-for-lambda-input-format) — `resource`, `path`, `httpMethod`, `body`
- [Lambda proxy response format](https://docs.aws.amazon.com/lambda/latest/dg/services-apigateway.html) — `statusCode`, `headers`, `body`
