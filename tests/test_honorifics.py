"""Tests for honorific/title stripping used in author-name matching
(catalogue/honorifics.py) and its wiring into verify._tokens / verify_person.
"""
from __future__ import annotations

import pytest

from catalogue.services import honorifics as H
from catalogue.db_store import fold_key, init_db
from catalogue.services import verify


# ── strip_honorifics: titles removed when ≥2 name tokens survive ────────────────
@pytest.mark.parametrize("name,expected_key", [
    ("Geshe Lhundub Sopa", "Lhundub Sopa"),
    ("Khenpo Tsultrim Gyamtso", "Tsultrim Gyamtso"),
    ("Dr. Robert Thurman", "Robert Thurman"),
    ("Lopön Tenzin Namdak", "Tenzin Namdak"),
])
def test_strip_pure_honorifics(name, expected_key):
    assert fold_key(H.strip_honorifics(name)) == fold_key(expected_key)


# Single-name guard: stripping that would leave ONE bare given name keeps the
# full titled form instead (a lone given name matches too many people).
@pytest.mark.parametrize("name", [
    "Kyabje Trijang Rinpoche",   # → "Trijang" alone → kept whole
    "Lama Zopa Rinpoche",        # → "Zopa" alone → kept whole
    "Acharya Nagarjuna",         # → "Nagarjuna" alone → kept whole
    "Ven. Bhikkhu Bodhi",        # → "Bodhi" alone → kept whole
    "Sogyal Rinpoche",           # → "Sogyal" alone → kept whole
])
def test_single_name_strip_is_guarded(name):
    assert H.strip_honorifics(name) == name.strip()


def test_title_only_name_falls_back_to_original():
    assert H.strip_honorifics("Rinpoche") == "Rinpoche"     # never empty
    assert H.strip_honorifics("") == ""


# ── extended mode (--person-resolution-extensions, task 1) ─────────────────────
@pytest.mark.parametrize("name,expected", [
    ("Acarya Nagarjuna", "Nagarjuna"),          # Sanskrit mononym title → lone name OK
    ("Acharya Kamalashila", "Kamalashila"),
    ("Arya Nagarjuna", "Nagarjuna"),
    ("Pandita Vasubandhu", "Vasubandhu"),
    ("Panchen Lozang Chokyi Gyaltsen", "Lozang Chokyi Gyaltsen"),  # bare office prefix
])
def test_extended_strips_classical_titles_and_bare_office(name, expected):
    assert fold_key(H.strip_honorifics(name, extended=True)) == fold_key(expected)


@pytest.mark.parametrize("name", [
    "14th Panchen Lama", "Panchen Lama XI", "Dalai Lama XIV", "14th Dalai Lama",
])
def test_extended_keeps_office_ordinal_identity(name):
    # An office WITH an ordinal is identity, never stripped — even in extended mode.
    assert H.strip_honorifics(name, extended=True) == name.strip()


@pytest.mark.parametrize("name", ["Lama Yeshe", "Sogyal Rinpoche", "Lama Zopa Rinpoche"])
def test_extended_keeps_courtesy_title_single_name_guard(name):
    # Extended mode must NOT strip a COURTESY title down to a lone given name (the
    # "Lama Yeshe" → "Yeshe" false positive the guard exists for).
    assert H.strip_honorifics(name, extended=True) == name.strip()


@pytest.mark.parametrize("name", [
    "Acharya Nagarjuna", "Arya Nagarjuna", "Panchen Lozang Chokyi Gyaltsen",
])
def test_default_mode_unchanged_by_extension(name):
    # The documented default-mode behaviour (single-name guard, office kept) is
    # byte-for-byte unchanged — extensions only apply when explicitly requested.
    assert H.strip_honorifics(name) == name.strip()


# ── the office + ordinal exception ─────────────────────────────────────────────
@pytest.mark.parametrize("name", [
    "14th Dalai Lama",
    "Fourteenth Dalai Lama",
    "Dalai Lama XIV",
    "16th Karmapa",
    "Seventeenth Karmapa",
    "Panchen Lama XI",
])
def test_office_with_ordinal_is_left_intact(name):
    # Identity-bearing: must NOT be reduced to bare "Lama"/"Karmapa".
    assert H.strip_honorifics(name) == name.strip()


def test_office_distinguishing_word_survives_even_without_ordinal():
    # No ordinal: "lama" (courtesy) is stripped, but the office word "Dalai" must
    # remain so different offices stay distinguishable.
    out = fold_key(H.strip_honorifics("Dalai Lama"))
    assert "dalai" in out
    assert out != ""


# ── name_matches_exactly: the Lama Yeshe precision regression ───────────────────
def test_exact_match_rejects_lone_given_name_false_positives():
    m = verify.name_matches_exactly
    # "Lama Yeshe" must NOT match the obscure bare "Yeshe" nor a longer name that
    # merely contains the token (Lama Yeshe Losal Rinpoche) — the real bug.
    assert not m("Lama Yeshe", "Yeshe", "ཡེ་ཤེས།")
    assert not m("Lama Yeshe", "Lama Yeshe Losal Rinpoche")
    # …but DOES match the record that actually carries the exact "Lama Yeshe" alias.
    assert m("Lama Yeshe", "Thubten Yeshe", "Lama Yeshe", "Yeshe")
    # a bare given-name query must not grab a longer same-given-name person
    assert not m("Yeshe", "Yeshe Gyaltsen")


def test_exact_match_accepts_inversion_and_stripped_forms():
    m = verify.name_matches_exactly
    assert m("Robert Thurman", "Thurman, Robert")        # VIAF inverted form
    assert m("Geshe Lhundub Sopa", "Lhundub Sopa")       # titled query vs bare
    assert m("Tenzin Gyatso", "Tenzin Gyatso")


def test_ordinal_value_unifies_forms():
    assert H.ordinal_value("14th Dalai Lama") == 14
    assert H.ordinal_value("Fourteenth Dalai Lama") == 14
    assert H.ordinal_value("Dalai Lama XIV") == 14
    assert H.ordinal_value("16th Karmapa") == 16
    assert H.ordinal_value("Karmapa XVI") == 16
    assert H.ordinal_value("Dalai Lama") is None
    assert H.ordinal_value("Tenzin Gyatso") is None


def test_exact_match_ordinal_gate_distinguishes_incumbents():
    m = verify.name_matches_exactly
    # different incumbents of the same office must NOT match
    assert not m("14th Dalai Lama", "7th Dalai Lama")
    assert not m("16th Karmapa", "17th Karmapa")
    # a numbered office must not match the unnumbered/generic office
    assert not m("14th Dalai Lama", "Dalai Lama")
    # the SAME incumbent across digit / written / roman forms must match
    assert m("14th Dalai Lama", "Dalai Lama XIV")
    assert m("14th Dalai Lama", "Fourteenth Dalai Lama")
    assert m("Seventh Dalai Lama", "7th Dalai Lama")
    assert m("16th Karmapa", "Karmapa XVI")


def test_exact_match_keeps_short_syllables_no_collapse():
    """Regression: 'Lama Yeshe Chö Pel' wrongly matched 'Lama Yeshe' because the old
    4-char token floor dropped 'Chö'/'Pel'. Every syllable must count now."""
    m = verify.name_matches_exactly
    assert not m("Lama Yeshe", "Lama Yeshe Chö Pel")
    assert not m("Lama Yeshe", "Lama Yeshe Gyaltsen")
    # the genuinely-correct candidate (alias literally 'Lama Yeshe') still matches
    assert m("Lama Yeshe", "Thubten Yeshe", "Lama Yeshe")


def test_exact_match_no_digraph_over_merge_for_english():
    """The exact auto-bind gate uses search_normalize (diacritic-strip, NO aspirate
    digraph collapse), so plain English names that differ only by an aspirate are NOT
    merged — but diacritic variants still are."""
    m = verify.name_matches_exactly
    assert not m("Smith", "Smit")          # th→t must NOT bridge here
    assert not m("Booth", "Boot")
    assert not m("Stephen Batchelor", "Stepen Batchelor")   # ph→p must NOT bridge
    # diacritic folding is still wanted (the SAFE normalization)
    assert m("Santideva", "Śāntideva")
    assert m("Muller", "Müller")


def test_has_ordinal():
    assert H.has_ordinal("14th Dalai Lama")
    assert H.has_ordinal("Karmapa XVI")
    assert H.has_ordinal("Seventh Dalai Lama")
    assert not H.has_ordinal("Tenzin Gyatso")
    assert not H.has_ordinal("Lhundub Sopa 1923")     # a year is not an ordinal


# ── honorific_keys feeds the token filter ──────────────────────────────────────
def test_honorific_keys_contains_titles_not_offices():
    keys = H.honorific_keys()
    assert fold_key("geshe") in keys and fold_key("lama") in keys
    assert fold_key("rinpoche") in keys
    assert fold_key("dalai") not in keys      # office word, never a honorific
    assert fold_key("karmapa") not in keys


# ── wiring into verify._tokens / name_overlaps (the false-positive fix) ─────────
def test_lama_token_no_longer_creates_false_overlap():
    # Before the fix these shared the 4-char token "lama" and falsely overlapped.
    assert not verify.name_overlaps("Lama Zopa", "Lama Yeshe")
    # Real shared personal name still overlaps.
    assert verify.name_overlaps("Lama Zopa", "Zopa Rinpoche")


def test_tokens_strips_titles():
    assert verify._tokens("Geshe Lhundub Sopa") == verify._tokens("Lhundub Sopa")


# ── end-to-end: a titled person still matches a title-free authority ───────────
class _FakePersonResolver:
    """Returns a hit only when queried with the title-free name — proving the
    query was stripped before the lookup."""
    def __init__(self, expect, result):
        self.expect, self.result = expect, result

    def resolve_person(self, conn, text, scheme=None, *, offline=False):
        return self.result if text == self.expect else None

    def resolve_work(self, conn, text, scheme=None, *, offline=False):
        return None


def test_verify_person_searches_title_free(tmp_path):
    from catalogue.services.work_canonical_resolver import ResolverResult
    db = init_db(tmp_path / "h.db")
    pid = db.execute(
        "INSERT INTO person (primary_name) VALUES ('Geshe Lhundub Sopa')"
    ).lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Geshe Lhundub Sopa', 'english', ?)",
               (pid, fold_key("Geshe Lhundub Sopa")))
    # Resolver only answers to the stripped query "Lhundub Sopa" — proving the
    # honorific was stripped before the lookup. A BDRC person hit is now PROVISIONAL
    # (queued, not auto-applied), so the outcome is "candidate" and the queued
    # candidate carries the id the stripped query resolved to.
    res = ResolverResult("Lhundub Sopa", "bdrc", "bdr:P100", [], "bdrc")
    v = verify.BdrcVerifier(resolver=_FakePersonResolver("Lhundub Sopa", res))
    assert verify.verify_person(db, [v], pid) == "candidate"
    q = db.execute("SELECT payload_json FROM review_queue "
                   "WHERE item_type='person_authority'").fetchone()
    assert q is not None and "bdr:P100" in q[0]


# ── --person-resolution-extensions: alias-based matching + extended strip (task 2) ─
class _HardNameVerifier:
    """A stub authority that HARD-binds only when queried with one of `wanted`
    (exact string). Proves which query form reached the chain."""
    name = "stub"

    def __init__(self, wanted: dict):
        self.wanted = wanted               # {query_text: external_id}

    def verify(self, db, kind, text):
        if kind != "person" or text not in self.wanted:
            return None
        return verify.Match(self.wanted[text], "wikidata", text, [], self.name)


def test_extensions_match_via_alias_not_primary(tmp_path):
    """Office/ordinal primary ("Dalai Lama XIV") binds through a personal-name alias
    ("Tenzin Gyatso") — but ONLY with extensions on; the default ignores aliases."""
    db = init_db(tmp_path / "a.db")
    pid = db.execute(
        "INSERT INTO person (primary_name) VALUES ('Dalai Lama XIV')").lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Tenzin Gyatso', 'english', ?)",
               (pid, fold_key("Tenzin Gyatso")))
    v = _HardNameVerifier({"Tenzin Gyatso": "wikidata:Q17293"})

    # Default: only primary_name is tried ("Dalai Lama XIV" kept whole) → no hit.
    assert verify.verify_person(db, [v], pid) == "unmatched"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None

    # Extensions: the alias form reaches the chain → hard bind.
    assert verify.verify_person(db, [v], pid, extensions=True) == "matched"
    assert (db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0]
            == "wikidata:Q17293")


def test_extensions_strip_classical_title_to_mononym(tmp_path):
    """Extended honorific stripping lets "Acarya Nagarjuna" reach the chain as the
    bare mononym "Nagarjuna"; the default keeps the titled form and misses."""
    db = init_db(tmp_path / "b.db")
    pid = db.execute(
        "INSERT INTO person (primary_name) VALUES ('Acarya Nagarjuna')").lastrowid
    v = _HardNameVerifier({"Nagarjuna": "wikidata:Q171195"})

    assert verify.verify_person(db, [v], pid) == "unmatched"          # titled form misses
    assert verify.verify_person(db, [v], pid, extensions=True) == "matched"
    assert (db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0]
            == "wikidata:Q171195")


def test_extensions_off_by_default_in_verify_all(tmp_path, monkeypatch):
    """verify_all threads extensions through to verify_person (default False)."""
    db = init_db(tmp_path / "c.db")
    db.execute("INSERT INTO person (primary_name, verification_status) "
               "VALUES ('Acarya Nagarjuna', 'provisional')")
    seen = []

    def _spy(conn, verifiers, pid, *, commit=True, extensions=False,
             defer_to_joint=False):
        seen.append(extensions)
        return "unmatched"

    monkeypatch.setattr(verify, "verify_person", _spy)
    verify.verify_all(db, verifiers=[], kinds=("person",))
    verify.verify_all(db, verifiers=[], kinds=("person",), extensions=True)
    assert seen == [False, True]


# ── transliteration variants (vocab.json _translit_variant) ───────────────────
def test_translit_variants_expands_known_phonetic_forms():
    H.reload()
    out = H.translit_variants("Lozang Chokyi Gyaltsen")
    keys = {fold_key(v) for v in out}
    # z↔b variants of the first token, rest preserved
    assert fold_key("Lobzang Chokyi Gyaltsen") in keys
    assert fold_key("Lobsang Chokyi Gyaltsen") in keys
    # never returns the input itself, deduped
    assert fold_key("Lozang Chokyi Gyaltsen") not in keys


def test_translit_variants_empty_when_no_known_token():
    H.reload()
    assert H.translit_variants("Robert Thurman") == []


def test_person_query_forms_includes_translit_under_extensions():
    db = init_db(":memory:")
    pid = db.execute("INSERT INTO person (primary_name) VALUES ('Lozang Gyatso')").lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Lozang Gyatso', 'english', ?)", (pid, fold_key("Lozang Gyatso")))
    H.reload()
    forms = verify._person_query_forms(db, pid, "Lozang Gyatso", extensions=True)
    keys = {fold_key(f) for f in forms}
    assert fold_key("Lobsang Gyatso") in keys or fold_key("Lobzang Gyatso") in keys
    # default (no extensions) does NOT expand
    base = verify._person_query_forms(db, pid, "Lozang Gyatso", extensions=False)
    assert all("lobsang" not in fold_key(f) and "lobzang" not in fold_key(f) for f in base)
