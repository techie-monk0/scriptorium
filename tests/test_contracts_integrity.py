"""IntegrityGate + Query contracts — the validate/normalize gate and the read-query shape.

BasicGate is the declarative default: collapse whitespace, reject unknown fields, enforce required +
max length — turning a malformed write payload into Impact.blocks. Query is the serializable
substring-filter + pagination shape. Pure, no I/O. See contracts/integrity.py.
"""
from catalogue.contracts import BasicGate, FieldRule, Query


def _gate():
    return BasicGate({"subject": {"name": FieldRule(required=True, max_len=5),
                                  "kind": FieldRule()}})


def test_normalize_collapses_whitespace_and_passes_valid():
    norm, blocks = _gate().check("subject", {"name": "  a   b "})
    assert norm["name"] == "a b" and blocks == ()       # collapsed; kind optional


def test_required_field_missing_is_blocked():
    _norm, blocks = _gate().check("subject", {"kind": "topic"})
    assert any("required" in b.message for b in blocks)


def test_unknown_field_is_blocked():
    _norm, blocks = _gate().check("subject", {"name": "x", "bogus": 1})
    assert any("unknown field" in b.message and b.code == "validation" for b in blocks)


def test_max_len_is_blocked():
    _norm, blocks = _gate().check("subject", {"name": "abcdef"})   # 6 > max_len 5
    assert any("exceeds" in b.message for b in blocks)


def test_partial_update_skips_absent_required_but_blocks_emptying():
    g = _gate()                                              # name required
    assert g.check("subject", {"kind": "series"}, partial=True)[1] == ()   # absent name OK on a patch
    _n, blocks = g.check("subject", {"name": ""}, partial=True)            # but emptying it is blocked
    assert any("required" in b.message for b in blocks)


def test_query_roundtrips_and_defaults():
    q = Query(contains="mad", limit=10, offset=20)
    assert Query.from_dict(q.to_dict()) == q
    assert Query() == Query(contains=None, limit=50, offset=0)
