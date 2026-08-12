"""
Stage 2 — Classification (LLM, fingerprint-only input).

Emits a schema-validated routing object for the whole file:
  {file_class: 'fund'|'mis', subtype, entity_name, confidence}

Reuses the proven preingest.file_classifier (fund vs company_mis, judged from
content not filename) and shapes it to the document's contract. Records below
the confidence threshold τ are flagged for the review gate by the pipeline.
"""
from ..preingest.file_classifier import classify_file as _classify

CONFIDENCE_TAU = 0.55
_CONF = {'high': 0.9, 'low': 0.4}


def classify(fingerprints) -> dict:
    info = _classify(fingerprints)
    kind = info.get('kind')
    file_class = 'mis' if kind == 'company_mis' else 'fund'
    conf = _CONF.get(info.get('confidence', 'low'), 0.4)
    return {
        'file_class': file_class,
        'subtype': 'company_mis' if file_class == 'mis' else 'fund_workbook',
        'entity_name': info.get('company_name'),
        'confidence': conf,
        'needs_review': conf < CONFIDENCE_TAU,
    }
