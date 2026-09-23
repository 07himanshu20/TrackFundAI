"""Reddening control for the cost_basis invested-amount drop bug.

The Stage-1 classifier prompt offers BOTH synonyms for a portfolio "Cost"
column: `Amount_Invested -> total_invested` AND `Cost_of_Investment ->
cost_basis`. Which one the (non-deterministic) model picks varies run to run.
_persist_portfolio only ever read total_invested / tranche_amount / amount, so
the run that mapped "Cost (₹Cr)" -> cost_basis produced 10 rows that were ALL
skipped as "no amount":
    investments 10 -> 0, valuations 10 -> 0 (valuations attach to investments),
    active FV, deployment, MOIC, RVPI, residual NAV all collapsed to blank.
The previous run happened to map to total_invested, so the bug was latent.

_invested_amount must recover the amount from EITHER synonym, with the historical
chain winning first so a row that already carries total_invested is unchanged.
"""
from decimal import Decimal

from dataimport.phase2_persister import _invested_amount


def test_cost_basis_only_row_recovers_amount_REDDENING():
    # the exact shape the broken run emitted — every one of these was dropped
    assert _invested_amount({'company_name': 'Agnikul Cosmos', 'cost_basis': '65'}) == Decimal('65')
    assert _invested_amount({'cost_basis': '110'}) == Decimal('110')
    # before the fix this returned None -> row skipped -> 0 investments


def test_historical_synonyms_still_win_first():
    # preservation: when total_invested is present the result is byte-identical
    # to the old chain (cost_basis is a last-resort fallback, never an override)
    assert _invested_amount({'total_invested': '65', 'cost_basis': '999'}) == Decimal('65')
    assert _invested_amount({'tranche_amount': '18', 'cost_basis': '999'}) == Decimal('18')
    assert _invested_amount({'amount': '25', 'cost_basis': '999'}) == Decimal('25')


def test_no_amount_stays_none_fail_closed():
    # a row with no cost signal of any kind must still be skipped (under-coverage
    # beats a fabricated 0) — the `if not amount: continue` guard must still fire
    assert _invested_amount({'company_name': 'Header Row', 'sector': 'X'}) is None
    assert _invested_amount({}) is None
    assert _invested_amount({'cost_basis': ''}) is None
    assert _invested_amount({'cost_basis': None}) is None
