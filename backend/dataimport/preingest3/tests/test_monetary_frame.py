"""Permanent regression fixture for the geography-gated monetary frame.

Currency and scale are one 'frame', resolved once per statement. The key danger
the fixture guards: an INR mention ANYWHERE in a workbook must never flip a
known-foreign entity's statement to INR (a Malaysian company's MYR sheet is not
rupees because some FX note names them). Geography gates the INR fallback.
"""
from backend.dataimport.preingest3 import units


def test_statement_header_currency_wins():
    ccy, esc, _, _ = units.resolve_currency(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=True)
    assert ccy == 'MYR' and not esc


def test_inr_mention_never_overrides_foreign_domicile():
    # no statement currency, Malaysian domicile, workbook mentions INR somewhere
    ccy, esc, _, flags = units.resolve_currency(stmt_currency=None, geo_currency='MYR', inr_mentioned=True)
    assert ccy == 'MYR' and not esc
    assert 'inr_mention_ignored_foreign_domicile' in flags


def test_inr_fallback_only_when_domicile_unknown():
    ccy, esc, _, _ = units.resolve_currency(stmt_currency=None, geo_currency=None, inr_mentioned=True)
    assert ccy == 'INR' and not esc


def test_statement_inr_vs_foreign_domicile_is_held():
    # a Singapore entity 'reporting' INR in-sheet is a mislabel → escalate, never apply
    ccy, esc, reason, flags = units.resolve_currency(stmt_currency='INR', geo_currency='SGD', inr_mentioned=False)
    assert esc and ccy is None
    assert 'currency_inr_vs_foreign_domicile' in flags


def test_default_inr_when_no_evidence():
    ccy, esc, _, flags = units.resolve_currency(stmt_currency=None, geo_currency=None, inr_mentioned=False)
    assert ccy == 'INR' and not esc and 'currency_assumed_inr' in flags


def test_frame_holds_all_when_scale_unresolved():
    # currency resolves (INR) but no unit label and no anchor → whole frame escalates
    fr = units.resolve_monetary_frame(stmt_currency=None, geo_currency='INR', inr_mentioned=True,
                                      declared_unit=None, sample_values=[1234567], anchor_cr=None,
                                      ratecard=None)
    assert fr.escalate and fr.scale is None and fr.currency == 'INR'


def test_declared_unit_that_lies_by_orders_is_held_on_emit_path():
    # EMIT-PATH root fix (premise #2): Agnikul-BS-class — the header declares 'millions' but the
    # values are ABSOLUTE rupees. Under 'millions' the top line is ~10^8 Cr (5.4 decades off a
    # ₹464Cr anchor) while 'absolute' fits (0.6). resolve_statement_scale previously TRUSTED the
    # label unconditionally and shipped a 10^6-wrong number; it must now HOLD the whole statement.
    from backend.dataimport.preingest3.ratecard import default_inr_card
    from decimal import Decimal
    fr = units.resolve_monetary_frame(stmt_currency='INR', geo_currency='INR', inr_mentioned=True,
                                      declared_unit='millions', sample_values=[1176646229],
                                      anchor_cr=Decimal('464'), ratecard=default_inr_card('2026-02-28'))
    assert fr.escalate and fr.scale is None


def test_honest_declared_unit_still_emits_on_emit_path():
    # the cross-check is coverage-NEUTRAL on honest files (proven on all 10): a genuinely-millions
    # statement, plausible vs its anchor, is trusted and emits at 'millions' — only a clear
    # magnitude LIE (declared ≥2 decades off with another scale <1) is held.
    from backend.dataimport.preingest3.ratecard import default_inr_card
    from decimal import Decimal
    fr = units.resolve_monetary_frame(stmt_currency='INR', geo_currency='INR', inr_mentioned=True,
                                      declared_unit='millions', sample_values=[5000],
                                      anchor_cr=Decimal('500'), ratecard=default_inr_card('2026-02-28'))
    assert not fr.escalate and fr.scale == 'millions'


def test_whole_company_anchor_grosses_up_minority_stake():
    # fund paid ₹16Cr for a 20% stake → implied entry valuation ≈ ₹80Cr
    a = units.whole_company_anchor(cost_cr=16, ownership_frac=0.20, fair_value_cr=None)
    assert abs(float(a) - 80.0) < 1e-6
    # COST preferred over FV (stable, non-circular, avoids over-high anchor) →
    # 16 / 0.25 = 64, NOT the FV-based 30 / 0.25 = 120
    a2 = units.whole_company_anchor(cost_cr=16, ownership_frac=0.25, fair_value_cr=30)
    assert abs(float(a2) - 64.0) < 1e-6
    # FV only as a fallback when cost is absent
    a3 = units.whole_company_anchor(cost_cr=None, ownership_frac=0.25, fair_value_cr=30)
    assert abs(float(a3) - 120.0) < 1e-6


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
