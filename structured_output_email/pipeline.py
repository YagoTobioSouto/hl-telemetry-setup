"""
pipeline.py  —  Brief → EmailDraft → HTML

Usage:
    python3 pipeline.py                  # run both steps, open result in browser
    python3 pipeline.py --agent-only     # stop after generating the JSON draft
    python3 pipeline.py --render-only    # skip the agent, re-render existing draft
"""

import sys
import webbrowser
from pathlib import Path

SAMPLES = Path(__file__).parent / "samples"
DRAFT_PATH = SAMPLES / "email-draft-generated.json"
HTML_PATH  = SAMPLES / "email-draft-generated.html"

agent_only  = "--agent-only"  in sys.argv
render_only = "--render-only" in sys.argv

# ---------------------------------------------------------------------------
# Step 1 — Agent: Brief → EmailDraft JSON
# ---------------------------------------------------------------------------
if not render_only:
    print("── Step 1: running agent (Brief → EmailDraft JSON) ──")
    import importlib.util, sys as _sys
    spec = importlib.util.spec_from_file_location(
        "agent", Path(__file__).parent / "agent" / "strands-structured-output.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    draft = mod.run_sync()

    import json
    DRAFT_PATH.write_text(
        json.dumps(draft.model_dump(by_alias=True, mode="json"), indent=2),
        encoding="utf-8",
    )
    print(f"   ✅  Draft saved → {DRAFT_PATH}")
    print(f"       subject : {draft.subject}")
    print(f"       blocks  : {len(draft.blocks)}")

    if agent_only:
        sys.exit(0)

# ---------------------------------------------------------------------------
# Step 2 — Renderer: EmailDraft JSON → HTML
# ---------------------------------------------------------------------------
print("── Step 2: rendering (EmailDraft JSON → HTML) ──")
import importlib.util
spec = importlib.util.spec_from_file_location(
    "renderer", Path(__file__).parent / "renderer" / "render_email.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import json, sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).parent / "schema"))
from email_blocks_schema import EmailDraft

raw   = json.loads(DRAFT_PATH.read_text())
draft = EmailDraft.model_validate(raw)
html  = mod.render(draft)
HTML_PATH.write_text(html, encoding="utf-8")
print(f"   ✅  HTML saved  → {HTML_PATH}")

webbrowser.open(HTML_PATH.as_uri())
