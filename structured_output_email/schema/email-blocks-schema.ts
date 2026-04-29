/**
 * @copycraft/email-blocks/schema
 *
 * Canonical schema for the structured output produced by Strands and consumed
 * by the Copycraft email-blocks editor + renderer.
 *
 * Contract boundaries:
 *   - LLM authors content blocks only (hero, heading, paragraph, cta, ...)
 *   - Brand chrome (top logo, bottom footer, attribution preludes) is added by
 *     the renderer at compile time based on BrandConfig — NOT in this schema.
 *   - Inline Liquid (e.g. salutation) lives inside paragraph HTML as
 *     <span data-liquid-tag="..."> nodes; the renderer substitutes raw Liquid
 *     at export and a preview value in the editor.
 *
 * Strands integration: feed `EmailDraftSchema` to `zod-to-json-schema` and use
 * the result as Strands' structured output schema. Parse failures should be
 * fed back to the agent loop as validation errors.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Block types (authored)
// ---------------------------------------------------------------------------

const BlockId = z.string().min(1);

/** Hero — references a brand-scoped HeroTemplate (pre-baked image asset). */
const HeroBlock = z.object({
  id: BlockId,
  type: z.literal('hero'),
  template_id: z.string(),                // refs HeroTemplate in brand library
  slot_values: z.record(z.string()).optional(), // for templates with editable text overlays
  alt_override: z.string().optional(),    // override template's default alt text
});

/** Heading — h1, h2, h3. Multiple consecutive headings allowed. */
const HeadingBlock = z.object({
  id: BlockId,
  type: z.literal('heading'),
  level: z.enum(['h1', 'h2', 'h3']),
  text: z.string(),
  align: z.enum(['left', 'center']).optional(), // default brand-driven
});

/**
 * Paragraph — TipTap-authored rich text serialised as sanitised HTML.
 * Allowed inline marks: bold, italic, link, lists, strong-emphasis pairs.
 * Inline Liquid tags: <span data-liquid-tag="<tag_id>">[Label]</span>
 *   - tag_id refs a LiquidTag in the brand's registry
 *   - inner text is the editor preview label; replaced with raw Liquid at export
 */
const ParagraphBlock = z.object({
  id: BlockId,
  type: z.literal('paragraph'),
  html: z.string(),                       // sanitised TipTap HTML output
  variant: z.enum(['body', 'small_print', 'footnote']).default('body'),
});

/** Single CTA button. Brand controls colours; renderer applies brand.urlFilter to href. */
const CtaBlock = z.object({
  id: BlockId,
  type: z.literal('cta'),
  label: z.string(),
  href: z.string().url(),
});

/** Feature row — icon (44px typical) + heading + body. Repeated horizontally is common. */
const FeatureRowBlock = z.object({
  id: BlockId,
  type: z.literal('feature_row'),
  icon_url: z.string().url(),
  heading: z.string(),
  body: z.string(),
});

/** Generic column layout — for app/social rows, side-by-side comparisons, etc. */
const ColumnLayoutBlock: z.ZodType<{
  id: string;
  type: 'column_layout';
  columns: Array<{ width_pct: number; blocks: AuthoredBlock[] }>;
}> = z.lazy(() =>
  z.object({
    id: BlockId,
    type: z.literal('column_layout'),
    columns: z
      .array(
        z.object({
          width_pct: z.number().min(10).max(90),
          blocks: z.array(BlockSchema),
        }),
      )
      .min(2)
      .max(3),
  }),
);

/** Disclaimer — regulatory body text (campaign-specific, distinct from brand_footer). */
const DisclaimerBlock = z.object({
  id: BlockId,
  type: z.literal('disclaimer'),
  html: z.string(),
  variant: z.enum(['standard', 'small_print']).default('small_print'),
});

/** Divider — visual rule. */
const DividerBlock = z.object({
  id: BlockId,
  type: z.literal('divider'),
});

/** Spacer — explicit whitespace. Use sparingly; prefer brand-driven block spacing. */
const SpacerBlock = z.object({
  id: BlockId,
  type: z.literal('spacer'),
  height: z.number().int().min(4).max(120),
});

/**
 * Liquid macro — opaque Liquid expression resolved at send time.
 * Examples:
 *   - {{ ${email_footer} }}  → HL's centrally stored regulatory footer
 *   - {% include 'unsubscribe_block' %}
 * Renders as `[Email Footer]` placeholder in the editor; raw Liquid at export.
 */
const LiquidMacroBlock = z.object({
  id: BlockId,
  type: z.literal('liquid_macro'),
  expression: z.string(),                 // raw Liquid
  preview_label: z.string(),              // editor placeholder, e.g. "[Email Footer]"
});

// ---------------------------------------------------------------------------
// Discriminated union
// ---------------------------------------------------------------------------

export const BlockSchema = z.discriminatedUnion('type', [
  HeroBlock,
  HeadingBlock,
  ParagraphBlock,
  CtaBlock,
  FeatureRowBlock,
  ColumnLayoutBlock as z.ZodTypeAny,     // lazy ref
  DisclaimerBlock,
  DividerBlock,
  SpacerBlock,
  LiquidMacroBlock,
]);

export type AuthoredBlock = z.infer<typeof BlockSchema>;

// ---------------------------------------------------------------------------
// Annotations (Editor + Fin Prams + Compliance)
// ---------------------------------------------------------------------------

export const AnnotationSchema = z.object({
  id: z.string(),
  block_id: BlockId,                       // module-scoped
  source: z.enum(['editor', 'fin_prams', 'compliance']),
  severity: z.enum(['advisory', 'med', 'high']),
  tag: z.string(),                         // 'clarity', 'tone', 'compliance', 'accuracy', etc.
  text: z.string(),                        // the issue identified
  suggestion: z.string().optional(),       // proposed rewrite or remediation
  status: z.enum(['open', 'accepted', 'dismissed']).default('open'),
});

export type Annotation = z.infer<typeof AnnotationSchema>;

// ---------------------------------------------------------------------------
// Draft envelope
// ---------------------------------------------------------------------------

export const EmailDraftSchema = z.object({
  draft_id: z.string(),
  campaign_id: z.string(),
  brand_id: z.string(),                    // refs BrandConfig
  email_type: z.string(),                  // 'isa-deadline', 'gilt-launch', 'product-update', ...
  persona: z.string().nullable(),          // null = neutral draft (pre-personalisation)

  // Inbox-line modules
  subject: z.string().min(1).max(150),
  pre_header: z.string().min(1).max(200),

  // Body content (brand chrome added at render time)
  blocks: z.array(BlockSchema),

  // Review-loop output
  annotations: z.array(AnnotationSchema),

  // Run metadata
  metadata: z.object({
    pass: z.number().int().min(1),
    converged: z.boolean(),
    word_count: z.number().int(),
    reading_age: z.number().optional(),
    tone: z.string().optional(),
  }),
});

export type EmailDraft = z.infer<typeof EmailDraftSchema>;
