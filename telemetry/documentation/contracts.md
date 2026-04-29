# Telemetry interface contracts

Interface contracts for the [hl-telemetry-setup](../../dev/expr/hl-telemetry-setup) design doc. Everything an implementer needs to wire up backend OTEL tracing, feedback APIs, S3 storage, frontend types, and CDK resources against the existing copycraft codebase.

## Problem

All user feedback (thumbs up/down, annotation accept/dismiss) is localStorage-only. No backend telemetry exists. Agent traces aren't exported. There's no way to correlate user satisfaction with agent performance.

## Approach

Three integration surfaces:

1. **Backend OTEL** — Enable Strands' built-in tracing, propagate `sessionId` through to CloudWatch/X-Ray via ADOT on AgentCore.
2. **Feedback API** — Two new Lambda-backed endpoints behind API Gateway for edit decisions and session ratings, with Comprehend PII redaction, writing to S3.
3. **Frontend feedback harness** — `FeedbackProvider` context + `useFeedback()` hook + `<FeedbackHarness>` wrapper, wired to existing `AnnotationsPanel`, `FeedbackWidget`, and `ActionBar` components.

---

## 1. Backend OTEL — Strands trace attributes

### Dependency change

File: `agent/pyproject.toml`

```diff
-    "strands-agents >= 1.13.0",
+    "strands-agents[otel] >= 1.13.0",
```

Also add to `agent/requirements.txt`:

```
aws-opentelemetry-distro>=0.10.0
```

### Telemetry init

File: `agent/src/main.py` — add before `app = AGUIApp()`:

```python
from strands.telemetry import StrandsTelemetry

StrandsTelemetry().setup_otlp_exporter().setup_meter(enable_otlp_exporter=True)
```

No endpoint env var needed — ADOT on AgentCore handles collection automatically.

### Trace attributes on agent invocations

File: `agent/src/orchestrator.py` — pass `trace_attributes` when creating each agent.

The existing agent factories (`agents/researcher.py`, `agents/copywriter.py`, etc.) create `strands.Agent` instances. Add a `trace_attributes` dict:

```python
trace_attrs = {
    "session.id": session_id,
    "invocation.id": invocation_id,
    "user.id": user_id,
    "brief.email_type": brief.email_type.value,
    "brief.tone": brief.tone.value,
}
```

Pass to each `Agent(trace_attributes=trace_attrs)` call.

### Session ID propagation

The spec introduces a `sessionId` concept (persists across Generate + Personalise clicks). Currently the frontend generates a fresh `threadId` per request. Change:

- **First `/invocations` call**: backend generates `sessionId` (UUID) if not provided in `forwarded_props`.
- **Subsequent calls** (refine, personalise): frontend passes `sessionId` back in `forwarded_props`.
- Backend sets OTEL baggage: `baggage.set_baggage("session.id", session_id)`.
- Backend sets AgentCore header: `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`.

### New SSE event: `session_init`

Emitted once at the start of the first run, before `RUN_STARTED`:

```python
# In orchestrator.py, at the top of run_pipeline()
yield CustomEvent(
    type=EventType.CUSTOM,
    name="session_init",
    value={
        "session_id": session_id,
        "invocation_id": invocation_id,
        "trace_id": trace_id,  # from opentelemetry.trace.get_current_span().get_span_context().trace_id
    },
)
```

Frontend SSE type addition in `sse.ts`:

```typescript
| { type: "CUSTOM"; name: "session_init"; value: { session_id: string; invocation_id: string; trace_id: string } }
```

---

## 2. Feedback API — Lambda contracts

Two separate Lambda functions behind API Gateway. Both follow the same pattern: validate → redact PII → write to S3.

### 2a. Edit decision endpoint

```
POST /api/feedback/edit-decision
Content-Type: application/json
Authorization: Bearer <cognito-token>
```

**Request body:**

```json
{
  "sessionId": "abc-1234",
  "invocationId": "inv-1",
  "issueId": "iss-001",
  "action": "accept",
  "severity": "high",
  "category": "fca_compliance",
  "issueText": "The phrase 'draws to a close' is slightly formal...",
  "suggestionText": "Change to 'Your ISA allowance for this year ends on 5 April.'",
  "sourceAgent": "fin_proms",
  "userId": "user-123",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

**Validation rules:**

| Field            | Type   | Required | Constraints                                                                   |
| ---------------- | ------ | -------- | ----------------------------------------------------------------------------- |
| `sessionId`      | string | yes      | UUID format                                                                   |
| `invocationId`   | string | yes      | non-empty                                                                     |
| `issueId`        | string | yes      | non-empty                                                                     |
| `action`         | string | yes      | one of: `accept`, `dismiss`                                                   |
| `severity`       | string | yes      | one of: `high`, `medium`, `low`                                               |
| `category`       | string | yes      | one of: `fca_compliance`, `brand_voice`, `product_accuracy`, `clarity`, `cta` |
| `issueText`      | string | yes      | max 2000 chars                                                                |
| `suggestionText` | string | yes      | max 2000 chars                                                                |
| `sourceAgent`    | string | yes      | one of: `researcher`, `copywriter`, `editor`, `fin_proms`                     |
| `userId`         | string | yes      | non-empty                                                                     |
| `traceId`        | string | no       | 32-char hex if present                                                        |

**PII redaction fields:** `issueText`, `suggestionText`

**S3 write path:** `s3://{BUCKET}/edit-decisions/year={YYYY}/month={MM}/day={DD}/{sessionId}_{issueId}.json`

**S3 object body:** Same as request body, with `issueText` and `suggestionText` redacted + `timestamp` added (ISO 8601).

**Response:**

```json
// 200
{ "status": "ok" }

// 400
{ "error": "Invalid action: must be accept or dismiss" }

// 500
{ "error": "Internal server error" }
```

### 2b. Session rating endpoint

```
POST /api/feedback/rating
Content-Type: application/json
Authorization: Bearer <cognito-token>
```

**Request body:**

```json
{
  "sessionId": "abc-1234",
  "score": 1,
  "comment": "Tone was too formal",
  "userId": "user-123",

  "emailType": "isa_deadline",
  "tone": "action_oriented",
  "subjectAngle": "Your ISA allowance expires on 5 April",
  "cta": "Top up your ISA",

  "passes": 3,
  "convergenceResult": "CONVERGED_BY_DELTA",
  "wordCount": 195,

  "issuesResolved": 1,
  "issuesAdvisory": 0,
  "issuesUnresolved": 0,
  "totalAccepts": 1,
  "totalDismissals": 0,

  "sourceCount": 3,
  "sourceIds": ["src-001", "src-002", "src-003"],

  "segmentsSelected": ["brian", "ellie", "harrisons", "hnw"],
  "segmentsPersonalised": 4,

  "exportFormat": "markdown",
  "exportedAt": "2026-04-28T13:01:50Z"
}
```

**Validation rules:**

| Field                  | Type     | Required | Constraints                                                                                                  |
| ---------------------- | -------- | -------- | ------------------------------------------------------------------------------------------------------------ |
| `sessionId`            | string   | yes      | UUID format                                                                                                  |
| `score`                | integer  | yes      | `1` (thumbs up) or `-1` (thumbs down)                                                                        |
| `comment`              | string   | no       | max 500 chars                                                                                                |
| `userId`               | string   | yes      | non-empty                                                                                                    |
| `emailType`            | string   | yes      | one of: `isa_deadline`, `market_update`, `product_education`, `regulatory_notice`, `onboarding`, `retention` |
| `tone`                 | string   | yes      | one of: `reassuring_expert`, `plain_english`, `action_oriented`, `formal_regulatory`                         |
| `subjectAngle`         | string   | no       | max 200 chars                                                                                                |
| `cta`                  | string   | yes      | max 500 chars                                                                                                |
| `passes`               | integer  | yes      | 1–3                                                                                                          |
| `convergenceResult`    | string   | yes      | one of: `CONVERGED`, `CONVERGED_BY_DELTA`, `MAX_PASSES_REACHED`                                              |
| `wordCount`            | integer  | yes      | >= 0                                                                                                         |
| `issuesResolved`       | integer  | yes      | >= 0                                                                                                         |
| `issuesAdvisory`       | integer  | yes      | >= 0                                                                                                         |
| `issuesUnresolved`     | integer  | yes      | >= 0                                                                                                         |
| `totalAccepts`         | integer  | yes      | >= 0                                                                                                         |
| `totalDismissals`      | integer  | yes      | >= 0                                                                                                         |
| `sourceCount`          | integer  | yes      | >= 0                                                                                                         |
| `sourceIds`            | string[] | yes      | array of non-empty strings                                                                                   |
| `segmentsSelected`     | string[] | no       | valid segment IDs: `harrisons`, `susan`, `brian`, `raj`, `ellie`, `hnw`                                      |
| `segmentsPersonalised` | integer  | no       | >= 0                                                                                                         |
| `exportFormat`         | string   | no       | one of: `plain`, `metadata`, `markdown`, `html`                                                              |
| `exportedAt`           | string   | no       | ISO 8601 if present                                                                                          |

**PII redaction fields:** `comment`

**S3 write path:** `s3://{BUCKET}/ratings/year={YYYY}/month={MM}/day={DD}/{sessionId}.json`

**S3 object body:** Same as request body, with `comment` redacted + `timestamp` added.

**Response:** Same as edit-decision endpoint.

---

## 3. Frontend types

### New types

File: `frontend/lib/types.ts` — add:

```typescript
// --- Feedback types ---

export type AnnotationAction = "accept" | "dismiss";
export type ExportFormat = "plain" | "metadata" | "markdown" | "html";

export interface EditDecisionPayload {
  sessionId: string;
  invocationId: string;
  issueId: string;
  action: AnnotationAction;
  severity: Annotation["severity"];
  category: Annotation["category"];
  issueText: string;
  suggestionText: string;
  sourceAgent: StepName;
  userId: string;
  traceId?: string;
}

export interface RatingPayload {
  sessionId: string;
  score: 1 | -1;
  comment?: string;
  userId: string;

  emailType: EmailType;
  tone: ToneType;
  subjectAngle?: string;
  cta: string;

  passes: number;
  convergenceResult: RunResult["convergence_reason"];
  wordCount: number;

  issuesResolved: number;
  issuesAdvisory: number;
  issuesUnresolved: number;
  totalAccepts: number;
  totalDismissals: number;

  sourceCount: number;
  sourceIds: string[];

  segmentsSelected?: string[];
  segmentsPersonalised?: number;

  exportFormat?: ExportFormat;
  exportedAt?: string;
}

export interface SessionTelemetry {
  sessionId: string;
  invocationId: string;
  traceId: string;
}
```

### SSE event addition

File: `frontend/lib/sse.ts` — add to the `SSEEvent` union:

```typescript
| { type: "CUSTOM"; name: "session_init"; value: SessionTelemetry }
```

### FeedbackContext shape

File: `frontend/lib/FeedbackContext.tsx` (new file):

```typescript
export interface FeedbackContextValue {
  /** Set after first generate response */
  session: SessionTelemetry | null;

  /** Accumulated session state */
  totalAccepts: number;
  totalDismissals: number;
  segmentsSelected: string[];
  exportFormat: ExportFormat | null;
  exportedAt: string | null;

  /** Actions */
  setSession: (s: SessionTelemetry) => void;
  trackDecision: (
    payload: Omit<
      EditDecisionPayload,
      "sessionId" | "invocationId" | "userId" | "traceId"
    >,
  ) => Promise<void>;
  trackPersonalisation: (segments: string[]) => void;
  trackExport: (format: ExportFormat) => void;
  submitRating: (score: 1 | -1, comment?: string) => Promise<void>;
}
```

### useFeedback hook

File: `frontend/lib/useFeedback.ts` (new file):

```typescript
export function useFeedback(): FeedbackContextValue;
```

Reads `FeedbackContext`. The `trackDecision` method:

1. Reads `sessionId`, `invocationId`, `traceId` from context.
2. Reads `userId` from `AuthContext`.
3. POSTs to `/api/feedback/edit-decision`.
4. Increments `totalAccepts` or `totalDismissals` in context.

The `submitRating` method:

1. Assembles full `RatingPayload` from context state + current `RunResult` + `Brief`.
2. POSTs to `/api/feedback/rating`.

### API client

File: `frontend/lib/feedback.ts` (new file):

```typescript
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";

export async function postEditDecision(
  payload: EditDecisionPayload,
): Promise<void> {
  const res = await fetch(`${API_URL}/api/feedback/edit-decision`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error((await res.json()).error);
}

export async function postRating(payload: RatingPayload): Promise<void> {
  const res = await fetch(`${API_URL}/api/feedback/rating`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error((await res.json()).error);
}
```

---

## 4. Component wiring

### Existing components to modify

| Component              | Change                                                                                              |
| ---------------------- | --------------------------------------------------------------------------------------------------- |
| `AnnotationsPanel.tsx` | Wire Accept/Dismiss buttons to `useFeedback().trackDecision()` instead of local-only `onAction`     |
| `DraftArea.tsx`        | Same — inline annotation Accept/Dismiss calls `trackDecision()`                                     |
| `FeedbackWidget.tsx`   | Wire thumbs up/down to `useFeedback().submitRating()` instead of local-only `onRate`                |
| `ActionBar.tsx`        | Wire export actions to `useFeedback().trackExport(format)`                                          |
| `SegmentPanel.tsx`     | Wire personalise click to `useFeedback().trackPersonalisation(segments)`                            |
| `app/page.tsx`         | Wrap with `<FeedbackProvider>`, handle `session_init` SSE event to call `setSession()`              |
| `lib/usePipeline.ts`   | Handle `session_init` event in `handleEvent()` reducer, expose `sessionTelemetry` in pipeline state |

### New components

| Component              | Purpose                                                                     |
| ---------------------- | --------------------------------------------------------------------------- |
| `FeedbackProvider.tsx` | React context provider. Wraps the page. Holds session state.                |
| `FeedbackHarness.tsx`  | Thin wrapper per annotation. Props: issue metadata. Renders Accept/Dismiss. |

### FeedbackHarness props

```typescript
interface FeedbackHarnessProps {
  issueId: string;
  severity: Annotation["severity"];
  category: Annotation["category"];
  issueText: string;
  suggestionText: string;
  sourceAgent: StepName;
  children: React.ReactNode;
}
```

---

## 5. CDK resources

File: `infra/stacks/telemetry_stack.py` (new file)

### S3 bucket

```python
bucket = s3.Bucket(
    self, "TelemetryBucket",
    bucket_name=f"copycraft-telemetry-{self.account}",
    encryption=s3.BucketEncryption.KMS_MANAGED,
    enforce_ssl=True,
    lifecycle_rules=[
        s3.LifecycleRule(prefix="edit-decisions/", expiration=Duration.days(365)),
        s3.LifecycleRule(prefix="ratings/", expiration=Duration.days(365)),
    ],
    removal_policy=RemovalPolicy.RETAIN,
)
```

### Feedback Lambdas

Two Lambda functions, identical IAM shape:

```python
edit_decision_fn = lambda_.Function(
    self, "EditDecisionHandler",
    function_name="copycraft-feedback-edit-decision",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="handler.lambda_handler",
    code=lambda_.Code.from_asset("lambda/feedback"),
    environment={
        "TELEMETRY_BUCKET": bucket.bucket_name,
        "FEEDBACK_TYPE": "edit-decision",
    },
    timeout=Duration.seconds(10),
)

rating_fn = lambda_.Function(
    self, "RatingHandler",
    function_name="copycraft-feedback-rating",
    runtime=lambda_.Runtime.PYTHON_3_12,
    handler="handler.lambda_handler",
    code=lambda_.Code.from_asset("lambda/feedback"),
    environment={
        "TELEMETRY_BUCKET": bucket.bucket_name,
        "FEEDBACK_TYPE": "rating",
    },
    timeout=Duration.seconds(10),
)
```

### IAM

Both Lambdas need:

```python
bucket.grant_put(edit_decision_fn)
bucket.grant_put(rating_fn)

edit_decision_fn.add_to_role_policy(iam.PolicyStatement(
    actions=["comprehend:DetectPiiEntities"],
    resources=["*"],
))
# Same for rating_fn
```

### API Gateway

```python
api = apigateway.RestApi(self, "FeedbackApi", rest_api_name="copycraft-feedback")

feedback = api.root.add_resource("api").add_resource("feedback")
feedback.add_resource("edit-decision").add_method("POST", apigateway.LambdaIntegration(edit_decision_fn))
feedback.add_resource("rating").add_method("POST", apigateway.LambdaIntegration(rating_fn))
```

Cognito authorizer (reuse from `AuthStack`):

```python
authorizer = apigateway.CognitoUserPoolsAuthorizer(
    self, "FeedbackAuthorizer",
    cognito_user_pools=[user_pool],
)
# Apply to both methods
```

### CloudFront integration

File: `infra/stacks/frontend_stack.py` — add a new origin behaviour:

```python
# Add API Gateway as an additional origin for /api/feedback/*
distribution.add_behavior(
    "/api/feedback/*",
    origins.RestApiOrigin(api),
    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
)
```

### Wire into `infra/app.py`

```python
from stacks.telemetry_stack import TelemetryStack

telemetry_stack = TelemetryStack(
    app, "CopycraftTelemetry", env=_env,
    user_pool=auth_stack.user_pool,
)
```

---

## 6. Lambda handler contract

File: `lambda/feedback/handler.py` (new file)

Single handler, behaviour driven by `FEEDBACK_TYPE` env var.

```python
def lambda_handler(event: dict, context) -> dict:
    """
    event["body"] is the JSON string from API Gateway.

    Steps:
    1. Parse and validate body against the schema for FEEDBACK_TYPE.
    2. Redact PII fields via Comprehend.
    3. Add "timestamp" (ISO 8601 UTC).
    4. Write JSON to S3 at the time-partitioned key.
    5. Return {"statusCode": 200, "body": '{"status": "ok"}'}.
    """
```

### Comprehend redaction function

```python
def redact_pii(text: str) -> str:
    """Replace PII entities with [REDACTED]. Return original if text is empty."""
    if not text:
        return text
    response = comprehend.detect_pii_entities(Text=text, LanguageCode="en")
    chars = list(text)
    for entity in sorted(response["Entities"], key=lambda e: e["BeginOffset"], reverse=True):
        chars[entity["BeginOffset"]:entity["EndOffset"]] = list("[REDACTED]")
    return "".join(chars)
```

### S3 key generation

```python
def s3_key(feedback_type: str, session_id: str, issue_id: str | None = None) -> str:
    now = datetime.utcnow()
    prefix = f"{feedback_type}s/year={now.year}/month={now.month:02d}/day={now.day:02d}"
    if feedback_type == "edit-decision":
        return f"{prefix}/{session_id}_{issue_id}.json"
    return f"{prefix}/{session_id}.json"
```

---

## Acceptance criteria

1. Given a Generate request, when the backend starts the pipeline, then a `session_init` SSE event is emitted containing `session_id`, `invocation_id`, and `trace_id`.
2. Given the `session_init` event, when the frontend receives it, then `FeedbackContext` stores the session telemetry and all subsequent feedback calls include these IDs.
3. Given a user clicks Accept on an annotation, when `trackDecision()` fires, then a JSON file appears at `s3://{BUCKET}/edit-decisions/year=.../` with PII-redacted `issueText` and `suggestionText`.
4. Given a user clicks 👍 or 👎, when `submitRating()` fires, then a JSON file appears at `s3://{BUCKET}/ratings/year=.../` with the full session metadata and PII-redacted `comment`.
5. Given the agent runs with `strands-agents[otel]` and `trace_attributes` set, then spans appear in CloudWatch/X-Ray with `session.id`, `invocation.id`, and `user.id` attributes.
6. Given a feedback Lambda receives a request with PII in text fields, then Comprehend redacts the PII before the S3 write.
7. Given the CDK stack is deployed, then the S3 bucket exists with KMS encryption, 365-day lifecycle, and the two Lambda functions have `s3:PutObject` + `comprehend:DetectPiiEntities` permissions.
8. Given the frontend submits feedback, when the user is not authenticated, then the API Gateway returns 401.

## Error handling

| Scenario                           | Behaviour                                                                                                   |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Invalid request body               | Lambda returns 400 with `{"error": "<field> validation message"}`                                           |
| Comprehend call fails              | Lambda logs error, writes to S3 with `"[PII_REDACTION_FAILED]"` placeholder, returns 200 (don't block user) |
| S3 write fails                     | Lambda returns 500 with `{"error": "Internal server error"}`, logs full error                               |
| Frontend network error on feedback | `useFeedback()` catches, logs to console, does not show error to user (feedback is best-effort)             |
| Missing `session_init` event       | `trackDecision()` and `submitRating()` no-op if `session` is null                                           |

## Out of scope

- CloudWatch RUM (browser-level page metrics) — separate initiative.
- Frontend OTEL browser SDK (click-level tracing) — separate initiative.
- Athena/QuickSight dashboards over the S3 data.
- DynamoDB Streams or real-time feedback processing.
- Feedback read-back API (this is write-only analytics data).
- Agent prompt/completion content in traces (stripped by ADOT config).

## Dependencies

- `hl-telemetry-setup` design doc (the source of truth for architecture decisions).
- Cognito `UserPool` and `UserPoolClient` from `CopycraftAuth` stack (for API Gateway authorizer).
- AgentCore Runtime with ADOT support (already deployed).
- Comprehend access in `eu-west-2`.

## Open questions

1. **Feedback API routing**: The spec shows a separate API Gateway. Should the feedback endpoints instead be added as routes on the existing AgentCore app (`main.py`), avoiding a second API Gateway? This would simplify CloudFront config but couples feedback to the agent runtime.
2. **Session ID ownership**: The spec says backend generates `sessionId`. Currently the frontend generates `threadId` (UUID) per request. Should we reuse `threadId` as `sessionId` for the first call, or generate a separate ID?
3. **Comprehend cost**: PII redaction adds ~$0.0001 per request. At low volume this is negligible, but should we add a feature flag to disable it in dev?
4. **S3 bucket naming**: The spec says `telemetry-bucket`. Should it follow the existing pattern: `copycraft-telemetry-{account}`?
