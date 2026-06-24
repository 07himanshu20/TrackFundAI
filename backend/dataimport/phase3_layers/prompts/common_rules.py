"""
Shared prompt blocks for all Phase 3 layer prompts.

The 34 forensic rules + persona currently live as `_SYSTEM_RULES` in
single_call_extractor.py. We re-export them here so that:

  • Phase 3 layer prompts (Layer 1 / 2 / 3) share one canonical rule set.
  • Phase 2 single-call fallback uses the same rules.
  • When single_call_extractor.py is deleted (~3 months post Phase 3 GA),
    move the literal text into this module and remove the import.
"""

from ...single_call_extractor import _SYSTEM_RULES as _CANONICAL_RULES

COMMON_PREAMBLE = _CANONICAL_RULES
COMMON_RULES = _CANONICAL_RULES
COMMON_HARD_GUARDS = _CANONICAL_RULES


# Shape-enforcement block appended to every layer prompt so Gemini emits
# exactly the JSON contract the merger expects.
JSON_OUTPUT_CONTRACT = """\
═══════════════════════════════════════════════════════════════════════════
OUTPUT CONTRACT (LAYER-SPECIFIC)
═══════════════════════════════════════════════════════════════════════════
Return EXACTLY one JSON object. Top-level keys MUST be a subset of the
layer-specific allowed keys listed below — do NOT emit keys that belong to
other layers, even if the workbook contains the data (another layer is
extracting those in parallel; the merger combines our outputs).

For every aggregate value you emit, populate the matching `provenance`
sub-object (Rule 32) — cell reference for extracted values, formula
expression for computed values. Values without provenance are rejected.

Empty arrays `[]` are encouraged for sections present in this layer's
allowed keys but absent from the workbook. NEVER fabricate placeholder rows.
═══════════════════════════════════════════════════════════════════════════
"""
