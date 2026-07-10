"""Match & dedup (§7.5).

Two passes:
  1. **ISBN exact match** → auto-merge (a 13-digit ISBN is a strong identifier).
  2. **Title fold-key match** → enqueue `review_queue.edition_dedup`
     (titles are too ambiguous to auto-merge — `Introduction`,
     `Chapter One`, etc.).

`merge_editions(canonical, duplicate)` moves all `holding` and
`edition_work` rows from the duplicate onto the canonical edition and
deletes the now-empty duplicate. Idempotent: a second run does nothing.
"""
from __future__ import annotations

from dataclasses import dataclass

from catalogue.db_store import fold_key


def _acc(conn):
    """A system Access over this connection — engine-routed edition reads, the edition-merge op,
    and the review queue (`acc.review`). The caller owns the commit."""
    from catalogue.access_api import system_conn
    return system_conn(conn)


@dataclass
class MatchReport:
    isbn_merges: int = 0
    title_candidates_queued: int = 0


# ── ISBN pass ────────────────────────────────────────────────────────────
def find_isbn_duplicates(conn) -> list[tuple[int, int, str]]:
    """Return `(canonical_id, duplicate_id, isbn)` tuples for every pair
    of editions sharing an ISBN. The lowest id is canonical (stable
    across runs)."""
    out: list[tuple[int, int, str]] = []
    for isbn, ids in _acc(conn).editions.reads.isbn_duplicate_groups():
        canonical = ids[0]
        for dup in ids[1:]:
            out.append((canonical, dup, isbn))
    return out


# ── Title pass (exact fold-key bucket) ───────────────────────────────────
def find_title_candidates(conn) -> list[tuple[int, int, str]]:
    """Return `(a_id, b_id, fold_key)` for every pair of editions whose
    title folds to the same key. Step 5 v1: exact key matching; fuzzy
    similarity above a threshold is a Step-9-class extension."""
    buckets: dict[str, list[int]] = {}
    for eid, title, _publisher in _acc(conn).editions.reads.titled():
        key = fold_key(title)
        if not key:
            continue
        buckets.setdefault(key, []).append(eid)
    out: list[tuple[int, int, str]] = []
    for key, ids in buckets.items():
        if len(ids) < 2:
            continue
        ids.sort()
        a = ids[0]
        for b in ids[1:]:
            out.append((a, b, key))
    return out


# ── Merge ────────────────────────────────────────────────────────────────
def merge_editions(conn, canonical_id: int, duplicate_id: int) -> None:
    """Move holdings + edition_work links from `duplicate` to `canonical`
    and delete the duplicate row, via the edition-merge engine op
    (`acc.editions.writes.merge`). Idempotent: if `duplicate_id` doesn't
    exist (e.g. already merged), this is a no-op."""
    acc = _acc(conn)
    if canonical_id == duplicate_id or acc.editions.reads.get(duplicate_id) is None:
        return
    acc.editions.writes.merge(duplicate_id, canonical_id)   # fold loser → winner


# ── Orchestrator ─────────────────────────────────────────────────────────
def run_match(conn) -> MatchReport:
    """ISBN dupes are auto-merged; title dupes are queued for review."""
    rep = MatchReport()
    review = _acc(conn).review

    for canonical, dup, isbn in find_isbn_duplicates(conn):
        merge_editions(conn, canonical, dup)
        rep.isbn_merges += 1

    # After ISBN merges, recompute title candidates (some merges may have
    # collapsed buckets) and queue what's left.
    for a, b, key in find_title_candidates(conn):
        # Avoid double-queueing on re-runs.
        if review.reads.exists_pending(
                "edition_dedup", f'%"canonical": {a}%', f'%"duplicate": {b}%'):
            continue
        review.writes.enqueue("edition_dedup", {
            "canonical": a, "duplicate": b,
            "match_key": key, "reason": "title_fold_key",
        })
        rep.title_candidates_queued += 1

    conn.commit()
    return rep
