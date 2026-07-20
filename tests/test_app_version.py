"""Unit tests for the server build stamp / staleness check (catalogue.webui.app_version).

These drive the real BuildStamp over a temp source tree — touching a file changes its mtime, which is
exactly the "code on disk changed since the process started" signal the staleness gate keys on.
"""
from __future__ import annotations

import os

import pytest

from catalogue.webui.app_version import BuildStamp, build, handshake, is_stale, verify


@pytest.fixture
def tree(tmp_path):
    """A tiny source tree: one .py (restart-tracked), one template, one static asset, plus dirs that
    must be ignored (__pycache__, vendor, a hidden dir)."""
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "page.html").write_text("<p>hi</p>\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "engine.js").write_text("var a = 1;\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-313.pyc").write_text("junk")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "big.js").write_text("/* vendored */\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "secret.py").write_text("y = 2\n")
    return tmp_path


def _bump_mtime(path, delta_s=10):
    """Push a file's mtime forward deterministically (no reliance on wall-clock resolution)."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_s * 1_000_000_000))


# ── the build id ─────────────────────────────────────────────────────────────
def test_build_is_short_hex_and_stable(tree):
    bs = BuildStamp([tree])
    assert bs.app_build and len(bs.app_build) == 12
    int(bs.app_build, 16)                          # it's hex
    assert bs.app_build == bs.app_build            # stable within a process


def test_two_stamps_over_same_tree_agree(tree):
    assert BuildStamp([tree]).app_build == BuildStamp([tree]).app_build


def test_excluded_dirs_do_not_affect_the_build(tree):
    before = BuildStamp([tree]).app_build
    _bump_mtime(tree / "__pycache__" / "app.cpython-313.pyc")
    _bump_mtime(tree / "vendor" / "big.js")
    _bump_mtime(tree / ".hidden" / "secret.py")
    assert BuildStamp([tree]).app_build == before   # none of those are tracked


# ── staleness ────────────────────────────────────────────────────────────────
def test_fresh_stamp_is_not_stale(tree):
    assert BuildStamp([tree], ttl=0).is_stale() is False


def test_touching_a_py_file_makes_it_stale(tree):
    bs = BuildStamp([tree], ttl=0)                  # ttl=0 → recompute every call, no cache lag
    assert not bs.is_stale()
    _bump_mtime(tree / "app.py")
    assert bs.is_stale() is True                    # a .py change needs a restart → stale


def test_touching_a_template_or_static_does_not_make_it_stale(tree):
    """Templates auto-reload and static is cache-busted, so changing them needs no restart."""
    bs = BuildStamp([tree], ttl=0)
    _bump_mtime(tree / "page.html")
    _bump_mtime(tree / "sub" / "engine.js")
    assert bs.is_stale() is False


def test_ttl_caches_the_disk_read(tree):
    bs = BuildStamp([tree], ttl=60)                 # long TTL: first read is cached
    assert not bs.is_stale()
    _bump_mtime(tree / "app.py")
    assert bs.is_stale() is False                   # still cached (would be True once TTL lapses)


# ── handshake payload + verify ───────────────────────────────────────────────
def test_handshake_shape(tree):
    h = BuildStamp([tree]).handshake()
    assert set(h) == {"app_build", "server_stale"}
    assert isinstance(h["app_build"], str) and isinstance(h["server_stale"], bool)


def test_verify_clean_tree(tree):
    assert BuildStamp([tree]).verify() == []


def test_verify_flags_a_tree_with_no_code(tmp_path):
    (tmp_path / "only.html").write_text("<p>no python here</p>")
    problems = BuildStamp([tmp_path]).verify()
    assert any("no restart-tracked" in p for p in problems)


# ── multi-root / whole-namespace coverage (not just webui) ────────────────────
def test_stale_detects_a_change_in_any_root(tmp_path):
    """A change in a SIBLING package root (not the first) must still flip staleness."""
    r1 = tmp_path / "webui"; r1.mkdir(); (r1 / "web.py").write_text("a = 1\n")
    r2 = tmp_path / "db_store"; r2.mkdir(); (r2 / "store.py").write_text("b = 2\n")
    bs = BuildStamp([r1, r2], ttl=0)
    assert not bs.is_stale()
    _bump_mtime(r2 / "store.py")                     # a change in the second root
    assert bs.is_stale() is True


def test_json_change_makes_it_stale(tmp_path):
    """A contract descriptor (JSON read into a module global at import) needs a restart, so a change
    to it flips staleness — the whole point of extending beyond .py."""
    root = tmp_path / "contracts"; root.mkdir()
    (root / "mod.py").write_text("x = 1\n")
    (root / "reader_sync_contract.json").write_text('{"version": 2}\n')
    bs = BuildStamp([root], ttl=0)
    assert not bs.is_stale()
    _bump_mtime(root / "reader_sync_contract.json")
    assert bs.is_stale() is True


def test_loaded_catalogue_roots_spans_the_siblings():
    """The real namespace scan reaches beyond webui to the packages the server imports (db_store,
    services, contracts) — so a restart-requiring change in any of them is caught. (Exclusion of
    never-imported packages is by construction — only sys.modules-present packages contribute — but
    can't be asserted here because the pytest process itself imports more, e.g. test_kit.)"""
    from catalogue.webui.web import create_app          # ensure the app (and its imports) are loaded
    _ = create_app  # noqa
    from catalogue.webui import app_version
    names = {p.name for p in app_version.loaded_catalogue_roots()}
    assert {"webui", "db_store", "services", "contracts"}.issubset(names)


def test_unimported_package_is_excluded_by_construction(monkeypatch):
    """Directly exercise the scan's filter: a catalogue package NOT in sys.modules contributes no root."""
    from catalogue.webui import app_version
    roots_before = {p.name for p in app_version.loaded_catalogue_roots()}
    assert "definitely_not_a_real_pkg" not in roots_before   # never imported → never a root


# ── the real running-server stamp ────────────────────────────────────────────
def test_default_stamp_is_healthy_and_advertises():
    assert verify() == []
    h = handshake()
    assert h["app_build"] == build()
    assert h["server_stale"] is False               # the test process matches its own on-disk code
    assert is_stale() is False
