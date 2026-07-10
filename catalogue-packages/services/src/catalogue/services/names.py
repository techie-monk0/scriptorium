"""Personal-name normalization (§4.2 / punch-list M4).

`split_name_dates` peels trailing biographical dates off a personal name
("Tsongkhapa, 1357-1419" → "Tsongkhapa" + "1357-1419") so the name proper goes
in `person.primary_name` and the dates go in `person.dates`. Dropping the date
tail also lets the fold-key dedup merge a dated and an undated spelling of the
same person.

`normalize_person_dates` applies that to existing rows and merges the duplicates
it exposes — used by the one-time cleanup and re-usable after future loads.
"""
from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

from catalogue.db_store import contributor_store as cs
from catalogue.db_store import VOCAB_PATH, add_alias, authority_vocab, fold_key


def _acc(db):
    """A system Access over this connection — engine-routed person reads/writes, the row-snapshot
    journal (for the fold-collapse hard-deletes + scalar/alias updates), and edition reads."""
    from catalogue.access_api import system_conn
    return system_conn(db)


def _reads(db):
    """The person READ surface bound over this caller's connection (engine-routed, live-only)."""
    return _acc(db).persons.reads

# Trailing dates on a personal name. Conservative on purpose: it requires a
# separator (comma / paren / space) AND either a 3–4 digit year, a qualifier
# (b./d./fl./ca./r.) + year, or an "Nth century" — so ordinary names and
# digit-mojibake like "Cabez6n" or a roman-numeral epithet are left untouched.
_DATE_TAIL = re.compile(
    r"""[\s,(]+
        (?:
            (?:b\.?|d\.?|fl\.?|ca?\.?|circa|r\.?)\s*\d{1,4}(?:\s*[-–—/]\s*\d{0,4})?  # qualifier + year(s)
          | \d{3,4}(?:\s*[-–—/]\s*\d{0,4})?(?:\s*(?:CE|BCE|AD|BC))?                   # 3–4 digit year / range
          | \d{1,2}\s*(?:st|nd|rd|th)?\s*(?:cent\.?|century)                         # Nth century
        )
        \s*\)?\.?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def split_name_dates(name: str):
    """Return (clean_name, dates). `dates` is the trailing date chunk without its
    leading separator/parens, or None. The clean name never ends with a date."""
    if not name:
        return name, None
    m = _DATE_TAIL.search(name)
    if not m:
        return name.strip(), None
    clean = name[: m.start()].rstrip(" ,(").strip()
    if not clean:                       # the whole string was a date — leave it
        return name.strip(), None
    # Trim the leading separator and any wrapping parens, but keep abbreviation
    # periods ("14th cent.").
    dates = name[m.start():].strip().lstrip(" ,(").rstrip(" )").strip()
    return clean, (dates or None)


# ── Contributor blob splitting (M4) ───────────────────────────────────────────
# A contributor field is sometimes several people mashed together. Splitting is
# precision-first: we auto-split ONLY on high-confidence separators, never on a
# bare comma (which is just as likely "Surname, Given" or "Name, Title VII").
# A trailing role marker "(TRN)"/"(ed.)" is stripped; a single-comma piece that
# looks like "Surname, Given" is reordered to "Given Surname". Comma-joined
# multi-person blobs are left intact and reported by `is_ambiguous_blob` so a
# human can split them, rather than risk wrong auto-splits.
_ROLE_MARKER = re.compile(
    r"\s*\((?:trn|tr|trans\.?|translator|ed\.?|eds\.?|editor|author|comp\.?|compiler)\)",
    re.IGNORECASE)
# High-confidence people separators: semicolon, ampersand (spaced).
_HARD_SEP = re.compile(r"\s*;\s*|\s+&\s+")
# Generational suffix on a single name ("…, Jr" / "… Sr."). Peeled off before the
# Surname/Given reorder and ignored when merging, so "Lopez, Donald S. Jr",
# "Donald S. Lopez, Jr" and "Donald S. Lopez" collapse to one person.
_GEN_SUFFIX = re.compile(r",?\s*(jr|sr)\.?\s*$", re.IGNORECASE)
# Title/epithet words: if they appear after a single comma it's "Name, Title"
# (one person), not "Surname, Given".
_TITLE_WORDS = {
    "dalai", "lama", "rinpoche", "rinpoché", "tulku", "geshe", "khenpo", "khen",
    "panchen", "karmapa", "sakya", "kyabje", "gyalwang", "ven", "the", "jr", "sr",
    "phd", "khentrul", "rinpoché", "lopön", "lopon", "khenchen", "gen", "ani",
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii",
    "xiii", "xiv", "xv", "xvi", "xvii",
}


def _looks_surname_given(before: str, after: str) -> bool:
    """True for a single-comma "Surname, Given" piece (→ reorder), False for
    "Name, Title" or a multi-token surname (→ leave whole)."""
    if not before or not after or " " in before.strip():
        return False                      # multi-token before → not a bare surname
    toks = after.strip().split()
    if not toks:
        return False
    if any(t.strip(".").lower() in _TITLE_WORDS for t in toks):
        return False                      # "Name, Dalai Lama VII" → keep whole
    return after.strip()[0].isalpha()


def split_contributors(raw: str) -> list[str]:
    """Split a contributor field into individual names, conservatively. Auto-
    splits on ';' and '&', strips role markers, reorders "Surname, Given", and
    dedupes by fold-key. Bare-comma multi-person blobs are NOT split (see
    `is_ambiguous_blob`) — they stay as one string for human review."""
    if not raw:
        return []
    out: list[str] = []
    for seg in _HARD_SEP.split(raw.strip()):
        seg = _ROLE_MARKER.sub("", seg)
        # Drop a trailing "[inverted form]" duplicate, e.g.
        # "Beth Newman [Newman, Beth]" → "Beth Newman".
        seg = re.sub(r"\s*\[[^\]]*\]\s*$", "", seg)
        seg = seg.strip().strip(",;").strip()
        if not seg:
            continue
        m = _GEN_SUFFIX.search(seg)
        suffix = ""
        if m:                                  # peel "Jr"/"Sr" before reordering
            suffix = " " + m.group(1).capitalize()
            seg = seg[: m.start()].strip().strip(",").strip()
        if seg.count(",") == 1:
            before, after = (p.strip() for p in seg.split(","))
            if _looks_surname_given(before, after):
                seg = f"{after} {before}"
        out.append(seg + suffix)
    # dedupe by fold-key, preserve order
    seen, res = set(), []
    for n in out:
        k = fold_key(n)
        if k and k not in seen:
            seen.add(k)
            res.append(n)
    return res or ([raw.strip()] if raw.strip() else [])


def is_ambiguous_blob(name: str) -> bool:
    """True if `name` still looks like multiple comma-joined people after a
    confident split — i.e. it should be split, but only a human can do it safely.
    Heuristic: ≥2 commas, or one comma whose halves are both multi-token names
    that aren't a "Name, Title" pair."""
    parts = split_contributors(name)
    if len(parts) != 1:
        return False                       # it already split cleanly
    piece = parts[0]
    n_commas = piece.count(",")
    if n_commas >= 2:
        return True
    if n_commas == 1:
        before, after = (p.strip() for p in piece.split(","))
        # A title/epithet on EITHER side means one person ("Seventh Karmapa, Chötra
        # Gyatso" = Title,Name; "Name, Dalai Lama VII" = Name,Title). Only when
        # neither side is a title and both are multi-word is it likely two people
        # ("Je Tsongkhapa, Gavin Kilty").
        has_title = any(t.strip(".").lower() in _TITLE_WORDS
                        for t in (before + " " + after).split())
        if (not _looks_surname_given(before, after) and not has_title
                and " " in before and " " in after):
            return True
    return False


def normalize_person_dates(db, *, commit: bool = True) -> dict:
    """Strip trailing dates from every `person.primary_name` (moving them to
    `person.dates`), clean alias texts, and merge persons that collapse onto the
    same fold-key once their dates are gone. Idempotent. Returns a summary."""
    persons = [(p.id, p.primary_name, p.dates) for p in _reads(db).directory()]  # LIVE persons only
    groups: dict[str, list[int]] = defaultdict(list)
    info: dict[int, tuple[str, str | None]] = {}
    for pid, name, dates in persons:
        clean, parsed = split_name_dates(name or "")
        info[pid] = (clean, parsed or dates)
        groups[fold_key(clean)].append(pid)

    merged = 0
    for pids in groups.values():
        pids = sorted(pids)
        canon = pids[0]
        canon_clean, canon_dates = info[canon]
        for dup in pids[1:]:
            _, dup_dates = info[dup]
            # Repoint contributor edges (both FRBR homes), then drop the duplicate.
            cs.repoint_person(db, dup, canon)
            if not canon_dates and dup_dates:
                canon_dates = dup_dates
            _acc(db).journal.clear("person", "id", [dup])
            merged += 1
        _acc(db).journal.update_row(
            "person", {"primary_name": canon_clean, "dates": canon_dates}, {"id": canon})
        _clean_aliases(db, canon)

    if commit:
        db.commit()
    return {"persons_before": len(persons), "merged": merged,
            "persons_after": len(persons) - merged}


def _clean_aliases(db, pid: int) -> None:
    """Strip dates from a person's alias texts and keep normalized_key in sync,
    dropping any alias that would duplicate another after cleaning. Guarantees a
    clean alias for the (now dateless) primary name exists."""
    acc = _acc(db)
    rows = [(aid, text) for aid, text, _scheme in acc.persons.reads.aliases(pid)]
    seen_keys = set()
    for aid, text in rows:
        clean, _ = split_name_dates(text or "")
        key = fold_key(clean)
        if key in seen_keys:
            acc.journal.clear("person_alias", "id", [aid])
            continue
        seen_keys.add(key)
        if clean != text:
            acc.journal.update_row(
                "person_alias", {"text": clean, "normalized_key": key}, {"id": aid})
    name = _reads(db).get(pid).primary_name
    if fold_key(name) not in seen_keys:
        add_alias(db, "person", pid, name, "english")


def _editions_of(db, work_ids, blob_pid: int) -> set:
    """Editions a split should put a translator part on: those containing the blob's
    authored works, plus any the blob itself already translated."""
    eids = set(cs.person_edition_ids_as_translator(db, blob_pid))
    for wid in work_ids:
        for ed in _acc(db).editions.reads.by_work(wid):
            eids.add(ed.id)
    return eids


def split_existing_contributors(db, *, apply: bool = False) -> dict:
    """Split person rows whose `primary_name` is a confident multi-person blob
    (semicolon/ampersand-joined) into separate, deduped persons, repointing
    work_contributor + edition_work links and removing the now-empty blob row.

    Bare-comma blobs that *might* be multiple people are NOT touched — they're
    returned under `ambiguous` for manual review (auto-splitting them risks
    breaking "Surname, Given" / "Name, Title").

    With apply=False (default) this is a DRY RUN: it computes and returns the
    plan (counts, samples, ambiguous list) without mutating anything."""
    from .promote import get_or_create_person   # deferred: promote imports names

    rows = [(p.id, p.primary_name) for p in _reads(db).directory()]   # LIVE persons only
    plan = []        # (pid, blob, [split names])
    ambiguous = []   # (pid, blob) — review by hand
    for pid, name in rows:
        parts = split_contributors(name or "")
        if len(parts) > 1:
            plan.append((pid, name, parts))
        elif is_ambiguous_blob(name or ""):
            ambiguous.append((pid, name))

    summary = {
        "applied": apply,
        "blobs_split": len(plan),
        "new_links": sum(len(p[2]) for p in plan),
        "ambiguous_for_review": len(ambiguous),
        "sample_split": [(b, parts) for _, b, parts in plan[:8]],
        "sample_ambiguous": [b for _, b in ambiguous[:12]],
    }
    if not apply:
        return summary

    for blob_pid, _blob, parts in plan:
        new_pids = []
        for nm in parts:
            np, _ = get_or_create_person(db, nm)
            if np != blob_pid and np not in new_pids:
                new_pids.append(np)
        if not new_pids:
            continue
        # Each split person inherits the blob's role-per-edge: author on every work
        # the blob authored (work_author), translator on every edition it translated
        # (edition_translator). Then the blob is detached and dropped.
        authored = [w for w, _r in _acc(db).persons.reads.authored_work_roles(blob_pid)]
        editions = _editions_of(db, authored, blob_pid)
        for np in new_pids:
            for wid in authored:
                cs.add_work_author(db, wid, np)
            for eid in editions:
                cs.add_edition_translator(db, eid, np)
        cs.detach_person(db, blob_pid)
        _acc(db).journal.clear("person", "id", [blob_pid])   # cascade aliases

    db.commit()
    return summary


# Person merge now lives in the access-API engine (acc.persons.writes.merge); the legacy
# `_merge_person` helper was retired once every caller (apply_merge + _merge_by_fold_key) routed
# through it. See contributor_edit.apply_merge.
def _merge_fold(name: str) -> str:
    """fold_key, minus a trailing generational suffix, so 'Donald S. Lopez Jr'
    and 'Donald S. Lopez' merge."""
    return re.sub(r"\s+(jr|sr)$", "", fold_key(name))


def _merge_by_fold_key(db) -> int:
    """Merge all persons whose primary_name shares a merge-fold (lowest id wins). Each merge runs
    through the access-API engine, staged onto `db` (bind_conn) — the caller owns the commit.
    `allow_cross_authority` since a name-fold merge is by design authority-agnostic (the legacy
    `_merge_person` it replaces never checked)."""
    from catalogue.access_api import system_conn
    from catalogue.contracts import Ref
    groups: dict[str, list[int]] = defaultdict(list)
    for pid, name in _acc(db).persons.reads.live_names():
        groups[_merge_fold(name)].append(pid)
    acc = system_conn(db)
    merged = 0
    for pids in groups.values():
        if len(pids) < 2:
            continue
        pids.sort()
        for dup in pids[1:]:
            acc.persons.writes.merge(Ref("person", dup), Ref("person", pids[0]),
                                     allow_cross_authority=True)
            merged += 1
    return merged


# ── Dalai Lama canonicalization (curated — VERIFY against BDRC/VIAF) ───────────
# Ordinal-aware: we parse whatever number the source gives ("Seventh"/"VII"/"7th"
# → VII) so different incarnations stay distinct; a bare "Dalai Lama" defaults to
# the current (XIV) per the operator's rule. The name→number table is small,
# well-known, and HAND-CURATED from model knowledge — treat as provisional until
# an authority file confirms it.
_ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
          "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII"]
_ROMAN_VAL = {r.lower(): i for i, r in enumerate(_ROMAN) if r}
_WORD_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
             "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
             "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14}
_DALAI_LAMA_NAMES = {           # personal name → incarnation number
    "tenzin gyatso": 14,
    "kalzang gyatso": 7, "kelzang gyatso": 7, "bskal bzang rgya mtsho": 7,
    "ngawang lobsang gyatso": 5,
}


def canonical_dalai_lama(name: str):
    """If `name` denotes a Dalai Lama, return the canonical 'Dalai Lama <roman>'
    (bare/14th/Tenzin Gyatso → XIV; Seventh/VII/Kalzang Gyatso → VII; …). Else None."""
    import re
    s = " ".join(re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())
    if not s:
        return None
    has_dl = "dalai lama" in s
    toks = s.split()
    n = None
    for t in toks:
        if t in _WORD_ORD:
            n = _WORD_ORD[t]; break
        if t in _ROMAN_VAL:
            n = _ROMAN_VAL[t]; break
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", t)
        if m:
            n = int(m.group(1)); break
    if has_dl:
        if n is None:
            for nm, num in _DALAI_LAMA_NAMES.items():
                if nm in s:
                    n = num; break
            n = n or 14                      # bare "Dalai Lama" → current incarnation
        return f"Dalai Lama {_ROMAN[n]}" if 0 < n < len(_ROMAN) else None
    # No "dalai lama" in the string — only map a known personal name.
    for nm, num in _DALAI_LAMA_NAMES.items():
        if nm in s:
            return f"Dalai Lama {_ROMAN[num]}"
    return None


def normalize_person_names(db, *, apply: bool = False) -> dict:
    """Reorder single "Surname, Given" rows → "Given Surname", canonicalize Dalai
    Lama variants, then merge the duplicates these expose (e.g. three 'Donald S.
    Lopez' spellings → one). Blobs are left to `split_existing_contributors`.
    apply=False is a dry run."""
    renames = []                              # (pid, old, new)
    for p in _reads(db).directory():          # LIVE persons only
        pid, name = p.id, p.primary_name
        parts = split_contributors(name or "")
        if len(parts) != 1:
            continue                          # multi-person blob — not here
        cand = parts[0]
        new = canonical_dalai_lama(cand) or cand
        if new != (name or ""):
            renames.append((pid, name, new))
    summary = {"applied": apply, "renamed": len(renames),
               "sample": [(o, n) for _, o, n in renames[:12]]}
    if not apply:
        # report how many merges the renames would cause
        rename_by_pid = {p: nn for p, _o, nn in renames}
        seen: dict[str, list[int]] = {}
        for p in _reads(db).directory():      # LIVE persons only
            nm = rename_by_pid.get(p.id, p.primary_name)
            seen.setdefault(fold_key(nm), []).append(p.id)
        summary["merges"] = sum(len(v) - 1 for v in seen.values() if len(v) > 1)
        return summary

    for pid, old, new in renames:
        _acc(db).journal.update_row("person", {"primary_name": new}, {"id": pid})
        if not _acc(db).persons.reads.has_alias_key(pid, fold_key(old)):
            add_alias(db, "person", pid, old, "english")   # keep the old form searchable
    summary["merged"] = _merge_by_fold_key(db)
    db.commit()
    return summary


# ── Curated resolution of the flagged comma-blobs (VERIFY) ────────────────────
# Hand-authored from model knowledge for the specific blobs flagged by
# `is_ambiguous_blob` (operator-approved). Each entry: blob primary_name →
# [(canonical_name, role, [aliases])]. Names run through canonical_dalai_lama and
# fold-key dedup on apply, so they merge onto existing persons.
FLAGGED_BLOBS = {
    "Venerable Rendawa, Zho-nu Lo-dro":
        [("Rendawa Zhönu Lodrö", "author", ["Rendawa", "Zhönu Lodrö"])],
    "Loden Sherap Dagyab [Dagyab, Loden Sherap]":
        [("Loden Sherap Dagyab", "author", [])],
    "Je Tsongkhapa, Gavin Kitty":
        [("Tsongkhapa", "author", ["Je Tsongkhapa"]),
         ("Gavin Kilty", "translator", ["Gavin Kitty"])],
    "Red Pine, Bill Porter, Mike O'Connor":
        [("Bill Porter", "translator", ["Red Pine"]),
         ("Mike O'Connor", "translator", [])],
    "Jetsun Milarepa, Rinpoche Kunga, Brian Cutillo, Amy":
        [("Milarepa", "author", ["Jetsun Milarepa"]),
         ("Lama Kunga Rinpoche", "translator", []),
         ("Brian Cutillo", "translator", [])],
    "Trijang Rinpoche, Dalai Lama, Sharpa Tulku Tenzin Trinley":
        [("Trijang Rinpoche", "author", []),
         ("Dalai Lama XIV", "author", ["Dalai Lama"]),
         ("Sharpa Tulku Tenzin Trinley", "translator", [])],
}


def apply_flagged_blobs(db, mapping=None, *, commit: bool = True) -> dict:
    """Resolve the curated comma-blobs into correctly-roled, deduped persons."""
    from .promote import get_or_create_person
    mapping = mapping if mapping is not None else FLAGGED_BLOBS
    out = {"resolved": 0, "persons_created_or_linked": 0}
    for blob_name, people in mapping.items():
        bpid = _acc(db).persons.reads.id_by_name(blob_name)
        if bpid is None:
            continue
        authored = list(dict.fromkeys(
            w for w, _r in _acc(db).persons.reads.authored_work_roles(bpid)))
        editions = _editions_of(db, authored, bpid)
        for cname, role, aliases in people:
            canon = canonical_dalai_lama(cname) or cname
            np, _ = get_or_create_person(db, canon)
            for a in aliases:
                if not _acc(db).persons.reads.has_alias_key(np, fold_key(a)):
                    add_alias(db, "person", np, a, "english")
            # author → the blob's authored works; translator → its translated editions.
            if role == "translator":
                for eid in editions:
                    cs.add_edition_translator(db, eid, np)
            else:
                for wid in authored:
                    cs.add_work_author(db, wid, np, role)
            out["persons_created_or_linked"] += 1
        cs.detach_person(db, bpid)
        _acc(db).journal.clear("person", "id", [bpid])
        out["resolved"] += 1
    if commit:
        db.commit()
    return out


# ── Organizations misfiled as persons (translation groups / committees) ────────
# A "Padmakara Translation Group" is a contributor (the translator) but NOT a
# person, so it will never match a person authority (BDRC/VIAF/Wikidata person).
# We mark such rows verification_status='organization' — keeping their work edges
# intact but dropping them from the person match worklist (the picker selects only
# provisional+external_id-null rows). Markers live in vocab.json `_organization`.
_DEFAULT_ORG_MARKERS = (
    "translation group", "translation committee", "translation society",
    "translation team", "committee", "publications", "publishing", "press",
    "foundation", "institute", "society", "sangha", "monastery", "association",
    "editions",
)


def _plain_words(text: str) -> str:
    """Lowercase, strip diacritics, reduce to space-separated alphanumeric WORDS,
    padded with spaces. Deliberately NOT fold_key — fold_key collapses digraphs and
    removes spaces ('sangha'→'sanga', joined), which cross-word-matched 'Arya Asanga'.
    Word-boundary matching on this plain form keeps 'Asanga' (a person) clear of the
    'sangha' marker."""
    import unicodedata
    d = unicodedata.normalize("NFKD", text or "")
    d = "".join(c for c in d if not unicodedata.combining(c)).lower()
    return " " + " ".join(re.findall(r"[a-z0-9]+", d)) + " "


@lru_cache(maxsize=1)
def _org_markers() -> tuple:
    """Organization name markers from vocab.json `_organization` (falls back to the
    built-ins), each as a space-padded plain-word phrase for word-boundary matching.
    Call reload_org_markers() after editing the JSON."""
    markers = _DEFAULT_ORG_MARKERS
    data = authority_vocab.vocab_config(VOCAB_PATH)
    if isinstance(data.get("_organization"), list) and data["_organization"]:
        markers = data["_organization"]
    return tuple(_plain_words(m) for m in markers if m and m.strip())


def reload_org_markers() -> None:
    authority_vocab.reload()
    _org_markers.cache_clear()


def is_organization_name(name: str) -> bool:
    """True if `name` looks like an organization, not a person — a WORD-BOUNDARY match
    of an `_organization` marker phrase against the name (case/diacritic-insensitive).
    'Padmakara Translation Group' matches 'translation group'; 'Arya Asanga' does NOT
    match 'sangha' (word-boundary, not substring)."""
    hay = _plain_words(name)
    return hay.strip() != "" and any(m in hay for m in _org_markers())


def mark_organizations(db, *, apply: bool = False, commit: bool = True) -> dict:
    """Flag every provisional person whose name matches an organization marker as
    verification_status='organization' (off the person match worklist; work edges
    kept). Idempotent. `apply=False` is a dry-run — returns the rows that WOULD be
    marked without writing."""
    rows = [(r[0], r[1]) for r in _reads(db).unresolved()]   # provisional+unbound LIVE persons
    # Skip comma-blobs (a person + an org joined, e.g. "Jamgön Kongtrul…, Kalu Rinpoche
    # Translation Group") — those go through SPLIT first; only their org half is marked.
    hits = [{"id": pid, "name": name} for pid, name in rows
            if "," not in name and is_organization_name(name)]
    if apply and hits:
        for h in hits:   # hits are provisional + unbound, so the guarded write always lands
            _acc(db).persons.writes.set_kind_if_unbound(h["id"], "organization")
        if commit:
            db.commit()
    return {"matched": len(hits), "applied": bool(apply and hits), "rows": hits}


def set_person_kind(db, pid: int, *, organization: bool, commit: bool = True) -> bool:
    """Manual toggle (picker action): mark a person as an organization, or revert an
    organization back to provisional. Never touches a person already bound to an
    external authority. Returns True if a row changed."""
    new = "organization" if organization else "provisional"
    changed = _acc(db).persons.writes.set_kind_if_unbound(pid, new)
    if commit:
        db.commit()
    return changed
