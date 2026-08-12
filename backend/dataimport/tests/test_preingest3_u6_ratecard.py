"""U6 rate-card WIRING (preingest3_views) — the run-input selection + fail-closed
invariant, tested at the view layer (the engine's conversion is proven separately).

The invariant that must never break: a supplied, attributed card is used; an absent
card → INR-only, so a foreign figure fail-closes to a HOLD downstream — NEVER a
silent INR-default conversion. A malformed card raises → the run fails loudly.
"""
import json

import pytest

from dataimport.preingest3_views import _rate_card_for, _parse_rate_card_field
from dataimport.preingest3.ratecard import RateCard, RateCardError

_MYR = {'currency': 'MYR', 'inr_per_unit': 18.6, 'rate_date': '2026-02-28',
        'source': 'ref', 'source_type': 'reference'}


# ── selection: payload → attributed card; absent → INR-only ──────────────────
def test_supplied_card_is_used_as_run_input():
    rc = _rate_card_for({'rate_card': {'as_of': '2026-02-28', 'rates': [_MYR]}}, '2026-02-28')
    assert 'MYR' in rc.rates and rc.rates['MYR'].inr_per_unit == __import__('decimal').Decimal('18.6')
    assert rc.card_id.startswith('rc_')


def test_absent_card_falls_back_to_inr_only():
    rc = _rate_card_for({}, '2026-02-28')
    assert rc.rates == {}                                 # no foreign rate
    assert _rate_card_for(None, '2026-02-28').rates == {}


def test_card_as_of_defaults_to_run_as_of():
    rc = _rate_card_for({'rate_card': {'rates': [_MYR]}}, '2026-02-28')   # no as_of in payload
    assert rc.as_of == '2026-02-28'


def test_malformed_card_raises_loudly_not_silent():
    # a bad rate must halt the run (caught by _run_job's except → job 'failed'),
    # never fall through to an INR default that silently mis-converts.
    with pytest.raises(RateCardError):
        _rate_card_for({'rate_card': {'as_of': '2026-02-28',
                                      'rates': [{'currency': 'MYR', 'inr_per_unit': -1,
                                                 'source': 'x'}]}}, '2026-02-28')


# ── the fail-closed invariant: uncovered currency never silently converts ─────
def test_inr_only_card_holds_uncovered_currency_never_silent_convert():
    rc = _rate_card_for({}, '2026-02-28')                 # INR-only
    assert rc.missing_for(['MYR']) == ['MYR']             # coverage gap is visible
    from decimal import Decimal
    with pytest.raises(RateCardError):                    # conversion REFUSES, not defaults
        rc.to_inr(Decimal('100'), 'MYR')


def test_supplied_card_converts_covered_currency():
    rc = _rate_card_for({'rate_card': {'as_of': '2026-02-28', 'rates': [_MYR]}}, '2026-02-28')
    from decimal import Decimal
    assert rc.missing_for(['MYR']) == []
    assert rc.to_inr(Decimal('100'), 'MYR') == Decimal('1860.0')


# ── parse: dict / JSON string / empty / malformed ────────────────────────────
def test_parse_rate_card_field_forms():
    assert _parse_rate_card_field(None) is None
    assert _parse_rate_card_field('') is None
    assert _parse_rate_card_field({}) is None
    assert _parse_rate_card_field({'rates': []}) == {'rates': []}
    assert _parse_rate_card_field(json.dumps({'rates': [_MYR]}))['rates'][0]['currency'] == 'MYR'
    with pytest.raises(ValueError):
        _parse_rate_card_field('{not valid json')
