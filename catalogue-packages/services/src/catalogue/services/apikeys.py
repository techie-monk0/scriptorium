"""One place to resolve API keys / secrets.

Per key, resolution is: environment variable wins, else a `KEY=VALUE` line in a
git-ignored secret file — `api_key.txt` (the project's existing key store), then
`.kdrive_settings`. A bare single-line `api_key.txt` (the legacy format) is read as
`ANTHROPIC_API_KEY`, so nothing breaks. An `export ` prefix and surrounding quotes are
tolerated, so shell-style files work too.

Put every key in ONE file — `api_key.txt`, one per line:

    ANTHROPIC_API_KEY=sk-ant-...
    GOOGLE_BOOKS_API_KEY=AIza...
    KDRIVE_WEBDAV_URL=https://XXXX.connect.kdrive.infomaniak.com
    KDRIVE_WEBDAV_USER=you@example.com
    KDRIVE_WEBDAV_PASS=app-password
    KDRIVE_LOCAL_ROOT=/Users/you/kDrive 2

(.kdrive_settings keeps working too; api_key.txt just becomes the single home if you
move everything there.)"""
from __future__ import annotations

import os
from pathlib import Path

SECRET_FILES = ("api_key.txt", ".kdrive_settings")


def _roots():
    out = [Path.cwd()]
    try:
        out.append(Path(__file__).resolve().parents[2])      # repo root
    except Exception:
        pass
    return out


def _parse(text: str, *, bare_is_anthropic: bool) -> dict:
    lines = [ln.strip() for ln in text.splitlines()]
    nonblank = [ln for ln in lines if ln and not ln.startswith("#")]
    if bare_is_anthropic and len(nonblank) == 1 and "=" not in nonblank[0]:
        return {"ANTHROPIC_API_KEY": nonblank[0]}            # legacy bare key
    out: dict = {}
    for ln in nonblank:
        if ln.startswith("export "):
            ln = ln[len("export "):]
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def file_values() -> dict:
    """Merged KEY→value from the secret files (first file/copy that defines a key wins).
    Read fresh each call (files are tiny) so a newly-added key is picked up promptly."""
    vals: dict = {}
    for fname in SECRET_FILES:
        for r in _roots():
            p = r / fname
            try:
                text = p.read_text("utf-8")
            except OSError:
                continue
            for k, v in _parse(text, bare_is_anthropic=(fname == "api_key.txt")).items():
                vals.setdefault(k, v)
            break                                            # first existing copy wins
    return vals


def get(name: str, default=None):
    """The value for `name`: env var first, else the secret files, else `default`."""
    return os.environ.get(name) or file_values().get(name) or default


def require(name: str) -> str:
    v = get(name)
    if not v:
        raise RuntimeError(f"missing {name}: set the env var or add `{name}=...` to api_key.txt")
    return v
