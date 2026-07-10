"""Tradition-classifier access surface — the read signals + the join-table write the
Phase-2 work classifier and the person-lineage roster need, so those services hold NO SQL.

A flat policy-gated repo (not a soft-delete aggregate): reads run over the RO connection;
the one write STAGES onto the RW connection (the caller owns the commit), matching the cache
repos. The `tradition` ENTITY itself (name CRUD + work links) lives in `traditions.py`
(`acc.traditions`); this repo is the classifier's bespoke query/write layer over the
`work_tradition` join + the person/edition/subject signals that feed the model. See [[db-access-modules]].
"""
from __future__ import annotations

from catalogue.contracts import AccessMode, Action

_RESOURCE = "tradition_classify"


class TraditionClassifyRepo:
    """`acc.tradition_classify` — signal reads + the rule→llm work_tradition write."""

    def __init__(self, access):
        self._a = access

    # ── reads (Phase-2 work classifier) ──────────────────────────────────────────
    def rule_only_candidates(self, threshold: float) -> list[int]:
        """work_ids whose tradition is still only a low-confidence RULE guess — every row is
        `source LIKE 'rule-%'` and the best confidence is < `threshold` (the ambiguous tail).
        Works carrying a human/llm verdict, or a confident rule, are settled and skipped."""
        self._a.authorize(Action(_RESOURCE, "candidates", AccessMode.READ))
        return [r[0] for r in self._a.ro.execute(
            "SELECT work_id FROM work_tradition GROUP BY work_id "
            "HAVING MAX(CASE WHEN source LIKE 'rule-%' THEN 1 ELSE 0 END) = 1 "  # only rule rows
            "   AND MIN(CASE WHEN source LIKE 'rule-%' THEN 1 ELSE 0 END) = 1 "
            "   AND MAX(confidence) < ?", (threshold,)).fetchall()]

    def work_signals(self, work_id: int) -> dict:
        """The signals fed to the model for one work: edition titles, the work's native-script
        titles, its (+ its editions') subjects, author rows as `(name, external_id)` (the caller
        maps external_id → a lineage hint), and publishers. Pure read."""
        self._a.authorize(Action(_RESOURCE, "work_signals", AccessMode.READ))
        ro = self._a.ro
        titles = [r[0] for r in ro.execute(
            "SELECT DISTINCT e.title FROM edition e JOIN edition_work ew ON ew.edition_id=e.id "
            "WHERE ew.work_id=? AND e.title IS NOT NULL AND e.deleted_at IS NULL",
            (work_id,)).fetchall()]
        natives = [t for t in ro.execute(
            "SELECT sanskrit_title, tibetan_title FROM work WHERE id=?", (work_id,)).fetchone() or ()
            if t]
        subjects = [r[0] for r in ro.execute(
            "SELECT DISTINCT name FROM ("
            "  SELECT s.name FROM work_subject ws JOIN subject s ON s.id=ws.subject_id "
            "    WHERE ws.work_id=? AND s.deleted_at IS NULL "
            "  UNION SELECT s.name FROM edition_subject es JOIN edition_work ew ON ew.edition_id=es.edition_id "
            "    JOIN subject s ON s.id=es.subject_id WHERE ew.work_id=? AND s.deleted_at IS NULL)",
            (work_id, work_id)).fetchall()]
        author_rows = [(name, xid) for name, xid in ro.execute(
            "SELECT DISTINCT p.primary_name, p.external_id FROM work_author wa "
            "JOIN person p ON p.id=wa.person_id WHERE wa.work_id=? AND p.deleted_at IS NULL",
            (work_id,)).fetchall()]
        publishers = [r[0] for r in ro.execute(
            "SELECT DISTINCT e.publisher FROM edition e JOIN edition_work ew ON ew.edition_id=e.id "
            "WHERE ew.work_id=? AND e.publisher IS NOT NULL AND e.deleted_at IS NULL",
            (work_id,)).fetchall()]
        return {"titles": titles, "natives": natives, "subjects": subjects,
                "author_rows": author_rows, "publishers": publishers}

    def tradition_name_ids(self) -> dict:
        """{name: id} for every live tradition — the classifier's write target lookup."""
        self._a.authorize(Action(_RESOURCE, "name_ids", AccessMode.READ))
        return {r[1]: r[0] for r in self._a.ro.execute(
            "SELECT id, name FROM tradition WHERE deleted_at IS NULL").fetchall()}

    def cached_count(self, classify_version: int) -> int:
        """How many classify results are already memoized for `classify_version` (the dry report)."""
        self._a.authorize(Action(_RESOURCE, "cached_count", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT COUNT(*) FROM classification_cache WHERE classify_version=?",
            (classify_version,)).fetchone()[0]

    # ── reads (person-lineage roster) ────────────────────────────────────────────
    def authoring_persons(self, min_works: int) -> list[tuple]:
        """`(id, primary_name, dates, external_id, tradition, n_works)` for every LIVE person who
        authored ≥ `min_works` works, most-prolific first — the roster the person classifier walks.
        Empty strings (not NULL) for the optional text columns."""
        self._a.authorize(Action(_RESOURCE, "authoring_persons", AccessMode.READ))
        return self._a.ro.execute(
            "SELECT p.id, p.primary_name, COALESCE(p.dates,''), COALESCE(p.external_id,''), "
            "       COALESCE(p.tradition,''), COUNT(DISTINCT wa.work_id) n "
            "FROM person p JOIN work_author wa ON wa.person_id=p.id "
            "WHERE p.deleted_at IS NULL GROUP BY p.id HAVING n >= ? "
            "ORDER BY n DESC, p.primary_name", (min_works,)).fetchall()

    def person_publishers(self, person_id: int, limit: int = 6) -> list[str]:
        """Up to `limit` distinct publishers across the editions of this person's works."""
        self._a.authorize(Action(_RESOURCE, "person_publishers", AccessMode.READ))
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT e.publisher FROM edition e "
            "JOIN edition_work ew ON ew.edition_id=e.id JOIN work_author wa ON wa.work_id=ew.work_id "
            "WHERE wa.person_id=? AND e.publisher IS NOT NULL AND e.deleted_at IS NULL LIMIT ?",
            (person_id, limit)).fetchall()]

    def person_subjects(self, person_id: int, limit: int = 8) -> list[str]:
        """Up to `limit` distinct subject names across this person's authored works."""
        self._a.authorize(Action(_RESOURCE, "person_subjects", AccessMode.READ))
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT s.name FROM subject s JOIN work_subject ws ON ws.subject_id=s.id "
            "JOIN work_author wa ON wa.work_id=ws.work_id "
            "WHERE wa.person_id=? AND s.deleted_at IS NULL LIMIT ?",
            (person_id, limit)).fetchall()]

    # ── write (Phase-2 verdict) ──────────────────────────────────────────────────
    def replace_rule_rows_with_llm(self, work_id: int, tradition_ids: list[int],
                                   confidence: float, evidence: str) -> int:
        """Replace the work's `rule-%` rows with the LLM verdict — one `source='llm'` row per
        tradition id. `INSERT OR IGNORE` preserves any `source='human'` row already present.
        Returns rows inserted. STAGES onto the RW connection; the caller owns the commit."""
        self._a.authorize(Action(_RESOURCE, "write_verdict", AccessMode.WRITE))
        rw = self._a.rw
        rw.execute("DELETE FROM work_tradition WHERE work_id=? AND source LIKE 'rule-%'", (work_id,))
        n = 0
        for tid in tradition_ids:
            n += rw.execute(
                "INSERT OR IGNORE INTO work_tradition "
                "(work_id, tradition_id, confidence, source, evidence) VALUES (?,?,?,?,?)",
                (work_id, tid, confidence, "llm", evidence)).rowcount
        return n
