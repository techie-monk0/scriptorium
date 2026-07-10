"""scan/OCR provenance through the bound gateway (`acc.scan_ocr`).

The write path: reads run over the RO connection, writes over RW and commit through the gateway, and
the policy gate fires for both — the scan_ocr module stays the owner of the supersede/repoint logic.
Uses the test-kit fixtures + sample seeder. See access_api/scan_ocr_access.py.
"""
import pytest

from catalogue.contracts import AccessMode, Denied
from catalogue.test_kit import DenyAll, RecordingPolicy, seed_minimal


def test_record_event_then_read_provenance(cat_conn, cat_acc):
    hid = seed_minimal(cat_conn)["holding"]
    cat_conn.commit()
    ev = cat_acc.scan_ocr.writes.record_event(
        hid, stage="ocr", engine="tesseract_iast", evidence="pipeline")
    assert ev.stage == "ocr" and ev.engine == "tesseract_iast"
    prov = cat_acc.scan_ocr.reads.provenance(hid)        # read-back over the RO connection
    assert prov.ocr is not None and prov.ocr.id == ev.id


def test_record_event_supersedes_prior(cat_conn, cat_acc):
    hid = seed_minimal(cat_conn)["holding"]
    cat_conn.commit()
    cat_acc.scan_ocr.writes.record_event(hid, stage="ocr", engine="tesseract_iast")
    second = cat_acc.scan_ocr.writes.record_event(hid, stage="ocr", engine="gcv")
    assert cat_acc.scan_ocr.reads.latest(hid, "ocr").id == second.id
    assert len(cat_acc.scan_ocr.reads.events(hid, stage="ocr")) == 2


def test_set_provenance_kind(cat_conn, cat_acc):
    hid = seed_minimal(cat_conn)["holding"]
    cat_conn.commit()
    cat_acc.scan_ocr.writes.set_provenance_kind(hid, "born_digital")
    assert cat_acc.scan_ocr.reads.provenance(hid).kind == "born_digital"


def test_engines_and_kinds_list(cat_acc):
    assert any(code == "tesseract_iast" for code, _s, _l in cat_acc.scan_ocr.reads.engines("ocr"))
    assert any(code == "born_digital" for code, _l in cat_acc.scan_ocr.reads.provenance_kinds())


def test_reads_are_read_gated_and_declare_mode(cat_acc):
    rec = RecordingPolicy()
    cat_acc.policy = rec
    cat_acc.scan_ocr.reads.engines("ocr")
    a = rec.actions()[-1]
    assert (a.resource, a.verb, a.mode) == ("scan_ocr", "engines", AccessMode.READ)


def test_writes_are_denied_by_policy(cat_conn, cat_acc):
    hid = seed_minimal(cat_conn)["holding"]
    cat_conn.commit()
    cat_acc.policy = DenyAll()
    with pytest.raises(Denied):
        cat_acc.scan_ocr.writes.record_event(hid, stage="ocr", engine="tesseract_iast")
