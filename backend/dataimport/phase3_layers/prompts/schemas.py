"""
Vertex AI response_schema definitions per layer.

Strategy: ENFORCE TOP-LEVEL SHAPE ONLY. Each layer's allowed top-level
keys are declared with type=object or type=array; array items are typed
as {"type": "object"} with no property constraints so Gemini can still
emit arbitrary domain fields (the field vocabulary lives in the prompt).

Why minimal: Vertex AI's response_schema rejects unsupported OpenAPI 3
constructs (oneOf, anyOf with primitives, deeply nested $refs). A strict
exhaustive schema would either reject in production or strip useful
fields. The minimal top-level schema eliminates the highest-impact
malformed-JSON failure class (A6: non-dict top-level, A2: missing/extra
top-level keys) without risking 400 errors.

USE_RESPONSE_SCHEMA can be turned off via env if a specific model
version rejects this schema shape — the auto-split + tolerant parser
defenses still cover correctness.
"""

import os


USE_RESPONSE_SCHEMA = os.environ.get(
    'PHASE3_USE_RESPONSE_SCHEMA', 'True'
).lower() in ('true', '1', 'yes')


_OBJECT_ITEM = {'type': 'object'}
_ARRAY_OF_OBJECTS = {'type': 'array', 'items': _OBJECT_ITEM}
_OBJECT_BLOCK = {'type': 'object'}


LAYER1_SCHEMA = {
    'type': 'object',
    'properties': {
        'fund_master':        _OBJECT_BLOCK,
        'investors':          _ARRAY_OF_OBJECTS,
        'commitments':        _ARRAY_OF_OBJECTS,
        'capital_calls':      _ARRAY_OF_OBJECTS,
        'distributions':      _ARRAY_OF_OBJECTS,
        'nav_records':        _ARRAY_OF_OBJECTS,
        'waterfall':          _OBJECT_BLOCK,
        'fund_performance':   _OBJECT_BLOCK,
        'entities':           _ARRAY_OF_OBJECTS,
        'compliance_records': _ARRAY_OF_OBJECTS,
        'sheet_completeness': _ARRAY_OF_OBJECTS,
        'provenance':         _OBJECT_BLOCK,
    },
}

LAYER2_SCHEMA = {
    'type': 'object',
    'properties': {
        'portfolio_investments': _ARRAY_OF_OBJECTS,
        'valuations':            _ARRAY_OF_OBJECTS,
        'exits':                 _ARRAY_OF_OBJECTS,
        'quoted_unquoted':       _ARRAY_OF_OBJECTS,
        'sheet_completeness':    _ARRAY_OF_OBJECTS,
        'provenance':            _OBJECT_BLOCK,
    },
}

LAYER3_SCHEMA = {
    'type': 'object',
    'properties': {
        'portfolio_kpis_periodic': _ARRAY_OF_OBJECTS,
        'monthly_pl_rows':         _ARRAY_OF_OBJECTS,
        'monthly_bs_rows':         _ARRAY_OF_OBJECTS,
        'monthly_cf_rows':         _ARRAY_OF_OBJECTS,
        'budget_vs_actual':        _ARRAY_OF_OBJECTS,
        'burn_runway':             _ARRAY_OF_OBJECTS,
        'sheet_completeness':      _ARRAY_OF_OBJECTS,
        'provenance':              _OBJECT_BLOCK,
    },
}


SCHEMA_FOR_LAYER = {
    'L1': LAYER1_SCHEMA,
    'L2': LAYER2_SCHEMA,
    'L3': LAYER3_SCHEMA,
}


def schema_for(layer: str):
    if not USE_RESPONSE_SCHEMA:
        return None
    return SCHEMA_FOR_LAYER.get(layer)
