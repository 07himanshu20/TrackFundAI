"""COVERAGE GATE (advisor Step 2) — the standing guard that no number ships unverified.

WHAT IT MEASURES: every slot of a FIXED target schema (the MIS concepts × the MIS files),
so coverage is comparable run-to-run and never flatters itself by only counting what it
found. Each slot lands in exactly ONE of three buckets:
  • VERIFIED-EMIT   — emits AND matches ground truth traced to a raw cell (a correct number)
  • UNVERIFIED-EMIT — emits but is NOT ground-truthed (a RISK: a number nobody has checked)
  • HELD            — no value (fail-closed; SAFE, not an accuracy failure)

THE HARD ASSERTION: zero UN-ALLOWLISTED unverified-emit. A number that emits must be
either ground-truthed (GT) or explicitly, justifiably allowlisted — never silently trusted.

PRODUCTION CONFIG, NOT A RICHER HYPOTHETICAL: extraction uses EXACTLY what _run_job passes
— default_inr_card (INR-only) + fund anchors built from the fund files (pipeline.run builds
them internally). A gate measured on a supplied multi-currency card would report a green
production does not have (foreign-currency money correctly HOLDS under the shipping card).

THE ALLOWLIST IS A DISCIPLINED VALVE, NOT AN ESCAPE HATCH: an entry is ONLY for a
GENUINELY UNVERIFIABLE emit (no ground truth exists in the file to check it against). It is
NEVER for "not audited yet" — that must be value-audited down to verified-or-held. Every
entry carries a per-entry justification and a review-by date (enforced below: a past date
fails the gate), so the valve cannot quietly become a graveyard. It is EMPTY today: the
value-audit resolved all four prior unverified emits (two headcounts verified against their
period-end cells; CPM revenue and Analisa ebitda correctly HOLD under the INR-only card).

SLOW: extracts all 10 MIS files. Marked `slow`; skipped if fixtures absent.
"""
import datetime as _dt
import os
from decimal import Decimal

import pytest

from backend.dataimport.preingest3 import fund_anchor
from backend.dataimport.preingest3.extract import extract_company, MIS_CONCEPTS
from backend.dataimport.preingest3.ratecard import default_inr_card
from backend.dataimport.preingest3.cir import Figure

IN = 'backend/media/preingest/trivesta/100e86d5/in'
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present'),
]

FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']
# (label, filename, fund-anchor token)
MIS = [
    ('Hubler', 'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx', 'hubbler'),
    ('Agnikul', 'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx', 'agnikul'),
    ('Clientell', 'AVF_2026_03_16_P_Clientell_MIS_Feb26.xlsx', 'clientell'),
    ('InstaAstro', 'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx', 'instaastro'),
    ('LDC', 'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx', 'ldc'),
    ('Aliste', 'AVF_2026_03_26_P_Aliste_MIS_Feb26.xlsx', 'aliste'),
    ('CPC', 'CPC_Monthly_MIS-_May_25_to_be_sent_to_EL.xlsx', 'cpc'),
    ('CPM', '0625_CPM_Monthly_Report-R1.xlsx', 'chemopharm'),
    ('CSS', '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx', 'css'),
    ('Analisa', '01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx', 'analisa'),
]

# PINNED GROUND TRUTH — every value traced to a raw cell this session (verdict, not count).
# These live in the TEST (the ruler), never in the engine; the extractor still derives each
# number universally. Adding a value here is a claim "this emit is confirmed correct".
GT = {
    ('Hubler', 'revenue'): '4.1837', ('Hubler', 'ebitda'): '-0.4304',
    ('Hubler', 'headcount'): '13',          # fork-b re-source: KPIs!AZ146 'Total employees', 28-Feb-26
    ('Hubler', 'cash'): '1.0621',           # R4 recovery: P&L!BB98 'Closing balance' latest = 10,620,993

    ('Agnikul', 'revenue'): '5.1460', ('Agnikul', 'cash'): '117.6646',
    ('Agnikul', 'headcount'): '299',        # Feb'26 period-end (B29); NOT an average/sum
    ('InstaAstro', 'ebitda'): '-8.8686', ('InstaAstro', 'cash'): '17.7422',
    ('LDC', 'revenue'): '316.4753', ('LDC', 'cash'): '241.9993', ('LDC', 'headcount'): '309',
    ('CPC', 'ebitda'): '15.6492',
    ('CPC', 'revenue'): '111.4026',         # fork-b re-source: PL Summary!F8 'Total Revenue' YTD (₹M);
                                            # corroborated by 'PL schedule' (same 1114.03) → own statement
    ('Aliste', 'revenue'): '5.3052',        # R1a recovery: live 'P&L' row 15 'Revenue From Operation',
                                            # FYTD Apr-25..Feb-26 (11 months, Mar-26=0 placeholder) =
                                            # ₹53,052,306 = 5.3052 Cr; corroborated TO THE RUPEE by the
                                            # sheet's OWN 'YTD' column (P15 = 53,052,306.19)
    ('Aliste', 'ebitda'): '-0.0117',        # R1d recovery: live 'P&L' row 44 'EBITDA', FYTD Apr-25..Feb-26
                                            # = −₹117,058 = −0.0117 Cr (roughly breakeven — +/- months net
                                            # near-zero); corroborated TO THE RUPEE by the sheet's OWN 'YTD'
                                            # column (P44 = −117,057.56). Was FALSELY held by the figure-
                                            # anchor magnitude guard (profit legitimately << valuation scale)
    ('Clientell', 'cash'): '9.3253',        # fork-b re-source: Analysis!B19 'Cash Balance' Feb-26 (₹Lakh),
                                            # corroborated by the ~₹9.2Cr summary cell in the same column
    ('Clientell', 'headcount'): '19',       # Feb'26 period-end (AM80); NOT an average/sum
}

# GENUINELY-UNVERIFIABLE emits only. Empty by design. Shape enforced by the discipline test.
#   (name, concept): {'reason': <why no ground truth exists>, 'review_by': 'YYYY-MM-DD'}
ALLOWLIST: dict = {}

_TOL = Decimal('0.01')


def _anchors():
    return fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND])


def _anchor(anchors, token):
    return next((ca for k, ca in anchors.items() if token in k.lower()), None)


def _bucket_all():
    """Extract every MIS file under production config; return (verified, unverified, held)."""
    anchors = _anchors()
    verified, unverified, held = [], [], []
    for name, fn, token in MIS:
        ca = _anchor(anchors, token)
        rec = extract_company(name, os.path.join(IN, fn), rate_card=default_inr_card('2026-02-28'),
                              entity=name, domicile=(ca.domicile if ca else None),
                              anchor_cr=(ca.anchor_cr if ca else None), use_model=False)
        figs = {v.concept: v for v in rec.fields.values() if isinstance(v, Figure)}
        for c in MIS_CONCEPTS:
            f = figs.get(c)
            val = f.value_cr if f else None
            if val is None:
                held.append((name, c))
            elif (name, c) in GT and abs(val - Decimal(GT[(name, c)])) < _TOL:
                verified.append((name, c))
            else:
                unverified.append((name, c, str(val),
                                   (f.provenance.sheet if f else ''), (f.provenance.cell if f else '')))
    return verified, unverified, held


def test_allowlist_discipline():
    """The valve cannot be used loosely: an entry with no justification, or a lapsed
    review-by, fails the gate. And a slot cannot be BOTH ground-truthed and allowlisted."""
    today = _dt.date.today()
    for key, meta in ALLOWLIST.items():
        assert isinstance(meta, dict) and meta.get('reason'), f'{key}: allowlist entry needs a reason'
        rb = meta.get('review_by')
        assert rb, f'{key}: allowlist entry needs a review_by date (no permanent silencing)'
        assert _dt.date.fromisoformat(rb) >= today, f'{key}: allowlist entry expired ({rb}) — re-audit'
        assert key not in GT, f'{key}: allowlisted AND ground-truthed — pick one'


def test_no_unverified_emit_in_production_coverage():
    """HARD GATE: under the shipping config, every emitted number is verified or (justifiably)
    allowlisted. Any other emit is a number production trusts that nobody has checked."""
    verified, unverified, held = _bucket_all()
    leaked = [u for u in unverified if (u[0], u[1]) not in ALLOWLIST]
    total = len(MIS) * len(MIS_CONCEPTS)
    scoreboard = (f'COVERAGE [prod/INR-only]: verified {len(verified)}/{total} · '
                  f'unverified {len(unverified)}/{total} · held {len(held)}/{total}')
    assert not leaked, (
        f'{scoreboard}\nUN-ALLOWLISTED UNVERIFIED EMITS (value-audit to verified, or the emit '
        f'is wrong and must HOLD — do NOT allowlist a verifiable number):\n' +
        '\n'.join(f'  {n}/{c} = {v} @ {sh}!{cell}' for n, c, v, sh, cell in leaked))
