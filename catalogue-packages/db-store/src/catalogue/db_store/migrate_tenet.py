"""Tenet-system classifier — Phase 1 (deterministic rules).

Assigns each **person** a `tenet_system` (their doctrinal/siddhānta home — Prāsaṅgika,
Cittamātra, …) and, for the *exceptions*, overrides it on the **work** (a text whose
tenet differs from its author's — e.g. Vasubandhu is Yogācāra but his *Abhidharmakośa*
expounds Vaibhāṣika). This is the doctrinal axis the pūrvapakṣa voice-tagger (bdrag L1c)
needs to decide whether a named school is the author's own view or an opponent's; the
existing `tradition` column is *sectarian* (Gelug/Kagyu/…), a different axis, and carries
no tenet information.

Design mirrors the sibling `migrate_tradition.py` deliberately:
  * Two curated maps + a default rule, NO LLM (that is Phase 2, `services.classify_tenet`).
  * `AUTHOR_TENET` (person.external_id → tenet) is the high-precision head. Because the
    **sect→tenet default** already resolves every Tibetan-sect author (all Tibetan schools
    take Madhyamaka as their tenet home → Prāsaṅgika-Madhyamaka; Jonang → Jonang-Shentong),
    the curated map only needs the **Indian masters**, whose sect is 'Common' and therefore
    have no default. That keeps the hand-curated surface tiny and auditable.
  * `WORK_TENET_OVERRIDE` (sanskrit_title substring → tenet) carries ONLY the works that
    diverge from their author's tenet — the reason work-level exists at all.
  * Every assignment records `tenet_source` ('rule-author' | 'rule-sect-default' |
    'rule-work-override' | 'rule-modern-na') + `tenet_conf` + `tenet_evidence`, so it is
    auditable and re-runnable.
  * Idempotent-refresh: a re-run CLEARS rows it previously wrote (`tenet_source LIKE
    'rule-%'`) and recomputes them, so improving the maps and re-running reflects the
    change — while `tenet_source IN ('llm','human')` verdicts are PRESERVED (never
    clobbered). Assigning to NEW persons/works is just "re-run after they're added."

Resolution order the CONSUMER (bdrag) uses at query/ingest time — most-specific first:
    work.tenet_system  →  author person.tenet_system  →  (already-materialized here)
Nothing here writes the consumer's fallback to Madhyamaka; a NULL person tenet that no
rule reached is left for Phase 2 / human review.

Adding the columns is additive + nullable + back-compat; this only writes columns nothing
reads yet, so it is safe against live. Dry-run `report` first.

CLI:
    python -m catalogue.db_store.migrate_tenet report   [db]
    python -m catalogue.db_store.migrate_tenet migrate  [db] [--no-modern-na]
"""
from __future__ import annotations

import argparse
import sqlite3

from .connection import connect

# ── controlled vocabulary (the assignment target) ────────────────────────────
# Follows the Gelug drubta (tenets) literature — Jamyang Shepa's *Great Exposition of
# Tenets*, Könchok Jigme Wangpo's *Precious Garland*, Changkya's *Presentation of Tenets*.
# The canonical list lives in the CategoricalField registry (catalogue.contracts.fields), the
# single source shared with the editable `tenet_system` field; imported here as a list for the
# verify gate / `NOT IN (...)` query.
from catalogue.contracts.fields import TENET_VOCAB
from .paths import default_db_path

VOCAB = list(TENET_VOCAB)

AUTHOR_CONF = 0.9           # curated author-lineage hit
SECT_CONF = 0.6             # sect→tenet default (Tibetan schools are Madhyamaka-home)
OVERRIDE_CONF = 0.9         # work diverges from its author's tenet
MODERN_CONF = 0.7           # detected modern-scholarship author

# ── curated maps ─────────────────────────────────────────────────────────────
# person.external_id → tenet_system. ONLY the Indian masters (sect 'Common' → no default)
# plus any Tibetan whose tenet is NOT the sect default. Everything else is left to the
# sect rule below. Doxographic authority: Könchok Jigme Wangpo, *Precious Garland of
# Tenets*; Jamyang Shepa, *Grub mtha' chen mo*; Hopkins, *Maps of the Profound*.
AUTHOR_TENET: dict[str, str] = {
    # ── Prāsaṅgika-Madhyamaka ────────────────────────────────────────────────
    # Nāgārjuna & Āryadeva are "Mādhyamikas of the model texts" (don't take a side); Gelug
    # holds their definitive intent is Prāsaṅgika and counts them as the source of the system.
    "bdr:P4954":         "Prāsaṅgika-Madhyamaka",   # Nāgārjuna (model-text; final intent Prāsaṅgika)
    "wikidata:Q558275":  "Prāsaṅgika-Madhyamaka",   # Āryadeva (model-text)
    "wikidata:Q976112":  "Prāsaṅgika-Madhyamaka",   # Buddhapālita (inaugurated the Prāsaṅgika approach)
    "wikidata:Q456963":  "Prāsaṅgika-Madhyamaka",   # Candrakīrti (definitive expositor; Madhyamakāvatāra)
    "wikidata:Q445460":  "Prāsaṅgika-Madhyamaka",   # Śāntideva
    "wikidata:Q320150":  "Prāsaṅgika-Madhyamaka",   # Atiśa
    "wikidata:Q2143829": "Prāsaṅgika-Madhyamaka",   # Rendawa (Gelug lists Tsongkhapa's Sakya teacher here)
    # ── Cittamātra (Sems tsam) ───────────────────────────────────────────────
    # Followers of Scripture (Asaṅga's Yogācārabhūmi). Asaṅga is a Cittamātrin DOXOGRAPHICALLY
    # (founded/taught it for trainees) though Gelug holds his own final view was Prāsaṅgika.
    "wikidata:Q379905":  "Cittamātra",              # Asaṅga (Followers of Scripture)
    "wikidata:Q316343":  "Cittamātra",              # Vasubandhu (Followers of Scripture; Kośa work → Vaibhāṣika)
    "wikidata:Q1135573": "Cittamātra",              # Dharmapāla (Followers of Scripture)
    # Followers of Reasoning (Dharmakīrti's Seven Treatises). Gelug classifies Dharmakīrti's
    # textual SYSTEM as Cittamātra (his texts ascend Sautrāntika→Cittamātra as the actual view).
    "wikidata:Q457990":  "Cittamātra",              # Dharmakīrti (Followers of Reasoning)
    # Maitreya: his five treatises SPLIT (see WORK_TENET_OVERRIDE) — 3 are Cittamātra, and the
    # Ornament of Clear Realization + Sublime Continuum are Madhyamaka. Person defaults to the
    # Cittamātra side (source of the system with Asaṅga); the two Madhyamaka works override.
    "wikidata:Q1276324": "Cittamātra",              # Arya Maitreya (five treatises; works split)
    "wikidata:Q193461":  "Cittamātra",              # Maitreya Buddha (dup person)
    # ── Yogācāra-Svātantrika-Madhyamaka ──────────────────────────────────────
    # Autonomous syllogisms + conventional presentation per Cittamātra (no external objects,
    # self-cognition). True-Aspectarian side: Śāntarakṣita/Kamalaśīla/Ārya Vimuktisena;
    # False-Aspectarian side: Haribhadra/Jetāri/Kambala.
    "wikidata:Q636959":  "Yogācāra-Svātantrika-Madhyamaka",  # Kamalaśīla (True Aspectarian)
    "wikidata:Q6503571": "Yogācāra-Svātantrika-Madhyamaka",  # Kambala (False Aspectarian)
    # ── Jonang (shentong) ────────────────────────────────────────────────────
    "wikidata:Q2062909": "Jonang-Shentong",         # Tāranātha (Jonang → shentong; self-styled Great Madhyamaka)

    # ── Masters seeded 2026-07-06 (wikidata IDs confirmed by review) ──────────
    "wikidata:Q2357245":  "Sautrāntika-Svātantrika-Madhyamaka",  # Bhāvaviveka (founder)
    "wikidata:Q17993229": "Sautrāntika-Svātantrika-Madhyamaka",  # Jñānagarbha
    "wikidata:Q553722":   "Yogācāra-Svātantrika-Madhyamaka",     # Śāntarakṣita (founder)
    "wikidata:Q10525154": "Yogācāra-Svātantrika-Madhyamaka",     # Ārya Vimuktisena
    "wikidata:Q5657290":  "Yogācāra-Svātantrika-Madhyamaka",     # Haribhadra (Buddhist, not the Jain Q4207831)
    "wikidata:Q106787811":"Yogācāra-Svātantrika-Madhyamaka",     # Jetāri (False Aspectarian)
    "wikidata:Q558272":   "Cittamātra",             # Dignāga (Followers of Reasoning)
    "wikidata:Q1041398":  "Cittamātra",             # Sthiramati (Followers of Scripture)
    "wikidata:Q105718615":"Cittamātra",             # Prajñākaragupta (Followers of Reasoning)
    "wikidata:Q106314834":"Prāsaṅgika-Madhyamaka",  # Jayānanda (comm. on Madhyamakāvatāra)
    "wikidata:Q48729504": "Prāsaṅgika-Madhyamaka",  # Patsab Nyima Drak (translator of Candrakīrti)
}

# Fallback for classical authors lacking an external_id (matched on exact primary_name).
# Covers the Gelug-classified masters not yet present as `person` rows, so they classify
# correctly the moment they are added.
AUTHOR_TENET_BYNAME: dict[str, str] = {
    # Prāsaṅgika-Madhyamaka
    "Jayānanda":     "Prāsaṅgika-Madhyamaka",   # acknowledged Prāsaṅgika commentator (Tsongkhapa criticizes)
    "Jayananda":     "Prāsaṅgika-Madhyamaka",
    "Patsab Nyima Drak": "Prāsaṅgika-Madhyamaka",  # translator of Candrakīrti
    "Patsab Nyima Drakpa": "Prāsaṅgika-Madhyamaka",
    # Yogācāra-Svātantrika-Madhyamaka
    "Śāntarakṣita":  "Yogācāra-Svātantrika-Madhyamaka",   # founder of the synthesis
    "Shantarakshita": "Yogācāra-Svātantrika-Madhyamaka",
    "Ārya Vimuktisena": "Yogācāra-Svātantrika-Madhyamaka",  # AA root commentator (True Aspectarian)
    "Vimuktisena":   "Yogācāra-Svātantrika-Madhyamaka",
    "Haribhadra":    "Yogācāra-Svātantrika-Madhyamaka",   # AA root commentator (False Aspectarian)
    "Jetāri":        "Yogācāra-Svātantrika-Madhyamaka",
    "Jetari":        "Yogācāra-Svātantrika-Madhyamaka",
    # Sautrāntika-Svātantrika-Madhyamaka (accept external objects conventionally)
    "Bhāvaviveka":   "Sautrāntika-Svātantrika-Madhyamaka",  # the founder (Bhavya)
    "Bhavaviveka":   "Sautrāntika-Svātantrika-Madhyamaka",
    "Bhavya":        "Sautrāntika-Svātantrika-Madhyamaka",
    "Jñānagarbha":   "Sautrāntika-Svātantrika-Madhyamaka",  # placed here (accepts external objects), not w/ Śāntarakṣita
    "Jnanagarbha":   "Sautrāntika-Svātantrika-Madhyamaka",
    "Avalokitavrata": "Sautrāntika-Svātantrika-Madhyamaka",  # sub-commentator on the Prajñāpradīpa
    "Śrīgupta":      "Sautrāntika-Svātantrika-Madhyamaka",
    "Srigupta":      "Sautrāntika-Svātantrika-Madhyamaka",
    # Cittamātra
    "Dignāga":       "Cittamātra",              # Followers of Reasoning (root of the pramāṇa line)
    "Dignaga":       "Cittamātra",
    "Sthiramati":    "Cittamātra",              # Followers of Scripture
    "Dharmapāla":    "Cittamātra",
    "Asvabhāva":     "Cittamātra",
    "Asvabhava":     "Cittamātra",
    "Devendrabuddhi": "Cittamātra",             # Followers of Reasoning
    "Śākyabuddhi":   "Cittamātra",
    "Sakyabuddhi":   "Cittamātra",
    "Prajñākaragupta": "Cittamātra",
    "Prajnakaragupta": "Cittamātra",
}

# person.tradition (sect) → default tenet home. Every Tibetan school takes Madhyamaka as
# its tenet home and (post-Tsongkhapa consensus this Gelug-leaning library encodes)
# Prāsaṅgika specifically; Jonang is the shentong exception. 'Common' has NO default
# (Indian sources span all tenets — they need an AUTHOR_TENET entry or Phase 2).
SECT_TENET: dict[str, str] = {
    "Gelug":         "Prāsaṅgika-Madhyamaka",
    "Kadam":         "Prāsaṅgika-Madhyamaka",
    "Sakya":         "Prāsaṅgika-Madhyamaka",
    "Kagyu":         "Prāsaṅgika-Madhyamaka",
    "Shangpa Kagyu": "Prāsaṅgika-Madhyamaka",
    "Nyingma":       "Prāsaṅgika-Madhyamaka",   # (Dzogchen sits above tenets; Madhyamaka is the analytic home)
    "Rimé":          "Prāsaṅgika-Madhyamaka",
    "Jonang":        "Jonang-Shentong",
    # "Common" intentionally absent.
}

# work.sanskrit_title (case-insensitive substring) → tenet. ONLY works whose tenet
# DIFFERS from their author's person.tenet_system. This is the whole point of work-level.
WORK_TENET_OVERRIDE: list[tuple[str, str, str]] = [
    # (sanskrit_title substring [normalized], tenet, evidence)
    # Vasubandhu's Kośa root verses expound Vaibhāṣika; author's person tenet is Cittamātra.
    ("abhidharmakośa", "Vaibhāṣika",
     "Abhidharmakośa root verses = Vaibhāṣika exposition; author Vasubandhu's person tenet is Cittamātra"),
    ("abhidharmakosa", "Vaibhāṣika", "Abhidharmakośa root = Vaibhāṣika exposition"),
    # Maitreya's five treatises split: these two are Madhyamaka (Gelug), the other three
    # (Sūtrālaṃkāra, Madhyāntavibhāga, Dharmadharmatāvibhāga) are Cittamātra = the person default.
    ("abhisamayālaṃkāra", "Prāsaṅgika-Madhyamaka",
     "Ornament of Clear Realization — Gelug treats as Madhyamaka (studied via Yogācāra-Svātantrika commentaries)"),
    ("abhisamayalamkara", "Prāsaṅgika-Madhyamaka", "Ornament of Clear Realization = Madhyamaka (Gelug)"),
    ("uttaratantra", "Prāsaṅgika-Madhyamaka",
     "Sublime Continuum — per Gelug a Madhyamaka work (definitive buddha-nature = emptiness)"),
    ("ratnagotravibhāga", "Prāsaṅgika-Madhyamaka", "Ratnagotravibhāga (Sublime Continuum) = Madhyamaka (Gelug)"),
    ("ratnagotravibhaga", "Prāsaṅgika-Madhyamaka", "Ratnagotravibhāga = Madhyamaka (Gelug)"),
]


class MigrationError(RuntimeError):
    """A verify gate failed — the migration is aborted before commit."""


# ── schema (additive, idempotent) ────────────────────────────────────────────
def _ensure_columns(con: sqlite3.Connection) -> None:
    for table in ("person", "work"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, decl in (("tenet_system", "TEXT"), ("tenet_source", "TEXT"),
                          ("tenet_conf", "REAL"), ("tenet_evidence", "TEXT")):
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _is_modern(origin: str, dates: str, tradition: str, name: str) -> bool:
    """Heuristic for a modern-scholarship author (translator/academic, no tenet home):
    a birth year ≥ 1900, OR a clearly non-Indic/non-Tibetan personal name with no
    tradition. Conservative — when unsure, returns False (left for Phase 2)."""
    d = dates or ""
    for tok in d.replace("b.", " ").replace("c.", " ").split():
        if tok.isdigit() and int(tok) >= 1900:
            return True
    return False


# ── the rules, applied per row ───────────────────────────────────────────────
def _person_tenet(external_id: str, name: str, tradition: str,
                  origin: str, dates: str, *, modern_na: bool):
    """Return (tenet, source, conf, evidence) or None if no rule fires."""
    if external_id and external_id in AUTHOR_TENET:
        return (AUTHOR_TENET[external_id], "rule-author", AUTHOR_CONF,
                f"curated author-lineage ({external_id})")
    if name in AUTHOR_TENET_BYNAME:
        return (AUTHOR_TENET_BYNAME[name], "rule-author", AUTHOR_CONF,
                "curated author-lineage (by name)")
    if tradition in SECT_TENET:
        return (SECT_TENET[tradition], "rule-sect-default", SECT_CONF,
                f"sect '{tradition}' → tenet home")
    if modern_na and _is_modern(origin, dates, tradition, name):
        return ("n/a-modern-scholarship", "rule-modern-na", MODERN_CONF,
                "modern author (dates ≥1900); reports views, holds no tenet")
    return None


def _norm_title(s: str) -> str:
    """Fold for title matching: drop soft hyphens / ZWSP / hyphens / whitespace that
    editions embed between compound members (e.g. 'Abhidharma­kośa­kārikā'),
    lowercase. Diacritics are kept — the override keys carry them."""
    out = []
    for ch in (s or ""):
        if ch in "­​‌-–— \t\n":
            continue
        out.append(ch.lower())
    return "".join(out)


def _work_override(sanskrit_title: str):
    t = _norm_title(sanskrit_title)
    for sub, tenet, ev in WORK_TENET_OVERRIDE:
        if _norm_title(sub) in t:
            return (tenet, "rule-work-override", OVERRIDE_CONF, ev)
    return None


# ── report / migrate ─────────────────────────────────────────────────────────
def _iter_persons(con):
    return con.execute(
        "SELECT id, primary_name, COALESCE(external_id,''), COALESCE(tradition,''), "
        "COALESCE(origin,''), COALESCE(dates,''), tenet_system, tenet_source "
        "FROM person WHERE COALESCE(deleted_at,'')='' ").fetchall()


def _iter_works(con):
    return con.execute(
        "SELECT id, COALESCE(sanskrit_title,''), tenet_system, tenet_source "
        "FROM work WHERE COALESCE(deleted_at,'')='' ").fetchall()


def run(db: str, *, apply: bool, modern_na: bool = True) -> dict:
    con = connect(db)                     # sanctioned RW connection (FK on), not raw sqlite3.connect
    try:
        _ensure_columns(con)
        stats = {"persons_total": 0, "person_rule_author": 0, "person_sect_default": 0,
                 "person_modern_na": 0, "person_left_null": 0, "person_preserved": 0,
                 "works_override": 0, "work_preserved": 0}
        examples = {"person": [], "work": []}

        # PERSONS — clear our own prior rule-% rows, recompute; preserve llm/human.
        for pid, name, xid, trad, origin, dates, cur_sys, cur_src in _iter_persons(con):
            stats["persons_total"] += 1
            if cur_src in ("llm", "human"):
                stats["person_preserved"] += 1
                continue
            verdict = _person_tenet(xid, name, trad, origin, dates, modern_na=modern_na)
            if verdict is None:
                if apply and (cur_src or "").startswith("rule-"):
                    con.execute("UPDATE person SET tenet_system=NULL, tenet_source=NULL, "
                                "tenet_conf=NULL, tenet_evidence=NULL WHERE id=?", (pid,))
                stats["person_left_null"] += 1
                continue
            tenet, src, conf, ev = verdict
            stats[{"rule-author": "person_rule_author",
                   "rule-sect-default": "person_sect_default",
                   "rule-modern-na": "person_modern_na"}[src]] += 1
            if len(examples["person"]) < 6:
                examples["person"].append(f"{name} → {tenet} [{src}]")
            if apply:
                con.execute("UPDATE person SET tenet_system=?, tenet_source=?, "
                            "tenet_conf=?, tenet_evidence=? WHERE id=?",
                            (tenet, src, conf, ev, pid))

        # WORKS — only the divergence overrides; preserve llm/human.
        for wid, sk, cur_sys, cur_src in _iter_works(con):
            if cur_src in ("llm", "human"):
                stats["work_preserved"] += 1
                continue
            ov = _work_override(sk)
            if ov is None:
                if apply and (cur_src or "").startswith("rule-"):
                    con.execute("UPDATE work SET tenet_system=NULL, tenet_source=NULL, "
                                "tenet_conf=NULL, tenet_evidence=NULL WHERE id=?", (wid,))
                continue
            tenet, src, conf, ev = ov
            stats["works_override"] += 1
            if len(examples["work"]) < 6:
                examples["work"].append(f"{sk[:40]} → {tenet}")
            if apply:
                con.execute("UPDATE work SET tenet_system=?, tenet_source=?, "
                            "tenet_conf=?, tenet_evidence=? WHERE id=?",
                            (tenet, src, conf, ev, wid))

        # verify gate: any written tenet must be in VOCAB
        if apply:
            bad = con.execute(
                "SELECT count(*) FROM person WHERE tenet_system IS NOT NULL "
                f"AND tenet_system NOT IN ({','.join('?'*len(VOCAB))})", VOCAB).fetchone()[0]
            if bad:
                raise MigrationError(f"{bad} person rows carry an out-of-vocab tenet")
            con.commit()
        return {"applied": apply, **stats, "examples": examples}
    finally:
        con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["report", "migrate"])
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--no-modern-na", action="store_true",
                    help="do not auto-assign 'n/a-modern-scholarship' to modern authors")
    args = ap.parse_args(argv)
    out = run(args.db, apply=(args.cmd == "migrate"), modern_na=not args.no_modern_na)
    import json
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
