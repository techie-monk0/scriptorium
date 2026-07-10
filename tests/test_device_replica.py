"""Device-local replica export + sync API contract, and the storage provider seam.

Black-box: builds a small catalogue, exports the replica, and asserts the client-facing
shape (one row per edition, lookup fields, per-holding StorageRef) and the `/api/v1/*`
contract (replica + ETag/304 + health). The provider is a fake so `open_url` is
deterministic and no network is touched — proving the provider seam is honored.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import add_alias, connect, init_db
from catalogue.services import export_replica as ER
from catalogue.services import storage as ST
from catalogue.webui.web import create_app


# ── a fake provider: proves the seam (no kDrive, no network) ───────────────────
class _FakeProvider(ST.StoragePort):
    name = "fake"

    def covers(self, local_path):
        return local_path.endswith((".pdf", ".epub"))

    def locator(self, local_path):
        if not self.covers(local_path):
            return None
        return ST.StorageRef("fake", relpath=local_path.split("/")[-1],
                             open_url=f"https://example/open?p={local_path.split('/')[-1]}")


# ── small catalogue fixture ────────────────────────────────────────────────────
@pytest.fixture
def db(tmp_path):
    init_db(tmp_path / "cat.db").close()
    return connect(tmp_path / "cat.db")


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    add_alias(db, "person", pid, name, "english")
    return pid


def _book(db, title, *, author=None, isbn=None, subject=None, file_path=None, form="electronic"):
    eid = db.execute("INSERT INTO edition (title, isbn) VALUES (?, ?)", (title, isbn)).lastrowid
    if author:
        db.execute("INSERT INTO edition_author (edition_id, person_id, seq) VALUES (?, ?, 1)",
                   (eid, _person(db, author)))
    if subject:
        sid = db.execute("INSERT INTO subject (name) VALUES (?)", (subject,)).lastrowid
        db.execute("INSERT INTO edition_subject (edition_id, subject_id) VALUES (?, ?)", (eid, sid))
    db.execute("INSERT INTO holding (edition_id, file_path, form) VALUES (?, ?, ?)",
               (eid, file_path, form))
    db.commit()
    return eid


# ── export shape ───────────────────────────────────────────────────────────────
def test_one_row_per_edition_with_lookup_fields(db):
    _book(db, "Words of My Perfect Teacher", author="Patrul Rinpoche",
          isbn="9780300165326", subject="Dzogchen", file_path="/lib/wopt.pdf")
    doc = ER.build_replica(db, provider=_FakeProvider(), exported_at="t0")

    assert doc["schema_version"] == ER.SCHEMA_VERSION
    assert doc["exported_at"] == "t0"
    assert doc["count"] == 1
    row = doc["editions"][0]
    assert row["title"] == "Words of My Perfect Teacher"
    assert row["display_title"] == "Words of My Perfect Teacher"   # no volume → unchanged
    assert row["authors"] == ["Patrul Rinpoche"]
    assert row["isbns"] == ["9780300165326"]
    assert row["subjects"] == ["Dzogchen"]
    assert row["cover_url"] == f"/edition/{row['edition_id']}/cover.jpg"   # opaque art handles
    assert row["spine_url"] == f"/edition/{row['edition_id']}/spine.svg"
    # v4 home-rail primitives: when-added key + series namespace (empty here).
    assert row["date_added"] is not None          # the holding gives an entry timestamp
    assert row["series"] == []                     # no series tag on this book
    # folded search blob is accent/case-insensitive and contains the lookup tokens
    assert "patrul" in row["search_text"] and "9780300165326" in row["search_text"]
    # doc-level topic forest (the Tier-2 source for home SUBJECT rails) is present.
    assert any(n["name"] == "Dzogchen" for n in doc["subject_forest"])


def test_search_text_includes_all_work_aliases(db):
    """v4: EVERY work-alias spelling is folded into search_text, so a client's single-box lookup finds
    a book by an alternate spelling of its work (e.g. the Sanskrit alias) the edition title doesn't use
    — the cross-frontend search-parity fix (web's Work box already matched aliases)."""
    import unicodedata
    eid = db.execute("INSERT INTO edition (title) VALUES ('The Way of the Bodhicharyavatara')").lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    add_alias(db, "work", wid, "Bodhicharyāvatāra", "english")
    add_alias(db, "work", wid, "Bodhicaryāvatāra", "other")        # the alternate (c) spelling
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    db.commit()
    st = ER.build_replica(db, provider=None)["editions"][0]["search_text"]
    # The CLIENT matcher strips combining marks; assert against the same mark-stripped form.
    stripped = "".join(c for c in unicodedata.normalize("NFKD", st) if not unicodedata.combining(c))
    assert "bodhicharyavatara" in stripped      # the edition/display spelling
    assert "bodhicaryavatara" in stripped       # the alternate work alias — now searchable too


def test_replica_carries_series_for_home_rail(db):
    """v4: the replica exposes each edition's SERIES membership (the namespace `subjects`
    drops) so a client builds the home "Series" rail itself — no server-composed payload."""
    from catalogue.services import subjects as S
    eid = _book(db, "Liberation in the Palm", subject="Lamrim", file_path="/lib/lp.pdf")
    S.add_subject(db, "edition", eid, "Lam Rim Teachings", subject_kind="series")
    db.commit()
    row = ER.build_replica(db, provider=_FakeProvider())["editions"][0]
    assert row["series"] == ["Lam Rim Teachings"]
    assert row["subjects"] == ["Lamrim"]           # series stays OUT of the topic facet


def test_holding_carries_storage_ref_from_provider(db):
    _book(db, "The Jewel Ornament", file_path="/lib/jewel.epub")
    row = ER.build_replica(db, provider=_FakeProvider())["editions"][0]
    h = row["holdings"][0]
    assert h["has_file"] is True
    assert h["kind"] == "epub"                      # reader dispatch key
    assert h["storage"]["provider"] == "fake"
    assert h["storage"]["relpath"] == "jewel.epub"
    assert h["storage"]["open_url"].endswith("jewel.epub")


def test_no_provider_means_null_storage_not_crash(db):
    _book(db, "No Cloud Book", file_path="/lib/x.pdf")
    row = ER.build_replica(db, provider=None)["editions"][0]
    assert row["holdings"][0]["storage"] is None
    assert row["holdings"][0]["has_file"] is True


def test_fileless_holding_has_no_storage(db):
    _book(db, "Catalogued But Not Held", file_path=None)
    row = ER.build_replica(db, provider=_FakeProvider())["editions"][0]
    assert row["holdings"][0]["has_file"] is False
    assert row["holdings"][0]["storage"] is None


# ── provider seam: kDrive URL template builds from a resolver, never hardcoded ──
def test_kdrive_open_url_template():
    from catalogue.services.webdav import Mount, WebDAVClient

    class R(ST.FileIdResolver):
        def resolve(self, relpath, *, drive_id):
            return ("279", "272", "pdf")

    prov = ST.KDriveProvider(Mount("/root", WebDAVClient("https://x"), name="kdrive"),
                             drive_id="2451995", resolver=R())
    ref = prov.locator("/root/sub/book.pdf")
    assert ref.open_url == ("https://ksuite.infomaniak.com/all/kdrive/app/drive/"
                            "2451995/files/272/preview/pdf/279")
    assert ref.relpath == "sub/book.pdf"


def test_kdrive_open_url_null_when_resolver_cant():
    from catalogue.services.webdav import Mount, WebDAVClient
    prov = ST.KDriveProvider(Mount("/root", WebDAVClient("https://x"), name="kdrive"),
                             drive_id="2451995")  # NullFileIdResolver
    ref = prov.locator("/root/book.pdf")
    assert ref.relpath == "book.pdf"
    assert ref.open_url is None  # client falls back to streaming


# ── HTTP contract: /api/v1/* ───────────────────────────────────────────────────
@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    with app.test_client() as c:
        yield c, app


def test_health(client):
    c, _ = client
    r = c.get("/api/v1/health")
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_replica_endpoint_and_etag_304(client):
    c, app = client
    db = connect(app.config["DB_PATH"])
    _book(db, "Mind in Comfort and Ease", isbn="9780861712786", file_path="/lib/m.pdf")

    r = c.get("/api/v1/replica")
    assert r.status_code == 200
    doc = json.loads(r.data)
    assert doc["count"] == 1 and doc["editions"][0]["isbns"] == ["9780861712786"]
    etag = r.headers["ETag"]
    assert etag

    # unchanged content → 304 even though exported_at is re-stamped each call
    r2 = c.get("/api/v1/replica", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_capture_endpoint_idempotent(client):
    c, _ = client
    payload = {"isbn": "9780262033848"}            # valid ISBN-13
    r1 = c.post("/api/v1/capture", json=payload)
    assert r1.status_code == 201
    body = r1.get_json()
    assert body["status"] == "ok" and body["duplicate"] is False
    # re-flush of the same scan dedupes (append-only, no double row)
    r2 = c.post("/api/v1/capture", json=payload)
    assert r2.status_code == 201 and r2.get_json()["duplicate"] is True


def test_capture_rejects_bad_isbn(client):
    c, _ = client
    r = c.post("/api/v1/capture", json={"isbn": "nope"})
    assert r.status_code == 422


def test_pwa_assets_served(client):
    c, _ = client
    assert c.get("/app").status_code == 200
    sw = c.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers.get("Service-Worker-Allowed") == "/"
    assert "application/javascript" in sw.headers.get("Content-Type", "")
    man = c.get("/manifest.webmanifest")
    assert man.status_code == 200 and man.get_json()["start_url"] == "/app"
