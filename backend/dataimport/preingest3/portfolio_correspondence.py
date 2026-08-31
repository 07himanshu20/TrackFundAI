"""Cross-CIR portfolio-vs-fund soft correspondence (U7, App A flagship).

Compares the BOTTOM-UP Σ of the company MIS files against the fund's OWN top-down aggregate
(a reference-only comparator drawn from a budget-vs-actual sheet — never an emitted actual).

Two axes drive the gap, and their ORDER is deliberate:

  1. COVERAGE (first-order).  A held or absent company contributes 0 to Σ. For a partially
     covered portfolio the dominant driver of "Σ ≪ fund total" is missing companies, NOT
     performance and NOT extraction error. Publishing a bare "−64% variance" would be the
     §09/§11 failure ("not wrong in any one cell, wrong in its conclusion") with coverage as
     the culprit. So the row is a COMPLETENESS METER: Σ covers n/N companies = ₹X = Y% of the
     fund's own reported total; the held/absent breakdown LEADS the explanation.

  2. BASIS (second-order).  The fund figure is stated annualised; the company MIS figures are
     partial-year / mixed month-counts. Summing native partials against an annualised total
     manufactures a basis artifact, so every company flow is brought to a common annualised
     basis (U5) before it enters Σ; a flow that cannot be annualised cleanly is EXCLUDED and
     disclosed, never annualised blindly.

The vs-ACTUAL row is the completeness/reconciliation anchor. The vs-BUDGET row is
performance-vs-plan — but a performance verdict is only meaningful at high coverage, so below
_COVERAGE_PERF_MIN it is published as CONTEXT ONLY, explicitly not a performance verdict.

Fire-when-present, DISCLOSE-when-absent: no fund comparator, or no company figure emitted →
a disclosure row ("not computable"), never a fabricated comparison. Everything published (U7).
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from . import reconcile
from .cir import CIR, Figure
from .quantity import format_pct

# a full-year basis needs no annualisation; a 1-11 month flow is scaled ×12/months.
_FULLYEAR_BASES = {'ttm', 'fy', 'annual', 'annualised', 'annualized'}
# below this fraction of the fund's own reported total, vs-budget is CONTEXT, not a performance verdict.
_COVERAGE_PERF_MIN = Decimal('0.80')

# company MIS concept -> the reference comparator concept filed in cir.comparators
_CONCEPTS = (('revenue', 'portfolio_revenue'), ('ebitda', 'portfolio_ebitda'))


def _dec(x) -> Optional[Decimal]:
    if x is None or x == '':
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _f(x: Optional[Decimal]) -> str:
    if x is None:
        return '—'
    return str(Decimal(str(x)).quantize(Decimal('0.01')))


def _annualise_cr(fig: Figure):
    """(annualised ₹Cr, ok, reason) for a confirmed FLOW figure.
      1-11 months → ×12/months (partial/YTD);  12 months or a full-year basis → as-is;
      otherwise (no month count and not full-year) → CANNOT annualise → (None, False, reason),
    so the caller EXCLUDES + discloses it rather than guess a part-year up to annual (a large error)."""
    if not fig.confirmed or fig.value_cr is None:
        return None, False, 'not confirmed'
    v = Decimal(str(fig.value_cr))
    m = fig.months
    basis = (fig.basis or '').strip().lower()
    if m and 1 <= m <= 11:
        return v * Decimal(12) / Decimal(m), True, f'×12/{m}mo'
    if m == 12 or basis in _FULLYEAR_BASES:
        return v, True, 'already annual'
    return None, False, f'basis={fig.basis or "?"}/months={m or "?"}'


def _coverage(cir: CIR, concept: str) -> dict:
    """Partition the portfolio roster for one concept: which companies contribute an annualised
    figure to Σ, which are held, which cannot be annualised, which have no MIS at all."""
    roster = [r.entity_id for r in cir.records if r.domain == 'portfolio_investments']
    contributing, held, un_annualisable = [], [], []
    seen = set()
    months = []
    for r in (r for r in cir.records if r.domain == 'mis'):
        seen.add(r.entity_id)
        f = r.fields.get(concept)
        if not isinstance(f, Figure):
            continue
        if not f.confirmed:
            held.append((r.entity_id, (f.hold_reason or 'held')[:48]))
            continue
        ann, ok, why = _annualise_cr(f)
        if ok:
            contributing.append((r.entity_id, ann, f.stale))
            if f.months:
                months.append(f.months)
        else:
            un_annualisable.append((r.entity_id, why))
    no_mis = [e for e in roster if e not in seen]
    return {'total': len(roster), 'contributing': contributing, 'held': held,
            'un_annualisable': un_annualisable, 'no_mis': no_mis,
            'months_min': (min(months) if months else None),
            'months_max': (max(months) if months else None),
            'n_stale': sum(1 for *_x, st in contributing if st)}


def _pct(frac: Optional[Decimal]) -> str:
    return format_pct(frac) if frac is not None else 'n/r'


def build(cir: CIR) -> List[dict]:
    """The portfolio-vs-fund soft rows + disclosures. Appended to cir.checks by the caller.
    Never mutates records or figures — reads the CIR, writes only check dicts."""
    checks: List[dict] = []
    comps = {c['concept']: c for c in cir.comparators}
    for concept, comp_key in _CONCEPTS:
        cov = _coverage(cir, concept)
        total = cov['total']
        contributing = cov['contributing']
        n_cov = len(contributing)
        sigma = sum((a for _e, a, _st in contributing), Decimal(0)) if contributing else None
        comp = comps.get(comp_key)
        held_n, nomis_n, unann_n = len(cov['held']), len(cov['no_mis']), len(cov['un_annualisable'])

        if comp is None:                                   # fire-when-present: no fund comparator
            checks.append(reconcile.disclosure(
                f'portfolio_{concept}_vs_fund',
                f'fund states no aggregate portfolio {concept}; bottom-up Σ ({n_cov}/{total} cos) '
                f'has no fund-side correspondent — not computable', cross_checked=False,
                coverage_n=n_cov, coverage_total=total))
            continue

        actual, budget = _dec(comp.get('actual')), _dec(comp.get('budget'))
        if sigma is None or n_cov == 0:                    # nothing emitted → disclose, never fabricate
            checks.append(reconcile.disclosure(
                f'portfolio_{concept}_vs_fund',
                f'no company {concept} emitted ({total} cos: {held_n} held, {nomis_n} no-MIS) — '
                f'correspondence not computable', cross_checked=False,
                coverage_n=0, coverage_total=total))
            continue

        span = (f'{cov["months_min"]}-{cov["months_max"]}mo'
                if cov['months_min'] and cov['months_min'] != cov['months_max']
                else (f'{cov["months_min"]}mo' if cov['months_min'] else 'mixed'))
        cov_frac = (sigma / actual) if (actual and actual != 0) else None

        # ── vs ACTUAL — the completeness/reconciliation anchor, coverage LED ──
        row = reconcile.soft_correspondence(
            f'portfolio_{concept}_vs_fund_actual', reconcile._lit(sigma), actual,
            a_label=f'Σ company {concept}', b_label=f'fund actual {concept}')
        excl = f'{held_n} held,{nomis_n} no-MIS,{unann_n} basis-excl'
        row['detail'] = (f'COVERAGE {n_cov}/{total} cos: Σ={_f(sigma)}Cr(annualised {span}) '
                         f'= {_pct(cov_frac)} of fund actual {_f(actual)}Cr. Gap≈coverage ({excl}), '
                         f'not performance. Fund basis annualised; company side annualised {span}'
                         + (f'; {cov["n_stale"]} stale(>6mo)' if cov['n_stale'] else '') + '.')
        row.update({'coverage_n': n_cov, 'coverage_total': total,
                    'coverage_pct': _pct(cov_frac),
                    'excluded_held': held_n, 'excluded_no_mis': nomis_n,
                    'excluded_basis': unann_n})
        checks.append(row)

        # ── vs BUDGET — performance-vs-plan, GATED on coverage ──
        if budget is not None:
            brow = reconcile.soft_correspondence(
                f'portfolio_{concept}_vs_fund_budget', reconcile._lit(sigma), budget,
                a_label=f'Σ company {concept}', b_label=f'fund budget {concept}')
            perf_ok = cov_frac is not None and cov_frac >= _COVERAGE_PERF_MIN
            if perf_ok:
                brow['detail'] = (f'PERFORMANCE-vs-PLAN (coverage {_pct(cov_frac)} — reliable): '
                                  f'Σ={_f(sigma)}Cr vs budget {_f(budget)}Cr.')
            else:
                brow['detail'] = (f'CONTEXT ONLY — NOT a performance verdict (coverage {_pct(cov_frac)} '
                                  f'< {_pct(_COVERAGE_PERF_MIN)}): Σ={_f(sigma)}Cr vs budget {_f(budget)}Cr; '
                                  f'gap dominated by {n_cov}/{total} coverage, not plan variance.')
            brow['coverage_pct'] = _pct(cov_frac)
            brow['performance_meaningful'] = bool(perf_ok)
            checks.append(brow)

    checks.extend(_realisations_vs_exits(cir))
    return checks


def _realisations_vs_exits(cir: CIR) -> List[dict]:
    """#3 — the exit ledger's Σ GROSS proceeds vs the fund's OWN stated realised GROSS.
    Concept-matched gross-to-gross (net/gain are different concepts, never the correspondent).
    Fire-when-present, disclose-when-absent."""
    gp = [r.fields.get('gross_proceeds') for r in cir.records if r.domain == 'exits']
    gp_conf = [f for f in gp if isinstance(f, Figure) and f.confirmed]
    comp = next((c for c in cir.comparators if c.get('concept') == 'realised_gross'), None)
    n_exits, n_conf = len(gp), len(gp_conf)
    if comp is None or not gp_conf:
        why = ('no fund realised-gross line stated' if comp is None
               else f'no confirmed exit gross proceeds ({n_conf}/{n_exits})')
        return [reconcile.disclosure('realisations_vs_exits',
                f'realisations-vs-exits not computable — {why}', cross_checked=False)]
    sigma = sum((Decimal(str(f.value_cr)) for f in gp_conf), Decimal(0))
    fund_gross = _dec(comp.get('actual'))
    row = reconcile.soft_correspondence('realisations_vs_exits', reconcile._lit(sigma), fund_gross,
                                        a_label='Σ exits gross', b_label='fund realised gross')
    row['detail'] = (f'Σ {n_conf} exit GROSS proceeds = {_f(sigma)}Cr vs fund stated realised GROSS '
                     f'{_f(fund_gross)}Cr @ {comp.get("actual_cell", "")} (gross-to-gross; '
                     f'net/gain are different concepts, excluded).')
    return [row]
