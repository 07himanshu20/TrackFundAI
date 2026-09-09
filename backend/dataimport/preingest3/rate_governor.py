"""Client-side rate GOVERNOR — PREVENTS hitting the Vertex quota, rather than only reacting to a 429
after the fact (that reactive backoff still lives in llm.call_json as the backstop). A single global
token-bucket pair — requests/min (RPM) AND tokens/min (TPM) — that every model issue must acquire
from across ALL worker threads, so the batch's aggregate issue rate stays under the confirmed quota
no matter how many files/concepts run concurrently. This is the governed path B's adaptive-thinking
escalations will also flow through (escalation = extra calls → they must be governed too).

Design invariants:
  • DEFAULT OFF (no RPM/TPM configured) → acquire() is an instant True → behaviour byte-identical to
    before this module. Enable by setting the real quota once known (env or configure()).
  • FAIL-CLOSED: if capacity cannot be obtained within max_wait_s the call is NOT issued — acquire()
    returns False and the caller HOLDS (CallResult ERROR → held record). Never a wrong number, never
    a flood, never a crash.
  • Thread-safe: one lock/condition guards both buckets; shared on purpose (rate is a global resource,
    unlike the per-thread metrics/currency contextvars).
  • A single oversized call can never deadlock the TPM bucket — its demand is capped to bucket capacity.
"""
from __future__ import annotations

import os
import threading
import time

_OUTPUT_TOKEN_EST = int(os.environ.get('PREINGEST3_OUTPUT_TOKEN_EST', '1024'))  # conservative reply size


def est_tokens(prompt: str) -> int:
    """Conservative token estimate for TPM accounting: ~chars/4 for the prompt + a fixed reply budget.
    Over-estimating is the safe direction (governs slightly harder, never exceeds the real quota)."""
    return (len(prompt) // 4) + _OUTPUT_TOKEN_EST


class _Bucket:
    def __init__(self, per_min: float):
        self.capacity = float(per_min)
        self.tokens = float(per_min)
        self.rate_per_s = per_min / 60.0
        self.last = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate_per_s)
        self.last = now

    def time_until(self, n: float) -> float:
        self._refill()
        if self.tokens >= n:
            return 0.0
        return (n - self.tokens) / self.rate_per_s if self.rate_per_s > 0 else float('inf')

    def take(self, n: float):
        self._refill()
        self.tokens -= n


class RateGovernor:
    def __init__(self, rpm: int = None, tpm: int = None, max_wait_s: float = 120.0):
        self._cv = threading.Condition()
        self._req = _Bucket(rpm) if rpm else None
        self._tok = _Bucket(tpm) if tpm else None
        self.max_wait_s = max_wait_s
        self.acquired = 0
        self.holds = 0
        self.wait_s_total = 0.0

    @property
    def enabled(self) -> bool:
        return self._req is not None or self._tok is not None

    def acquire(self, tokens: int) -> bool:
        """Block until 1 request-token AND `tokens` token-tokens are available, then consume both and
        return True. If that cannot happen within max_wait_s, consume nothing and return False (HOLD)."""
        if not self.enabled:
            return True
        t_start = time.monotonic()
        deadline = t_start + self.max_wait_s
        with self._cv:
            while True:
                need_t = min(tokens, self._tok.capacity) if self._tok else 0
                r_wait = self._req.time_until(1) if self._req else 0.0
                t_wait = self._tok.time_until(need_t) if self._tok else 0.0
                if r_wait == 0.0 and t_wait == 0.0:
                    if self._req:
                        self._req.take(1)
                    if self._tok:
                        self._tok.take(need_t)
                    self.acquired += 1
                    self.wait_s_total += time.monotonic() - t_start
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.holds += 1
                    return False
                self._cv.wait(timeout=min(max(r_wait, t_wait) + 0.01, remaining))

    def stats(self) -> dict:
        return {'enabled': self.enabled, 'rpm': self._req.capacity if self._req else None,
                'tpm': self._tok.capacity if self._tok else None, 'acquired': self.acquired,
                'holds': self.holds, 'wait_s_total': round(self.wait_s_total, 2)}


def _from_env() -> RateGovernor:
    rpm = os.environ.get('PREINGEST3_RPM')
    tpm = os.environ.get('PREINGEST3_TPM')
    mw = os.environ.get('PREINGEST3_GOVERNOR_MAX_WAIT_S')
    return RateGovernor(rpm=int(rpm) if rpm else None, tpm=int(tpm) if tpm else None,
                        max_wait_s=float(mw) if mw else 120.0)


_GOVERNOR = _from_env()


def acquire(tokens: int) -> bool:
    return _GOVERNOR.acquire(tokens)


def configure(rpm: int = None, tpm: int = None, max_wait_s: float = 120.0) -> None:
    """Set/replace the global governor (call once at startup with the confirmed quota, or in tests)."""
    global _GOVERNOR
    _GOVERNOR = RateGovernor(rpm=rpm, tpm=tpm, max_wait_s=max_wait_s)


def current() -> RateGovernor:
    return _GOVERNOR


def stats() -> dict:
    return _GOVERNOR.stats()
