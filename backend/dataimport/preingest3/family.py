"""Statement-FAMILY classification for the net (Step-5 stage-0).

The net has TWO orthogonal axes. This module owns the FIRST: FAMILY — which statement type
a concept belongs to (income statement / balance sheet / cash-flow statement), and whether
a candidate sheet actually IS that family. The SECOND axis (TIER: primary statement >
derived sheet > raw GL/TB dump) is the selector's job and lives elsewhere.

A family is CONFIRMED BY ITS OWN ACCOUNTING IDENTITY, never a caption guess:
  • income statement  ⟺ revenue − cogs = gross_profit ; gross_profit − opex = ebitda
  • balance sheet     ⟺ assets = liabilities + equity
  • cash-flow statement⟺ opening_cash + (receipts − payments) = closing_cash

TWO NON-NEGOTIABLE PROPERTIES (advisor 2026-07-26), encoded in the 3-valued verdict:

  ADDITIVE, NEVER A GATE (Trap A). Confirmation DISAMBIGUATES competing COMPLETE statements
  and adds confidence; it must NEVER require a complete statement to exist. A summary/KPI
  sheet (LDC `Consolidated MIS`: a flow + a stock + a count on one tab) legitimately lacks
  the full identity — that is INSUFFICIENT, which means "fall back to per-concept
  verification", NOT "reject". Only CONTRADICTED (concepts present AND the identity fails) is
  a negative signal. So a currently-correct emit can never be subtracted by this module.

  IDENTITY CONFIRMS FAMILY, IT DOES NOT REJECT A DUMP (Trap B). A trial-balance dump BALANCES
  by construction (debits = credits ⇒ assets = liabilities + equity), so the balance-sheet
  identity returns CONFIRMED on BOTH the real balance sheet AND the SAP dump. Rejecting the
  dump is the TIER selector's job (presentation structure), NOT this identity's. The
  test-suite documents this explicitly so no later stage assumes the identity filtered dumps.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

# 3-valued family-confirmation verdict.
CONFIRMED = 'confirmed'          # the identity is computable AND holds
CONTRADICTED = 'contradicted'    # the identity is computable AND fails — the ONLY negative
INSUFFICIENT = 'insufficient'    # too few concepts to test — degrade gracefully, never reject

INCOME_STATEMENT = 'income_statement'
BALANCE_SHEET = 'balance_sheet'
CASH_FLOW = 'cash_flow'

# Concept → AUTHORITATIVE statement family. Spanning concepts (a concept that legitimately
# appears on >1 statement) are resolved by intended meaning + concept_nature, NOT a naive
# name lookup — enumerated here so the classifier never silently picks the wrong statement:
#   • cash        — STOCK on the balance sheet; its MOVEMENTS (receipts/payments) are the CFS.
#                   concept_nature already routes cash(stock)→BS, receipts/payments(flow)→CFS.
#   • opening_cash/closing_cash — the CFS bridge endpoints (also the BS cash balance).
#   • net_income  — IS bottom line, CFS top line, and the equity bridge. Authoritative = IS.
#   • depreciation— appears on IS and (as an add-back) CFS. Authoritative = IS.
# headcount has NO financial family/identity (operational) → family-agnostic, per-concept only.
CONCEPT_FAMILY: Dict[str, str] = {
    'revenue': INCOME_STATEMENT, 'cogs': INCOME_STATEMENT, 'gross_profit': INCOME_STATEMENT,
    'opex': INCOME_STATEMENT, 'ebitda': INCOME_STATEMENT, 'net_income': INCOME_STATEMENT,
    'depreciation': INCOME_STATEMENT,
    'assets': BALANCE_SHEET, 'liabilities': BALANCE_SHEET, 'equity': BALANCE_SHEET,
    'cash': BALANCE_SHEET,
    'opening_cash': CASH_FLOW, 'closing_cash': CASH_FLOW,
    'receipts': CASH_FLOW, 'payments': CASH_FLOW,
}


def _tol(target: Decimal, rel: Decimal = Decimal('0.02')) -> Decimal:
    return max(abs(target) * rel, Decimal('0.01'))


def _d(values: Dict[str, object], key: str) -> Optional[Decimal]:
    v = values.get(key)
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _fold(checks) -> str:
    """Fold the per-identity outcomes: any real CONTRADICTION dominates; else any CONFIRM;
    else INSUFFICIENT. A contradiction is a strong negative (a concept present but the books
    don't add up), so it is never masked by a second, computable-but-passing identity."""
    seen_ok = False
    for lhs, rhs in checks:                       # each check is (lhs, rhs) or None if not computable
        if lhs is None:
            continue
        if abs(lhs - rhs) <= _tol(rhs if rhs != 0 else lhs):
            seen_ok = True
        else:
            return CONTRADICTED
    return CONFIRMED if seen_ok else INSUFFICIENT


def confirm_income_statement(values: Dict[str, object]) -> str:
    """revenue − cogs = gross_profit — the RELIABLE, per-period income-statement identity (the
    accounting DEFINITION of gross profit, which holds in any single period).

    The gross_profit − opex = ebitda link is deliberately NOT checked. Real P&Ls carry D&A
    INSIDE operating expenses (EBITDA adds it back) plus other income/expense, so that link
    fails on legitimate statements — Hubler shows a consistent D&A gap (gross_profit − opex =
    EBIT, not EBITDA). Checking it would manufacture a false CONTRADICTED on a real income
    statement. The caller reads all inputs from a COMMON period column, so a collapse-window
    mismatch (each row summing a slightly different trailing run) can't fake a failure either."""
    rev, cogs, gp = _d(values, 'revenue'), _d(values, 'cogs'), _d(values, 'gross_profit')
    if rev is None or cogs is None or gp is None:
        return INSUFFICIENT
    return _fold([(rev - cogs, gp)])


def confirm_balance_sheet(values: Dict[str, object]) -> str:
    """assets = liabilities + equity. NB (Trap B): a trial-balance dump satisfies this too —
    this confirms BALANCE-SHEET FAMILY, it does not reject a dump (that is the tier selector)."""
    a, l, e = _d(values, 'assets'), _d(values, 'liabilities'), _d(values, 'equity')
    if a is None or l is None or e is None:
        return INSUFFICIENT
    return _fold([(a, l + e)])


def confirm_cash_flow(values: Dict[str, object]) -> str:
    """opening_cash + (receipts − payments) = closing_cash (the CFS bridge)."""
    op, cl = _d(values, 'opening_cash'), _d(values, 'closing_cash')
    rc, pm = _d(values, 'receipts'), _d(values, 'payments')
    if op is None or cl is None or rc is None or pm is None:
        return INSUFFICIENT
    return _fold([(op + rc - pm, cl)])


def confirm_family(family: str, values: Dict[str, object]) -> str:
    return {INCOME_STATEMENT: confirm_income_statement,
            BALANCE_SHEET: confirm_balance_sheet,
            CASH_FLOW: confirm_cash_flow}[family](values)


# Disambiguation preference for the Increment-2 CONSUME step: among COMPETING candidate
# sources for one concept, prefer CONFIRMED > INSUFFICIENT > CONTRADICTED (an actively-failing
# statement is a worse source than a merely-untestable one — but still a source).
_PREFERENCE = {CONFIRMED: 0, INSUFFICIENT: 1, CONTRADICTED: 2}


def select_source(candidates):
    """candidates: list of (source_id, verdict). Return the source_id to prefer.

    THE ADDITIVE RULE, encoded (advisor Trap A / refinement 2): a SOLE candidate is ALWAYS
    returned regardless of its verdict — CONTRADICTED and INSUFFICIENT NEVER gate a sole source
    (the caller falls back to per-concept verification + raises a statement-integrity disclosure
    when it's CONTRADICTED). Verdict preference ONLY breaks a tie between competing candidates;
    equal verdicts break by source_id (stable, deterministic — the lex-min backstop sitting UNDER
    the semantic order, never overriding it)."""
    if not candidates:
        return None
    return min(candidates, key=lambda sv: (_PREFERENCE.get(sv[1], 3), sv[0]))[0]
