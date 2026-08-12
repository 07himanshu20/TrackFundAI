"""Permanent fixture for the currency-convert-AND-disclose path (foreign files:
CPM/CSS/Analisa are SGD/MYR). A converted number is only trustworthy if the rate
that produced it is disclosed with its source, and an UNCOVERED foreign currency
must be reported (blocked), never silently treated as INR / zero.
"""
from decimal import Decimal

from backend.dataimport.preingest3.ratecard import RateCard
from backend.dataimport.preingest3.cir import CIR
from backend.dataimport.preingest3 import assemble


def _card():
    return RateCard.from_input({'as_of': '2026-02-28', 'rates': [
        {'currency': 'SGD', 'inr_per_unit': Decimal('62.0'), 'source': 'RBI ref', 'source_type': 'reference'}]})


def test_fx_conversion_is_correct_and_inr_passes_through():
    rc = _card()
    assert rc.to_inr(Decimal('100'), 'SGD') == Decimal('6200.0')   # 100 SGD × 62
    assert rc.to_inr(Decimal('100'), 'INR') == Decimal('100')      # base passes through
    assert rc.to_inr(Decimal('0'), 'SGD') == Decimal('0')


def test_uncovered_foreign_currency_is_reported_not_silently_inr():
    rc = _card()
    assert rc.missing_for(['SGD', 'MYR', 'INR']) == ['MYR']        # MYR uncovered → blocked upstream
    assert rc.missing_for(['SGD', 'INR']) == []


def test_ratecard_disclosure_sheet_names_currency_and_source():
    rc = _card()
    wb = assemble.build(CIR(as_of='2026-02-28', rate_card_id=rc.card_id), rate_card=rc)
    assert '_RateCard' in wb.sheetnames
    txt = ' '.join(str(c.value) for row in wb['_RateCard'].iter_rows()
                   for c in row if c.value is not None)
    assert 'SGD' in txt and 'RBI' in txt and '62' in txt          # rate + source visible


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
