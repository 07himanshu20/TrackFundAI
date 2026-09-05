"""gemini_service HttpOptions timeout — the SDK-version-agnostic timeout shaping (root-cause fix for
the staging failure `HttpOptions timeout Input should be a valid integer ... input_value=(10.0, 240.0)`).

ROOT CAUSE: HttpOptions is importable from `_api_client` in BOTH the old requests-based SDK (timeout =
SECONDS, a float or a (connect, read) tuple) and a newer httpx-based SDK (timeout = a MILLISECOND int).
The old code inferred the wire-format from the IMPORT LOCATION, so on staging (newer SDK, still exposing
`_api_client.HttpOptions`) it built the seconds-tuple, and the new int-typed pydantic field rejected it —
every Gemini call died before it started.

FIX: decide the shape from the field's OWN declared type, not the import location. These tests prove the
choice is correct on BOTH SDK generations (simulated with fake pydantic models), that it NEVER feeds a
milliseconds integer to a seconds field, and — the reddening control — that the OLD tuple shape really is
rejected by the NEW int model (reproducing the staging error), so the shape choice is load-bearing."""
import os
import sys

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../backend
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault('TFAI_ENV', 'local')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

try:
    import django
    django.setup()
    import pydantic
    from typing import Optional, Tuple, Union
    from api import gemini_service as gs
    _OK = True
except Exception as exc:  # pragma: no cover — environment without django/pydantic on the path
    _OK = False
    _WHY = repr(exc)

pytestmark = pytest.mark.skipif(not _OK, reason='django/gemini_service not importable here')


if _OK:
    class _OldHttp(pydantic.BaseModel):     # requests-based SDK: seconds — float OR (connect, read) tuple
        timeout: Optional[Union[float, Tuple[float, float]]] = None

    class _NewHttp(pydantic.BaseModel):     # httpx-based SDK: milliseconds — a plain int
        timeout: Optional[int] = None

    class _WeirdHttp(pydantic.BaseModel):   # an unforeseen SDK the fix must not CRASH on
        timeout: Optional[str] = None

    OLD_ANN = _OldHttp.model_fields['timeout'].annotation
    NEW_ANN = _NewHttp.model_fields['timeout'].annotation


def test_old_sdk_seconds_tuple_is_chosen_and_accepted():
    shapes = gs._timeout_shapes(OLD_ANN, 10, 240)
    assert shapes[0] == (10.0, 240.0)                    # seconds (connect, read) tuple chosen first
    _OldHttp(timeout=shapes[0])                           # and the requests-based model accepts it


def test_new_sdk_milliseconds_int_is_chosen_and_accepted():
    shapes = gs._timeout_shapes(NEW_ANN, 10, 240)
    assert shapes[0] == 240000                            # 240s → 240000 ms chosen first
    assert isinstance(shapes[0], int)
    _NewHttp(timeout=shapes[0])                           # and the httpx-based int model accepts it


def test_reddening_old_tuple_shape_is_rejected_by_new_sdk_reproducing_staging():
    # EXACTLY the staging failure: a seconds-tuple handed to the new int-timeout field.
    with pytest.raises(pydantic.ValidationError):
        _NewHttp(timeout=(10.0, 240.0))
    # the FIX never produces that shape for the new SDK → the staging crash cannot recur.
    assert gs._timeout_shapes(NEW_ANN, 10, 240)[0] != (10.0, 240.0)


def test_never_feeds_milliseconds_to_a_seconds_field_the_240000s_trap():
    # a seconds field must never receive the ms integer first (240000 read as 240000 SECONDS = 66 hours).
    assert gs._timeout_shapes(OLD_ANN, 10, 240)[0] != 240000


def test_build_http_options_picks_an_accepted_shape_on_the_new_sdk(monkeypatch):
    monkeypatch.setattr(gs, '_HttpOptions', _NewHttp)
    monkeypatch.setattr(gs, '_TIMEOUT_ANNOTATION', NEW_ANN)
    monkeypatch.setattr(gs, '_TIMEOUT_HAS_FIELD', True)
    opt = gs._build_http_options(10, 240)
    assert isinstance(opt, _NewHttp) and opt.timeout == 240000     # the exact bug is fixed, no exception


def test_build_http_options_still_correct_on_the_old_sdk(monkeypatch):
    monkeypatch.setattr(gs, '_HttpOptions', _OldHttp)
    monkeypatch.setattr(gs, '_TIMEOUT_ANNOTATION', OLD_ANN)
    monkeypatch.setattr(gs, '_TIMEOUT_HAS_FIELD', True)
    opt = gs._build_http_options(10, 240)
    assert opt.timeout == (10.0, 240.0)                            # unchanged for requests-based SDKs


def test_build_http_options_never_crashes_on_an_unforeseen_field(monkeypatch):
    # every candidate shape rejected → still returns a client-buildable instance (no timeout), never
    # raises — an untimed call is bounded by the pipeline process-kill backstop; a crashed import is not.
    monkeypatch.setattr(gs, '_HttpOptions', _WeirdHttp)
    monkeypatch.setattr(gs, '_TIMEOUT_ANNOTATION', _WeirdHttp.model_fields['timeout'].annotation)
    monkeypatch.setattr(gs, '_TIMEOUT_HAS_FIELD', True)
    opt = gs._build_http_options(10, 240)
    assert isinstance(opt, _WeirdHttp) and opt.timeout is None


def test_installed_sdk_builds_without_error():
    # the REAL installed SDK (whatever version) must build an HttpOptions with no exception.
    opt = gs._build_http_options(10, 240)
    assert opt is not None
