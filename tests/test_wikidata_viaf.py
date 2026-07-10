"""Tests for the Wikidata + VIAF clients and their pure entity readers.

All offline: transports are injected callables returning canned JSON. These pin
the shapes the verifiers/sources depend on so a future refactor can't silently
change what `is_human` / `labels_and_aliases` / `suggest` extract.
"""
from __future__ import annotations

import catalogue.services.wikidata as W
from catalogue.services.viaf import VIAFClient


# ── canned entities ─────────────────────────────────────────────────────────────
def _person_entity(qid="Q187310", label="Tsongkhapa",
                   bo="ཙོང་ཁ་པ་", ewts="tsong kha pa"):
    return {"entities": {qid: {
        "labels": {"en": {"value": label},
                   "bo": {"value": bo},
                   "bo-x-ewts": {"value": ewts}},
        "aliases": {"en": [{"value": "Je Tsongkhapa"}]},
        "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]},
    }}}


def _work_entity(qid="Q2", title="Lamrim Chenmo", author_qid="Q187310"):
    return {"entities": {qid: {
        "labels": {"en": {"value": title}},
        "claims": {
            "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q571"}}}}],
            "P50": [{"mainsnak": {"datavalue": {"value": {"id": author_qid}}}}],
        },
    }}}


def _transport_map(mapping):
    """Return a transport that dispatches by substring match on the URL."""
    def _t(url):
        for needle, payload in mapping.items():
            if needle in url:
                return payload
        return {}
    return _t


# ── entity readers ──────────────────────────────────────────────────────────────
def test_is_human_and_is_work():
    pe = _person_entity()["entities"]["Q187310"]
    we = _work_entity()["entities"]["Q2"]
    assert W.is_human(pe) and not W.is_work(pe)   # person: human, not a work
    assert W.is_work(we) and not W.is_human(we)   # work: work-class, not human


def test_is_work_accepts_untyped_but_authored():
    ent = {"claims": {"P50": [{"mainsnak": {"datavalue": {"value": {"id": "Q9"}}}}]}}
    assert W.is_work(ent)               # has an author → counts as a work


def test_is_work_accepts_mahayana_sutra_class():
    # The Lankāvatāra regression: an authorless sutra whose class is 'Mahayana sutra'
    # (Q1191035) — was rejected by the old narrow WORK_CLASSES set.
    ent = {"claims": {"P31": [
        {"mainsnak": {"datavalue": {"value": {"id": "Q1191035"}}}}]}}
    assert W.is_work(ent)


def test_is_work_accepts_title_only():
    # Carries a title (P1476) but no author and an unknown class → still a work.
    ent = {"claims": {"P1476": [
        {"mainsnak": {"datavalue": {"value": "Some Text"}}}]}}
    assert W.is_work(ent)


def test_is_work_class_label_hint_fallback():
    # Unknown class Q-id, but its English label matches a hint ('commentary') — only
    # accepted when the caller supplies class_labels (resolving them costs lookups).
    ent = {"claims": {"P31": [
        {"mainsnak": {"datavalue": {"value": {"id": "Q999999"}}}}]}}
    assert not W.is_work(ent)                                    # no labels → reject
    assert W.is_work(ent, class_labels={"Q999999": "Buddhist commentary"})


def test_is_work_rejects_plain_human():
    ent = {"claims": {"P31": [
        {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}]}}
    assert not W.is_work(ent)


def test_labels_and_aliases_pulls_scripts():
    pe = _person_entity()["entities"]["Q187310"]
    primary, aliases = W.labels_and_aliases(pe)
    assert primary == "Tsongkhapa"
    assert "ཙོང་ཁ་པ་" in aliases and "tsong kha pa" in aliases
    assert "Je Tsongkhapa" in aliases


def test_claim_ids_handles_missing_shape():
    assert W.claim_ids({}, "P50") == []
    assert W.claim_ids({"claims": {"P50": [{}]}}, "P50") == []   # no datavalue


# ── client search + entity ──────────────────────────────────────────────────────
def test_wikidata_search_and_entity():
    search_payload = {"search": [{"id": "Q187310", "label": "Tsongkhapa",
                                  "description": "Tibetan teacher"}]}
    client = W.WikidataClient(transport=_transport_map({
        "wbsearchentities": search_payload,
        "ids=Q187310": _person_entity(),
    }))
    hits = client.search("Tsongkhapa")
    assert hits and hits[0][0] == "Q187310"
    ent = client.entity("Q187310")
    assert W.is_human(ent)


def test_wikidata_client_swallows_errors():
    def boom(url):
        raise OSError("network down")
    client = W.WikidataClient(transport=boom)
    assert client.search("anything") == []
    assert client.entity("Q1") is None


# ── reverse resolution (regional authority id → Wikidata hub) ─────────────────────
def test_resolve_by_external_id_bdrc_to_hub():
    # haswbstatement reverse lookup: the BDRC id 'bdr:P4954' (bare 'P4954' in Wikidata)
    # resolves to the QID that carries P2477=P4954.
    client = W.WikidataClient(transport=_transport_map({
        "P4954": {"query": {"search": [{"title": "Q187310"}]}},   # URL-encoded; P4954 is literal
    }))
    assert client.resolve_by_external_id("bdr:P4954") == "wikidata:Q187310"


def test_resolve_by_external_id_viaf_and_passthrough_and_miss():
    client = W.WikidataClient(transport=_transport_map({
        "264715620": {"query": {"search": [{"title": "Q109478559"}]}},
    }))
    assert client.resolve_by_external_id("viaf:264715620") == "wikidata:Q109478559"
    # already a wikidata id → returned as-is, no network
    assert client.resolve_by_external_id("wikidata:Q1") == "wikidata:Q1"
    # genuine miss (empty search) and unsupported scheme → None
    assert client.resolve_by_external_id("viaf:0000000") is None
    assert client.resolve_by_external_id("toh:123") is None


def test_resolve_by_external_id_raises_on_transport_failure():
    from catalogue.services.http_util import AuthorityUnavailable
    def boom(url):
        raise AuthorityUnavailable("429")
    import pytest
    with pytest.raises(AuthorityUnavailable):
        W.WikidataClient(transport=boom).resolve_by_external_id("bdr:P4954")


# ── VIAF ────────────────────────────────────────────────────────────────────────
def test_viaf_suggest_filters_personal():
    payload = {"result": [
        {"viafid": "100", "term": "Thurman, Robert", "nametype": "personal"},
        {"viafid": "200", "term": "Some Press", "nametype": "corporate"},
    ]}
    client = VIAFClient(transport=lambda url: payload)
    out = client.suggest("Robert Thurman")
    assert out == [("100", "Thurman, Robert")]


def test_viaf_swallows_errors():
    def boom(url):
        raise OSError("down")
    assert VIAFClient(transport=boom).suggest("x") == []
