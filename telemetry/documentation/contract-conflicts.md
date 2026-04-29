# Contract conflicts

Deferred decisions and open items from `contracts.md` that are **not yet
applied** to the Phase 1 telemetry stack. Each entry captures what the
contract specifies, what the stack currently does, and why a decision is
pending.

The goal is to make every deviation explicit so nothing quietly drifts
from the client's spec.

The top of this document lists what's **open** — grouped by theme so the
team can walk down them one by one in review. The **applied** and
**resolved** sections lower down are the audit trail for what's already
been decided.

---

## Open decisions — at a glance

Eight items remain open, grouped into five review topics. Each § below
has a full write-up in the next section.

| Group                           | Item                                          | Decision owner          |
| ------------------------------- | --------------------------------------------- | ----------------------- |
| **A. Production readiness**     | §2 Bucket removal policy (RETAIN vs DESTROY)  | Client (env gating)     |
| **B. Harness integration**      | §5 Enum casing (severity/category/sourceAgent) | Dev, alongside harness  |
| (waiting on frontend traffic)   | §6 Request validation                         | Dev, alongside harness  |
|                                 | §7 PII redaction wiring                       | Client + Dev            |
| **C. Auth wiring**              | §8 Cognito authorizer activation              | Client / integrator     |
| **D. AgentCore / streaming**    | §10 AGUI `/api/generate` + `/api/personalise` | Agent team              |
| (Phase 2)                       | §15c Placeholder routes (dead-weight risk)    | Resolve with §10        |
| **E. CloudFront / edge**        | §11 CloudFront `/api/feedback/*` behaviour    | Frontend / integrator   |
|                                 | §15b CORS preflight (doubled-header risk)     | Resolve with §11        |

---

## Open decisions in detail

### Group A — Production readiness

---

#### §2 — Bucket removal policy

**Contract:** `removal_policy=RemovalPolicy.RETAIN` (prod-grade, bucket
survives stack deletion).

**Current stack:** `RemovalPolicy.DESTROY` + `auto_delete_objects=True`
(dev iteration; stack deletes cleanly).

**Why deferred:** this decision is tied to environment (dev/stage/prod),
not a simple code fix. Keeping DESTROY makes the dev loop fast; RETAIN is
what we want the moment data we care about starts landing in the bucket.

**Proposed resolution:** gate on a CDK context or environment variable:

```python
is_prod = self.node.try_get_context("environment") == "prod"
removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY
auto_delete_objects=not is_prod
```

**Decision owner:** client (when moving to shared/prod environment).

---

### Group B — Harness integration

These three items are all gated on the front-end feedback harness being
wired up and sending real traffic. Until then, hand-rolled schemas drift
from what the frontend actually emits.

---

#### §5 — Severity / category / enum casing

**Contract:**

- `severity`: `high`, `medium`, `low` (lowercase)
- `category`: `fca_compliance`, `brand_voice`, `product_accuracy`,
  `clarity`, `cta` (snake_case)
- `sourceAgent`: `researcher`, `copywriter`, `editor`, `fin_proms`

**PLAN.md / README.md (pre-contract):** mixed/title case (`Med`, `Clarity`).

**Current stack:** no validation — any string passes through to S3.

**Why deferred:** scoped to "when feedback & tables are in place" (i.e.
after the harness is wired). Defining enums now without a live producer
risks divergence from what the frontend actually sends.

**Decision owner:** implement alongside the harness.

---

#### §6 — Request validation

**Contract:** strict per-field validation (enum checks, length caps, UUID
format, numeric ranges). Invalid payloads return HTTP 400 with a
field-specific error message.

**Current stack:** only checks that `sessionId` and `issueId` exist.

**Why deferred:** same as §5 — scoped to "when feedback & tables are in
place". Until the harness is sending real traffic, hand-rolled validators
would drift from the schema the frontend actually emits.

**Proposed resolution when decided:** add a `validators/` module with
one function per route that returns either a validated dict or a
`(status, body)` tuple for the 400 response. Pydantic is probably
overkill for Lambda cold starts.

**Decision owner:** implement alongside the harness.

---

#### §7 — PII redaction wiring

**Contract:** call `comprehend:DetectPiiEntities` on `issueText`,
`suggestionText`, and `comment` before writing to S3. If Comprehend
fails, log the error, write `"[PII_REDACTION_FAILED]"` as a placeholder,
and still return 200 (best-effort — don't block the user).

**Current stack:** IAM permission is granted, but the handler leaves PII
redaction as a `# TODO`. Fields are written to S3 untransformed.

**Why deferred:** the user explicitly said "skip PII redaction for now"
when scoping the initial build. Infra is ready (IAM is in place); only
the handler code change and an `ENABLE_PII_REDACTION` env var are
missing.

**Proposed implementation:** add an `ENABLE_PII_REDACTION` env var
(default `false`) so dev can run without hitting Comprehend. Contract
open question #3 agrees — Comprehend has a real per-call cost.

**Decision owner:** implement when the harness starts sending real user
text.

---

### Group C — Auth wiring

---

#### §8 — Cognito authorizer integration

**Contract:** both feedback routes require a valid Cognito JWT. Unauth
requests must 401 (acceptance criterion #8). User pool comes from the
existing `CopycraftAuth` stack.

**Current stack:** `TelemetryStack.__init__` accepts an optional
`user_pool: cognito.IUserPool` kwarg. When provided, builds a
`CognitoUserPoolsAuthorizer` and attaches it to both methods. When
`None` (dev default), routes are open.

**Why modular-but-not-wired:** we don't have a reference to the actual
`CopycraftAuth` user pool yet. The harness informs what auth token shape
the frontend will actually send, which determines whether a vanilla
Cognito authorizer is sufficient or if we need a custom lambda
authorizer.

**Activation:** when wiring into the surrounding CDK app:

```python
TelemetryStack(
    app, "CopycraftTelemetry",
    env=_env,
    user_pool=auth_stack.user_pool,
)
```

**Decision owner:** client / integrator.

---

### Group D — AgentCore / streaming (Phase 2)

---

#### §10 — AGUI and `/api/generate` + `/api/personalise`

**Contract:** implied routes for AgentCore Runtime integration
(documented in README.md, not explicitly in contracts.md).

**Current stack:** placeholder resources (`api_resource.add_resource(...)`)
with no methods attached. They exist only so CORS preflight options
cover the eventual paths. The actual Lambda/AgentCore integration is
Phase 2.

**Gotchas for Phase 2:**

- REST API Gateway does **not** support Lambda response streaming. AGUI
  streams SSE events, so either:
  1. Use a Lambda Function URL with `InvokeMode: RESPONSE_STREAM` and
     route CloudFront directly, or
  2. Use HTTP API (WebSocket) instead of REST, or
  3. Point CloudFront `/api/generate` directly at the AgentCore Runtime
     endpoint (bypass API Gateway for the streaming routes).
- REST API Gateway has a 29-second integration timeout; the multi-agent
  pipeline will exceed this.

**Decision owner:** resolve when designing the AgentCore integration.

---

#### §15c — Placeholder `/api/generate` and `/api/personalise` resources

**Contract:** silent. The contract's §5 CDK block only adds the
`/api/feedback/edit-decision` and `/api/feedback/rating` methods. The
README mentions `/api/generate` and `/api/personalise`, but as AgentCore
Runtime routes — not API Gateway routes.

**Current stack:** `api_resource.add_resource("generate")` and
`api_resource.add_resource("personalise")` create empty resources with
no methods. They exist only so CORS preflight (OPTIONS) covers their
paths.

**Rationale:** the frontend may hit these paths while the AgentCore
integration is still in flight; without the resource, API Gateway
returns 403 with no CORS headers and the browser shows a misleading CORS
error instead of a 403.

**Potential issue:** the contract's Phase 2 AgentCore integration (§10)
may use a different mechanism entirely (Lambda Function URL, HTTP API,
or direct CloudFront → AgentCore origin). If it does, these placeholder
resources become dead weight and should be removed.

**Decision owner:** resolve alongside §10.

---

### Group E — CloudFront / edge integration

---

#### §11 — CloudFront `/api/feedback/*` behaviour

**Contract:** `frontend_stack.py` adds a CloudFront behaviour for
`/api/feedback/*` pointing at the REST API.

**Current stack:** emits `ApiUrl` as a `CfnOutput`. The frontend stack is
expected to consume this via cross-stack reference when the time comes.

**Decision owner:** integrator of the combined CDK app.

---

#### §15b — CORS preflight configuration

**Contract:** silent on CORS. The contract's architecture assumes
CloudFront fronts the API (see §11), which would handle CORS at the
edge.

**Current stack:** `default_cors_preflight_options` with explicit
`allow_headers=[Content-Type, X-Amz-Date, Authorization, X-Api-Key,
X-Amz-Security-Token, X-Session-Id, X-Trace-Id]`, `allow_origins=*`,
`allow_methods=[POST, OPTIONS]`.

**Rationale:** during dev, the frontend calls the API Gateway endpoint
directly (no CloudFront yet — see §11). Without CORS, the browser blocks
every request. The explicit `X-Session-Id` and `X-Trace-Id` headers are
additions so the frontend can send telemetry IDs on every request.

**Potential issue once CloudFront lands (§11):** doubled CORS handling —
both CloudFront and API Gateway set `Access-Control-*` headers. Browsers
reject responses with duplicated headers. When §11 is resolved, decide
whether CORS lives at CloudFront or API Gateway (not both) and remove
the other.

**Decision owner:** resolve alongside §11.

---

## Applied in this stack

For reference, these contract items **are already in the code** and do
not need to be revisited:

- 365-day S3 lifecycle on both `edit-decisions/` and `ratings/` prefixes
- `comprehend:DetectPiiEntities` IAM permission on the Lambda role
- Modular Cognito authorizer (optional `user_pool` kwarg — attaches
  `CognitoUserPoolsAuthorizer` to both routes when provided, no-ops
  otherwise)
- Python CDK (ported from TypeScript for consistency with the rest of
  the codebase)
- **Single-Lambda design** — client agreed on 2026-04-29 (see §1 below
  for history). As a consequence, the contract's `FEEDBACK_TYPE` env var
  is not used; we dispatch on `event["resource"]` instead.
- **Bucket name** `copycraft-telemetry-{account}` — matches the
  `copycraft-*` convention used elsewhere in the codebase (applied
  2026-04-29, see §3)
- **Lambda asset path** `lambda/feedback/` and **handler**
  `handler.lambda_handler` — matches the contract's shape (applied
  2026-04-29, see §4)
- **Lambda timeout 10s** — matches contract (applied 2026-04-29, was
  20s)
- **Success response body** `{"status": "ok"}` — matches contract
  (applied 2026-04-29; previously returned
  `{"message": "ok", "key": "..."}`)
- **Lambda env var** `TELEMETRY_BUCKET` — matches contract (applied
  2026-04-29, was `BUCKET_NAME`)
- **Lambda `function_name`** `copycraft-feedback-handler` — explicit
  name instead of CDK auto-generated (applied 2026-04-29, see §14 for
  history). Singular because of the single-Lambda design.
- **REST API `rest_api_name`** `copycraft-feedback` — matches contract
  (applied 2026-04-29, was `telemetry-api`, see §14)
- **Stack construct ID** `CopycraftTelemetry` — matches contract
  (applied 2026-04-29, was `TelemetryStack`, see §14). Stack had not
  been deployed yet, so the rename is free.
- **Deployment region pinned to `eu-west-2`** via `cdk.Environment` in
  `app.py` — satisfies the contract's Comprehend-access dependency
  (applied 2026-04-29, see §14)
- **S3 encryption: default SSE-S3** — client explicitly confirmed
  (2026-04-29) that default encryption is acceptable instead of SSE-KMS.
  See §13 for the trade-offs that were reviewed.
- **`BlockPublicAccess.BLOCK_ALL`** — retained as a safer-than-contract
  addition (see §15a history). No client objection.
- **500-error response** returns the literal string
  `{"error": "Internal server error"}` with the full exception logged
  server-side — matches contract (applied 2026-04-29, previously leaked
  `str(e)` into the response body).

---

## Resolved items — history

Full text kept for the audit trail. Nothing here is actionable; all of
these have been applied to the stack.

---

### §1 — Single Lambda vs. two Lambdas — RESOLVED

**Contract:** two separate functions, `EditDecisionHandler` and
`RatingHandler`, each with a `FEEDBACK_TYPE` env var driving the
behaviour.

**Current stack:** one Lambda, `FeedbackHandler` (logical ID) /
`copycraft-feedback-handler` (physical name), routing by
`event["resource"]`.

**Resolution (2026-04-29):** client agreed to keep the single-Lambda
design. Rationale: both routes share identical IAM (S3 PutObject +
Comprehend), the handler's route branches can't interfere with each
other, and a single warm container serves both routes — benefiting the
lower-frequency rating route. If per-route CloudWatch metrics become
important, we'll emit structured EMF from the handler rather than
splitting the function.

The contract's `FEEDBACK_TYPE` env var does not apply to the
single-Lambda design — we dispatch on `event["resource"]` which API
Gateway populates for free. No code change needed; just noting the
contract parameter became obsolete.

---

### §3 — Bucket name — RESOLVED

**Contract:** `copycraft-telemetry-{account}` — matches the existing
`copycraft-*` naming convention in the broader codebase.

**Resolution (2026-04-29):** client approved. Stack now sets
`bucket_name=f"copycraft-telemetry-{self.account}"`. The previous
auto-generated name was safer during early iteration (stack
delete-redeploy cycles didn't collide on the global S3 namespace) but
the explicit name is what the contract asks for and matches how other
Copycraft stacks consume resources by name.

**Implication:** if a stale bucket from a previous deploy still exists
at this name, `cdk deploy` will fail with a "bucket already exists"
error. Delete the old bucket (or its contents + the bucket) before the
first redeploy.

---

### §4 — Lambda asset path and handler name — RESOLVED

**Contract:** `lambda/feedback/` directory, handler
`handler.lambda_handler`.

**Resolution (2026-04-29):** renamed. The handler file now lives at
`lambda/feedback/handler.py` with a `lambda_handler(event, context)`
entry point. The old `lambdas/feedback_handler.py` has been removed.

Single-Lambda design is preserved — the directory rename is purely
cosmetic, it does not split the function.

---

### §9 — Bucket name collision risk — RESOLVED

Resolved by §3. The `copycraft-telemetry-{account}` name is globally
unique per AWS account, so single-account deploys are safe. Cross-account
or same-account redeploys after a stack delete need to ensure the bucket
has been fully deleted first (see §3 "Implication").

---

### §13 — S3 encryption: default SSE-S3 instead of SSE-KMS — RESOLVED

**Contract:** `encryption=BucketEncryption.KMS_MANAGED` (AWS-managed KMS
key — SSE-KMS).

**Resolution (2026-04-29):** client confirmed default encryption is
acceptable. The `encryption` argument is omitted in the stack, and S3
applies SSE-S3 (S3-managed keys) automatically (the default for every
new bucket since 2023-01-05).

**Trade-offs the client signed off on:**

- No per-object audit trail in CloudTrail KMS events. With SSE-KMS,
  every object read/write generates a `kms:Decrypt` /
  `kms:GenerateDataKey` CloudTrail entry. With SSE-S3 you only get S3
  data-plane events (which require S3 data events to be enabled
  separately).
- No envelope-key rotation policy under client control. SSE-S3 keys are
  rotated by AWS on an undocumented schedule.
- If the client's compliance posture later requires CMK-backed
  encryption (e.g. certain FCA or internal data-classification
  policies), flipping back to `BucketEncryption.KMS_MANAGED` is a
  one-line change — but it is a replacement on the bucket for new
  objects only; existing objects stay under their original SSE-S3
  envelope until re-uploaded.

**Implication for Acceptance Criterion #7:** the contract's AC#7 text
says "the S3 bucket exists with KMS encryption, 365-day lifecycle, …".
With §13 resolved as SSE-S3, AC#7's "KMS encryption" phrasing needs to
be restated as "server-side encryption" (or similar) in the client-facing
acceptance record. Functional intent (data encrypted at rest) is
preserved; only the key-management mechanism differs.

---

### §14 — Naming and deployment-config deviations — RESOLVED

**Resolution (2026-04-29):** all four sub-items applied as parameter
changes. The contract's identifiers and deployment wiring are now
matched.

- **14a Lambda `function_name`** → `copycraft-feedback-handler`
  (singular, since we kept the single-Lambda design per §1)
- **14b REST API `rest_api_name`** → `copycraft-feedback`
- **14c Stack construct ID** → `CopycraftTelemetry` in `app.py`. The
  stack had not been deployed yet, so the rename did not require a
  CloudFormation stack migration.
- **14d Region pinning** → `app.py` now creates a
  `cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
  region="eu-west-2")` and passes it to the stack. Satisfies the
  contract's "Comprehend access in eu-west-2" dependency.

Nothing outstanding here. `app.py` is still standalone — when it gets
folded into the combined Copycraft CDK app, the integrator should pass
`user_pool=auth_stack.user_pool` as well (see §8).

---

### §15a — `BlockPublicAccess.BLOCK_ALL` — RESOLVED

Client confirmed on 2026-04-29 that this should stay. The setting is
effectively the AWS account-level default for accounts created after
April 2023, but setting it explicitly in CDK guarantees the behaviour
regardless of account age or future AWS policy changes.

---

### §16 — 500 error response — RESOLVED

**Contract (Error handling table):** "S3 write fails | Lambda returns
500 with `{"error": "Internal server error"}`, logs full error".

**Resolution (2026-04-29):** handler now returns the literal string
`{"error": "Internal server error"}` and calls `logger.exception(...)`
to capture the full traceback in CloudWatch Logs. A module-level
`logging.getLogger()` was added at `INFO` level.

The `KeyError` branch (missing required field) still surfaces the field
name, which is safe — the frontend needs it to know which field was
missing.

---

## §12 — Contract open questions (from `contracts.md`)

Kept here for traceability with the source contract.

1. **Feedback API routing**: separate API Gateway (current design) vs.
   routes on the existing AgentCore app (`main.py`). Keeping them
   separate decouples feedback from the agent runtime but adds a
   CloudFront config step. Related: §10, §11.
2. **Session ID ownership**: spec says backend generates `sessionId`;
   frontend currently generates `threadId` per request. Reuse or
   separate?
3. **Comprehend cost**: add `ENABLE_PII_REDACTION` flag for dev?
   (Proposed yes — see §7.)
4. **Bucket naming**: resolved in §3 — `copycraft-telemetry-{account}`.
