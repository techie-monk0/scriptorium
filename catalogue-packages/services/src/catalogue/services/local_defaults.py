"""Machine-local fallback defaults for paths that used to be hardcoded.

These are the *last-resort* built-in defaults for a few library paths (inbox, trash,
mount root). The real resolution order is unchanged and lives in the callers:

    $ENV override  →  vocab.json (operator /settings)  →  THIS file  →  ""

Keeping the values here — loaded from a git-ignored file under ``private/`` — means no
personal path is baked into tracked, public-facing source. A fresh public clone has no
such file, so every default resolves to ``""``, which the callers already treat as "not
configured" (fail-open). The maintainer's real values live in
``private/local_defaults.json`` (which is stripped from the public release with the rest
of ``private/``).

Override the file location with ``$CATALOGUE_LOCAL_DEFAULTS`` if needed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# repo-root-relative default location (works for the editable install / repo checkout).
_REPO_ROOT = Path(__file__).resolve().parents[5]
_DEFAULT_PATH = _REPO_ROOT / "private" / "local_defaults.json"


def _load() -> dict:
    override = os.environ.get("CATALOGUE_LOCAL_DEFAULTS")
    path = Path(override) if override else _DEFAULT_PATH
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_DEFAULTS = _load()


def get(key: str, fallback: str = "") -> str:
    """A machine-local default string, or ``fallback`` (default ``""``) when unset."""
    v = _DEFAULTS.get(key)
    return v if isinstance(v, str) and v else fallback
