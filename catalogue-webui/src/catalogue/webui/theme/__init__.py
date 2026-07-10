"""Display-theme package — the single-source palette + its code generators.

`palette.json` is the one place every display colour lives (named palettes, light/dark/…). `gen.py`
renders the derived consumers from it (the CSS variables in `static/css/tokens.css` and the PWA
manifest's chrome colours), so the numbers are defined once and switched by `<html data-theme="…">`.
"""
