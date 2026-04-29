"""
copycraft.email_blocks.schema

Canonical schema for the structured output produced by Strands and consumed
by the Copycraft email-blocks editor + renderer.

Source of truth: this file. JSON Schema is generated via `EmailDraft.model_json_schema()`.
TypeScript types should be generated from the JSON Schema via codegen
(e.g. json-schema-to-typescript) rather than hand-maintained.

Contract boundaries:
    - LLM authors content blocks only (hero, heading, paragraph, cta, ...).
    - Brand chrome (top logo, bottom footer, attribution preludes) is added by
      the renderer at compile time based on BrandConfig — NOT in this schema.
    - Inline Liquid (e.g. salutation) lives inside paragraph HTML as
      <span data-liquid-tag="..."> nodes; the renderer substitutes raw Liquid
      at export and a preview value in the editor.

Strands integration:
    from copycraft.email_blocks.schema import EmailDraft
    schema = EmailDraft.model_json_schema()
    # pass `schema` to Strands as the structured output contract
"""

from __future__ import annotations
from typing import Annotated, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

# ---------------------------------------------------------------------------
# Block types (authored)
# ---------------------------------------------------------------------------


class BlockBase(BaseModel):
    """Common fields for all authored blocks."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Unique block ID within the draft.")


class HeroBlock(BlockBase):
    """References a brand-scoped HeroTemplate (pre-baked image asset)."""

    type: Literal["hero"] = "hero"
    template_id: str = Field(description="Refs HeroTemplate in brand's hero library.")
    slot_values: Optional[dict[str, str]] = Field(
        default=None,
        description="For templates with editable text overlays.",
    )
    alt_override: Optional[str] = Field(
        default=None,
        description="Override the template's default alt text.",
    )


class HeadingBlock(BlockBase):
    """Heading element. Multiple consecutive headings are allowed."""

    type: Literal["heading"] = "heading"
    level: Literal["h1", "h2", "h3"]
    text: str
    align: Optional[Literal["left", "center"]] = Field(
        default=None,
        description="Default is brand-driven when omitted.",
    )


class ParagraphBlock(BlockBase):
    """
    TipTap-authored rich text serialised as sanitised HTML.

    Allowed inline marks: bold, italic, link, lists, strong-emphasis pairs.

    Inline Liquid tags use the form:
        <span data-liquid-tag="<tag_id>">[Label]</span>

    Where tag_id refers to a LiquidTag in the brand's registry. The inner
    text is the editor preview label and is replaced with raw Liquid at export.
    """

    type: Literal["paragraph"] = "paragraph"
    html: str = Field(description="Sanitised TipTap HTML output.")
    variant: Literal["body", "small_print", "footnote"] = "body"


class CtaBlock(BlockBase):
    """Single CTA button. Renderer applies brand.urlFilter to href at export."""

    type: Literal["cta"] = "cta"
    label: str
    href: HttpUrl


class FeatureRowBlock(BlockBase):
    """Icon (44px typical) + heading + body. Repeated horizontally is common."""

    type: Literal["feature_row"] = "feature_row"
    icon_url: HttpUrl
    heading: str
    body: str


class ColumnDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_pct: int = Field(ge=10, le=90)
    blocks: List["ColumnContentBlock"]


class ColumnLayoutBlock(BlockBase):
    """Generic column layout — for app/social rows, side-by-side comparisons."""

    type: Literal["column_layout"] = "column_layout"
    columns: List[ColumnDefinition] = Field(min_length=2, max_length=3)


class DisclaimerBlock(BlockBase):
    """Regulatory body text (campaign-specific, distinct from brand_footer)."""

    type: Literal["disclaimer"] = "disclaimer"
    html: str
    variant: Literal["standard", "small_print"] = "small_print"


class DividerBlock(BlockBase):
    """Visual rule."""

    type: Literal["divider"] = "divider"


class SpacerBlock(BlockBase):
    """Explicit whitespace. Use sparingly; prefer brand-driven block spacing."""

    type: Literal["spacer"] = "spacer"
    height: int = Field(ge=4, le=120, description="Pixel height.")


class LiquidMacroBlock(BlockBase):
    """
    Opaque Liquid expression resolved at send time.

    Examples:
        {{ ${email_footer} }}        — HL's centrally stored regulatory footer
        {% include 'unsubscribe' %}

    Renders as a labelled placeholder in the editor; raw Liquid at export.
    """

    type: Literal["liquid_macro"] = "liquid_macro"
    expression: str = Field(description="Raw Liquid expression.")
    preview_label: str = Field(description="Editor placeholder, e.g. '[Email Footer]'.")


# ---------------------------------------------------------------------------
# Discriminated unions
# ---------------------------------------------------------------------------
#
# Two unions are defined:
#
#   AuthoredBlock        — top-level blocks in EmailDraft.blocks.
#   ColumnContentBlock   — blocks legal *inside* a ColumnDefinition.
#
# ColumnContentBlock deliberately excludes ColumnLayoutBlock to break the
# schema cycle (Strands' JSON-Schema-to-tool-spec converter cannot yet walk
# $ref cycles — see strands/tools/structured_output/structured_output_utils.py).
# This is also a desirable invariant: nested column layouts render poorly in
# email clients (especially Outlook), so "no columns in columns" is a good
# constraint regardless.

AuthoredBlock = Annotated[
    Union[
        HeroBlock,
        HeadingBlock,
        ParagraphBlock,
        CtaBlock,
        FeatureRowBlock,
        ColumnLayoutBlock,
        DisclaimerBlock,
        DividerBlock,
        SpacerBlock,
        LiquidMacroBlock,
    ],
    Field(discriminator="type"),
]

ColumnContentBlock = Annotated[
    Union[
        HeroBlock,
        HeadingBlock,
        ParagraphBlock,
        CtaBlock,
        FeatureRowBlock,
        DisclaimerBlock,
        DividerBlock,
        SpacerBlock,
        LiquidMacroBlock,
    ],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Annotations (Editor + Fin Prams + Compliance)
# ---------------------------------------------------------------------------


class Annotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    block_id: str = Field(
        description="Module-scoped: refs a block.id within this draft."
    )
    source: Literal["editor", "fin_prams", "compliance"]
    severity: Literal["advisory", "med", "high"]
    tag: str = Field(
        description="Categorisation tag, e.g. 'clarity', 'tone', 'compliance', 'accuracy'."
    )
    text: str = Field(description="The issue identified.")
    suggestion: Optional[str] = Field(
        default=None,
        description="Proposed rewrite or remediation.",
    )
    status: Literal["open", "accepted", "dismissed"] = "open"


# ---------------------------------------------------------------------------
# Draft envelope
# ---------------------------------------------------------------------------


class DraftMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pass_: int = Field(ge=1, alias="pass")
    converged: bool
    word_count: int = Field(ge=0)
    reading_age: Optional[float] = None
    tone: Optional[str] = None


class EmailDraft(BaseModel):
    """
    The full structured output produced by Strands and consumed by the
    Copycraft editor + renderer.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    draft_id: str
    campaign_id: str
    brand_id: str = Field(description="Refs BrandConfig.")
    email_type: str = Field(description="e.g. 'isa-deadline', 'gilt-launch'.")
    persona: Optional[str] = Field(
        description="Persona slug; null = neutral draft (pre-personalisation).",
    )

    # Inbox-line modules
    subject: str = Field(min_length=1, max_length=150)
    pre_header: str = Field(min_length=1, max_length=200)

    # Body content (brand chrome added at render time)
    blocks: List[AuthoredBlock]

    # Review-loop output
    annotations: List[Annotation]

    # Run metadata
    metadata: DraftMetadata


# Forward-reference resolution for ColumnDefinition.blocks recursion
ColumnDefinition.model_rebuild()
