"""Tradition classifier — Phase 1 (deterministic rules).

Populates `work_tradition` from two curated maps — a subject-path map and an
author-lineage map keyed by `person.external_id` — plus an optional presumed-Gelug
fallback for otherwise-unclassified Buddhist works. NO LLM (that's Phase 2). It only
ADDS to the (empty) `work_tradition` join, which nothing reads yet, so it is safe to
run against live; dry-run `report` first.

Multi-label: a work may get several traditions (agreement between the two rules boosts
confidence). Every row records `source` ('rule-subject' | 'rule-author' | 'rule-default')
+ `confidence` + an `evidence` trace, so each assignment is auditable and re-runnable.

Idempotent-refresh: each `migrate` first CLEARS existing `source LIKE 'rule-%'` rows and
recomputes them, so improving the maps and re-running reflects the change — while
`source IN ('llm','human')` rows are preserved (INSERT OR IGNORE won't clobber a manual
verdict for the same (work, tradition) pair, and the default rule skips any work that
already carries a manual row).

CLI:
    python -m catalogue.db_store.migrate_tradition report   [db]
    python -m catalogue.db_store.migrate_tradition migrate  [db] [--no-default-gelug]
"""
from __future__ import annotations

import argparse

from .db import init_db
from .paths import default_db_path


class MigrationError(RuntimeError):
    """A verify gate failed — the migration is aborted before commit."""


AUTHOR_CONF = 0.85          # fixed confidence for an author-lineage hit
DEFAULT_CONF = 0.5          # presumed-Gelug fallback (low — Phase 2 LLM revisits)
AGREEMENT_BONUS = 0.05      # both rules agree on a tradition → nudge up (cap 0.95)

# ── curated maps ─────────────────────────────────────────────────────────────
# Subject name (exact) → [(tradition, confidence)]. Only tradition-DIAGNOSTIC
# subjects appear; topic subjects (Emptiness, Biography…) say nothing about school.
# 'Buddhism/Other traditions' is deliberately absent — ambiguous, deferred to Phase 2.
SUBJECT_TRADITION: dict[str, list[tuple[str, float]]] = {
    "Buddhism/Sakya":          [("Sakya", 0.90)],
    "Buddhism/Dzogchen":       [("Nyingma", 0.90)],   # Dzogchen ⇒ Nyingma
    "Buddhism/Shangpa Kagyu":  [("Shangpa Kagyu", 0.90)],
    "Buddhism/Mahamudra":      [("Kagyu", 0.70)],      # bare Mahāmudrā leans Kagyu (weakly)
    "Buddhism/Kagyu Mahamudra": [("Gelug", 0.85), ("Kagyu", 0.85)],  # the Gelug–Kagyu synthesis
    "Buddhism/Yamantaka":      [("Gelug", 0.80)],      # Vajrabhairava is Gelug-emphasised
}

# person.external_id (exact, incl. 'wikidata:'/'bdr:' prefix) → tradition.
# High-precision head of the collection's authors; unmapped authors fire nothing.
# Indian mahāsiddhas / Madhyamaka masters who predate the Tibetan schools are 'Common' —
# INCLUDING the Kagyu forefathers (Tilopa, Nāropa, Milarepa, Gampopa), which this library
# treats as shared source figures, not school-specific (curation decision 2026-07). The
# one Indian-source⇒school exception kept is Virūpa ⇒ Sakya (Lamdré is definitionally Sakya).
AUTHOR_TRADITION: dict[str, str] = {
    # Gelug (Tsongkhapa's tradition & successors)
    "wikidata:Q323439":  "Gelug",   # Tsongkhapa
    "wikidata:Q1664172": "Gelug",   # Pabongkha Dechen Nyingpo
    "wikidata:Q930398":  "Gelug",   # Panchen Lozang Chökyi Gyaltsen
    "wikidata:Q17293":   "Gelug",   # Dalai Lama XIV
    "wikidata:Q1353874": "Gelug",   # Dalai Lama VII
    "wikidata:Q25252":   "Gelug",   # Dalai Lama V
    "bdr:P84":           "Gelug",   # Dalai Lama II
    "bdr:P999":          "Gelug",   # Dalai Lama III
    "bdr:P197":          "Gelug",   # Dalai Lama XIII
    "wikidata:Q7235105": "Gelug",   # Ngulchu Dharmabhadra
    "wikidata:Q2182680": "Gelug",   # Losang Lungtog Tenzin Trinley
    "wikidata:Q1481493": "Gelug",   # Jam-yang-shay-ba
    "bdr:P105":          "Gelug",   # Yongzin Yeshé Gyaltsen
    "wikidata:Q1554667": "Gelug",   # Gungthangpa
    "bdr:P1846":         "Gelug",   # Gomchen Ngawang Drakpa (Tsongkhapa's disciple)
    # Kadam (Atisha's lineage; lojong / mind-training — feeds Gelug)
    "wikidata:Q320150":  "Kadam",   # Atisha
    "wikidata:Q1260120": "Kadam",   # Dromtönpa
    "wikidata:Q1564675": "Kadam",   # Geshe Langri Thangpa (Eight Verses)
    "wikidata:Q1069026": "Kadam",   # Geshe Chekawa Yeshé Dorjé (Seven-Point Mind Training)
    "wikidata:Q17002117": "Kadam",  # Dharmarakṣita (Wheel of Sharp Weapons)
    "wikidata:Q25673888": "Kadam",  # Dölpa Marshurpa (Blue Compendium; NOT Dölpopa of Jonang)
    "wikidata:Q106790242": "Kadam", # Künchok Gyaltsen (co-compiler, Mind Training: Great Collection)
    "wikidata:Q106796609": "Kadam", # Chegom (Book of Kadam)
    "wikidata:Q106790330": "Kadam", # Khenchen Nyima Gyaltsen (Book of Kadam)
    # Kagyu (Marpa lineage & Karmapas). NOTE: the forefathers Tilopa/Nāropa/Milarepa/
    # Gampopa are classed 'Common' (below), not here — see the map header.
    "wikidata:Q965802":  "Rimé",    # Jamgön Kongtrul Lodrö Taye (the paradigmatic Rimé master)
    "wikidata:Q548603":  "Kagyu",   # 3rd Karmapa Rangjung Dorjé
    "wikidata:Q934469":  "Kagyu",   # 9th Karmapa Wangchuk Dorje
    "wikidata:Q5208651": "Kagyu",   # Takpo Tashi Namgyal (Moonbeams of Mahāmudrā)
    # Sakya
    "wikidata:Q982008":  "Sakya",   # Sakya Paṇḍita
    "wikidata:Q662562":  "Sakya",   # Jetsün Drakpa Gyaltsen
    "wikidata:Q2143829": "Sakya",   # Rendawa Zhönu Lodrö
    "wikidata:Q3489220": "Sakya",   # Virūpa (Lamdré source)
    # Gelug (cont.)
    "wikidata:Q106791432": "Gelug", # Gyalrong Tsultrim Nyima (Selected Teachings of the Geluk School)
    # Nyingma
    "wikidata:Q1708503": "Nyingma", # Longchenpa
    "wikidata:Q1271624": "Nyingma", # Dudjom Lingpa
    "wikidata:Q16924400": "Nyingma", # Sera Khandro
    "wikidata:Q106791974": "Nyingma", # Khenchen Kunzang Palden
    "wikidata:Q106791611": "Nyingma", # Minyak Kunzang Sönam
    "wikidata:Q106804124": "Nyingma", # Pema Tashi (Dzogchen cycle w/ Dudjom Lingpa)
    # Common (Indian sources — pan-Buddhist, predate the Tibetan schools). Includes the
    # Kagyu forefathers (curation 2026-07): shared source figures, not school-specific.
    "wikidata:Q453191":  "Common",  # Gampopa
    "wikidata:Q448793":  "Common",  # Tilopa
    "wikidata:Q203060":  "Common",  # Naropa
    "wikidata:Q58431":   "Common",  # Milarepa
    "bdr:P4954":         "Common",  # Nāgārjuna
    "wikidata:Q558275":  "Common",  # Āryadeva
    "wikidata:Q456963":  "Common",  # Candrakīrti
    "wikidata:Q445460":  "Common",  # Śāntideva
    "wikidata:Q316343":  "Common",  # Vasubandhu
    "wikidata:Q379905":  "Common",  # Asaṅga
    "wikidata:Q1276324": "Common",  # Maitreya (five treatises)
    "wikidata:Q106787791": "Common", # Saraha
    "bdr:P44":           "Common",  # Maitrīpa
    "bdr:P3299":         "Common",  # Kāṇha
    "bdr:P7609":         "Common",  # Mitrayogin
    "wikidata:Q106787940": "Common", # Serlingpa (Dharmakīrtiśrī)
    "wikidata:Q11819163": "Common", # Ḍombipa
    "wikidata:Q6503571": "Common",  # Kambala
}


# ── signal gathering ─────────────────────────────────────────────────────────
def _work_subjects(db) -> dict[int, set[str]]:
    """work_id → set of subject names, pooled from the work's own subjects AND the
    subjects of its editions (richer edition-level signal lifted to the work)."""
    rows = db.execute(
        "SELECT work_id, name FROM ("
        "  SELECT ws.work_id AS work_id, s.name AS name FROM work_subject ws "
        "    JOIN subject s ON s.id = ws.subject_id WHERE s.deleted_at IS NULL "
        "  UNION "
        "  SELECT ew.work_id AS work_id, s.name AS name FROM edition_subject es "
        "    JOIN edition_work ew ON ew.edition_id = es.edition_id "
        "    JOIN subject s ON s.id = es.subject_id WHERE s.deleted_at IS NULL)"
    ).fetchall()
    out: dict[int, set[str]] = {}
    for wid, name in rows:
        out.setdefault(wid, set()).add(name)
    return out


def _work_authors(db) -> dict[int, set[str]]:
    """work_id → set of author external_ids, from work_author AND the authors of the
    work's editions. Only persons carrying an external_id (the map's key) are returned."""
    rows = db.execute(
        "SELECT work_id, external_id FROM ("
        "  SELECT wa.work_id AS work_id, p.external_id AS external_id FROM work_author wa "
        "    JOIN person p ON p.id = wa.person_id "
        "    WHERE p.external_id IS NOT NULL AND p.deleted_at IS NULL "
        "  UNION "
        "  SELECT ew.work_id AS work_id, p.external_id AS external_id FROM edition_author ea "
        "    JOIN edition_work ew ON ew.edition_id = ea.edition_id "
        "    JOIN person p ON p.id = ea.person_id "
        "    WHERE p.external_id IS NOT NULL AND p.deleted_at IS NULL)"
    ).fetchall()
    out: dict[int, set[str]] = {}
    for wid, xid in rows:
        out.setdefault(wid, set()).add(xid)
    return out


def _author_names(db) -> dict[str, str]:
    """external_id → primary_name, for evidence strings."""
    return {r[0]: r[1] for r in db.execute(
        "SELECT external_id, primary_name FROM person "
        "WHERE external_id IS NOT NULL AND deleted_at IS NULL").fetchall()}


# ── rule engine ──────────────────────────────────────────────────────────────
def compute(db, *, default_gelug: bool = True) -> dict[int, dict[str, dict]]:
    """Pure computation (no writes). Returns work_id → {tradition: {conf, source,
    evidence}}. Applies subject + author rules, boosts agreed traditions, then the
    optional presumed-Gelug fallback for Buddhist works no rule touched."""
    subs = _work_subjects(db)
    auths = _work_authors(db)
    names = _author_names(db)

    # Works already carrying a manual (llm/human) verdict — the default rule leaves
    # them alone (they're classified; INSERT OR IGNORE protects the specific pairs).
    manual_works = {r[0] for r in db.execute(
        "SELECT DISTINCT work_id FROM work_tradition "
        "WHERE source IS NULL OR source NOT LIKE 'rule-%'").fetchall()}

    result: dict[int, dict[str, dict]] = {}

    def add(wid: int, trad: str, conf: float, source: str, ev: str) -> None:
        labels = result.setdefault(wid, {})
        e = labels.setdefault(trad, {"conf": -1.0, "source": None,
                                     "srcset": set(), "ev": []})
        if conf > e["conf"]:
            e["conf"], e["source"] = conf, source
        e["srcset"].add(source)
        e["ev"].append(ev)

    all_work_ids = set(subs) | set(auths)
    for wid in all_work_ids:
        for name in subs.get(wid, ()):
            for trad, conf in SUBJECT_TRADITION.get(name, ()):
                add(wid, trad, conf, "rule-subject", f"subject:{name}")
        for xid in auths.get(wid, ()):
            trad = AUTHOR_TRADITION.get(xid)
            if trad:
                who = names.get(xid, "?")
                add(wid, trad, AUTHOR_CONF, "rule-author", f"author:{xid}({who})")

    # Agreement boost: a tradition backed by >1 distinct rule source is more certain.
    for labels in result.values():
        for e in labels.values():
            if len(e["srcset"]) > 1:
                e["conf"] = min(0.95, e["conf"] + AGREEMENT_BONUS)

    # Presumed-default fallback (the user's "if not marked otherwise, it's <default>"
    # prior, made EXPLICIT and low-confidence). The default tradition is CONFIG-driven:
    # the first `_tradition` entry in vocab.json, i.e. the lowest-id live tradition
    # (Gelug for this library). Only Buddhist works that (a) got no rule label and
    # (b) carry no manual verdict.
    if default_gelug:
        row = db.execute("SELECT name FROM tradition WHERE deleted_at IS NULL "
                         "ORDER BY id LIMIT 1").fetchone()
        default_name = row[0] if row else None
        if default_name:
            for wid, names_set in subs.items():
                if wid in manual_works or result.get(wid):
                    continue
                if any(n == "Buddhism" or n.startswith("Buddhism/") for n in names_set):
                    add(wid, default_name, DEFAULT_CONF, "rule-default",
                        "default:no-school-signal")

    # Collapse the internal bookkeeping to the stored shape.
    return {wid: {trad: {"conf": round(e["conf"], 3), "source": e["source"],
                         "evidence": "; ".join(e["ev"])}
                  for trad, e in labels.items()}
            for wid, labels in result.items()}


def _tradition_ids(db) -> dict[str, int]:
    return {r[1]: r[0] for r in db.execute(
        "SELECT id, name FROM tradition WHERE deleted_at IS NULL").fetchall()}


def _verify(db, proposed: dict[int, dict[str, dict]]) -> None:
    """Fail fast on a map typo: every tradition the rules emit must be a seeded row."""
    known = set(_tradition_ids(db))
    used = {t for labels in proposed.values() for t in labels}
    unknown = used - known
    if unknown:
        raise MigrationError(f"rules reference unseeded tradition(s): {sorted(unknown)}")
    # Sanity: the map static names must exist too (catches typos even if unused).
    static = {t for v in SUBJECT_TRADITION.values() for t, _ in v} | set(AUTHOR_TRADITION.values())
    missing = static - known
    if missing:
        raise MigrationError(f"curated map names not in tradition table: {sorted(missing)}")


def _clear_rule_rows(db) -> int:
    before = db.execute("SELECT COUNT(*) FROM work_tradition").fetchone()[0]
    db.execute("DELETE FROM work_tradition WHERE source LIKE 'rule-%'")
    return before - db.execute("SELECT COUNT(*) FROM work_tradition").fetchone()[0]


def seed_person_tradition(db) -> int:
    """Fill `person.tradition` (the author's lineage) from the curated author→lineage
    map, keyed by external_id — but ONLY where it is currently NULL, so a human edit in
    the person UI is never overwritten. Returns rows set. This is the source of the
    DEFAULT tradition for a person's works/editions."""
    if "tradition" not in {r[1] for r in db.execute("PRAGMA table_info(person)")}:
        return 0                      # pre-v8 DB; nothing to seed
    n = 0
    for xid, trad in AUTHOR_TRADITION.items():
        n += db.execute(
            "UPDATE person SET tradition=? WHERE external_id=? AND tradition IS NULL "
            "AND deleted_at IS NULL", (trad, xid)).rowcount
    return n


def migrate(db, *, default_gelug: bool = True, commit: bool = True) -> dict:
    """Recompute the rule rows: clear old 'rule-%' rows, insert fresh ones (preserving
    any llm/human rows), and seed author lineages. Aborts (no commit) if a gate fails."""
    proposed = compute(db, default_gelug=default_gelug)
    _verify(db, proposed)
    tids = _tradition_ids(db)
    persons_seeded = seed_person_tradition(db)
    cleared = _clear_rule_rows(db)
    inserted = 0
    for wid, labels in proposed.items():
        for trad, e in labels.items():
            cur = db.execute(
                "INSERT OR IGNORE INTO work_tradition "
                "(work_id, tradition_id, confidence, source, evidence) VALUES (?,?,?,?,?)",
                (wid, tids[trad], e["conf"], e["source"], e["evidence"]))
            inserted += cur.rowcount
    if commit:
        db.commit()
    by_source = {r[0]: r[1] for r in db.execute(
        "SELECT source, COUNT(*) FROM work_tradition GROUP BY source").fetchall()}
    return {"rule_rows_cleared": cleared, "rows_inserted": inserted,
            "works_tagged": len(proposed), "persons_seeded": persons_seeded,
            "by_source": by_source}


def report(db, *, default_gelug: bool = True) -> dict:
    """Dry preview (no writes): what migrate would produce, broken down."""
    proposed = compute(db, default_gelug=default_gelug)
    by_source: dict[str, int] = {}
    by_tradition: dict[str, int] = {}
    for labels in proposed.values():
        for trad, e in labels.items():
            by_source[e["source"]] = by_source.get(e["source"], 0) + 1
            by_tradition[trad] = by_tradition.get(trad, 0) + 1
    total_works = db.execute(
        "SELECT COUNT(*) FROM work WHERE deleted_at IS NULL").fetchone()[0]
    return {"works_tagged": len(proposed),
            "works_total": total_works,
            "rows_proposed": sum(len(v) for v in proposed.values()),
            "by_source": dict(sorted(by_source.items())),
            "by_tradition": dict(sorted(by_tradition.items(), key=lambda x: -x[1])),
            "already_in_work_tradition": db.execute(
                "SELECT COUNT(*) FROM work_tradition").fetchone()[0]}


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("report", "migrate"):
        p = sub.add_parser(name)
        p.add_argument("db", nargs="?", default=default_db_path())
        p.add_argument("--no-default-gelug", action="store_true",
                       help="skip the presumed-Gelug fallback for unclassified works")
    args = ap.parse_args(argv)

    db = init_db(args.db)
    default_gelug = not args.no_default_gelug
    if args.cmd == "report":
        for k, v in report(db, default_gelug=default_gelug).items():
            print(f"  {k}: {v}")
    else:
        try:
            res = migrate(db, default_gelug=default_gelug)
        except MigrationError as e:
            print(f"ABORTED (verify gate failed): {e}")
            return 1
        print("tradition rules applied:")
        for k, v in res.items():
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
