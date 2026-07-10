"""Slot-based single-work Sanskrit title extraction — precision regression tests.

The whole point of `sanskrit_title.extract_sanskrit_title` is that a Sanskrit word which
is merely an INLINE AUTHOR reference ("Candrakīrti's Introduction…", "by Nāgārjuna") is
NEVER mistaken for the title — only a structural slot (parenthetical / post-colon subtitle
with the author stripped / Sanskrit lead / CIP uniform) counts.
"""
from catalogue.services.sanskrit_title import extract_sanskrit_title as ex


def _texts(title, uniform=None):
    return [t for t, _src in ex(title, uniform_title=uniform)]


def test_parenthetical_without_diacritics():
    # the user's example — a lone long compound in parens, no diacritics
    assert "Vaidalyaprakarana" in _texts("Crushing the Categories (Vaidalyaprakarana)")


def test_parenthetical_with_diacritics():
    assert "Caryāmelāpakapradīpa" in _texts(
        "The Lamp for Integrating the Practices (Caryāmelāpakapradīpa) by Āryadeva")


def test_post_colon_strips_author_possessive():
    # "<Author>'s <SanskritTitle>" → the author is dropped, the Sanskrit kept
    assert _texts("The Dispeller of Disputes: Nāgārjuna’s Vigrahavyāvartanī",
                  "Vigrahavyāvartanī") == ["Vigrahavyāvartanī"]


def test_sanskrit_lead_before_of_author():
    assert "Mūlamadhyamakakārikā" in _texts(
        "Mūlamadhyamakakārikā of Nāgārjunā: The Philosophy of the Middle Way")


def test_cip_uniform_is_authoritative():
    assert "Yuktiṣaṣṭikākārikā" in _texts(
        "The Reason Sixty by Nāgārjuna with the Reason Sixty Commentary by Chandrakīrti",
        "Yuktiṣaṣṭikākārikā")


def test_marc_pitaka_prefix_peeled():
    assert _texts("Rice Seedling Sutra: An Introduction to Dependent Arising",
                  "Tripiṭaka. Sūtrapiṭaka. Śālistambasūtra") == ["Śālistambasūtra"]


def test_inline_possessive_author_is_NOT_a_title():
    # the false positive the user flagged: Candrakīrti is the commented author, not the
    # Sanskrit title of THIS book → nothing extracted
    assert _texts("Candrakīrti’s Introduction to the Middle Way: A Guide") == []


def test_commentary_on_author_inline_rejected_but_paren_kept():
    out = _texts("Buddhapālita’s Commentary on Nāgārjuna’s Middle Way: "
                 "(Buddhapālita-Mūlamadhyamaka-Vṛtti)", "Mūlamadhyamakavr°tti")
    assert any("Mūlamadhyamaka" in t for t in out)        # the paren/uniform title
    assert not any(t in ("Nāgārjuna", "Buddhapālita") for t in out)   # not the authors


def test_english_only_title_yields_nothing():
    assert _texts("Introduction to Emptiness: A Study of Buddhist Logic") == []


def test_edition_parenthetical_rejected():
    assert _texts("Some Treatise (Revised Edition)") == []
    assert _texts("Some Treatise (A Guide)") == []
