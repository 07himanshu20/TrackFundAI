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


def _finder_concepts(target_concepts: List[str]) -> List[str]:
    """The whole-file finder requests the TARGET concepts ONLY — NOT the over-location anchors.
    request_concepts adds identity anchors (period_total, assets, liabilities, …) that are bounded
    and useful within ONE statement, but across an 80-sheet workbook they match dozens of rows each
    (period_total matches every 'Total …' row) → a combinatorial OUTPUT explosion that blows the
    call past its timeout and floods the located set with wrong-concept noise. Identity anchors are
    located PER-STATEMENT downstream (triangulation), where they belong. Sorted → order-invariant."""
    return sorted({c for c in target_concepts if c})


# ── ONE-PASS WHOLE-FILE finder (model phase, Step 4) ─────────────────────────────────────────────
# The per-statement locate_rows re-discovers each statement one call at a time. The finder asks ONCE
# over the COMPLETE subsheet inventory and requires EVERY location of each concept across ALL listed
# statements — never first-match-stop — so the same concept on the balance sheet AND the cash-flow
# statement both come back and reconcile_locations can cross-check them. Locator-only (rows, never
# values or columns), same RowRecord contract; code reads + triangulates + reconciles every location.
_FILE_ROW_PROMPT = """You LOCATE the ROW of each figure across an ENTIRE multi-statement financial workbook for an Indian AIF pipeline.
Per concept you return EVERY place it appears — as {sheet, row number, row_label} — NEVER a column, NEVER a value. Code picks the reporting-period column, reads the number, and cross-checks your locations against each other.

STRICT RULES (a violation is discarded):
- EXHAUSTIVE: return EVERY location of each concept across ALL listed statements. Do NOT stop at the first match. The same concept legitimately appears on more than one statement (e.g. cash on the balance sheet AND the cash-flow statement) — list them all, each as its own record.
- form "direct": one {sheet,row} that holds the figure. PREFER THE TOTAL line (e.g. "Total revenue") over a component or sub-line.
- form "expression": ONLY when the figure is split across component rows on the SAME sheet that SUM to it. Give "rows"=[row numbers], same sheet, at most 12, ADDITION only, never subtract.
- form "absent": the concept appears in NONE of the listed statements → return exactly ONE record with form "absent" for it. Do not guess.
- row_label: the exact text you read on that row. sheet: the exact sheet name as listed.

Return STRICT JSON only:
{"located":[{"concept":"cash","sheet":"Balance Sheet","form":"direct","row":34,"row_label":"Cash in Bank","rows":[]},
            {"concept":"cash","sheet":"Cashflow (Indirect)","form":"direct","row":88,"row_label":"Closing cash balance","rows":[]}]}

CONCEPTS TO LOCATE (find EVERY location of each across ALL statements; one "absent" record if a concept is in none):
{concepts}

STATEMENTS (each: a sheet header, then its row labels as `row_number = label`):
{statements}
"""


def _file_statements_block(inventory: List[Statement], grid: Dict[str, List[List[Any]]]) -> str:
    """The complete subsheet inventory rendered for the prompt: every statement window, its sheet
    name + row range, then its row labels (row_number = label). This is the 'menu' the model MUST
    cover exhaustively — code supplies it so the model never has to discover which sheets exist."""
    blocks = []
    for st in inventory:
        labels = _row_labels(grid.get(st.sheet) or [], st)
        title = f' — "{st.title}"' if st.title else ''
        head = f'=== SHEET "{st.sheet}" (rows {st.start_row + 1}-{st.end_row + 1}){title} ==='
        blocks.append(head + '\n' + '\n'.join(f'{n} = {t}' for n, t in labels))
    return '\n\n'.join(blocks)


def _parse_located_file(located: Any, valid_by_sheet: Dict[str, set]) -> tuple:
    """Parse the finder's 'located' array into (records, returned, absent).
    records = [(sheet, RowRecord)]; a concept may appear MANY times (one per location). `returned`
    = every requested concept the model gave a determinate answer for (≥1 valid located row, or an
    explicit "absent"); a concept omitted, or given only out-of-range/unknown-sheet rows, is NOT in
    `returned` so the caller detects the silent drop and re-requests it (completeness/recall net)."""
    records, returned, absent = [], set(), set()
    if not isinstance(located, list):
        return records, returned, absent
    for obj in located:
        if not isinstance(obj, dict):
            continue
        concept = str(obj.get('concept') or '').strip().lower()
        form = str(obj.get('form') or '').strip().lower()
        sheet = str(obj.get('sheet') or '').strip()
        label = str(obj.get('row_label') or '').strip()
        if not concept:
            continue
        if form == ls.FORM_ABSENT:
            returned.add(concept)
            absent.add(concept)
            continue
        valid = valid_by_sheet.get(sheet)
        if valid is None:                                  # unknown sheet name → discard (never guess a sheet)
            continue
        if form == ls.FORM_EXPRESSION:
            ors = [int(x) - 1 for x in (obj.get('rows') or []) if _is_int(x)]
            ors = [r for r in ors if r in valid][:ls.MAX_OPERANDS]
            if not ors:
                continue
            returned.add(concept)
            records.append((sheet, RowRecord(concept, ls.FORM_EXPRESSION, None, ors, label)))
        else:
            r = obj.get('row')
            if not _is_int(r):
                continue
            r0 = int(r) - 1
            if r0 not in valid:
                continue
            returned.add(concept)
            records.append((sheet, RowRecord(concept, ls.FORM_DIRECT, r0, [], label)))
    return records, returned, absent


def _locate_across_file_call(inventory: List[Statement], grid: Dict[str, List[List[Any]]],
                             concepts: List[str], *, content_fp: str, mode: str) -> dict:
    """One whole-file model round for an EXACT concept list (no anchor re-expansion on retry).
    Returns {'records':[(sheet,RowRecord)], 'returned':set, 'absent':set, 'error'?}."""
    valid_by_sheet: Dict[str, set] = {}          # UNION per sheet: a row-range split can place two
    for st in inventory:                          # windows of the SAME sheet in one chunk; a plain
        valid_by_sheet.setdefault(st.sheet, set()).update(st.rows_range)  # dict would drop the first.
    prompt = (_FILE_ROW_PROMPT
              .replace('{concepts}', _concepts_block(concepts))
              .replace('{statements}', _file_statements_block(inventory, grid)))
    sheets_sig = ','.join(f'{st.sheet}:{st.start_row}-{st.end_row}' for st in inventory)
    sig = f'{content_fp}|FILE|{sheets_sig}|{mode}|{",".join(concepts)}'
    res = llm.call_json('locate_file', sig, prompt, read_timeout_s=180.0, stream=False)
    if res.is_error:
        return {'error': res.reason, 'records': [], 'returned': set(), 'absent': set()}
    located = (res.data or {}).get('located') if isinstance(res.data, dict) else None
    records, returned, absent = _parse_located_file(located, valid_by_sheet)
    return {'records': records, 'returned': returned, 'absent': absent}


def locate_across_file(inventory: List[Statement], grid: Dict[str, List[List[Any]]],
                       target_concepts: List[str], *, content_fp: str) -> dict:
    """ONE-PASS finder: ask the model for EVERY location of each TARGET concept (targets only — the
    over-location anchors are located PER-STATEMENT downstream, not here; see _finder_concepts) across
    the COMPLETE statement inventory in a single call. ORDER-INVARIANT (_finder_concepts canonicalises)
    and COMPLETENESS-COMPLETE: a concept the model silently drops is re-requested ONCE
    in a focused call; any still-undetermined concept is returned in 'missing' — never silently lost.
    Every row is validated to lie inside its named statement. Returns
    {'records':[(sheet,RowRecord)], 'absent':[concept], 'missing':[concept], 'error'?}."""
    if not inventory:
        return {'records': [], 'absent': [], 'missing': list(_finder_concepts(target_concepts))}
    concepts = _finder_concepts(target_concepts)
    first = _locate_across_file_call(inventory, grid, concepts, content_fp=content_fp, mode='file')
    if first.get('error'):
        return {'error': first['error'], 'records': [], 'absent': [], 'missing': list(concepts)}
    records = list(first['records'])
    returned = set(first['returned'])
    absent = set(first['absent'])
    missing = [c for c in concepts if c not in returned]
    if missing:                                            # completeness net — re-request the silent drops
        retry = _locate_across_file_call(inventory, grid, missing, content_fp=content_fp, mode='file-retry')
        if not retry.get('error'):
            have = {(s, r.concept, r.row, tuple(r.operand_rows)) for s, r in records}
            for s, r in retry['records']:
                key = (s, r.concept, r.row, tuple(r.operand_rows))
                if key not in have:
                    records.append((s, r))
                    have.add(key)
            returned |= retry['returned']
            absent |= retry['absent']
    still_missing = [c for c in concepts if c not in returned]
    return {'records': records, 'absent': sorted(absent), 'missing': still_missing}


# ── TOKEN-BUDGETED CHUNKING (Step 4, §3b) — split, never filter-to-fit ────────────────────────────
# A large workbook can't go in ONE request (it times out), and it must NOT be filtered down to fit
# (that silently drops real statements — the never-miss violation). The resolution is to SPLIT the
# inventory into token-budgeted chunks of WHOLE sheets, locate each, and UNION the answers: every sheet
# is seen, just never all at once. A concept split across chunks unions exactly as one split across
# sheets, and reconcile_locations judges the union. Chunk boundaries are deterministic (workbook order,
# packed by budget), so the same file always chunks the same way.
_CHUNK_TOKEN_BUDGET = 14000   # est. tokens of the statements block per chunk. The 8-sheet CSS chunk
# (~17k total prompt tokens) answered in 40s, well inside the 120s timeout; a smaller budget keeps every
# chunk comfortably within it. Tunable: larger = fewer chunks (faster) at more timeout risk.


def _estimate_stmt_tokens(st: Statement, grid: Dict[str, List[List[Any]]]) -> int:
    """Rough token size of ONE statement's labels block (chars/4). Deterministic, no model."""
    return len(_file_statements_block([st], grid)) // 4


def _split_statement_by_budget(st: Statement, grid: Dict[str, List[List[Any]]],
                               token_budget: int) -> List[Statement]:
    """Split ONE statement whose labels alone exceed the budget into contiguous row-range windows,
    each ≤ budget. Every window keeps the SAME sheet name, header_row, label_col and title, so the
    1-based row numbers (and therefore row validity) are unchanged — a window carries exactly the
    labels it spans. The finder is locate-by-row-label only, so a concept is found in whichever
    window holds its row and the union recovers the whole sheet. Splits at label-row boundaries
    (never mid-row); a single row that alone exceeds the budget becomes its own window (unavoidable —
    a row can't be halved). Deterministic (workbook row order). Universal: keyed on token size, never
    on a sheet name or row count."""
    if _estimate_stmt_tokens(st, grid) <= token_budget:
        return [st]
    rows = grid.get(st.sheet) or []
    labels = _row_labels(rows, st)                       # [(1-based row, label)] — the rendered lines
    title = f' — "{st.title}"' if st.title else ''
    head_tok = len(f'=== SHEET "{st.sheet}" (rows {st.start_row + 1}-{st.end_row + 1}){title} ===') // 4 + 1
    boundaries: List[int] = []                           # 0-based rows where a NEW window begins
    cur_tok = head_tok
    for n1, label in labels:
        line_tok = len(f'{n1} = {label}') // 4 + 1
        if cur_tok + line_tok > token_budget and cur_tok > head_tok:
            boundaries.append(n1 - 1)                    # this label overflows → start a window at it
            cur_tok = head_tok
        cur_tok += line_tok
    starts = [st.start_row] + boundaries
    ends = [b - 1 for b in boundaries] + [st.end_row]
    return [Statement(st.sheet, s, e, st.header_row, st.label_col, st.title, st.kind)
            for s, e in zip(starts, ends)]


def _chunk_by_budget(inventory: List[Statement], grid: Dict[str, List[List[Any]]],
                     token_budget: int) -> List[List[Statement]]:
    """Pack statements (workbook order → deterministic) into chunks until the next would exceed the
    budget. A single sheet bigger than the budget is first split into row-range windows
    (_split_statement_by_budget) so no chunk can exceed the budget — every window is ≤ budget and the
    union of windows covers the whole sheet, so nothing is filtered to fit."""
    expanded: List[Statement] = []
    for st in inventory:
        expanded.extend(_split_statement_by_budget(st, grid, token_budget))
    chunks: List[List[Statement]] = []
    cur: List[Statement] = []
    cur_t = 0
    for st in expanded:
        t = _estimate_stmt_tokens(st, grid)
        if cur and cur_t + t > token_budget:
            chunks.append(cur)
            cur, cur_t = [], 0
        cur.append(st)
        cur_t += t
    if cur:
        chunks.append(cur)
    return chunks


def locate_across_file_chunked(inventory: List[Statement], grid: Dict[str, List[List[Any]]],
                               target_concepts: List[str], *, content_fp: str,
                               token_budget: int = _CHUNK_TOKEN_BUDGET) -> dict:
    """Chunked one-pass finder: split the inventory into token-budgeted chunks of whole sheets, locate
    each chunk SEQUENTIALLY (no aggressive parallelism), and UNION the located rows. Every sheet is
    seen — nothing is filtered to fit. Per-chunk completeness/recall is unchanged (locate_across_file).
    Cross-chunk status is fail-closed on absence:
      • located  — found in ≥1 chunk (union, de-duplicated).
      • absent   — located in NO chunk AND the model said 'absent' in every chunk that ran.
      • missing  — located in no chunk AND undetermined in ≥1 chunk (a chunk errored, or a silent drop
                   the per-chunk recall could not recover) → NEVER downgraded to a silent 'absent'.
    Returns {'records':[(sheet,RowRecord)], 'absent':[...], 'missing':[...], 'chunks':N, 'chunk_error':bool}."""
    concepts = _finder_concepts(target_concepts)
    if not inventory:
        return {'records': [], 'absent': [], 'missing': list(concepts), 'chunks': 0, 'chunk_error': False}
    chunks = _chunk_by_budget(inventory, grid, token_budget)
    records, have, located, missing_any = [], set(), set(), set()
    chunk_error = False
    for ci, chunk in enumerate(chunks):
        out = locate_across_file(chunk, grid, target_concepts,
                                 content_fp=f'{content_fp}|chunk{ci + 1}of{len(chunks)}')
        if out.get('error'):
            chunk_error = True
            missing_any |= set(concepts)              # a failed chunk can't prove absence for its scope
            continue
        for s, r in out['records']:
            key = (s, r.concept, r.row, tuple(r.operand_rows))
            if key not in have:
                have.add(key)
                records.append((s, r))
                located.add(r.concept)
        missing_any |= set(out.get('missing', []))
    absent, missing = [], []
    for c in concepts:
        if c in located:
            continue
        (missing if c in missing_any else absent).append(c)
    return {'records': records, 'absent': sorted(absent), 'missing': sorted(missing),
            'chunks': len(chunks), 'chunk_error': chunk_error}


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
