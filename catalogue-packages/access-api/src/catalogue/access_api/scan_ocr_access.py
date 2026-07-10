"""Scan/OCR provenance — the gateway-bound access surface over the `scan_ocr` module.

`scan_ocr.py` is the standalone typed layer (functions over a raw connection, caller owns the txn).
This wraps it as a policy-gated repo on the gateway (`acc.scan_ocr`) so a bound client reads/writes
provenance the same way it does every other entity: `.reads` run over the RO connection (a read path
physically cannot write), `.writes` run over RW and commit through the gateway's unit-of-work seam.
The module functions stay the single owner of the supersede/repoint logic; this layer only adds
authz, connection routing, and transaction management. See entity_api_model.md §8/§9.
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

from . import scan_ocr

_RESOURCE = "scan_ocr"


class _Reads:
    def __init__(self, access):
        self._a = access

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(_RESOURCE, verb, AccessMode.READ))

    def provenance(self, holding_id: int):
        """A holding's resolved provenance: kind + current capture + current OCR event."""
        self._read("provenance")
        return scan_ocr.provenance(self._a.ro, holding_id)

    def events(self, holding_id: int, *, stage=None, include_superseded: bool = True):
        """All digitization events for a holding (newest first), optionally one stage."""
        self._read("events")
        return scan_ocr.events(self._a.ro, holding_id, stage=stage,
                               include_superseded=include_superseded)

    def latest(self, holding_id: int, stage: str):
        """The current (non-superseded) event for a stage, or None."""
        self._read("latest")
        return scan_ocr.latest(self._a.ro, holding_id, stage)

    def engines(self, stage=None):
        """(code, stage, label) for every registered engine, optionally one stage."""
        self._read("engines")
        return scan_ocr.engines(self._a.ro, stage)

    def provenance_kinds(self):
        """(code, label) for every provenance kind."""
        self._read("provenance_kinds")
        return scan_ocr.provenance_kinds(self._a.ro)


class _Writes:
    def __init__(self, access):
        self._a = access

    def _write(self, verb: str) -> None:
        self._a.authorize(Action(_RESOURCE, verb, AccessMode.WRITE))

    def _commit(self, result):
        try:
            self._a.commit()
        except Exception:
            self._a.rollback()
            raise
        return result

    def record_event(self, holding_id: int, **kwargs):
        """Record a capture/OCR pass and make it current for its stage (one transaction)."""
        self._write("record_event")
        try:
            ev = scan_ocr.record_event(self._a.rw, holding_id, **kwargs)
        except Exception:
            self._a.rollback()
            raise
        return self._commit(ev)

    def set_provenance_kind(self, holding_id: int, kind: str) -> None:
        """Set a holding's provenance kind (born_digital | scanned | downloaded | unknown)."""
        self._write("set_provenance_kind")
        try:
            scan_ocr.set_provenance_kind(self._a.rw, holding_id, kind)
        except Exception:
            self._a.rollback()
            raise
        self._commit(None)

    def register_engine(self, code: str, stage: str, label: str) -> None:
        """Add a capture/OCR engine to the open vocabulary (data, not a migration)."""
        self._write("register_engine")
        try:
            scan_ocr.register_engine(self._a.rw, code, stage, label)
        except Exception:
            self._a.rollback()
            raise
        self._commit(None)


class ScanOcrRepo:
    """`.reads` (queries, READ) + `.writes` (record/set/register, WRITE) over a bound `Access`."""

    def __init__(self, access):
        self.reads = _Reads(access)
        self.writes = _Writes(access)
