"""`Session` — a unit of work over ONE transaction (multi-aggregate atomicity).

Real operations span aggregates: an import touches edition + holdings + works; a cascade-merge
touches several roots. Those must be all-or-nothing. A `Session` stages several planned `Impact`s
into the gateway's single RW transaction, then **one** `commit()` makes them atomic (or `rollback()`
undoes them all); the filesystem effects (trashed files) every staged op deferred run once, after
that commit. A bare `writer.apply()` is just a one-op session. See entity_api_model.md §5.

```python
with acc.session() as s:                       # one transaction, one commit
    s.stage(acc.editions.writes, acc.editions.writes.plan_delete(ed_ref))
    s.stage(acc.holdings.writes, acc.holdings.writes.plan_set_text_status(h_ref, "ocr_good"))
    combined = s.impact()                       # the merged Impact across all staged ops
# commit on clean exit; rollback (files untouched) on exception
```
"""
from __future__ import annotations

from catalogue.contracts import Impact, Ref


class Session:
    """Stages writes into one RW transaction; commits/rolls back all together. Each writer exposes
    `_stage(conn, impact)` which executes its DB mutations WITHOUT committing and returns the
    file_ops to run post-commit — the session collects those and trashes them after its commit."""

    def __init__(self, access):
        self._a = access
        self._impacts: list[Impact] = []
        self._file_ops: list = []
        self._closed = False

    def stage(self, writer, impact: Impact) -> Impact:
        """Execute one planned Impact into the session's shared transaction (no commit yet). A
        non-appliable or stale Impact raises here, aborting the session (→ rollback on exit)."""
        if self._closed:
            raise RuntimeError("session already committed/rolled back")
        self._file_ops.extend(writer._stage(impact))
        self._impacts.append(impact)
        return impact

    def impact(self) -> Impact:
        """The combined Impact across everything staged (op='session'), for preview/serialization."""
        target = self._impacts[0].target if self._impacts else Ref("session", 0)
        cat = lambda attr: tuple(x for i in self._impacts for x in getattr(i, attr))
        return Impact("session", target, cascades=cat("cascades"), orphans=cat("orphans"),
                      ref_purges=cat("ref_purges"), file_ops=cat("file_ops"),
                      link_repoints=cat("link_repoints"), blocks=cat("blocks"))

    def commit(self) -> None:
        if self._closed:
            return
        try:
            self._a.commit()
        except Exception:
            self._a.rollback()
            self._closed = True
            raise
        self._closed = True
        self._a.backing.run(self._file_ops, self._a.trash_dir)   # after the single commit is durable

    def rollback(self) -> None:
        if self._closed:
            return
        self._a.rollback()
        self._closed = True

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, exc_type, *_) -> bool:
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False
