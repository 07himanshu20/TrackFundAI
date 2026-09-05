"""
U3 — The Layout Template Registry (the model-cost guarantee, G4) — cache A.

Against each LAYOUT fingerprint (identity.layout_fp — structure with values stripped) store the ROW
LOCATIONS the model locator resolved for each statement (concept → row/expression + the label it read),
so a LATER file with the SAME layout fingerprint (next month's file on the same template, or a different
company on the same template) reuses those rows and SKIPS the model locate call entirely — the model cost
then scales with distinct LAYOUTS, not file count.

Two invariants make this safe by construction (caching_safe_design_build_spec §1A + §2):
  • LOCATIONS, NEVER VALUES. The registry remembers only WHERE a concept's row is, never the number. On a
    hit the caller re-reads the actual cells from THIS file and re-runs the three triangulation signals —
    "known where it is" never becomes "trust the number". A stale/wrong/cross-tenant location cannot emit a
    wrong figure: the re-verify catches it and the concept is HELD. (This is why the registry needs no
    org-scoping the golden store needs — it serves a location, and the location is re-proven every hit.)
  • VERSION-KEYED / SELF-ERASING. The store is stamped with net_logic_version(); a logic change invalidates
    the whole store (treated as empty → rebuilt), so the registry can never freeze yesterday's locate logic.

Stores the FULL located set the layout produced — exactly what the model locate call returned (every emit
TARGET *and* every identity intermediate cogs/gross_profit/opex …), whatever each row's triangulation
signals were. This is what makes the registry a TRANSPARENT SUBSTITUTE for the locate call: a hit replays
the same set through the same collapse+triangulate+emit path, so a hit emits the IDENTICAL result a miss
would — including a target that only verifies via an imperfect intermediate. Safety is the re-verify above,
NOT a put-time filter (filtering to only all-PASS rows would drop an imperfect-but-consistent intermediate
and silently hold a target on a hit that a miss emitted — a hit≠miss coverage gap). The stored shape
matches the LIVE model-locate seam (locator.RowRecord: row-index, not an A1 address) — a plain dict
{concept, form, row, operand_rows, row_label}; the caller owns the RowRecord↔dict conversion so this
module stays decoupled from the locator/llm import chain.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

from .identity import net_logic_version
from .statements import Statement

_STORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'media', 'preingest3_templates.json',
)
_lock = threading.Lock()

_ROW_KEYS = ('concept', 'form', 'row', 'operand_rows', 'row_label')
_DIRECT = 'direct'
_EXPRESSION = 'expression'


def _stmt_key(st: Statement) -> str:
    return f'{st.sheet}|{st.start_row}-{st.end_row}'


def _load() -> dict:
    """The store, or a FRESH empty store when it is absent, unreadable, or stamped with a DIFFERENT
    logic version (self-erasing: a locate-logic change discards every cached location and rebuilds)."""
    empty = {'version': net_logic_version(), 'layouts': {}}
    if not os.path.exists(_STORE):
        return empty
    try:
        with open(_STORE) as fh:
            data = json.load(fh)
    except Exception:
        return empty
    if not isinstance(data, dict) or data.get('version') != net_logic_version():
        return empty                                  # version skew → treat as empty (rebuild under new logic)
    data.setdefault('layouts', {})
    return data


def _save(data: dict):
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = f'{_STORE}.{os.getpid()}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, _STORE)                            # atomic on POSIX


def _valid_row(d: dict, st: Statement) -> bool:
    """A stored row is usable only if it still fits THIS statement's row range — a corrupt/stale entry,
    or one whose statement window shifted, is rejected (→ the caller re-locates), never trusted blindly."""
    if not isinstance(d, dict) or d.get('concept') is None:
        return False
    rng = st.rows_range
    form = d.get('form')
    if form == _DIRECT:
        r = d.get('row')
        return isinstance(r, int) and r in rng
    if form == _EXPRESSION:
        ors = d.get('operand_rows') or []
        return bool(ors) and all(isinstance(r, int) and r in rng for r in ors)
    return False


def has_layout(layout_fp: str) -> bool:
    return layout_fp in _load().get('layouts', {})


def get_statement_rows(layout_fp: str, st: Statement) -> Optional[List[dict]]:
    """The stored, re-validated row locations for one statement of a known layout, or None on a miss.
    Each returned dict is {concept, form, row, operand_rows, row_label} — the RowRecord shape the caller
    rebuilds and feeds through the SAME collapse+triangulate+emit path (re-read + re-verify every hit)."""
    tpl = _load().get('layouts', {}).get(layout_fp)
    if not tpl:
        return None
    recs = tpl.get('statements', {}).get(_stmt_key(st))
    if not recs:
        return None
    out = [{k: d.get(k) for k in _ROW_KEYS} for d in recs if _valid_row(d, st)]
    return out or None


def put_statement_rows(layout_fp: str, st: Statement, records: List) -> None:
    """Freeze the PROVEN-CORRECT row locations for one statement against its layout. `records` is a list
    of RowRecord-like objects (duck-typed .concept/.form/.row/.operand_rows/.row_label); the CALLER passes
    ONLY locations whose triangulation signals all PASS (never a held/escalated locate). No-op on empty."""
    rows = [{'concept': r.concept, 'form': r.form, 'row': r.row,
             'operand_rows': list(r.operand_rows or []), 'row_label': r.row_label}
            for r in records if getattr(r, 'concept', None) is not None]
    if not rows:
        return
    with _lock:
        data = _load()                                # re-load under the lock (picks up a concurrent version bump)
        tpl = data['layouts'].setdefault(layout_fp, {'statements': {}})
        tpl['statements'][_stmt_key(st)] = rows
        _save(data)


def stats() -> dict:
    layouts = _load().get('layouts', {})
    return {'layouts': len(layouts),
            'statements': sum(len(t.get('statements', {})) for t in layouts.values())}
