"""U6 Phase 3 — rate-card intake: two CO-EQUAL paths (manual entry / uploaded schedule) into one
validated, card_id-stamped card, and the fail-closed gate that makes a manual rate as trustworthy as
a sourced schedule.

The controls prove the gate FIRES on any VALIDITY failure (a mistyped/garbage rate is refused WHOLE,
never coerced), that both paths are genuinely equal (identical input → identical card_id), that a VALID
rate the run does not need is ACCEPTED-and-noted rather than refused (the standing/superset-card case),
and — at the run level — that supplying a currency's rate CONVERTS it while a currency with no rate (or
one blocked upstream) stays held. An AMBIGUOUS-currency figure is never converted even with a full card
(it is held at the currency choke, upstream of any rate lookup).

Run:
  pytest backend/dataimport/preingest3/tests/test_ratecard_intake.py -p no:cacheprovider -o addopts="" -m ""
"""
import os

import pytest

from backend.dataimport.preingest3 import units
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.ratecard_intake import (
    IntakeError, card_from_manual, card_from_schedule, noop_currencies, _decimal_shift_k)

AS_OF = '2026-06-30'


def _entry(**kw):
    e = {'currency': 'MYR', 'rate': '18.6', 'date': AS_OF, 'source': 'RBI ref'}
    e.update(kw)
    return e


# ── the happy paths, and the two are genuinely CO-EQUAL ──────────────────────────────────────────────
def test_manual_entry_builds_a_stamped_card():
    card = card_from_manual([_entry()], as_of=AS_OF)
    assert 'MYR' in card.rates and str(card.rates['MYR'].inr_per_unit) == '18.6'
    assert card.rates['MYR'].source == 'RBI ref' and card.card_id.startswith('rc_')
    assert card.as_of == AS_OF


def test_manual_and_schedule_produce_the_SAME_card():
    # equal weight is not a slogan: the identical rate, typed vs uploaded, yields the identical card_id.
    manual = card_from_manual([_entry(rate='18.6')], as_of=AS_OF)
    sched = card_from_schedule([['Currency', 'INR per unit', 'Rate Date', 'Source'],
                                ['MYR', 18.6, AS_OF, 'RBI ref']], as_of=AS_OF)
    assert manual.card_id == sched.card_id
    assert sched.rates['MYR'].source == 'RBI ref'


def test_pair_forms_all_resolve_to_the_foreign_side():
    for pair in ('MYR', 'MYR/INR', 'INR-MYR', 'MYR→INR'):
        card = card_from_manual([_entry(currency=pair)], as_of=AS_OF)
        assert 'MYR' in card.rates, pair


# ── the fail-closed GATE: each violation is REFUSED, not coerced (reddening) ──────────────────────────
@pytest.mark.parametrize('bad, why', [
    (_entry(rate='-1'), 'negative rate'),
    (_entry(rate='0'), 'zero rate'),
    (_entry(rate='abc'), 'non-numeric rate'),
    (_entry(rate=''), 'empty rate'),
    (_entry(date='2026-03-31'), 'date != as_of'),
    (_entry(date=''), 'missing date'),
    (_entry(source=''), 'missing source'),
    (_entry(currency='INR'), 'base currency needs no rate'),
    (_entry(currency='XX'), 'not a 3-letter code'),
    (_entry(currency='MYR/SGD'), 'ambiguous pair — no INR base, two foreign'),
    (_entry(currency=''), 'empty currency'),
])
def test_gate_refuses_bad_manual_entry(bad, why):
    with pytest.raises(IntakeError):
        card_from_manual([bad], as_of=AS_OF)


def test_all_or_nothing_one_bad_entry_refuses_the_WHOLE_card():
    # a partially-accepted card would silently drop a rate — fail-closed means all-or-nothing.
    with pytest.raises(IntakeError):
        card_from_manual([_entry(currency='MYR'), _entry(currency='SGD', rate='-2')], as_of=AS_OF)


def test_empty_intake_is_refused():
    with pytest.raises(IntakeError):
        card_from_manual([], as_of=AS_OF)


# ── rate-entry sanity band (#2): the fat-finger decimal-shift guard ───────────────────────────────────
# A typo band is a COMPARISON against the currency's OWN prior rate (reference-only, never a conversion
# input). A near-power-of-ten shift is a decimal-point slip → REFUSE fail-closed; a genuine % move passes;
# NO reference (first upload) → cannot fire (disclosed unverified at the API layer, never faked); an
# explicit human confirm overrides. Reddening BOTH directions + the pure fingerprint.
@pytest.mark.parametrize('new,ref,k', [
    ('233.8', '23.38', 1),      # ×10 over-type — the canonical 23.38 vs 233.8 fat-finger
    ('2.338', '23.38', -1),     # ÷10 under-type
    ('2338', '23.38', 2),       # ×100
    ('26.0', '23.38', None),    # a real ~11% FX move — NOT a power of ten
    ('14.0', '18.6', None),     # a real ~25% move — NOT a power of ten
    ('250.0', '23.0', 1),       # ×10 typo even with ~9% underlying drift (intended ~25) — still caught
    # tightness (must-not-misfire): genuine FX volatility is never a near-exact 10× — even LARGE real moves
    # must pass. The band flags only a ratio in ~[7.08×, 14.1×] (|log10 − 1| ≤ 0.15), so:
    ('27.9', '18.6', None),     # a big but real 1.5× move — passes
    ('37.2', '18.6', None),     # a 2× move (doubling) — passes
    ('93.0', '18.6', None),     # even a 5× move (log 0.70, nearest k=1 off by 0.30) — still passes
])
def test_decimal_shift_fingerprint(new, ref, k):
    assert _decimal_shift_k(new, ref) == k


def test_decimal_shift_needs_a_reference():
    assert _decimal_shift_k('233.8', None) is None      # first upload — cannot fire, never faked


def test_band_refuses_a_decimal_shift_typo_against_the_prior_rate():
    # MUST-HANDLE: 233.8 entered where the currency's prior rate was 23.38 → ×10 slip → refused whole.
    with pytest.raises(IntakeError):
        card_from_manual([_entry(rate='233.8', reference_rate='23.38')], as_of=AS_OF)


def test_band_passes_a_genuine_fx_move():
    # MUST-NOT-MISFIRE: a real ±% move vs the prior rate is not a power of ten → builds normally.
    card = card_from_manual([_entry(rate='19.2', reference_rate='18.6')], as_of=AS_OF)
    assert str(card.rates['MYR'].inr_per_unit) == '19.2'


def test_band_cannot_fire_without_a_reference():
    # first upload for the currency — no prior rate → the band does not block (the disclosed-unverified
    # path is handled at the API layer), never a faked band or a per-currency magic range.
    card = card_from_manual([_entry(rate='233.8')], as_of=AS_OF)   # no reference_rate supplied
    assert str(card.rates['MYR'].inr_per_unit) == '233.8'


def test_explicit_confirm_overrides_the_band_and_is_load_bearing():
    # human-in-loop: an unusual (power-of-ten) rate the user ATTESTS is intended builds — proving the band
    # is a confirm-gate, not a hard block. This is also the reddening neutralizer: WITHOUT 'confirmed' the
    # same entry is refused (test_band_refuses_... above), so the band is load-bearing, not incidental.
    card = card_from_manual([_entry(rate='233.8', reference_rate='23.38', confirmed=True)], as_of=AS_OF)
    assert str(card.rates['MYR'].inr_per_unit) == '233.8'


# ── validity ≠ actionability: a VALID rate the run does not need is ACCEPTED-and-noted, not refused ────
def test_valid_rate_for_a_non_uncovered_currency_is_accepted_and_noted():
    # SUPERSEDES the old refuse-non-actionable rule. The run flags only MYR uncovered; a user pastes a
    # standing card carrying both MYR and SGD. Both are VALID, so the card is BUILT whole (nothing dropped)
    # and SGD — valid but not needed this run — is DISCLOSED as a no-op, never a card-refusing error. This
    # is what lets a standing/org card (or a pasted full card) ingest on a run that needs only some pairs.
    card = card_from_manual([_entry(currency='MYR'), _entry(currency='SGD', rate='63')], as_of=AS_OF)
    assert 'MYR' in card.rates and 'SGD' in card.rates                 # superset accepted whole
    noop = noop_currencies(card, uncovered={'MYR'})
    assert [n['currency'] for n in noop] == ['SGD']                    # SGD surfaced as 'supplied, not needed'
    assert noop and 'not rate-actionable' in noop[0]['reason']         # with a disclosure reason
    assert noop_currencies(card, uncovered={'MYR', 'SGD'}) == []       # both actionable → nothing to note
    assert noop_currencies(card, uncovered=None) == []                 # no run set → nothing to disclose


def test_validity_failure_still_refuses_the_whole_superset_card():
    # reddening: making actionability non-fatal must NOT let a BAD number ride in on a no-op currency.
    # a superset whose SGD rate is malformed is still refused WHOLE — validity is the only refusal left.
    with pytest.raises(IntakeError):
        card_from_manual([_entry(currency='MYR'), _entry(currency='SGD', rate='-9')], as_of=AS_OF)


# ── schedule parsing is format-agnostic (fuzzy headers) and never guesses a layout ───────────────────
def test_schedule_fuzzy_headers_and_spacer_rows():
    card = card_from_schedule([['FX reference rates'],
                               ['CCY', 'INR Rate', 'as of', 'Provider'],
                               ['MYR', 18.6, AS_OF, 'RBI'],
                               ['', '', '', ''],                       # spacer — skipped, not a rate line
                               ['USD', 83.0, AS_OF, 'RBI']], as_of=AS_OF)
    assert set(card.rates) == {'MYR', 'USD'}


def test_schedule_with_no_recognisable_header_is_refused():
    with pytest.raises(IntakeError):
        card_from_schedule([['some notes'], ['not', 'a', 'rate', 'sheet']], as_of=AS_OF)


# ── an AMBIGUOUS-currency figure is never converted, even with a full card (held at the choke) ────────
def test_ambiguous_currency_frame_holds_even_with_a_full_card():
    full = card_from_manual([_entry(currency='MYR')], as_of=AS_OF)
    fr = units.resolve_monetary_frame(stmt_currency=None, geo_currency=None, inr_mentioned=True,
                                      declared_unit='crore', sample_values=[5], anchor_cr=None,
                                      ratecard=full)
    assert fr.escalate and fr.currency is None        # ambiguous currency → held BEFORE any rate lookup


# ── run-level convert-on-card, on real files (slow) ──────────────────────────────────────────────────
_IN = 'backend/media/preingest/trivesta/100e86d5/in'


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isdir(_IN), reason='real fixture files not present')
def test_myr_card_converts_myr_and_leaves_sgd_disclosed():
    from backend.dataimport.preingest3 import pipeline
    files = [(f, os.path.join(_IN, f)) for f in sorted(os.listdir(_IN)) if f.lower().endswith('.xlsx')]

    # baseline (reddening): INR-only card → MYR is UNCOVERED, a prompt is required
    base = pipeline.run(files, as_of=AS_OF, org='p3base', rate_card=default_inr_card(AS_OF)).currency_report
    assert 'MYR' in [u['currency'] for u in base['uncovered']] and base['prompt_required'] is True

    # supply the MYR rate via the INTAKE path (typed). Intake accepts any VALID rate; actionability
    # (which currencies this run needs) is a separate disclosure, not a build-time filter.
    card = card_from_manual([_entry(currency='MYR', rate='18.6')], as_of=AS_OF)
    rep = pipeline.run(files, as_of=AS_OF, org='p3myr', rate_card=card).currency_report

    # MYR moved uncovered → covered (rate applied); nothing rate-actionable remains → no prompt
    assert 'MYR' not in [u['currency'] for u in rep['uncovered']]
    assert 'MYR' in [c['currency'] for c in rep['covered']]
    assert rep['prompt_required'] is False
    assert rep['card_id'] == card.card_id
    # SGD/Chemoscience is STILL only disclosed (blocked upstream) — a MYR rate never falsely surfaced it
    assert 'Chemoscience Pte Ltd' in [e['entity'] for e in rep['foreign_domicile_unresolved']]
    assert 'SGD' not in [c['currency'] for c in rep['covered']]


if __name__ == '__main__':
    print('run via pytest')
