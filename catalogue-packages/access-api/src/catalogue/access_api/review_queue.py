"""Review queue + promotion — the gateway-bound access surface (`acc.review`).

The `review_queue` is the operator's work list — rows of `(item_type, payload_json, status)` for
title proposals, work-authorship candidates, edition dedup/verify, ingest promotions, … The
`promotion` table records what a promoted ingest item created (`work_ids`/`person_ids` JSON +
`holding_id`) so a promotion can be reverted. Neither is a soft-delete *entity* (no identity
fingerprint, no tombstone), so this is a FLAT policy-gated repo, not an aggregate: `.reads` run over
the RO connection; `.writes` STAGE on the connection and the CALLER commits (services drive the queue
inside their own transaction via `system_conn`). See entity_api_model.md §8/§9.
"""
from __future__ import annotations

import json

from catalogue.contracts import AccessMode, Action

_RESOURCE = "review_queue"
_PROMOTION_COLS = ("work_ids", "person_ids", "holding_id")


def _payload(p) -> str:
    """Accept a dict (json-encode it) or an already-serialized json string."""
    return p if isinstance(p, str) else json.dumps(p)


def _check_promo_col(column: str) -> None:
    if column not in _PROMOTION_COLS:
        raise ValueError(f"not a promotion column: {column!r}")


class _Reads:
    def __init__(self, access):
        self._a = access

    def _read(self, verb: str) -> None:
        self._a.authorize(Action(_RESOURCE, verb, AccessMode.READ))

    def get(self, item_id: int):
        """{id, item_type, payload_json, status} for an item, or None."""
        self._read("get")
        r = self._a.ro.execute(
            "SELECT id, item_type, payload_json, status FROM review_queue WHERE id = ?",
            (item_id,)).fetchone()
        return dict(zip(("id", "item_type", "payload_json", "status"), r)) if r else None

    def get_typed(self, item_id: int, item_type):
        """(payload_json, status) for an item iff it is one of `item_type` (a str or tuple), else
        None — mirrors the `WHERE id = ? AND item_type = ?` guard the accept/reject paths use."""
        self._read("get_typed")
        types = (item_type,) if isinstance(item_type, str) else tuple(item_type)
        ph = ",".join("?" * len(types))
        r = self._a.ro.execute(
            f"SELECT payload_json, status FROM review_queue WHERE id = ? AND item_type IN ({ph})",
            (item_id, *types)).fetchone()
        return (r[0], r[1]) if r else None

    def status_of(self, item_id: int, item_type=None):
        """The status of an item (optionally constrained to type(s)), or None if absent/mismatch."""
        self._read("status_of")
        if item_type is None:
            r = self._a.ro.execute(
                "SELECT status FROM review_queue WHERE id = ?", (item_id,)).fetchone()
        else:
            types = (item_type,) if isinstance(item_type, str) else tuple(item_type)
            ph = ",".join("?" * len(types))
            r = self._a.ro.execute(
                f"SELECT status FROM review_queue WHERE id = ? AND item_type IN ({ph})",
                (item_id, *types)).fetchone()
        return r[0] if r else None

    def pending_payloads(self, item_type: str):
        """payload_json of every pending item of a type, id-ordered."""
        self._read("pending_payloads")
        return [r[0] for r in self._a.ro.execute(
            "SELECT payload_json FROM review_queue WHERE item_type = ? AND status = 'pending' "
            "ORDER BY id", (item_type,)).fetchall()]

    def payloads_by_type(self, item_type: str):
        """payload_json of every item of a type REGARDLESS of status (the structure-guess seed reads
        resolved/rejected proposals too), id-ordered."""
        self._read("payloads_by_type")
        return [r[0] for r in self._a.ro.execute(
            "SELECT payload_json FROM review_queue WHERE item_type = ? ORDER BY id",
            (item_type,)).fetchall()]

    def pending_items(self, item_type: str):
        """(id, payload_json) of every pending item of a type, id-ordered."""
        self._read("pending_items")
        return self._a.ro.execute(
            "SELECT id, payload_json FROM review_queue WHERE item_type = ? AND status = 'pending' "
            "ORDER BY id", (item_type,)).fetchall()

    def all_pending(self):
        """(id, item_type, payload_json) for every pending item."""
        self._read("all_pending")
        return self._a.ro.execute(
            "SELECT id, item_type, payload_json FROM review_queue WHERE status = 'pending'").fetchall()

    def pending_count(self, item_type: str) -> int:
        """How many items of `item_type` are pending — a queue-badge count."""
        self._read("pending_count")
        return self._a.ro.execute(
            "SELECT count(*) FROM review_queue WHERE item_type = ? AND status = 'pending'",
            (item_type,)).fetchone()[0]

    def type_counts(self, status: str) -> dict:
        """{item_type: count} of items at `status` — the review tab counts."""
        self._read("type_counts")
        return {r[0]: r[1] for r in self._a.ro.execute(
            "SELECT item_type, COUNT(*) FROM review_queue WHERE status = ? GROUP BY item_type",
            (status,)).fetchall()}

    def list_at_status(self, status: str, item_type=None, limit: int = 500):
        """(id, item_type, status) for items at `status` (optionally one type), created_at-ordered."""
        self._read("list_at_status")
        sql = "SELECT id, item_type, status FROM review_queue WHERE status = ?"
        args = [status]
        if item_type:
            sql += " AND item_type = ?"
            args.append(item_type)
        sql += " ORDER BY created_at LIMIT ?"
        args.append(limit)
        return self._a.ro.execute(sql, args).fetchall()

    def item_type_codes(self):
        """Every review_item_type code (the review-tab vocabulary), code-ordered."""
        self._read("item_type_codes")
        return [r[0] for r in self._a.ro.execute(
            "SELECT code FROM review_item_type ORDER BY code").fetchall()]

    def detail(self, item_id: int):
        """(id, item_type, payload_json, status, created_at, resolved_at) for an item, or None."""
        self._read("detail")
        return self._a.ro.execute(
            "SELECT id, item_type, payload_json, status, created_at, resolved_at "
            "FROM review_queue WHERE id = ?", (item_id,)).fetchone()

    def status_count(self, item_type: str, status: str) -> int:
        """How many items of `item_type` are in `status`."""
        self._read("status_count")
        return self._a.ro.execute(
            "SELECT count(*) FROM review_queue WHERE item_type = ? AND status = ?",
            (item_type, status)).fetchone()[0]

    def items_by_type_status(self, item_type: str, status: str):
        """(id, payload_json) for items of `item_type` in `status`, id-ordered."""
        self._read("items_by_type_status")
        return self._a.ro.execute(
            "SELECT id, payload_json FROM review_queue WHERE item_type = ? AND status = ? ORDER BY id",
            (item_type, status)).fetchall()

    def latest_pending_payload(self, item_type: str, *likes: str):
        """payload_json of the NEWEST (highest-id) pending item of `item_type` matching ALL `likes`,
        or None — the 'what title would this become' lookup."""
        self._read("latest_pending_payload")
        clause = "".join(" AND payload_json LIKE ?" for _ in likes)
        r = self._a.ro.execute(
            f"SELECT payload_json FROM review_queue WHERE item_type = ? AND status = 'pending'"
            f"{clause} ORDER BY id DESC LIMIT 1", (item_type, *likes)).fetchone()
        return r[0] if r else None

    def latest_payload_of_type(self, item_type: str, *likes: str):
        """payload_json of the NEWEST item of `item_type` (ANY status) matching ALL `likes`, or None
        — the 'what did the last resolve apply' lookup."""
        self._read("latest_payload_of_type")
        clause = "".join(" AND payload_json LIKE ?" for _ in likes)
        r = self._a.ro.execute(
            f"SELECT payload_json FROM review_queue WHERE item_type = ?{clause} "
            f"ORDER BY id DESC LIMIT 1", (item_type, *likes)).fetchone()
        return r[0] if r else None

    def pending_id_by_json(self, item_type: str, json_path: str, value):
        """The id of the pending item of `item_type` whose `json_extract(payload, json_path)` == value,
        or None — the idempotent-on-a-payload-field enqueue lookup (indexed, not an O(n) scan)."""
        self._read("pending_id_by_json")
        r = self._a.ro.execute(
            "SELECT id FROM review_queue WHERE item_type = ? AND status = 'pending' "
            "AND json_extract(payload_json, ?) = ?", (item_type, json_path, value)).fetchone()
        return r[0] if r else None

    def exists_pending(self, item_type: str, *likes: str) -> bool:
        """Whether a pending item of `item_type` exists whose payload_json matches ALL `likes`
        (the idempotent-re-enqueue guard). Pass the LIKE patterns positionally."""
        self._read("exists_pending")
        clause = "".join(" AND payload_json LIKE ?" for _ in likes)
        return self._a.ro.execute(
            f"SELECT 1 FROM review_queue WHERE item_type = ? AND status = 'pending'{clause} LIMIT 1",
            (item_type, *likes)).fetchone() is not None

    # ── promotion ───────────────────────────────────────────────────────────────
    def promotion(self, review_item_id: int):
        """(work_ids, person_ids, holding_id) for a promotion row, or None."""
        self._read("promotion")
        return self._a.ro.execute(
            "SELECT work_ids, person_ids, holding_id FROM promotion WHERE review_item_id = ?",
            (review_item_id,)).fetchone()

    def promotion_exists(self, review_item_id: int) -> bool:
        self._read("promotion_exists")
        return self._a.ro.execute(
            "SELECT 1 FROM promotion WHERE review_item_id = ?", (review_item_id,)).fetchone() is not None

    def promotion_column(self, review_item_id: int, column: str):
        """One whitelisted promotion column for a row, or None."""
        self._read("promotion_column")
        _check_promo_col(column)
        r = self._a.ro.execute(
            f"SELECT {column} FROM promotion WHERE review_item_id = ?", (review_item_id,)).fetchone()
        return r[0] if r else None

    def promotion_rows(self, column: str):
        """(review_item_id, <column>) for every promotion row — the dangling-ref scan input."""
        self._read("promotion_rows")
        _check_promo_col(column)
        return self._a.ro.execute(
            f"SELECT review_item_id, {column} FROM promotion").fetchall()


class _Writes:
    def __init__(self, access):
        self._a = access

    def _write(self, verb: str) -> None:
        self._a.authorize(Action(_RESOURCE, verb, AccessMode.WRITE))

    def enqueue(self, item_type: str, payload) -> int:
        """Append an item (payload = dict or json str); returns its id. Staged; caller commits."""
        self._write("enqueue")
        return self._a.rw.execute(
            "INSERT INTO review_queue (item_type, payload_json) VALUES (?, ?)",
            (item_type, _payload(payload))).lastrowid

    def set_payload(self, item_id: int, payload) -> None:
        self._write("set_payload")
        self._a.rw.execute("UPDATE review_queue SET payload_json = ? WHERE id = ?",
                           (_payload(payload), item_id))

    def set_status(self, item_id: int, status: str) -> None:
        """Set an item's status; stamps resolved_at (or clears it back to NULL for 'pending')."""
        self._write("set_status")
        if status == "pending":
            self._a.rw.execute(
                "UPDATE review_queue SET status = 'pending', resolved_at = NULL WHERE id = ?",
                (item_id,))
        else:
            self._a.rw.execute(
                "UPDATE review_queue SET status = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, item_id))

    def resolve(self, item_id: int) -> None:
        self.set_status(item_id, "resolved")

    def reject(self, item_id: int) -> None:
        self.set_status(item_id, "rejected")

    def reopen(self, item_id: int) -> None:
        self.set_status(item_id, "pending")

    def delete(self, item_id: int) -> None:
        self._write("delete")
        self._a.rw.execute("DELETE FROM review_queue WHERE id = ?", (item_id,))

    def delete_by_json_in(self, item_type: str, json_path: str, values) -> int:
        """Delete items of `item_type` whose `json_extract(payload, json_path)` is in `values` (the
        reprocess clear of stale low_quality_ocr rows); returns rowcount. Staged; caller commits."""
        self._write("delete_by_json_in")
        vals = list(values)
        if not vals:
            return 0
        return self._a.rw.execute(
            f"DELETE FROM review_queue WHERE item_type = ? "
            f"AND json_extract(payload_json, ?) IN ({','.join('?' * len(vals))})",
            (item_type, json_path, *vals)).rowcount

    def resolve_pending_of_type(self, item_type: str, *likes: str) -> int:
        """Mark every PENDING item of `item_type` matching ALL `likes` resolved (an authoritative
        record superseding a queued guess); returns the rowcount. Staged; caller commits."""
        self._write("resolve_pending_of_type")
        clause = "".join(" AND payload_json LIKE ?" for _ in likes)
        return self._a.rw.execute(
            f"UPDATE review_queue SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP "
            f"WHERE item_type = ? AND status = 'pending'{clause}",
            (item_type, *likes)).rowcount

    def delete_pending_of_type(self, item_type: str, *likes: str) -> int:
        """Delete every PENDING item of `item_type` matching ALL `likes` (stale-row cleanup);
        returns the rowcount. Staged; caller commits."""
        self._write("delete_pending_of_type")
        clause = "".join(" AND payload_json LIKE ?" for _ in likes)
        return self._a.rw.execute(
            f"DELETE FROM review_queue WHERE item_type = ? AND status = 'pending'{clause}",
            (item_type, *likes)).rowcount

    # ── promotion ───────────────────────────────────────────────────────────────
    def insert_promotion(self, review_item_id: int, holding_id, work_ids, person_ids) -> None:
        self._write("insert_promotion")
        self._a.rw.execute(
            "INSERT INTO promotion (review_item_id, holding_id, work_ids, person_ids) "
            "VALUES (?, ?, ?, ?)",
            (review_item_id, holding_id, _payload(work_ids), _payload(person_ids)))

    def set_promotion_column(self, review_item_id: int, column: str, value) -> None:
        self._write("set_promotion_column")
        _check_promo_col(column)
        self._a.rw.execute(
            f"UPDATE promotion SET {column} = ? WHERE review_item_id = ?", (value, review_item_id))

    def delete_promotion(self, *, review_item_id: int = None, holding_id: int = None) -> int:
        """Drop promotion rows by review_item_id and/or holding_id (whichever is given); returns the
        total rowcount removed."""
        self._write("delete_promotion")
        n = 0
        if review_item_id is not None:
            n += self._a.rw.execute(
                "DELETE FROM promotion WHERE review_item_id = ?", (review_item_id,)).rowcount
        if holding_id is not None:
            n += self._a.rw.execute(
                "DELETE FROM promotion WHERE holding_id = ?", (holding_id,)).rowcount
        return n


class ReviewQueueRepo:
    """`.reads` (queue/promotion queries, READ) + `.writes` (enqueue/resolve/promote, WRITE) over a
    bound `Access`. Flat (not an aggregate) — the queue is a work list, not a soft-delete entity."""

    def __init__(self, access):
        self.reads = _Reads(access)
        self.writes = _Writes(access)
