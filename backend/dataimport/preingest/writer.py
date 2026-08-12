"""
Write the consolidated canonical workbook (TFAI.xlsx).

Layout:
  • one sheet per domain — rows are canonical records, columns are the union
    of keys seen for that domain (provenance columns first).
  • _Manifest — every source sheet across every file and how it was classified.
  • _Review  — only the items a human must confirm (uncertain unit / entity /
    possible duplicate / no cached numbers). This is the focused review screen
    in spreadsheet form; short by design.
  • _Coverage — per-file totals so nothing is silently dropped.

Values are written exactly as copied from the source cells. No number is
computed or invented here.
"""
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

_PROV_ORDER = ['__source_file__', '__source_sheet__', '__entity__',
               '__unit__', '__currency__', '__unit_confidence__']

_HEADER_FILL = PatternFill('solid', fgColor='1F3A5F')
_HEADER_FONT = Font(color='FFFFFF', bold=True)
_REVIEW_FILL = PatternFill('solid', fgColor='FDE9D9')


def _clean(v):
    """openpyxl can't write Decimal-subclass oddities or tz-aware edge cases
    cleanly in every case; coerce to a safe primitive."""
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
    return v


def _domain_columns(records):
    prov = [c for c in _PROV_ORDER if any(c in r for r in records)]
    others = []
    seen = set(prov)
    for r in records:
        for k in r.keys():
            if k not in seen:
                others.append(k)
                seen.add(k)
    return prov + others


def _write_table(ws, columns, rows):
    ws.append(columns)
    for ci in range(1, len(columns) + 1):
        c = ws.cell(row=1, column=ci)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
    for r in rows:
        ws.append([_clean(r.get(c)) for c in columns])
    # reasonable column widths
    for ci, name in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = min(
            max(12, len(str(name)) + 2), 40)
    ws.freeze_panes = 'A2'


def build_workbook(domain_records, manifest, review, coverage):
    """domain_records: {domain: [record,...]}. Returns an openpyxl Workbook."""
    wb = Workbook()
    wb.remove(wb.active)

    # audit sheets first so they're easy to find
    mws = wb.create_sheet('_Manifest')
    mcols = ['file', 'sheet', 'domain', 'layout', 'unit', 'unit_confidence',
             'entity', 'entity_confidence', 'records', 'status', 'notes']
    _write_table(mws, mcols, manifest)

    rws = wb.create_sheet('_Review')
    rcols = ['file', 'sheet', 'issue', 'detail', 'suggested_value']
    _write_table(rws, rcols, review)
    for row in rws.iter_rows(min_row=2):
        for c in row:
            c.fill = _REVIEW_FILL

    cws = wb.create_sheet('_Coverage')
    ccols = ['file', 'sheets_total', 'sheets_mapped', 'sheets_skipped',
             'records_total', 'sheets_needing_review']
    _write_table(cws, ccols, coverage)

    for domain in sorted(domain_records.keys()):
        records = domain_records[domain]
        if not records:
            continue
        # Excel sheet-name limit 31 chars, must be unique
        name = domain[:31]
        ws = wb.create_sheet(name)
        _write_table(ws, _domain_columns(records), records)

    return wb
