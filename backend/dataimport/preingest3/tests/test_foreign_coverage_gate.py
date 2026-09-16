"""RATE-SUPPLIED FOREIGN COVERAGE GATE — locks foreign-currency emits the INR-only gate can't see.

WHY THIS EXISTS (the structural blind spot)
    The standing coverage gate (test_coverage_gate) and the dual-run golden both run under the
    SHIPPING DEFAULT config: an INR-only rate card. Under that card EVERY foreign-currency figure
    correctly HOLDS (fail-closed, no rate). So no foreign emit is ever checked by either gate — a
    latent regression the never-a-wrong-number net cannot catch. This gate closes that blind spot.

WHAT PRODUCTION STATE IT REPRODUCES (the real path, never a reconstruction)
    A real foreign-currency upload reaches an emit only after TWO production steps, both reproduced
    here exactly:
      1. RATE CARD supplied at intake (U6) — the FX rate is a required per-upload run input (it moves
         daily; never a stored default). Here: SGD @ 75.43 (client-supplied).
      2. REVIEW-GATE alias confirmed — a file labelled 'CSS' is not auto-matched to the anchor
         'Chemoscience Pte Ltd' (the resolver fail-closes on a non-distinctive token, correctly
         refusing to guess vs the other 'Chem…' company). A human confirms it once; the alias store
         records it (provenance='human'). Here: seeded in a TEMP store, exactly as the gate would.
    In that confirmed state the SGD frame resolves (domicile='Singapore' is the load-bearing
    positive-evidence input) and CSS emits. This runs the REAL pipeline.run — the same path the GUI
    uses — so the lock is over production behaviour, not a hand-built call.

THE LOCK + THE DISCIPLINE
    • LOCK: every FOREIGN_GT slot must EMIT and match ground truth traced to a raw cell. A regression
      that makes CSS hold again, or shifts its value, fails HERE (the INR-only gate stays green, blind).
    • DISCIPLINE (same as the INR gate): under the supplied card, NO foreign-entity money emit may be
      un-ground-truthed and un-allowlisted. A foreign money figure is either GT-verified-EMIT or HELD.

REUSABLE BY CONSTRUCTION (no per-currency code, ever)
    A new currency/country is DATA: add its rate to RATES, its review-gate confirmation to ALIASES (if
    its file needs disambiguation), and its traced values to FOREIGN_GT. No Singapore/Malaysia branch
    exists or may be written. MYR is now ACTIVATED (#4): Analisa's three cells are traced + locked in
    FOREIGN_GT; Chemopharm still HOLDS (Lever 1b), its reasons kept in MYR_STATUS.

SLOW: runs the whole corpus through the real pipeline. Marked `slow`; skipped if fixtures absent.
"""
import datetime as _dt
import os
import tempfile

import pytest

from backend.dataimport.preingest3 import fund_anchor, pipeline
from backend.dataimport.preingest3.alias_ledger import AliasLedger
from backend.dataimport.preingest3.ratecard import RateCard
from backend.dataimport.preingest3.cir import Figure
# the gate's decision logic lives django-free in the logic module (unit-tested + reddening-proven there)
from backend.dataimport.preingest3.tests.test_foreign_gate_logic import lock_failures, leaked_emits

IN = 'backend/media/preingest/trivesta/100e86d5/in'
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present'),
]

FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']
AS_OF = '2026-06-30'

# Money concepts that pass through currency conversion (headcount is a count — no FX).
MONEY = ('revenue', 'ebitda', 'cash')

# The supplied rate card for THIS upload (DATA, not code). FX is a required per-upload input — one
# rate per currency per upload, never a stored default. Add a currency by adding a row here.
RATES = [
    {'currency': 'SGD', 'inr_per_unit': '75.43', 'source': 'client-supplied 2026 upload'},
    # MYR ACTIVATED (foreign-safety #4): Analisa's revenue/ebitda/cash are now traced + locked in
    # FOREIGN_GT below (the ~9× 'period-basis question' was RESOLVED in #3 — the emit is the TRUE labelled
    # 5-month YTD, not the stale annual). Chemopharm still HOLDS (period ambiguity / Lever 1b), so no
    # Chemopharm MYR number ships. Per the per-upload FX rule: one rate per currency per upload, dated.
    {'currency': 'MYR', 'inr_per_unit': '23.38', 'source': 'client-supplied 2026 upload'},
]

# Review-gate human confirmations (label → anchor key). Exactly what a reviewer resolves once. A file
# whose distinctive token already matches its anchor needs NO entry (Analisa/Chemopharm self-resolve).
ALIASES = {
    '0525_CSS_Monthy_Report_-_Consol_Updated': 'chemoscience pte ltd',
}

# PINNED GROUND TRUTH for foreign emits — each traced to a raw cell × the supplied rate.
#   (entity_company, concept): 'value_cr'
FOREIGN_GT = {
    # Chemoscience Pte Ltd (Singapore, SGD @ 75.43), as-of May-2025 (file period).
    ('Chemoscience Pte Ltd', 'cash'): '17.4248',     # BS!G27 (May) = 2,310,059.87 SGD × 75.43; the May
                                                     # as-of is the Lever-4 fix (trailing 0-filled future
                                                     # months dropped) — NOT the stale Jan column (3.20M).
    ('Chemoscience Pte Ltd', 'revenue'): '30.5607',  # 'ANA & LS' statement, SGD-native × 75.43
    ('Chemoscience Pte Ltd', 'ebitda'): '11.8477',   # 'ANA & LS' statement, SGD-native × 75.43
    # Analisa Resources Sdn Bhd (Malaysia, MYR @ 23.38), as-of May-2025 (file period) — #4 lock.
    # Traced to raw cell × rate; revenue/ebitda cross-checked to the Summary '05 P&L (2)' YTD column.
    ('Analisa Resources Sdn Bhd', 'revenue'): '8.6368',   # '05 Summary P&L'!H6 = 3,694,112.18 MYR (YTD CY25,
                                                          # 5mo) × 23.38 = 8.63683. #3-resolved labelled 5-month
                                                          # YTD (basis=YTD·5mo), NOT the stale annual. Lever 5
                                                          # sub-A re-pointed the source from the normalized
                                                          # variant to the plain Summary — SAME value (revenue
                                                          # is not normalized), provenance sheet changed only.
    ('Analisa Resources Sdn Bhd', 'ebitda'): '0.5459',    # Lever 5 sub-A RE-BASE (DONE): plain/standard EBITDA
                                                          # is now PRIMARY — '05 Summary P&L'!H11 = 233,485.32
                                                          # MYR (YTD) × 23.38 = 0.5459. The engine prefers the
                                                          # plain/actual sheet over the normalized variant ('PL
                                                          # rectify (Normalised)', H18 = 393,262.32 → ₹0.9194)
                                                          # per the prefer-plain policy. The Normalized EBITDA
                                                          # (₹0.9194 = plain + one-time adj H12 159,777, forex
                                                          # reclass) is DISCLOSED in the provenance note;
                                                          # structured Adjusted slot deferred (gated on the
                                                          # downstream basis-awareness fact).
    ('Analisa Resources Sdn Bhd', 'cash'): '16.1842',     # 08 BS!AE2 = 6,922,257.27 MYR (May-2025 cash;
                                                          # AE1=2025-05-31, future months empty → Lever-4
                                                          # keeps May) × 23.38 = 16.18424. BS==CFS CROSS-TIE
                                                          # CONFIRMED: '09 Cash Flow (Jan-May)'!O34 closing =
                                                          # 6922.2574K = 6,922,257.4 MYR == BS (Δ0.11 MYR,
                                                          # thousands-rounding); May opening = April BS. Gold-
                                                          # standard lock, not merely cell-trace + as-of.
}

# Why each REMAINING MYR cell is not a verified emit (kept as an explicit ledger, not a silent gap).
# Analisa's three cells were LOCKED in #4 (traced into FOREIGN_GT above); Chemopharm still HOLDS.
MYR_STATUS = {
    ('Chemopharm Sdn Bhd', 'revenue'): 'HELD (fail-closed) — 5 periods carry ≥2 differing values with no '
        'disambiguator (needs the CPM banner / Lever 1b + period basis). Not a currency hold.',
    ('Chemopharm Sdn Bhd', 'ebitda'): 'HELD — same period ambiguity as revenue.',
    ('Chemopharm Sdn Bhd', 'cash'): 'HELD — money stock rounds to ₹0 (wrong-row bind); needs Lever 1b.',
}

# GENUINELY-UNVERIFIABLE foreign emits only (no ground truth exists to check against). Empty by design.
#   (entity_company, concept): {'reason': ..., 'review_by': 'YYYY-MM-DD'}
ALLOWLIST: dict = {}


def _card() -> RateCard:
    return RateCard.from_input({'as_of': AS_OF, 'rates': RATES})


def _foreign_companies() -> set:
    """Companies whose domicile is NOT India — derived from the fund anchors, no hardcoded names."""
    anchors = fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])
    return {ca.company for ca in anchors.values()
            if (getattr(ca, 'domicile', None) or '').strip().lower() not in ('india', '')}


def _run(store):
    files = [(os.path.splitext(f)[0], os.path.join(IN, f)) for f in sorted(os.listdir(IN))
             if f.lower().endswith('.xlsx') and not f.startswith('~$')]
    return pipeline.run(files, as_of=AS_OF, org='fxgate', rate_card=_card(), alias_store=store)


def _foreign_figs():
    """Run the real pipeline with the card + seeded aliases; return {(company, concept): Figure} for
    every money figure on a foreign-domicile entity."""
    with tempfile.TemporaryDirectory() as tmp:
        store = AliasLedger(org='fxgate', path=os.path.join(tmp, 'alias.json'))
        for label, key in ALIASES.items():
            store.learn([label], key, provenance='human')
        res = _run(store)
    foreign = _foreign_companies()
    out = {}
    for r in res.cir.records:
        if r.domain != 'mis' or r.entity_id not in foreign:
            continue
        for v in r.fields.values():
            if isinstance(v, Figure) and v.concept in MONEY:
                out[(r.entity_id, v.concept)] = v
    return out


def test_allowlist_discipline():
    today = _dt.date.today()
    for key, meta in ALLOWLIST.items():
        assert isinstance(meta, dict) and meta.get('reason'), f'{key}: allowlist entry needs a reason'
        rb = meta.get('review_by')
        assert rb, f'{key}: allowlist entry needs a review_by date'
        assert _dt.date.fromisoformat(rb) >= today, f'{key}: allowlist entry expired ({rb})'
        assert key not in FOREIGN_GT, f'{key}: allowlisted AND ground-truthed — pick one'


def test_foreign_gt_slots_emit_and_match():
    """THE LOCK: every ground-truthed foreign slot must EMIT and equal GT (not merely 'not leak' — a
    held slot would pass a leak check vacuously). This is what guards CSS's SGD emits from regression."""
    problems = lock_failures(_foreign_figs(), FOREIGN_GT)
    assert not problems, ('FOREIGN EMIT LOCK BROKEN (a supplied-rate foreign value regressed):\n' +
                          '\n'.join(problems))


def test_no_unverified_foreign_emit():
    """DISCIPLINE: under the supplied card, every foreign-entity money EMIT is GT-verified or
    (justifiably) allowlisted. An un-GT'd foreign emit is a number nobody checked — audit it to
    verified, or it is wrong and must HOLD."""
    leaked = leaked_emits(_foreign_figs(), FOREIGN_GT, ALLOWLIST)
    assert not leaked, ('UN-VERIFIED FOREIGN EMIT(S) under the supplied card (trace to GT, or the emit '
                        'is wrong and must HOLD — do NOT allowlist a verifiable number):\n' +
                        '\n'.join(leaked))
