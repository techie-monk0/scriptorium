"""Generate the derived palette consumers from the single source (`palette.json`).

`tokens.css` (the CSS custom properties the web UI + PWA read) and the PWA `manifest.webmanifest`
chrome colours are GENERATED here — do not hand-edit them; change a colour in `palette.json` and run
`python -m catalogue.webui.theme.gen` (or let `tests/test_palette_master.py` tell you they drifted).

`render_tokens_css` / `render_manifest` are pure (master dict → string) so the test can assert the
committed files equal a fresh render — the losslessness guard the display_environment_plan calls for.
"""
from __future__ import annotations

import json
import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
PALETTE_PATH = _HERE / "palette.json"
_STATIC = _HERE.parent / "static"
TOKENS_CSS_PATH = _STATIC / "css" / "tokens.css"
MANIFEST_PATH = _STATIC / "pwa" / "manifest.webmanifest"
# In-book reader themes (White/Sepia/Gray/Night) — a second master section (`reading_themes`), emitted
# to a reader-local CSS file (web/PWA) and a Swift port (iOS), kept out of the app palette above.
READER_THEMES_CSS_PATH = _STATIC / "reader" / "reader-themes.css"
# The native iOS client (`catalogue-app`) consumes the SAME master — gen emits a Swift port so the
# drift test keeps web + iOS in lockstep from one source (ios_native_plan.md §3). Repo root is 4
# parents above the theme dir (theme → webui → catalogue → src → catalogue-webui → <root>).
_REPO_ROOT = _HERE.parents[4]
PALETTE_SWIFT_PATH = (
    _REPO_ROOT / "catalogue-app" / "ios" / "CatalogueApp-Pkg" / "Sources" / "CatalogueDesign"
    / "Generated" / "Palette.swift"
)
READING_PALETTE_SWIFT_PATH = (
    _REPO_ROOT / "catalogue-app" / "ios" / "CatalogueApp-Pkg" / "Sources" / "CatalogueDesign"
    / "Generated" / "ReadingPalette.swift"
)

_GENERATED_BANNER = "GENERATED from theme/palette.json by `python -m catalogue.webui.theme.gen` — DO NOT edit by hand."


def _swift_case(token: str) -> str:
    """A palette token name → a Swift enum case name (`surface-2`→`surface2`, `nav-hover`→`navHover`)."""
    head, *tail = token.split("-")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def load_palette(path: pathlib.Path | None = None) -> dict:
    """The parsed master palette."""
    return json.loads((path or PALETTE_PATH).read_text(encoding="utf-8"))


def _order(master: dict) -> list[str]:
    """Token emit order — the explicit `token_order`, then any token not listed (alpha), so a token
    added to a palette but forgotten in token_order still emits (deterministically)."""
    explicit = master.get("token_order", [])
    seen = set(explicit)
    extra = sorted({t for p in master["palettes"].values() for t in p["colors"]} - seen)
    return explicit + extra


def _vars_block(colors: dict, order: list[str], scheme: str, indent: str) -> str:
    lines = [f"{indent}color-scheme: {scheme};"]
    lines += [f"{indent}--{name}: {colors[name]};" for name in order if name in colors]
    return "\n".join(lines)


def render_tokens_css(master: dict) -> str:
    """The full `tokens.css`: `:root` = the default palette, an OS-dark media block (only when no
    explicit theme is set), then one `:root[data-theme="<name>"]` block per palette so the runtime
    toggle can switch to ANY of them. Behaviour for light/dark is identical to the old hand-written
    file; extra palettes drop in for free."""
    order = _order(master)
    palettes = master["palettes"]
    default = master["default"]
    dft = palettes[default]

    out = [
        f"/* {_GENERATED_BANNER}",
        " * The SINGLE source of truth for the colour palette, shared by the web view (_base.html links",
        " * this) and the PWA (app.html links this). Add/rename a theme in palette.json, not here.",
        f" * Default theme is '{default}'; an explicit <html data-theme=\"…\"> overrides the OS preference.",
        " * A native client should encode the SAME values (palette.json is the language-neutral master).",
        " * (Reader-chrome tokens like --bar/--reader-bg are intentionally reader-local, not here.) */",
        ":root {",
        _vars_block(dft["colors"], order, dft.get("scheme", "light"), "  "),
        "}",
    ]

    # OS dark — only when the user hasn't pinned a theme (an explicit data-theme always wins below).
    if "dark" in palettes:
        d = palettes["dark"]
        out += [
            "@media (prefers-color-scheme: dark) {",
            "  :root:not([data-theme]) {",
            _vars_block(d["colors"], order, d.get("scheme", "dark"), "    "),
            "  }",
            "}",
        ]

    # One explicit block per palette → `data-theme="<name>"` selects it deterministically.
    for name, p in palettes.items():
        out += [
            f':root[data-theme="{name}"] {{',
            _vars_block(p["colors"], order, p.get("scheme", "light"), "  "),
            "}",
        ]
    return "\n".join(out) + "\n"


def _reading_order(master: dict) -> list[str]:
    """Reading-theme token emit order — explicit `reading_token_order`, then any extra (alpha)."""
    explicit = master.get("reading_token_order", [])
    seen = set(explicit)
    extra = sorted({t for p in master.get("reading_themes", {}).values() for t in p["colors"]} - seen)
    return explicit + extra


def render_reader_theme_css(master: dict) -> str:
    """The generated `reader-themes.css`: one `[data-reader-theme="<name>"]` block per reading theme
    (White/Sepia/Gray/Night) setting the reader-local vars (`--reader-bg`, `--reader-fg`, chrome, accent)
    + `color-scheme`. Namespaced on `data-reader-theme` so it never collides with the app's
    `html[data-theme]`. Pure (master → string) for the drift guard."""
    themes = master.get("reading_themes", {})
    if not themes:
        return ""
    order = _reading_order(master)
    out = [
        f"/* {_GENERATED_BANNER}",
        " * In-book reader themes (Apple Books style: White/Sepia/Gray/Night). Selected at runtime with",
        " * <body data-reader-theme=\"…\">; kept separate from the app palette (html[data-theme]).",
        " * Change values in palette.json `reading_themes` and regenerate — do not hand-edit. */",
    ]
    for name, p in themes.items():
        out += [
            f'[data-reader-theme="{name}"] {{',
            _vars_block(p["colors"], order, p.get("scheme", "light"), "  "),
            "}",
        ]
    return "\n".join(out) + "\n"


def render_reading_palette_swift(master: dict) -> str:
    """The `ReadingPalette.swift` port: a `ReadingTheme` enum (one case per reading theme), a
    `ReadingToken` enum (raw value = the web token name), and `ReadingPalette.hex[ReadingTheme][Token]`.
    Mirrors `render_palette_swift` so the same drift test keeps web + iOS reading themes in lockstep."""
    themes = master.get("reading_themes", {})
    if not themes:
        return ""
    order = _reading_order(master)
    default_name = next(iter(themes))

    out = [
        f"// {_GENERATED_BANNER}",
        "// In-book reader themes (White/Sepia/Gray/Night); master is theme/palette.json `reading_themes`.",
        "// Regenerate with `python -m catalogue.webui.theme.gen`; tests/test_palette_master.py guards drift.",
        "import SwiftUI",
        "",
        "/// A selectable in-book reading theme (one per `reading_themes` block in the master).",
        "public enum ReadingTheme: String, CaseIterable, Sendable {",
    ]
    out += [f"    case {name}" for name in themes]
    out += [
        "",
        f"    public static let `default`: ReadingTheme = .{default_name}",
        "",
        "    /// The OS colour scheme this reading theme paints as (drives PDF night-invert).",
        "    public var colorScheme: ColorScheme {",
        "        switch self {",
    ]
    for name, p in themes.items():
        scheme = "dark" if p.get("scheme", "light") == "dark" else "light"
        out.append(f"        case .{name}: return .{scheme}")
    out += [
        "        }",
        "    }",
        "",
        "    public var isDark: Bool { colorScheme == .dark }",
        "}",
        "",
        "/// Every reading-theme token (raw value = the canonical token name shared with reader-themes.css).",
        "public enum ReadingToken: String, CaseIterable, Sendable {",
    ]
    for token in order:
        case = _swift_case(token)
        out.append(f'    case {case} = "{token}"' if case != token else f"    case {case}")
    out += [
        "}",
        "",
        "public enum ReadingPalette {",
        "    /// token → hex (\"#rrggbb\"), per reading theme — ported 1:1 from the master.",
        "    public static let hex: [ReadingTheme: [ReadingToken: String]] = [",
    ]
    for name, p in themes.items():
        colors = p["colors"]
        out.append(f"        .{name}: [")
        for token in order:
            if token in colors:
                out.append(f'            .{_swift_case(token)}: "{colors[token]}",')
        out.append("        ],")
    out += [
        "    ]",
        "}",
        "",
    ]
    return "\n".join(out)


def render_manifest(master: dict, current: dict) -> dict:
    """`current` manifest dict with ONLY the chrome colours overwritten from the chosen
    `manifest_palette` (a static install can't theme-switch, so it picks one palette). Everything else
    — name/icons/display/… — is preserved verbatim."""
    pal = master["palettes"][master["manifest_palette"]]["colors"]
    out = dict(current)
    for manifest_key, token in master["manifest_map"].items():
        out[manifest_key] = pal[token]
    return out


def render_palette_swift(master: dict) -> str:
    """The `Palette.swift` port: a `Theme` enum (one case per palette), a `Token` enum (one case per
    colour token, raw value = the canonical web token name), and `Palette.hex[Theme][Token]` carrying
    the exact hex values. Pure (master → string) so the drift test can assert the committed file equals
    a fresh render — the same losslessness guard `tokens.css` already has, extended to iOS."""
    order = _order(master)
    palettes = master["palettes"]
    default = master["default"]

    out = [
        f"// {_GENERATED_BANNER}",
        "// The language-neutral master is theme/palette.json; change a colour there and regenerate",
        "// (`python -m catalogue.webui.theme.gen`). tests/test_palette_master.py asserts this file",
        "// equals a fresh render, so a hand-edit here (or a palette.json change without regen) fails CI.",
        "import SwiftUI",
        "",
        "/// A selectable colour theme (one per palette in the master). `auto` (follow OS) is a runtime",
        "/// preference, not a Theme; the renderer maps `auto` → the OS scheme before picking a Theme.",
        "public enum Theme: String, CaseIterable, Sendable {",
    ]
    out += [f"    case {name}" for name in palettes]
    out += [
        "",
        "    /// The default theme (matches palette.json `default`).",
        f"    public static let `default`: Theme = .{default}",
        "",
        "    /// The OS colour scheme this theme paints as.",
        "    public var colorScheme: ColorScheme {",
        "        switch self {",
    ]
    for name, p in palettes.items():
        scheme = "dark" if p.get("scheme", "light") == "dark" else "light"
        out.append(f"        case .{name}: return .{scheme}")
    out += [
        "        }",
        "    }",
        "}",
        "",
        "/// Every colour token (raw value = the canonical token name shared with the web `tokens.css`).",
        "public enum Token: String, CaseIterable, Sendable {",
    ]
    for token in order:
        case = _swift_case(token)
        out.append(f'    case {case} = "{token}"' if case != token else f"    case {case}")
    out += [
        "}",
        "",
        "public enum Palette {",
        "    /// token → hex (\"#rrggbb\"), per theme — the single source of colour truth, ported 1:1.",
        "    public static let hex: [Theme: [Token: String]] = [",
    ]
    for name, p in palettes.items():
        colors = p["colors"]
        out.append(f"        .{name}: [")
        for token in order:
            if token in colors:
                out.append(f'            .{_swift_case(token)}: "{colors[token]}",')
        out.append("        ],")
    out += [
        "    ]",
        "}",
        "",
    ]
    return "\n".join(out)


def write(master: dict | None = None) -> list[str]:
    """Write the generated consumers; return the paths that changed."""
    master = master or load_palette()
    changed = []

    css = render_tokens_css(master)
    if not TOKENS_CSS_PATH.exists() or TOKENS_CSS_PATH.read_text(encoding="utf-8") != css:
        TOKENS_CSS_PATH.write_text(css, encoding="utf-8")
        changed.append(str(TOKENS_CSS_PATH))

    current = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = render_manifest(master, current)
    rendered = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if MANIFEST_PATH.read_text(encoding="utf-8") != rendered:
        MANIFEST_PATH.write_text(rendered, encoding="utf-8")
        changed.append(str(MANIFEST_PATH))

    swift = render_palette_swift(master)
    if not PALETTE_SWIFT_PATH.exists() or PALETTE_SWIFT_PATH.read_text(encoding="utf-8") != swift:
        PALETTE_SWIFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        PALETTE_SWIFT_PATH.write_text(swift, encoding="utf-8")
        changed.append(str(PALETTE_SWIFT_PATH))

    # Reading themes (second master section) → reader CSS + Swift port.
    reader_css = render_reader_theme_css(master)
    if reader_css and (not READER_THEMES_CSS_PATH.exists()
                       or READER_THEMES_CSS_PATH.read_text(encoding="utf-8") != reader_css):
        READER_THEMES_CSS_PATH.parent.mkdir(parents=True, exist_ok=True)
        READER_THEMES_CSS_PATH.write_text(reader_css, encoding="utf-8")
        changed.append(str(READER_THEMES_CSS_PATH))

    reading_swift = render_reading_palette_swift(master)
    if reading_swift and (not READING_PALETTE_SWIFT_PATH.exists()
                          or READING_PALETTE_SWIFT_PATH.read_text(encoding="utf-8") != reading_swift):
        READING_PALETTE_SWIFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        READING_PALETTE_SWIFT_PATH.write_text(reading_swift, encoding="utf-8")
        changed.append(str(READING_PALETTE_SWIFT_PATH))
    return changed


def main() -> None:
    changed = write()
    if changed:
        print("regenerated from palette.json:")
        for p in changed:
            print(f"  {p}")
    else:
        print("up to date — nothing to regenerate.")


if __name__ == "__main__":
    main()
