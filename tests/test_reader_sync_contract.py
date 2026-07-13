"""reader_sync_contract — the versioned, language-neutral descriptor of the GET/POST /sync/reader
wire surface (bookmarks + annotations) that every reader client speaks (web, PWA, and the native
app's postilla AnnotationStore adapter).

These are the *provider-side* guarantees: the descriptor cannot lie about the running store code
(every declared record field is a real reader_state dataclass field; every declared op field is a
real apply_* parameter), the API advertises the descriptor's version, and required is a subset of
declared. See docs/frontend/sync_architecture.md.
"""
from __future__ import annotations

import dataclasses

import pytest

from catalogue.db_store import (
    READER_SYNC_CONTRACT_VERSION,
    reader_sync_contract_descriptor,
    reader_sync_contract_version_payload,
    verify_reader_sync_contract,
)
from catalogue.db_store import reader_sync_contract
from catalogue.db_store.reader_state import Annotation, Bookmark, ReaderStateStore


def test_api_advertises_the_descriptor_version():
    assert reader_sync_contract_version_payload() == {"contract_version": READER_SYNC_CONTRACT_VERSION}
    assert READER_SYNC_CONTRACT_VERSION == reader_sync_contract_descriptor()["version"]


def test_descriptor_conforms_to_the_live_store_code():
    # The headline guarantee: descriptor ⟷ reader_state dataclasses + apply_* signatures.
    assert verify_reader_sync_contract() == []


def test_record_fields_exist_on_their_dataclass():
    d = reader_sync_contract_descriptor()
    for cls, name in ((Bookmark, "bookmark"), (Annotation, "annotation")):
        declared = set(d["records"][name]["fields"])
        have = {f.name for f in dataclasses.fields(cls)}
        assert declared <= have, f"{name}: descriptor declares fields absent from {cls.__name__}: {declared - have}"


def test_op_fields_are_real_apply_parameters():
    d = reader_sync_contract_descriptor()
    import inspect
    for meth, name in ((ReaderStateStore.apply_bookmark, "bookmark"),
                       (ReaderStateStore.apply_annotation, "annotation")):
        params = set(inspect.signature(meth).parameters)
        declared = set(d["ops"][name]["fields"])
        assert declared <= params, f"{name} op declares fields with no {meth.__name__} param: {declared - params}"


def test_required_is_a_subset_of_declared():
    d = reader_sync_contract_descriptor()
    for name in ("bookmark", "annotation"):
        rec = d["records"][name]
        assert set(rec["required"]) <= set(rec["fields"])
        # `type` is the wire discriminator, not a record field — allow it in op `required`.
        op = d["ops"][name]
        assert set(op["required"]) <= (set(op["fields"]) | {"type"})
    assert d["identity_key"] in d["records"]["bookmark"]["fields"]


def test_verify_catches_a_declared_field_with_no_backing(monkeypatch):
    # Simulate the drift a docstring misses: a descriptor that claims a field the code doesn't have.
    doctored = reader_sync_contract_descriptor()
    doctored["records"]["annotation"]["fields"]["totally_bogus"] = "x"
    monkeypatch.setattr(reader_sync_contract, "descriptor", lambda: doctored)
    problems = reader_sync_contract.verify()
    assert any("totally_bogus" in p for p in problems)
