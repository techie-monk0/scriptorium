"""Feature flags — gate experimental/low-quality surfaces behind config.

Config-driven via `vocab.json` `_features`: a flat `{name: bool}` map. A flag that
is absent defaults to OFF, so a new experimental surface stays hidden until it's
explicitly turned on:

    "_features": { "multi_work_detection": false }

Read everywhere a gated surface is exposed — nav link, web route, CLI entry — so the
single config switch hides the whole feature at once.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _flags() -> dict:
    try:
        from catalogue.db_store.db import VOCAB_PATH
        return dict(json.loads(VOCAB_PATH.read_text("utf-8")).get("_features") or {})
    except Exception:
        return {}


def _env_flags() -> set:
    """`CATALOGUE_FEATURES` — a comma-separated list of flags forced ON (overrides the
    config). Lets tests / a one-off session enable a gated surface without editing the
    file. Read live (not cached) so a monkeypatched env takes effect immediately."""
    return {f.strip() for f in (os.environ.get("CATALOGUE_FEATURES") or "").split(",") if f.strip()}


def feature_enabled(name: str, default: bool = False) -> bool:
    """Whether feature `name` is on: `CATALOGUE_FEATURES` env override, else vocab.json
    `_features`, else `default` (OFF)."""
    if name in _env_flags():
        return True
    return bool(_flags().get(name, default))


@lru_cache(maxsize=1)
def library_root() -> str:
    """The on-disk root of the library tree (vocab.json `_library_root`), used to show
    file paths as a short subtree instead of a long absolute path."""
    try:
        from catalogue.db_store.db import VOCAB_PATH
        return str(json.loads(VOCAB_PATH.read_text("utf-8")).get("_library_root") or "")
    except Exception:
        return ""


def rel_path(p) -> str:
    """`p` shown relative to its owning library root — everything below that root,
    slash-trimmed. With multiple roots, the root that actually contains `p` is used
    (longest-prefix). Falls back to a substring match against the primary root, then
    to `p` unchanged if no root contains it."""
    if not p:
        return ""
    from .mount import owning_root
    r = owning_root(str(p))
    if r:                                            # strip the owning root's prefix
        return str(p).replace("\\", "/")[len(r.path):].lstrip("/")
    root = library_root().strip("/")                 # legacy substring fallback
    if root:
        idx = str(p).find(root)
        if idx != -1:
            return str(p)[idx + len(root):].lstrip("/")
    return str(p)


def reload() -> None:
    """Drop the cached config (after editing it, e.g. in tests)."""
    _flags.cache_clear()
    library_root.cache_clear()
