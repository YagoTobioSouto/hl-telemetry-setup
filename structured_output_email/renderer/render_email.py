"""
render_email.py

Minimal renderer: EmailDraft JSON → self-contained HTML email.

Usage:
    python3 render_email.py                          # renders email-draft-generated.json
    python3 render_email.py my-draft.json            # renders a specific draft
    python3 render_email.py --preview                # opens in browser after rendering

What this does:
    - Walks draft.blocks in order
    - Renders each block type with inline styles (required for email clients)
    - Substitutes Liquid <span> tags with their preview labels
    - Passes liquid_macro blocks through as visible placeholders
    - Wraps everything in brand chrome (HL colours, logo placeholder, footer)

What this does NOT do (renderer's job in production):
    - MJML compilation (this uses plain HTML tables instead)
    - Real Liquid substitution (uses preview labels)
    - Fetching actual hero images (uses a placeholder)
    - Outlook VML / MSO conditionals
"""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "schema"))
from email_blocks_schema import (
    CtaBlock,
    DisclaimerBlock,
    DividerBlock,
    EmailDraft,
    FeatureRowBlock,
    HeadingBlock,
    HeroBlock,
    LiquidMacroBlock,
    ParagraphBlock,
    SpacerBlock,
)

# ---------------------------------------------------------------------------
# Brand config (stub — in production this comes from BrandConfig)
# ---------------------------------------------------------------------------

BRAND = {
    "primary_colour": "#003366",      # HL navy
    "accent_colour":  "#00a3e0",      # HL blue
    "cta_colour":     "#e8520a",      # HL orange
    "text_colour":    "#333333",
    "small_print_colour": "#666666",
    "font_family":    "Arial, Helvetica, sans-serif",
    "max_width":      "600px",
    "logo_text":      "Hargreaves Lansdown",  # placeholder for actual logo img
}

# ---------------------------------------------------------------------------
# Block renderers
# ---------------------------------------------------------------------------

def _render_hero(block: HeroBlock) -> str:
    alt = block.alt_override or f"Hero image: {block.template_id}"
    return f"""
    <tr><td style="padding:0;">
      <div style="background:{BRAND['primary_colour']};color:#fff;
                  text-align:center;padding:40px 24px;font-size:13px;">
        [{alt}]<br>
        <span style="opacity:0.6;font-size:11px;">template: {block.template_id}</span>
      </div>
    </td></tr>"""


def _render_heading(block: HeadingBlock) -> str:
    sizes = {"h1": "26px", "h2": "20px", "h3": "16px"}
    align = block.align or "left"
    return f"""
    <tr><td style="padding:24px 32px 8px;">
      <{block.level} style="margin:0;font-family:{BRAND['font_family']};
        font-size:{sizes[block.level]};color:{BRAND['primary_colour']};
        text-align:{align};">{block.text}</{block.level}>
    </td></tr>"""


def _render_paragraph(block: ParagraphBlock) -> str:
    colour = BRAND["small_print_colour"] if block.variant != "body" else BRAND["text_colour"]
    size   = "11px" if block.variant in ("small_print", "footnote") else "14px"
    # Replace Liquid preview spans with their visible label
    html = re.sub(
        r'<span data-liquid-tag="[^"]*">(\[.*?\])</span>',
        r'<span style="color:{};font-style:italic;">\1</span>'.format(BRAND["accent_colour"]),
        block.html,
    )
    return f"""
    <tr><td style="padding:8px 32px;font-family:{BRAND['font_family']};
                   font-size:{size};color:{colour};line-height:1.6;">
      {html}
    </td></tr>"""


def _render_cta(block: CtaBlock) -> str:
    return f"""
    <tr><td style="padding:20px 32px;text-align:center;">
      <a href="{block.href}"
         style="display:inline-block;background:{BRAND['cta_colour']};color:#fff;
                font-family:{BRAND['font_family']};font-size:15px;font-weight:bold;
                text-decoration:none;padding:14px 32px;border-radius:4px;">
        {block.label}
      </a>
    </td></tr>"""


def _render_feature_row(block: FeatureRowBlock) -> str:
    return f"""
    <tr><td style="padding:16px 32px;">
      <table width="100%" cellpadding="0" cellspacing="0"><tr>
        <td width="52" style="vertical-align:top;">
          <img src="{block.icon_url}" width="44" height="44" alt="" style="display:block;">
        </td>
        <td style="padding-left:12px;vertical-align:top;
                   font-family:{BRAND['font_family']};color:{BRAND['text_colour']};">
          <strong style="font-size:15px;">{block.heading}</strong><br>
          <span style="font-size:13px;">{block.body}</span>
        </td>
      </tr></table>
    </td></tr>"""


def _render_divider(block: DividerBlock) -> str:
    return f"""
    <tr><td style="padding:8px 32px;">
      <hr style="border:none;border-top:1px solid #dddddd;margin:0;">
    </td></tr>"""


def _render_spacer(block: SpacerBlock) -> str:
    return f"""
    <tr><td style="height:{block.height}px;line-height:{block.height}px;">&nbsp;</td></tr>"""


def _render_disclaimer(block: DisclaimerBlock) -> str:
    return f"""
    <tr><td style="padding:8px 32px 16px;font-family:{BRAND['font_family']};
                   font-size:11px;color:{BRAND['small_print_colour']};line-height:1.5;
                   border-top:1px solid #eeeeee;">
      {block.html}
    </td></tr>"""


def _render_liquid_macro(block: LiquidMacroBlock) -> str:
    return f"""
    <tr><td style="padding:8px 32px;font-family:{BRAND['font_family']};
                   font-size:11px;color:{BRAND['accent_colour']};
                   background:#f0f8ff;text-align:center;">
      {block.preview_label}
      <span style="opacity:0.5;font-size:10px;"> — {block.expression}</span>
    </td></tr>"""


# ---------------------------------------------------------------------------
# Block dispatch
# ---------------------------------------------------------------------------

def _render_block(block) -> str:
    match block:
        case HeroBlock():        return _render_hero(block)
        case HeadingBlock():     return _render_heading(block)
        case ParagraphBlock():   return _render_paragraph(block)
        case CtaBlock():         return _render_cta(block)
        case FeatureRowBlock():  return _render_feature_row(block)
        case DividerBlock():     return _render_divider(block)
        case SpacerBlock():      return _render_spacer(block)
        case DisclaimerBlock():  return _render_disclaimer(block)
        case LiquidMacroBlock(): return _render_liquid_macro(block)
        case _:
            return f"<tr><td style='padding:8px 32px;color:red;'>[unhandled block type: {block.type}]</td></tr>"


# ---------------------------------------------------------------------------
# Chrome (brand wrapper)
# ---------------------------------------------------------------------------

def _chrome_header() -> str:
    return f"""
    <tr>
      <td style="background:{BRAND['primary_colour']};padding:16px 32px;text-align:center;">
        <span style="color:#fff;font-family:{BRAND['font_family']};
                     font-size:20px;font-weight:bold;letter-spacing:1px;">
          {BRAND['logo_text']}
        </span>
      </td>
    </tr>"""


def _chrome_footer() -> str:
    return f"""
    <tr>
      <td style="background:#f5f5f5;padding:16px 32px;text-align:center;
                 font-family:{BRAND['font_family']};font-size:11px;
                 color:{BRAND['small_print_colour']};">
        Hargreaves Lansdown Asset Management Limited · One College Square South,
        Anchor Road, Bristol, BS1 5HL<br>
        Authorised and regulated by the Financial Conduct Authority.
      </td>
    </tr>"""


# ---------------------------------------------------------------------------
# Full document
# ---------------------------------------------------------------------------

def render(draft: EmailDraft) -> str:
    block_rows = "\n".join(_render_block(b) for b in draft.blocks)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{draft.subject}</title>
</head>
<body style="margin:0;padding:0;background:#f0f0f0;">

  <!-- pre-header (hidden preview text) -->
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    {draft.pre_header}
  </div>

  <!-- email wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0"
         style="background:#f0f0f0;padding:24px 0;">
    <tr><td align="center">

      <!-- content table -->
      <table width="{BRAND['max_width']}" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:4px;overflow:hidden;">

        {_chrome_header()}
        {block_rows}
        {_chrome_footer()}

      </table>

    </td></tr>
  </table>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]
    preview = "--preview" in args
    args = [a for a in args if a != "--preview"]

    draft_path = Path(args[0]) if args else Path(__file__).parent.parent / "samples" / "email-draft-generated.json"
    out_path   = Path(__file__).parent.parent / "samples" / (draft_path.stem + ".html")

    raw = json.loads(draft_path.read_text())
    draft = EmailDraft.model_validate(raw)

    html = render(draft)
    out_path.write_text(html, encoding="utf-8")
    print(f"✅  Rendered {len(draft.blocks)} blocks → {out_path}")
    print(f"    subject: {draft.subject}")

    if preview:
        webbrowser.open(out_path.as_uri())
