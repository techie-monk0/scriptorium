"""Read-only listing of books (editions) under a subject, optionally narrowed to
an author/translator, with the on-disk file location of every holding. Writes
NOTHING to the DB.

The subject may be FULLY qualified (a leaf, e.g. `Buddhism/Tantra/Kalachakra`)
or PARTIALLY qualified (an interior node, e.g. `Buddhism/Tantra`). Either way the
match is PREFIX-INCLUSIVE — a partial subject rolls up the node itself AND every
subject nested beneath it (the same `name = X OR name LIKE X || '/%'` rule the
home shelves / subject page use, via `subject_tree`). Subject→edition uses the
canonical JOIN: an edition is covered if it is tagged directly (`edition_subject`)
OR through a contained work (`edition_work` → `work_subject`).

`--author` matches a person by `primary_name` or any alias (case-insensitive
substring) and keeps only editions where that person is a book author
(`edition_author`), a translator (`edition_translator`), or the author of a
contained work (`work_author` of an `edition_work`). Classical texts carry their
author on the WORK, so the work path matters.

`--since-date YYYY-MM-DD` keeps only editions newly added on/after that date, and
`--since-edition-num N` keeps only editions added after edition #N (i.e. id > N).
"Added" means the EARLIEST `holding.date_added` across an edition's copies. All
filters intersect; `--subject` is optional, but at least one filter is required.

    python3 -m catalogue.cli.books_by_subject catalogue-db/catalogue.db --subject "Buddhism/Tantra"
    python3 -m catalogue.cli.books_by_subject catalogue-db/catalogue.db --subject "Buddhism" --author "Tsongkhapa"
    python3 -m catalogue.cli.books_by_subject catalogue-db/catalogue.db --since-date 2026-06-15
    python3 -m catalogue.cli.books_by_subject catalogue-db/catalogue.db --subject "Buddhism/Tantra" --since-edition-num 400
    python3 -m catalogue.cli.books_by_subject catalogue-db/catalogue.db --subject "Buddhism/Madhyamaka" --json
"""
from __future__ import annotations

import argparse
import json
import sys

from catalogue.access_api import system_access


def _added_date(acc, eid: int) -> str | None:
    """When the edition entered the catalogue — the EARLIEST `holding.date_added`
    across its holdings (an edition is "added" once its first copy lands)."""
    return acc.holdings.reads.earliest_added(eid)


def _edition_record(acc, eid: int) -> dict:
    """Full book record for one edition: bibliographic fields, edition-level
    authors/translators, contained works (with their authors), subjects, and the
    file location of every holding. `acc` is the bound access-API."""
    from catalogue.services import library as L

    e = acc.editions.reads.full_record(eid)
    if e is None:
        return {}
    title, subtitle, volume, publisher, year, isbn, language, structure = e

    persons = L.edition_persons(acc.ro, eid)        # edition-level authors/translators
    works = L.edition_work_summaries(acc.ro, eid)   # contained works (carry their authors)

    subjects = [name for name, _kind in acc.editions.reads.subject_names_kinds(eid)]

    holdings = [
        {"id": h[0], "form": h[1], "file_path": h[2], "archival_pdf_path": h[3],
         "shelf_location": h[4], "holding_type": h[5], "text_status": h[6]}
        for h in acc.holdings.reads.full_rows(eid)]

    return {
        "edition_id": eid,
        "title": title, "subtitle": subtitle, "volume": volume,
        "publisher": publisher, "year": year, "isbn": isbn, "language": language,
        "structure": structure,
        "added": _added_date(acc, eid),
        "authors": [a["name"] for a in persons["authors"]],
        "translators": [t["name"] for t in persons["translators"]],
        "works": [{"title": w.get("title"),
                   "authors": [a.get("name") for a in w.get("authors", [])]}
                  for w in works],
        "subjects": subjects,
        "holdings": holdings,
    }


def _print_text(records: list[dict], *, filters: list[str]) -> None:
    print("Filters: " + ("  +  ".join(filters) if filters else "(none)"))
    print(f"{len(records)} book(s)\n")
    for r in records:
        t = r["title"]
        if r.get("volume"):
            t = f"{t}  [vol. {r['volume']}]"
        added = f"  (added {r['added']})" if r.get("added") else ""
        print(f"#{r['edition_id']}  {t}{added}")
        if r.get("subtitle"):
            print(f"      {r['subtitle']}")
        meta = " · ".join(str(x) for x in (r.get("publisher"), r.get("year"),
                                           r.get("isbn")) if x)
        if meta:
            print(f"      {meta}")
        if r["authors"]:
            print(f"      authors:     {', '.join(r['authors'])}")
        if r["translators"]:
            print(f"      translators: {', '.join(r['translators'])}")
        for w in r["works"]:
            wa = f" — {', '.join(a for a in w['authors'] if a)}" if w["authors"] else ""
            print(f"        · {w['title']}{wa}")
        if r["subjects"]:
            print(f"      subjects:    {'; '.join(r['subjects'])}")
        if r["holdings"]:
            print("      holdings:")
            for h in r["holdings"]:
                loc = h["file_path"] or h["shelf_location"] or "(no file_path)"
                print(f"        - [{h['form'] or '?'}] {loc}")
                if h["archival_pdf_path"]:
                    print(f"            archival: {h['archival_pdf_path']}")
        else:
            print("      holdings:    (none)")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="path to catalogue.db")
    ap.add_argument("--subject", default=None,
                    help="subject name, full (leaf) or partial (interior); prefix-inclusive")
    ap.add_argument("--author", default=None,
                    help="narrow to books with this author/translator (name substring)")
    ap.add_argument("--since-date", default=None, metavar="YYYY-MM-DD",
                    help="only editions newly added on/after this date")
    ap.add_argument("--since-edition-num", type=int, default=None, metavar="N",
                    help="only editions added after edition #N (id > N)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    if not (args.subject or args.author or args.since_date
            or args.since_edition_num is not None):
        ap.error("at least one filter is required "
                 "(--subject / --author / --since-date / --since-edition-num)")

    filters: list[str] = []
    if args.subject:
        filters.append(f"subject «{args.subject}» (prefix-inclusive)")
    if args.author:
        filters.append(f"author «{args.author}»")
    if args.since_date:
        filters.append(f"added since {args.since_date}")
    if args.since_edition_num is not None:
        filters.append(f"edition # > {args.since_edition_num}")

    with system_access(args.db) as acc:
        # The whole filter intersection (subject prefix-inclusive / author / since-date /
        # since-edition) now lives behind the access-API; this CLI only orchestrates + renders.
        eids = acc.editions.reads.find_ids(
            subject=args.subject, author=args.author,
            since_date=args.since_date, since_edition=args.since_edition_num)
        if eids is None:    # a subject/author filter matched nothing — say which (cheap, error path)
            if args.subject and acc.editions.reads.find_ids(subject=args.subject) is None:
                print(f"No subject matches «{args.subject}».", file=sys.stderr)
            elif args.author and acc.editions.reads.find_ids(author=args.author) is None:
                print(f"No person matches author «{args.author}».", file=sys.stderr)
            return 1

        records = [_edition_record(acc, e) for e in eids]
        records = [r for r in records if r]

    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        _print_text(records, filters=filters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
