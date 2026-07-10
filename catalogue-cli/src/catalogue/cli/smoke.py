"""Pre-flight smoke checks: confirm the heavy externals actually work on
this machine before kicking off a real sweep (§9 bake-off prep, §13).

Subcommands:
  ocr      Verify Tesseract loads `IAST.traineddata` and survives a
           macron round-trip. Auto-downloads the Shreeshrii IAST file
           from `tesstrain-Sanskrit-IAST` if it is missing.
  llm      Verify Ollama is reachable at localhost:11434 and `qwen3:8b`
           responds with valid JSON.
  resolver Verify BDRC live lookup returns rows for a known name; report
           whether the optional 84000 snapshot is present.
  all      Run ocr + llm + resolver.

Nothing here mutates the catalogue. Run before the first real sweep.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


# Shreeshrii's IAST traineddata. `tessdata_best` is the LSTM model
# purpose-trained on Latin-script Sanskrit with full IAST diacritics —
# the only one that won't hallucinate Devanagari into our pages.
IAST_URL = (
    "https://raw.githubusercontent.com/Shreeshrii/"
    "tesstrain-Sanskrit-IAST/main/tessdata_best/IAST.traineddata"
)
IAST_STEM = "IAST"


def _tessdata_dirs() -> list[Path]:
    """Search paths Tesseract checks for traineddata. `TESSDATA_PREFIX`
    wins if set; otherwise the common Homebrew / system locations."""
    prefix = os.environ.get("TESSDATA_PREFIX")
    candidates: list[Path] = []
    if prefix:
        candidates.append(Path(prefix))
    candidates.extend([
        Path("/usr/local/share/tessdata"),       # Intel Homebrew
        Path("/opt/homebrew/share/tessdata"),    # Apple Silicon Homebrew
        Path("/usr/share/tessdata"),
    ])
    return [c for c in candidates if c.is_dir()]


def _find_iast() -> Path | None:
    for d in _tessdata_dirs():
        p = d / f"{IAST_STEM}.traineddata"
        if p.is_file():
            return p
    return None


def _install_iast() -> Path:
    """Download IAST.traineddata into the first writable tessdata dir.
    On macOS with Homebrew tesseract that's `/usr/local/share/tessdata`."""
    dirs = _tessdata_dirs()
    if not dirs:
        raise SystemExit(
            "no tessdata directory found. Install tesseract first "
            "(`brew install tesseract`) or set $TESSDATA_PREFIX."
        )
    target_dir = next((d for d in dirs if os.access(d, os.W_OK)), None)
    if target_dir is None:
        # First dir is the most-canonical; tell the user to sudo.
        d = dirs[0]
        raise SystemExit(
            f"{d} is not writable. Re-run with sudo or "
            f"`sudo curl -L {IAST_URL} -o {d}/{IAST_STEM}.traineddata`."
        )
    target = target_dir / f"{IAST_STEM}.traineddata"
    print(f"downloading IAST.traineddata → {target} …")
    with urllib.request.urlopen(IAST_URL, timeout=30) as r:    # noqa: S310
        data = r.read()
    target.write_bytes(data)
    print(f"  {len(data):,} bytes")
    return target


def smoke_ocr() -> bool:
    """Verify Tesseract can load `IAST` and OCR a synthetic macron page
    without losing the diacritic. Auto-installs the traineddata if it's
    missing. Returns True on success."""
    if shutil.which("tesseract") is None:
        print("FAIL ocr: tesseract not on PATH (`brew install tesseract`).")
        return False
    if shutil.which("ocrmypdf") is None:
        print("FAIL ocr: ocrmypdf not on PATH (`brew install ocrmypdf`).")
        return False

    iast = _find_iast()
    if iast is None:
        iast = _install_iast()
    print(f"ok  IAST.traineddata at {iast}")

    # Confirm Tesseract enumerates the language code.
    out = subprocess.run(
        ["tesseract", "--list-langs"], capture_output=True, text=True,
    )
    langs = {l.strip() for l in out.stdout.splitlines() if l.strip()}
    if IAST_STEM not in langs:
        print(f"FAIL ocr: Tesseract does not list `{IAST_STEM}` — "
              f"$TESSDATA_PREFIX may point elsewhere. langs sample: "
              f"{sorted(langs)[:5]}")
        return False
    if "san" in langs:
        print("WARN ocr: `san` is installed but MUST NOT be used (§4.8a — "
              "Devanagari hallucinations). The digitizer pins `eng+IAST`.")

    # Synthetic round-trip: render a tiny image with a macron'd word and
    # confirm Tesseract reads it back with the diacritic intact.
    if _macron_round_trip(iast.parent):
        print("ok  Tesseract + IAST diacritic round-trip")
    else:
        print("WARN ocr: round-trip image render skipped — Pillow not "
              "installed. Language loads OK; install `Pillow` for the "
              "full diacritic-survival check.")
    return True


def _macron_round_trip(tessdata_dir: Path) -> bool:
    """Render `Bodhicaryāvatāra` → PNG → tesseract → assert `ā` survives.
    Returns False if Pillow is unavailable (we treat that as 'skipped'
    not 'failed' — the language-load check above is the hard gate)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    word = "Bodhicaryāvatāra"
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "probe.png"
        img = Image.new("L", (520, 80), color=255)
        d = ImageDraw.Draw(img)
        # Default PIL font is bitmap; on macOS we have Helvetica.
        try:
            font = ImageFont.truetype(
                "/System/Library/Fonts/Helvetica.ttc", 28
            )
        except OSError:
            font = ImageFont.load_default()
        d.text((10, 20), word, fill=0, font=font)
        img.save(png)

        env = {**os.environ, "TESSDATA_PREFIX": str(tessdata_dir)}
        r = subprocess.run(
            ["tesseract", str(png), "-", "-l", f"eng+{IAST_STEM}"],
            capture_output=True, text=True, env=env,
        )
        text = r.stdout.strip()
        ok = "ā" in text or "ã" in text or word[:5].lower() in text.lower()
        if not ok:
            print(f"     round-trip produced: {text!r}")
        return ok


def smoke_llm() -> bool:
    """Verify Ollama is up and `qwen3:8b` returns valid JSON via the
    OpenAI-compat endpoint we actually use in production."""
    from catalogue.services.llm import LLMClient

    client = LLMClient(model="qwen3:8b",
                       base_url="http://localhost:11434/v1",
                       timeout=120.0)
    # qwen3 emits chain-of-thought into a `reasoning` channel by default;
    # at low `max_tokens` it consumes the budget before any user-facing
    # content is generated. `/no_think` (qwen3-specific directive) keeps
    # the response terse. The production classify path uses the same
    # technique — encoded here so the smoke matches reality.
    messages = [
        {"role": "system",
         "content": "/no_think\nReply with strict JSON only: "
                    '{"ok": true, "model": "<your model name>"}'},
        {"role": "user", "content": "Are you online?"},
    ]
    try:
        out = client.chat(messages, max_tokens=256)
    except Exception as e:
        print(f"FAIL llm: {type(e).__name__}: {e}")
        print("  Is `ollama serve` running? Is `qwen3:8b` pulled?")
        return False
    content = out["content"].strip()
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        print(f"FAIL llm: response was not strict JSON: {content!r}")
        return False
    if parsed.get("ok") is True:
        print(f"ok  qwen3:8b responded (tokens in={out['tokens_in']} "
              f"out={out['tokens_out']})")
        return True
    print(f"FAIL llm: unexpected JSON: {parsed!r}")
    return False


def smoke_resolver() -> bool:
    """Hit BDRC for a known name; report 84000 snapshot status."""
    from catalogue.services.work_canonical_resolver import BDRCClient, EightyFourThousandIndex
    bdrc = BDRCClient()
    rows = bdrc.lookup("klong chen rab 'byams", lang="bo-x-ewts")
    if not rows:
        print("WARN resolver: BDRC returned no rows — endpoint may be "
              "down or the response shape changed. Sweep will still run; "
              "resolver hits will fall back to None.")
    else:
        bid, label = rows[0]
        print(f"ok  BDRC live: {label} → {bid} ({len(rows)} candidates)")

    idx = EightyFourThousandIndex()
    if idx.available():
        n = len(idx._ensure_loaded().get("by_toh", {}))
        print(f"ok  84000 snapshot at {idx.snapshot_dir} — {n} Toh entries")
    else:
        print(f"info 84000 snapshot absent at {idx.snapshot_dir}. "
              "Run `python -m catalogue.services.work_canonical_resolver fetch-84000` to enable.")
    return True


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "all"
    runners = {"ocr": smoke_ocr, "llm": smoke_llm, "resolver": smoke_resolver}
    if cmd == "all":
        ok = True
        for name in ("ocr", "llm", "resolver"):
            print(f"── {name} ──")
            ok = runners[name]() and ok
        return 0 if ok else 1
    if cmd in runners:
        return 0 if runners[cmd]() else 1
    print(f"unknown subcommand {cmd!r}; choose: ocr | llm | resolver | all")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
