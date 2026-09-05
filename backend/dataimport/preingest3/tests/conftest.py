"""Shared test isolation for the preingest3 suite."""
import pytest

from backend.dataimport.preingest3 import templates


@pytest.fixture(autouse=True)
def _isolate_template_store(monkeypatch, tmp_path):
    """The Layout Template Registry (templates.py, cache A) is a GLOBAL json store on disk. Point it at a
    per-test temp file so a model-ON test can never pollute the real store or read another test's cached
    layout (determinism). Model-OFF tests never enter _model_fill, so they never touch the registry — this
    is a harmless no-op for them, including the whole byte-identical real-corpus spine."""
    monkeypatch.setattr(templates, '_STORE', str(tmp_path / 'preingest3_templates.json'))
