"""Similarity Evaluator agent — central config.

Everything tunable lives here. Matches the handler's "no env vars, no
runtime config" house style: when something needs to change, it's a
code edit, not a deploy-time knob.

Two things in particular get tweaked often during rollout:

* ``EVALUATOR_MODEL_ID`` — start with Nemotron 3 Super, swap to GLM 5
  (or anything else Bedrock serves) by changing this one string. If
  your region requires a cross-region inference profile, put the
  profile ID here instead of the bare model ID (e.g.
  ``"eu.nvidia.nemotron-super-3-120b"``).
* ``EVALUATOR_SYSTEM_PROMPT`` — the guardrails. This is what stops
  the LLM inventing verdicts or dressing up low-score noise. Tune
  with care and re-run ``run_local.py --live`` against both fixtures
  sets before trusting new wording.
"""

from __future__ import annotations

# --- Bedrock model -----------------------------------------------------

# Default: NVIDIA Nemotron 3 Super 120B. Fast, cheap, well-suited to
# short constrained outputs. Confirmed available in eu-west-2.
#
# Alternative: "zai.glm-5" for GLM 5. Swap at will — the agent code
# has no model-specific assumptions.
EVALUATOR_MODEL_ID = "nvidia.nemotron-super-3-120b"

# Region pinned to match the similarity Lambda. Cross-region adds
# latency for no benefit.
EVALUATOR_REGION = "eu-west-2"

# Temperature 0 — we want reproducible, constrained output. This is
# not a creative writing task, it's a structured explanation over
# deterministic evidence.
EVALUATOR_TEMPERATURE = 0.0

# ~25 words × ~4 tokens/word = ~100 tokens, plus a small buffer for
# punctuation and quoted phrases. Keeps responses tight and cheap.
EVALUATOR_MAX_TOKENS = 128


# --- Similarity Lambda invocation --------------------------------------

# Name of the scoring Lambda the agent calls as a tool. Must match
# ``infra/stacks/similarity_stack.py`` → ``function_name``.
LAMBDA_FUNCTION_NAME = "copycraft-similarity-handler"

# Lambda region. Kept separate from the agent's region on the off
# chance they diverge (they shouldn't, but the split makes the
# dependency explicit).
LAMBDA_REGION = "eu-west-2"


# --- System prompt -----------------------------------------------------
#
# Writes the agent's one-sentence explanation. Three guardrails in
# descending order of importance:
#
# 1. Never contradict the verdict. The Lambda already decided it.
# 2. Distinguish substantive shared terms from boilerplate. A low
#    score with filler evidence ("let me know if", "wanted share")
#    must be called out as *distinct*, not dressed up as similarity.
# 3. One sentence, ≤25 words, no preamble.
#
# The three examples are deliberate: one for each verdict bucket, so
# the LLM has a concrete template for each path through the decision.

EVALUATOR_SYSTEM_PROMPT = """\
You produce a single-sentence description of how closely a drafted email \
resembles the closest source email it was written from. The scoring has \
already been done deterministically and its output is provided to you in \
the user message as JSON. You MUST NOT re-judge it.

Your job is to INFORM the reader about what the similarity actually is. \
Do NOT recommend actions. Do NOT say things like "safe to send", \
"consider rewording", "review before sending", or any other advice. The \
copywriter decides what to do with the information — you only describe \
the relationship between the draft and the sources.

Pay attention to these fields in the scoring output:

- `verdict` — the authoritative headline (`distinct`, `related`, or \
`near_duplicate`). Never contradict it.
- `evidence.shared_terms` — the words driving the score. Decide whether \
they are substantive topic words ("tier skus", "pricing discussion") or \
generic email boilerplate ("wanted", "share", "happy", "15").
- `evidence.longest_shared_phrase` — the longest verbatim run between \
draft and closest source. A short phrase like "let me know if" (≤4 \
tokens) is filler; a 10+ token match is meaningful reuse.

Write ONE sentence, ≤25 words, no preamble, no recommendations. Match \
the tone to the verdict:

- `verdict=distinct` with boilerplate evidence → Describe plainly that \
the draft does not meaningfully resemble any source, and that any overlap \
is generic email filler. Example: "Your draft is distinct from all source \
emails; the only overlap is generic email filler ('let me know if', \
'wanted to share')."

- `verdict=related` → Describe what is genuinely shared without \
overstating. Example: "Your draft shares the 'pricing discussion' topic \
with id_2 but uses different phrasing and structure."

- `verdict=near_duplicate` → Describe the damning evidence concretely, \
quoting the shared phrase if it is substantive. Example: "Your draft \
contains a 12-word phrase nearly verbatim from id_1 ('quick summary of \
where we landed…')."

Your sentence is read by a human copywriter who will use it to decide \
whether the draft meets their originality bar. Be descriptive, specific, \
and neutral. Do not pad, do not advise.\
"""
