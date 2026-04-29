## Overview

End-to-end telemetry pipeline for an agentic copywriting application. The app uses Strands Agents SDK running on **Bedrock AgentCore Runtime**. Users interact with a Next.js front-end that generates email drafts via a multi-agent pipeline, surfaces issues for review, allows segment personalisation, and captures feedback.

---

## Application Flow

```
1. User fills brief (email type, tone, subject/angle, key message, CTA)
2. Clicks "Generate"
3. Multi-agent pipeline runs automatically:
     Researcher → Copywriter (3 auto-passes until convergence) → Editor → Fin Proms
4. Results shown: draft, sources (RAG from S3 Vectors), issues found
5. User Accept/Dismiss on each issue
6. User selects segments → "Personalise for N segments"
     → single additional pass on the final draft
7. User exports (Copy draft | Copy with metadata | Download as Markdown | Copy as HTML)
8. User rates 👍/👎 + optional comment
```

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Next.js Front-End (Single Page)                      │
│                                                        │
│  <FeedbackProvider userId={uid}>                       │
│    │                                                   │
│    │  context: sessionId (from first generate),        │
│    │           userId, currentInvocationId,             │
│    │           currentTraceId, sessionMetadata          │
│    │                                                   │
│    ├── "Generate" click → POST /api/generate           │
│    │   Pipeline: Researcher → Copywriter (3 passes)    │
│    │             → Editor → Fin Proms                  │
│    │   Returns: sessionId, invocationId, traceId,      │
│    │            draft, sources, issues, metadata        │
│    │                                                   │
│    ├── Issues Found                                    │
│    │   ├── <FeedbackHarness> [Accept] [Dismiss]        │
│    │   └── <FeedbackHarness> [Accept] [Dismiss]        │
│    │                                                   │
│    ├── "Personalise for N segments" click               │
│    │   → POST /api/personalise                         │
│    │   → single additional pass on final draft         │
│    │                                                   │
│    ├── Export ▾ (Copy draft | Copy with metadata |     │
│    │            Download as Markdown | Copy as HTML)   │
│    │                                                   │
│    └── <RatingPanel> [👍] [👎] [+ Add comment]         │
│                                                        │
└──────────────────┬─────────────────────────────────────┘
                   │
                   ▼
          ┌────────────────┐
          │   CloudFront    │
          │  /* → Next.js   │
          │  /api/* ────────────────────────┐
          └────────────────┘                │
                                            ▼
                                   ┌─────────────────────────┐
                                   │  API Gateway              │
                                   │                           │
                                   │  POST /api/generate       │
                                   │    → AgentCore Runtime    │
                                   │      (Strands + ADOT)     │
                                   │                           │
                                   │  POST /api/personalise    │
                                   │    → AgentCore Runtime    │
                                   │                           │
                                   │  POST /api/feedback/      │
                                   │    edit-decision           │
                                   │    → Lambda ──┐           │
                                   │       PII     │           │
                                   │     redaction │           │
                                   │    (Comprehend)           │
                                   │               ▼           │
                                   │            S3 write       │
                                   │                           │
                                   │  POST /api/feedback/      │
                                   │    rating                 │
                                   │    → Lambda ──┐           │
                                   │       PII     │           │
                                   │     redaction │           │
                                   │    (Comprehend)           │
                                   │               ▼           │
                                   │            S3 write       │
                                   └───────────┬──────────────┘
                                               │
              ┌────────────────────────────────┼──────────────────┐
              │                                │                   │
              ▼                                ▼                   ▼
    ┌──────────────────┐      ┌──────────────────┐   ┌──────────────────┐
    │  S3               │      │  S3               │   │  CloudWatch/X-Ray │
    │  /edit-decisions/  │      │  /ratings/         │   │  (via ADOT in     │
    │                    │      │                    │   │   AgentCore)      │
    │  one JSON per      │      │  one JSON per      │   │                   │
    │  decision          │      │  session           │   │  GenAI Dashboard  │
    │  time-partitioned  │      │  time-partitioned  │   │  Logs Insights    │
    └──────────────────┘      └──────────────────┘   └───────────────────┘
```

### ID Hierarchy

```
sessionId            ← generated by backend on first /api/generate call
  ├── invocationId   ← one per "Generate" or "Personalise" click
  │     └── issueId  ← one per issue found (Accept/Dismiss)
  └── rating         ← one per session (final verdict)
```

---

## Front-End Feedback Harness (Next.js)

### Component Structure

```
Layout / Page
└── <FeedbackProvider userId={userId}>
        │
        │  context: sessionId (set after first generate), userId,
        │           currentInvocationId, currentTraceId, sessionMetadata
        │  owns API client (talks to API Gateway)
        │  sessionId + metadata populated from first /api/generate response
        │  currentInvocationId updates on each Generate/Personalise response
        │
        ├── Issues Found (after pipeline completes)
        │     ├── <FeedbackHarness issueId="iss-001">
        │     │     └── <IssueCard severity="Med" category="Clarity" />
        │     │           [Accept] [Dismiss]  → useFeedback().trackDecision()
        │     │
        │     └── <FeedbackHarness issueId="iss-002">
        │           └── <IssueCard />
        │
        ├── <SegmentPanel>
        │     → useFeedback().trackPersonalisation(segments)
        │
        ├── <ExportMenu>
        │     → useFeedback().trackExport(format)
        │
        └── <RatingPanel>
              [👍] [👎] [+ Add comment]
              → useFeedback().submitRating(score, comment)
```

### Three Pieces

1. **`<FeedbackProvider>`** — page/layout level, one per session. Holds IDs in React context, owns API client.
2. **`useFeedback()` hook** — `trackDecision()`, `trackPersonalisation()`, `trackExport()`, `submitRating()`. Auto-attaches sessionId, invocationId, traceId.
3. **`<FeedbackHarness>`** — thin wrapper per issue. Renders Accept/Dismiss, wires to hook.

---

## Multi-Invocation Model

```
sessionId
  │
  ├── invocationId=inv-1 (Generate)
  │     Pipeline: Researcher → Copywriter (3 auto-passes) → Editor → Fin Proms
  │     Produces: draft, 3 sources, issues found
  │     User: Accept/Dismiss on issues
  │
  ├── invocationId=inv-2 (Personalise)
  │     Single additional pass on the final draft
  │     Adapts for selected segments (Brian, Ellie, The Harrisons, High Net Worth)
  │
  ├── Export (Copy draft | Copy with metadata | Download as Markdown | Copy as HTML)
  │
  └── Rating (👍/👎 + comment)
```

The 3 auto-passes within "Generate" are internal to the Copywriter agent — the user doesn't interact between them. `invocationId` maps to one user action (Generate or Personalise), not one pass.

---

## S3 Data Schemas

Feedback data goes directly to S3 as JSON files. No DynamoDB — this is write-and-forget analytics data, not read-back application state.

### Edit Decisions

One JSON file per decision, written by the feedback Lambda after PII redaction.

**S3 path:** `s3://telemetry-bucket/edit-decisions/year=YYYY/month=MM/day=DD/{sessionId}_{issueId}.json`

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

### Session Ratings

One JSON file per session, written when the user rates. Contains the full session context.

**S3 path:** `s3://telemetry-bucket/ratings/year=YYYY/month=MM/day=DD/{sessionId}.json`

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

**Not in this file** (lives in agent traces via CloudWatch): `totalTime`, `tokenCount`, `cost`, `model`. These are agent execution metrics — query them in CloudWatch Logs Insights.

### S3 Bucket Structure

```
s3://telemetry-bucket/
├── edit-decisions/
│   └── year=2026/month=04/day=28/
│         ├── abc-1234_iss-001.json
│         └── abc-1234_iss-002.json
│
└── ratings/
    └── year=2026/month=04/day=28/
          └── abc-1234.json
```

Time-partitioned. `sessionId`/`userId` are fields inside the JSON — filter on them at query time.

---

## Agent Traces — AgentCore + ADOT

Strands agents run on **Bedrock AgentCore Runtime** with built-in ADOT. No separate OTel Collector needed.

### How Traces Flow

```
AgentCore Runtime (Strands Agent + ADOT SDK)
  │
  │ Auto-instrumented: agent spans, cycle spans, LLM spans, tool spans
  │ ADOT exports automatically
  ▼
CloudWatch / X-Ray
  ├── GenAI Observability Dashboard (latency, tokens, errors)
  └── CloudWatch Logs Insights (SQL-like queries on trace logs)
```

Traces stay in CloudWatch. Query them directly with Logs Insights.

### Setup

**1. Enable CloudWatch Transaction Search** (one-time):

- CloudWatch console → Application Signals → Transaction Search → Enable
- Check "ingest spans as structured logs"

**2. Add ADOT SDK** to agent dependencies:

```
aws-opentelemetry-distro>=0.10.0
boto3
```

**3. Run with auto-instrumentation:**

```dockerfile
CMD ["opentelemetry-instrument", "python", "main.py"]
```

**4. Propagate session ID** via OTEL baggage:

```python
from opentelemetry import baggage
from opentelemetry.context import attach

ctx = baggage.set_baggage("session.id", session_id)
attach(ctx)
```

Request header for AgentCore:

```
X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <sessionId>
```

**5. Set trace attributes** on the Strands agent:

```python
agent = Agent(
    system_prompt="You are a helpful assistant.",
    trace_attributes={
        "session.id": session_id,
        "invocation.id": invocation_id,
        "user.id": user_id,
    },
)
```

---

## PII Protection

PII can leak into `issueText`, `suggestionText`, `comment` fields (names, emails, addresses from the email copy).

| Layer                         | What                                     | How                                                           |
| ----------------------------- | ---------------------------------------- | ------------------------------------------------------------- |
| **Comprehend redaction**      | `issueText`, `suggestionText`, `comment` | Redact in feedback Lambda before S3 write                     |
| **Trace attribute filtering** | Full prompts/completions in spans        | Configure ADOT to strip raw text, keep token counts + latency |
| **S3 encryption**             | All data at rest                         | SSE-KMS                                                       |
| **S3 lifecycle**              | Data retention                           | Auto-delete after N days                                      |

### Comprehend Redaction (in feedback Lambda)

```python
import boto3

comprehend = boto3.client("comprehend")

def redact_pii(text: str) -> str:
    response = comprehend.detect_pii_entities(Text=text, LanguageCode="en")
    redacted = list(text)
    for entity in sorted(response["Entities"], key=lambda e: e["BeginOffset"], reverse=True):
        redacted[entity["BeginOffset"]:entity["EndOffset"]] = "[REDACTED]"
    return "".join(redacted)
```

---

## Join Key

`sessionId` is the universal join key across all datasets:

| Dataset         | Where sessionId lives                                                                   |
| --------------- | --------------------------------------------------------------------------------------- |
| Agent traces    | `trace_attributes["session.id"]` + `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header |
| Edit decisions  | `sessionId` field in JSON + S3 filename                                                 |
| Session ratings | `sessionId` field in JSON + S3 filename                                                 |

---

## Data Flow Summary

| Data            | Source                             | Path                                   | Storage         |
| --------------- | ---------------------------------- | -------------------------------------- | --------------- |
| Agent traces    | Strands SDK (auto)                 | ADOT → CloudWatch/X-Ray                | CloudWatch Logs |
| Edit decisions  | `<FeedbackHarness>`                | API Gateway → Lambda (PII redact) → S3 | S3 JSON         |
| Session ratings | `<RatingPanel>` + session metadata | API Gateway → Lambda (PII redact) → S3 | S3 JSON         |

---

## Implementation Plan (CDK + Application Code)

### Phase 1: Infrastructure (AWS CDK)

**1.1 — S3 Telemetry Bucket**

- Create `telemetry-bucket` with SSE-KMS encryption
- Add lifecycle rules (e.g. expire after 90/365 days per prefix)
- Prefixes: `edit-decisions/`, `ratings/`

**1.2 — Feedback Lambdas**

- `edit-decision-handler` Lambda — receives issue decision, calls Comprehend for PII redaction, writes JSON to S3
- `rating-handler` Lambda — receives session rating + metadata, calls Comprehend for PII redaction, writes JSON to S3
- IAM roles: S3 `PutObject` on `telemetry-bucket`, Comprehend `DetectPiiEntities`

**1.3 — API Gateway**

- REST API with routes:
  - `POST /api/generate` → AgentCore Runtime integration
  - `POST /api/personalise` → AgentCore Runtime integration
  - `POST /api/feedback/edit-decision` → edit-decision Lambda
  - `POST /api/feedback/rating` → rating Lambda
- CloudFront origin for `/api/*`

**1.4 — AgentCore Observability**

- Enable CloudWatch Transaction Search
- Ensure AgentCore runtime agent has ADOT SDK in dependencies

### Phase 2: Backend (Agent + API)

**2.1 — `/api/generate` response contract**

- Backend generates `sessionId` (UUID) + `invocationId` on first call
- Returns:
  ```json
  {
    "sessionId": "uuid",
    "invocationId": "inv-1",
    "traceId": "otel-trace-id",
    "draft": "...",
    "sources": [{ "id": "src-1", "matchPercent": 87 }],
    "issues": [
      {
        "issueId": "iss-1",
        "severity": "Med",
        "category": "Clarity",
        "text": "...",
        "suggestion": "..."
      }
    ],
    "metadata": {
      "passes": 3,
      "convergenceResult": "Converged (delta)",
      "wordCount": 195
    }
  }
  ```

**2.2 — Strands agent trace attributes**

- Set `session.id`, `invocation.id`, `user.id` in `trace_attributes`
- Propagate session ID via `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header
- Set OTEL baggage for session ID

**2.3 — `/api/personalise` response contract**

- Accepts `sessionId` + `segments` list
- Returns new `invocationId` + personalised variants

**2.4 — Feedback Lambda logic**

- PII redaction on text fields via Comprehend
- Write JSON to S3 with time-partitioned key
- Return success/failure

### Phase 3: Front-End (Next.js)

**3.1 — `FeedbackProvider` context**

- Initialises with `userId` only
- After first `/api/generate` response: stores `sessionId`, `invocationId`, `traceId`, `sessionMetadata`
- Updates `currentInvocationId` + `currentTraceId` on each subsequent generate/personalise call
- Accumulates session state: `totalAccepts`, `totalDismissals`, `segmentsSelected`, `exportFormat`

**3.2 — `useFeedback()` hook**

- `trackDecision(issueId, action, severity, category, issueText, suggestionText, sourceAgent)` → POST `/api/feedback/edit-decision`
- `trackPersonalisation(segments)` → updates provider state
- `trackExport(format)` → updates provider state
- `submitRating(score, comment?)` → bundles all accumulated metadata → POST `/api/feedback/rating`

**3.3 — `<FeedbackHarness>` wrapper**

- Props: `issueId`, `severity`, `category`, `issueText`, `suggestionText`, `sourceAgent`
- Renders Accept/Dismiss buttons, calls `useFeedback().trackDecision()` on click

**3.4 — `<RatingPanel>` component**

- Renders 👍/👎 + comment input
- On submit: calls `useFeedback().submitRating()` which pulls all session metadata from provider context

### Phase 4: Verification

**4.1 — End-to-end test**

- Generate → Accept/Dismiss issues → Personalise → Export → Rate
- Verify JSON files land in S3 with correct structure and partitioning
- Verify traces appear in CloudWatch GenAI Dashboard

### Suggested Dev Order

```
Week 1:  Phase 1 (CDK infra) + Phase 2.1-2.2 (generate contract + trace attrs)
Week 2:  Phase 2.3-2.4 (personalise + feedback Lambdas) + Phase 3.1-3.2 (provider + hook)
Week 3:  Phase 3.3-3.4 (harness + rating panel) + Phase 4 (verification)
```

Dependencies: Phase 2 needs Phase 1 (S3 bucket + API Gateway). Phase 3 needs Phase 2 (API contracts). Phase 4 needs everything.

---

## References

- [AgentCore Observability — Configure](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [AgentCore Observability — Telemetry Concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-telemetry.html)
- [AgentCore Runtime Metrics](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)
- [Strands Agents — Observability](https://strandsagents.com/docs/user-guide/observability-evaluation/observability/index.md)
- [Strands Agents — Traces](https://strandsagents.com/docs/user-guide/observability-evaluation/traces/index.md)
- [Deploy Strands to AgentCore](https://strandsagents.com/docs/user-guide/deploy/deploy_to_bedrock_agentcore/index.md)
