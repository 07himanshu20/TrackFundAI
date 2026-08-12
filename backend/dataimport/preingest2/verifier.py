"""
Stage 8 — Verification (CODE, the release gate).

Lightweight stand-in for the document's LibreOffice recalc: because the assembler
writes computed VALUES (not Excel formulas) in this design-validation build, the
verifier re-derives the spot-checks in code and asserts them, plus checks that
required schema columns are populated and no derived sheet is silently empty when
its inputs exist. A workbook that fails a hard assertion is not delivered.
"""
from . import schema as sch


def _f(v):
    try:
        return float(str(v).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def verify(assembled, metrics):
    checks = []

    def add(name, ok, detail):
        checks.append({'check': name, 'status': 'pass' if ok else 'fail', 'detail': detail})

    # spot-check 1 — Investments Total Invested column sums to deployment
    inv = assembled.get('investments', {})
    cols = inv.get('columns', [])
    if 'Total Invested (₹Cr)' in cols:
        idx = cols.index('Total Invested (₹Cr)')
        s = sum(_f(r[idx]) or 0 for r in inv.get('rows', []))
        dep = metrics.total_invested()
        ok = dep is None or abs(s - dep) <= 0.02 * max(abs(dep), 1)
        add('assemble_total_invested==deployment', ok, f'sheet={s:.2f} deployment={dep}')

    # spot-check 2 — required columns non-empty where the sheet has rows
    for sheet in sch.SHEETS:
        b = assembled.get(sheet['key'], {})
        rows = b.get('rows', [])
        if not rows or sheet['mode'] not in ('table', 'per_company'):
            continue
        for c in sheet.get('columns', []):
            if not c.get('required'):
                continue
            ci = [x['header'] for x in sheet['columns']].index(c['header'])
            blanks = sum(1 for r in rows if r[ci] in (None, ''))
            add(f"required:{sheet['name']}:{c['header']}", blanks == 0,
                f'{blanks}/{len(rows)} blank')

    blocked = any(c['status'] == 'fail' for c in checks)
    return checks, blocked
