"""The verify walk must STOP cleanly when an authority throttles mid-pass, never
record the remaining people as false 'unmatched' (the bug behind '10% matched').
"""
from __future__ import annotations

from catalogue.db_store import init_db, fold_key
from catalogue.services import verify
from catalogue.services.http_util import AuthorityUnavailable, MalformedResponse


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (pid, name, fold_key(name)))
    return pid


class _ThrottleAfter:
    """Verifier that matches the first N people, then throttles (raises)."""
    name = "throttle"
    def __init__(self, n):
        self.n = n
        self.calls = 0
    def verify(self, db, kind, text):
        if kind != "person":
            return None
        self.calls += 1
        if self.calls > self.n:
            raise AuthorityUnavailable("429 simulated")
        return verify.Match("wikidata:Q%d" % self.calls, "wikidata", text, [], self.name)


def test_walk_stops_on_throttle_and_does_not_mark_rest_unmatched(tmp_path):
    db = init_db(tmp_path / "t.db")
    pids = [_person(db, "Person %d" % i) for i in range(5)]
    v = _ThrottleAfter(2)                       # match 2, then 429

    summary = verify.verify_all(db, verifiers=[v], kinds=("person",))
    t = summary["person"]
    assert t["matched"] == 2
    assert t.get("stopped_early") is True
    # the remaining 3 are NOT counted as unmatched
    assert t["unmatched"] == 0
    # and they stay 'provisional' in the DB (re-runnable), none cached as a miss
    statuses = [db.execute("SELECT verification_status FROM person WHERE id=?",
                           (p,)).fetchone()[0] for p in pids]
    assert statuses.count("provisional") == 3
    assert statuses.count("verified") == 2


class _MalformedSource:
    """A verifier whose underlying client got a broken response (e.g. VIAF now
    returns HTML). The CLIENT swallows MalformedResponse into [], so the verifier
    returns None — a clean per-person miss. The walk must NOT halt (contrast with
    AuthorityUnavailable). This is the exact VIAF-killed-the-pass regression."""
    name = "malformed"
    def verify(self, db, kind, text):
        # Simulate the client having already swallowed a MalformedResponse → no hit.
        return None


def test_walk_does_not_halt_on_malformed_source(tmp_path):
    db = init_db(tmp_path / "t.db")
    pids = [_person(db, "Person %d" % i) for i in range(5)]
    # First verifier is "broken" (returns nothing), second matches everyone — proves
    # a dead source is skipped per-person, the chain falls through, the walk completes.
    class _Always:
        name = "always"
        def verify(self, db, kind, text):
            return verify.Match("wikidata:Q1", "wikidata", text, [], self.name) \
                if kind == "person" else None
    summary = verify.verify_all(db, verifiers=[_MalformedSource(), _Always()],
                                kinds=("person",))
    t = summary["person"]
    assert t.get("stopped_early") is not True   # NOT halted
    assert t["matched"] == 5                     # all completed via the fallback source
    assert t["unmatched"] == 0


def test_rerun_resumes_after_throttle(tmp_path):
    db = init_db(tmp_path / "t.db")
    for i in range(4):
        _person(db, "P %d" % i)
    # First pass: throttle after 1.
    verify.verify_all(db, verifiers=[_ThrottleAfter(1)], kinds=("person",))
    done1 = db.execute("SELECT COUNT(*) FROM person WHERE verification_status='verified'").fetchone()[0]
    assert done1 == 1
    # Second pass with a non-throttling verifier finishes the remaining 3.
    class _Always:
        name = "always"
        def verify(self, db, kind, text):
            return verify.Match("wikidata:Qx", "wikidata", text, [], self.name) if kind == "person" else None
    verify.verify_all(db, verifiers=[_Always()], kinds=("person",))
    done2 = db.execute("SELECT COUNT(*) FROM person WHERE verification_status='verified'").fetchone()[0]
    assert done2 == 4
