"""System tests for the redesigned dashboard: a 5-feature hub, unified search
grouped by a dynamic type registry, the tabbed Review hub, and subject curation.

Arrange via SQL (+ catalogue.db_store.add_alias for the folded keys); Act/Assert over HTTP.
"""
from __future__ import annotations

import sqlite3

from catalogue.db_store import add_alias


def _alias(app, kind, pid, text, scheme="english"):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        add_alias(conn, kind, pid, text, scheme)
        conn.commit()
    finally:
        conn.close()


def _seed_lotus(app, seed):
    """One shared token ('lotus') reachable through every search group."""
    eid = seed("INSERT INTO edition (title) VALUES ('Lotus Sutra Edition')").lastrowid
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    _alias(app, "work", wid, "Lotus Treatise")
    pid = seed("INSERT INTO person (primary_name) VALUES ('Lotus Master')").lastrowid
    _alias(app, "person", pid, "Lotus Master")
    sid = seed("INSERT INTO subject (name) VALUES ('Lotus Studies')").lastrowid
    seed("INSERT INTO work_subject (work_id, subject_id) VALUES (?, ?)", (wid, sid))
    return eid, wid, pid, sid


# ── Hub ───────────────────────────────────────────────────────────────────────
def test_home_hub_links_the_features(app_env, seed):
    c, _, _ = app_env
    page = c.get("/")
    assert page.status_code == 200
    # Section nav is the shared client-rendered floating menu (LibraryUI.nav); its items carry
    # the routes. Search (the sectioned module) + Text (full-text content) + Review (the editable
    # module) replace the old Library/Search/Review-hub trio. Review is desktop-only but still
    # emitted in the page's nav script (gated client-side by a media query).
    for href in (b"'/search'", b"'/text'", b"'/reconcile'",
                 b"'/capture'", b"'/review'", b"'/settings'"):
        assert href in page.data, href


# ── Unified search ─────────────────────────────────────────────────────────────
# The `/find` ("Browse") + `/api/v1/find` + `/find/suggest` HTTP surface was removed
# (the Search page covers it). The grouped-search + suggest BEHAVIOUR still lives in
# the domain (`search.aggregate_search` / `search.suggest`), pinned here directly.
def test_find_groups_results_by_type(app_env, seed):
    from catalogue.services import search as SEARCH
    c, app, _ = app_env
    _seed_lotus(app, seed)
    db = sqlite3.connect(app.config["DB_PATH"])
    doc = SEARCH.aggregate_search(db, "lotus"); db.close()
    groups = {g["label_plural"]: g for g in doc["groups"]}
    for heading in ("Editions", "Works", "People", "Subjects"):
        assert heading in groups, heading
    labels = " ".join(h["label"] for g in doc["groups"] for h in g["hits"])
    assert "Lotus Sutra Edition" in labels
    assert "Lotus Treatise" in labels
    assert "Lotus Master" in labels
    assert "Lotus Studies" in labels


def test_find_chip_filters_to_one_group(app_env, seed):
    from catalogue.services import search as SEARCH
    c, app, _ = app_env
    _seed_lotus(app, seed)
    db = sqlite3.connect(app.config["DB_PATH"])
    doc = SEARCH.aggregate_search(db, "lotus", only="people"); db.close()
    assert [g["key"] for g in doc["groups"]] == ["people"]   # only the People group
    labels = " ".join(h["label"] for g in doc["groups"] for h in g["hits"])
    assert "Lotus Master" in labels
    assert "Lotus Sutra Edition" not in labels


def test_find_suggest_prefixes_each_match_with_its_type(app_env, seed):
    from catalogue.services import search as SEARCH
    c, app, _ = app_env
    _seed_lotus(app, seed)
    db = sqlite3.connect(app.config["DB_PATH"])
    matches = SEARCH.suggest(db, "lotus"); db.close()
    types = {m["type"] for m in matches}
    # Singular type labels from the registry (the completion prefixes).
    assert {"Edition", "Work", "Person", "Subject"} <= types
    assert all("url" in m and "label" in m for m in matches)


def test_find_by_internal_number(app_env, seed):
    """Searching the bare catalogue number (or '#N') surfaces edition / work /
    person #N directly, and the number shows in each result's sublabel."""
    from catalogue.services import search as S
    c, app, _ = app_env
    eid, wid, pid, _sid = _seed_lotus(app, seed)
    db = sqlite3.connect(app.config["DB_PATH"])

    ed = S._search_editions(db, str(eid), 25)
    assert any(h["id"] == eid and f"#{eid}" in h["sublabel"] for h in ed)
    wk = S._search_works(db, f"#{wid}", 25)
    assert any(h["id"] == wid and f"#{wid}" in h["sublabel"] for h in wk)
    pl = S._search_people(db, str(pid), 25)
    assert any(h["id"] == pid and f"#{pid}" in h["sublabel"] for h in pl)

    # A non-numeric query never triggers an id lookup; a missing number is just empty.
    assert _as_id_none(S)
    assert S._search_works(db, str(wid + 99999), 25) == []
    db.close()


def _as_id_none(S):
    return S._as_id("Lotus") is None and S._as_id("") is None and S._as_id("#7") == 7


def test_find_empty_query_is_calm(app_env, seed):
    from catalogue.services import search as SEARCH
    c, app, _ = app_env
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert SEARCH.aggregate_search(db, "") == {"q": "", "groups": []}
        assert SEARCH.suggest(db, "") == []
    finally:
        db.close()


# ── Review hub ──────────────────────────────────────────────────────────────────
def test_review_hub_dispatches_and_renders_tab_strip(app_env, seed):
    c, _, _ = app_env
    # Books/Works/People are the existing queues (redirect to them).
    assert c.get("/review-hub").status_code == 302
    assert c.get("/review-hub?tab=works").status_code == 302
    assert c.get("/review-hub?tab=people").status_code == 302
    # The hub tab strip is present on each queue surface. Subjects moved OFF the hub
    # onto the Review module (/review/subjects, its own Catalogue↔Subjects tab strip).
    for url in ("/works/detect/single", "/works/incomplete", "/picker/person"):
        page = c.get(url, follow_redirects=True).data
        assert b'class="rtabs"' in page, url
    # The legacy subjects deep-link redirects onto the Review module.
    assert c.get("/review-hub?tab=subjects").status_code == 302


def test_review_hub_subjects_tab_lists_subjects(app_env, seed):
    c, app, _ = app_env
    _seed_lotus(app, seed)
    page = c.get("/review/subjects").data
    assert b"Lotus Studies" in page
    assert b"all-subjects" in page          # the datalist completion source


# ── Subject curation ─────────────────────────────────────────────────────────────
def _subject_status(app, name):
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        return conn.execute("SELECT id FROM subject WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()


def test_subject_rename(app_env, seed):
    c, app, _ = app_env
    sid = seed("INSERT INTO subject (name) VALUES ('emptiness/raw')").lastrowid
    c.post(f"/subject/{sid}/rename", data={"name": "Emptiness"})
    assert _subject_status(app, "Emptiness") is not None
    assert _subject_status(app, "emptiness/raw") is None


def test_subject_rename_onto_existing_merges(app_env, seed):
    c, app, _ = app_env
    keep = seed("INSERT INTO subject (name) VALUES ('Madhyamaka')").lastrowid
    dup = seed("INSERT INTO subject (name) VALUES ('madhyamaka/raw')").lastrowid
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    seed("INSERT INTO work_subject (work_id, subject_id) VALUES (?, ?)", (wid, dup))
    c.post(f"/subject/{dup}/rename", data={"name": "Madhyamaka"})
    # dup is gone; its tag moved to the surviving subject.
    assert _subject_status(app, "madhyamaka/raw") is None
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        n = conn.execute("SELECT COUNT(*) FROM work_subject WHERE subject_id = ?",
                         (keep,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_subject_delete(app_env, seed):
    c, app, _ = app_env
    sid = seed("INSERT INTO subject (name) VALUES ('Orphan')").lastrowid
    c.post(f"/subject/{sid}/delete")
    assert _subject_status(app, "Orphan") is None


# ── Review Books count = combined single + multi ────────────────────────────────
def test_books_badge_counts_single_and_multi_detections(app_env, seed):
    from catalogue.webui.web import review_backlog_counts
    c, app, _ = app_env
    e1 = seed("INSERT INTO edition (title) VALUES ('Single Bk')").lastrowid
    e2 = seed("INSERT INTO edition (title, structure) VALUES ('Multi Bk', 'multi_work')").lastrowid
    seed("INSERT INTO work_detection (edition_id, kind, payload_json) VALUES (?, 'single', '{}')", (e1,))
    seed("INSERT INTO work_detection (edition_id, kind, payload_json) VALUES (?, 'multi', '{}')", (e2,))
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert review_backlog_counts(conn)["books"] == 2   # combined, not just single
    finally:
        conn.close()
    # an applied detection drops out of the count
    seed("UPDATE work_detection SET payload_json = '{\"applied\": true}' WHERE edition_id = ?", (e1,))
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert review_backlog_counts(conn)["books"] == 1
    finally:
        conn.close()


# ── Browse: field-scoped autocomplete + person (any role) ───────────────────────
def _seed_book_with_author(app, seed):
    eid = seed("INSERT INTO edition (title) VALUES ('Way of the Bodhisattva')").lastrowid
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    _alias(app, "work", wid, "Bodhicaryavatara")
    seed("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    pid = seed("INSERT INTO person (primary_name) VALUES ('Shantideva')").lastrowid
    _alias(app, "person", pid, "Shantideva")
    seed("INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, 'author')", (wid, pid))
    return eid, wid, pid


def test_browse_has_person_field_with_autocomplete(app_env, seed):
    c, _, _ = app_env
    page = c.get("/library").get_data(as_text=True)
    assert ">Person<" in page                       # field renamed from "Author"
    assert 'placeholder="Author"' not in page
    for url in ("/library/suggest/person", "/editions/search", "/works/search"):
        assert url in page                          # each field wired to a scoped suggest


def test_person_suggest_carries_roles(app_env, seed):
    c, app, _ = app_env
    _seed_book_with_author(app, seed)
    matches = c.get("/library/suggest/person?q=shantideva").get_json()["matches"]
    assert matches and matches[0]["name"] == "Shantideva"
    assert "author" in matches[0]["roles"]


def test_browse_person_search_matches_any_role(app_env, seed):
    c, app, _ = app_env
    _seed_book_with_author(app, seed)
    page = c.get("/library?person=shantideva").get_data(as_text=True)
    assert "Way of the Bodhisattva" in page         # found via the author role


# ── Work detail: full editable card (with holdings) shown inline, no page jump ───
def test_work_card_fragment_lists_holdings_and_is_editable(app_env, seed, tmp_path):
    c, app, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('Ed A')").lastrowid
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    _alias(app, "work", wid, "Some Treatise")
    seed("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    seed("INSERT INTO holding (id, edition_id, form, file_path) VALUES (90, ?, 'electronic', '/t/a.pdf')", (eid,))
    card = c.get(f"/work/{wid}/card").get_data(as_text=True)
    assert "Editions &amp; holdings" in card or "Editions & holdings" in card
    assert "openHolding(90" in card                 # the holding's open control
    assert f'action="/work/{wid}/edit"' in card     # editable in place
    assert "<nav" not in card                        # chrome-less fragment


def test_works_review_pane_embeds_the_work_card_inline(app_env, seed):
    # An incomplete work (no author/subject/type) lands in Review→Works; its pane
    # embeds /work/<id>/card so all details show inline (no navigation).
    c, app, _ = app_env
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    _alias(app, "work", wid, "Lonely Work")
    page = c.get("/works/incomplete").get_data(as_text=True)
    assert f'data-card-url="/work/{wid}/card"' in page
    assert "__workCardJs" in page                    # the card's button JS is on the page


# ── Person detail shown inline in the Browse pane (no page jump) ─────────────────
def test_person_card_fragment_is_chromeless_and_full(app_env, seed):
    c, app, _ = app_env
    pid = seed("INSERT INTO person (primary_name, dates) VALUES ('Tsongkhapa', '1357–1419')").lastrowid
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    _alias(app, "work", wid, "Lamrim Chenmo")
    seed("INSERT INTO work_author (work_id, person_id, role) VALUES (?, ?, 'author')", (wid, pid))
    card = c.get(f"/person/{pid}/card").get_data(as_text=True)
    assert "<nav" not in card                              # chrome-less fragment
    assert f'action="/person/{pid}/edit"' in card          # editable in place
    assert "Lamrim Chenmo" in card and "Works" in card     # their works listed


def test_browse_person_mode_shows_person_read_only_in_pane(app_env, seed):
    # The Search page (/library) is READ-ONLY: a picked person shows their name + a
    # link to their person page to edit, NOT the editable person card. Their works /
    # editions appear as left-pane sections.
    c, app, _ = app_env
    pid = seed("INSERT INTO person (primary_name) VALUES ('Tsongkhapa')").lastrowid
    page = c.get(f"/library?pid={pid}").get_data(as_text=True)
    assert f'data-card-url="/person/{pid}/card"' not in page   # no editable card on Search
    assert "Tsongkhapa" in page
    assert f'href="/person/{pid}"' in page                     # read-only → edit on the person page
    # the Browse Person pick targets person-mode (deep-links ?pid on the SAME module,
    # BASE + '?pid='), not /person/<id>.
    lib = c.get("/library").get_data(as_text=True)
    assert '"/library"' in lib and "?pid=" in lib


# ── Merge / dedup available on every record type ────────────────────────────────
def test_person_card_offers_merge(app_env, seed):
    c, app, _ = app_env
    pid = seed("INSERT INTO person (primary_name) VALUES ('Dup Person')").lastrowid
    card = c.get(f"/person/{pid}/card").get_data(as_text=True)
    assert "merge-widget" in card
    assert f'data-merge-apply="/picker/person/{pid}/merge"' in card


def test_edition_card_offers_merge(app_env, seed):
    c, app, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('Dup Edition')").lastrowid
    card = c.get(f"/edition/{eid}/card").get_data(as_text=True)
    assert "merge-widget" in card
    assert f'data-merge-apply="/edition/{eid}/merge"' in card


def test_edition_merge_endpoint_repoints_and_is_reversible(app_env, seed):
    c, app, _ = app_env
    dup = seed("INSERT INTO edition (title) VALUES ('Dup')").lastrowid
    keep = seed("INSERT INTO edition (title) VALUES ('Keep')").lastrowid
    seed("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', '/t/d.pdf')", (dup,))
    res = c.post(f"/edition/{dup}/merge", data={"into": keep}).get_json()
    assert res.get("status") == "merged" and "undo_token" in res
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert conn.execute("SELECT 1 FROM edition WHERE id = ?", (dup,)).fetchone() is None
        assert conn.execute("SELECT edition_id FROM holding WHERE file_path = '/t/d.pdf'").fetchone()[0] == keep
    finally:
        conn.close()


def test_all_record_types_have_a_merge_affordance(app_env, seed):
    """Subject / person / work / edition each expose merge in their pane."""
    c, app, _ = app_env
    sid = seed("INSERT INTO subject (name) VALUES ('S')").lastrowid
    pid = seed("INSERT INTO person (primary_name) VALUES ('P')").lastrowid
    wid = seed("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    _alias(app, "work", wid, "W")
    eid = seed("INSERT INTO edition (title) VALUES ('E')").lastrowid
    assert "Merge into" in c.get("/review/subjects").get_data(as_text=True)   # subject
    assert "merge-widget" in c.get(f"/person/{pid}/card").get_data(as_text=True)       # person
    assert "merge-target-btn" in c.get(f"/work/{wid}/card").get_data(as_text=True)     # work
    assert "merge-widget" in c.get(f"/edition/{eid}/card").get_data(as_text=True)      # edition


# ── Browse detail: READ-ONLY on the Search page (editing lives on Review) ──────
def test_browse_detail_is_read_only(app_env, seed):
    c, _, _ = app_env
    eid = seed("INSERT INTO edition (title) VALUES ('A Book')").lastrowid
    page = c.get(f"/library?eid={eid}").get_data(as_text=True)
    # No editing affordances on the read-only Search page…
    assert "edit-details" not in page
    assert 'data-card-url="/edition/%d/card"' % eid not in page
    # …but the read-only summary + Connections still show for browsing.
    assert "Connections" in page
    assert 'data-card-url="/edition/%d/works-summary"' % eid in page
