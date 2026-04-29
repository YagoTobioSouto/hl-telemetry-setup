# Contract conflicts

Deferred decisions and open items from `contracts.md` that are **not yet
applied** to the Phase 1 telemetry stack. Each entry captures what the
contract specifies, what the stack currently does, and why a decision is
pending.

The goal is to make every deviation explicit so nothing quietly drifts from
the client's spec.

---

## Applied in this stack

For reference, these contract items **are already in the code** and do not
need to appear below:

- 365-day S3 lifecycle on both `edit-decisions/` and `ratings/` prefixes
- `comprehend:DetectPiiEntities` IAM permission on the Lambda role
- Modular Cognito authorizer (optional `user_pool` kwarg — attaches
  `CognitoUserPoolsAuthorizer` to both routes when provided, no-ops otherwise)
- Python CDK (ported from TypeScript for consistency with the rest of the
  codebase)

---

## §1 — Single Lambda vs. two Lambdas

**Contract:** two separate functions, `EditDecisionHandler` and
`RatingHandler`, each with a `FEEDBACK_TYPE` env var driving the behaviour.

**Current stack:** one Lambda, `FeedbackHandler`, routing by
`event["resource"]`.

**Why deferred:** PLAN.md explicitly chose single-Lambda for code reuse; the
contract splits them without articulating a benefit. Three paths forward:

- **A.** Match the contract exactly — two `Function` constructs, two log
  groups, two deployments.
- **B.** Push back — keep single Lambda, document the rationale.
- **C.** Hybrid — one `Code.from_asset()`, two `Function` constructs pointing
  at it, each with its own `FEEDBACK_TYPE` env var. Matches the contract's
  deployment model without duplicating code.

**Decision owner:** client (architectural preference).

**Trigger:** resolve before the harness lands — deploying the harness against
one Lambda when the contract expects two would mean a rename in production.

---

## §2 — Bucket removal policy

**Contract:** `removal_policy=RemovalPolicy.RETAIN` (prod-grade, bucket
survives stack deletion).

**Current stack:** `RemovalPolicy.DESTROY` + `auto_delete_objects=True` (dev
iteration; stack deletes cleanly).

**Why deferred:** user explicitly said "keep the easy fixes in place" — this
decision is tied to environment (dev/stage/prod), not a simple code fix.

**Proposed resolution:** gate on a CDK context or environment variable:

```python
is_prod = self.node.try_get_context("environment") == "prod"
removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY
auto_delete_objects=not is_prod
```

**Decision owner:** client (when moving to shared/prod environment).

---

## §3 — Bucket name

**Contract:** `copycraft-telemetry-{account}` — matches the existing
`copycraft-*` naming convention in the broader codebase.

**Current stack:** CDK auto-generated name (e.g.
`telemetrystack-telemetrybucket710ff2c8-xxxxx`).

**Why deferred:** this is contract open question #4 — the client asked it
themselves. Using an auto-generated name is safer during iteration
(stack-delete-redeploy cycles don't collide on the global S3 namespace).

**Proposed resolution when decided:**

```python
bucket_name=f"copycraft-telemetry-{self.account}"
```

**Decision owner:** client.

---

## §4 — Lambda asset path and handler name

**Contract:** `lambda/feedback/` directory, handler `handler.lambda_handler`.

**Current stack:** `lambdas/` directory, handler `feedback_handler.handler`.

**Why deferred:** cosmetic; the code works either way. Renaming is
mechanical but should happen alongside §1 (if we split into two Lambdas,
we'll reorganise the directory anyway).

**Decision owner:** resolve with §1.

---

## §5 — Severity / category / enum casing

**Contract:**

- `severity`: `high`, `medium`, `low` (lowercase)
- `category`: `fca_compliance`, `brand_voice`, `product_accuracy`, `clarity`,
  `cta` (snake_case)
- `sourceAgent`: `researcher`, `copywriter`, `editor`, `fin_proms`

**PLAN.md / README.md (pre-contract):** mixed/title case (`Med`, `Clarity`).

**Current stack:** no validation — any string passes through to S3.

**Why deferred:** user scoped validation to "when feedback & tables are in
place" (i.e. after the harness is wired). Defining enums now without a
live producer risks divergence from what the frontend actually sends.

**Decision owner:** implement alongside the harness.

---

## §6 — Request validation

**Contract:** strict per-field validation (enum checks, length caps, UUID
format, numeric ranges). Invalid payloads return HTTP 400 with a
field-specific error message.

**Current stack:** only checks that `sessionId` and `issueId` exist.

**Why deferred:** same as §5 — scoped to "when feedback & tables are in
place". Until the harness is sending real traffic, hand-rolled validators
would drift from the schema the frontend actually emits.

**Proposed resolution when decided:** add a `validators/` module with one
function per route that returns either a validated dict or a `(status, body)`
tuple for the 400 response. Pydantic is probably overkill for Lambda cold
starts.

**Decision owner:** implement alongside the harness.

---

## §7 — PII redaction wiring

**Contract:** call `comprehend:DetectPiiEntities` on `issueText`,
`suggestionText`, and `comment` before writing to S3. If Comprehend fails,
log the error, write `"[PII_REDACTION_FAILED]"` as a placeholder, and still
return 200 (best-effort — don't block the user).

**Current stack:** IAM permission is granted, but the handler leaves PII
redaction as a `# TODO`. Fields are written to S3 untransformed.

**Why deferred:** the user explicitly said "skip PII redaction for now" when
scoping the initial build. Infra is ready (IAM + env var can be added); only
the handler code change is missing.

**Proposed implementation:** add an `ENABLE_PII_REDACTION` env var (default
`false`) so dev can run without hitting Comprehend. Contract open question
#3 agrees — Comprehend has a real per-call cost.

**Decision owner:** implement when the harness starts sending real user
text.

---

## §8 — Cognito authorizer integration

**Contract:** both feedback routes require a valid Cognito JWT. Unauth
requests must 401 (acceptance criterion #8). User pool comes from the
existing `CopycraftAuth` stack.

**Current stack:** `TelemetryStack.__init__` accepts an optional
`user_pool: cognito.IUserPool` kwarg. When provided, builds a
`CognitoUserPoolsAuthorizer` and attaches it to both methods. When `None`
(dev default), routes are open.

**Why modular-but-not-wired:** we don't have a reference to the actual
`CopycraftAuth` user pool yet. The user said "for 11 we need to wait for
the harness input" — the harness informs what auth token shape the frontend
will actually send, which determines whether a vanilla Cognito authorizer
is sufficient or if we need a custom lambda authorizer.

**Activation:** when wiring into the surrounding CDK app:

```python
TelemetryStack(
    app, "CopycraftTelemetry",
    user_pool=auth_stack.user_pool,
)
```

**Decision owner:** client / integrator.

---

## §9 — Bucket name collision risk (contract open question #4)

Resolved together with §3. If the client picks the auto-generated name,
there is no collision. If they pick `copycraft-telemetry-{account}`, the
single-account assumption is explicit.

---

## §10 — AGUI and `/api/generate` + `/api/personalise`

**Contract:** implied routes for AgentCore Runtime integration (documented
in README.md, not explicitly in contracts.md).

**Current stack:** placeholder resources (`api_resource.add_resource(...)`)
with no methods attached. They exist only so CORS preflight options cover
the eventual paths. The actual Lambda/AgentCore integration is Phase 2.

**Gotchas for Phase 2:**

- REST API Gateway does **not** support Lambda response streaming. AGUI
  streams SSE events, so either:
  1. Use a Lambda Function URL with `InvokeMode: RESPONSE_STREAM` and route
     CloudFront directly, or
  2. Use HTTP API (WebSocket) instead of REST, or
  3. Point CloudFront `/api/generate` directly at the AgentCore Runtime
     endpoint (bypass API Gateway for the streaming routes).
- REST API Gateway has a 29-second integration timeout; the multi-agent
  pipeline will exceed this.

**Decision owner:** resolve when designing the AgentCore integration.

---

## §11 — CloudFront `/api/feedback/*` behaviour

**Contract:** `frontend_stack.py` adds a CloudFront behaviour for
`/api/feedback/*` pointing at the REST API.

**Current stack:** emits `ApiUrl` as a `CfnOutput`. The frontend stack is
expected to consume this via cross-stack reference when the time comes.

**Decision owner:** integrator of the combined CDK app.

---

## §12 — Contract open questions

Direct copies from `contracts.md` for traceability:

1. **Feedback API routing**: separate API Gateway (current design) vs. routes
   on the existing AgentCore app (`main.py`). Keeping them separate
   decouples feedback from the agent runtime but adds a CloudFront config
   step.
2. **Session ID ownership**: spec says backend generates `sessionId`;
   frontend currently generates `threadId` per request. Reuse or separate?
3. **Comprehend cost**: add `ENABLE_PII_REDACTION` flag for dev? (Proposed
   yes — see §7.)
4. **Bucket naming**: `telemetry-bucket` (PLAN.md) vs.
   `copycraft-telemetry-{account}` (contract convention)? See §3.

---

## Summary of what blocks what

| Decision           | Blocks                                             |
| ------------------ | -------------------------------------------------- |
| §1 Lambda split    | §4 directory rename, deployment automation         |
| §2 Removal policy  | Prod deployment                                    |
| §3 Bucket name     | Discovery from other stacks, cross-account imports |
| §5/§6 Validation   | Front-end harness reliability                      |
| §7 PII wiring      | Compliance review                                  |
| §8 Cognito wiring  | Acceptance criterion #8 (auth test)                |
| §10 AGUI streaming | Phase 2 agent integration                          |
| §11 CloudFront     | Front-end consuming the feedback API               |
