"""Phase 2a — the classifier routing tail, proven OFFLINE with a fake classifier.

The structural router is authoritative for anything it recognises; the model is
consulted ONLY to rescue a file it returns 'unknown', and even then only to PROMOTE
a real company MIS the code missed. Everything here is fail-closed: a classifier
error, a non-MIS class, or no provider all keep the safe 'unknown' hold. Confidence
never enters the decision (build rule #4) — only the discrete file_class does.
"""
import os
import tempfile
import types

import openpyxl
import pytest

from backend.dataimport.preingest3 import llm, pipeline
from backend.dataimport.preingest3.profiler import profile_file


def _classify_provider(file_class, entity='FooCo'):
    payload = (f'{{"file_class":"{file_class}","subtype":"x","entity_name":"{entity}",'
               f'"confidence":0.92,"reason":"t"}}')

    def provider(prompt, **kw):
        return types.SimpleNamespace(text=payload)
    return provider


def _raise_503(prompt, **kw):
    class ServerError(Exception):
        code = 503
    raise ServerError('upstream')


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    saved = llm._MODEL_PROVIDER
    monkeypatch.setattr(llm, '_rl_sleep', lambda *a, **k: None)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path / 'calls'))
    llm.new_metrics()
    yield
    llm._MODEL_PROVIDER = saved


def _unknown_file(d):
    # a cover/notes sheet — NO period axis (not 'mis'), NO numeric investment
    # schedule (not 'fund') → the structural router returns 'unknown'. This is the
    # tail the classifier exists to adjudicate.
    p = os.path.join(d, 'oddball.xlsx')
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in [['Monthly Business Review'], ['Confidential — Internal use only'],
              ['Region', 'APAC'], ['Prepared by', 'Finance']]:
        ws.append(r)
    wb.save(p)
    return p


def test_precondition_file_is_structurally_unknown():
    with tempfile.TemporaryDirectory() as d:
        prof = profile_file('oddball', _unknown_file(d))
        assert pipeline._route_structural(prof) == 'unknown'


def test_unknown_promoted_to_mis_when_classifier_says_mis():
    with tempfile.TemporaryDirectory() as d:
        path = _unknown_file(d)
        prof = profile_file('oddball', path)
        llm.set_model_provider(_classify_provider('mis'))
        assert pipeline._route_file(prof, label='oddball', path=path, use_model=True) == 'mis'


def test_unknown_stays_unknown_when_classifier_says_fund():
    # a disagreement toward fund-level is NOT promoted to a company MIS — held.
    with tempfile.TemporaryDirectory() as d:
        path = _unknown_file(d)
        prof = profile_file('oddball', path)
        llm.set_model_provider(_classify_provider('fund'))
        assert pipeline._route_file(prof, label='oddball', path=path, use_model=True) == 'unknown'


def test_unknown_stays_unknown_on_classifier_transport_error():
    # a 5xx during classify must NOT fail open — the file stays a safe 'unknown' hold.
    with tempfile.TemporaryDirectory() as d:
        path = _unknown_file(d)
        prof = profile_file('oddball', path)
        llm.set_model_provider(_raise_503)
        assert pipeline._route_file(prof, label='oddball', path=path, use_model=True) == 'unknown'


def test_no_model_flag_returns_structural_without_calling():
    def _boom(prompt, **kw):
        raise AssertionError('classifier must not be called when use_model=False')
    with tempfile.TemporaryDirectory() as d:
        path = _unknown_file(d)
        prof = profile_file('oddball', path)
        llm.set_model_provider(_boom)
        assert pipeline._route_file(prof, label='oddball', path=path, use_model=False) == 'unknown'
        assert llm.current_metrics().calls == 0


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
