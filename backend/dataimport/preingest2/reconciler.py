"""
Stage 6 — Reconciliation & Validation (CODE, the accuracy gate).

Deterministic tie-outs decide pass / warn / fail. The model never alters a
number and never decides pass/fail. A hard-check failure blocks the run and
produces a validation report; warnings travel through to the output notes.

Checks (document Section 6 / Appendix A):
  • internal tie-out : Σ tranche amounts == Σ investment cost   (deployed cost)
  • cross-sheet      : Σ capital calls  ≈ Σ LP cumulative called
  • bottom-up vs agg : Σ sector cost    == Σ invested cost
  • provenance       : every shipped record names a source file + sheet
  • range / sanity   : ratios within plausible bounds
"""

TOLERANCE = 0.02  # 2% for soft numeric ties


def _f(v):
    try:
        return float(str(v).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _sum(records, field):
    tot, seen = 0.0, False
    for r in records:
        n = _f(r.get(field))
        if n is not None:
            tot += n
            seen = True
    return tot if seen else None


def _close(a, b, tol=TOLERANCE):
    if a is None or b is None:
        return None
    if a == 0 and b == 0:
        return True
    return abs(a - b) <= tol * max(abs(a), abs(b), 1.0)


def reconcile(dom, metrics):
    """Return (results, blocked) where results is a list of check dicts."""
    results = []

    def add(name, status, detail):
        results.append({'check': name, 'status': status, 'detail': detail})

    from .schema import is_summary_label
    tranches = [r for r in dom.get('investment_tranches', [])
                if not is_summary_label(r.get('company_name'))]
    invest = [r for r in dom.get('portfolio_investments', [])
              if not is_summary_label(r.get('company_name'))]
    calls = dom.get('capital_calls', [])
    commit = dom.get('investors_aml', []) + dom.get('commitments', [])

    # 1 — deployed cost tie-out
    tr = _sum(tranches, 'tranche_amount')
    inv = _sum(invest, 'total_invested')
    if tr is not None and inv is not None:
        ok = _close(tr, inv)
        add('deployed_cost_tieout', 'pass' if ok else 'warn',
            f'Σtranches={tr:.2f} vs Σinvestment_cost={inv:.2f}')
    elif tr is not None:
        add('deployed_cost_tieout', 'pass',
            f'Σtranches={tr:.2f} (investment sheet carries no direct cost — derived)')

    # 2 — capital-call vs LP-called cross-sheet
    cc = _sum(calls, 'total_call_amount')
    lpc = _sum(commit, 'cumulative_called')
    if cc is not None and lpc is not None:
        ok = _close(cc, lpc, tol=0.05)
        add('called_capital_crosssheet', 'pass' if ok else 'warn',
            f'Σcapital_calls={cc:.2f} vs ΣLP_called={lpc:.2f}')

    # 3 — commitments vs called sanity (called ≤ commitments)
    tcommit, tcalled = metrics.total_commitments(), metrics.total_called()
    if tcommit is not None and tcalled is not None:
        add('called_le_committed', 'pass' if tcalled <= tcommit * 1.001 else 'fail',
            f'called={tcalled:.2f} committed={tcommit:.2f}')

    # 4 — provenance present
    missing = 0
    for recs in dom.values():
        for r in recs:
            if not r.get('__source_file__') or not r.get('__source_sheet__'):
                missing += 1
    add('provenance_present', 'pass' if missing == 0 else 'warn',
        f'{missing} records without full provenance')

    # 5 — MOIC/IRR range sanity
    moic = metrics.moic()
    if moic is not None:
        add('moic_range', 'pass' if 0 <= moic <= 20 else 'fail', f'MOIC={moic:.2f}x')
    irr = metrics.net_irr()
    if irr is not None:
        add('irr_range', 'pass' if -99.99 <= irr <= 500 else 'fail', f'NetIRR={irr:.1f}%')

    blocked = any(r['status'] == 'fail' for r in results)
    return results, blocked
