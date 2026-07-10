"""Shared fixtures for system tests.

Convention: setup may seed via direct SQL (Arrange), but Act/Assert go
through HTTP or the top-level Python entry points only.
"""
from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from catalogue.webui.web import create_app


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Isolated app + DB + upload dir per test."""
    monkeypatch.setenv("CATALOGUE_UPLOAD_DIR", str(tmp_path / "uploads"))
    app = create_app(tmp_path / "system.db")
    app.testing = True
    # Offline by default: the capture cross-format verdict (contract v2) calls
    # both ISBN resolvers. Stub them to no-ops so the suite never hits the network;
    # tests that exercise resolution override these per-test.
    app.config["ISBN_LOOKUP"] = lambda _i: None
    app.config["ISBN_WORK_KEY_LOOKUP"] = lambda _i: None
    with app.test_client() as c:
        yield c, app, tmp_path


@pytest.fixture
def seed(app_env):
    """Direct SQL writer for test setup. Tests use the HTTP surface for
    Act and Assert; this exists only because there is no `/seed` endpoint
    (and there shouldn't be)."""
    _, app, _ = app_env

    def _write(sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        finally:
            conn.close()

    return _write


def make_epub(path: Path, bodies: list[str]) -> None:
    """Build a minimal EPUB for sweep/extract tests."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        for i, body in enumerate(bodies):
            z.writestr(
                f"OEBPS/ch{i}.xhtml",
                f"<html><body>{body}</body></html>",
            )
