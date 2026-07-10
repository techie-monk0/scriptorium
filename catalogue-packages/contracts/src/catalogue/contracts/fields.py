"""Categorical (controlled-vocabulary) scalar fields — one declarative registry for every
enum-like column that hangs off an entity (genre, tenet_system, tradition, work_type…).

A *scalar controlled-vocab field* is a single TEXT column whose value is drawn from a
controlled list — the `tenet_system` shape, NOT the tradition-*entity* shape (a separate
table + multi-label `work_tradition` join). One `CategoricalField` per (entity, column) is the
SINGLE source of truth the whole stack reads, so a new controlled-vocab column is added in ONE
place instead of being hand-threaded through schema/gate/routes/templates:

  • db_store migration      — `ADD COLUMN` for each declared field (`ensure_field_columns`)
  • the write gate          — reject a value outside a `strict` field's vocab (`choices` on
                              `FieldRule`) + the direct-command store guard (`validate`)
  • the edit-form vocab     — `acc.vocab.field_values(entity, name)` → the <select>/<datalist>
  • the writable-scalar set — the scalar columns a create/update may touch
  • superset queries        — `subtree(field, value)` expands a taxonomy node to its closure

Two vocab sources: a **fixed** closed list (`values=…`, e.g. genre/tenet_system) or a **table**
whose live rows ARE the vocabulary (`vocab_table=…`, e.g. tradition/work_type — runtime-
extensible via config/seed). `strict` fields render a <select> and reject an out-of-vocab write;
non-strict fields render a <datalist> of suggestions and accept free text (tradition's
established behaviour). `open_vocab` fields register an unseen code on write (work_type).

**Hierarchy.** A field may carry a `taxonomy` (`parent node → child nodes`). The stored value is
still ONE node — the most specific known (a leaf), or an internal node when only the coarse class
is known. A query for a node matches its whole SUBTREE: `subtree(tenet, "Madhyamaka")` returns
Madhyamaka + both Svātantrika sub-schools + Prāsaṅgika, so `WHERE tenet_system IN (…)` realises
`Madhyamaka = Svātantrika-Madhyamaka ∪ Prāsaṅgika-Madhyamaka` at query time — no ancestry table,
no schema cost. A field with no taxonomy degrades to plain equality.

Pure data + pure functions — no I/O, no DB (a table-backed field's live vocabulary is supplied by
the caller via `table_lookup`), so the same registry drives the server and a client previewing a
create. See docs/access/entity_api_model.md §4.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── fixed vocabularies ────────────────────────────────────────────────────────
# Tenet taxonomy — an internal (superset) node → its immediate children. A stored value may be
# ANY node: a leaf when the specific tenet is known, an internal node when only the coarse class
# is. Declared beside TENET_VOCAB so the vocab and its hierarchy stay in one place.
TENET_TAXONOMY: "dict[str, tuple[str, ...]]" = {
    "Madhyamaka": ("Svātantrika-Madhyamaka", "Prāsaṅgika-Madhyamaka"),
    "Svātantrika-Madhyamaka": ("Sautrāntika-Svātantrika-Madhyamaka",
                               "Yogācāra-Svātantrika-Madhyamaka"),
}

# The tenet/siddhānta controlled vocabulary (every storable node, leaves + supersets) — the
# canonical home the classifier (db_store.migrate_tenet) and the editable field both read.
# Follows the Gelug drubta (tenets) literature; see migrate_tenet for the doxographic sources.
TENET_VOCAB: tuple[str, ...] = (
    "Vaibhāṣika", "Sautrāntika", "Cittamātra",
    # Madhyamaka subtree (superset nodes first, then leaves — see TENET_TAXONOMY)
    "Madhyamaka",
    "Svātantrika-Madhyamaka",
    "Sautrāntika-Svātantrika-Madhyamaka", "Yogācāra-Svātantrika-Madhyamaka",
    "Prāsaṅgika-Madhyamaka",
    "Jonang-Shentong",
    # Shentong of the Kagyü/Rimé revival (Rangjung Dorjé, Kongtrul, Situ…) — distinct from the
    # Dolpopa/Jonang line above. A flat leaf (not nested under Cittamātra) though Gelug polemics
    # reduce it there; kept its own label so the figures aren't silently filed as Cittamātra.
    "Shentong",
    # non-Buddhist siddhāntas (rare as authors here, common as opponents in text)
    "Sāṃkhya", "Nyāya", "Vaiśeṣika", "Mīmāṃsā", "Cārvāka", "Jaina",
    # scope labels — NOT tenets and deliberately OUTSIDE the taxonomy (a query for a tenet must
    # never sweep them in). 'Common' = a text shared across all schools, not propounding one
    # (e.g. the Pramāṇavārttika — a pramāṇa treatise, even though its author is a Cittamātrin);
    # setting it explicitly is how a work says "tenet-neutral on purpose" and stops inheriting the
    # author's tenet. 'n/a-modern-scholarship' = a modern author who holds no tenet.
    "Common",
    "n/a-modern-scholarship",
)

# A work's rhetorical genre (flat — apologetic/polemic live in the Argumentative definition, not
# as stored sub-values).
GENRE_VALUES: tuple[str, ...] = ("Argumentative", "Doxography", "Monograph")

_GENRE_HELP = (
    "Argumentative = apologetic or polemic — apologetic: a work whose primary aim is offensive "
    "(to attack and demolish rival positions); polemic: a work whose primary aim is defensive "
    "(to shield one's own position against actual or anticipated attacks). "
    "Doxography = a work that surveys and classifies many schools' opinions, usually into a "
    "ranked hierarchy. "
    "Monograph = a treatise expounding a single subject or system in a systematic, self-contained "
    "way."
)


@dataclass(frozen=True)
class CategoricalField:
    """One scalar controlled-vocab column on an entity — the declaration every layer reads."""
    name: str                                    # column + logical name, e.g. "genre"
    entity: str                                  # "work" | "person" | "edition"
    label: str                                   # UI label, "Genre"
    values: tuple[str, ...] = ()                 # fixed closed vocab (mutually exclusive w/ vocab_table)
    vocab_table: "str | None" = None             # draw live vocab from this table's rows instead
    strict: bool = True                          # reject out-of-vocab writes + render a <select>
    open_vocab: bool = False                     # register an unseen code on write (work_type)
    taxonomy: "dict[str, tuple[str, ...]] | None" = None  # parent → children (superset queries)
    help: str = ""                               # form help text
    max_len: int = 200                           # column length guard


# The registry — every scalar controlled-vocab field, across entities.
FIELDS: tuple[CategoricalField, ...] = (
    # ── NEW: rhetorical genre on works ───────────────────────────────────────
    CategoricalField("genre", "work", "Genre", values=GENRE_VALUES, help=_GENRE_HELP),
    # ── retrofit: the pre-existing scalar controlled-vocab fields ─────────────
    CategoricalField("tenet_system", "work", "Tenet system", values=TENET_VOCAB,
                     taxonomy=TENET_TAXONOMY,
                     help="Doctrinal / siddhānta home (e.g. Prāsaṅgika-Madhyamaka)."),
    CategoricalField("tenet_system", "person", "Tenet system", values=TENET_VOCAB,
                     taxonomy=TENET_TAXONOMY,
                     help="Doctrinal / siddhānta home (e.g. Prāsaṅgika-Madhyamaka)."),
    # tradition: a free box with config-driven suggestions (its established behaviour) — the
    # scalar mirror; the tradition *entity* (table + multi-label work_tradition join) is separate.
    CategoricalField("tradition", "work", "Tradition", vocab_table="tradition", strict=False),
    CategoricalField("tradition", "person", "Tradition", vocab_table="tradition", strict=False),
    CategoricalField("tradition", "edition", "Tradition", vocab_table="tradition", strict=False),
    # work_type: an open vocab (root/commentary today; a new code is registered on write).
    CategoricalField("work_type", "work", "Type", vocab_table="work_type", strict=False,
                     open_vocab=True),
)


# ── registry helpers ──────────────────────────────────────────────────────────
def fields_for(entity: str) -> "tuple[CategoricalField, ...]":
    """Every categorical field declared on `entity`, in registry order."""
    return tuple(f for f in FIELDS if f.entity == entity)


def get_field(entity: str, name: str) -> "CategoricalField | None":
    """The declaration for (entity, column), or None."""
    return next((f for f in FIELDS if f.entity == entity and f.name == name), None)


def writable_field_names(entity: str) -> "tuple[str, ...]":
    """The column names of `entity`'s categorical fields — folded into the writable-scalar set."""
    return tuple(f.name for f in fields_for(entity))


def allowed_values(field: CategoricalField, table_lookup=None) -> "tuple[str, ...]":
    """The field's current allowed vocabulary: its fixed `values`, or the rows of its
    `vocab_table` supplied by `table_lookup(table) -> iterable[str]` (empty when a table-backed
    field has no lookup)."""
    if field.values:
        return tuple(field.values)
    if field.vocab_table is not None and table_lookup is not None:
        return tuple(table_lookup(field.vocab_table))
    return ()


def subtree(field: CategoricalField, value: str) -> "tuple[str, ...]":
    """Every value a query for `value` should match: `value` plus all its descendants in the
    field's taxonomy (a superset expands to its subtree). A leaf, an unknown value, or a field
    with no taxonomy yields just `(value,)`. The set-union query semantics, evaluated in memory."""
    tax = field.taxonomy or {}
    out: list[str] = []
    seen: set[str] = set()
    stack = [value]
    while stack:
        v = stack.pop()
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
        stack.extend(tax.get(v, ()))
    return tuple(out)


def query_closure(entity: str, name: str, value: str) -> "tuple[str, ...]":
    """`subtree` for the registered (entity, field) — the values a `WHERE col IN (…)` filter for
    `value` should use (superset → its whole subtree; leaf/unknown → itself)."""
    f = get_field(entity, name)
    return subtree(f, value) if f else (value,)


def _norm(v):
    return v.strip() if isinstance(v, str) else v


def validate(entity: str, values: dict, table_lookup=None) -> "tuple[str, ...]":
    """Error messages for a write payload `values` ({column: value}). Only the categorical
    fields PRESENT are checked (an update patch is fine); clearing (None / '') is always allowed.
    Flags an out-of-vocab value on a `strict`, non-open field. `table_lookup` supplies live vocab
    for table-backed strict fields (unused by the current registry, which is fixed-vocab)."""
    errs: list[str] = []
    ent = {f.name: f for f in fields_for(entity)}
    for name, raw in values.items():
        f = ent.get(name)
        if f is None:
            continue
        v = _norm(raw)
        if v is None or v == "":
            continue
        if f.strict and not f.open_vocab:
            allowed = allowed_values(f, table_lookup)
            if allowed and v not in allowed:
                errs.append(f"{f.label}: {v!r} is not a valid option ({', '.join(allowed)})")
    return tuple(errs)
