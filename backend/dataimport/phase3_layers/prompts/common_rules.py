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
STRICT JSON OUTPUT FORMAT — READ BEFORE EMITTING ANY OUTPUT
═══════════════════════════════════════════════════════════════════════════
Your ENTIRE response must be EXACTLY one syntactically-valid JSON object,
parseable by Python's json.loads on the first attempt. Concretely:

  • Start with `{` and end with `}` — no leading or trailing prose.
  • No markdown code fences (```), no ```json wrapper, no commentary before
    or after the object.
  • All keys and string values use double quotes ("…"), never single quotes.
  • No trailing commas after the last array element or object key.
  • No comments (// or /* */) — JSON does not allow them.
  • No `NaN`, `Infinity`, `-Infinity`, or `undefined` — use `null` instead.
  • All control characters inside strings MUST be escaped (\\n, \\t, \\", \\\\).
  • Newlines inside string values: use \\n, never a raw line break.
  • Numbers must be plain JSON numbers (no thousands separators, no currency
    symbols, no units appended — emit 100000 not "₹1,00,000" or "10 Cr").
  • Dates must be ISO-8601 strings ("2024-03-31"), never Excel serials or
    locale-specific formats.
  • Every opening `{` / `[` must have a matching closing `}` / `]`.

If you cannot fit the complete object inside your output budget, emit fewer
records (per the ROW PRESERVATION RULE below) but ALWAYS finish with a
syntactically-valid JSON close. NEVER stop mid-string, mid-number, or
mid-object — that produces an unparseable response and costs us a retry.
═══════════════════════════════════════════════════════════════════════════

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

═══════════════════════════════════════════════════════════════════════════
ROW PRESERVATION RULE — STRICT (applies to every array in every layer)
═══════════════════════════════════════════════════════════════════════════
Every populated data row in every sheet you receive MUST appear as exactly
one record in the relevant output array, UNLESS the row is:
  (a) a header row,
  (b) a blank separator,
  (c) a subtotal / grand-total / summary row,
  (d) a section title banner (e.g. "── INVESTORS ──").

Specifically: if a sheet has N companies listed (e.g. PC001 ... PC050) and
you receive all N in your input, your output MUST contain exactly N entries
for that array (one per company). Skipping a single company in the middle of
a list is a CRITICAL FAILURE that breaks downstream accounting.

Before returning, mentally count: for each sheet you touched, how many
populated data rows were in your input vs. how many entries you emitted for
that sheet's target array(s)? They must match (allowing only header /
separator / subtotal exclusions).

If your output token budget cannot fit all rows: return as many complete
records as fit AND set `sheet_completeness[].truncated_in_prompt = true` for
that sheet so the orchestrator knows to re-split. NEVER silently drop rows.
═══════════════════════════════════════════════════════════════════════════
"""
