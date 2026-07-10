"""Tests for the reusable ALA-LC → EWTS converter (catalogue/translit.py), driven by
real (often OCR-mangled) uniform-title / name-heading strings from the corpus."""
from __future__ import annotations

from catalogue.services.translit import (
    classify_script, fold_confusables, strip_language_subfield, to_ewts,
)


# ── the five real letter substitutions ───────────────────────────────────────────────
def test_clean_tibetan_uniform_title():
    # e299: Tsongkhapa's Illuminating the Intent, as printed in clean ALA-LC.
    s = "Dbu ma la 'jug pa'i rgya cher bśad pa dgoṅs pa rab gsal"
    assert to_ewts(s) == "dbu ma la 'jug pa'i rgya cher bshad pa dgongs pa rab gsal"


def test_each_diacritic_maps():
    assert to_ewts("ṅa ña śa źa") == "nga nya sha zha"


def test_achung_variants_all_fold_to_ascii_apostrophe():
    for q in ("'jug", "ʼjug", "’jug", "‘jug", "`jug"):
        assert to_ewts(q) == "'jug"


# ── name headings (hyphen-joined syllables) ───────────────────────────────────────────
def test_name_heading_hyphens_to_spaces():
    assert to_ewts("Tsong-kha-pa Blo-bzang-grags-pa", names=True) == \
        "tsong kha pa blo bzang grags pa"


def test_name_with_achung():
    assert to_ewts("Blo-gros-mtha'-yas", names=True) == "blo gros mtha' yas"


# ── OCR tolerance: caron-for-acute is the #1 scanned-diacritic error ──────────────────
def test_ocr_caron_for_acute_repaired():
    # "Šes" (s-caron) for "Śes" (s-acute) → EWTS "shes"
    assert to_ewts("Šes bya", ocr=True) == "shes bya"
    assert fold_confusables("Šes") == "Śes"


def test_ocr_conservative_does_not_promote_plain_s():
    # A bare ASCII 's' must NOT become 'sh' — that would corrupt correct text.
    assert to_ewts("Ses bya", ocr=True) == "ses bya"


# ── language / Selections subfield stripping (MARC 240 $l/$k) ──────────────────────────
def test_strip_language_and_selections():
    core, lang, sel = strip_language_subfield(
        "Śes bya mtha' yas pa'i rgya mtsho. English. Selections")
    assert core == "Śes bya mtha' yas pa'i rgya mtsho"
    assert lang == "English" and sel is True


def test_strip_language_only():
    core, lang, sel = strip_language_subfield("Bodhicaryāvatāra. English")
    assert core == "Bodhicaryāvatāra" and lang == "English" and sel is False


# ── script classification: Tibetan (convert) vs Sanskrit IAST (keep) ──────────────────
def test_classify_tibetan_by_achung():
    assert classify_script("Dbu ma la 'jug pa'i rgya cher bshad pa") == "tibetan"


def test_classify_sanskrit_by_macrons_no_achung():
    assert classify_script("Bodhicaryāvatāra") == "sanskrit"
    assert classify_script("Daśabhūmivibhāṣāśāstra") == "sanskrit"


def test_classify_tibetan_beats_shared_diacritics():
    # Has ṅ/ś (shared with Sanskrit) but also a-chung + particles → Tibetan.
    assert classify_script("Grub mtha' rtsa ba'i tshig tik shel dkar me long") \
        == "tibetan"


def test_sanskrit_visarga_apostrophe_not_tibetan():
    # e303 regression: a concatenated Sanskrit title whose only apostrophe is a visarga
    # ("…catuh'sata…") must not be read as Tibetan (no space, no particle).
    assert classify_script("bodhisattvayogacaracatuh'sataktika") != "tibetan"


def test_english_possessive_not_mistaken_for_achung():
    # e31/e59 regression: a Latin/English title with a possessive must NOT read Tibetan.
    assert classify_script("Āryadeva's Lamp that integrates the practices") == "sanskrit"
    assert classify_script("Commentary on Shantideva's work") == "unknown"


def test_empty_and_noise():
    assert to_ewts("") == "" and to_ewts("   ") == ""
    assert classify_script("") == "unknown"
