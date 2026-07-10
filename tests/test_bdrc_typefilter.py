"""BDRC BLMP returns a name-ranked list across ALL resource types, so the top
hit is often the wrong type (a work for a person query). The resolver must filter
to the caller's entity type BEFORE picking the top hit — otherwise a person like
"Nagarjuna" (whose top BLMP hit is a work) resolves to nothing.
"""
from __future__ import annotations

from catalogue.db_store import init_db
from catalogue.services.bdrc import BDRCClient
from catalogue.services.work_canonical_resolver import LiveResolver


def _sparql(*pairs):
    """Build a BLMP SPARQL-JSON response from (local_id, literal) pairs."""
    return {"results": {"bindings": [
        {"s": {"value": f"http://purl.bdrc.io/resource/{lid}"},
         "lit": {"value": lit}}
        for lid, lit in pairs
    ]}}


def _client(response):
    return BDRCClient(transport=lambda url: response)


def test_person_query_skips_work_typed_top_hit(tmp_path):
    db = init_db(tmp_path / "t.db")
    # Top hit is a WORK; the person sits at #2 — exactly the "Nagarjuna" shape.
    client = _client(_sparql(
        ("WA0RTI1253", "ātmaparijñānadṛṣṭyupadeśa"),   # work, ranked first
        ("P4954", "Nāgārjuna"),                          # the person we want
    ))
    res = LiveResolver(bdrc=client).resolve_person(db, "Nagarjuna")
    assert res is not None
    assert res.canonical_number == "bdr:P4954"
    assert res.canonical_name == "Nāgārjuna"


def test_work_query_skips_person_typed_top_hit(tmp_path):
    db = init_db(tmp_path / "t.db")
    client = _client(_sparql(
        ("P4954", "Nāgārjuna"),                          # person, ranked first
        ("WA0RTI1253", "Mūlamadhyamakakārikā"),          # the work we want
    ))
    res = LiveResolver(bdrc=client).resolve_work(db, "Mulamadhyamakakarika")
    assert res is not None
    assert res.canonical_number == "bdr:WA0RTI1253"


def test_person_query_with_no_person_hit_returns_none(tmp_path):
    db = init_db(tmp_path / "t.db")
    # Only work-typed hits → a person query must NOT fall back to a work.
    client = _client(_sparql(("WA0RTI1253", "some work"),
                             ("MW123", "another work")))
    assert LiveResolver(bdrc=client).resolve_person(db, "Whatever") is None
