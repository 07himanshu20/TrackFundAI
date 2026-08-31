"""Permanent regression fixture for the geography-gated monetary frame.

Currency and scale are one 'frame', resolved once per statement. Currency is CONFIRMED
only by POSITIVE, NON-CONFLICTING evidence (a statement token, or a known domicile);
absent or conflicting evidence is AMBIGUOUS ⇒ hold, never INR-by-default. The key dangers
guarded: (1) an INR mention ANYWHERE must never flip a known-foreign entity to INR; (2) the
UNTOKENED-FOREIGN hole — a foreign figure with no marker and unknown domicile must HOLD, never
silently ship as INR (a 15-20× error); (3) any statement-token-vs-domicile conflict holds in
BOTH directions (INR-stmt-vs-foreign AND foreign-stmt-vs-INR), never auto-applied.
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


# ── REDDENING CONTROLS: each fail-OPEN branch must be DEAD. Every assertion proves BOTH the new
# fail-closed behaviour AND the death of the old INR-emit path (ccy is None, not 'INR'; the old flag
# absent). These are the real guarantee — they redden the instant anyone reintroduces fail-open, and
# they can't be bent the way a flipped positive assertion can. ──────────────────────────────────────

def test_untokened_foreign_with_inr_mention_now_HOLDS_not_inr():
    # untokened-foreign hole: no token, domicile UNKNOWN, only a workbook-wide INR mention. USED to emit
    # INR ('currency_inr_by_mention') → a foreign file's FX note names rupees too (15-20× error).
    ccy, esc, _, flags = units.resolve_currency(stmt_currency=None, geo_currency=None, inr_mentioned=True)
    assert esc and ccy is None                                   # new: held
    assert ccy != 'INR' and 'currency_inr_by_mention' not in flags   # negative control: old path dead
    assert 'currency_ambiguous_no_evidence' in flags


def test_no_evidence_at_all_HOLDS_not_default_inr():
    # no token, no domicile, no mention. USED to emit INR ('currency_assumed_inr').
    ccy, esc, _, flags = units.resolve_currency(stmt_currency=None, geo_currency=None, inr_mentioned=False)
    assert esc and ccy is None
    assert ccy != 'INR' and 'currency_assumed_inr' not in flags       # negative control: old path dead
    assert 'currency_ambiguous_no_evidence' in flags


def test_indian_no_label_still_emits_inr_rule_ii_load_bearing():
    # ADJUDICATED (branch 3): a KNOWN India domicile with no token is POSITIVE evidence → INR emits
    # (rule ii). This is unchanged and LOAD-BEARING — Hubler/Clientell/InstaAstro/Aliste/CPC all resolve
    # their currency this way. It is NOT the untokened-foreign hole (that is geo=None).
    ccy, esc, _, _ = units.resolve_currency(stmt_currency=None, geo_currency='INR', inr_mentioned=False)
    assert ccy == 'INR' and not esc


def test_statement_inr_vs_foreign_domicile_is_held():
    # symmetric conflict, direction A (already held pre-fix): a Singapore entity 'reporting' INR is a
    # mislabel → hold, never apply.
    ccy, esc, reason, flags = units.resolve_currency(stmt_currency='INR', geo_currency='SGD', inr_mentioned=False)
    assert esc and ccy is None and any('currency_conflict' in f for f in flags)


def test_foreign_token_vs_indian_domicile_is_held_the_direction_that_used_to_emit():
    # symmetric conflict, direction B (the newly-closed asymmetry): domicile India but the statement
    # carries a foreign token. USED to trust the token and EMIT MYR (escalate=False) — now HELD.
    ccy, esc, _, flags = units.resolve_currency(stmt_currency='MYR', geo_currency='INR', inr_mentioned=False)
    assert esc and ccy is None                                   # new: held
    assert ccy != 'MYR'                                          # negative control: old emit path dead
    assert any('currency_conflict' in f for f in flags)


def test_foreign_no_label_known_domicile_resolves_foreign_then_rate_gated():
    # foreign-no-label with a KNOWN foreign domicile → the foreign currency (rule ii); it then holds
    # downstream for want of a rate, never silently INR. (The geo=None variant holds at currency above.)
    ccy, esc, _, flags = units.resolve_currency(stmt_currency=None, geo_currency='MYR', inr_mentioned=True)
    assert ccy == 'MYR' and not esc and 'inr_mention_ignored_foreign_domicile' in flags


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
