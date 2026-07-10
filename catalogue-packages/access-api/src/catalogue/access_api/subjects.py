"""Subject entity — a topical/series heading. Leaf root: only a work edge, trivial cascade delete."""
from __future__ import annotations

from catalogue.contracts import BasicGate, FieldRule, Subject

from ._leaf import LeafRepo, LeafSpec

SUBJECT_SPEC = LeafSpec(
    resource="subject",
    table="subject",
    columns=("id", "name", "kind", "rev"),
    make_dto=lambda r: Subject(id=r[0], name=r[1], kind=r[2], rev=r[3]),
    work_link=("work_subject", "subject_id"),
    writable=("name", "kind"),
    gate=BasicGate({"subject": {"name": FieldRule(required=True, max_len=200),
                                "kind": FieldRule()}}),
)


class SubjectRepo(LeafRepo):
    def __init__(self, access):
        super().__init__(access, SUBJECT_SPEC)
        self._access = access
        self._graph = None

    @property
    def graph(self):
        """The subject-GRAPH store: the attach/detach + hierarchy + folder-map primitives the
        `services.subjects` domain layer composes (kept distinct from leaf CRUD)."""
        if self._graph is None:
            self._graph = SubjectGraph(self._access)
        return self._graph


_KINDS = ("work", "edition")


class SubjectGraph:
    """Port-adapter for the subject graph: every SQL primitive `services.subjects` needs, behind the
    engine's RO/RW connections. Holds NO domain logic (hierarchy materialization, rename cascade, folder
    derivation stay in the service) — only the data operations the service orchestrates."""

    def __init__(self, access):
        self._a = access

    # ── subject rows ──────────────────────────────────────────────────────────────
    def id_by_name(self, name):
        r = self._a.ro.execute(
            "SELECT id FROM subject WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return r[0] if r else None

    def name_of(self, sid):
        r = self._a.ro.execute("SELECT name FROM subject WHERE id = ?", (sid,)).fetchone()
        return r[0] if r else None

    def name_kind_of(self, sid):
        return self._a.ro.execute(
            "SELECT name, kind FROM subject WHERE id = ?", (sid,)).fetchone()

    def name_taken(self, name, exclude_sid):
        r = self._a.ro.execute(
            "SELECT id FROM subject WHERE name = ? COLLATE NOCASE AND id != ?",
            (name, exclude_sid)).fetchone()
        return r[0] if r else None

    def all_names(self):
        return [r[0] for r in self._a.ro.execute("SELECT name FROM subject").fetchall()]

    def names(self, kind=None):
        """Subject names (optionally one kind), name-ordered — the <datalist> autocomplete source."""
        if kind:
            return [r[0] for r in self._a.ro.execute(
                "SELECT name FROM subject WHERE kind = ? ORDER BY name", (kind,)).fetchall()]
        return [r[0] for r in self._a.ro.execute(
            "SELECT name FROM subject ORDER BY name").fetchall()]

    def descendants(self, prefix_like):
        """(id, name) for subjects whose name LIKE `prefix_like` (already escaped, e.g. 'A/%')."""
        return self._a.ro.execute(
            "SELECT id, name FROM subject WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE",
            (prefix_like,)).fetchall()

    def insert_subject(self, name, kind):
        return self._a.rw.execute(
            "INSERT INTO subject (name, kind) VALUES (?, ?)", (name, kind)).lastrowid

    def update_name(self, sid, name):
        self._a.rw.execute("UPDATE subject SET name = ? WHERE id = ?", (name, sid))

    def delete_subject(self, sid):
        self._a.rw.execute("DELETE FROM subject WHERE id = ?", (sid,))

    # ── work/edition attachments ──────────────────────────────────────────────────
    def attach(self, kind, parent_id, sid):
        assert kind in _KINDS, kind
        self._a.rw.execute(
            f"INSERT OR IGNORE INTO {kind}_subject ({kind}_id, subject_id) VALUES (?, ?)",
            (parent_id, sid))

    def detach(self, kind, parent_id, sid):
        assert kind in _KINDS, kind
        self._a.rw.execute(
            f"DELETE FROM {kind}_subject WHERE {kind}_id = ? AND subject_id = ?",
            (parent_id, sid))

    def is_attached(self, kind, parent_id, sid):
        assert kind in _KINDS, kind
        return self._a.ro.execute(
            f"SELECT 1 FROM {kind}_subject WHERE {kind}_id = ? AND subject_id = ?",
            (parent_id, sid)).fetchone() is not None

    def tags_for(self, kind, parent_id, subject_kind=None):
        assert kind in _KINDS, kind
        sql = (f"SELECT s.id, s.name FROM {kind}_subject js "
               "JOIN subject s ON s.id = js.subject_id "
               f"WHERE js.{kind}_id = ? ")
        args = [parent_id]
        if subject_kind:
            sql += "AND s.kind = ? "
            args.append(subject_kind)
        sql += "ORDER BY s.name COLLATE NOCASE"
        return [(r[0], r[1]) for r in self._a.ro.execute(sql, args).fetchall()]

    def has_topic(self, kind, parent_id):
        assert kind in _KINDS, kind
        return self._a.ro.execute(
            f"SELECT 1 FROM {kind}_subject js JOIN subject s ON s.id = js.subject_id "
            f"WHERE js.{kind}_id = ? AND s.kind = 'topic' LIMIT 1", (parent_id,)).fetchone() is not None

    def has_named_tag(self, kind, parent_id, name):
        assert kind in _KINDS, kind
        return self._a.ro.execute(
            f"SELECT 1 FROM {kind}_subject js JOIN subject s ON s.id = js.subject_id "
            f"WHERE js.{kind}_id = ? AND s.name = ? COLLATE NOCASE LIMIT 1",
            (parent_id, name)).fetchone() is not None

    def real_topic_count(self, kind, parent_id, exclude_name):
        assert kind in _KINDS, kind
        return self._a.ro.execute(
            f"SELECT COUNT(*) FROM {kind}_subject js JOIN subject s ON s.id = js.subject_id "
            f"WHERE js.{kind}_id = ? AND s.kind = 'topic' AND s.name <> ? COLLATE NOCASE",
            (parent_id, exclude_name)).fetchone()[0]

    def clear_named(self, kind, parent_id, name):
        assert kind in _KINDS, kind
        self._a.rw.execute(
            f"DELETE FROM {kind}_subject WHERE {kind}_id = ? AND subject_id IN "
            "(SELECT id FROM subject WHERE name = ? COLLATE NOCASE)", (parent_id, name))

    def repoint_tags(self, kind, src_id, dst_id):
        assert kind in _KINDS, kind
        self._a.rw.execute(
            f"INSERT OR IGNORE INTO {kind}_subject ({kind}_id, subject_id) "
            f"SELECT {kind}_id, ? FROM {kind}_subject WHERE subject_id = ?", (dst_id, src_id))
        self._a.rw.execute(f"DELETE FROM {kind}_subject WHERE subject_id = ?", (src_id,))

    # ── curation reads ────────────────────────────────────────────────────────────
    def list_with_counts(self, q, kind, limit):
        sql = ("SELECT s.id, s.name, s.kind, "
               "(SELECT COUNT(*) FROM work_subject WHERE subject_id = s.id), "
               "(SELECT COUNT(*) FROM edition_subject WHERE subject_id = s.id) "
               "FROM subject s ")
        where, args = [], []
        if q and q.strip():
            where.append("s.name LIKE ? COLLATE NOCASE")
            args.append("%" + q.strip() + "%")
        if kind:
            where.append("s.kind = ?")
            args.append(kind)
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY s.name COLLATE NOCASE"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return self._a.ro.execute(sql, args).fetchall()

    def uncurated_count(self, protected_names):
        ph = ",".join("?" * len(protected_names))
        return self._a.ro.execute(
            f"SELECT COUNT(*) FROM subject s WHERE s.kind = 'topic' "
            f"AND s.name COLLATE NOCASE NOT IN ({ph}) "
            "AND NOT EXISTS (SELECT 1 FROM work_subject WHERE subject_id = s.id) "
            "AND NOT EXISTS (SELECT 1 FROM edition_subject WHERE subject_id = s.id) "
            "AND NOT EXISTS (SELECT 1 FROM subject c "
            "                WHERE c.name LIKE s.name || '/%' ESCAPE '\\' COLLATE NOCASE)",
            tuple(protected_names)).fetchone()[0]

    def tagged_works(self, sid):
        return self._a.ro.execute(
            "SELECT w.id, (SELECT text FROM work_alias WHERE work_id = w.id ORDER BY id LIMIT 1) "
            "FROM work_subject ws JOIN work w ON w.id = ws.work_id "
            "WHERE ws.subject_id = ? ORDER BY w.id", (sid,)).fetchall()

    def tagged_editions(self, sid):
        return self._a.ro.execute(
            "SELECT e.id, e.title FROM edition_subject es JOIN edition e ON e.id = es.edition_id "
            "WHERE es.subject_id = ? ORDER BY e.id", (sid,)).fetchall()

    # ── folder-map + holding paths (subject derivation source) ────────────────────
    def folder_map(self):
        return {r[0]: r[1] for r in self._a.ro.execute(
            "SELECT raw_key, label FROM subject_folder_map").fetchall()}

    def set_folder_label(self, raw_key, label):
        self._a.rw.execute(
            "INSERT INTO subject_folder_map (raw_key, label) VALUES (?, ?) "
            "ON CONFLICT(raw_key) DO UPDATE SET label = excluded.label", (raw_key, label))

    def holding_file_paths(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT DISTINCT file_path FROM holding "
            "WHERE file_path IS NOT NULL AND TRIM(file_path) <> ''").fetchall()]

    def first_holding_path(self, edition_id):
        r = self._a.ro.execute(
            "SELECT file_path FROM holding WHERE edition_id = ? "
            "AND file_path IS NOT NULL AND TRIM(file_path) <> '' ORDER BY id LIMIT 1",
            (edition_id,)).fetchone()
        return r[0] if r else None

    def edition_holding_paths(self):
        """(edition_id, file_path) for every holding with a path, id-ordered."""
        return self._a.ro.execute(
            "SELECT edition_id, file_path FROM holding "
            "WHERE file_path IS NOT NULL AND TRIM(file_path) <> '' ORDER BY id").fetchall()

    def work_ids_of_edition(self, edition_id):
        return [r[0] for r in self._a.ro.execute(
            "SELECT work_id FROM edition_work WHERE edition_id = ?", (edition_id,)).fetchall()]

    # ── hierarchy / inheritance (subject_tree) ────────────────────────────────────
    def topic_names(self):
        return [r[0] for r in self._a.ro.execute(
            "SELECT name FROM subject WHERE kind = 'topic'").fetchall()]

    def id_name_kind(self, sid):
        return self._a.ro.execute("SELECT id, name, kind FROM subject WHERE id = ?", (sid,)).fetchone()

    def descendant_ids(self, name):
        """`name` itself plus every subject nested beneath it (prefix match), as ids."""
        esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return [r[0] for r in self._a.ro.execute(
            "SELECT id FROM subject WHERE name = ? COLLATE NOCASE "
            "OR name LIKE ? ESCAPE '\\' COLLATE NOCASE", (name, esc + "/%")).fetchall()]

    def has_children(self, name, kind):
        esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self._a.ro.execute(
            "SELECT 1 FROM subject WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE "
            "AND kind = ? LIMIT 1", (esc + "/%", kind)).fetchone() is not None

    def editions_covering(self, subject_ids):
        """LIVE edition ids a set of subjects covers — tagged directly OR via a contained work."""
        if not subject_ids:
            return []
        ph = ",".join("?" * len(subject_ids))
        return [r[0] for r in self._a.ro.execute(
            f"SELECT DISTINCT eid FROM ("
            f"  SELECT edition_id AS eid FROM edition_subject WHERE subject_id IN ({ph})"
            f"  UNION"
            f"  SELECT ew.edition_id FROM edition_work ew"
            f"    JOIN work_subject ws ON ws.work_id = ew.work_id"
            f"    WHERE ws.subject_id IN ({ph})"
            f") e JOIN edition ed ON ed.id = e.eid WHERE ed.deleted_at IS NULL",
            (*subject_ids, *subject_ids)).fetchall()]
