"""
Stage MOVE — deterministic. No Gemini, no heuristics that interpret content.

Given a sheet's MAP (from mapper.py) and its cached rows (workbook_cache,
data_only=True), copy the actual values into canonical records. Every record
carries provenance (__source_file__, __source_sheet__, __entity__, __unit__,
__unit_confidence__) so nothing is ever anonymous in the consolidated file.

Design — universal, not per-format:

  1. The MAP is the single source of truth for structure. The semantic layer
     (Gemini) decided header_row, label_column, value_columns, column_map and
     layout. Python does NOT second-guess that with content regexes (no
     "is this header a period?", no "does this label look like a banner?").
     Those heuristics only work for one shape and break on the next
     (period columns vs Actual/Budget/PY scenario columns vs entity columns).

  2. Any multi-column layout (wide_period, entity_pivoted, scenario columns)
     collapses to ONE generic operation — unpivot: for every
     (label row  x  value column) emit {line_item, dimension, value} where
     `dimension` is the column header verbatim (a month, a quarter, "Actual",
     "Budget", an LP id — whatever the client wrote). This is format-agnostic.

  3. Values are copied VERBATIM from cached cells. No number is scaled,
     multiplied, or invented here. Per-line unit meaning (money vs count vs
     ratio) is genuinely unknowable deterministically, so unit is RECORDED as
     metadata (__unit__ / __unit_confidence__) and normalization is deferred to
     the separate confirmed step. There is no hardcoded list of "money fields".

  4. The ONLY rows dropped are dropped by universal structural rules already
     used across this codebase: fully-blank rows, is_junk_row (totals /
     subtotals / notes), is_section_title_row, and the generic rule "a data row
     must carry at least one value in the mapped value columns".
"""
import re

from ..phase6_extractor.helpers import is_junk_row, is_section_title_row, slug


def _norm(text):
    return slug(text) if text else ''


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        or type(v).__name__ == 'Decimal'


def _build_col_index(header_cells, column_map):
    """raw_header text -> column index, matched exact then normalized."""
    by_exact, by_norm = {}, {}
    for ci, cell in enumerate(header_cells):
        t = str(cell).strip() if cell is not None else ''
        if t and t not in by_exact:
            by_exact[t] = ci
        n = _norm(t)
        if n and n not in by_norm:
            by_norm[n] = ci
    resolved = {}  # canonical_field -> column index
    for raw, canonical in (column_map or {}).items():
        ci = by_exact.get(raw)
        if ci is None:
            ci = by_norm.get(_norm(raw))
        if ci is not None:
            resolved[canonical] = ci
    return resolved, by_exact, by_norm


def _resolve_col(ref, by_exact, by_norm):
    """Resolve a label_column / value_column reference (raw header text, a
    'col:<index>' token, or an int) to a column index."""
    if ref is None:
        return None
    if isinstance(ref, int):
        return ref
    s = str(ref).strip()
    mm = re.match(r'^col:(\d+)$', s, re.I)
    if mm:
        return int(mm.group(1))
    if s in by_exact:
        return by_exact[s]
    return by_norm.get(_norm(s))


def _header_text(cell):
    """A column-header rendered as a stable string dimension label."""
    if cell is None:
        return ''
    if hasattr(cell, 'isoformat'):
        try:
            return cell.date().isoformat()
        except Exception:
            return cell.isoformat()
    return str(cell).strip()


def _provenance(file_label, sheet_name, m):
    return {
        '__source_file__': file_label,
        '__source_sheet__': sheet_name,
        '__entity__': m.get('entity'),
        '__unit__': m.get('unit', 'raw'),
        '__currency__': m.get('currency'),
        '__unit_confidence__': m.get('unit_confidence', 'low'),
    }


def _data_rows(rows, header_idx):
    """Yield candidate data rows below the header, applying only the universal
    structural filters (blank / junk / section-title)."""
    start = header_idx + 1 if header_idx >= 0 else 0
    for r in rows[start:]:
        if not any(v not in (None, '') for v in r):
            continue
        if is_section_title_row(r) or is_junk_row(r):
            continue
        yield r


def _value_columns(m, header_cells, label_ci, by_exact, by_norm):
    """The columns whose cells carry values, taken from the MAP. If the map
    named none, fall back to every column (except the label) that has a
    non-empty header. Returns (cols, used_fallback) where cols is
    [(dimension_label, col_index), ...]. No content interpretation — the
    dimension is the header text verbatim; when the columns had to be inferred
    (used_fallback) the caller flags the sheet for review."""
    cols, seen = [], set()
    for vc in (m.get('value_columns') or []):
        ci = _resolve_col(vc, by_exact, by_norm)
        if ci is None or ci in seen:
            continue
        dim = _header_text(header_cells[ci]) if ci < len(header_cells) else ''
        cols.append((dim or str(vc), ci))
        seen.add(ci)
    if cols:
        return cols, False
    for ci, cell in enumerate(header_cells):
        if ci == label_ci:
            continue
        dim = _header_text(cell)
        if dim:
            cols.append((dim, ci))
            seen.add(ci)
    return cols, True


def move_sheet(file_label, sheet_name, rows, m):
    """Return (records, report). records is a list of canonical dicts; report
    describes what happened, for the _Manifest / _Review audit sheets."""
    report = {
        'file': file_label, 'sheet': sheet_name,
        'domain': m.get('domain'), 'layout': m.get('layout'),
        'unit': m.get('unit'), 'unit_confidence': m.get('unit_confidence'),
        'entity': m.get('entity'), 'entity_confidence': m.get('entity_confidence'),
        'records': 0, 'status': 'ok', 'notes': m.get('notes', ''),
        'needs_review': [],
    }
    if m.get('skip') or not m.get('domain'):
        report['status'] = 'skipped'
        return [], report

    hdr = m.get('header_row')
    header_idx = (hdr - 1) if isinstance(hdr, int) and hdr > 0 else -1
    header_cells = rows[header_idx] if 0 <= header_idx < len(rows) else []
    resolved, by_exact, by_norm = _build_col_index(header_cells, m.get('column_map'))
    prov = _provenance(file_label, sheet_name, m)
    layout = m.get('layout', 'tabular')
    records = []

    if m.get('unit_confidence') != 'high':
        report['needs_review'].append('unit_uncertain')
    if m.get('entity') and m.get('entity_confidence') != 'high':
        report['needs_review'].append('entity_uncertain')

    if layout in ('wide_period', 'entity_pivoted'):
        label_ci = _resolve_col(m.get('label_column'), by_exact, by_norm)
        if label_ci is None:
            label_ci = 0
        value_cols, used_fallback = _value_columns(
            m, header_cells, label_ci, by_exact, by_norm)
        if not value_cols:
            report['status'] = 'no_value_columns'
            report['needs_review'].append('no_value_columns')
            return [], report
        if used_fallback:
            report['needs_review'].append('inferred_value_columns')
        if header_idx < 0:
            report['needs_review'].append('no_header_row')
        found_num = False
        for r in _data_rows(rows, header_idx):
            label = r[label_ci] if label_ci < len(r) else None
            label = str(label).strip() if label is not None else ''
            if not label:
                continue
            # generic rule: keep the row only if it carries a value in >=1
            # mapped value column (a text-only banner row carries none)
            cells = [(dim, r[ci]) for dim, ci in value_cols
                     if ci < len(r) and r[ci] not in (None, '')]
            if not cells:
                continue
            for dim, val in cells:
                if _is_number(val):
                    found_num = True
                rec = dict(prov)
                rec['line_item'] = label
                rec['dimension'] = dim
                rec['value'] = val
                records.append(rec)
        if not found_num:
            report['status'] = 'no_numeric_values'
            report['needs_review'].append('no_cached_numbers')

    elif layout == 'key_value':
        data = list(_data_rows(rows, header_idx))
        # Universal label/value column detection. Do NOT blindly default to
        # columns 0/1 — many sheets have a leading blank/indent column, which
        # would make every label empty. Take the classifier's hint when it
        # resolves to a column that actually holds text labels; otherwise pick
        # the column with the most text labels as the label column, and the
        # next column that most often holds a value as the value column.
        hint_label = _resolve_col(m.get('label_column'), by_exact, by_norm)
        vcs = m.get('value_columns') or []
        hint_value = _resolve_col(vcs[0], by_exact, by_norm) if vcs else None
        width = max((len(r) for r in data), default=0)
        text_count = [0] * width
        value_count = [0] * width
        for r in data:
            for ci in range(width):
                v = r[ci] if ci < len(r) else None
                if v in (None, ''):
                    continue
                value_count[ci] += 1
                if not _is_number(v) and not hasattr(v, 'isoformat'):
                    text_count[ci] += 1
        label_ci = hint_label if (hint_label is not None and hint_label < width
                                  and text_count[hint_label] > 0) else None
        if label_ci is None:
            label_ci = max(range(width), key=lambda c: text_count[c]) if width else 0
        if hint_value is not None and hint_value < width and hint_value != label_ci:
            val_ci = hint_value
        else:
            after = [c for c in range(label_ci + 1, width) if value_count[c] > 0]
            val_ci = (max(after, key=lambda c: value_count[c]) if after else
                      max((c for c in range(width) if c != label_ci),
                          key=lambda c: value_count[c], default=label_ci + 1))
        for r in data:
            label = r[label_ci] if label_ci < len(r) else None
            label = str(label).strip() if label is not None else ''
            if not label:
                continue
            val = r[val_ci] if val_ci < len(r) else None
            if val in (None, ''):
                continue
            rec = dict(prov)
            rec['line_item'] = label
            rec['value'] = val
            records.append(rec)

    else:  # tabular
        if not resolved:
            report['status'] = 'no_columns_mapped'
            report['needs_review'].append('no_column_map')
            return [], report
        for r in _data_rows(rows, header_idx):
            rec = dict(prov)
            has_value = False
            for canonical, ci in resolved.items():
                val = r[ci] if ci < len(r) else None
                if val in (None, ''):
                    continue
                rec[canonical] = val   # verbatim; never scaled
                has_value = True
            if has_value:
                records.append(rec)

    report['records'] = len(records)
    return records, report
