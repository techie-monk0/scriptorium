"""Lightweight server performance tracing — gated, opt-in, near-zero cost when off.

Turn it on with the server's `--perflog` flag (or `CATALOGUE_PERFLOG=1`). Every line is prefixed
`[PERF]` and carries a wall-clock timestamp (matching werkzeug's access-log style so the two
interleave readably) plus, where relevant, an elapsed-ms figure — so you can see WHAT the server is
doing and WHERE the time goes (file resolve, the kDrive online-only xattr probe, the byte-range
read, a full-file cache copy, total request time, …).

When disabled, every entry point is a single boolean check and returns immediately, so leaving the
calls in the hot path costs nothing in production.

  from catalogue.services import perf
  perf.log("opening holding 543")                 # → [PERF] 16:35:47.812 opening holding 543
  with perf.span("resolve holding 543"):          # → [PERF] 16:35:47.910 +98ms resolve holding 543
      ...
  perf.log("range read", ms=1023.4, n=65536)      # → [PERF] 16:35:48.9 +1023ms range read (65536 B)
"""
from __future__ import annotations

import contextlib
import os
import sys
import time

_enabled = os.environ.get("CATALOGUE_PERFLOG", "").strip().lower() in ("1", "true", "yes", "on")


def enable(on: bool = True) -> None:
    """Turn perf tracing on/off at runtime (the `--perflog` flag calls this)."""
    global _enabled
    _enabled = bool(on)


def is_enabled() -> bool:
    return _enabled


def _stamp() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def log(msg: str, *, ms: "float | None" = None, n: "int | None" = None) -> None:
    """Emit one `[PERF]` line (no-op when disabled). `ms` adds an elapsed figure; `n` adds a byte
    count — handy for range reads."""
    if not _enabled:
        return
    parts = [f"[PERF] {_stamp()}"]
    if ms is not None:
        parts.append(f"+{ms:.0f}ms")
    parts.append(msg)
    if n is not None:
        parts.append(f"({n} B)")
    print(" ".join(parts), file=sys.stderr, flush=True)


@contextlib.contextmanager
def span(label: str):
    """Time a block and log its elapsed ms on exit (no-op when disabled)."""
    if not _enabled:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log(label, ms=(time.perf_counter() - t0) * 1000)


def timed(fn, label: str):
    """Run `fn()` and log its elapsed ms; returns fn()'s result. (For one-liners not worth a
    `with` block.)"""
    if not _enabled:
        return fn()
    t0 = time.perf_counter()
    try:
        return fn()
    finally:
        log(label, ms=(time.perf_counter() - t0) * 1000)
