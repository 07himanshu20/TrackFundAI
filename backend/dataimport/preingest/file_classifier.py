"""
File-type classification — ONE cheap Gemini call per file.

Decides whether a workbook is:
  • "fund"        — a fund-level workbook (LP register, capital calls, the fund's
                    investments/valuations/waterfall/NAV/compliance). Feeds the
                    fund-level output sheets.
  • "company_mis" — ONE operating company's monthly MIS (P&L / BS / cashflow /
                    KPIs for a single entity). Collapses to ONE Portfolio row.

Judged purely from the structural fingerprints (entity banners, sheet mix), never
from the filename. Universal: works for any company or fund, any market.
"""
import json
import logging

from ..gemini_column_mapper import _call_gemini

logger = logging.getLogger(__name__)


def _compact(fingerprints):
    out = []
    for fp in fingerprints:
        out.append({
            'sheet': fp['sheet'],
            'entities': [h['text'] for h in fp.get('entity_hints', [])][:3],
            'header': fp.get('header', [])[:12],
        })
    return out


def classify_file(fingerprints, timeout_ms=120_000):
    """Return {'kind': 'fund'|'company_mis', 'company_name': str|None,
    'confidence': 'high'|'low'}."""
    sheets = _compact(fingerprints)
    prompt = f"""You are a fund-operations analyst. Below are the sheets of ONE Excel
workbook (names, the company/entity text seen in the banner, and header cells).

Decide what this workbook IS as a whole:
  "fund"        = a FUND's workbook: it tracks Limited Partners / capital calls /
                  the fund's portfolio of MANY companies / distributions /
                  waterfall / NAV / fund compliance.
  "company_mis" = the monthly MIS of a SINGLE operating company: profit & loss,
                  balance sheet, cash flow, KPIs — all for ONE business entity.

Also return the single company/business name if kind is "company_mis" (from the
banner text), else null.

Return JSON only:
{{"kind": "fund"|"company_mis", "company_name": <str|null>, "confidence": "high"|"low"}}

SHEETS:
{json.dumps(sheets, indent=0)}
"""
    resp = _call_gemini(prompt, context_label='preingest.fileclass',
                        timeout_ms=timeout_ms) or {}
    kind = resp.get('kind')
    if kind not in ('fund', 'company_mis'):
        # Deterministic universal fallback: a workbook whose sheets nearly all
        # carry the SAME single entity banner is a company MIS; otherwise fund.
        banners = set()
        for fp in fingerprints:
            for h in fp.get('entity_hints', [])[:1]:
                banners.add(h['text'].lower())
        kind = 'company_mis' if len(banners) == 1 else 'fund'
        return {'kind': kind, 'company_name': (banners.pop() if len(banners) == 1 else None),
                'confidence': 'low'}
    return {'kind': kind, 'company_name': resp.get('company_name'),
            'confidence': resp.get('confidence', 'low')}
