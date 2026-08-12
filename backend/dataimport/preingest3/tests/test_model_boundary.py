"""Model-boundary robustness guard (Phase 1) — the fix for the swallowed-404.

For weeks the model 'never ran' and no one knew, because a wrong model id (404) was
caught by the same blanket except that catches a transient timeout and returned as a
per-file HOLD — indistinguishable from a legitimate 'nothing here'. A config fault
wore the costume of a data answer. This suite freezes the cure: the boundary is now
THREE-valued and the classes are NOT interchangeable —

  • 404 / 401 / 403 / 400 / missing creds → ModelConfigError → HALTS the run loudly.
  • 429 → backs off (honours Retry-After) and retries; a persistent wall → ERROR (hold).
  • 5xx / timeout / garbled → ERROR (hold + retry the run), NEVER fatal, NEVER a blank.
  • genuine parseable answer → OK.

Plus a live-shaped health-check that fails loud on 404/auth before any file is
touched, and per-run metrics so 'the model ran / didn't' is measured, not assumed.
All exercised with injected fake providers — no live Gemini, deterministic in CI.
"""
import pytest

from backend.dataimport.preingest3 import llm, pipeline


# ── fake transports: each reproduces exactly one boundary outcome ──────────
class _ApiExc(Exception):
    def __init__(self, code):
        super().__init__(f'HTTP {code}')
        self.code = code          # google.genai APIError carries .code (int status)


def _raises(code):
    def provider(prompt, **kw):
        raise _ApiExc(code)
    return provider


def _raises_exc(exc):
    def provider(prompt, **kw):
        raise exc
    return provider


def _timeout_provider(prompt, **kw):
    class ReadTimeout(Exception):     # no .code — transport timeout shape
        pass
    raise ReadTimeout('inter-chunk idle > 60s')


class _Resp:
    def __init__(self, text):
        self.text = text


def _ok_provider(prompt, **kw):
    return _Resp('{"ok": true}')


def _garbled_provider(prompt, **kw):
    return _Resp('sorry, I could not do that')  # arrived but unparseable → ERROR


@pytest.fixture(autouse=True)
def _isolate_boundary(monkeypatch):
    """Snapshot/restore the injected provider and give each test fresh metrics and a
    no-op backoff so 429 retries don't actually sleep."""
    saved = llm._MODEL_PROVIDER
    monkeypatch.setattr(llm, '_rl_sleep', lambda *a, **k: None)
    llm.new_metrics()
    yield
    llm._MODEL_PROVIDER = saved


def _call():
    return llm.call_json('probe', 'sig', 'prompt', use_cache=False)


# ── FATAL: config/auth faults halt, never hold ────────────────────────────
@pytest.mark.parametrize('code', [404, 403, 401, 400])
def test_config_and_auth_faults_raise_not_hold(code):
    llm.set_model_provider(_raises(code))
    with pytest.raises(llm.ModelConfigError):
        _call()
    assert llm.current_metrics().fatal == 1        # counted as fatal, not error/hold


def test_missing_credentials_is_fatal():
    # get_client() raises ValueError when no api-key/project — a deployment fault
    # identical for every file, so it must halt, not become 15 silent holds.
    llm.set_model_provider(_raises_exc(ValueError('GOOGLE_API_KEY not set')))
    with pytest.raises(llm.ModelConfigError):
        _call()


# ── TRANSIENT: 5xx / timeout hold the file, never halt, never blank ───────
def test_timeout_is_error_not_fatal():
    llm.set_model_provider(_timeout_provider)
    r = _call()
    assert r.is_error and r.kind == llm.ERROR      # a hold, not a crash
    assert r.data is None                           # never a readable blank
    assert llm.current_metrics().error == 1 and llm.current_metrics().fatal == 0


def test_server_5xx_is_error_not_fatal():
    llm.set_model_provider(_raises(503))
    r = _call()
    assert r.is_error and llm.current_metrics().fatal == 0


def test_garbled_reply_is_error():
    llm.set_model_provider(_garbled_provider)
    r = _call()
    assert r.is_error and 'garbled' in r.reason


# ── RATE-LIMIT: 429 backs off then holds; distinct from fatal ─────────────
def test_rate_limit_retries_then_holds(monkeypatch):
    monkeypatch.setattr(llm, '_RL_ATTEMPTS', 3)
    llm.set_model_provider(_raises(429))
    r = _call()
    assert r.is_error                               # persistent wall → hold, not crash
    m = llm.current_metrics()
    assert m.calls == 3 and m.retries == 2 and m.error == 1


def test_rate_limit_then_success(monkeypatch):
    monkeypatch.setattr(llm, '_RL_ATTEMPTS', 4)
    state = {'n': 0}

    def flaky(prompt, **kw):
        state['n'] += 1
        if state['n'] < 2:
            raise _ApiExc(429)
        return _Resp('{"located": "P&L!D20"}')

    llm.set_model_provider(flaky)
    r = llm.call_json('probe', 'sig', 'p', use_cache=False)
    assert r.is_ok and r.data == {'located': 'P&L!D20'}
    assert llm.current_metrics().ok == 1 and llm.current_metrics().retries == 1


def test_retry_after_header_is_honoured(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, '_RL_ATTEMPTS', 2)
    monkeypatch.setattr(llm, '_rl_sleep',
                        lambda attempt, ra: captured.__setitem__('ra', ra))

    class _Resp429(_ApiExc):
        def __init__(self):
            super().__init__(429)
            self.response = type('R', (), {'headers': {'Retry-After': '7'}})()

    llm.set_model_provider(_raises_exc(_Resp429()))
    _call()
    assert captured.get('ra') == 7.0                # server's Retry-After reached backoff


# ── OK + metrics ──────────────────────────────────────────────────────────
def test_ok_call_records_metrics():
    llm.set_model_provider(_ok_provider)
    r = _call()
    assert r.is_ok and r.data == {'ok': True}
    m = llm.current_metrics()
    assert m.calls == 1 and m.ok == 1 and m.error == 0 and m.fatal == 0
    assert m.latency_s >= 0.0
    assert m.summary()['ok'] == 1                   # metrics are visible/serialisable


# ── HEALTH-CHECK: the door that a broken boundary can't get past ──────────
def test_health_check_ok():
    llm.set_model_provider(_ok_provider)
    h = llm.health_check()
    assert h.ok and h.model == llm.PREINGEST_MODEL


def test_health_check_fatal_raises_loud():
    llm.set_model_provider(_raises(404))
    with pytest.raises(llm.ModelConfigError):
        llm.health_check()


def test_health_check_transient_is_degraded_not_fatal():
    llm.set_model_provider(_timeout_provider)
    h = llm.health_check()
    assert h.ok is False and 'transient' in h.detail   # degraded, caller may proceed


def test_health_check_no_provider_raises(monkeypatch):
    monkeypatch.setattr(llm, '_resolve_provider', lambda: None)
    with pytest.raises(llm.ModelConfigError):
        llm.health_check()


# ── the DONE-criterion: a wrong model id halts the whole run loudly ───────
def test_run_halts_loudly_on_bad_model_when_require_model():
    with pytest.raises(llm.ModelConfigError):
        pipeline.run([], as_of='2026-06-30', org='t',
                     model_provider=_raises(404), require_model=True)


def test_run_passes_health_check_and_reports_metrics_when_healthy():
    r = pipeline.run([], as_of='2026-06-30', org='t',
                     model_provider=_ok_provider, require_model=True)
    assert r.model_health and r.model_health['ok'] is True
    assert r.model_metrics is not None and 'calls' in r.model_metrics


def test_deterministic_run_reports_zero_calls_not_absent():
    # a purely deterministic run (no model wired in the path yet) must HONESTLY
    # report calls=0 — the metric that would have exposed 'the model never ran'.
    r = pipeline.run([], as_of='2026-06-30', org='t')
    assert r.model_metrics == {'calls': 0, 'ok': 0, 'error': 0, 'fatal': 0,
                               'retries': 0, 'latency_s': 0.0, 'by_status': {}}


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q']))
