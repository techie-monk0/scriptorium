# Editions, works & commentary relationships — data model

How the catalogue models **classical vs modern authorship** and the **two layers of
commentary relationships** between texts. This sits alongside
[`frbr_data_model.md`](frbr_data_model.md) (the FRBR work↔edition↔holding spine) and
zooms in on the root/commentary structure used by the Browse editions/works pages.

> **Status.** Both layers are **live**. *Layer 1* (work↔work `relationship`) and *Layer 2*
> (edition→work modern commentary, `edition_commentary_on`) are implemented: the table,
> the `library.edition_commentaries` builder, the add/remove routes
> (`/edition/<eid>/modern-commentary/add` + `…/<wid>/remove`), and the Browse banner +
> per-work ⬑ back-ref. The old single-only degenerate-work commentary UI was replaced.

Terminology note: **"classical"** here is loose — it means *the source text being
commented on* (the older/root work), **not** a DB flag. Nothing in the model keys off a
"classical" attribute; it's all driven by the relationship edges.

---

## 1. Core entities

| Entity | Table | Key fields | Meaning |
|---|---|---|---|
| **Edition** | `edition` | `id`, `title`, `structure` ∈ {`single_work`, `multi_work`} | A published text (FRBR Expression+Manifestation, collapsed). Groups **one or more holdings**. |
| **Holding** | `holding` | `id`, `edition_id`, `form`, `holding_type`, `file_path`, `isbn`, … | A concrete copy of an edition (FRBR Item): a physical book, a PDF, an EPUB. |
| **Work** | `work` | `id`, `work_type` ∈ {`root`, `commentary`, `null`} | An abstract text (FRBR Work). The thing being expressed. |
| **Work title(s)** | `work_alias` | `work_id`, `text`, `scheme` (english/wylie/iast/…) | A work has many titles across scripts; the first is the display title. |
| **Person** | `person` | `id`, `primary_name`, `dates`, … | An author / translator / teacher. |

### An edition is NOT "the single book you hold"

An **edition** is the *abstract published text*; the physical/electronic copies of it are
**holdings**. One edition can have **several holdings of different forms** — a physical
copy, a PDF, and an EPUB of the same published text are **three holdings under one
edition**, not three editions. (This is why duplicate-format editions were consolidated
into one edition with many holdings.)

```
holding(id, edition_id → edition, form, file_path, file_hash, holding_type,
        text_status, ocr_quality_score, shelf_location, isbn, notes, …)
```

- `form` ∈ {`physical`, `electronic`}  (`form_type`)
- `holding_type` ∈ {`physical`, `pdf`, `epub`}  (`holding_type`)
- Holdings cascade-delete with their edition (`ON DELETE CASCADE`).

So: **Work** (abstract text) → realized by → **Edition** (published text) → instantiated
as → **Holding(s)** (the physical/digital copies).

**Containment:** `edition_work(edition_id, work_id, sequence, locator)` — which works a
book contains, in order.

---

## 2. Two kinds of authorship (the crux)

Authorship lives at **two levels**, meaning two different things:

- **Work-level** — `work_author(work_id, person_id, role)`. The authors *of the source
  text itself* — the **classical / root** authors (Tsongkhapa; First Panchen Lama #21).
- **Edition-level** — `edition_author`, `edition_translator`. The **modern** figures who
  produced *this book* — the teacher giving the commentary, the translator (Lama Yeshe;
  Dalai Lama XIV #1 + Alexander Berzin #67).

"Classical" vs "modern" is therefore **which level the person sits at** — not a flag. The
contained works carry classical authorship; the edition carries modern authorship.

---

## 3. Two layers of commentary relationships

### Layer 1 — work ↔ work (classical, scholastic) — **EXISTS**

```sql
relationship(id, from_work_id → work, relation → relation_type, to_work_id → work,
             from_section_locator)
relation_type(code, label)   -- commentary_on, comments_on, sub_comments_on, summarizes, cites
```

- **Direction:** `from_work_id` = the commentary, `to_work_id` = the text it comments on.
- **Scope:** edges *between contained works* — the internal scholastic structure (a root
  and its auto-commentary; a sub-commentary on a commentary).
- A work can be **both at once** (a commentary on X *and* commented-on by Y) → a
  **sub-commentary chain** `r ← w ← w'`. Because of this, **`work.work_type` is only a
  hint** — a single label can't capture a dual-role work. **The edges are the source of
  truth**, not the label.

### Layer 2 — edition → work (modern commentary)

```sql
-- Many rows per edition allowed (many-to-many).
edition_commentary_on(edition_id → edition, to_work_id → work)
```

- **Meaning:** *this whole book, by its modern (edition-level) author, is a modern
  commentary on classical work `to_work_id`.*
- **Many-to-many:** an edition may comment on **several** classical works (one modern
  book that walks through multiple source texts → several edges).
- **Target can be internal** (a work also contained in this edition) **or external** (a
  source text not held in this book).
- Replaces the "degenerate-work" trick (§5) and works **identically** for single- and
  multi-work editions.

> Shipped as a single-purpose `edition_commentary_on` table. A general typed
> `edition_relationship(edition_id, relation, to_work_id)` — to later add `translation_of` /
> `abridgement_of` — remains a possible future generalization.

---

## 4. Single-work vs multi-work editions

`edition.structure` distinguishes them; it changes **display surfacing**, not the
relationship model.

- **`single_work` (ES):** one primary contained work. Historically the modern commentary
  was faked via a **degenerate placeholder work** (title = edition title) carrying a
  Layer-1 edge to the source. With Layer 2, just an `edition_commentary_on` edge → no
  placeholder needed.
- **`multi_work` (EM):** several contained classical works. **No placeholder exists** to
  hang a modern-commentary edge on — which is *why* Layer 2 must be **edition-level**. The
  contained works relate to each other via Layer 1; the book-as-a-whole relates to its
  source(s) via Layer 2.

Surfacing rule (`edition_work_summaries`): a contained work is shown when the edition is
multi-work, OR it shares the edition with siblings, OR it has a canonical number, OR it
participates in a root/commentary edge.

---

## 5. The "degenerate work" — and why Layer 2 supersedes it

A *degenerate work* is an auto-minted placeholder whose title equals the edition title,
created during import before a real work is curated in. It was the **only** way to attach
a modern-commentary edge in a single-work edition (hang a Layer-1 edge off the
placeholder). A multi-work edition has no such placeholder — so the trick doesn't
generalize. Making Layer 2 a first-class **edition→work** edge removes the need for the
degenerate work and makes ES and EM uniform.

---

## 6. The full picture

```
                        edition_author / edition_translator
                                   │  (MODERN people: Lama Yeshe, Dalai Lama, translators)
                                   ▼
   ┌────────────────────────── EDITION ──────────────────────────┐
   │  structure: single_work | multi_work                        │
   │                                                             │
   │   edition_commentary_on ───────────► WORK   (Layer 2:       │   ← edition is a MODERN
   │      (0..N edges, internal or external)     "modern         │     commentary on these
   │                                              commentary on") │
   │                                                             │
   │   edition_work (containment, sequence)                      │
   │      ├─► WORK ──┐                                           │
   │      ├─► WORK   │  Layer 1: relationship.commentary_on      │
   │      └─► WORK ──┘  (from=commentary → to=root, among         │   ← CLASSICAL scholastic
   │                    contained works; chains allowed)          │     structure
   │                                                             │
   │   holding (0..N: physical / pdf / epub of THIS edition)     │   ← the copies you hold
   └─────────────────────────────────────────────────────────────┘
              │
              ▼
   work_author (CLASSICAL people: Tsongkhapa, Panchen Lama)
   work_alias  (titles per script)
```

---

## 7. Worked examples (live data)

**ES — #19 "The Bliss of Inner Fire"** (Lama Yeshe):
- Contains classical work **#483** "A Book of Three Inspirations" (Tsongkhapa).
- **Layer 2 (proposed):** edition #19 → #483 — *the book is Lama Yeshe's modern commentary
  on 3I*.
- **Layer 1:** #483 → #437 (3I is itself a classical commentary on "Personal Instructions
  on the Six Yogas"); and #652 → #483 ("Notes on 3I", a sub-commentary living in #23).

**EM — #172 "The Gelug/Kagyü Tradition of Mahāmudrā"** (Dalai Lama XIV #1, tr. Berzin #67):
- Contains **#594** (Mahāmudrā Root Text) and **#630** (Auto-Commentary), both by Panchen
  Lozang Chökyi Gyaltsen #21.
- **Layer 1:** #630 → #594 (the Panchen Lama's auto-commentary on his own root).
- **Layer 2 (proposed):** edition #172 → #630 — *the Dalai Lama's modern commentary is on
  the auto-commentary #630*. An **internal** target (#630 is also contained).

**EM with multiple Layer-2 edges:** a modern teaching book → {#A, #B, #C} carries three
`edition_commentary_on` rows.

---

## 8. How Browse renders it

### Edition page

```
┌ <Edition title>   (single_work | multi_work)   by <modern author> · tr. <translator>
│   📘 This edition is a modern commentary on:           ← Layer 2 banner
│        • #A  <Classical Work A>
│        • #B  <Classical Work B>   (external — links out)
│   [edition basics: ISBN, publisher, subjects…]
│
│   Works in this edition                                ← contained classical works
│   ─ #A  <Classical Work A>
│        📖 Commentary on #R <Root>                      ← Layer 1 line (classical)
│        ⬑ this edition's modern commentary is on this work   ← back-ref to Layer 2
│   ─ #D  <other contained work, no edge>                ← no marker
│
│   Holdings                                             ← the copies of this edition
│   ─ 📄 PDF · 📕 EPUB · 📦 Physical copy (shelf …)
└
```

Rules:
- The **📘 banner** appears whenever the edition has ≥1 Layer-2 edge: a single line for one
  target, a bulleted list for several.
- A Layer-2 target that **is** a contained work gets a **⬑ back-reference** on its block;
  external targets appear in the banner only.
- **📖 Commentary on …** (Layer 1) shows whenever a contained work has an outgoing
  `commentary_on` edge — **regardless of its `work_type` label**, so a dual-role work
  (commentary on X *and* commented-on by Y) still shows its commentary line.
- A contained work with no edge gets no line.
- **Holdings** are listed once per edition (physical / pdf / epub), not as separate
  editions.

---

## 9. Query cookbook

```sql
-- contained works of an edition, in order
SELECT work_id FROM edition_work WHERE edition_id = :e ORDER BY sequence;

-- holdings (copies) of an edition
SELECT id, form, holding_type, file_path, isbn FROM holding WHERE edition_id = :e;

-- Layer 1: what a work comments on (its root), if anything
SELECT to_work_id FROM relationship WHERE from_work_id = :w AND relation = 'commentary_on';

-- Layer 1: what comments on a work (it is a root for these)
SELECT from_work_id FROM relationship WHERE to_work_id = :w AND relation = 'commentary_on';

-- Layer 2: classical works this edition is a modern commentary on
SELECT to_work_id FROM edition_commentary_on WHERE edition_id = :e;

-- Layer 2: editions that are modern commentaries on a work
SELECT edition_id FROM edition_commentary_on WHERE to_work_id = :w;

-- classical (work-level) authors of a contained work
SELECT person_id, role FROM work_author WHERE work_id = :w;

-- modern (edition-level) people
SELECT person_id FROM edition_author WHERE edition_id = :e;
SELECT person_id FROM edition_translator WHERE edition_id = :e ORDER BY seq;
```

---

## 10. Summary

- An **edition** is an abstract published text that **groups many holdings** (physical,
  pdf, epub) — *not* a single physical object.
- **Classical authorship** lives on the **work** (`work_author`); **modern authorship**
  lives on the **edition** (`edition_author`/`edition_translator`). "Classical/modern" =
  which level, not a flag.
- **Layer 1** = work↔work classical commentary (`relationship`, exists). **Layer 2** =
  edition→work modern commentary (`edition_commentary_on`, **proposed**, many-to-many,
  internal or external target).
- **`structure`** (single/multi-work) only affects **surfacing**; the relationship model
  is identical.
- The **relationship edges — not the `work_type` label — are the source of truth.**
```
