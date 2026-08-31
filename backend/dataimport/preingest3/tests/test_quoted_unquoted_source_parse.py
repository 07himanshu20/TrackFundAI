"""Q2 — the ISOLATED source-location parse: SPEC-TESTED-UNTIL-FIELD-TESTED.

Locating the ISIN / exchange / share-type columns in a REAL listed-holdings sheet is the one
piece of Q2 that cannot be validated until such a fund actually arrives — its column layout
is field-dependent. So it is isolated (quoted_unquoted.locate_listing_signals, wired into
fund_anchor as pure text enrichment) and proven here against SYNTHETIC fixtures plus the ISIN
checksum gate. When a real quoted fund shows up, ONLY this locator firms up against its real
layout; the classification logic, partition, tie, and subsheet do not change.

Every file we have today has NO such columns, so this whole path is a verified no-op on them —
asserted by test_listing_columns_absent_is_a_noop (the neutrality guarantee for real files).

Run:
  pytest backend/dataimport/preingest3/tests/test_quoted_unquoted_source_parse.py -p no:cacheprovider -o addopts="" -m ""
"""
from dataimport.preingest3 import fund_anchor, quoted_unquoted as qu


def _by_company(recs):
    return {r['company']: r for r in (recs or [])}


def test_listing_columns_flow_from_schedule_to_records():
    # a synthetic investment schedule that DOES carry listing columns (unlike any real file).
    rows = [
        ['Portfolio holdings'],
        ['Company', 'Cost', 'Fair Value', 'ISIN', 'Stock Exchange', 'Valuation Method'],
        ['ListedCo Ltd', 40, 90, 'US0378331005', 'NYSE', 'Market price'],
        ['PrivateCo Pvt Ltd', 12, 15, None, None, 'EV/EBITDA (comparable)'],
    ]
    recs = _by_company(fund_anchor._schedule_rows_from_sheet(rows))
    assert set(recs) == {'ListedCo Ltd', 'PrivateCo Pvt Ltd'}
    # the listed row carried its ISIN + exchange through the enrichment path
    assert recs['ListedCo Ltd']['isin'] == 'US0378331005'
    assert recs['ListedCo Ltd']['listing_exchange'] == 'NYSE'
    # the private row carried neither (columns blank for it)
    assert recs['PrivateCo Pvt Ltd'].get('isin') is None


def test_parsed_listing_signals_then_classify_end_to_end():
    rows = [
        ['Company', 'Cost', 'Fair Value', 'ISIN', 'Valuation Method'],
        ['ListedCo Ltd', 40, 90, 'US0378331005', 'Market price'],       # hard ISIN anchor ⇒ quoted
        ['PrivateCo Pvt Ltd', 12, 15, None, 'Revenue multiple (ARR)'],   # private method ⇒ unquoted
    ]
    recs = _by_company(fund_anchor._schedule_rows_from_sheet(rows))
    listed = qu.classify(qu.signals_from_fields(recs['ListedCo Ltd']))
    private = qu.classify(qu.signals_from_fields(recs['PrivateCo Pvt Ltd']))
    assert listed.is_quoted is True and listed.basis == qu.STATED
    assert private.is_quoted is False and private.basis == qu.INFERRED


def test_listing_columns_absent_is_a_noop():
    # the real-file case: no ISIN / exchange / share-type columns ⇒ NO listing fields added,
    # classification falls to methodology inference. This is why the wiring is neutral today.
    rows = [
        ['Company', 'Cost', 'Fair Value', 'Sector', 'Valuation Method'],
        ['Analisa Resources', 25, 30, 'Industrials', 'EV/EBITDA (comparable)'],
    ]
    rec = _by_company(fund_anchor._schedule_rows_from_sheet(rows))['Analisa Resources']
    assert 'isin' not in rec and 'listing_exchange' not in rec and 'share_type' not in rec
    assert qu.classify(qu.signals_from_fields(rec)).basis == qu.INFERRED
