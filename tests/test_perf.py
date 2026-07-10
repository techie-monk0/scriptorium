"""`catalogue.services.perf` — the gated [PERF] server tracing, plus that the file route actually
emits the diagnostics (range, resolve, byte-range read, per-request total) when enabled."""
from __future__ import annotations

from catalogue.services import perf


def test_disabled_is_silent(capsys):
    perf.enable(False)
    perf.log("nope", ms=5, n=10)
    with perf.span("nada"):
        pass
    assert perf.timed(lambda: 7, "calc") == 7
    assert capsys.readouterr().err == ""           # nothing emitted when off


def test_enabled_emits_prefixed_lines(capsys):
    perf.enable(True)
    try:
        perf.log("hello", ms=12.3, n=4096)
        with perf.span("work"):
            pass
        assert perf.timed(lambda: 42, "calc") == 42
    finally:
        perf.enable(False)
    err = capsys.readouterr().err
    assert "[PERF]" in err
    assert "hello" in err and "4096 B" in err      # byte-count annotation
    assert "+" in err and "ms" in err              # elapsed annotation
    assert "work" in err and "calc" in err
    # (the file route emitting these diagnostics is covered in tests/system/test_reader.py)
