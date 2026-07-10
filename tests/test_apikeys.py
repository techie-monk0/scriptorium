"""Shared API-key resolver (catalogue/domain/apikeys.py): env wins, then KEY=VALUE in
api_key.txt / .kdrive_settings, with legacy bare-line api_key.txt = ANTHROPIC_API_KEY."""
from __future__ import annotations

import os

import pytest

from catalogue.services import apikeys


@pytest.fixture
def in_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # apikeys reads files relative to CWD
    # apikeys._roots() also searches the REPO ROOT, where a developer's real api_key.txt
    # (with a live GOOGLE_BOOKS_API_KEY) lives — that would leak into "no key" assertions.
    # Pin the search to the temp dir so the fixture's own files are the only source.
    monkeypatch.setattr(apikeys, "_roots", lambda: [tmp_path])
    for k in ("ANTHROPIC_API_KEY", "GOOGLE_BOOKS_API_KEY", "KDRIVE_WEBDAV_URL"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_legacy_bare_api_key_txt_is_anthropic(in_dir):
    (in_dir / "api_key.txt").write_text("sk-ant-legacy-bare\n")
    assert apikeys.get("ANTHROPIC_API_KEY") == "sk-ant-legacy-bare"


def test_key_value_lines_for_all_keys(in_dir):
    (in_dir / "api_key.txt").write_text(
        "# my keys\n"
        "ANTHROPIC_API_KEY=sk-ant-xyz\n"
        'export GOOGLE_BOOKS_API_KEY="AIzaKEY"\n'
        "KDRIVE_WEBDAV_URL=https://abc.connect.kdrive.infomaniak.com\n")
    assert apikeys.get("ANTHROPIC_API_KEY") == "sk-ant-xyz"
    assert apikeys.get("GOOGLE_BOOKS_API_KEY") == "AIzaKEY"      # export + quotes stripped
    assert apikeys.get("KDRIVE_WEBDAV_URL").endswith("infomaniak.com")
    assert apikeys.get("MISSING") is None


def test_env_wins_over_file(in_dir, monkeypatch):
    (in_dir / "api_key.txt").write_text("GOOGLE_BOOKS_API_KEY=from-file\n")
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "from-env")
    assert apikeys.get("GOOGLE_BOOKS_API_KEY") == "from-env"


def test_falls_back_to_kdrive_settings(in_dir):
    (in_dir / ".kdrive_settings").write_text('export KDRIVE_WEBDAV_USER="me@e.com"\n')
    assert apikeys.get("KDRIVE_WEBDAV_USER") == "me@e.com"


def test_require_raises_when_missing(in_dir):
    with pytest.raises(RuntimeError):
        apikeys.require("NOPE_KEY")


def test_googlebooks_url_includes_key_when_present(in_dir):
    from catalogue.services import covers
    (in_dir / "api_key.txt").write_text("GOOGLE_BOOKS_API_KEY=AIzaTEST\n")
    assert "key=AIzaTEST" in covers._gb_url({"q": "isbn:123"})
    (in_dir / "api_key.txt").unlink()
    assert "key=" not in covers._gb_url({"q": "isbn:123"})
