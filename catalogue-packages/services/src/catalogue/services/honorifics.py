"""Honorific / title stripping for author-name MATCHING (never for storage).

Buddhist author names carry titles that are not part of the personal name:
Tibetan (Geshe, Lama, Kyabje, Rinpoche, Khenpo, Tulku, Lopön…), Sanskrit/Pali
(Acharya, Pandita, Bhikshu, Shri, Thera…), and Western courtesy (Dr, Ven, Prof).
Two records for one author often differ only by title — "Geshe Lhundub Sopa" vs
"Lhundub Sopa", "Kyabje Trijang Rinpoche" vs "Trijang Rinpoche" — so name
matching must compare title-free forms. We strip ONLY for comparison/search;
stored `primary_name` and aliases keep whatever the source wrote (the §4.2
invariant: never mangle stored text).

CRITICAL EXCEPTION — an OFFICE plus an ORDINAL is the identity, not a title.
"14th Dalai Lama" and "7th Dalai Lama" are different people; "16th Karmapa" ≠
"17th Karmapa". When an office word (dalai, panchen, karmapa, trizin…) co-occurs
with an ordinal (14th / fourteenth / XIV), `strip_honorifics` returns the name
unchanged. Office words are also never in the honorific list, so they survive
token stripping regardless — a double guard.

Lists live in catalogue/vocab.json under `_honorific` / `_office` (underscore =
config, NOT a DB lookup table — db.load_vocab skips `_`-prefixed keys). Adding a
title is a data edit, matching the open-vocab convention (§12.4).

Comparison is on `fold_key` forms, so diacritic/digraph variants of a title
(Rinpoché/Rinpoche, Khenpo/Kenpo, Śrī/Shri/Sri) all collapse to one — the JSON
need only list a plain ASCII form.
"""
from __future__ import annotations

import re
from functools import lru_cache

from catalogue.db_store import VOCAB_PATH, authority_vocab, fold_key

# Built-in fallback used when vocab.json lacks the keys (older deployments). The
# JSON, when present, REPLACES these — edit the JSON, not this, to extend.
_DEFAULT_HONORIFICS = (
    # Tibetan
    "geshe", "geshema", "lama", "rinpoche", "kyabje", "tulku", "khenpo",
    "khenchen", "khentrul", "khen", "lopon", "ponlop", "gen", "ani", "je",
    "jetsun", "jetsunma", "choje", "terton", "togden", "gomchen", "drupon",
    "drubpon", "gyaltsab", "naljorpa", "lharampa",
    # Sanskrit / Pali
    "acharya", "acarya", "pandita", "pandit", "mahapandita", "bhikshu",
    "bhikkhu", "bhikshuni", "bhikkhuni", "shri", "sri", "arya", "sthavira",
    "thera", "mahathera", "mahasiddha", "siddha", "guru", "swami", "upasaka",
    # other Buddhist
    "sayadaw", "ajahn", "ajaan", "roshi", "sensei",
    # Western courtesy / academic
    "venerable", "ven", "rev", "reverend", "dr", "prof", "professor",
    "mr", "mrs", "ms", "sir",
)
# Office words: identity-bearing (with an ordinal), so NEVER stripped. Kept as a
# distinct set so they survive even though e.g. "lama" (a courtesy title) is
# stripped — "Dalai Lama" keeps "Dalai", the distinguishing word.
_DEFAULT_OFFICES = (
    "dalai", "panchen", "karmapa", "trizin", "tripa", "shamarpa", "sharmapa",
    "gyalwang", "gyalwa", "ganden",
)

# Sanskrit scholarly titles that prefix a classical MONONYM (Ācārya Nāgārjuna,
# Ārya Asaṅga, Paṇḍita Kamalaśīla). Stripping one leaves a single COMPLETE name,
# so under `extended` matching the single-name guard is bypassed for these — unlike
# a Tibetan courtesy title, where the residue ("Lama Yeshe" → "Yeshe") is too weak a
# key. (These are already in _DEFAULT_HONORIFICS; this set only marks the subset
# whose lone residue is trustworthy.)
_MONONYM_TITLE_KEYS = frozenset(
    fold_key(t) for t in ("acarya", "acharya", "arya", "pandita", "mahapandita",
                          "pandit"))

# Written ordinals (office incumbents are usually written out in English titles).
_WRITTEN_ORDINALS = frozenset((
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
    "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
    "twentieth", "twentyfirst", "twentysecond", "twentythird", "twentyfourth",
    "twentyfifth",
))


def _roman_set(n: int = 40) -> frozenset:
    """Lowercase roman numerals 1..n, for ordinal detection (Dalai Lama XIV)."""
    vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
            (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"),
            (4, "iv"), (1, "i")]
    out = set()
    for i in range(1, n + 1):
        s, x = "", i
        for v, sym in vals:
            while x >= v:
                s += sym
                x -= v
        out.add(s)
    return frozenset(out)


_ROMAN = _roman_set()
_DIGIT_ORDINAL = re.compile(r"\b\d{1,2}(st|nd|rd|th)\b", re.IGNORECASE)
_DIGIT_ORDINAL_TOKEN = re.compile(r"^\d{1,2}(st|nd|rd|th)$", re.IGNORECASE)


def is_ordinal_token(tok: str) -> bool:
    """True if `tok` (a lowercased, diacritic-stripped name token — NOT fold-keyed)
    is an ordinal marker: a digit ordinal ('14th'), a written ordinal ('fourteenth'),
    or a 2+ char roman numeral ('xiv'). Single-letter romans (i/v/x) are NOT treated
    as ordinals — they're usually initials, and ordinal_value() still compares those
    by value. Used to drop the ordinal from a name-token set so '14th'/'Fourteenth'/
    'XIV' forms of one incumbent compare equal on the rest of the name."""
    t = (tok or "").strip(".")
    if _DIGIT_ORDINAL_TOKEN.match(t):
        return True
    if t.lower() in _WRITTEN_ORDINAL_VALUE:
        return True
    return len(t) >= 2 and t.lower() in _ROMAN

# value lookups for ordinal_value(): written word → int, roman → int.
_WRITTEN_ORDINAL_VALUE = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twentyfirst": 21, "twentysecond": 22, "twentythird": 23,
    "twentyfourth": 24, "twentyfifth": 25,
}


def _roman_to_int(s: str) -> "int | None":
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    if not s or any(ch not in vals for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


def ordinal_value(name: str) -> "int | None":
    """The regnal/ordinal number in `name` as an int, unifying the three forms:
    '14th'/'XIV'/'fourteenth' → 14. None if there's no ordinal. Used to keep
    '14th Dalai Lama' and '7th Dalai Lama' from matching (same office, different
    incumbent). Roman is checked only for a lone numeral token, so the 'i' in a
    name doesn't get read as 1."""
    if not name:
        return None
    m = _DIGIT_ORDINAL.search(name)
    if m:
        try:
            return int(m.group(0)[:-2])
        except ValueError:
            pass
    for tok in _plain(name).split():
        t = tok.strip(".")
        if t in _WRITTEN_ORDINAL_VALUE:
            return _WRITTEN_ORDINAL_VALUE[t]
        if t in _ROMAN:
            rv = _roman_to_int(t)
            if rv:
                return rv
    return None


@lru_cache(maxsize=1)
def _lists() -> tuple[frozenset, frozenset]:
    """(honorific_keys, office_keys) as fold_key sets. Reads vocab.json once;
    falls back to the built-ins when the keys are absent. Call `reload()` after
    editing the JSON in a long-running process."""
    hon, off = set(_DEFAULT_HONORIFICS), set(_DEFAULT_OFFICES)
    data = authority_vocab.vocab_config(VOCAB_PATH)
    if isinstance(data.get("_honorific"), list):
        hon = set(data["_honorific"])
    if isinstance(data.get("_office"), list):
        off = set(data["_office"])
    return (frozenset(fold_key(h) for h in hon if h),
            frozenset(fold_key(o) for o in off if o))


@lru_cache(maxsize=1)
def _translit_groups() -> tuple:
    """Transliteration-variant groups from vocab.json `_translit_variant`, each as a
    tuple of RAW (lowercased) spellings — kept un-fold-keyed so substitution yields a
    real alternate spelling to query an authority with (e.g. 'choekyi', not the folded
    'coekyi'). Membership is tested by fold_key per member (fold_key can't collapse the
    phonetic variants, which is the whole reason these are listed). Empty if the key is
    absent. See translit_variants()."""
    groups = authority_vocab.vocab_config(VOCAB_PATH).get("_translit_variant") or []
    out = []
    for g in groups:
        if isinstance(g, list) and len(g) >= 2:
            out.append(tuple(dict.fromkeys(s.strip().lower() for s in g if s and s.strip())))
    return tuple(out)


def reload() -> None:
    """Drop the cached lists (e.g. tests that point CATALOGUE_VOCAB elsewhere)."""
    authority_vocab.reload()
    _lists.cache_clear()
    _translit_groups.cache_clear()


def translit_variants(name: str, *, cap: int = 8) -> list:
    """Alternative spellings of `name` produced by substituting any token that is a
    known transliteration variant (vocab.json `_translit_variant`) — e.g.
    "Lozang Chökyi Gyaltsen" → ["Lobzang Chökyi Gyaltsen", "Lobsang …", …]. For
    MATCHING/SEARCH only (never storage), used by the --person-resolution-extensions
    query expansion. Returns variants only (never the input itself), deduped on
    fold_key, bounded to `cap`. Tokens are matched/substituted on their fold_key, so
    diacritics on the rest of the name are preserved from the original token."""
    if not name or not name.strip():
        return []
    groups = _translit_groups()
    if not groups:
        return []
    tokens = name.split()
    # Per-token alternate spellings (fold_keys) for tokens that ARE a known variant.
    alt_at = {}                                   # position -> [alt spellings (raw)]
    for i, tok in enumerate(tokens):
        k = fold_key(tok)
        for g in groups:
            if any(fold_key(s) == k for s in g):
                alt_at[i] = [s for s in g if fold_key(s) != k]
                break
    if not alt_at:
        return []
    import itertools
    out, seen = [], {fold_key(name)}
    positions = sorted(alt_at)
    # Generate by INCREASING number of simultaneously-substituted tokens: all single-
    # token swaps first (a one-token misspelling is far likelier than several), then
    # pairs, etc. So "Lobsang Chokyi Gyaltsen" (1 change) precedes a 3-change form.
    for r in range(1, len(positions) + 1):
        for combo_pos in itertools.combinations(positions, r):
            for choices in itertools.product(*(alt_at[p] for p in combo_pos)):
                toks = list(tokens)
                for p, c in zip(combo_pos, choices):
                    toks[p] = c
                cand = " ".join(toks)
                key = fold_key(cand)
                if key not in seen:
                    seen.add(key)
                    out.append(cand)
                    if len(out) >= cap:
                        return out
    return out


def honorific_keys() -> frozenset:
    """Fold-keyed honorific tokens — used to subtract title noise from a token
    set (see verify._tokens). Office words are deliberately excluded so different
    offices stay distinguishable."""
    return _lists()[0]


def _plain(text: str) -> str:
    """Lowercase + strip diacritics, WITHOUT fold_key's digraph collapse — that
    collapse mangles ordinals ('seventh'→'sevent', 'fourteenth'→'fourteent') and
    roman 'xiv', so ordinal detection must not use it."""
    import unicodedata
    d = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in d if not unicodedata.combining(c)).lower()


def has_ordinal(name: str) -> bool:
    """True if `name` carries a regnal/ordinal marker: a digit ordinal (14th),
    a written ordinal (fourteenth), or a roman numeral token (XIV)."""
    if not name:
        return False
    if _DIGIT_ORDINAL.search(name):
        return True
    for tok in _plain(name).split():
        t = tok.strip(".")
        if t in _WRITTEN_ORDINALS or t in _ROMAN:
            return True
    return False


def _has_office(name: str) -> bool:
    off = _lists()[1]
    return any(fold_key(t).strip(".") in off for t in name.split())


def strip_honorifics(name: str, *, extended: bool = False) -> str:
    """Return `name` with honorific tokens removed, for MATCHING/SEARCH only.

    - Office + ordinal (identity-bearing) → returned unchanged.
    - Otherwise honorific tokens are dropped from anywhere in the name; office
      words and ordinals are kept (default mode).
    - **Single-name guard:** if stripping would leave only ONE name token, the
      original (titled) form is kept instead. A lone given name ("Lama Yeshe" →
      "Yeshe") is too ambiguous — it lets any same-given-name person match. Keeping
      "Lama Yeshe" forces the match to a record that actually carries that fuller
      form. (Also covers the empty case where the string was only a title.)

    `extended=True` (the --person-resolution-extensions name-matching path) loosens
    two things, WITHOUT touching the default behaviour:
      - **Bare office prefix:** an office word on a name carrying NO ordinal is a
        title, not an identity, so it is stripped — "Panchen Lozang Chökyi Gyaltsen"
        → "Lozang Chökyi Gyaltsen" (but "Panchen Lama XI" / "14th Panchen Lama" stay
        whole via the office+ordinal rule above).
      - **Mononym titles:** the single-name guard is bypassed when what was stripped
        is a Sanskrit scholarly title (Ācārya/Ārya/Paṇḍita…) — "Acarya Nagarjuna" →
        "Nagarjuna" — but NOT for a courtesy title (Lama Yeshe → "Yeshe" still
        rejected)."""
    if not name or not name.strip():
        return name
    hon, off = _lists()
    office = _has_office(name)
    if office and has_ordinal(name):
        return name.strip()                 # office + ordinal = identity, never strip
    bare_office = extended and office and not has_ordinal(name)
    tokens = name.split()

    def _drop(tok: str) -> bool:
        k = fold_key(tok).strip(".,")
        return k in hon or (bare_office and k in off)

    kept = [t for t in tokens if not _drop(t)]
    # Need ≥2 surviving name tokens to trust the stripped form; otherwise a bare
    # given name is too weak a key — fall back to the full titled name. Under
    # `extended`, a lone residue is trusted only when a Sanskrit MONONYM title was
    # what we stripped (never a courtesy title — the "Lama Yeshe" false positive).
    if len(kept) < 2:
        if extended and len(kept) == 1:
            survivor = fold_key(kept[0]).strip(".,")
            dropped = {fold_key(t).strip(".,") for t in tokens} - {survivor}
            if dropped & _MONONYM_TITLE_KEYS:
                return kept[0].strip(" ,.")
        return name.strip()
    return " ".join(kept).strip(" ,.")
