"""Tests for personal-name date stripping + dedup merge (punch-list M4)."""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db, fold_key
from catalogue.services.names import (
    split_name_dates, normalize_person_dates,
    split_contributors, is_ambiguous_blob, split_existing_contributors,
    canonical_dalai_lama, normalize_person_names, apply_flagged_blobs,
)
from catalogue.services import promote


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "names.db")
    yield conn
    conn.close()


@pytest.mark.parametrize("name,clean,dates", [
    ("Tsongkhapa, 1357-1419", "Tsongkhapa", "1357-1419"),
    ("Patrul Rinpoche (1808-1887)", "Patrul Rinpoche", "1808-1887"),
    ("Khenpo Tsultrim, b. 1934", "Khenpo Tsultrim", "b. 1934"),
    ("Zabs-dkar Tshogs-drug-rang-grol, 1781-1851",
     "Zabs-dkar Tshogs-drug-rang-grol", "1781-1851"),
    ("Someone, 14th cent.", "Someone", "14th cent."),
    ("Dīpaṃkara, 982–1054", "Dīpaṃkara", "982–1054"),
    # left untouched — no real date tail:
    ("Jose Ignacio Cabez6n", "Jose Ignacio Cabez6n", None),
    ("Nāgārjuna", "Nāgārjuna", None),
    ("Karmapa XVI", "Karmapa XVI", None),
    ("Author 2", "Author 2", None),          # lone 1–2 digit, no qualifier → kept
])
def test_split_name_dates(name, clean, dates):
    assert split_name_dates(name) == (clean, dates)


def test_normalize_merges_dated_and_undated(db):
    # Two persons, same name, one carrying dates → one after normalization.
    db.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Zhabs dkar')")
    db.execute("INSERT INTO person (id, primary_name) VALUES (2, 'Zhabs dkar, 1781-1851')")
    for pid, nm in ((1, "Zhabs dkar"), (2, "Zhabs dkar, 1781-1851")):
        db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
                   "VALUES (?, ?, 'english', ?)", (pid, nm, fold_key(nm)))
    # work_contributor pointing at the dated dupe must survive the merge.
    db.execute("INSERT INTO work (id) VALUES (10)")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (10, 2, 'author')")

    out = normalize_person_dates(db)
    assert out["merged"] == 1
    assert db.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1
    row = db.execute("SELECT primary_name, dates FROM person").fetchone()
    assert row == ("Zhabs dkar", "1781-1851")          # name clean, dates kept
    # contributor repointed to the surviving person (id 1)
    assert db.execute("SELECT person_id FROM work_author WHERE work_id=10").fetchone()[0] == 1


def test_normalize_is_idempotent(db):
    db.execute("INSERT INTO person (id, primary_name) VALUES (1, 'Tsongkhapa, 1357-1419')")
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (1, 'Tsongkhapa, 1357-1419', 'english', ?)",
               (fold_key("Tsongkhapa, 1357-1419"),))
    normalize_person_dates(db)
    first = db.execute("SELECT primary_name, dates FROM person").fetchone()
    normalize_person_dates(db)                          # second pass: no change
    assert db.execute("SELECT primary_name, dates FROM person").fetchone() == first
    assert first == ("Tsongkhapa", "1357-1419")


@pytest.mark.parametrize("raw,expected", [
    # high-confidence separators
    ("Wallace, B. Alan; Sogyal; Wallace, B. Alan", ["B. Alan Wallace", "Sogyal"]),
    ("Great Vajradhara; Chandrakirti; Campbell, John R. (TRN); Thurman, Robert (TRN)",
     ["Great Vajradhara", "Chandrakirti", "John R. Campbell", "Robert Thurman"]),
    ("Khyentse Rinpoche & Matthieu Ricard", ["Khyentse Rinpoche", "Matthieu Ricard"]),
    # Surname, Given reorder (single comma)
    ("Lopez, Donald S.", ["Donald S. Lopez"]),
    # left intact — single person despite the comma
    ("Seventh Karmapa, Chötra Gyatso", ["Seventh Karmapa, Chötra Gyatso"]),
    ("Bskal-bzang-rgya-mtsho, Dalai Lama VII", ["Bskal-bzang-rgya-mtsho, Dalai Lama VII"]),
    ("Nāgārjuna", ["Nāgārjuna"]),
    # ambiguous bare-comma blob is NOT auto-split (flagged separately)
    ("Je Tsongkhapa, Gavin Kilty", ["Je Tsongkhapa, Gavin Kilty"]),
])
def test_split_contributors(raw, expected):
    assert split_contributors(raw) == expected


def test_is_ambiguous_blob():
    assert is_ambiguous_blob("Je Tsongkhapa, Gavin Kilty")          # two full names, one comma
    assert is_ambiguous_blob("Trijang Rinpoche, Dalai Lama, Sharpa Tulku")  # ≥2 commas
    assert not is_ambiguous_blob("Lopez, Donald S.")               # Surname, Given
    assert not is_ambiguous_blob("Seventh Karmapa, Chötra Gyatso")  # Name, Title
    assert not is_ambiguous_blob("Wallace, B. Alan; Sogyal")        # splits cleanly
    assert not is_ambiguous_blob("Nāgārjuna")


def test_split_existing_contributors_dry_run_and_apply(db):
    # A blob author linked to a work; dry-run plans, apply splits + relinks.
    pid = db.execute("INSERT INTO person (primary_name) VALUES "
                     "('Chandrakirti; Thurman, Robert (TRN)')").lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)",
               (pid, "Chandrakirti; Thurman, Robert (TRN)",
                fold_key("Chandrakirti; Thurman, Robert (TRN)")))
    db.execute("INSERT INTO work (id) VALUES (10)")
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (10, ?, 'author')", (pid,))

    dry = split_existing_contributors(db, apply=False)
    assert dry["blobs_split"] == 1 and dry["applied"] is False
    assert db.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1   # unchanged

    split_existing_contributors(db, apply=True)
    names = sorted(r[0] for r in db.execute("SELECT primary_name FROM person"))
    assert names == ["Chandrakirti", "Robert Thurman"]
    # both linked to the work as authors, blob gone
    linked = sorted(r[0] for r in db.execute(
        "SELECT p.primary_name FROM work_author wa "
        "JOIN person p ON p.id=wa.person_id WHERE wa.work_id=10"))
    assert linked == ["Chandrakirti", "Robert Thurman"]


def test_promotion_splits_blob_authors(db):
    eid = db.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    hid = db.execute("INSERT INTO holding (edition_id, form, text_status) "
                     "VALUES (?, 'electronic', 'ocr_good')", (eid,)).lastrowid
    import json
    rid = db.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('book_toc_pattern', ?)",
        (json.dumps({"holding_id": hid, "structure": "single_work",
                     "book_authors": ["Gampopa & Jamgön Kongtrul"], "book_translators": [],
                     "works": [{"title": "T", "authors": ["Gampopa & Jamgön Kongtrul"],
                                "translators": [], "kind": "work"}]}),),
    ).lastrowid
    promote.promote_proposal(db, rid)
    names = sorted(r[0] for r in db.execute("SELECT primary_name FROM person"))
    assert names == ["Gampopa", "Jamgön Kongtrul"]


@pytest.mark.parametrize("name,canon", [
    ("Dalai Lama", "Dalai Lama XIV"),
    ("the Dalai Lama", "Dalai Lama XIV"),
    ("H.H. the Dalai Lama, Tenzin Gyatso", "Dalai Lama XIV"),
    ("Tenzin Gyatso", "Dalai Lama XIV"),
    ("Fourteenth Dalai Lama", "Dalai Lama XIV"),
    ("His Holiness the XIV Dalai Lama", "Dalai Lama XIV"),
    ("Seventh Dalai Lama", "Dalai Lama VII"),
    ("Dalai Lama VII", "Dalai Lama VII"),
    ("Bskal-bzang-rgya-mtsho, Dalai Lama VII", "Dalai Lama VII"),
    ("Fifth Dalai Lama", "Dalai Lama V"),
    # not a Dalai Lama → no mapping
    ("Thubten Chodron", None),
    ("Tsongkhapa", None),
])
def test_canonical_dalai_lama(name, canon):
    assert canonical_dalai_lama(name) == canon


def test_normalize_reorders_and_merges_inverted_dups(db):
    # Three spellings of one person → reordered + merged to one.
    for nm in ("Lopez, Donald S.", "Lopez, Donald S. Jr", "Donald S. Lopez, Jr"):
        pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (nm,)).lastrowid
        db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
                   "VALUES (?, ?, 'english', ?)", (pid, nm, fold_key(nm)))
    out = normalize_person_names(db, apply=True)
    names = [r[0] for r in db.execute("SELECT primary_name FROM person")]
    # "Lopez, Donald S." → "Donald S. Lopez"; the "Jr" variants fold together
    assert all("," not in n or n.endswith("Jr") for n in names)
    assert len(names) < 3                       # at least some merged


def test_normalize_canonicalizes_and_merges_dalai_lamas(db):
    for nm in ("Dalai Lama", "Tenzin Gyatso", "His Holiness the Dalai Lama"):
        pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (nm,)).lastrowid
        db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
                   "VALUES (?, ?, 'english', ?)", (pid, nm, fold_key(nm)))
    # a different incarnation must stay separate
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Seventh Dalai Lama')").lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Seventh Dalai Lama', 'english', ?)", (pid, fold_key("Seventh Dalai Lama")))
    normalize_person_names(db, apply=True)
    names = sorted(r[0] for r in db.execute("SELECT primary_name FROM person"))
    assert names == ["Dalai Lama VII", "Dalai Lama XIV"]


def test_apply_flagged_blobs_splits_with_roles(db):
    # The Je Tsongkhapa / Gavin Kilty blob → author + translator, OCR fixed.
    eid = db.execute("INSERT INTO edition (title) VALUES ('Autumn Moon')").lastrowid
    db.execute("INSERT INTO work (id) VALUES (50)")
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, 50, 1)", (eid,))
    bpid = db.execute("INSERT INTO person (primary_name) VALUES ('Je Tsongkhapa, Gavin Kitty')").lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Je Tsongkhapa, Gavin Kitty', 'english', ?)",
               (bpid, fold_key("Je Tsongkhapa, Gavin Kitty")))
    db.execute("INSERT INTO work_author (work_id, person_id, role) VALUES (50, ?, 'author')", (bpid,))

    apply_flagged_blobs(db)
    # author on the work (work_author), translator on the work's edition (edition_translator)
    authors = dict(db.execute(
        "SELECT p.primary_name, wa.role FROM work_author wa "
        "JOIN person p ON p.id=wa.person_id WHERE wa.work_id=50").fetchall())
    assert authors == {"Tsongkhapa": "author"}
    trans = [r[0] for r in db.execute(
        "SELECT p.primary_name FROM edition_translator et "
        "JOIN person p ON p.id=et.person_id WHERE et.edition_id=?", (eid,)).fetchall()]
    assert trans == ["Gavin Kilty"]
    # the blob row is gone, OCR "Kitty" kept as a searchable alias of Kilty
    assert not db.execute("SELECT 1 FROM person WHERE primary_name LIKE '%Kitty%'").fetchone()
    kilty = db.execute("SELECT id FROM person WHERE primary_name='Gavin Kilty'").fetchone()[0]
    keys = {x[0] for x in db.execute("SELECT normalized_key FROM person_alias WHERE person_id=?", (kilty,))}
    assert fold_key("Gavin Kitty") in keys


def test_promotion_stores_clean_name_and_dates(db):
    # A proposal whose author carries dates → person row has clean name + dates.
    eid = db.execute("INSERT INTO edition (title) VALUES ('Bk')").lastrowid
    hid = db.execute("INSERT INTO holding (edition_id, form, text_status) "
                     "VALUES (?, 'electronic', 'ocr_good')", (eid,)).lastrowid
    import json
    rid = db.execute(
        "INSERT INTO review_queue (item_type, payload_json) VALUES ('book_toc_pattern', ?)",
        (json.dumps({"holding_id": hid, "structure": "single_work",
                     "book_authors": ["Longchenpa, 1308-1364"], "book_translators": [],
                     "works": [{"title": "T", "authors": ["Longchenpa, 1308-1364"],
                                "translators": [], "kind": "work"}]}),),
    ).lastrowid
    promote.promote_proposal(db, rid)
    row = db.execute("SELECT primary_name, dates FROM person").fetchone()
    assert row == ("Longchenpa", "1308-1364")


# ── organizations misfiled as persons ─────────────────────────────────────────
def test_is_organization_name():
    from catalogue.services.names import is_organization_name, reload_org_markers
    reload_org_markers()
    assert is_organization_name("Padmakara Translation Group")
    assert is_organization_name("THE PADMAKARA TRANSLATION GROUP")   # case-insensitive
    assert is_organization_name("Dharmachakra Translation Committee")
    assert not is_organization_name("Tenzin Gyatso")
    assert not is_organization_name("Nāgārjuna")


def test_mark_organizations_flags_and_is_dryrunnable():
    from catalogue.services.names import mark_organizations
    db = init_db(":memory:")
    org = db.execute("INSERT INTO person (primary_name) VALUES "
                     "('Marpa Translation Society')").lastrowid
    person = db.execute("INSERT INTO person (primary_name) VALUES ('Milarepa')").lastrowid
    bound = db.execute("INSERT INTO person (primary_name, external_id, verification_status) "
                       "VALUES ('Padmakara Group', 'Q1', 'verified')").lastrowid
    db.commit()
    # dry-run: reports the match, writes nothing
    dry = mark_organizations(db, apply=False)
    assert dry["matched"] == 1 and not dry["applied"]
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (org,)).fetchone()[0] == "provisional"
    # apply: only the provisional org flips; the person + the bound row are untouched
    rep = mark_organizations(db, apply=True)
    assert rep["matched"] == 1 and rep["applied"]
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (org,)).fetchone()[0] == "organization"
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (person,)).fetchone()[0] == "provisional"
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (bound,)).fetchone()[0] == "verified"


def test_names_batch_jobs_skip_tombstoned():
    """The name-cleanup batch jobs scan LIVE persons only, so a soft-deleted person is
    never re-marked, repointed, or hard-deleted (which would destroy its restore)."""
    from catalogue.services.names import mark_organizations, normalize_person_dates
    from catalogue.services import contributor_edit as CE
    from catalogue.db_store import add_alias
    db = init_db(":memory:")
    org = db.execute("INSERT INTO person (primary_name, verification_status) "
                     "VALUES ('Ghost Translation Society','provisional')").lastrowid
    add_alias(db, "person", org, "Ghost Translation Society", "english")
    db.commit()
    CE.apply_delete(db, org)                                       # tombstone
    assert mark_organizations(db, apply=True)["matched"] == 0      # not on the live worklist
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (org,)).fetchone()[0] == "provisional"       # status untouched
    normalize_person_dates(db)
    assert db.execute("SELECT deleted_at FROM person WHERE id=?",
                      (org,)).fetchone()[0] is not None            # still tombstoned, not hard-deleted


def test_set_person_kind_toggles_and_protects_bound():
    from catalogue.services.names import set_person_kind
    db = init_db(":memory:")
    p = db.execute("INSERT INTO person (primary_name) VALUES ('Some Committee')").lastrowid
    bound = db.execute("INSERT INTO person (primary_name, external_id, verification_status) "
                       "VALUES ('Atisha', 'Q1', 'verified')").lastrowid
    db.commit()
    assert set_person_kind(db, p, organization=True)
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (p,)).fetchone()[0] == "organization"
    assert set_person_kind(db, p, organization=False)          # revert
    assert db.execute("SELECT verification_status FROM person WHERE id=?",
                      (p,)).fetchone()[0] == "provisional"
    assert not set_person_kind(db, bound, organization=True)   # never touch a bound row


def test_org_marker_does_not_misfire_on_person_names():
    """Regression: 'Arya Asanga' (the master Asaṅga) must NOT be flagged an org. The
    earlier fold_key substring match collapsed 'sangha'→'sanga' and cross-word-matched
    'a-sanga'. Word-boundary matching on a plain (non-digraph-folded) form fixes it."""
    from catalogue.services.names import is_organization_name, reload_org_markers
    reload_org_markers()
    for person in ("Arya Asanga", "Asanga", "Asaṅga", "Tenzin Gyatso", "Nāgārjuna"):
        assert not is_organization_name(person), person
    for org in ("Padmakara Translation Group", "Dharmachakra Translation Committee"):
        assert is_organization_name(org), org
