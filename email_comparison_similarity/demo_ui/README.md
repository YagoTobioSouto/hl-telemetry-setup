# Demo UI

Single-file HTML mockup showing what the `SimilarityEvaluation` JSON
could look like when rendered for a copywriter. **Not a deliverable**
— this is here so you have something visual to show in meetings
without hand-waving at a JSON blob.

## Run it

```bash
open demo_ui/index.html
```

Or drag the file into any browser. Works from `file://` — no server,
no build, no dependencies.

## What's in it

- **Two scenarios**, toggleable from the top-right:
  - *All unrelated* — the JSON output where the draft shares nothing
    substantive with any of the three source emails. Verdict
    `distinct`, confidence `low`.
  - *One near-duplicate* — same draft, but one of the sources is a
    heavily-paraphrased version of it. Verdict `near_duplicate`,
    confidence `high`.
- **Headline card**: verdict badge (green/amber/red), confidence
  badge, and the one-sentence explanation produced by the Evaluator
  agent.
- **Collapsible detail**: draft text on the left, ranked sources on
  the right with similarity bars, and the evidence block (shared
  terms chips, longest shared phrase, originality ratio) underneath.

## Fidelity

- Scores and evidence are copied verbatim from real runs of
  `evaluator_agent/run_local.py` against `fixtures/` and a
  near-duplicate reference set. The JSON shape matches
  `SimilarityEvaluation` exactly.
- Draft and source email texts are embedded inline so the page works
  without any network or file system access.
- Shared-term "substantive vs boilerplate" colour coding uses the same
  set of boilerplate terms (`_BOILERPLATE_TERMS`) that the mock
  templater and system prompt classify against in `evaluator_agent/`.
  So when you see a chip rendered in grey vs blue, that's the same
  judgement the real agent makes.

## What it is not

- Not wired to the live agent. If you want end-to-end, run
  `evaluator_agent/run_local.py --live-llm` separately and compare
  the JSON output to what the demo renders.
- Not a proposed production UI. It's a visual reference for
  whichever team ends up building the real UI — a concrete starting
  point for "what does this payload look like when rendered?".
- Not tested cross-browser. Works in modern Chrome/Firefox/Safari.
