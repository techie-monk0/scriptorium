"""Tests for the BDRC verify gate (catalogue/wylie_resolve.py) — hermetic, canned hits.
The headline case: the author anchor must pick Tsongkhapa's `dgongs pa rab gsal` over the
identically-titled works by other authors (the homonym trap this whole pipeline exists for)."""
from __future__ import annotations

from catalogue.services.bdrc import BdrcWorkSearch
from catalogue.services.wylie_resolve import verify_from_cip, verify_work


# Canned BDRC work hits: Tsongkhapa's work AND a same-titled work by another author.
_TSONGKHAPA = {
    "id": "bdr:MW3KG147", "score": 90.0,
    "titles": ["dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal",
               "bstan bcos chen po dbu ma la 'jug pa'i rnam bshad dgongs pa rab gsal"],
    "authors": ["tsong kha pa blo bzang grags pa"]}
_SHERAB_WANGPO = {
    "id": "bdr:MW1NLM763", "score": 70.0,
    "titles": ["dbu ma la 'jug pa'i rnam bshad dgongs pa rab gsal"],
    "authors": ["rje drung shes rab dbang po"]}


def _fake_search(hits):
    return lambda title, author: hits


def test_author_anchor_picks_correct_homonym():
    v = verify_work(
        "dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal",
        author_ewts="tsong kha pa blo bzang grags pa",
        search=_fake_search([_TSONGKHAPA, _SHERAB_WANGPO]))
    assert v.matched is True
    assert v.bdrc_id == "bdr:MW3KG147"
    assert "author confirmed" in v.reason


def test_author_mismatch_is_rejected_even_with_title_hit():
    # Same exact title, but we assert Tsongkhapa and only the wrong-author work exists.
    v = verify_work(
        "dbu ma la 'jug pa'i rnam bshad dgongs pa rab gsal",
        author_ewts="tsong kha pa blo bzang grags pa",
        search=_fake_search([_SHERAB_WANGPO]))
    assert v.matched is False
    assert v.reason == "author mismatch"


def test_no_author_anchor_matches_title_but_lower_confidence():
    v = verify_work(
        "dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal",
        search=_fake_search([_TSONGKHAPA]))
    assert v.matched is True
    assert "no author anchor" in v.reason
    assert v.confidence < 1.0           # downweighted vs an author-confirmed match


def test_weak_title_match_rejected():
    v = verify_work("dbu ma la 'jug pa'i something else entirely here",
                    author_ewts="tsong kha pa blo bzang grags pa",
                    search=_fake_search([_TSONGKHAPA]))
    assert v.matched is False
    assert v.reason == "weak title match"


def test_no_hits():
    v = verify_work("dbu ma dgongs pa rab gsal", search=_fake_search([]))
    assert v.matched is False and v.reason == "no BDRC hits"


def test_search_error_is_not_a_definitive_miss():
    def boom(title, author):
        raise ConnectionError("offline")
    v = verify_work("dbu ma dgongs pa rab gsal", search=boom)
    assert v.matched is False and "search error" in v.reason


# ── verify_from_cip: ALA-LC + script gating ──────────────────────────────────────────
def test_verify_from_cip_converts_alalc_and_matches():
    # ALA-LC as printed (ś/ṅ) + hyphenated author → converted → matched.
    v = verify_from_cip(
        "Dbu ma la 'jug pa'i rgya cher bśad pa dgoṅs pa rab gsal",
        script="tibetan", author_alalc="Tsoṅ-kha-pa Blo-bzaṅ-grags-pa",
        search=_fake_search([_TSONGKHAPA, _SHERAB_WANGPO]))
    assert v.matched is True and v.bdrc_id == "bdr:MW3KG147"
    assert v.ewts_query == "dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal"


def test_verify_from_cip_sanskrit_short_circuits():
    v = verify_from_cip("Vigrahavyāvartanī", script="sanskrit",
                        search=_fake_search([_TSONGKHAPA]))
    assert v.matched is False and "not a Wylie title" in v.reason


# ── BdrcWorkSearch query/parse with a canned transport ────────────────────────────────
def test_work_search_builds_query_and_parses_hits():
    captured = {}
    def transport(body):
        captured["body"] = body
        return {"responses": [{"hits": {"hits": [
            {"_id": "MW3KG147", "_score": 88.0,
             "_source": {"prefLabel_bo_x_ewts": ["dbu ma ... dgongs pa rab gsal"],
                         "authorName_bo_x_ewts": ["tsong kha pa blo bzang grags pa"]}}]}}]}
    hits = BdrcWorkSearch(transport=transport).work_search(
        "dgongs pa rab gsal", "tsong kha pa")
    assert hits[0]["id"] == "bdr:MW3KG147"
    assert hits[0]["titles"] == ["dbu ma ... dgongs pa rab gsal"]
    assert hits[0]["authors"] == ["tsong kha pa blo bzang grags pa"]
    # author clause only added when an author is supplied
    assert "authorName_bo_x_ewts" in captured["body"]
