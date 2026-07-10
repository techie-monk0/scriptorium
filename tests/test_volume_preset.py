"""Volume-type presets → ProcessConfig flag mapping + mutual exclusion."""
from types import SimpleNamespace

import pytest

from catalogue.services.process import ProcessConfig, apply_volume_preset


def test_single_author_multi_work():
    cfg = ProcessConfig()
    apply_volume_preset(cfg, single_author_multi_work=True)
    assert cfg.toc_hierarchy is True
    assert cfg.title_by_author is False
    assert cfg.title_with_possessive is False


def test_multi_author():
    cfg = ProcessConfig(toc_hierarchy=True, title_by_author=False, title_with_possessive=False)
    apply_volume_preset(cfg, multi_author=True)
    assert cfg.toc_hierarchy is False
    assert cfg.title_by_author is True
    assert cfg.title_with_possessive is True


def test_neither_leaves_defaults_untouched():
    cfg = ProcessConfig()
    before = (cfg.toc_hierarchy, cfg.title_by_author, cfg.title_with_possessive)
    apply_volume_preset(cfg)
    assert (cfg.toc_hierarchy, cfg.title_by_author, cfg.title_with_possessive) == before


def test_mutually_exclusive_raises():
    with pytest.raises(ValueError):
        apply_volume_preset(ProcessConfig(), single_author_multi_work=True, multi_author=True)


def test_duck_typed_on_namespace():
    # The runners resolve the preset on a lightweight namespace (no full config).
    ns = SimpleNamespace(toc_hierarchy=False, title_by_author=True, title_with_possessive=True)
    apply_volume_preset(ns, single_author_multi_work=True)
    assert ns.toc_hierarchy is True and ns.title_by_author is False
