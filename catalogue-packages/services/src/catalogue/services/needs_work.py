"""Three-tier needs-work list (§1.1, §7.5).

Tier A — **digitize**: edition has physical holdings only (no electronic,
         or all electronic are image-only/none).
Tier B — **re-OCR**: edition has electronic holding(s) but none with
         clean text (`native` or `ocr_good`); at least one is poor.
Tier C — **clean**: edition has at least one electronic holding with
         clean text.

Editions with zero holdings are surfaced separately as "orphan" so they
can't silently disappear — they usually come from a staging row that was
resolved to a new edition without a matching holding insert (a bug
signal, worth a glance).

An edition the operator has tagged ANNOTATED (title, or all holdings under an
ANNOTATED-named folder — see catalogue/skip.py) is surfaced as its own
"skipped" tier, ahead of the others: it is intentionally out of the catalogue,
so it must NOT be counted as work-to-do or mistaken for an orphan/missing copy.
"""
from __future__ import annotations

from dataclasses import dataclass

from .skip import SKIP_TOKEN, is_skipped


# n_hold + n_skip drive the "skipped" tier: an edition is skipped if every one
# of its holdings sits under an ANNOTATED-named path (or its title is ANNOTATED,
# checked in Python). LIKE is case-insensitive for ASCII, so the token matches
# any case. n_skip counts holdings whose path carries the token.


@dataclass(frozen=True)
class EditionTier:
    id: int
    title: str
    isbn: str | None
    n_phys: int
    n_clean: int
    n_dirty: int
    n_hold: int = 0
    n_skip: int = 0


@dataclass
class NeedsWorkReport:
    digitize: list[EditionTier]    # Tier A
    reocr:    list[EditionTier]    # Tier B
    clean:    list[EditionTier]    # Tier C
    orphan:   list[EditionTier]    # editions with no holdings
    skipped:  list[EditionTier]    # ANNOTATED — intentionally out of the catalogue

    @property
    def counts(self) -> dict[str, int]:
        return {
            "digitize": len(self.digitize),
            "reocr":    len(self.reocr),
            "clean":    len(self.clean),
            "orphan":   len(self.orphan),
            "skipped":  len(self.skipped),
        }


def tier_editions(conn) -> NeedsWorkReport:
    from catalogue.access_api import system_conn
    digitize, reocr, clean, orphan, skipped = [], [], [], [], []
    for r in system_conn(conn).editions.reads.needs_work_tiers(SKIP_TOKEN):
        et = EditionTier(*r)
        # Skipped wins first: an ANNOTATED title, or every holding under an
        # ANNOTATED path. Checked before orphan so a skipped book never reads
        # as a missing-data bug signal.
        if is_skipped(et.title) or (et.n_hold > 0 and et.n_skip == et.n_hold):
            skipped.append(et)
        elif et.n_clean == 0 and et.n_dirty == 0 and et.n_phys == 0:
            orphan.append(et)
        elif et.n_clean > 0:
            clean.append(et)
        elif et.n_dirty > 0:
            reocr.append(et)
        else:
            # Has only physical holding(s) → digitize.
            digitize.append(et)
    return NeedsWorkReport(
        digitize=digitize, reocr=reocr, clean=clean, orphan=orphan,
        skipped=skipped,
    )
