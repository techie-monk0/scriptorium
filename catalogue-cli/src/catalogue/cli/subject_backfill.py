"""Backfill hierarchical subject keywords from holding file paths.

The directory path between the library root and a holding's file becomes the
edition's subject, each folder cleaned or remapped:

    <root>/01 Books - Dharma/Emptiness/A.pdf   →   edition subject "Dharma/Emptiness"

Dry-run by default; pass --apply to commit. Idempotent and non-destructive —
already-attached subjects are skipped, nothing is removed.

  python3 -m catalogue.cli.subject_backfill DB                       # preview
  python3 -m catalogue.cli.subject_backfill DB --apply               # commit
  python3 -m catalogue.cli.subject_backfill DB --also-works          # also tag each work
  python3 -m catalogue.cli.subject_backfill DB --root /lib/books     # override root detection
  python3 -m catalogue.cli.subject_backfill DB --map "01 Books - Dharma=Dharma"   # set a folder→label map
  python3 -m catalogue.cli.subject_backfill DB --map "Misc="          # drop a folder from the path
  python3 -m catalogue.cli.subject_backfill DB --list-maps           # show current maps
  python3 -m catalogue.cli.subject_backfill DB --read-subject-from-dir-structure --apply
                                                                     # seed the subject table from the folder tree
"""
import argparse

from catalogue.db_store import init_db
from catalogue.services import subjects as S
from catalogue.db_store import default_db_path


def plan_backfill(db, *, root=None, mapping=None, also_works=False, limit=None):
    """List `(kind, parent_id, subject)` attachments derived from holding paths,
    de-duplicated within the run. Read-only."""
    if root is None:
        root = S.subject_root(db)
    if mapping is None:
        mapping = S.folder_map(db)
    from catalogue.access_api import system_conn
    rows = [(eid, fp) for _hid, eid, fp, _fh, _ch in system_conn(db).holdings.reads.with_files()]
    seen: set = set()
    plan: list = []
    n = 0
    for eid, path in rows:
        subj = S.derive_subject(path, root, mapping)
        if not subj:
            continue
        for kind, pid in _targets(db, eid, also_works):
            key = (kind, pid, subj.casefold())
            if key not in seen:
                seen.add(key)
                plan.append((kind, pid, subj))
        n += 1
        if limit and n >= limit:
            break
    return plan


def _targets(db, eid, also_works):
    from catalogue.access_api import system_conn
    yield ("edition", eid)
    if also_works:
        for wid in system_conn(db).works.reads.ids_in_edition(eid):
            yield ("work", wid)


def _already_attached(db, kind, parent_id, subject) -> bool:
    from catalogue.access_api import system_conn
    return system_conn(db).subjects.graph.has_named_tag(kind, parent_id, subject)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", nargs="?", default=default_db_path())
    ap.add_argument("--apply", action="store_true",
                    help="commit (default: dry-run preview, nothing written)")
    ap.add_argument("--also-works", action="store_true",
                    help="also tag every work in the edition, not just the edition")
    ap.add_argument("--root", default=None,
                    help="library-root prefix to strip (default: auto-detect common prefix)")
    ap.add_argument("--map", action="append", default=[], metavar="RAW=LABEL",
                    help="set a folder→label override (repeatable); persisted then used")
    ap.add_argument("--list-maps", action="store_true", help="print folder→label maps and exit")
    ap.add_argument("--limit", type=int, default=None, help="cap holdings processed (testing)")
    ap.add_argument("--read-subject-from-dir-structure", action="store_true",
                    help="walk the on-disk library tree (--root or auto) and seed the subject "
                         "table with every folder's label — predefined subjects for autocomplete. "
                         "Honours config subtree exclusions (ANNOTATED, etc.).")
    args = ap.parse_args(argv)
    db = init_db(args.db)

    # Folder maps are persisted (so they apply to the UI and future runs too).
    if args.map:
        for spec in args.map:
            raw, sep, label = spec.partition("=")
            if not sep:
                ap.error(f"--map needs RAW=LABEL form, got {spec!r}")
            S.set_folder_label(db, raw, label)
        db.commit()
        print(f"set {len(args.map)} folder map(s)")

    if args.list_maps:
        for raw, label in sorted(S.folder_map(db).items()):
            print(f"  {raw!r:40} → {label!r}")
        return 0

    root = args.root if args.root is not None else S.subject_root(db)

    if args.read_subject_from_dir_structure:
        vocab = S.populate_subject_vocab(db, root)               # seed the subject menu
        att = S.attach_dir_subjects(db, root)                    # tag works (editions inherit) / modern editions
        print(f"root = {root!r}  [{'APPLY' if args.apply else 'dry-run'}]")
        print(f"vocab: scanned {vocab['scanned']} folder(s) · {len(vocab['added'])} new subject(s)")
        print(f"attach: {att['work']} work(s) tagged (editions inherit) · "
              f"{att['edition']} work-less edition(s) tagged directly")
        for s in vocab["added"]:
            print(f"  + subject {s!r}")
        db.commit() if args.apply else db.rollback()
        return 0

    plan = plan_backfill(db, root=root, also_works=args.also_works, limit=args.limit)
    new = [(k, pid, s) for (k, pid, s) in plan if not _already_attached(db, k, pid, s)]

    print(f"root = {root!r}")
    print(f"{len(plan)} attachment(s) derived · {len(new)} new "
          f"[{'APPLY' if args.apply else 'dry-run'}]")
    for kind, pid, subj in new:
        print(f"  + {kind} {pid}  ←  {subj!r}")

    if args.apply and new:
        for kind, pid, subj in new:
            S.add_subject(db, kind, pid, subj)
        db.commit()
        print(f"committed {len(new)} new attachment(s)")
    elif not args.apply:
        db.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
