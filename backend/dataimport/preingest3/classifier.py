"""
S3 — Classification (model, cache-gated).

From the bounded profile ALONE (never the workbook), decide what a file is:
  • fund          — a fund-level workbook (LP register, capital calls,
                    investments, valuations, distributions, accounts, compliance)
  • mis           — a single portfolio company's monthly MIS (P&L / BS / CF / KPI)
  • unrecognised  — a FIRST-CLASS outcome with a rejection path (doc S3). An
                    unrecognised file is never guessed into the pipeline; it is
                    held and reported.

The model returns a class + a claimed entity name + a self-reported confidence.
Per build rule #4 that confidence NEVER contributes to a downstream pass
decision — it only orders the review queue and gates whether a human confirms
the classification. The call is small (profile summary in, one object out) and
checkpointed, so re-runs cost nothing.
"""
from __future__ import annotations

import json
from typing import List

from . import llm
from .contract import CONFIDENCE_TAU

FUND = 'fund'
MIS = 'mis'
UNRECOGNISED = 'unrecognised'

_PROMPT = """You classify one uploaded spreadsheet for an Indian AIF fund-consolidation pipeline.
You are given ONLY a compact structural profile of each sheet (names, headers, row labels) — never the raw data.

Decide the file's class:
- "fund": a fund-level workbook — e.g. LP/investor register, capital calls/drawdowns, investment/deployment schedule, valuations, distributions, fund accounts/fees/budget, SEBI compliance.
- "mis": ONE portfolio company's operating monthly MIS — company P&L / balance sheet / cash flow / operating KPIs for a single business.
- "unrecognised": does not match either (e.g. a random unrelated spreadsheet).

Also extract the single entity name the file is about (the fund name for "fund", the company name for "mis"), or "" if none is evident.

Return STRICT JSON only, no prose:
{"file_class":"fund|mis|unrecognised","subtype":"short-tag","entity_name":"...","confidence":0.0-1.0,"reason":"one short sentence"}

FILE: {label}
SHEETS:
{sheets}
"""


def _summary(profiles: List[dict]) -> str:
    out = []
    for p in profiles:
        mv = p if isinstance(p, dict) else p.to_model_view()
        heads = ', '.join(h['text'] for h in mv.get('headers', [])[:12])
        labs = ' | '.join(l['text'] for l in mv.get('labels', [])[:12])
        tag = ' LEDGER' if mv.get('is_raw_ledger') else ''
        out.append(f"- {mv['sheet']} [{mv['dims'][0]}x{mv['dims'][1]}{tag}]"
                   f" headers: {heads} :: labels: {labs}")
    return '\n'.join(out)


def classify(label: str, profiles: List, content_fp: str) -> dict:
    """profiles = list[SheetProfile]. Returns a classification dict with a
    needs_review flag. Never raises on a model hiccup — an unparseable reply
    degrades to UNRECOGNISED + needs_review (fail-closed, never fail-open)."""
    model_views = [p.to_model_view() for p in profiles]
    prompt = _PROMPT.replace('{label}', label).replace('{sheets}', _summary(model_views))
    res = llm.call_json('classify', f'{content_fp}', prompt, read_timeout_s=60.0)

    if res.is_error:
        # a failed call is NOT an 'unrecognised file' — it's a transport ERROR
        # that must HOLD the file for retry, never silently blank it.
        return {'file_class': 'error', 'subtype': '', 'entity_name': '',
                'confidence': 0.0, 'reason': res.reason, 'needs_review': True, 'error': True}
    data = res.data
    if not isinstance(data, dict) or data.get('file_class') not in (FUND, MIS, UNRECOGNISED):
        return {'file_class': UNRECOGNISED, 'subtype': '', 'entity_name': '',
                'confidence': 0.0, 'reason': 'no usable classification from model',
                'needs_review': True}

    try:
        conf = float(data.get('confidence') or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    fc = data['file_class']
    return {
        'file_class': fc,
        'subtype': str(data.get('subtype') or ''),
        'entity_name': str(data.get('entity_name') or '').strip(),
        'confidence': conf,
        'reason': str(data.get('reason') or ''),
        # review when the model is unsure, or when it can't name the entity, or
        # when it doesn't recognise the file at all.
        'needs_review': fc == UNRECOGNISED or conf < CONFIDENCE_TAU,
    }
