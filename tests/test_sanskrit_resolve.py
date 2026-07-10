"""The Sanskrit verify gate + the Toh by_sanskrit/by_tibetan index maps + the
live_classical Sanskrit path. Hermetic — the Toh index and BDRC search are
constructed/injected, no snapshot or network."""
from catalogue.db_store import init_db
from catalogue.services import sanskrit_resolve as SR
from catalogue.services import work_detect as WD
from catalogue.services.work_canonical_resolver import EightyFourThousandIndex, _native_key


def _index(entries):
    """An EightyFourThousandIndex with a hand-built in-memory index (no snapshot).
    Keys match the real rebuild (separator-insensitive `_native_key`)."""
    idx = EightyFourThousandIndex()
    by_sa, by_bo = {}, {}
    for e in entries:
        if e.get("sanskrit"):
            by_sa[_native_key(e["sanskrit"])] = e
        if e.get("tibetan"):
            by_bo[_native_key(e["tibetan"])] = e
    idx._index = {"by_toh": {e["toh"]: e for e in entries}, "by_english": {},
                  "by_sanskrit": by_sa, "by_tibetan": by_bo}
    return idx


MMK = {"toh": "3824", "english": "The Root Stanzas on the Middle Way",
       "sanskrit": "Mūlamadhyamakakārikā", "tibetan": "dbu ma rtsa ba"}


def test_index_by_sanskrit_folds_variants():
    idx = _index([MMK])
    assert idx.by_sanskrit("Mūlamadhyamakakārikā")["toh"] == "3824"
    assert idx.by_sanskrit("mulamadhyamakakarika")["toh"] == "3824"   # diacritic fold
    assert idx.by_sanskrit("Totally Other") is None
    assert idx.by_tibetan("dbu ma rtsa ba")["toh"] == "3824"


def test_verify_sanskrit_toh_hit():
    idx = _index([MMK])
    v = SR.verify_sanskrit("Mulamadhyamakakarika", toh_index=idx)
    assert v["matched"] and v["system"] == "toh" and v["number"] == "3824"
    assert v["english"] == MMK["english"] and v["tibetan"] == MMK["tibetan"]
    assert v["confidence"] >= 0.9


def test_verify_sanskrit_bdrc_fallback():
    idx = _index([])                                   # no Toh hit
    def fake_search(title, author=None):
        return [{"id": "bdr:WA0RK", "score": 9.0,
                 "titles": ["mulamadhyamakakarika"], "authors": []}]
    v = SR.verify_sanskrit("mulamadhyamakakarika", toh_index=idx, bdrc_search=fake_search)
    assert v["matched"] and v["system"] == "bdrc" and v["number"] == "bdr:WA0RK"


def test_verify_sanskrit_no_match():
    v = SR.verify_sanskrit("mulamadhyamakakarika", toh_index=_index([]),
                           bdrc_search=lambda t, a=None: [])
    assert not v["matched"]


def test_live_classical_resolves_sanskrit(tmp_path):
    db = init_db(tmp_path / "c.db")
    # An edition whose title carries the IAST title in a structural slot.
    eid = db.execute("INSERT INTO edition (title, structure) VALUES "
                     "('Fundamental Wisdom (Mūlamadhyamakakārikā)', 'single_work')").lastrowid
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/x.pdf')", (eid,))
    resolve = WD.live_classical(toh_index=_index([MMK]), bdrc_work_search=None)
    res = WD.detect_single(db, eid, classical=resolve)
    assert res["determination"] == "classical"
    assert res["canonical"]["system"] == "toh" and res["canonical"]["number"] == "3824"
    assert res["title"]["tibetan"] == "dbu ma rtsa ba"
