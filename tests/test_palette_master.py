"""Losslessness guard for the generated display palette (display_environment_plan.md).

`catalogue-webui/.../theme/palette.json` is the single source of truth; `tokens.css` and the PWA
manifest's chrome colours are GENERATED from it by `catalogue.webui.theme.gen`. This test asserts the
COMMITTED files equal a fresh regeneration — so (a) palette.json really is the source (no hand-edit
drifted in), and (b) `python -m catalogue.webui.theme.gen` was re-run after any palette change. If it
fails: run the generator and commit the result (or, if you edited tokens.css/manifest by hand, move
the change into palette.json instead).
"""
from __future__ import annotations

import json

import pytest

from catalogue.webui.theme import gen

# The native-app Swift files aren't in this tree, so the Swift-drift checks skip.
_native_app_present = pytest.mark.skipif(
    not gen.PALETTE_SWIFT_PATH.exists(),
    reason="native-app palette not present in this tree",
)


def test_tokens_css_is_regenerated_from_palette():
    master = gen.load_palette()
    committed = gen.TOKENS_CSS_PATH.read_text(encoding="utf-8")
    assert committed == gen.render_tokens_css(master), (
        "static/css/tokens.css is stale vs palette.json — run "
        "`uv run python -m catalogue.webui.theme.gen` and commit, or move a hand-edit into palette.json."
    )


def test_manifest_chrome_is_regenerated_from_palette():
    master = gen.load_palette()
    committed = json.loads(gen.MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = gen.render_manifest(master, committed)   # only the chrome colours come from the master
    assert committed == expected, (
        "manifest.webmanifest chrome colours are stale vs palette.json — run the generator and commit."
    )


@_native_app_present
def test_palette_swift_is_regenerated_from_palette():
    """The native app reads the SAME master via a generated `Palette.swift`. Assert the
    committed Swift equals a fresh render — so a colour change in palette.json that wasn't followed by
    `python -m catalogue.webui.theme.gen` (leaving iOS stale) fails CI exactly like tokens.css does."""
    master = gen.load_palette()
    committed = gen.PALETTE_SWIFT_PATH.read_text(encoding="utf-8")
    assert committed == gen.render_palette_swift(master), (
        "The native Palette.swift is stale vs palette.json — run "
        "`python -m catalogue.webui.theme.gen` and commit."
    )


def test_reader_themes_css_is_regenerated_from_palette():
    """The in-book reader themes (palette.json `reading_themes`) generate static/reader/reader-themes.css.
    Assert the committed file equals a fresh render — same drift guard as tokens.css, for the reader."""
    master = gen.load_palette()
    committed = gen.READER_THEMES_CSS_PATH.read_text(encoding="utf-8")
    assert committed == gen.render_reader_theme_css(master), (
        "static/reader/reader-themes.css is stale vs palette.json `reading_themes` — run "
        "`python -m catalogue.webui.theme.gen` and commit."
    )


@_native_app_present
def test_reading_palette_swift_is_regenerated_from_palette():
    """The iOS reader reads the SAME `reading_themes` master via a generated ReadingPalette.swift."""
    master = gen.load_palette()
    committed = gen.READING_PALETTE_SWIFT_PATH.read_text(encoding="utf-8")
    assert committed == gen.render_reading_palette_swift(master), (
        "CatalogueDesign/Generated/ReadingPalette.swift is stale vs palette.json `reading_themes` — run "
        "`python -m catalogue.webui.theme.gen` and commit."
    )


def test_every_reading_theme_defines_the_same_token_set():
    """No reading theme may be missing a token another has (a missing reader var → half-themed frame)."""
    master = gen.load_palette()
    token_sets = {name: set(p["colors"]) for name, p in master["reading_themes"].items()}
    all_tokens = set().union(*token_sets.values())
    incomplete = {name: sorted(all_tokens - toks) for name, toks in token_sets.items()
                  if toks != all_tokens}
    assert not incomplete, f"reading themes missing tokens: {incomplete}"


def test_every_palette_defines_the_same_token_set():
    """No palette may be missing a token another palette has (a missing var would fall back to the
    default theme at runtime — a silent half-themed surface)."""
    master = gen.load_palette()
    token_sets = {name: set(p["colors"]) for name, p in master["palettes"].items()}
    all_tokens = set().union(*token_sets.values())
    incomplete = {name: sorted(all_tokens - toks) for name, toks in token_sets.items()
                  if toks != all_tokens}
    assert not incomplete, f"palettes missing tokens: {incomplete}"
