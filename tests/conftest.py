"""Shared pytest config for the whole suite.

Two hard invariants, set BEFORE any catalogue module is imported:

1. Hermetic authority matching: the web app runs authority matching at proposal
   accept time (CATALOGUE_INGEST_VERIFY defaults ON in production), which would hit
   the network from inside the promote/accept routes. Force it OFF for tests.

2. NEVER touch the live catalogue DB. The default DB path is `private/catalogue-db/
   catalogue.db` (web.DEFAULT_DB / sandbox.DEFAULT_LIVE, both read CATALOGUE_DB).
   A test that fell back to that default — `create_app()` with no path, a bare
   `sandbox.fork()`, a module CLI default — would mutate REAL data. We point
   CATALOGUE_DB at a throwaway so the default can't resolve to live, and a tripwire
   fixture fails any test that changes the live file anyway (e.g. via a hardcoded
   path). See `_guard_live_db` below.
"""
import os
import tempfile

import pytest

os.environ.setdefault("CATALOGUE_INGEST_VERIFY", "off")
# create_app() now fail-closes when no auth is configured (default-deny — see
# auth.provider_from_env). The suite is hermetic and localhost-only, so opt into open here;
# tests that exercise the gate set CATALOGUE_AUTH_USER/PASS (overriding this), and the
# default-deny test deletes this flag to assert the refusal.
os.environ.setdefault("CATALOGUE_ALLOW_OPEN", "1")
# Force (not setdefault) so an ambient CATALOGUE_DB pointing at real data can't leak
# into the suite. Tests that want a DB pass their own tmp_path explicitly anyway.
os.environ["CATALOGUE_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="catalogue-test-"), "default.db")

_LIVE_DB = "private/catalogue-db/catalogue.db"


def _live_fingerprint():
    """(size, mtime_ns) of the live DB, or None if it isn't present. Cheap — a
    single stat, no hashing — so it's fine to run around every test."""
    try:
        s = os.stat(_LIVE_DB)
    except FileNotFoundError:
        return None
    return (s.st_size, s.st_mtime_ns)


@pytest.fixture(autouse=True)
def _guard_live_db():
    """Tripwire: fail the offending test if it modified the real live catalogue DB.
    No-op when live isn't present (CI). Tests must use an isolated tmp DB."""
    before = _live_fingerprint()
    yield
    after = _live_fingerprint()
    assert after == before, (
        f"this test modified the LIVE catalogue DB ({_LIVE_DB}) — tests must use an "
        "isolated tmp_path DB, never the default/live path")
