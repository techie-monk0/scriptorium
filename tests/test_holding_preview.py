"""Title-page preview: render the first page of a holding's file (PDF/EPUB) as PNG,
shown (and clickable to open) at the bottom of the edition detail pane."""
import fitz
import pytest

from catalogue.db_store import connect
from catalogue.services import work_detect as WD
from catalogue.webui.web import create_app


def test_pdf_title_page_preview(tmp_path):
    pdf = tmp_path / "book.pdf"
    doc = fitz.open(); page = doc.new_page(); page.insert_text((72, 100), "TITLE PAGE")
    doc.save(str(pdf)); doc.close()

    app = create_app(tmp_path / "cat.db", ingest_verify=False)
    app.testing = True
    db = connect(app.config["DB_PATH"])
    eid = db.execute("INSERT INTO edition (title, structure) VALUES ('Bk', 'single_work')").lastrowid
    hid = db.execute("INSERT INTO holding (edition_id, form, file_path) VALUES (?, 'electronic', ?)",
                     (eid, str(pdf))).lastrowid
    nofile = db.execute("INSERT INTO holding (edition_id, form, file_path) "
                        "VALUES (?, 'physical', NULL)", (eid,)).lastrowid
    WD.store_detection(db, eid, "single", WD.detect_single(db, eid, classical=lambda c: {"english": "Bk"}))
    db.commit()

    with app.test_client() as c:
        r = c.get(f"/holding/{hid}/preview.png")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:4] == b"\x89PNG"
        assert c.get(f"/holding/{nofile}/preview.png").status_code == 404      # no file → 404
        assert c.get("/holding/999999/preview.png").status_code == 404         # missing → 404
        # the clickable preview renders in the detail pane (below the works, via
        # _edition_extras), no longer inside the editable Edition Basics card
        pane = c.get("/works/detect/single").data.decode()
    assert f"/holding/{hid}/preview.png" in pane and "Title page" in pane
