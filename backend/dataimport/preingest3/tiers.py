"""Presentation-TIER discrimination — the net selector's STRUCTURE axis (Step-5, axis-1).

The net has two orthogonal axes. family.py owns the FIRST (which statement type a concept
belongs to, confirmed by an accounting identity). This module owns the STRUCTURE axis: among
candidate statement sheets, separate a real hierarchical STATEMENT from a flat GL/TB DUMP.

WHY IT IS A DEMOTION, NOT A TIEBREAK. A full GL export mentions EVERY concept, so a dump wins
a concept-count race against a real statement (CPM: the SAP dump `SourcePL SAP` located all 4
MIS concepts, the real division P&Ls only 3). If tier were a mere tiebreak the dump would keep
winning. So a dump ranks BELOW every real statement in _best_sheet — before concept-count.

THE DEEP INVARIANT is FLAT LEDGER vs HIERARCHICAL STATEMENT: a statement is SHORT and rolls detail
up into a few subtotals; a dump is a LONG flat list of peer accounts. LENGTH IS THE NECESSARY
CONDITION (advisor 2026-07-27) — a SHORT sheet is NEVER a dump, however code-heavy, because a real
statement can carry account codes (Analisa `ProfitLoss (23)`: 77% GL-code but a real 133-line P&L
whose IS identity CONFIRMS). GL-code density and flatness only CORROBORATE length; neither
classifies a short, hierarchical sheet as a dump. We never detect a dump by name or caption (dumps
carry subtotal captions too — the finding that overturned the caption discriminator, and the reason
subtotal fraction does NOT separate: CSS `SourcePL-SAP` dump = 34.5% subtotals > Analisa real =
14.3%). So: a candidate is a dump ONLY IF it is LONG and either of two signatures of bulk data fires:

  • GL-CODE SIGNATURE. The fraction of line-item labels that OPEN with an account code (a 3+ digit
    numeric prefix): '83231010 - Sponsorship-cash', '410000 Turnover'. Real winners label by NAME
    → 0%; SAP/Tally GL & TB exports label by CODE → 33-99%. Corroborates a long sheet's bulk-ness.

  • STRUCTURAL SIGNATURE (name-only backstop). A dump labelled by account NAME with no numeric
    prefix ('Bank charges', 'Salaries — engineering', …) scores 0% GL-code. It still violates the
    invariant: LONG with near-ZERO roll-up/subtotal lines. So a long, flat, subtotal-less sheet is a
    dump too — the hole the GL-code signal leaves open.

THE ASYMMETRY (advisor 2026-07-27) sets the tuning direction. A FALSE POSITIVE (a real statement
mis-read as a dump) is SAFE — the concept falls to another sheet or holds; only coverage is lost —
BUT here it regressed a pinned binding (Analisa), so the length gate is tuned with MORE margin above
the largest real winner (133 labels) than below the smallest competitive dump (371). A FALSE
NEGATIVE (a dump passing as a statement) is DANGEROUS — a wrong source. The worst case is a LONE
BALANCING name-only TB dump with no competing real statement: it scores 0% GL-code, passes as a
statement, BALANCES (so the family identity CONFIRMS it), and with nothing beside it to out-rank gets
selected — only the structural backstop stops it. The bars are set so no real file in the corpus
trips them (the coverage/binding-truth regressions enforce it) while a synthetic name-only ledger
proves the backstop fires (test_tiers) — the hole is guarded the moment it closes.
"""
from __future__ import annotations

import re
from typing import List

from . import family

# ── LENGTH gate — the NECESSARY condition (a short sheet is never a dump) ──────
# DATA-CALIBRATED: the largest real WINNER is Analisa `ProfitLoss (23)` at 133 labels (a code-
# labelled but short & hierarchical P&L); the smallest winner-competitive dump is CSS `SourceTB-SAP`
# at 371 (CPM's `SourcePL SAP`, the one that actually mis-won, is 647). 200 sits in the (133, 371]
# gap with asymmetric margin (67 above the real max, 171 below the dump min) — biased toward NOT
# mis-demoting a real statement, the direction that regressed Analisa. Revisit when a file breaks it.
DUMP_MIN_LABELS = 200

# ── corroborator 1: GL-CODE fraction ─────────────────────────────────────────
_GL_CODE_RE = re.compile(r'^\s*\d{3,}\b')          # a label opening with a 3+ digit account code
# 0.30 sits inside the observed 0.00 .. 0.33 gap (real sheets 0%; the lowest real dump — CSS
# `SourcePL-SAP` — 33%). The 3+ digit regex is tuned to NUMERIC codes; alphanumeric / dotted schemes
# ('GL-4000', '4000.01') do NOT match — a KNOWN LIMITATION (strict-xfail in test_tiers), which is
# exactly why GL-code is a SIGNATURE of the flat-ledger invariant, never its definition.
DUMP_GL_FRACTION = 0.30

# ── corroborator 2: STRUCTURAL flatness (name-only dump) ──────────────────────
_DUMP_FLAT_SUBTOTAL_FRACTION = 0.005
# roll-up / subtotal vocabulary — a statement hierarchy's markers. UNIVERSAL accounting words,
# never file-specific. Their PRESENCE is not a statement signal (a dump carries them too); their
# NEAR-ABSENCE over a long sheet is the dump signal.
_SUBTOTAL_TOKENS = ('total', 'subtotal', 'sub-total', 'grand total', 'gross', 'net ',
                    'profit', 'ebitda', 'ebit', 'margin', 'surplus', 'deficit', 'balance')


def leftmost_labels(rows, first_data_row: int, label_col) -> List[str]:
    """The non-empty text labels in the statement's label column, below its axis row — the
    line-item names the tier signatures read. Universal; no fixed column."""
    if label_col is None:
        return []
    out = []
    for r in range(first_data_row, len(rows)):
        if label_col < len(rows[r]):
            v = rows[r][label_col]
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
    return out


def gl_code_fraction(labels: List[str]) -> float:
    if not labels:
        return 0.0
    return sum(1 for l in labels if _GL_CODE_RE.match(l)) / len(labels)


def _subtotal_fraction(labels: List[str]) -> float:
    if not labels:
        return 0.0
    n = sum(1 for l in labels if any(t in l.lower() for t in _SUBTOTAL_TOKENS))
    return n / len(labels)


def is_structural_dump(labels: List[str]) -> bool:
    """Name-only backstop: a LONG flat list of peer accounts with near-zero roll-up lines.
    Length-gated (necessary) — see the module docstring on the asymmetry."""
    return (len(labels) >= DUMP_MIN_LABELS
            and _subtotal_fraction(labels) < _DUMP_FLAT_SUBTOTAL_FRACTION)


def is_dump(rows, first_data_row: int, label_col) -> bool:
    """A candidate sheet is a raw GL/TB DUMP (not a hierarchical statement) ONLY IF it is LONG
    (length is the NECESSARY condition — a short sheet is never a dump, however code-heavy) AND a
    corroborating bulk-data signature fires: GL-code fraction OR the structural flat-list (name-only)
    backstop. Used as a DEMOTION in _best_sheet — a dump ranks below every real statement, even one
    carrying more concepts (a full GL export mentions them all)."""
    labels = leftmost_labels(rows, first_data_row, label_col)
    if len(labels) < DUMP_MIN_LABELS:              # length necessary: short ⇒ never a dump
        return False
    return gl_code_fraction(labels) >= DUMP_GL_FRACTION or is_structural_dump(labels)


def structure_rank(rows, first_data_row: int, label_col) -> int:
    """Co-CONFIRMED tiebreaker (advisor 2026-07-27): when candidate sheets tie on every prior
    key, prefer the one that looks MORE like a statement (has roll-up subtotals) over a flatter
    one, so an arbitrary lex-min never decides between a real statement and a slipped-through
    name-only dump. 0 = statement-shaped, 1 = flat. Sits ABOVE lex-min, BELOW the semantic keys."""
    labels = leftmost_labels(rows, first_data_row, label_col)
    return 0 if _subtotal_fraction(labels) > 0 else 1


def verdict_rank(concept_verdicts) -> int:
    """The best (lowest-preference-index) family verdict among a set — the axis-2 signal for the
    selector, consulted ONLY to break a (tier, concept-count, columns) tie (Finding B: verdict
    must never override concept-count, only order equals — the derived summary tab is often the
    correct source and would lose to a CONFIRMED raw statement otherwise). CONFIRMED(0) beats
    INSUFFICIENT(1) beats CONTRADICTED(2); an empty set is neutral (1)."""
    ranks = [family._PREFERENCE.get(v, 1) for v in concept_verdicts]
    return min(ranks) if ranks else 1
