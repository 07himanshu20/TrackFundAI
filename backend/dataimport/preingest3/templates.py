"""
U3 — The Layout Template Registry (the cost guarantee, G4).

Against each LAYOUT fingerprint (identity.layout_fp — structure with values
stripped), store the resolved coordinates per concept per statement, plus the
declared units/currency/period and the verified labels. On a later file with the
SAME layout fingerprint (next month's same template, or a different company on
the same template), code reads the stored coordinates directly and re-runs the
three signals to confirm nothing shifted — ZERO model calls. If the client
alters their template the fingerprint changes and the file is correctly treated
as new, so the registry cannot silently go stale.

Stored once a statement's triangulation PASSES — caching correct data is the
whole point; a template is never written from an escalated/held extraction.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

from .locator_schema import LocatorRecord, validate_record
from .statements import Statement

_STORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'media', 'preingest3_templates.json',
)
_lock = threading.Lock()


def _stmt_key(st: Statement) -> str:
    return f'{st.sheet}|{st.start_row}-{st.end_row}'


def _rec_to_dict(rec: LocatorRecord) -> dict:
    return {'concept': rec.concept, 'form': rec.form, 'address': rec.address,
            'operands': rec.operands, 'row_label': rec.row_label,
            'col_label': rec.col_label, 'declared_unit': rec.declared_unit,
            'declared_ccy': rec.declared_ccy, 'period_basis': rec.period_basis,
            'period_months': rec.period_months}


def _rec_from_dict(d: dict) -> LocatorRecord:
    return LocatorRecord(
        concept=d.get('concept', ''), form=d.get('form', ''),
        address=d.get('address'), operands=list(d.get('operands') or []),
        row_label=d.get('row_label', ''), col_label=d.get('col_label', ''),
        declared_unit=d.get('declared_unit'), declared_ccy=d.get('declared_ccy'),
        period_basis=d.get('period_basis'), period_months=d.get('period_months'))


def _load() -> dict:
    if os.path.exists(_STORE):
        try:
            with open(_STORE) as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = _STORE + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, _STORE)


def get_statement_records(layout_fp: str, st: Statement) -> Optional[List[LocatorRecord]]:
    """Stored, re-validated locator records for one statement of a known layout,
    or None on a miss. Re-validation guards against a corrupt/stale store."""
    tpl = _load().get(layout_fp)
    if not tpl:
        return None
    recs_d = tpl.get('statements', {}).get(_stmt_key(st))
    if not recs_d:
        return None
    out = []
    for d in recs_d:
        rec = _rec_from_dict(d)
        validate_record(rec, statement_rows=st.rows_range, statement_sheet=st.sheet)
        if rec.valid:
            out.append(rec)
    return out or None


def has_layout(layout_fp: str) -> bool:
    return layout_fp in _load()


def put_statement_records(layout_fp: str, st: Statement, records: List[LocatorRecord]):
    """Freeze the resolved coordinates for a PASSED statement against its layout."""
    with _lock:
        data = _load()
        tpl = data.setdefault(layout_fp, {'statements': {}})
        tpl['statements'][_stmt_key(st)] = [_rec_to_dict(r) for r in records]
        _save(data)


def stats() -> dict:
    data = _load()
    return {'layouts': len(data),
            'statements': sum(len(t.get('statements', {})) for t in data.values())}
