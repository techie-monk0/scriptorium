"""System tests — person↔work joint resolution (the homonym disambiguator).

Black-box, per the system-test convention (tests/system/conftest.py): ARRANGE via the
`seed` SQL fixture, ACT through the top-level entry point `resolve_all_person_works`
(the same function the CLI's main() calls) with an INJECTED fake resolver so the real
code path runs offline, ASSERT through the HTTP UI (/person/<id>, /work/<id>, /review).

The fake resolver maps a work title → a WorkAuthorityConsensus carrying source-neutral
`author_ids` — exactly what a live WorkAuthorityResolver would produce, minus the network.
"""
from __future__ import annotations

from catalogue.services import person_work
from catalogue.services.work_authority import WorkAuthorityConsensus


# ── fake resolver: title (fold-insensitive contains) → consensus ────────────────
def _consensus(author_name, author_qid, *, canonical=None):
    """A consensus as the live resolver would return for a confidently-resolved work
    with one identified author (Wikidata-style: name + Q-id + cross-links)."""
    ext = f"wikidata:{author_qid}"
    return WorkAuthorityConsensus(
        verdict="verified",
        authors=[author_name],
        canonical_system=("toh" if canonical else None),
        canonical_number=canonical,
        author_ids=[{"name": author_name, "external_id": ext,
                     "extra_ids": {"wikidata": ext, "bdrc": "bdr:P4954"}}],
    )


class _FakeResolver:
    """Maps work title → consensus. Unknown titles → empty 'none' consensus."""
    sources = []
    def __init__(self, by_title):
        self._by_title = by_title
    def resolve(self, title, *, language=None, aliases=()):
        return self._by_title.get(title, WorkAuthorityConsensus("none"))


def _seed_person_with_work(seed, person_name, work_title, *, canonical=None):
    """Insert a person + a work (with title alias) + an author work_contributor edge."""
    pid = seed("INSERT INTO person (primary_name, verification_status) "
               "VALUES (?, 'provisional')", (person_name,)).lastrowid
    seed("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
         "VALUES (?, ?, 'english', ?)", (pid, person_name, person_name.lower()))
    wid = seed("INSERT INTO work DEFAULT VALUES").lastrowid
    seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
         "VALUES (?, ?, 'english', ?)", (wid, work_title, work_title.lower()))
    seed("INSERT INTO work_author (work_id, person_id, role) "
         "VALUES (?, ?, 'author')", (wid, pid))
    return pid, wid


def _db(app):
    import sqlite3
    return sqlite3.connect(app.config["DB_PATH"])


# ── THE headline test: two same-named people resolve to DIFFERENT identities ────
def test_homonym_split_via_works(app_env, seed):
    c, app, _ = app_env
    # Two distinct person rows, both literally "Nagarjuna", each on a different work.
    p1, _ = _seed_person_with_work(seed, "Nagarjuna", "Mulamadhyamakakarika")
    p2, _ = _seed_person_with_work(seed, "Nagarjuna", "Guhyasamaja Sadhana")

    fake = _FakeResolver({
        "Mulamadhyamakakarika": _consensus("Nagarjuna", "Q171195"),   # the Madhyamaka master
        "Guhyasamaja Sadhana":  _consensus("Nagarjuna", "Q9999"),     # the tantric Nagarjuna
    })
    # ACT through the public entry point with the injected resolver.
    tally = person_work.resolve_all_person_works(_db(app), resolver=fake)
    assert tally["matched"] == 2

    # ASSERT through HTTP: the two same-named people now hold DIFFERENT identities,
    # each picked by the work they authored.
    page1 = c.get(f"/person/{p1}").data
    page2 = c.get(f"/person/{p2}").data
    assert b"wikidata:Q171195" in page1
    assert b"wikidata:Q9999" in page2
    assert b"wikidata:Q9999" not in page1     # not cross-contaminated


def test_manual_add_then_joint_binds_person_and_fills_work(app_env, seed):
    c, app, _ = app_env
    pid, wid = _seed_person_with_work(seed, "Nagarjuna", "Mulamadhyamakakarika",
                                      canonical="3824")
    fake = _FakeResolver({"Mulamadhyamakakarika":
                          _consensus("Nagarjuna", "Q171195", canonical="3824")})
    person_work.resolve_all_person_works(_db(app), resolver=fake)

    person_page = c.get(f"/person/{pid}").data
    assert b"wikidata:Q171195" in person_page
    assert b"bdr:P4954" in person_page         # cross-link harvested too
    # the work got its canonical id filled in the same pass
    work_page = c.get(f"/work/{wid}").data
    assert b"3824" in work_page


def test_conflict_is_queued_not_bound(app_env, seed):
    c, app, _ = app_env
    # ONE person attached to TWO works that resolve to DIFFERENT authors.
    pid = seed("INSERT INTO person (primary_name, verification_status) "
               "VALUES ('Nagarjuna', 'provisional')").lastrowid
    seed("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
         "VALUES (?, 'Nagarjuna', 'english', 'nagarjuna')", (pid,))
    for title in ("Work A", "Work B"):
        wid = seed("INSERT INTO work DEFAULT VALUES").lastrowid
        seed("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
             "VALUES (?, ?, 'english', ?)", (wid, title, title.lower()))
        seed("INSERT INTO work_author (work_id, person_id, role) "
             "VALUES (?, ?, 'author')", (wid, pid))
    fake = _FakeResolver({"Work A": _consensus("Nagarjuna", "Q171195"),
                          "Work B": _consensus("Nagarjuna", "Q9999")})
    tally = person_work.resolve_all_person_works(_db(app), resolver=fake)
    assert tally["candidate"] == 1 and tally["matched"] == 0

    # NOT bound; a conflict review item exists.
    conn = _db(app)
    assert conn.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None
    rj = conn.execute("SELECT payload_json FROM review_queue "
                      "WHERE item_type='person_work_joint'").fetchone()
    conn.close()
    assert rj and "work_conflict" in rj[0]
    # the review page lists it and disables accept
    page = c.get("/review-queue?type=person_work_joint").data
    assert b"person_work_joint" in page


def test_sajjana_guard_no_false_bind(app_env, seed):
    """Work resolves, but its author name ≠ the person's name → must NOT bind."""
    c, app, _ = app_env
    pid, _ = _seed_person_with_work(seed, "Nagarjuna", "Some Work")
    fake = _FakeResolver({"Some Work": _consensus("Sajjana", "Q4920")})  # wrong person
    person_work.resolve_all_person_works(_db(app), resolver=fake)
    conn = _db(app)
    ext = conn.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0]
    conn.close()
    assert ext is None
    assert b"wikidata:Q4920" not in c.get(f"/person/{pid}").data


def test_review_accept_binds_end_to_end(app_env, seed):
    """A queued needs_confirm item → POST accept → person shows the bound identity."""
    c, app, _ = app_env
    # fuzzy-name case: work author "Nagarjuna II" overlaps but isn't exact → needs_confirm
    pid, _ = _seed_person_with_work(seed, "Nagarjuna", "Some Tantra")
    fake = _FakeResolver({"Some Tantra": _consensus("Nagarjuna Gupta", "Q5555")})
    person_work.resolve_all_person_works(_db(app), resolver=fake)

    conn = _db(app)
    iid = conn.execute("SELECT id FROM review_queue "
                       "WHERE item_type='person_work_joint'").fetchone()
    conn.close()
    assert iid, "expected a queued needs_confirm candidate"
    iid = iid[0]
    r = c.post(f"/review-queue/{iid}/authority/accept")
    assert r.status_code in (200, 302)
    assert b"wikidata:Q5555" in c.get(f"/person/{pid}").data
