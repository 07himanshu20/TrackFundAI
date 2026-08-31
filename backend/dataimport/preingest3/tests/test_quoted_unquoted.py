"""Q2 — quoted/unquoted classification core (the fully-validatable-now half).

Proves, against synthetic fixtures + the ISIN checksum gate (no real quoted fund needed):
  • valid_isin is a real checksum gate — accepts checksum-valid ISINs, REDDENS on a flipped
    digit / bad length / non-alphanumeric body.
  • classify() is positive-evidence and fail-closed with the intended asymmetry: QUOTED needs
    a HARD anchor (ISIN/exchange) or an explicit source statement; a bare 'market price'
    method with no anchor HOLDS; a positive private method INFERS unquoted; every genuine
    contradiction and the no-evidence case HOLD (never a defaulted bucket).
  • the 10 real fund methodologies all infer unquoted (the census outcome, in code).
  • the partition + HARD tie: Σquoted+Σunquoted+Σheld ties to Σ portfolio FV, and the gate
    REDDENS when a company's FV never reaches the classifier (the negative control).
  • the isolated locator finds ISIN/exchange/share-type columns when present, none when absent.

Run:
  pytest backend/dataimport/preingest3/tests/test_quoted_unquoted.py -p no:cacheprovider -o addopts="" -m ""
"""
from decimal import Decimal

import pytest

from dataimport.preingest3 import quoted_unquoted as qu
from dataimport.preingest3.quoted_unquoted import QuotedClass, QUOTED, UNQUOTED, HELD, STATED, INFERRED


# ── the ISIN checksum gate ───────────────────────────────────────────────────
@pytest.mark.parametrize('code', ['US0378331005', 'GB0002634946', 'DE000BAY0017',
                                  'INE009A01021', 'US5949181045', 'FR0000131104'])
def test_valid_isin_accepts_checksum_valid(code):
    assert qu.valid_isin(code) is True


@pytest.mark.parametrize('code', ['US0378331004',        # last digit flipped ⇒ bad checksum
                                  'US037833100',          # 11 chars ⇒ wrong length
                                  'US0378331005X',         # 13 chars
                                  'INE009A0102X',          # non-digit check position ⇒ bad checksum
                                  '12ABCDEFGHI5',          # first two not letters
                                  '', None])
def test_valid_isin_reddens_on_malformed(code):
    assert qu.valid_isin(code) is False


# ── the census, in code: 10 real methodologies all infer unquoted ────────────
@pytest.mark.parametrize('method', [
    'P/B & recent round', 'Latest round price', 'Revenue multiple (ARR)',
    'Revenue multiple (post part-exit)', 'Cost / last-round (flat)',
    'EV/EBITDA (comparable)', 'EV/EBITDA (post part-exit)'])
def test_real_methodologies_infer_unquoted(method):
    c = qu.classify({'valuation_method': method})
    assert c.is_quoted is False
    assert c.classification == UNQUOTED and c.basis == INFERRED   # labelled inferred, not stated


# ── QUOTED requires a hard anchor or an explicit source statement ────────────
def test_hard_isin_anchor_is_quoted_stated():
    c = qu.classify({'valuation_method': 'anything at all', 'isin': 'US0378331005'})
    assert c.is_quoted is True and c.basis == STATED and 'ISIN' in c.evidence


def test_named_exchange_is_quoted_stated():
    c = qu.classify({'listing_exchange': 'NSE'})
    assert c.is_quoted is True and c.basis == STATED


def test_stated_share_type_listed_is_quoted():
    assert qu.classify({'share_type': 'Listed equity'}).is_quoted is True


def test_stated_share_type_unlisted_is_unquoted_stated():
    c = qu.classify({'share_type': 'Unlisted equity'})
    assert c.is_quoted is False and c.basis == STATED


def test_ipev_level_1_quoted_level_3_unquoted():
    assert qu.classify({'ipev_level': 1}).is_quoted is True
    assert qu.classify({'ipev_level': 3}).is_quoted is False


# ── fail-closed: the ambiguous middle and every contradiction HOLD ───────────
def test_market_price_method_without_anchor_holds():
    # the crux: a 'market price' method with NO ISIN/exchange is a claim of quoted-ness we
    # cannot confirm ⇒ HELD, never force-bucketed as quoted.
    c = qu.classify({'valuation_method': 'Market price'})
    assert c.is_quoted is None and c.classification == HELD


def test_conflict_hard_anchor_but_stated_unlisted_holds():
    c = qu.classify({'isin': 'US0378331005', 'share_type': 'unlisted'})
    assert c.is_quoted is None and 'conflict' in c.reason


def test_conflict_method_names_both_market_and_private_holds():
    c = qu.classify({'valuation_method': 'market price cross-checked with DCF'})
    assert c.is_quoted is None and c.classification == HELD


def test_no_evidence_holds_never_defaults_unquoted():
    for sig in ({'valuation_method': ''}, {'valuation_method': 'proprietary black box'}, {}):
        c = qu.classify(sig)
        assert c.is_quoted is None, 'no-evidence must HOLD, not default to unquoted'


# ── partition + the HARD tie control ─────────────────────────────────────────
def _c(kind):
    return {'q': QuotedClass(QUOTED, STATED), 'u': QuotedClass(UNQUOTED, INFERRED),
            'h': QuotedClass(HELD, 'held')}[kind]


def test_partition_buckets_untouched_fv():
    items = [('A', Decimal('231'), _c('u')), ('B', Decimal('40'), _c('q')),
             ('C', Decimal('15'), _c('h'))]
    p = qu.partition(items)
    assert p.unquoted_cr == Decimal('231') and p.quoted_cr == Decimal('40') and p.held_cr == Decimal('15')
    assert p.total_cr == Decimal('286') and p.bucket_total_cr == p.total_cr
    assert p.unquoted == ['A'] and p.quoted == ['B'] and p.held == ['C']


def test_held_fv_none_contributes_zero_but_is_named():
    items = [('A', Decimal('100'), _c('u')), ('B', None, _c('h'))]
    p = qu.partition(items)
    assert p.held_cr == Decimal('0') and p.held == ['B']       # named, but not an implicit 0 in a sum
    assert p.total_cr == Decimal('100')


def test_tie_check_passes_when_partition_matches_portfolio_total():
    items = [('A', Decimal('231'), _c('u')), ('B', Decimal('40'), _c('q'))]
    p = qu.partition(items)
    chk = qu.tie_check(p, Decimal('271'))
    assert chk['class'] == 'hard' and chk['status'] == 'pass'


def test_tie_check_reddens_when_a_company_fv_is_dropped():
    # negative control: the classifier saw only 2 of 3 companies (a join/drop bug) ⇒ its
    # partition total (271) ≠ the independently computed Σ portfolio FV (286) ⇒ HARD fail.
    items = [('A', Decimal('231'), _c('u')), ('B', Decimal('40'), _c('q'))]   # 'C' (15) dropped
    p = qu.partition(items)
    chk = qu.tie_check(p, Decimal('286'))
    assert chk['status'] == 'fail', 'a dropped company MUST redden the partition tie'


def test_tie_check_indeterminate_when_total_unavailable():
    p = qu.partition([('A', Decimal('100'), _c('u'))])
    assert qu.tie_check(p, None)['status'] == 'indeterminate'


# ── the isolated, spec-tested source-location parse ──────────────────────────
def test_locate_listing_signals_finds_columns():
    hdr = ['Company', 'Cost', 'FV', 'ISIN Code', 'Stock Exchange', 'Listing Status', 'Val Method']
    got = qu.locate_listing_signals(hdr)
    assert got == {'isin': 3, 'listing_exchange': 4, 'share_type': 5}


def test_locate_listing_signals_empty_when_absent():
    # the case for every file we have today — no ISIN/exchange/share-type column present.
    assert qu.locate_listing_signals(['Company', 'Cost', 'Fair Value', 'Sector', 'Val Method']) == {}


def test_locate_then_valid_isin_flows_to_quoted():
    # the forward-looking capability, end to end on a synthetic listed row: locate the ISIN
    # column, read a checksum-valid ISIN, classify quoted. (Field layout validation deferred.)
    hdr = ['Company', 'ISIN', 'FV']
    row = ['SomeCo Ltd', 'US0378331005', 90]
    cols = qu.locate_listing_signals(hdr)
    c = qu.classify({'isin': row[cols['isin']]})
    assert c.is_quoted is True and c.basis == STATED


# ── the REAL-file E2E: the census outcome, on the production path (slow) ──────
import os

_IN = 'backend/media/preingest/trivesta/100e86d5/in'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(_IN), reason='real fixture files not present')
def test_quoted_unquoted_on_real_files_is_all_inferred_unquoted_and_ties():
    # THE Q2 proof on the REAL 15 files via the REAL pipeline: this fund carries no source
    # listing data, so every holding must classify UNQUOTED with basis INFERRED (from its
    # private valuation method), the quoted and held buckets must be EMPTY, and the partition
    # must tie to the independently-summed Σ portfolio FV (the HARD control passes). Also the
    # NEUTRALITY guarantee: Σ portfolio FV is unchanged (~₹827 Cr) — the overlay re-labels,
    # never re-values.
    from decimal import Decimal
    from dataimport.preingest3 import pipeline, master_workbook
    from dataimport.preingest3.ratecard import default_inr_card
    from dataimport.preingest3.cir import Figure
    files = [(f, os.path.join(_IN, f)) for f in sorted(os.listdir(_IN)) if f.lower().endswith('.xlsx')]
    as_of = '2026-06-30'
    result = pipeline.run(files, as_of=as_of, org='q2e2e', rate_card=default_inr_card(as_of))
    cir = result.cir
    inv = [r for r in cir.records if r.domain == 'portfolio_investments']
    assert inv, 'expected portfolio_investments records from the fund valuation schedule'

    # every holding: unquoted, inferred (no source listing data on any real file)
    for r in inv:
        assert r.fields.get('is_quoted') is False, f'{r.fields.get("company")} should be unquoted'
        assert r.fields.get('quoted_basis') == INFERRED
        assert r.fields.get('isin') is None and r.fields.get('listing_exchange') is None

    # buckets: quoted and held EMPTY; unquoted = all
    n_q = sum(1 for r in inv if r.fields.get('is_quoted') is True)
    n_h = sum(1 for r in inv if r.fields.get('is_quoted') is None)
    assert n_q == 0 and n_h == 0

    # NEUTRALITY: Σ portfolio FV unchanged (the ~₹827 Cr headline), independent of the overlay
    sum_fv = sum(f.value_cr for r in inv if isinstance(f := r.fields.get('fair_value'), Figure)
                 and f.confirmed)
    assert Decimal('800') < sum_fv < Decimal('850'), f'Σ FV drifted to {sum_fv} — overlay is not neutral'

    # the HARD partition tie passes on the production path
    tie = next((c for c in cir.checks if c.get('id') == 'quoted_unquoted_partition_ties_to_total'), None)
    assert tie is not None and tie['class'] == 'hard' and tie['status'] == 'pass', tie

    # the always-present subsheet exists in the assembled workbook
    wb = master_workbook.build_master(cir, files=[f for f, _ in files])
    assert 'QUOTED_UNQUOTED' in wb.sheetnames
