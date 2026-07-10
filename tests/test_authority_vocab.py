"""Authority-control vocab (catalogue.db_store.authority_vocab): the shipped
vocab.json deep-merged with a user overlay so a public user can EXTEND the
controlled vocab without forking the file. Also covers that the honorifics /
names services actually see overlay additions.
"""
from __future__ import annotations

import json

import pytest

from catalogue.db_store import authority_vocab as AV


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    """Point $CATALOGUE_VOCAB_LOCAL at a writable temp overlay; clear caches
    around the test so nothing leaks between cases."""
    p = tmp_path / "vocab.local.json"
    monkeypatch.setenv(AV.OVERLAY_ENV, str(p))
    AV.reload()
    yield p
    AV.reload()


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    AV.reload()


# ── overlay resolution ──────────────────────────────────────────────────────
def test_overlay_path_env_wins(overlay, monkeypatch):
    assert AV.overlay_path() == overlay
    monkeypatch.delenv(AV.OVERLAY_ENV)
    # falls back to <data_dir>/vocab.local.json
    assert AV.overlay_path().name == AV.OVERLAY_FILENAME


def test_absent_overlay_is_identity(overlay):
    """No overlay file → merged config equals the shipped vocab.json verbatim."""
    from catalogue.db_store.db import VOCAB_PATH
    shipped = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    assert not overlay.exists()
    assert AV.vocab_config() == shipped


# ── deep merge semantics ────────────────────────────────────────────────────
def test_plain_list_is_extended_not_replaced(overlay):
    from catalogue.db_store.db import VOCAB_PATH
    base_hon = json.loads(VOCAB_PATH.read_text("utf-8"))["_honorific"]
    _write(overlay, {"_honorific": ["dorje-lopon"]})
    merged = AV.vocab_config()["_honorific"]
    assert "dorje-lopon" in merged
    assert set(base_hon).issubset(set(merged))     # base preserved


def test_translit_group_list_of_lists_merges(overlay):
    _write(overlay, {"_translit_variant": [["khyentse", "kyentse"]]})
    groups = AV.vocab_config()["_translit_variant"]
    assert ["khyentse", "kyentse"] in groups
    assert any("lozang" in g for g in groups)      # a shipped group is still there


def test_code_label_rows_merge_by_code(overlay):
    _write(overlay, {"work_type": [
        {"code": "sadhana", "label": "Sādhana"},        # new
    ]})
    rows = {r["code"]: r["label"] for r in AV.vocab_config()["work_type"]}
    assert rows["sadhana"] == "Sādhana"
    assert len(rows) > 1                                  # shipped work_types kept


def test_overlay_scalar_and_dict_merge(overlay):
    _write(overlay, {"_features": {"my_flag": True}})
    feats = AV.vocab_config()["_features"]
    assert feats["my_flag"] is True
    assert "multi_work_detection" in feats               # shipped flag preserved


def test_reload_picks_up_a_newly_created_overlay(overlay):
    assert "zzz-test-honorific" not in AV.vocab_config().get("_honorific", [])
    _write(overlay, {"_honorific": ["zzz-test-honorific"]})   # _write calls reload()
    assert "zzz-test-honorific" in AV.vocab_config()["_honorific"]


# ── services see overlay additions ──────────────────────────────────────────
def test_honorifics_service_sees_overlay(overlay):
    from catalogue.services import honorifics as H
    from catalogue.db_store import fold_key
    _write(overlay, {"_honorific": ["dorje-lopon"]})
    H.reload()
    assert fold_key("dorje-lopon") in H.honorific_keys()


def test_names_service_sees_overlay_org_marker(overlay):
    from catalogue.services import names as N
    _write(overlay, {"_organization": ["quiztotle collective"]})
    N.reload_org_markers()
    assert N.is_organization_name("The Quiztotle Collective")
