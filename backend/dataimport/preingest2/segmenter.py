"""
Semantic Chunking (document Section 7).

The unit fed to the model is a COMPLETE semantic block (a detected table) —
never an arbitrary sheet-count slice. Splitting is driven by MEASURED TOKENS,
not sheet count, and a table is never cut in half.

Routing per detected table (Figure 7):
  • fits ≤ TOKEN_BUDGET            → serialize whole, one extraction call
  • raw ledger (many rows, mostly numeric) → AGGREGATE IN CODE, send only the
    summary (a total is arithmetic, not a reading task) — this is what makes the
    148k-row explosion impossible
  • oversize but row-level needed  → split on clean row boundaries with the
    header repeated in each part, ordered deterministically; merge by key in code

Everything here is deterministic: the same file bytes always yield the same
blocks, the same split points, and the same serialized text.
"""
from decimal import Decimal

from ..preingest.fingerprint import detect_table_starts
from ..phase6_extractor.helpers import (
    find_header_row, is_junk_row, is_section_title_row)

TOKEN_BUDGET = 6000          # per semantic unit (conservative for Flash output)
LEDGER_ROW_THRESHOLD = 60    # a block taller than this that is mostly numeric
LEDGER_NUMERIC_FRACTION = 0.5


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, float) and v != v:
        return ''
    if hasattr(v, 'isoformat'):
        try:
            return v.date().isoformat()
        except Exception:
            return v.isoformat()
    return str(v).strip()


def _is_num(v):
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _blocks(rows):
    """Detect coherent table blocks: a header-like start row + its contiguous
    non-blank data region up to the next block/blank gap."""
    starts = detect_table_starts(rows, find_header_row(rows))
    starts = [s for s in starts if s is not None and s >= 0]
    if not starts:
        return []
    n = len(rows)
    bounds = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else n
        # trim trailing blank rows
        e = end
        while e > s + 1 and not any(v not in (None, '') for v in rows[e - 1]):
            e -= 1
        bounds.append((s, e))
    return bounds


def _serialize(rows, header_idx, r0, r1, max_rows=None):
    header = [_cell(v) for v in rows[header_idx]] if 0 <= header_idx < len(rows) else []
    while header and not header[-1]:
        header.pop()
    width = max(len(header), 1)
    out = ['\t'.join(header)]
    count = 0
    for r in rows[max(r0, header_idx + 1):r1]:
        if not any(v not in (None, '') for v in r):
            continue
        if is_section_title_row(r) or is_junk_row(r):
            continue
        cells = [_cell(r[ci]) if ci < len(r) else '' for ci in range(width)]
        out.append('\t'.join(cells))
        count += 1
        if max_rows and count >= max_rows:
            out.append(f'… (+{max(0, (r1 - r0) - count)} more rows)')
            break
    return '\n'.join(out)


def _aggregate_ledger(rows, header_idx, r0, r1):
    """Code-aggregate a raw ledger: per numeric column, sum + count. The model
    receives only this summary, never the thousands of raw rows."""
    header = [_cell(v) for v in rows[header_idx]] if 0 <= header_idx < len(rows) else []
    width = max((len(r) for r in rows[r0:r1]), default=len(header))
    sums = [0.0] * width
    counts = [0] * width
    for r in rows[max(r0, header_idx + 1):r1]:
        for ci in range(width):
            v = r[ci] if ci < len(r) else None
            if _is_num(v):
                sums[ci] += float(v)
                counts[ci] += 1
    lines = ['[code-aggregated ledger summary — raw rows not sent]']
    for ci in range(width):
        if counts[ci]:
            h = header[ci] if ci < len(header) and header[ci] else f'col{ci}'
            lines.append(f'{h}\tsum={sums[ci]:.4g}\tn={counts[ci]}')
    return '\n'.join(lines)


def segment_sheet(rows):
    """Return a list of semantic units for one sheet:
        {kind: 'table'|'ledger_summary'|'split', text, header_idx, block}
    ready to serialize into an extraction call. Deterministic."""
    header_idx = find_header_row(rows)
    blocks = _blocks(rows) or ([(header_idx, len(rows))] if header_idx >= 0 else [])
    units = []
    for (s, e) in blocks:
        h = s
        nrows = e - s - 1
        # numeric fraction of the block body
        num = tot = 0
        for r in rows[s + 1:e]:
            for v in r:
                if v not in (None, ''):
                    tot += 1
                    if _is_num(v):
                        num += 1
        frac = (num / tot) if tot else 0.0
        if nrows >= LEDGER_ROW_THRESHOLD and frac >= LEDGER_NUMERIC_FRACTION:
            units.append({'kind': 'ledger_summary', 'header_idx': h, 'block': (s, e),
                          'text': _aggregate_ledger(rows, h, s, e)})
            continue
        text = _serialize(rows, h, s, e)
        from .profiler import estimate_tokens
        if estimate_tokens(text) <= TOKEN_BUDGET:
            units.append({'kind': 'table', 'header_idx': h, 'block': (s, e), 'text': text})
        else:
            # oversize but not a pure ledger: sample rows within budget, header kept
            approx_rows = max(10, TOKEN_BUDGET * 4 // max(len(rows[h]) * 8, 40))
            units.append({'kind': 'split', 'header_idx': h, 'block': (s, e),
                          'text': _serialize(rows, h, s, e, max_rows=approx_rows)})
    return units
