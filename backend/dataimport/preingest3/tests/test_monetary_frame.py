"""Permanent regression fixture for the geography-gated monetary frame.

Currency and scale are one 'frame', resolved once per statement. Currency is CONFIRMED
only by POSITIVE, NON-CONFLICTING evidence (a statement token, or a known domicile);
absent or conflicting evidence is AMBIGUOUS ⇒ hold, never INR-by-default. The key dangers
guarded: (1) an INR mention ANYWHERE must never flip a known-foreign entity to INR; (2) the
UNTOKENED-FOREIGN hole — a foreign figure with no marker and unknown domicile must HOLD, never
silently ship as INR (a 15-20× error); (3) any statement-token-vs-domicile conflict holds in
BOTH directions (INR-stmt-vs-foreign AND foreign-stmt-vs-INR), never auto-applied.
"""
from decimal import Decimal

from backend.dataimport.preingest3 import profiler, units
from backend.dataimport.preingest3.ratecard import RateCard, default_inr_card


def _myr_card():
    return RateCard.from_input({'as_of': '2026-06-30', 'rates': [
        {'currency': 'MYR', 'inr_per_unit': '18.6', 'rate_date': '2026-06-30', 'source': 'test'}]})


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


# ── USER-CONFIRMED BASE CURRENCY (batch/fund confirm-prompt) — a THIRD positive-evidence source,
# weaker than a file token or domicile, DISCLOSED distinctly, with the conflict guard that keeps a
# genuinely-foreign file from ever taking the batch currency. ────────────────────────────────────────

def test_user_confirmed_base_currency_resolves_when_no_file_evidence():
    # no statement token, no domicile, but the user confirmed the batch base currency = INR. This is
    # the batch confirm-prompt payoff: the 10 Hubler/Aliste/InstaAstro/CPC holds emit via THIS rule.
    ccy, esc, reason, flags = units.resolve_currency(
        stmt_currency=None, geo_currency=None, inr_mentioned=False, base_currency='INR')
    assert ccy == 'INR' and not esc
    assert 'currency_user_confirmed' in flags                     # tagged for the audit trail
    assert 'user-confirmed base currency' in reason              # disclosed distinctly (not file-detected)


def test_foreign_statement_token_conflicts_with_batch_base_currency_HOLDS():
    # THE conflict-guard reddening control (CSS/CPM live case): a file whose OWN statement carries a
    # foreign token (SGD) must STILL HOLD under a batch INR confirmation — never take the batch currency.
    ccy, esc, _, flags = units.resolve_currency(
        stmt_currency='SGD', geo_currency=None, inr_mentioned=False, base_currency='INR')
    assert esc and ccy is None                                    # held, NOT forced to INR
    assert ccy != 'INR'                                           # negative control: batch never overrides a foreign token
    assert any('currency_conflict' in f for f in flags)


def test_domicile_outranks_user_confirmed_base_currency():
    # a KNOWN entity domicile (more specific than a batch-wide assertion) wins: a Singapore-domiciled
    # entity under a batch INR confirmation resolves SGD (rule ii), not the batch INR.
    ccy, esc, reason, _ = units.resolve_currency(
        stmt_currency=None, geo_currency='SGD', inr_mentioned=False, base_currency='INR')
    assert ccy == 'SGD' and not esc and 'domicile-implied' in reason


def test_base_currency_none_is_byte_identical_still_holds():
    # base_currency defaults to None → the no-evidence path is UNCHANGED (holds), so the whole feature
    # is inert unless a base currency is supplied — the byte-identical guarantee for the default path.
    ccy, esc, _, flags = units.resolve_currency(
        stmt_currency=None, geo_currency=None, inr_mentioned=False, base_currency=None)
    assert esc and ccy is None and 'currency_ambiguous_no_evidence' in flags


def test_file_token_still_wins_over_base_currency():
    # a statement's OWN INR token is file-detected evidence and agrees with the batch INR → emits INR,
    # and (implicitly) is NOT tagged user-confirmed (the absence of the tag means file-determined).
    ccy, esc, reason, flags = units.resolve_currency(
        stmt_currency='INR', geo_currency=None, inr_mentioned=False, base_currency='INR')
    assert ccy == 'INR' and not esc
    assert 'currency_user_confirmed' not in flags and 'statement-header' in reason


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


# ── #3 Q2 — FOREIGN-SCOPED SCALE CORROBORATION (the scale×FX 1000× door) ─────────────────────────────
# A FOREIGN statement's declared scale is trusted ONLY when the whole-company anchor can corroborate it;
# a bare foreign banner (a stale/template "'000") applied with no cross-check is a 10^k error compounded
# by the FX rate. Uniform across ALL foreign currencies (config-B pattern), NEVER per-currency. INR/
# domestic is untouched — anchored MIS are covered by the magnitude-lie check, and the fund's own ₹Cr
# books are the trusted, self-tied backbone. All corpus foreign cos are anchored ⇒ blast radius 0 today.

def test_foreign_declared_scale_without_anchor_HOLDS_the_1000x_door():
    # MUST-HANDLE: a MYR statement declares 'thousands' with NO whole-company anchor to corroborate it.
    # USED to trust the banner and apply ×1000 (a stale/template '000 → 1000× × FX). Now HELD.
    fr = units.resolve_monetary_frame(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=False,
                                      declared_unit='thousands', sample_values=[15342397.11],
                                      anchor_cr=None, ratecard=_myr_card())
    assert fr.escalate and fr.scale is None
    assert 'not corroborated' in fr.reason


def test_foreign_no_anchor_door_reddening_neutralized(monkeypatch):
    # REDDENING: neutralize the door (restore trust-when-uncheckable) and the SAME foreign no-anchor
    # statement applies 'thousands' again — the 1000× reappears. Proves the gate is load-bearing.
    monkeypatch.setattr(units, '_foreign_scale_uncorroborated', lambda *a, **k: False)
    ss = units.resolve_statement_scale(declared_unit='thousands', currency='MYR',
                                       sample_values=[15342397.11], anchor_cr=None, ratecard=_myr_card())
    assert ss.scale == 'thousands' and not ss.escalate      # the wrong-number path, laid bare


def test_foreign_declared_scale_with_consistent_anchor_emits():
    # MUST-NOT-MISFIRE: a GENUINE foreign 'thousands' statement whose top line sits in-band with its
    # anchor is corroborated → emits at 'thousands'. 5000 (thousands) MYR ×18.6 = ₹9.3Cr = the anchor;
    # 'absolute' would be 3 decades low. The door only holds the UNCORROBORATED case.
    fr = units.resolve_monetary_frame(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=False,
                                      declared_unit='thousands', sample_values=[5000],
                                      anchor_cr=Decimal('9.3'), ratecard=_myr_card())
    assert not fr.escalate and fr.scale == 'thousands'


def test_foreign_declared_scale_lie_still_held_with_anchor():
    # the real CPM shape: a MYR 'thousands' banner over ABSOLUTE values, anchor ₹239Cr → 'thousands' is
    # ~2 decades off while 'absolute' fits → magnitude-lie HOLD (corroboration present, scale contradicted).
    fr = units.resolve_monetary_frame(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=False,
                                      declared_unit='thousands', sample_values=[15342397.11],
                                      anchor_cr=Decimal('239.1'), ratecard=_myr_card())
    assert fr.escalate and fr.scale is None


def test_no_declared_unit_foreign_anchor_resolves_absolute():
    # MUST-NOT-MISFIRE (no-banner → anchor → absolute): the common foreign case (stale banner NOT read)
    # + a ₹239Cr anchor → the anchor resolves 'absolute' (CPM/Analisa's safe path). 15.34M MYR ×18.6 =
    # ₹28.5Cr fits; 'thousands' is out of band. The door does not apply (no declared unit).
    fr = units.resolve_monetary_frame(stmt_currency='MYR', geo_currency='MYR', inr_mentioned=False,
                                      declared_unit=None, sample_values=[15342397.11],
                                      anchor_cr=Decimal('239.1'), ratecard=_myr_card())
    assert not fr.escalate and fr.scale == 'absolute'


def test_domestic_declared_scale_without_anchor_STILL_emits_the_backbone():
    # MUST-NOT-MISFIRE + documents RESIDUAL (a): the foreign door must NOT touch INR/domestic. An INR
    # statement declaring 'crore' with NO anchor still EMITS. KNOWN RESIDUAL: a DOMESTIC portfolio co
    # missing from fund records (no anchor) + a stale READ banner is not caught here — rare (domestic
    # cos are normally anchored) and kept shut in practice by the banner read-gap below; revisit if a
    # real domestic-no-anchor case appears.
    fr = units.resolve_monetary_frame(stmt_currency='INR', geo_currency='INR', inr_mentioned=True,
                                      declared_unit='crore', sample_values=[826.4],
                                      anchor_cr=None, ratecard=default_inr_card('2026-06-30'))
    assert not fr.escalate and fr.scale == 'crore'


def test_fund_scale_trust_is_a_documented_assumption_residual_b():
    # DOCUMENTED ASSUMPTION (residual b, pre-existing, OUTSIDE #3's foreign scope): the fund domain
    # declares its own scale ('Rs Cr') and is trusted for it. Its internal cross-ties (NAV = called −
    # distributed + P&L; fees = 2% committed) corroborate RELATIONSHIPS but NOT a UNIFORM scale error —
    # ×100 on every figure leaves the identities intact. #3's foreign door neither adds nor removes this
    # trust; a fund-scale corroboration is a separate, later question. This control makes it explicit.
    fr = units.resolve_monetary_frame(stmt_currency='INR', geo_currency='INR', inr_mentioned=True,
                                      declared_unit='crore', sample_values=[500, 826.4, 1000],
                                      anchor_cr=None, ratecard=default_inr_card('2026-06-30'))
    assert not fr.escalate and fr.scale == 'crore'


def test_banner_read_gap_glued_and_curly_apostrophe_documented():
    # DOCUMENTED RESIDUAL: the "'000" unit token is NOT read from a currency-glued or curly-apostrophe
    # banner — token_present needs an alnum boundary, and CPM/Analisa write "In MYR'000"/"(RM’000)"
    # ('000 glued to the currency; curly ’). This currently PROTECTS coverage (Analisa's stale MYR'000
    # is ignored → its anchor picks 'absolute' → it emits). #3's door-close is UNIT-AGNOSTIC (anchor-
    # corroboration, not banner-reading), so the gap is left closed deliberately; this control locks the
    # current not-read behavior so any future widening of the read is a conscious, reddening change.
    assert profiler.token_present("'000", "in myr'000") is False        # glued to 'myr' → not a whole token
    assert profiler.token_present("'000", "(rm’000)") is False          # curly apostrophe → not matched
    assert profiler.token_present("'000", "rm '000") is True            # spaced → WOULD match (it's the boundary)


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
