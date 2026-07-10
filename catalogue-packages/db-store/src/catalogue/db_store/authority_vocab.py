"""Authority-control vocabulary — the catalogue's extensible controlled-vocab layer.

"Authority control" = the controlled vocabularies the catalogue matches and
classifies against:
  • honorifics / offices            (`_honorific`, `_office`)
  • name transliteration-variant groups for author matching (`_translit_variant`)
  • organization-name markers        (`_organization`)
  • traditions                       (`_tradition`)
  • the code/label lookup sets       (`work_type`, `holding_type`, `locator_type`, …)

The shipped defaults live in `vocab.json`. THIS module is the single place that
reads them and merges YOUR additions on top, so nothing else has to know where the
vocab comes from.

Extending it — no fork, no code change
--------------------------------------
Drop a `vocab.local.json` overlay next to your database
(`<data_dir>/vocab.local.json`, or point `$CATALOGUE_VOCAB_LOCAL` at any path). It
is DEEP-MERGED onto the shipped `vocab.json`:
  • plain lists are concatenated + de-duplicated (add honorifics, translit groups…)
  • code/label lists merge by `code` (add a new lookup value, or relabel one)
  • dicts merge key-by-key; a scalar in the overlay wins

Example `vocab.local.json` teaching the matcher a new honorific and a new
name-variant group, plus a new work_type:

    {
      "_honorific": ["dorje-lopon"],
      "_translit_variant": [["khyentse", "kyentse", "khyentsé"]],
      "work_type": [{"code": "sadhana", "label": "Sādhana"}]
    }

The shipped file is never edited, so upstream updates keep flowing. (To replace the
WHOLE base instead of overlaying, point `$CATALOGUE_VOCAB` at your own file.)
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from .paths import data_dir

#: Env var pointing at a user overlay file (full path). Overrides the default location.
OVERLAY_ENV = "CATALOGUE_VOCAB_LOCAL"
#: Default overlay filename, looked up inside the data directory.
OVERLAY_FILENAME = "vocab.local.json"


def overlay_path() -> Path:
    """Resolved user-overlay location: ``$CATALOGUE_VOCAB_LOCAL`` if set, else
    ``<data_dir>/vocab.local.json``. The file need not exist (absent ⇒ no overlay)."""
    env = os.environ.get(OVERLAY_ENV)
    return Path(env) if env else Path(data_dir()) / OVERLAY_FILENAME


def _merge_lists(base: list, over: list) -> list:
    combined = list(base) + list(over)
    # code/label rows → key by 'code' so an overlay adds new codes or relabels existing ones.
    coded = [x for x in combined if isinstance(x, dict)]
    if coded and coded == combined and any("code" in x for x in coded):
        merged: dict = {}
        order: list = []
        for x in combined:
            code = x.get("code")
            key = code if code is not None else f"_uncoded:{len(order)}"
            if key not in merged:
                order.append(key)
            merged[key] = {**merged.get(key, {}), **x}
        return [merged[k] for k in order]
    # plain list → concat + de-dupe, preserving order. Handles str and unhashable
    # nested lists (e.g. `_translit_variant` groups) via a JSON key.
    out, seen = [], set()
    for x in combined:
        key = x if isinstance(x, str) else json.dumps(x, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def _deep_merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(over, list):
        return _merge_lists(base, over)
    return over  # scalar, or a type change in the overlay → overlay wins


def _read(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=8)
def _merged(base_str: str, overlay_str: str) -> dict:
    data = _read(base_str)
    if overlay_str:
        data = _deep_merge(data, _read(overlay_str))
    return data


def vocab_config(base_path: "str | os.PathLike | None" = None) -> dict:
    """The merged authority-control vocab as a dict.

    ``base_path`` defaults to the shipped ``vocab.json`` (``db.VOCAB_PATH``, which
    already honours ``$CATALOGUE_VOCAB``); the user overlay (:func:`overlay_path`)
    is deep-merged on top. Cached — call :func:`reload` after editing either file
    in a long-running process.
    """
    if base_path is None:
        from .db import VOCAB_PATH  # lazy: db.py imports this module's package
        base_path = VOCAB_PATH
    ov = overlay_path()
    return _merged(str(base_path), str(ov) if ov.exists() else "")


def reload() -> None:
    """Drop the merged-config cache (tests, or after editing vocab.json / the overlay)."""
    _merged.cache_clear()
