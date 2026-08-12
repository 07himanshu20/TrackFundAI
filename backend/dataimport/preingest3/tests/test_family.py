"""Family-identity confirmers (net Step-5 stage-0). Pure, synthetic — no real files.
Locks the two non-negotiable properties from the advisor as executable assertions:
  • ADDITIVE, never a gate (Trap A): a summary sheet lacking the full identity is INSUFFICIENT
    (→ fall back to per-concept verification), NEVER CONTRADICTED — so it can't subtract a good emit.
  • Identity confirms family, does NOT reject a dump (Trap B): a BALANCED trial-balance set is
    CONFIRMED by the balance-sheet identity — dump rejection is the tier selector's job, not this.
"""
import pytest

from backend.dataimport.preingest3 import family as F
from backend.dataimport.preingest3.pipeline import _statement_is_fund_level


# ── income statement (revenue − cogs = gross_profit, the reliable per-period identity) ──
def test_income_statement_confirms_on_gross_profit_definition():
    assert F.confirm_income_statement({'revenue': 100, 'cogs': 60, 'gross_profit': 40}) == F.CONFIRMED


def test_income_statement_contradicted_when_gross_profit_breaks():
    assert F.confirm_income_statement({'revenue': 100, 'cogs': 60, 'gross_profit': 50}) == F.CONTRADICTED


def test_ebitda_link_is_NOT_an_identity_real_pnl_still_confirms():
    # A real P&L carries D&A inside opex (EBITDA adds it back) so gross_profit − opex ≠ ebitda.
    # That link must NOT be checked — Hubler's real shape (a consistent D&A gap) must still
    # CONFIRM via gross profit, not be falsely CONTRADICTED. This is the calibration finding.
    assert F.confirm_income_statement(
        {'revenue': 100, 'cogs': 60, 'gross_profit': 40, 'opex': 30, 'ebitda': 5}) == F.CONFIRMED
    # only revenue+ebitda (no cogs/gross_profit) → untestable → INSUFFICIENT, never a guess
    assert F.confirm_income_statement({'revenue': 100, 'ebitda': 20}) == F.INSUFFICIENT


# ── balance sheet ───────────────────────────────────────────────────────────
def test_balance_sheet_confirms_and_contradicts():
    assert F.confirm_balance_sheet({'assets': 100, 'liabilities': 60, 'equity': 40}) == F.CONFIRMED
    assert F.confirm_balance_sheet({'assets': 100, 'liabilities': 60, 'equity': 50}) == F.CONTRADICTED


def test_trap_B_balanced_dump_is_confirmed_not_rejected():
    # A trial-balance dump BALANCES by construction — the identity CONFIRMS family on it just
    # as on the real BS. Rejecting the dump is the TIER selector's job (Increment 2), NOT this.
    assert F.confirm_balance_sheet(
        {'assets': 1_000_000_000, 'liabilities': 600_000_000, 'equity': 400_000_000}) == F.CONFIRMED


# ── cash flow ───────────────────────────────────────────────────────────────
def test_cash_flow_confirms_and_contradicts():
    assert F.confirm_cash_flow(
        {'opening_cash': 10, 'receipts': 100, 'payments': 80, 'closing_cash': 30}) == F.CONFIRMED
    assert F.confirm_cash_flow(
        {'opening_cash': 10, 'receipts': 100, 'payments': 80, 'closing_cash': 45}) == F.CONTRADICTED


# ── Trap A: additive, never a gate ──────────────────────────────────────────
def test_trap_A_summary_sheet_is_insufficient_never_contradicted():
    # LDC `Consolidated MIS`-shape: a flow + a stock + a count on one tab, no full statement.
    # EVERY family must return INSUFFICIENT (→ per-concept fallback), NEVER CONTRADICTED, so the
    # currently-verified LDC/InstaAstro cash emits can never be gated out by family confirmation.
    summary = {'revenue': 316, 'cash': 242, 'headcount': 309}
    assert F.confirm_income_statement(summary) == F.INSUFFICIENT
    assert F.confirm_balance_sheet(summary) == F.INSUFFICIENT
    assert F.confirm_cash_flow(summary) == F.INSUFFICIENT


def test_partial_identity_is_insufficient_not_contradicted():
    # revenue alone, or assets without the other two, cannot be tested → INSUFFICIENT, not a negative.
    assert F.confirm_income_statement({'revenue': 100}) == F.INSUFFICIENT
    assert F.confirm_balance_sheet({'assets': 100}) == F.INSUFFICIENT
    assert F.confirm_cash_flow({'opening_cash': 10, 'closing_cash': 30}) == F.INSUFFICIENT


@pytest.mark.xfail(strict=True, reason=(
    "INTERIM fund guard is a count-threshold heuristic: a company statement with ≥2 legit "
    "fund-ish lines (loan commitment, facility drawdown) false-positives to fund-level. Phase-D "
    "replaces it with a fund STRUCTURAL-SPINE classifier confirmed by a fund identity "
    "(called+uncalled=commitment / NAV bridge) — when that lands this xpasses; flip to a normal test."))
def test_company_with_incidental_fund_lines_should_stay_mis():
    # DESIRED (Phase D): an income statement whose spine is company P&L, merely carrying a couple
    # of incidental fund-ish lines, is NOT fund-level. The interim count heuristic gets this wrong.
    rows = [['Revenue'], ['COGS'], ['Gross Profit'], ['EBITDA'],
            ['Loan commitment (facility)'], ['Facility drawdown'], ['Cash & bank']]
    assert _statement_is_fund_level(rows, 0) is False


def test_confirm_family_dispatch_and_concept_map():
    assert F.confirm_family(F.BALANCE_SHEET, {'assets': 100, 'liabilities': 60, 'equity': 40}) == F.CONFIRMED
    # spanning concepts resolved to their authoritative statement
    assert F.CONCEPT_FAMILY['cash'] == F.BALANCE_SHEET          # stock on the BS
    assert F.CONCEPT_FAMILY['receipts'] == F.CASH_FLOW          # cash movement
    assert F.CONCEPT_FAMILY['net_income'] == F.INCOME_STATEMENT
    assert F.CONCEPT_FAMILY['revenue'] == F.INCOME_STATEMENT and F.CONCEPT_FAMILY['ebitda'] == F.INCOME_STATEMENT
    assert 'headcount' not in F.CONCEPT_FAMILY                  # operational — no financial family/identity
