"""FOUNDATIONAL guard: the stock-vs-flow nature table is a single-point-of-truth for
THREE mechanisms — the zero-stock emit guard, the stock/flow bind guard + cash≡closing_cash
routing, and the CF1 collapse (stock=latest balance, flow=period sum). It has been WRONG
once (cash was labelled a flow), and one mislabel silently corrupts all three consumers.
So every entry is pinned against accounting first principles: a STOCK is a balance at an
instant (never period-summed / annualised); a FLOW accrues over a period.

Also documents the DEFAULT-FLOW trap: any concept absent from the table defaults to 'flow',
so a future balance-sheet stock (receivables/payables/inventory/debt) MUST be added as
'stock' when introduced, or it will be wrongly summed/annualised.
"""
from backend.dataimport.preingest3.contract import CONCEPT_NATURE, concept_nature
from backend.dataimport.preingest3.extract import MIS_CONCEPTS

# Ground-truth accounting classification, independent of the source table.
FLOWS = {'revenue', 'cogs', 'gross_profit', 'opex', 'ebitda', 'net_income',
         'receipts', 'payments', 'called', 'distributed', 'proceeds'}
STOCKS = {'cash', 'assets', 'liabilities', 'equity', 'opening_cash', 'closing_cash',
          'headcount', 'fair_value', 'cost', 'commitment'}


def test_every_table_entry_matches_accounting_ground_truth():
    for c in FLOWS:
        assert concept_nature(c) == 'flow', f'{c} must be a FLOW (accrues over a period)'
    for c in STOCKS:
        assert concept_nature(c) == 'stock', f'{c} must be a STOCK (instant balance)'
    # the table must not have drifted beyond what we've ground-truthed
    assert set(CONCEPT_NATURE) == FLOWS | STOCKS, \
        'CONCEPT_NATURE changed — re-audit the new/removed entry against ground truth'


def test_cash_family_natures_are_correct():
    # the exact mislabels that bit us: cash / opening / closing are STOCK balances;
    # receipts / payments are the FLOWS between them (cash-flow identity).
    assert concept_nature('cash') == 'stock'
    assert concept_nature('opening_cash') == 'stock'
    assert concept_nature('closing_cash') == 'stock'
    assert concept_nature('receipts') == 'flow'
    assert concept_nature('payments') == 'flow'


def test_all_mis_concepts_are_natured_explicitly():
    # the 4 output targets must never rely on the default — an unlisted target would
    # default to 'flow' and a stock (cash) would be wrongly period-summed.
    for c in MIS_CONCEPTS:
        assert c in CONCEPT_NATURE, f'MIS target {c!r} missing from CONCEPT_NATURE (would default to flow)'


def test_unknown_concept_defaults_to_flow_documented_trap():
    # DOCUMENTED behaviour, not endorsed: unknown → flow. A future BS stock added as a
    # concept but forgotten here would be silently mis-natured. This test exists so that
    # contract is explicit and any future BS-stock concept forces a table update.
    assert concept_nature('receivables') == 'flow'   # NOT because it's a flow — it's unlisted
    assert concept_nature('inventory') == 'flow'      # add these as 'stock' when they become concepts
