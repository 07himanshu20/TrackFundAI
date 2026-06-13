#!/usr/bin/env python3
"""Tails the backend log and produces a human-readable, append-only audit
trail of every import session at /Users/himanshusharma/portfolio-dashboard/
import_audit.log.

The audit log records, per session:
  - Fund file being imported
  - When each pass started + completed + what role it played
  - Pass 3.5 stage-1 (label-harvest) + stage-2 (semantic classification) +
    stage-3 (column-aware disambiguation) details
  - How many fields were EXTRACTED vs DERIVED
  - Every formula Gemini proposed and which one Python chose (with rejection
    reasons for hallucinated variables)
  - All Pass-7 CarriedInterest writes
  - Pass 5 audit summary

Read-only on the production pipeline — this script just observes the
backend log and structures it. Stop with Ctrl+C or via TaskStop.
"""

import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

AUDIT_FILE = '/Users/himanshusharma/portfolio-dashboard/import_audit.log'
SRC_LOG = '/tmp/tfa-backend.log'

# ──────────────────────────────────────────────────────────────────────────
# Pattern → human formatter table. Each tuple = (regex, format_string).
# Format strings receive .format(*match.groups()).
# ──────────────────────────────────────────────────────────────────────────
PATTERNS = [
    # ── Pass 1 / 1.5 ────────────────────────────────────────────────────
    (
        r"Gemini Pass 1: classified (\d+) sheets from (\d+) total",
        "PASS 1 (sheet classification): {0} of {1} sheets classified into "
        "canonical domains."
    ),
    (
        r"Gemini Pass 1\.5: classified (\d+) sections across (\d+) sheets",
        "PASS 1.5 (section classification): {0} sub-sections classified "
        "across {1} sheets."
    ),
    # ── Pass 2 ──────────────────────────────────────────────────────────
    (
        r"Pass 2 parallel round (\d+)/(\d+): dispatching (\d+) sheet\(s\) "
        r"with max_workers=(\d+)",
        "PASS 2 (column mapping) round {0}/{1}: dispatching {2} sheets in "
        "parallel ({3} workers)."
    ),
    (
        r"Pass2\.5 layout for \"(.+?)\": (\d+) sub-table\(s\); (.+)",
        "PASS 2.5 (layout) sheet={0}: detected {1} sub-table(s) — {2}"
    ),
    # ── Pass 2.6 (column semantic role classification) ────────────────
    (
        r"\[GEMINI Pass2\.6\] classify_column_roles\((.+?)\): (\d+)/(\d+) "
        r"columns classified \(roles: (.+?)\)",
        "  Pass 2.6 [section={0}]: {1}/{2} columns classified — roles={3}"
    ),
    (
        r"\[Pass2\.6\] classify_column_roles: classified roles for "
        r"(\d+) horizontal section\(s\)",
        "PASS 2.6 (column-role classification) COMPLETE: roles assigned "
        "to {0} horizontal section(s)"
    ),
    # ── Pass 3.5 role-filter + variant classifier ────────────────────
    (
        r"Pass 3\.5: ALL (\d+) candidates for (\w+) \(value_type=(\w+)\) "
        r"had incompatible column_role; leaving metric for Pass 4 derivation\.",
        "  Pass 3.5 ROLE-FILTER: [{1}] (value_type={2}) — all {0} candidates "
        "filtered out by role-compatibility; deferring to Pass 4"
    ),
    (
        r"\[Pass3\.5 role-filter\] dropped role-incompatible candidates: (\[.+\])",
        "  Pass 3.5 ROLE-FILTER summary: dropped per-metric counts → {0}"
    ),
    (
        r"\[GEMINI Pass3\.5\] classify_metric_variant\((\w+)\): tagged "
        r"(\d+)/(\d+) candidates",
        "  Pass 3.5 VARIANT-CLASSIFIER [{0}]: tagged {1}/{2} candidates "
        "with gross/net (or similar) variant"
    ),
    # ── Pass 4 with disjointness proof ────────────────────────────────
    (
        r"\[Pass4 identity\] (.+)",
        "  Pass 4 IDENTITY CHECK: {0}"
    ),
    (
        r"\[Pass7 identity-check scheme=(.+?)\] status=(\w+) "
        r"diff_pct=(\S+) reasoning=(.+)",
        "PASS 7 IDENTITY CHECK scheme={0}: status={1} diff_pct={2} — {3}"
    ),
    # ── Pass 8 (direct waterfall computation) ─────────────────────────
    (
        r"\[GEMINI Pass8\] compute_waterfall_metrics_directly: returned "
        r"(\d+) metric\(s\) for sheet (.+)",
        "PASS 8 (direct waterfall computation): Gemini returned {0} metric(s) "
        "from sheet {1}"
    ),
    (
        r"\s*\[Pass8\] (\w+) = (\S+) \(conf=(\S+), src=(\[.*?\]), "
        r"formula=\"(.+?)\"\)",
        "  Pass 8 [{0}] = {1} (conf={2}, src={3}, formula={4!r})"
    ),
    (
        r"\[Pass8\] removed (\d+) stale variant-tagged DerivedMetric rows "
        r"superseded by Pass 8 outputs\.",
        "  Pass 8 cleanup: removed {0} stale variant-tagged DerivedMetric "
        "rows that would shadow Pass 8 values."
    ),
    (
        r"\[Pass8\] scheme=(.+?) wrote (\d+) DerivedMetric rows: (\[.*?\])\. "
        r"Overall reasoning: (.+)",
        "PASS 8 COMPLETE: scheme={0} wrote {1} DerivedMetric rows: {2}\n"
        "    overall reasoning: {3}"
    ),
    # ── Pass 3 (per-sheet classify_enum / classify_labels / extract) ───
    (
        r"\[GEMINI Pass3\] classify_enum\((\w+)\): (\d+) values classified",
        "  Pass 3 [classify_enum/{0}]: {1} value(s) classified"
    ),
    (
        r"\[GEMINI Pass3\] classify_labels\((\w+)\): (\d+) labels → "
        r"(\d+) classified",
        "  Pass 3 [classify_labels/{0}]: {1} labels → {2} classified"
    ),
    (
        r"\[GEMINI Pass3\] extract_metadata\((.+?)\): (\d+) pairs → "
        r"(\d+) fields extracted",
        "  Pass 3 [extract_metadata/{0}]: {1} pairs → {2} fields extracted"
    ),
    # ── Pass 3.5 (stage 1 harvest, stage 2 classify, stage 3 disambig) ─
    (
        r"Pass 3\.5: harvested (\d+) unique label-value pairs across (\d+) "
        r"sheets",
        "PASS 3.5 STAGE-1 (harvest): {0} unique label-value occurrences "
        "captured across {1} sheets (now includes per-column candidates "
        "for tabular rows)."
    ),
    (
        r"\[GEMINI Pass3\.5\] select_authoritative_source\((\w+)\): "
        r"(\d+) candidates -> chosen_index=(\S+) confidence=(\S+)",
        "  Pass 3.5 STAGE-3 [disambiguate/{0}]: {1} candidates → chose "
        "index {2} (confidence {3})"
    ),
    (
        r"\[Pass3\.5\] persisted (\d+) DerivedMetric imported_direct rows "
        r"covering metrics: (\[.*?\])",
        "PASS 3.5 EXTRACTED: {0} fund-level metrics written as DerivedMetric "
        "(formula='(direct value imported)') → {1}"
    ),
    # ── Pass 4 (Gemini derivation + AST hallucination guard) ───────────
    (
        r"\[GEMINI Pass4\] derive_metric\((\w+)\): (\d+) candidate "
        r"formula\(s\) returned",
        "  Pass 4 [{0}]: Gemini returned {1} candidate formula(s)"
    ),
    (
        r"Pass4 rejected rank (\S+) candidate for (\w+) — "
        r"hallucinated variables: (\[.*?\])",
        "  Pass 4 [{1}] HALLUCINATION GUARD: rank {0} REJECTED — variables "
        "not in catalogue: {2}"
    ),
    (
        r"\[Pass4\] (\w+) ← rank (\d+) formula \"(.+?)\" → value=(\S+) "
        r"\(confidence=(\S+)\)",
        "  Pass 4 [{0}] DERIVED via rank-{1} formula:\n"
        "      formula = {2!r}\n"
        "      value   = {3}\n"
        "      conf    = {4}"
    ),
    (
        r"\[Pass4\] scheme=(.+?) outcomes=(\[.*?\])",
        "PASS 4 (Gemini derivation) COMPLETE: scheme={0}\n"
        "    outcomes={1}"
    ),
    # ── Pass 7 (CarriedInterest writer) ────────────────────────────────
    (
        r"\[Pass7\] CarriedInterest written for scheme=(.+)",
        "PASS 7 (CarriedInterest writer): wrote scheme={0} "
        "(reads from DerivedMetric — no formulas in code)"
    ),
    # ── Pass 6 (per-row metric completion via ranked candidate formulas)
    (
        r"\[GEMINI Pass6\] derive_per_row_formulas\((\S+)\): (\d+)/(\d+) "
        r"fields received formula sets \(total candidates: (\d+)\)",
        "  Pass 6 [{0}]: Gemini returned formula sets for {1}/{2} fields "
        "({3} candidate formula(s) total)"
    ),
    (
        r"\[Pass6\] (\S+)\.(\S+) ← (\d+) candidate\(s\) → wrote (\d+)/(\d+) "
        r"rows total \| (.+)",
        "  Pass 6 [{0}.{1}] DERIVED via {2} ranked candidate(s): {3}/{4} "
        "rows written\n"
        "      per-rank attribution: {5}"
    ),
    (
        r"\[Pass6\] per-row completion outcomes: (\[.*?\])",
        "PASS 6 (per-row completion) COMPLETE:\n    outcomes={0}"
    ),
    # ── Pass 6.5 / 6.6 (KPI projection + percentage derivation) ────────
    (
        r"\[Pass6\.5\] projected (\d+) PortfolioKPI rows from (\d+) "
        r"\(sheet, kpi_fields\) pairs: (\[.*?\])",
        "PASS 6.5 (universal KPI projection): {0} PortfolioKPI rows EXTRACTED "
        "from {1} source pairs: {2}"
    ),
    (
        r"\[Pass6\.6\] derived (\d+) percentage PortfolioKPI rows across "
        r"(\d+) target metrics \((\[.*?\])\)",
        "PASS 6.6 (KPI % derivation via Gemini formulas): {0} percentage "
        "PortfolioKPI rows DERIVED across {1} target metrics: {2}"
    ),
    # ── Pass 5 audit ───────────────────────────────────────────────────
    (
        r"\[Pass5 Audit\] (.+)",
        "PASS 5 AUDIT: {0}"
    ),
    # ── Legacy carry path (overwritten by Pass 7) ──────────────────────
    (
        r"\bCarried interest \((.+?)\): called=(\S+), distributions=(\S+), "
        r"pref_return=(\S+) .*?, carry_base=(\S+), carry_gross=(\S+), "
        r"clawback=(\S+), carry_net=(\S+), status=(\S+)",
        "  (LEGACY carry path — will be overwritten by Pass 7): scheme={0} "
        "called={1} dist={2} pref={3} base={4} gross={5} clawback={6} "
        "net={7} status={8}"
    ),
    # ── Errors / API failures ──────────────────────────────────────────
    (
        r"Gemini Pass(\S+) (.+?) failed after (\d+) attempt\(s\): (.+)",
        "  ❌ Pass {0} {1} FAILED after {2} attempts: {3}"
    ),
    (
        r"Pass4 _safe_eval cannot evaluate \"(.+?)\": (.+)",
        "  ❌ Pass 4 evaluator: cannot evaluate {0!r} — {1}"
    ),
    # ── File / fund identity (catches the import target) ───────────────
    (
        r"\[FundImportService\] importing fund=(.+)",
        "FUND IMPORT TARGET: {0}"
    ),
    (
        r"^ImportFile\b.+original_filename=([^ ]+)",
        "FUND FILE: {0}"
    ),
    (
        r"\bimport_fund.+filename[\"']?: ?[\"']?([^\"',]+)[\"']?",
        "FUND FILE: {0}"
    ),
]


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def time_str():
    return datetime.now().strftime('%H:%M:%S')


def write_audit(audit_file, line):
    audit_file.write(line + '\n')
    audit_file.flush()


def write_session_header(audit_file):
    sep = '=' * 96
    audit_file.write('\n\n')
    audit_file.write(sep + '\n')
    audit_file.write(f'IMPORT SESSION START: {now_str()}\n')
    audit_file.write('Backend log source:   ' + SRC_LOG + '\n')
    audit_file.write(sep + '\n\n')
    audit_file.flush()


def write_session_footer(audit_file, counters):
    sep = '─' * 96
    audit_file.write('\n')
    audit_file.write(sep + '\n')
    audit_file.write(f'[{time_str()}] SESSION ROLLUP (cumulative for this audit-log tail process):\n')
    for k, v in sorted(counters.items()):
        audit_file.write(f'  - {k}: {v}\n')
    audit_file.write(sep + '\n')
    audit_file.flush()


def tail_log_iter(path):
    """Tail-F equivalent. Yields each new line as it lands."""
    while not os.path.exists(path):
        time.sleep(1)
    with open(path, 'r') as f:
        f.seek(0, 2)  # End of file — only see NEW lines
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            yield line.rstrip('\n')


def main():
    audit_file = open(AUDIT_FILE, 'a', encoding='utf-8')
    write_session_header(audit_file)

    # Print one banner to stdout so the Monitor sees the audit-logger started
    banner = f'[{time_str()}] AUDIT LOGGER STARTED → {AUDIT_FILE}'
    print(banner, flush=True)
    write_audit(audit_file, banner)

    counters = Counter()
    last_summary_ts = time.time()

    try:
        for raw in tail_log_iter(SRC_LOG):
            stripped = raw.strip()
            if not stripped:
                continue

            # Match against every pattern. First match wins (so the
            # most-specific patterns should come earliest in PATTERNS).
            matched = False
            for pat, fmt in PATTERNS:
                m = re.search(pat, stripped)
                if not m:
                    continue
                try:
                    formatted = fmt.format(*m.groups())
                except (IndexError, KeyError):
                    formatted = stripped
                stamped = f'[{time_str()}] {formatted}'
                write_audit(audit_file, stamped)
                # Stdout = Monitor event stream
                print(stamped, flush=True)

                # Counters for the rollup line
                first_token = formatted.split(':', 1)[0].strip()
                counters[first_token] += 1
                matched = True
                break

            # Optionally capture unmatched lines that look interesting
            # (so we never silently lose data).
            if not matched:
                upper = stripped.upper()
                if any(needle in upper for needle in (
                    'TRACEBACK', 'ERROR', 'WARNING', 'IMPORT', 'PASS '
                )):
                    raw_log = f'[{time_str()}] raw: {stripped[:300]}'
                    write_audit(audit_file, raw_log)
                    # Don't print every raw line to stdout to avoid
                    # noisy Monitor notifications.

            # Periodic rollup once per minute
            now = time.time()
            if now - last_summary_ts > 60:
                last_summary_ts = now
                write_session_footer(audit_file, counters)
                # Reset between rollups so they're cumulative for the
                # last minute — easier to spot bursts.
                counters.clear()
    except KeyboardInterrupt:
        write_session_footer(audit_file, counters)
        print(f'[{time_str()}] AUDIT LOGGER STOPPED (Ctrl+C)', flush=True)
    finally:
        write_session_footer(audit_file, counters)
        audit_file.write(f'\n[{time_str()}] AUDIT LOGGER EXITED\n')
        audit_file.close()


if __name__ == '__main__':
    main()
