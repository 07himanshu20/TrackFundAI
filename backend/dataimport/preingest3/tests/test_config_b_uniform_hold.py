"""config-B — the untokened-foreign 'misread-as-INR' wrong-number path, closed by a UNIFORM fail-closed hold.

A resolved entity with NO positive currency evidence — no statement-header token, no known domicile, no
user-confirmed base currency — must HOLD ALL its monetary concepts UNIFORMLY, never silently emit them as
INR (a 15-20× error). Positive currency evidence, universally, is: statement token › domicile→currency
(INCLUDING INR for an Indian domicile) › user-confirmed base. Only the TRUE unknown holds; a domestic INR
entity (an INR token OR an Indian domicile) still emits — the coverage-collapse guard. A workbook-wide INR
mention is NOT evidence (a foreign file carries rupee FX notes) and must never rescue.

The uniform hold is STRUCTURAL: one MonetaryFrame is resolved per statement and applied to every money
figure, so an escalating frame holds them as a group (never one emitted, one held). This file is the
negative control ([[feedback_negative_control_gates]]) proving that path REDDENS. Measured latent on the
corpus today — every production emit routes through the fail-closed resolve_monetary_frame — and closed
regardless (the last silent-INR default, the dead per-figure units.resolve(), is now fail-closed too) so
no future re-wiring can re-open it.

CPC is the natural production witness: no statement currency token (local_ccy=None) with inr_mentioned=True.
WITH its Indian domicile it emits; with the domicile LOST it holds ALL monetary concepts — and the catch is
the uniform hold, NOT membership of the domicile-derived foreign set (a domicile-lost entity isn't in it).

Run:
  PYTHONPATH=backend DJANGO_SETTINGS_MODULE=config.settings TFAI_ENV=local \
    python -m pytest backend/dataimport/preingest3/tests/test_config_b_uniform_hold.py -q -p no:cacheprovider
"""
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import units
from backend.dataimport.preingest3.extract import extract_company
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure

IN = 'backend/media/preingest/trivesta/100e86d5/in'
RC = default_inr_card('2026-02-28')                       # INR-only production card
_MONEY = ('revenue', 'ebitda', 'cash')


def _frame(stmt, geo, inr_mentioned=False, base=None):
    return units.resolve_monetary_frame(stmt_currency=stmt, geo_currency=geo,
                                        inr_mentioned=inr_mentioned, declared_unit='crore',
                                        sample_values=[Decimal('5')], anchor_cr=None, ratecard=RC, base_currency=base)


# ── PURE-LOGIC reddening: the single currency choke (fast, no fixtures) ────────────────────────────────
def test_no_currency_evidence_escalates():
    # config-B must-handle: no token, no domicile, no base → the frame escalates (all money holds).
    assert _frame(stmt=None, geo=None).escalate


def test_inr_mention_alone_does_not_rescue():
    # THE config-B linchpin: a workbook INR mention is not on-figure evidence (a foreign file's rupee FX
    # note) — it must NOT turn an untokened, domicile-less figure into an INR emit.
    assert _frame(stmt=None, geo=None, inr_mentioned=True).escalate


def test_indian_domicile_is_positive_evidence_and_emits():
    # coverage-collapse guard: an Indian domicile IS positive evidence → INR resolves, frame does NOT
    # escalate (a domestic, often-tokenless file must not be swept into the hold).
    fr = _frame(stmt=None, geo='INR')
    assert not fr.escalate and fr.currency == 'INR'


def test_statement_token_and_base_currency_are_positive_evidence():
    assert not _frame(stmt='INR', geo=None).escalate                 # header token
    assert not _frame(stmt=None, geo=None, base='INR').escalate      # user-confirmed base


def test_dead_per_figure_resolver_is_fail_closed_on_no_evidence():
    # the last silent-INR default (units.resolve(), dead today) now escalates instead of assuming INR,
    # so a future re-wiring of normalize.py cannot re-open config-B.
    ur = units.resolve(value=Decimal('100'), declared_unit='crore', declared_ccy=None,
                       header_hints=None, sheet_hints=None, domicile=None, anchor_cr=None, ratecard=RC)
    assert ur.escalate and ur.currency is None
    # negative control: a real token still resolves (not 'hold everything')
    ur2 = units.resolve(value=Decimal('100'), declared_unit='crore', declared_ccy='INR',
                        domicile=None, anchor_cr=None, ratecard=RC)
    assert not ur2.escalate and ur2.currency == 'INR'


# ── PRODUCTION-PATH reddening: real extract_company, currency gated for the whole entity ───────────────
CPC = ('CPC', 'CPC_Monthly_MIS-_May_25_to_be_sent_to_EL.xlsx')      # no ccy token; inr_mentioned=True
LDC = ('LDC', 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx')       # carries an INR statement token


def _money(name, fn, domicile):
    rec = extract_company(name, os.path.join(IN, fn), rate_card=RC, entity=name,
                          domicile=domicile, anchor_cr=None, use_model=False)
    figs = {v.concept: v for v in rec.fields.values() if isinstance(v, Figure)}
    return {c: figs.get(c) for c in _MONEY}


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')
def test_domicile_lost_untokened_entity_holds_ALL_money_uniformly():
    # MUST-HANDLE (config-B): CPC has no statement currency token; with the domicile LOST there is no
    # positive evidence → EVERY monetary concept holds, together (uniform), citing the frame reason.
    m = _money(*CPC, domicile=None)
    for c in _MONEY:
        f = m[c]
        assert f is not None and (f.held or f.gap) and f.value_cr is None, \
            f'{c} must HOLD when currency is unresolved (config-B misread-as-INR door), got {f.value_cr if f else None}'
    # uniformity: not one emitted while another held
    states = {('HOLD' if (m[c] is None or m[c].held or m[c].gap) else 'EMIT') for c in _MONEY}
    assert states == {'HOLD'}, f'monetary concepts must hold as a GROUP, got {states}'
    # the reason names the true cause (no positive currency evidence), and it is NOT an INR emit
    assert any('currency' in (m[c].hold_reason or '').lower() for c in _MONEY), \
        'the hold must disclose the currency-evidence cause, not silently drop'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')
def test_uniform_hold_is_load_bearing(monkeypatch):
    # NEGATIVE CONTROL ([[feedback_negative_control_gates]]): reintroduce the config-B defect at the single
    # currency choke — force it to silently assume INR whenever it would have escalated — and CPC(domicile=
    # None) must REVERT to emitting misread-as-INR money. Proves the uniform hold above is caused by the
    # fail-closed frame, not something incidental (a green-only gate is unproven).
    import backend.dataimport.preingest3.units as u
    real = u.resolve_currency

    def forced(*, stmt_currency, geo_currency, inr_mentioned, base_currency=None):
        ccy, esc, reason, flags = real(stmt_currency=stmt_currency, geo_currency=geo_currency,
                                       inr_mentioned=inr_mentioned, base_currency=base_currency)
        if esc:                                   # the defect: assume INR instead of holding
            return ('INR', False, 'FORCED config-B defect', [])
        return (ccy, esc, reason, flags)

    monkeypatch.setattr(u, 'resolve_currency', forced)
    m = _money(*CPC, domicile=None)
    assert any((m[c] is not None and not (m[c].held or m[c].gap)) for c in _MONEY), \
        'with the config-B defect reintroduced, CPC(domicile=None) must emit misread-as-INR money — ' \
        'proves the uniform hold is load-bearing, not incidental'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')
def test_domestic_inr_entity_still_emits_not_over_held():
    # MUST-NOT-MISFIRE (coverage-collapse guard): the same CPC WITH its Indian domicile emits (domicile-INR
    # is positive evidence); and LDC with a LOST domicile still emits because it carries an INR statement
    # token — the uniform hold must never over-hold a legitimately-INR entity.
    cpc = _money(*CPC, domicile='india')
    assert any((cpc[c] is not None and not (cpc[c].held or cpc[c].gap)) for c in _MONEY), \
        'CPC with an Indian domicile must EMIT (domicile-INR is positive evidence)'
    ldc = _money(*LDC, domicile=None)
    assert any((ldc[c] is not None and not (ldc[c].held or ldc[c].gap)) for c in _MONEY), \
        'LDC with an INR statement token must EMIT even with a lost domicile (token is positive evidence)'
