"""
Token estimator + chunk planner — decides whether Flavor B activates inside
a given layer and produces row-range chunk plans.

Universal mechanics (works for any AIF Excel format / size):

  • Per-sheet cell count drives output-token estimate.
  • Threshold: if a layer's TOTAL est_output > _CHUNK_THRESHOLD → chunk.
  • Chunking strategy:
      1. Bin-pack small sheets together so a chunk's est_output ≤ _CHUNK_TARGET.
      2. Any single sheet whose est_output exceeds _CHUNK_TARGET is split
         into N row-range sub-chunks (each chunk owns rows [start, end) of
         that one sheet).
  • Row-range filtering happens IN PYTHON (orchestrator._serialize_sheets);
    Gemini never has to count or modulo. This eliminates duplicate or
    missed rows from chunk-boundary errors.

Layer 1 is allowed to chunk too (was previously blocked — a workbook
with a single huge fund_pl_bs sheet routed to L1 would have truncated).

Tuned 2026-06-24 (Mock_14 incident): tokens_per_cell=8.0, threshold=35K,
target=30K. Tuning constants exposed via env so ops can adjust without
redeploy. After enough imports we should switch to per-layer calibration
(F2) — for now, conservative globals work.
"""

import math
import os
import logging

import openpyxl

logger = logging.getLogger(__name__)


_TOKENS_PER_CELL = float(os.environ.get('PHASE3_TOKENS_PER_CELL', '8.0'))
_FIXED_OVERHEAD = int(os.environ.get('PHASE3_FIXED_OVERHEAD_TOKENS', '3000'))
_CHUNK_THRESHOLD = int(os.environ.get('PHASE3_LAYER_CHUNK_THRESHOLD_TOKENS', '35000'))
_CHUNK_TARGET = int(os.environ.get('PHASE3_CHUNK_TARGET_TOKENS', '30000'))


def _per_sheet_stats(filepath: str, sheets: list[str]) -> dict:
    """Walk only the given sheets. Returns {sheet: {'cells': N, 'rows': R}}."""
    if not sheets:
        return {}
    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
    stats: dict = {}
    try:
        for sname in sheets:
            if sname not in wb.sheetnames:
                stats[sname] = {'cells': 0, 'rows': 0}
                continue
            ws = wb[sname]
            cells = 0
            populated_rows = 0
            for row in ws.iter_rows(values_only=True):
                row_has_value = False
                for v in row:
                    if v is not None and v != '':
                        cells += 1
                        row_has_value = True
                if row_has_value:
                    populated_rows += 1
            stats[sname] = {'cells': cells, 'rows': populated_rows}
    finally:
        wb.close()
    return stats


def _estimate_output_tokens(cells: int) -> int:
    return _FIXED_OVERHEAD + int(cells * _TOKENS_PER_CELL)


def estimate_layer_output(filepath: str, sheets: list[str]) -> int:
    """Coarse estimate of output tokens for a layer covering the given sheets."""
    stats = _per_sheet_stats(filepath, sheets)
    total_cells = sum(s['cells'] for s in stats.values())
    return _estimate_output_tokens(total_cells)


def _split_sheet_into_ranges(sheet_name: str, total_rows: int,
                             cells_per_row: float, layer: str) -> list[dict]:
    """Split one large sheet into row-range chunks so each chunk's est
    output stays ≤ _CHUNK_TARGET. Each chunk owns rows [start, end)
    (1-indexed in workbook terms; 0-indexed in worker terms, see
    orchestrator._serialize_sheets)."""
    est = _estimate_output_tokens(int(cells_per_row * total_rows))
    n = max(2, math.ceil(est / _CHUNK_TARGET))
    rows_per_chunk = max(1, math.ceil(total_rows / n))

    chunks = []
    for i in range(n):
        start = i * rows_per_chunk
        end = min(total_rows, start + rows_per_chunk)
        if start >= end:
            break
        chunks.append({
            'chunk_id': f'{layer}.{sheet_name[:20]}.rows_{start}_{end}',
            'layer': layer,
            'sheets': [sheet_name],
            'row_ranges': {sheet_name: (start, end)},
            'est_output_tokens': _estimate_output_tokens(int(cells_per_row * (end - start))),
            'flavor_b': True,
        })
    return chunks


def plan_chunks(filepath: str, layer: str, sheets: list[str]) -> list[dict]:
    """Decide if Flavor B activates and produce a chunk plan.

    Returns a list of chunk dicts of shape:
      {
        'chunk_id':            str,
        'layer':               str,
        'sheets':              list[str],
        'row_ranges':          dict[sheet_name, (start, end)]  # optional
                               (default: full sheet),
        'est_output_tokens':   int,
        'flavor_b':            bool,
      }

    Layer 1 IS allowed to chunk (E2 fix). Some funds have huge fund_pl_bs
    sheets routed to L1 and would otherwise truncate.
    """
    if not sheets:
        return []

    stats = _per_sheet_stats(filepath, sheets)
    total_cells = sum(s['cells'] for s in stats.values())
    total_est = _estimate_output_tokens(total_cells)

    if total_est <= _CHUNK_THRESHOLD:
        return [{
            'chunk_id': layer,
            'layer': layer,
            'sheets': list(sheets),
            'row_ranges': {},
            'est_output_tokens': total_est,
            'flavor_b': False,
        }]

    # Identify sheets that ALONE exceed the chunk target → range-split them.
    # Pack the rest into bin-packed chunks.
    big_sheets: list[str] = []
    small_sheets: list[str] = []
    for sname in sheets:
        cells = stats.get(sname, {}).get('cells', 0)
        if _estimate_output_tokens(cells) > _CHUNK_TARGET:
            big_sheets.append(sname)
        else:
            small_sheets.append(sname)

    chunks: list[dict] = []

    for sname in big_sheets:
        rows = stats.get(sname, {}).get('rows', 0) or 1
        cells = stats.get(sname, {}).get('cells', 0)
        cells_per_row = (cells / rows) if rows else 1.0
        chunks.extend(_split_sheet_into_ranges(sname, rows, cells_per_row, layer))

    # Bin-pack small sheets first-fit-decreasing into chunks of ≤ _CHUNK_TARGET
    small_sheets.sort(
        key=lambda s: _estimate_output_tokens(stats.get(s, {}).get('cells', 0)),
        reverse=True,
    )
    bins: list[dict] = []
    for sname in small_sheets:
        est = _estimate_output_tokens(stats.get(sname, {}).get('cells', 0))
        placed = False
        for b in bins:
            if b['est_output_tokens'] + est <= _CHUNK_TARGET:
                b['sheets'].append(sname)
                b['est_output_tokens'] += est
                placed = True
                break
        if not placed:
            bins.append({
                'chunk_id': f'{layer}.bin_{len(bins) + 1}',
                'layer': layer,
                'sheets': [sname],
                'row_ranges': {},
                'est_output_tokens': est,
                'flavor_b': True,
            })
    chunks.extend(bins)

    logger.info(
        f'[phase3.token_estimator] {layer} est_output={total_est} > {_CHUNK_THRESHOLD} '
        f'→ Flavor B with {len(chunks)} chunks '
        f'({len(big_sheets)} sheet(s) row-split, {len(bins)} bin-packed chunk(s))'
    )
    return chunks


def estimate_workbook_input(filepath: str) -> int:
    """Rough total-input token estimate for the entire workbook (all sheets).
    Used for capacity-planning logging only — not for routing decisions.
    """
    wb = openpyxl.load_workbook(filepath, data_only=True, read_only=True)
    total = 0
    try:
        for sname in wb.sheetnames:
            ws = wb[sname]
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if v is not None and v != '':
                        total += 1
    finally:
        wb.close()
    return int(total * _TOKENS_PER_CELL)
