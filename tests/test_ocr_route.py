"""§4.8d Step-6 OCR router + valid-IAST filter (catalogue/ocr_route.py).

Encodes the 2026-05-30 bake-off decision (ocr_considerations.md §9): route
diacritic-relevant pages to Cloud Vision; keep Wylie/English local; flag
Cloud-Vision's non-IAST substitutions.
"""
from catalogue.services.ocr_route import (
    route_page, plan_escalation, count_foreign_diacritics,
)


def test_iast_dense_page_routes_to_cloud():
    t = ("Bhairavapadmāvatīkalpa oṃ hrīṃ hṛtkamale gajendravaśakaṃ "
         "sarvāṅgasandhiṣv māyām āvilikhet pariveṣṭya " * 3)
    d = route_page(t)
    assert d.route_to_cloud is True
    assert d.tdia > 25 and d.priority == "high"


def test_plain_english_page_stays_local():
    t = ("The bodhisattva path requires great compassion and steady wisdom "
         "cultivated over many lifetimes of patient practice. " * 5)
    assert route_page(t).route_to_cloud is False


def test_wylie_only_page_stays_local():
    """Wylie is ASCII; Tesseract handles it (94–96% recall). No diacritics,
    no Sanskrit vocab → must NOT route (no benefit, just privacy cost)."""
    t = ("byang chub sems dpa'i spyod pa la 'jug pa / shes rab kyi pha rol "
         "tu phyin pa'i man ngag / bstan bcos rnam par bshad pa. " * 4)
    d = route_page(t)
    assert d.route_to_cloud is False
    assert d.tdia == 0


def test_sanskrit_vocab_routes_even_when_diacritics_dropped():
    """A scan whose OCR stripped the marks still carries the vocabulary —
    relevance must survive on the diacritic-independent signal."""
    t = ("Madhyamaka and Yogacara on sunyata and pratityasamutpada; "
         "Nagarjuna, Candrakirti, Vasubandhu, Asanga, abhidharma, vijnana. " * 3)
    d = route_page(t)
    assert d.skt >= 10
    assert d.route_to_cloud is True


def test_foreign_diacritics_flags_cloud_vision_substitutions():
    # Cloud-Vision-style wrong marks (umlaut/tilde/breve/acute = non-IAST).
    bad = "Nägărjuna Müla-madhyamaka-kärikā Tõ. 387 prabhāsvara"
    # valid IAST marks here: ā(×2) — everything else is foreign.
    assert count_foreign_diacritics(bad) >= 5
    # Clean IAST has zero foreign diacritics.
    assert count_foreign_diacritics("Nāgārjuna Mūla-madhyamaka-kārikā śūnyatā") == 0


def test_plan_escalation_selects_only_relevant_pages():
    pages = [
        "Plain English introduction with no special vocabulary at all. " * 4,   # 0: no
        "oṃ hrīṃ Bhairavapadmāvatīkalpa māyām āvilikhet pariveṣṭya " * 4,        # 1: yes
        "byang chub sems dpa'i spyod pa la 'jug pa shes rab " * 4,               # 2: no (Wylie)
        "Madhyamaka Nagarjuna sunyata Yogacara abhidharma vijnana " * 4,         # 3: yes (vocab)
    ]
    assert plan_escalation(pages) == [1, 3]


def test_plan_escalation_handles_missing_pages():
    assert plan_escalation(None) == []
    assert plan_escalation([]) == []


def test_low_confidence_marks_priority():
    d = route_page("dharmakāya śūnyatā", conf=82.0)
    assert d.priority == "high"
    d2 = route_page("dharmakāya śūnyatā", conf=96.0)
    # tdia small here, so priority hinges on confidence
    assert d2.priority == "normal"
