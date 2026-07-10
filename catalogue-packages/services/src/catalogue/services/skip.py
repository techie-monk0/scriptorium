"""Exclusion rules — files/editions kept OUT of the catalogue.

Config-driven via `vocab.json` `_exclusions`: a list of `{field, op, value}` rules.
- `field` — `path` | `title` | `any` (default `any`)
- `op`    — `starts_with` | `ends_with` | `contains` | `under` (default `contains`)
- `value` — the substring/prefix/suffix, matched **case-insensitively**

`under` is path-segment containment: it matches a folder AND everything beneath
it (`/lib/Tantra` matches `/lib/Tantra/x.pdf` but NOT `/lib/TantraNotes`). The
/settings folder-exclusion tree writes these — see `set_subdir_excluded`.

A book is excluded if **any** rule matches its title or file path. The default
(when no config is present) excludes anything with **ANNOTATED** in its path/title
— the operator tags annotated copies by naming the folder/file accordingly, e.g.
"00 LTK Illuminating the Intent ANNOTATED— Jinpa — VGC Commentary".

Honored at the sweep (excluded files never ingest, `sweep._walk`) and removable
from already-ingested data with `python -m catalogue.cli.exclude_purge`.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

# Back-compat: needs_work.py still builds a SQL LIKE from this single token.
SKIP_TOKEN = "ANNOTATED"
_DEFAULT_RULES = ({"field": "any", "op": "contains", "value": SKIP_TOKEN},)


@lru_cache(maxsize=1)
def exclusion_rules() -> tuple:
    """The configured rules (vocab.json `_exclusions`), else the ANNOTATED default.
    Cached — call `exclusion_rules.cache_clear()` after editing the config in tests."""
    try:
        from catalogue.db_store.db import VOCAB_PATH
        data = json.loads(VOCAB_PATH.read_text("utf-8"))
        rules = data.get("_exclusions")
        if rules:
            return tuple(
                {"field": (r.get("field") or "any").lower(),
                 "op": (r.get("op") or "contains").lower(),
                 "value": str(r.get("value") or "")}
                for r in rules if r.get("value"))
    except Exception:
        pass
    return _DEFAULT_RULES


def _match(op: str, text: str, value: str) -> bool:
    t, v = text.upper(), value.upper()
    if op == "starts_with":
        return t.startswith(v)
    if op == "ends_with":
        return t.endswith(v)
    if op == "under":                      # folder + all descendants (path-segment)
        v = v.rstrip("/")
        return t == v or t.startswith(v + "/")
    return v in t                          # 'contains' (default)


def is_excluded(title: Optional[str] = None, file_path: Optional[str] = None) -> bool:
    """True if the title/path matches any configured exclusion rule."""
    for r in exclusion_rules():
        field = r["field"]
        targets = []
        if field in ("path", "any") and file_path:
            targets.append(file_path)
        if field in ("title", "any") and title:
            targets.append(title)
        if any(_match(r["op"], t, r["value"]) for t in targets):
            return True
    return False


def is_skipped(title: Optional[str] = None, file_path: Optional[str] = None) -> bool:
    """Back-compat alias for is_excluded (older call sites / needs_work)."""
    return is_excluded(title=title, file_path=file_path)


# ── subdirectory exclusion (the /settings folder checkbox tree) ───────────────
# Unchecking a folder writes a {field:path, op:under, value:<abs folder>} rule, so
# it and everything beneath it never ingest (sweep `_walk` prunes the subtree) and
# `exclude_purge` / `is_excluded` honour it for free — no parallel machinery.
SUBDIR_OP = "under"


def _norm_dir(path: str) -> str:
    """Canonical absolute folder key: normalised, no trailing slash."""
    return os.path.normpath(str(path)).rstrip("/") or "/"


def _raw_rules() -> list:
    """The `_exclusions` list exactly as stored (for editing), falling back to the
    ANNOTATED default so toggling a folder never silently drops it."""
    try:
        from catalogue.db_store.db import VOCAB_PATH
        data = json.loads(VOCAB_PATH.read_text("utf-8"))
        rules = data.get("_exclusions")
        if isinstance(rules, list):
            return [dict(r) for r in rules]
    except Exception:
        pass
    return [dict(r) for r in _DEFAULT_RULES]


def excluded_subdirs() -> list:
    """Absolute folder paths currently excluded via the folder tree (op=under)."""
    return [_norm_dir(r["value"]) for r in exclusion_rules()
            if r["op"] == SUBDIR_OP and r["field"] == "path" and r["value"]]


def subdir_excluded(path: str) -> bool:
    """True if THIS exact folder is itself an exclusion rule (checkbox unchecked)."""
    return _norm_dir(path) in excluded_subdirs()


def under_excluded(path: str) -> bool:
    """True if `path` is excluded by itself OR by an excluded ancestor folder."""
    p = _norm_dir(path)
    for v in excluded_subdirs():
        if p == v or p.startswith(v + "/"):
            return True
    return False


def set_subdir_excluded(path: str, excluded: bool) -> None:
    """Add (excluded=True) or remove an `under` exclusion for an absolute folder
    path, persisting to vocab.json `_exclusions` and busting the rule cache.
    Idempotent — toggling the same folder twice is a no-op pair."""
    p = _norm_dir(path)
    rules = [r for r in _raw_rules()
             if not (str(r.get("op") or "").lower() == SUBDIR_OP
                     and _norm_dir(str(r.get("value") or "")) == p)]
    if excluded:
        rules.append({"field": "path", "op": SUBDIR_OP, "value": p})
    from catalogue.services.mount import _write_vocab_value
    _write_vocab_value("_exclusions", rules)
    exclusion_rules.cache_clear()
