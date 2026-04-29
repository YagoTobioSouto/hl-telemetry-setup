# Email Blocks v1 — Design Proposal

**Status:** PROPOSED — for analytical review
**Author:** jessie (with claude in strategic mode)
**Date:** 2026-04-29
**Space:** copycraft

## Linked nodes

**Decisions**
- `copycraft:decision:5dnkgxjyzjo210071wye` — Renderer architecture: MJML AST + Beefree-shape adapter (Option C)
- `copycraft:decision:r2dspuhib7mlmqyhj9jv` — Schema source of truth: Pydantic v2 models, JSON Schema generated

**Observations**
- `copycraft:observation:rhbk9o01ymezjdc61lkx` — Email-blocks package design state
- `copycraft:observation:i17o2dqifig5oa7kuz7b` — Gilt template analysis (5 new patterns)

**Reference artefacts (delivered)**
- `email_blocks_schema.py` — Pydantic models (canonical)
- `email-blocks-schema.json` — generated JSON Schema (Strands contract)
- `email-draft-sample-isa-deadline.json` — validated sample
- `email-blocks-schema.ts` — Zod parallel (front-end, transitional)

## Context

Copycraft is an agentic tool (AWS Strands pipeline: Researcher → Copywriter → Editor → Fin Prams) that consumes creative briefs and produces customer email drafts. Drafts are reviewed and refined by humans, optionally personalised per persona, and exported to ESPs. Hargreaves Lansdown (HL) is the launch customer; Braze is the launch ESP.

The current pipeline emits free-form email drafts. This work moves to **structured module output** so drafts become typed, module-scoped, and consistently styleable across campaigns.

## Goal of this milestone

Ship a working **email-blocks package** integrated into Copycraft (NextJS 16.2.1 / TypeScript) that:

1. Defines the structured-output contract Strands produces (`EmailDraft`)
2. Renders drafts to email-safe HTML matching HL's existing template DOM structure (Beefree-shape), validated against fixtures
3. Provides a block-level editor with TipTap rich text, module-scoped annotations, regenerate actions, and live preview
4. Supports HL's Cash ISA and Gilt templates as the validation corpus

**Scope boundary:** Brand admin UI, hero template library admin, Liquid tag registry admin, and Braze export integration are **out of scope** for this milestone. They become Milestone 2. For Milestone 1, the HL brand config is a hardcoded TypeScript fixture.

## Decisions already made (locked)

These are recorded in the linked decision nodes and are inputs to this design — they are not re-litigated here.

1. **Renderer architecture: Option C.** Pipeline is `EmailDraft → MJML AST → MJML's HTML → per-target adapter → final HTML`. First adapter target: Beefree-shape (matches Braze's drag-drop editor expectations). MJML intermediate buys multi-ESP optionality without rewriting renderers.

2. **Schema source of truth: Pydantic v2.** JSON Schema is generated via `EmailDraft.model_json_schema()`. TypeScript types should eventually be generated from JSON Schema via codegen; for Milestone 1, the hand-written Zod parallel stays.

3. **Personalisation model.** No data-driven merge personalisation in content. Personas are prompt overlays that produce parallel drafts per persona. Narrow Liquid use is permitted via a typed registry (e.g. salutation, attribution preludes, central footer macro).

4. **TipTap for paragraph rich text.** Already in the Copycraft codebase. Constrained extension set: bold, italic, link, lists, plus a custom node for inline Liquid tags.

## Open questions resolved (proposed)

These were open at the end of the strategic-mode chat. This proposal makes calls so the analytical worker has a concrete spec to validate; the worker is free to push back where evidence supports a different choice.

### Q1. Block ID format

**Decision:** ULID-style opaque IDs prefixed with `blk_`. Example: `blk_01HXYZABC...`.

**Reasoning:** Stable, sortable, unique across drafts and persona variants. Strands generates them at structured-output time (provide ULID in the agent's tool harness). Format is enforced by Pydantic with a regex validator.

**Affects:** Pydantic schema (add `pattern=r"^blk_[0-9A-Z]{26}$"` to `BlockBase.id`). Sample JSON updated to use ULIDs in next iteration.

### Q2. `brand_id` and `email_type` typing

**Decision:** Stay as free-form strings for Milestone 1, with documented slug conventions (`kebab-case`). Tighten to `Literal[...]` or `Enum` once the brand registry and email-type taxonomy are populated.

**Reasoning:** Premature typing forces us to maintain a registry before we have a Brand admin UI. Strings are forgiving during the schema-shakeout period.

**Affects:** Schema description fields. Add a tightening task to Milestone 2.

### Q3. Hero block handling

**Decision:** `hero` block is **optional** (zero-or-one occurrence in `blocks`). The Strands agent receives the brand's hero template list as part of its prompt context; if no template fits, it omits the hero block entirely (Gilt-style: headline-only).

**Reasoning:** Cleaner than a `no_hero` sentinel; matches the Gilt template fixture which has no hero. The agent learns to pick from a closed set, reducing hallucinated `template_id` values.

**Affects:** Agent prompt construction (must include available templates). Renderer: when hero is present, fetch the template; when absent, render starting from the first heading.

### Q4. Annotation lifecycle on regeneration

**Decision:**
- `open` annotations carry forward to the next pass and are re-evaluated by the next agent run.
- `accepted` and `dismissed` annotations are **sticky** — they persist and are not re-raised even if the underlying issue is still detectable.
- Each annotation has a `pass_introduced: int` field for traceability.
- A new pass that resolves an open annotation (by rewriting the block) marks it `accepted` and links the resolving agent in the audit log.

**Reasoning:** Sticky resolution prevents agents from re-raising issues the human already adjudicated. Carry-forward of open annotations preserves continuity across passes.

**Affects:** Pydantic `Annotation` schema gains `pass_introduced: int`. Agent loop logic must read existing annotations and avoid re-raising sticky ones.

### Q5. Liquid tag scope

**Decision:** Brand-scoped registries with optional inheritance from a shared `global` library. Tag IDs follow `{brand_id}_{tag_name}` (e.g. `hargreaves-lansdown_salutation`) or `global_{tag_name}` (e.g. `global_unsubscribe_link`).

**Reasoning:** Most tags are brand-specific (HL's salutation Liquid uses HL's `custom_attribute.${salutation}` shape). Universal tags (unsubscribe, attribution patterns) live at the global level and brands inherit them.

**Affects:** `LiquidTag` registry schema (Milestone 2). For Milestone 1, the registry is a hardcoded TS fixture for HL with `hl_salutation`, `hl_attribution_marketing_savings`, `hl_attribution_marketing_standard`, `hl_email_footer_macro`.

### Q6. Multi-CTA layouts

**Decision:** Two consecutive `cta` blocks in the authored array. The renderer applies brand-default vertical spacing. For side-by-side CTAs, use `column_layout` with two columns each containing one `cta`.

**Reasoning:** No dedicated `cta_group` block until evidence shows it's needed. The combination of "consecutive blocks" + `column_layout` covers all observed cases.

**Affects:** No schema changes. Renderer must handle consecutive-CTA spacing correctly.

### Q7. Brand chrome modelling

**Decision:** Brand chrome (top logo, prelude attribution, divider, app/social row, regulatory footer, bottom logo) is stored as **MJML strings** on `BrandConfig`, not as authored blocks. Three fields:

- `prelude_mjml`: Liquid attribution at the very top
- `header_mjml`: brand logo (and any campaign-invariant top blocks)
- `footer_mjml`: divider through bottom logo

Token interpolation supported (`{{brand.colors.primary}}`, etc.) for theming consistency.

**Reasoning:** Brand chrome is operator-managed and rarely-changing. Modelling it as authored blocks would force the LLM to reproduce it every time and create false annotation surface. MJML strings give the brand admin direct control without forcing a block-modelling exercise.

**Affects:** `BrandConfig` schema design (Milestone 2). For Milestone 1, hardcode `BrandConfig` as a TS fixture with HL's chrome lifted directly from the existing HL template HTML files.

### Q8. URL filter for click tracking

**Decision:** Brand-level `url_filter_template` field on `BrandConfig` (e.g. `?lid={{${cblid} | lid: 'AUTO'}}`). The renderer wraps every authored `href` (CTA, link in paragraph rich text) at export time. Authors and the LLM never see this — they write clean URLs.

**Reasoning:** Operational concern, not authoring concern. HL applies this consistently; other brands may not. Brand-config-driven keeps it portable.

**Affects:** `BrandConfig.url_filter_template`. Renderer adds an href-rewriting pass before MJML compilation.

### Q9. Braze round-trip behaviour

**Decision:** Treat as **assumed** for Milestone 1, validated separately by a Claude Code spike that builds a hand-crafted Beefree-shape upload zip from the existing HL Cash ISA template, uploads to Braze, saves+re-exports through Braze's editor, and diffs the result.

**Reasoning:** This is the gating assumption behind Option C. If Braze re-shapes the HTML aggressively, the adapter must track Braze's output rather than Beefree's. The spike is small and parallel to schema/editor work.

**Affects:** Parallel task — does not block schema/editor implementation.

## Architecture overview

```
┌─────────────────────────────────────────────────────────────────┐
│                  Strands agent pipeline (Python)                │
│  Researcher → Copywriter → Editor → Fin Prams                   │
│                       │                                          │
│                       │ structured_output=EmailDraft (Pydantic)  │
│                       ▼                                          │
└─────────────────────────────────────────────────────────────────┘
                        │
                        │ EmailDraft (validated)
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│            email-blocks package (TypeScript / NextJS)           │
│                                                                  │
│   schema/        types from JSON Schema (or Zod transitional)   │
│      │                                                           │
│      ├── editor/ (client)                                       │
│      │      ▶ block list, TipTap rich text, annotation overlay  │
│      │      ▶ regen actions per block                           │
│      │                                                           │
│      └── renderer/ (server)                                     │
│             EmailDraft + BrandConfig                             │
│                  │                                               │
│                  ├─ resolve hero template, liquid tags          │
│                  ├─ apply url_filter to authored hrefs          │
│                  ├─ authored blocks → MJML AST                  │
│                  ├─ wrap with brand.prelude/header/footer MJML  │
│                  ├─ mjml2html                                   │
│                  └─ Beefree-shape adapter                       │
│                              │                                  │
│                              ▼                                  │
│                       email-safe HTML                            │
│                              │                                  │
│              ┌───────────────┴───────────────┐                  │
│              ▼                               ▼                  │
│        <iframe srcDoc>                  Braze REST API          │
│        (in-app preview)                 /templates/email/create │
│                                         (Milestone 2)            │
└─────────────────────────────────────────────────────────────────┘
```

## Package structure

```
packages/email-blocks/
├── src/
│   ├── schema/
│   │   ├── types.ts              # Generated from JSON Schema (or hand-Zod for M1)
│   │   ├── parse.ts              # validateEmailDraft(json) → EmailDraft | ValidationError
│   │   └── index.ts
│   │
│   ├── editor/                   # 'use client'
│   │   ├── BlockEditor.tsx       # Top-level: list of block cards
│   │   ├── BlockCard.tsx         # Wrapper: shows type, metadata, annotations
│   │   ├── blocks/               # Per-block edit surfaces
│   │   │   ├── HeroEditor.tsx
│   │   │   ├── HeadingEditor.tsx
│   │   │   ├── ParagraphEditor.tsx     # TipTap with Liquid tag node
│   │   │   ├── CtaEditor.tsx
│   │   │   ├── FeatureRowEditor.tsx
│   │   │   ├── ColumnLayoutEditor.tsx  # recursive
│   │   │   ├── DisclaimerEditor.tsx
│   │   │   ├── DividerEditor.tsx
│   │   │   ├── SpacerEditor.tsx
│   │   │   └── LiquidMacroEditor.tsx
│   │   ├── annotations/
│   │   │   ├── AnnotationOverlay.tsx
│   │   │   └── AnnotationActions.tsx   # accept / dismiss / rewrite
│   │   ├── tiptap/
│   │   │   ├── extensions.ts
│   │   │   └── LiquidTagNode.ts
│   │   └── index.ts
│   │
│   ├── renderer/                 # server-only
│   │   ├── compile.ts            # compile(draft, brand) → string
│   │   ├── mjml/
│   │   │   ├── components/       # one per block type
│   │   │   │   ├── hero.ts
│   │   │   │   ├── heading.ts
│   │   │   │   ├── paragraph.ts
│   │   │   │   ├── cta.ts
│   │   │   │   ├── featureRow.ts
│   │   │   │   ├── columnLayout.ts
│   │   │   │   ├── disclaimer.ts
│   │   │   │   ├── divider.ts
│   │   │   │   ├── spacer.ts
│   │   │   │   └── liquidMacro.ts
│   │   │   └── compose.ts        # walk blocks + brand chrome → MJML doc
│   │   ├── adapters/
│   │   │   ├── beefree.ts        # MJML's HTML → Beefree-shape HTML
│   │   │   └── index.ts          # export type AdapterFn
│   │   ├── resolve/
│   │   │   ├── liquidTags.ts     # span[data-liquid-tag] → raw Liquid
│   │   │   ├── heroTemplate.ts   # template_id → image URL + slot interpolation
│   │   │   └── urlFilter.ts      # apply brand.url_filter_template to hrefs
│   │   ├── preview.tsx           # <Preview html={...} mode="desktop|mobile" />
│   │   └── index.ts
│   │
│   ├── fixtures/                 # M1 only — replaced by registries in M2
│   │   ├── brands/
│   │   │   └── hargreaves-lansdown.ts   # hardcoded BrandConfig fixture
│   │   ├── heroTemplates/
│   │   │   └── hl.ts                    # hardcoded HeroTemplate list
│   │   └── liquidTags/
│   │       └── hl.ts                    # hardcoded LiquidTag registry
│   │
│   └── index.ts                  # public exports
│
├── tests/
│   ├── fixtures/
│   │   ├── hl-cash-isa.html       # canonical Beefree-shape HTML target
│   │   ├── hl-gilt.html           # canonical Beefree-shape HTML target
│   │   ├── hl-cash-isa.draft.json # the EmailDraft that should produce ↑
│   │   └── hl-gilt.draft.json     # the EmailDraft that should produce ↑
│   ├── renderer.test.ts           # snapshot vs hl-*.html fixtures
│   ├── schema.test.ts             # JSON Schema validation
│   └── editor.test.tsx            # block editor interaction tests
│
└── package.json                   # exports: ./schema, ./editor, ./renderer
```

## Build sequence

The order is dependency-driven; each step is independently testable.

1. **Schema package** — Pydantic models in the Python backend, JSON Schema generation, TS types (regenerated or hand-Zod for M1), validation helpers. *DONE in part: schema and sample exist.*

2. **MJML component library** — one component per authored block type. Unit-tested with synthetic block JSON → expected MJML attribute sets. No HTML comparison yet.

3. **MJML compose + brand chrome wrapping** — `compose(authored_blocks, brand)` returns full MJML document. Unit-tested with HL fixture brand and synthetic block lists.

4. **Renderer end-to-end (no adapter)** — `compile(draft, brand)` runs MJML compilation and returns MJML's HTML output. Snapshot-tested.

5. **Beefree-shape adapter** — transform from MJML's HTML to Beefree-shape HTML. Snapshot-tested against `tests/fixtures/hl-cash-isa.html` and `hl-gilt.html` as targets. **This is the most-likely-to-iterate step** — expect adapter tweaks as we discover where MJML and Beefree disagree.

6. **Resolve passes** — Liquid tag resolution, hero template resolution, URL filter wrapping. Each tested in isolation, then integrated.

7. **Preview component** — iframe with `srcDoc`, desktop/mobile width toggle, tied to a draft + brand selection.

8. **Block editor scaffolding** — `BlockEditor.tsx`, block list, block cards, block-type registry, basic edit surfaces (label/href/text-only fields).

9. **TipTap integration** — paragraph editor with constrained extension set, Liquid tag custom node, sanitisation pipeline. Tested with sample drafts from the schema package.

10. **Annotation overlay** — module-scoped annotation panels with accept/dismiss/rewrite actions, sticky-status logic. Hooks into the regen-loop API (defined in Milestone 2 but stubbed here).

11. **Strands integration** — Python pipeline emits `EmailDraft` via `structured_output=EmailDraft`. Validation feedback loop. Tested with at least 5 brief inputs to shake out schema issues.

12. **Documentation** — package README, schema field reference, fixture conventions, how to add a new block type.

## Validation criteria

This milestone is achieved when **all** of:

1. The HL Cash ISA template HTML can be reproduced (snapshot-equal modulo whitespace) from a hand-authored `hl-cash-isa.draft.json` + `hargreaves-lansdown` brand fixture, via the full compile pipeline.

2. The HL Gilt template HTML can be reproduced (snapshot-equal modulo whitespace) from a hand-authored `hl-gilt.draft.json` + same brand fixture.

3. Strands, given a sample brief and the `EmailDraft` schema, produces a valid draft on first attempt for at least 4 of 5 test briefs (validation-pass rate ≥ 80%).

4. The block editor renders, edits, and serialises a draft correctly through every block type, with no schema-validation errors on save.

5. The iframe preview matches the rendered HTML byte-for-byte (no drift).

6. The Braze upload spike (parallel track) confirms Beefree-shape HTML round-trips through Braze's editor without structural mangling. *If this fails, the adapter strategy must be revisited before Milestone 2.*

## Out of scope (Milestone 2)

- Brand admin UI — CRUD for `BrandConfig` records inside Copycraft
- Hero template library admin UI
- Liquid tag registry admin UI
- Braze export integration (REST API push)
- Persona variant fan-out automation (manual for now)
- Multi-brand support beyond the hardcoded HL fixture
- ESP adapters beyond Beefree-shape
- Preview environment matrix testing (Litmus / Email on Acid)

## Risks parked

| Risk | Mitigation |
|---|---|
| Braze re-shapes Beefree HTML on save, breaking Option C | Spike test (Q9) — gates milestone close |
| LLM produces ambiguous block-type assignments (e.g. `paragraph` for things that should be `disclaimer`) | Tighten schema descriptions iteratively; add few-shot examples to agent prompts |
| MJML's CSS handling differs subtly from Beefree's expectations | Adapter step is where this is absorbed; expect 1–2 iterations |
| `column_layout` recursion creates deep nesting that's hard to render | Cap recursion depth at 1 level (no nested column layouts within columns) — enforce in schema validator |
| Inline Liquid in TipTap breaks paste/copy behaviour | Use TipTap's atom node pattern; tag pills are non-editable units |

## Notes for the analytical worker

- Validate the package structure against the existing Copycraft codebase — the file paths assume pnpm workspace conventions; adjust if Copycraft uses a different layout.
- Confirm TipTap version in use; the constrained extension set may need adjustment based on what's already configured.
- The build sequence is dependency-correct but not parallelism-aware — multiple steps can be done in parallel by different workers. Identify the parallelisable subgraph.
- Open a question against any of the resolved Q1–Q9 calls if there's evidence to reconsider; this proposal is a starting point, not a contract.
- Cross-check the Pydantic schema against AWS Strands' actual `structured_output` semantics — the Pydantic-direct integration assumes a specific Strands API; if the real API requires JSON Schema input, the integration code shape changes.
