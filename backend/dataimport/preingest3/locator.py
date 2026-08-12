"""
S4 — Location (model, cache-gated). The single highest-value change in the
revision: the model returns ADDRESSES, code returns VALUES.

For ONE statement (the semantic unit) it is given a bounded, addressed window and
a concept list (the output's targets PLUS the over-location anchors), and returns
a locator record per concept — direct address, +-only expression, or absent —
plus the labels it read and the declared unit/currency/period. Every record is
then validated by locator_schema (the teeth). Nothing here reads a value; that
is S5's job. Nothing here computes; the model never does arithmetic.

Skipped entirely on a Layout Template hit (handled by the caller): a known shape
reuses stored coordinates and spends zero tokens.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import llm
from . import locator_schema as ls
from .contract import CONCEPT_LEXICON, OVER_LOCATION_ANCHORS
from .profiler import _addr, _cell_type
from .statements import Statement

_PROMPT = """You LOCATE figures in one financial statement for an Indian AIF pipeline.
You are given a bounded, addressed window of ONE statement — never the whole file.

STRICT RULES (a violation is discarded):
- You return the LOCATION of each figure, never its value. There is no field for a number.
- form "direct": a single cell address that holds the figure. PREFER THIS whenever a single total cell exists.
- form "expression": ONLY when no single cell holds the figure and it is split across cells that SUM to it
  (e.g. revenue split by segment). Give "operands" as a list of CELL ADDRESSES to add. Rules:
    · operands are cell addresses ONLY — never numbers/literals,
    · all operands in the SAME COLUMN and this SAME statement,
    · at most 12 operands. Only addition (+). Never subtract.
- form "absent": the figure is genuinely not in this statement. Say so; do not guess.
- Also report row_label (text to the LEFT you read), col_label (text ABOVE),
  declared_unit and declared_ccy exactly as printed on the sheet (or null),
  and period_hint {basis: YTD|MTD|TTM|FY|point_in_time, months: N}.

Return STRICT JSON only:
{"sheet":"...","located":[
  {"concept":"revenue","form":"direct","address":"Summary!K14","operands":[],
   "row_label":"Total Revenue","col_label":"Jun-25","declared_unit":"thousands",
   "declared_ccy":"MYR","period_hint":{"basis":"YTD","months":6}}
]}

STATEMENT: sheet {sheet}, rows {r0}-{r1}{title}
CONCEPTS TO LOCATE (locate every one; use "absent" if not present):
{concepts}

HEADER CELLS (address = text):
{headers}
ROW LABELS (address = text):
{labels}
SAMPLE DATA CELLS (address, value, type) — for context only, never copy values:
{sample}
"""


def _statement_window(rows: List[List[Any]], st: Statement) -> Dict[str, list]:
    """Bounded, addressed window scoped to ONE statement."""
    headers, labels, sample = [], [], []
    if st.header_row is not None and st.header_row < len(rows):
        for c, v in enumerate(rows[st.header_row]):
            if _cell_type(v) == 'text':
                headers.append({'addr': _addr(c, st.header_row), 'text': str(v).strip()})
    start = (st.header_row + 1) if st.header_row is not None else st.start_row
    for r in range(start, st.end_row + 1):
        if r >= len(rows):
            break
        if st.label_col is not None and st.label_col < len(rows[r]):
            v = rows[r][st.label_col]
            if _cell_type(v) == 'text':
                labels.append({'addr': _addr(st.label_col, r), 'text': str(v).strip()})
        for c, v in enumerate(rows[r]):
            if c == st.label_col:
                continue
            t = _cell_type(v)
            if t in ('num', 'date') and len(sample) < 60:
                sample.append({'addr': _addr(c, r),
                               'value': v.isoformat() if t == 'date' else v, 'type': t})
    return {'headers': headers[:40], 'labels': labels[:150], 'sample': sample}


def _concepts_block(concepts: List[str]) -> str:
    lines = []
    for c in concepts:
        syn = CONCEPT_LEXICON.get(c, [])
        hint = f" (e.g. {', '.join(syn[:4])})" if syn else ''
        lines.append(f'- {c}{hint}')
    return '\n'.join(lines)


def request_concepts(target_concepts: List[str]) -> List[str]:
    """Targets + over-location anchors, de-duplicated, ORDER-INVARIANT. Targets are
    SORTED so the same set of targets yields the same prompt and the same cache key
    no matter what order the caller supplies them — recall must never depend on
    request order (the cash-order-sensitivity bug). Anchors follow in their fixed
    identity-bundle order; they are the identity-bearing lines whose only purpose is
    free verification."""
    targets = sorted({c for c in target_concepts if c})
    seen, out = set(targets), list(targets)
    for c in OVER_LOCATION_ANCHORS:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ── ROW-only locator (S4, refined) — the model returns a ROW, never a cell ────
# Contract (2026-07-23): per concept the model returns a ROW (line-item), the LABEL
# it read, and the form — and NOTHING mechanical. Code owns the period COLUMN
# (detect_period_axis + collapse), scenario preference, scale and the value read.
# This removes the wrong-PERIOD error class (the model can't pick a column, so it
# can't pick the wrong one) and hardens triangulation: the label signal now checks
# a stable ROW label, and the identity chain is evaluated across rows in ONE
# code-chosen column instead of three model-picked ones. The prompt carries ONLY
# the label column (no 47 data columns) — smaller, faster, less noise.
_ROW_PROMPT = """You LOCATE the ROW of each figure in ONE financial statement for an Indian AIF pipeline.
You return a ROW NUMBER and the label text you read — NEVER a column, NEVER a value. Code picks the reporting-period column and reads the number.

STRICT RULES (a violation is discarded):
- form "direct": ONE row number that holds the figure. PREFER THE TOTAL line (e.g. "Total revenue") over a component or sub-line.
- form "expression": ONLY when the figure is split across component rows that SUM to it (e.g. revenue by segment). Give "rows" = a list of ROW NUMBERS to add. Same statement, at most 12, ADDITION only, never subtract.
- form "absent": the figure's line is genuinely not in this statement. Say so; do not guess.
- row_label: the exact text you read on that row.

Return STRICT JSON only:
{"located":[{"concept":"revenue","form":"direct","row":20,"row_label":"Total revenue","rows":[]}]}

STATEMENT: sheet {sheet}, rows {r0}-{r1}{title}
CONCEPTS TO LOCATE (locate every one; use "absent" if not present):
{concepts}

ROW LABELS (row_number = label):
{labels}
"""


class RowRecord:
    """A model-located ROW (0-based). Carries no column and no value — code resolves
    those. `form` is direct (one row) or expression (sum of component rows)."""
    __slots__ = ('concept', 'form', 'row', 'operand_rows', 'row_label')

    def __init__(self, concept, form, row, operand_rows, row_label):
        self.concept = concept
        self.form = form
        self.row = row                       # 0-based, for direct
        self.operand_rows = operand_rows     # 0-based list, for expression
        self.row_label = row_label


def _is_int(x) -> bool:
    return isinstance(x, int) or (isinstance(x, str) and x.strip().lstrip('-').isdigit())


def _row_labels(rows: List[List[Any]], st: Statement) -> List[tuple]:
    out = []
    lc = st.label_col
    if lc is None:
        return out
    for r in st.rows_range:
        if r < len(rows) and lc < len(rows[r]) and _cell_type(rows[r][lc]) == 'text':
            out.append((r + 1, str(rows[r][lc]).strip()))
    return out


def _parse_located_rows(located: Any, valid_rows: set) -> tuple:
    """Parse the model's 'located' array into (records, returned, absent).
    `returned` = every requested concept the model gave a DETERMINATE answer for
    (a valid direct/expression row, or an explicit "absent"). A concept the model
    omits — or answers with an out-of-range row — is NOT in `returned`, so the
    caller can detect the silent drop and re-request it (recall net)."""
    records, returned, absent = [], set(), set()
    if not isinstance(located, list):
        return records, returned, absent
    for obj in located:
        if not isinstance(obj, dict):
            continue
        concept = str(obj.get('concept') or '').strip().lower()
        form = str(obj.get('form') or '').strip().lower()
        label = str(obj.get('row_label') or '').strip()
        if not concept:
            continue
        if form == ls.FORM_ABSENT:
            returned.add(concept)
            absent.add(concept)
            continue
        if form == ls.FORM_EXPRESSION:
            ors = [int(x) - 1 for x in (obj.get('rows') or []) if _is_int(x)]
            ors = [r for r in ors if r in valid_rows][:ls.MAX_OPERANDS]
            if not ors:
                continue
            returned.add(concept)
            records.append(RowRecord(concept, ls.FORM_EXPRESSION, None, ors, label))
        else:
            r = obj.get('row')
            if not _is_int(r):
                continue
            r0 = int(r) - 1
            if r0 not in valid_rows:
                continue
            returned.add(concept)
            records.append(RowRecord(concept, ls.FORM_DIRECT, r0, [], label))
    return records, returned, absent


def _locate_rows_call(st: Statement, grid: Dict[str, List[List[Any]]], concepts: List[str],
                      *, content_fp: str, mode: str) -> dict:
    """One model round for an EXACT concept list (NO anchor expansion — the focused
    retry must ask for only the dropped concepts, not re-expand the anchor set).
    Returns {'records', 'returned', 'absent', 'error'?}."""
    rows = grid.get(st.sheet) or []
    labels = _row_labels(rows, st)
    prompt = (_ROW_PROMPT
              .replace('{sheet}', st.sheet)
              .replace('{r0}', str(st.start_row + 1)).replace('{r1}', str(st.end_row + 1))
              .replace('{title}', f' — "{st.title}"' if st.title else '')
              .replace('{concepts}', _concepts_block(concepts))
              .replace('{labels}', '\n'.join(f'{n} = {t}' for n, t in labels)))
    sig = f'{content_fp}|{st.sheet}|{st.start_row}-{st.end_row}|{mode}|{",".join(concepts)}'
    res = llm.call_json('locate_rows', sig, prompt, read_timeout_s=90.0, stream=False)
    if res.is_error:
        return {'error': res.reason, 'records': [], 'returned': set(), 'absent': set()}
    located = (res.data or {}).get('located') if isinstance(res.data, dict) else None
    records, returned, absent = _parse_located_rows(located, set(st.rows_range))
    return {'records': records, 'returned': returned, 'absent': absent}


def locate_rows(st: Statement, grid: Dict[str, List[List[Any]]], target_concepts: List[str],
                *, content_fp: str) -> dict:
    """Ask the model for the ROW of each concept (+ over-location anchors) in ONE
    statement. ORDER-INVARIANT (concepts are canonicalised by request_concepts) and
    RECALL-COMPLETE: a concept the model silently drops is re-requested ONCE in a
    focused call, and any concept still undetermined is returned in 'missing' — never
    silently lost. Every row is validated to lie inside the statement. Returns
    {'records':[RowRecord], 'absent':[concept], 'missing':[concept], 'error'?}."""
    concepts = request_concepts(target_concepts)
    first = _locate_rows_call(st, grid, concepts, content_fp=content_fp, mode='rows')
    if first.get('error'):
        return {'error': first['error'], 'records': [], 'absent': [], 'missing': list(concepts)}
    records = list(first['records'])
    returned = set(first['returned'])
    absent = set(first['absent'])
    missing = [c for c in concepts if c not in returned]
    if missing:                                   # recall net — re-request the silent drops, focused
        retry = _locate_rows_call(st, grid, missing, content_fp=content_fp, mode='rows-retry')
        if not retry.get('error'):
            have = {r.concept for r in records}
            for r in retry['records']:
                if r.concept not in have:
                    records.append(r)
                    have.add(r.concept)
            returned |= retry['returned']
            absent |= retry['absent']
    still_missing = [c for c in concepts if c not in returned]
    return {'records': records, 'absent': sorted(absent), 'missing': still_missing}


def locate_statement(st: Statement, grid: Dict[str, List[List[Any]]], target_concepts: List[str],
                     *, content_fp: str) -> dict:
    """Locate all requested concepts (+ anchors) in one statement. `grid` is the
    whole-workbook {sheet: rows} map — the SAME shape reader.read_figure takes,
    so a caller can never pass one where the other is expected. Returns
    {'records':[valid LocatorRecord], 'escalations':[{concept,reason}],
     'absent':[concept]}. Model call is per-statement and checkpointed."""
    concepts = request_concepts(target_concepts)
    rows = grid.get(st.sheet) or []
    win = _statement_window(rows, st)
    prompt = (_PROMPT
              .replace('{sheet}', st.sheet)
              .replace('{r0}', str(st.start_row + 1)).replace('{r1}', str(st.end_row + 1))
              .replace('{title}', f' — "{st.title}"' if st.title else '')
              .replace('{concepts}', _concepts_block(concepts))
              .replace('{headers}', '\n'.join(f"{h['addr']} = {h['text']}" for h in win['headers']))
              .replace('{labels}', '\n'.join(f"{l['addr']} = {l['text']}" for l in win['labels']))
              .replace('{sample}', '\n'.join(f"{s['addr']} = {s['value']} [{s['type']}]"
                                             for s in win['sample'])))

    sig = f'{content_fp}|{st.sheet}|{st.start_row}-{st.end_row}|{",".join(concepts)}'
    res = llm.call_json('locate', sig, prompt, read_timeout_s=120.0)

    if res.is_error:
        # transport failure — HOLD, do not report concepts as absent/empty
        return {'error': res.reason, 'records': [], 'escalations': [], 'absent': []}
    data = res.data
    records, escalations, absent = [], [], []
    located = (data or {}).get('located') if isinstance(data, dict) else None
    if not isinstance(located, list):
        return {'records': [], 'escalations': [{'concept': c, 'reason': 'no locator output'}
                                               for c in target_concepts], 'absent': []}
    st_rows = st.rows_range
    for obj in located:
        if not isinstance(obj, dict):
            continue
        rec = ls.from_model_obj(obj)
        ls.validate_record(rec, statement_rows=st_rows, statement_sheet=st.sheet)
        if not rec.valid:
            escalations.append({'concept': rec.concept, 'reason': rec.reject_reason})
        elif rec.form == ls.FORM_ABSENT:
            absent.append(rec.concept)
        else:
            records.append(rec)
    return {'records': records, 'escalations': escalations, 'absent': absent}
