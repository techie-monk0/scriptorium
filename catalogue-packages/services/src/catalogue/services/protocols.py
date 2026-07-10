"""Visibility *protocols* — the one place that decides whether a UI section is shown.

A *protocol* is a named capability gate: a section/menu-item declares a protocol, and the
protocol decides visibility from a runtime CONTEXT. Every section has a protocol; the
built-in ``default`` is always visible, so a section that declares nothing stays visible.

This is the canonical definition; it is **mirrored in the JS Tier-2 layer**
(``static/js/library-core.js`` → ``PROTOCOLS`` / ``protocolVisible``) so the web, PWA and a
native client all gate sections identically.

Context keys (populate the ones a side can know):
  ``local``   — the request is from the machine running the catalogue (the host).
  ``desktop`` — a desktop-class client (large screen / not a phone). The SERVER can't know a
                client's screen size, so server-rendered sections should gate on ``local`` /
                ``default``, never ``desktop`` (that one is for the client-rendered nav).
"""
from __future__ import annotations

from typing import Callable, Mapping

# name → predicate(context) -> visible?
PROTOCOLS: dict[str, Callable[[Mapping], bool]] = {
    "default": lambda ctx: True,
    "local": lambda ctx: bool(ctx.get("local")),
    "desktop": lambda ctx: bool(ctx.get("desktop")),
}

DEFAULT = "default"


def is_visible(protocol: str | None, ctx: Mapping) -> bool:
    """True if a section gated by ``protocol`` is visible in ``ctx``. Unknown/None → default."""
    return PROTOCOLS.get(protocol or DEFAULT, PROTOCOLS[DEFAULT])(ctx)
