"""Tests for the OCR-tolerant CIP-field parser (catalogue/cip.py), across the real
formats books ship with — including scanned-PDF OCR warts."""
from __future__ import annotations

from catalogue.services.cip import parse_cip


def test_no_cip_block_returns_none():
    assert parse_cip("just some prose with no cataloging block") is None


# ── uniform-title capture (MARC 240): the Wylie/IAST ORIGINAL, formerly discarded ─────
def test_uniform_title_labelled_tibetan():
    # e299: Illuminating the Intent — RDA 'Other titles:' carries the Wylie original.
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Names: Tsoṅ-kha-pa Blo-bzaṅ-grags-pa, 1357-1419, author. | "
        "Thupten Jinpa, translator.\n"
        "Title: Illuminating the intent: an exposition of Candrakīrti's "
        "\"Entering the middle way\" / Tsongkhapa; translated by Thupten Jinpa.\n"
        "Other titles: Dbu ma la 'jug pa'i rgya cher bśad pa dgoṅs pa rab gsal. English\n"
        "Identifiers: LCCN 2020045678 | ISBN 9781614294412\n")
    r = parse_cip(text)
    assert r.kind == "labelled"
    assert r.title == "Illuminating the intent: an exposition of Candrakīrti's \"Entering the middle way\""
    assert r.uniform_title == "Dbu ma la 'jug pa'i rgya cher bśad pa dgoṅs pa rab gsal"
    assert r.uniform_lang == "English" and r.uniform_selections is False
    assert r.uniform_script == "tibetan"
    assert "1357-1419" in r.author_dates
    # main-entry ALA-LC author (NOT the English translator) — the BDRC anchor
    assert r.author_heading == "Tsoṅ-kha-pa Blo-bzaṅ-grags-pa"


def test_uniform_title_bracketed_with_selections():
    # e225-234 shape: Treasury of Knowledge — AACR2 bracket + '. Selections' (partial).
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Kong-sprul Blo-gros-mtha'-yas, 1813-1899.\n"
        "[Śes bya mtha' yas pa'i rgya mtsho. English. Selections]\n"
        "The treasury of knowledge. Book six / Jamgön Kongtrul Lodrö Tayé.\n"
        "ISBN 978-1-55939-389-8\n")
    r = parse_cip(text)
    assert r.kind == "freeform"
    assert r.uniform_title == "Śes bya mtha' yas pa'i rgya mtsho"
    assert r.uniform_lang == "English" and r.uniform_selections is True
    assert r.uniform_script == "tibetan"
    assert "1813-1899" in r.author_dates
    # free-form main entry captured from the top line before the dates
    assert r.author_heading == "Kong-sprul Blo-gros-mtha'-yas"


def test_uniform_title_sanskrit_kept_as_iast():
    # e290: a Sanskrit uniform title must be classified sanskrit (NOT Wylie-converted).
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Nāgārjuna.\n"
        "[Vigrahavyāvartanī. English]\n"
        "The dispeller of disputes / Jan Westerhoff.\n"
        "ISBN 978-0-19-973269-0\n")
    r = parse_cip(text)
    assert r.uniform_title == "Vigrahavyāvartanī"
    assert r.uniform_script == "sanskrit"


def test_uniform_title_from_translation_of_note():
    # e381 (Liberation in Our Hands): AACR2 "Translation of:" note, wrapped + OCR noise.
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Pha-boṅ-kha-pa Byams-pa bstan-'dzin-'phrin-las-rgya-mtsho, 1878-1941.\n"
        "Liberation in our hands.\n"
        "Bibliography: p.\n"
        "Translation of: Rnam grol lag bcas su gtod pa'i man ngag\n"
        "zab mo tshad la ma nor ba'i chos kyi rgyal po'i thugs bcud\n"
        "1. Tsoṅ-kha-pa Blo-bzaṅ-grags pa, 1357-1419. Lam rim chen mo.\n"
        "ISBN 0-918753-08-2\n")
    r = parse_cip(text)
    assert r.uniform_title.startswith("Rnam grol lag bcas su gtod pa'i man ngag")
    assert r.uniform_script == "tibetan"
    assert "1878-1941" in r.author_dates


def test_uniform_title_strips_statement_of_responsibility():
    # e26 regression: "Translation of: <title> / by <name>" must drop the SOR.
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Dalai Lama XIV, 1935-\n"
        "Six-session guru yoga.\n"
        "Translation of: thun drug bla ma'i rnal 'byor / by Blo-bzang-chos-kyi-rgyal-mtshan\n"
        "ISBN 978-1-55939-123-7\n")
    r = parse_cip(text)
    assert r.uniform_title == "thun drug bla ma'i rnal 'byor"
    assert r.uniform_script == "tibetan"


def test_translation_of_english_prose_is_not_a_uniform_title():
    # e155 false friend: prose "...translation of Nagarjuna's Precious Garland..." (no
    # colon, English) must NOT be taken as a Wylie uniform title.
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Hopkins, Jeffrey.\n"
        "Buddhist Advice for Living : Nagarjuna's Precious Garland / Jeffrey Hopkins.\n"
        "This translation of Nagarjuna's Precious Garland makes it accessible.\n"
        "ISBN 978-1-55939-555-7\n")
    r = parse_cip(text)
    assert r.uniform_title is None


def test_no_uniform_title_when_absent():
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Names: McDonald, Kathleen, 1952- author.\n"
        "Title: How to meditate / by Kathleen McDonald.\n"
        "Identifiers: ISBN 9781614298939\n")
    r = parse_cip(text)
    assert r.uniform_title is None and r.uniform_script is None


def test_bracket_gmd_not_mistaken_for_uniform_title():
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Smith, John.\n"
        "Meditation basics [electronic resource] / John Smith.\n"
        "ISBN 978-0-19-973269-0\n")
    r = parse_cip(text)
    assert r.uniform_title is None


# ── modern labelled (e1: How to Meditate) ───────────────────────────────────────────
def test_labelled_record():
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Names: McDonald, Kathleen, 1952– author.\n"
        "Title: How to meditate on the stages of the path: a guide to the Lamrim "
        "/ by Kathleen McDonald (Sangye Khadro).\n"
        "Description: First edition. | New York: Wisdom Publications, 2024. | Includes index.\n"
        "Identifiers: LCCN 2024008414 (print) | LCCN 2024008415 (ebook) | "
        "ISBN 9781614298939 (paperback) | ISBN 9781614299066 (ebook)\n"
        "Subjects: LCSH: Meditation—Buddhism.\n")
    r = parse_cip(text)
    assert r.kind == "labelled"
    # title as PRINTED (sentence case); the consumer title-cases for storage
    assert r.title == "How to meditate on the stages of the path: a guide to the Lamrim"
    assert "9781614298939" in r.isbns and "9781614299066" in r.isbns
    assert r.lccn == "2024008414"
    assert r.year == 2024
    assert r.authors == ["McDonald, Kathleen"]
    assert r.publisher == "Wisdom Publications"


# ── older free-form (e29: Cakrasamvara), with author main-entry + uniform title ──────
def test_freeform_record():
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "Gray, David B., 1969–\n"
        "The Cakrasamvara Tantra : the discourse of Sri Heruka (Sriherukabhidhana) "
        "/ a study and annotated translation by David B. Gray.\n"
        "  p. cm. — (Treasury of the Buddhist sciences)\n"
        "Includes bibliographical references and index.\n"
        "ISBN 978-0-9753734-6-0 (alk. paper)\n"
        "1. Tripitaka. I. Title.\n")
    r = parse_cip(text)
    assert r.kind == "freeform"
    assert r.title == "The Cakrasamvara Tantra: the discourse of Sri Heruka (Sriherukabhidhana)"
    assert "9780975373460" in r.isbns


# ── scanned-PDF OCR warts (e5: Creation & Completion) ───────────────────────────────
def test_freeform_with_ocr_noise():
    """'p. cm.'→'p. em.', a middle-dot for the period after the author's dates, a
    bracketed uniform title, and an OCR-mangled ISBN that must be DROPPED."""
    text = (
        "Library of Congress Cataloging-in-Publication Data\n"
        "K0ngtrul Blo-gros-mtha' -yas, 1813-1899·\n"
        "[Lam ugs kyi gan zag. English]\n"
        "Creation & completion : essential points of tantric meditation "
        "/ Jamgon Kongtrul ; translated by Sarah Harding.\n"
        "p. em.\n"
        "Cover tide: Creation and completion.\n"
        "ISBN o-86171-311-5 (alk. paper)\n")       # OCR-corrupted (real is …312-5)
    r = parse_cip(text)
    assert r.kind == "freeform"
    assert r.title == "Creation & completion: essential points of tantric meditation"
    assert r.isbns == []          # the corrupted ISBN fails checksum → dropped, NOT trusted


# ── abbreviated (Science vol 2: "CIP data is available") ────────────────────────────
def test_abbreviated_record():
    text = ("Library of Congress Cataloging-in-Publication Data is available.\n"
            "LCCN 2017018045\n"
            "ISBN 978-1-61429-474-0\n")
    r = parse_cip(text)
    assert r.kind == "abbreviated"
    assert r.title is None
    assert "9781614294740" in r.isbns
    assert r.lccn == "2017018045"


# ── British Library ─────────────────────────────────────────────────────────────────
def test_british_library_record():
    text = ("A catalogue record for this book is available from the British Library.\n"
            "ISBN 978-1-55939-066-8\n")
    r = parse_cip(text)
    assert r.kind == "british_library"
    assert r.title is None
    assert "9781559390668" in r.isbns


# ── pre-ISBN card number ─────────────────────────────────────────────────────────────
def test_year_not_hijacked_by_author_lifespan():
    text = ("Library of Congress Cataloging-in-Publication Data\n"
            "Smith, John, 1945-2012, author.\n"
            "A book about things / by John Smith.\n"
            "p. cm. — Boston : Some Press, 1998.\n"
            "ISBN 978-1-61429-893-9\n")
    r = parse_cip(text)
    assert r.year == 1998          # publication year, NOT the author's death year 2012


def test_sandwich_title_no_sor_no_subtitle():
    """Very old record: no ' / ' SOR, no ' : ' subtitle, 'Bibliography: p.' not
    'p. cm.' — the title sits between the author entry and the notes block."""
    text = ("Library of Congress Cataloging-in-Publication Data\n"
            "Pabongka Rinpoche, 1878-1941.\n"
            "Liberation in our hands.\n"
            "Bibliography: p.\n"
            "Includes index.\n"
            "ISBN 0-918753-08-2\n")
    r = parse_cip(text)
    assert r.title == "Liberation in our hands"
    assert "9780918753083" in r.isbns


def test_labelled_title_mangled_slash_does_not_drag_sor():
    text = ("Library of Congress Cataloging-in-Publication Data\n"
            "Names: Doe, Jane, author.\n"
            "Title: The real title/ by Jane Doe and others.\n"     # missing space before name
            "Identifiers: ISBN 978-1-61429-893-9\n")
    r = parse_cip(text)
    assert r.title == "The real title"     # SOR dropped despite the missing space


def test_lowercase_start_title_not_rejected():
    text = ("Library of Congress Cataloging-in-Publication Data\n"
            "Author, An, 1950-\n"
            "the history of something : an account / by An Author.\n"
            "p. cm.\nISBN 978-1-61429-893-9\n")
    r = parse_cip(text)
    assert r.title and "history of something" in r.title.lower()


def test_ocr_label_colon_repaired():
    text = ("Library of Congress Cataloging-in-Publication Data\n"
            "Names; McDonald, Kathleen, author.\n"          # ';' instead of ':'
            "Title. How to meditate / by Kathleen McDonald.\n"  # '.' instead of ':'
            "Identifiers, ISBN 9781614298939\n")
    r = parse_cip(text)
    assert r.kind == "labelled"
    assert r.title == "How to meditate"


def test_card_number_record():
    text = "Library of Congress Catalog Card Number: 75-189390\n(no ISBN in this era)\n"
    r = parse_cip(text)
    assert r.kind == "card"
    assert r.lccn == "75189390"
    assert r.isbns == []


# ── block SELECTION: real CIP is not the first marker (regression: e31) ──────────────
def test_skips_prose_markers_before_real_cip():
    """Scholarly books mention "Library of Congress" / "National Library of …" in the
    introduction, bibliography, and manuscript-source lists — long BEFORE the
    copyright-page CIP. parse_cip must vet markers and parse the REAL block, not the
    first prose hit (which collapsed two different Treasury-series books onto the
    series name "Treasury of the Buddhist Sciences")."""
    text = (
        "Modern Scholarship, Darkly. Although the Library of Congress catalogs the "
        "esoteric writings attributed to Aryadeva under the rubric 'Aryadeva, 3rd "
        "cent.', this is debated. " + ("filler prose. " * 400) +
        "Sources: Library of Congress P.L. 480 program; National Library of Nepal "
        "microfilm. " + ("more filler. " * 400) +
        "Library of Congress Cataloging-in-Publication Data\n"
        "Names: Wedemeyer, Christian K., translator.\n"
        "Title: The lamp for integrating the practices (Caryamelapakapradipa) by "
        "Aryadeva: the gradual path of Vajrayana Buddhism / translated by Christian "
        "K. Wedemeyer.\n"
        "Description: Second edition. | New York: Wisdom Publications, 2021.\n"
        "Identifiers: LCCN 2020044305 | ISBN 9781949163186 (hardcover) | "
        "ISBN 9781949163193 (ebook)\n"
        "Subjects: LCSH: Tantric Buddhism.\n")
    r = parse_cip(text)
    assert r is not None
    assert r.kind == "labelled"
    assert r.title == ("The lamp for integrating the practices (Caryamelapakapradipa) "
                       "by Aryadeva: the gradual path of Vajrayana Buddhism")
    assert "9781949163186" in r.isbns


def test_bare_library_of_congress_in_prose_is_not_a_cip():
    """A bare 'Library of Congress' prose mention with NO CIP content → no record."""
    text = ("The Library of Congress holds the largest Tibetan collection outside "
            "Asia, acquired largely through its P.L. 480 program in the 1960s. "
            + ("scholarly discussion continues. " * 50))
    assert parse_cip(text) is None
