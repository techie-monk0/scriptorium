"""Impact / Ref contracts — the serializable boundary object (reorg Phase 3).

These cross the server↔client boundary as JSON, so the round-trip must be lossless and the
`appliable` gate exact. See docs/access/entity_api_model.md §4/§5.
"""
from __future__ import annotations

import json

from catalogue.contracts import (
    Block,
    FileOp,
    Impact,
    LinkRepoint,
    Orphan,
    OrphanDecision,
    Ref,
    RefPurge,
)


def _rich_impact() -> Impact:
    ed = Ref("edition", 5, "fp-ed5")
    return Impact(
        op="delete",
        target=ed,
        cascades=(Ref("holding", 9), Ref("holding", 10)),
        orphans=(Orphan(Ref("work", 3), "0 editions left", OrphanDecision.FLAG),
                 Orphan(Ref("person", 12), "0 works/editions", OrphanDecision.GC)),
        ref_purges=(RefPurge("cover_file", "covers/e5.jpg", ed),
                    RefPurge("cache_row", "raw_extract_cache:t:abc", ed)),
        file_ops=(FileOp("trash", "/lib/a.pdf"),),
        link_repoints=(LinkRepoint("edition_commentary_on", Ref("edition", 5), Ref("edition", 7)),),
    )


def test_ref_roundtrip_with_and_without_fingerprint():
    assert Ref.from_dict(Ref("work", 1, "fp").to_dict()) == Ref("work", 1, "fp")
    bare = Ref("work", 2)
    assert "fingerprint" not in bare.to_dict()
    assert Ref.from_dict(bare.to_dict()) == bare


def test_impact_roundtrips_through_json_losslessly():
    imp = _rich_impact()
    wire = json.dumps(imp.to_dict())          # serialize as a client would receive it
    back = Impact.from_dict(json.loads(wire))  # and decode it
    assert back == imp


def test_appliable_is_false_with_a_block():
    ok = _rich_impact()
    assert ok.appliable is True
    blocked = Impact(op="delete", target=Ref("edition", 5),
                     blocks=(Block("integrity", "would dangle review_queue payload"),))
    assert blocked.appliable is False
    assert Impact.from_dict(json.loads(json.dumps(blocked.to_dict()))) == blocked


def test_orphan_decision_serializes_as_its_string():
    o = Orphan(Ref("work", 3), "x", OrphanDecision.REFUSE)
    assert o.to_dict()["decision"] == "refuse"
    assert Orphan.from_dict(o.to_dict()).decision is OrphanDecision.REFUSE
