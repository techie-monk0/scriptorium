"""ANNOTATED skip rule (catalogue/skip.py) + the needs-work 'skipped' tier.

Standing operator rule: a book whose edition title, or whose holdings' folder,
carries 'ANNOTATED' is kept OUT of the normal catalogue — excluded from the
resolve/classify batch and surfaced as a dedicated 'skipped' tier so it is
never mistaken for missing/unprocessed data.
"""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db
from catalogue.services.needs_work import tier_editions
from catalogue.services.skip import is_skipped


# ── is_skipped ──────────────────────────────────────────────────────────────
def test_is_skipped_matches_path_and_title_case_insensitively():
    assert is_skipped(file_path="/x/00 Foo ANNOTATED— bar/ch.pdf")
    assert is_skipped(title="Some annotated edition")           # lowercase too
    assert is_skipped(title="Book", file_path="/x/ANNOTATED/y.pdf")


def test_is_skipped_false_for_normal_book():
    assert not is_skipped(title="Illuminating the Intent — Jinpa",
                          file_path="/x/00 Emptiness/LTK/intent.pdf")
    assert not is_skipped()                                      # nothing given


# ── needs-work 'skipped' tier ────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "skip.db")
    yield conn
    conn.close()


def _edition(db, eid, title):
    db.execute("INSERT INTO edition (id, title) VALUES (?, ?)", (eid, title))


def _holding(db, eid, *, form="electronic", status="ocr_good", path=None):
    db.execute(
        "INSERT INTO holding (edition_id, form, file_path, text_status) "
        "VALUES (?, ?, ?, ?)", (eid, form, path, status))


def test_skipped_tier_routes_annotated_and_spares_the_rest(db):
    _edition(db, 1, "Normal Book");        _holding(db, 1, path="/x/normal.pdf")
    _edition(db, 2, "Annotated Folder");   _holding(db, 2, path="/x/00 A ANNOTATED— b/ch.pdf")
    _edition(db, 3, "A Study, ANNOTATED"); _holding(db, 3, path="/x/plain.pdf")
    _edition(db, 4, "Orphan Edition")      # no holdings
    _edition(db, 5, "Physical Only");      _holding(db, 5, form="physical", status=None)
    db.commit()

    rep = tier_editions(db)
    assert rep.counts == {"digitize": 1, "reocr": 0, "clean": 1,
                          "orphan": 1, "skipped": 2}
    skipped_ids = {e.id for e in rep.skipped}
    assert skipped_ids == {2, 3}                       # folder-path + title
    # an ANNOTATED book must NOT leak into clean/orphan (the "missing data" trap)
    assert all(e.id not in skipped_ids for e in rep.clean + rep.orphan)


def test_image_only_annotated_is_skipped_not_digitize(db):
    # the 2 image_only ANNOTATED holdings must not be queued for digitization
    _edition(db, 1, "Annotated Scan")
    _holding(db, 1, status="image_only", path="/x/ANNOTATED— set/scan.pdf")
    db.commit()
    rep = tier_editions(db)
    assert rep.counts["skipped"] == 1 and rep.counts["reocr"] == 0
