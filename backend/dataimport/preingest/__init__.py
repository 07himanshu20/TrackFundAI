"""
Pre-ingestion layer — consolidate many raw client Excel files (10-20 MIS +
fund-level workbooks) into one canonical TFAI.xlsx that the normal Phase-6
import pipeline can then ingest.

Design (Approach 3'):
  1. fingerprint.py  — deterministic. Per sheet, build a compact structural
     fingerprint (top rows, detected header row, unit/entity/table hints,
     2 sample rows). ~200 tokens per sheet regardless of row count.
  2. mapper.py       — ONE Gemini call per file. Input = the fingerprints
     only (never raw data rows). Output = per-sheet map
     {domain, layout, unit, entity, header_row, column_map, tables, skip}.
     Gemini only MAPS; it never emits a single data value.
  3. mover.py        — deterministic. Given the map, read the CACHED cell
     values (openpyxl data_only=True) and copy them into canonical row
     dicts. Numbers never pass through the LLM → 100% reproducible.
  4. consolidator.py — orchestrates fingerprint→map→move for every file,
     assembles all rows by domain, writes TFAI.xlsx + _Manifest/_Review/
     _Coverage audit sheets.

Hard rule (proven against real MIS files): up to ~33% of formulas in these
workbooks reference OTHER sheets, so sheets are NOT self-contained at the
formula level — but every such cell carries a cached computed VALUE, so they
ARE self-contained at the value level. We therefore ALWAYS read cached values
and NEVER copy or re-evaluate a formula.
"""
