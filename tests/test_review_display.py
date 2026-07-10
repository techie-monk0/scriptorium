"""Review-pane display polish: file paths shown relative to the configured library
root, native-only original title, saved-only contributor names."""
import json

import pytest

from catalogue.db_store import connect
from catalogue.services import work_detect as WD, features
from catalogue.webui.web import create_app


def test_rel_path_strips_configured_root(monkeypatch, tmp_path):
    cfg = tmp_path / "vocab.json"
    cfg.write_text(json.dumps({"_library_root": "Users/me/Library/"}), "utf-8")
    monkeypatch.setattr("catalogue.db_store.db.VOCAB_PATH", cfg)
    features.reload()
    try:
        assert features.rel_path("/Users/me/Library/Emptiness/A.pdf") == "Emptiness/A.pdf"
        assert features.rel_path("Users/me/Library/B.pdf") == "B.pdf"     # no leading slash
        assert features.rel_path("/somewhere/else.pdf") == "/somewhere/else.pdf"  # root absent → unchanged
    finally:
        features.reload()


def test_single_report_native_title_saved_names_relative_path(tmp_path, monkeypatch):
    cfg = tmp_path / "vocab.json"
    cfg.write_text(json.dumps({"_library_root": "Library/"}), "utf-8")
    monkeypatch.setattr("catalogue.db_store.db.VOCAB_PATH", cfg)
    features.reload()
    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Nāgārjuna')").lastrowid
    wid = db.execute("INSERT INTO work (work_type) VALUES (NULL)").lastrowid
    db.execute("INSERT INTO work_author (work_id, person_id) VALUES (?, ?)", (wid, pid))
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Fundamental Wisdom', 'single_work')").lastrowid
    db.execute("INSERT INTO edition_work (edition_id, work_id, sequence) VALUES (?, ?, 1)", (eid, wid))
    db.execute("INSERT INTO holding (edition_id, form, file_path) "
               "VALUES (?, 'electronic', '/x/Library/Madhyamaka/MMK.pdf')", (eid,))
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {
        "english": c["title"], "sanskrit": "Mūlamadhyamakakārikā"}))
    db.commit()
    try:
        with app.test_client() as c:
            card = c.get(f"/works/detect/{eid}/edit").data.decode()   # editable Edition Basics
            pane = c.get("/works/detect/single").data.decode()        # full detail pane
    finally:
        features.reload()
    # Holdings + native title moved OUT of Edition Basics: holdings render in the detail
    # pane (_edition_extras), the native title in the read-only "Detected — verify" note.
    assert "Madhyamaka/MMK.pdf" in pane and "/x/Library" not in pane   # path trimmed to subtree
    assert "Mūlamadhyamakakārikā" in pane                                  # native title shown (detection facts)
    assert "Nāgārjuna" in card                                             # edition author still in basics
    assert "from book" not in pane                                         # comparison removed
