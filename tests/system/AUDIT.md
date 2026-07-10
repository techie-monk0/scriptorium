# Test audit — plan-driven vs implementation-mirroring

Honest pass over the suite as of Step 4. A test is **plan-driven** if it
would still hold under a faithful re-implementation of the plan
(different attribute/column/config names, different internal modules);
**implementation-mirroring** otherwise. "Both" = the assertion is
plan-shaped but reaches past the public surface to observe it.

| File | Tests | Plan | Impl | Both | Notes |
|---|--:|--:|--:|--:|---|
| test_db.py | 14 | 11 | 1 | 2 | Schema itself is the plan; mostly unavoidable |
| test_search.py | 9 | 5 | 3 | 1 | `test_pipeline_has_four_named_…` etc. assert attribute names |
| test_web.py | 5 | 3 | 1 | 1 | `…service_is_swappable_on_the_app` reaches `app.config` |
| test_extract.py | 6 | 0 | 6 | 0 | Unit-level — appropriate for small internal helpers |
| test_quality.py | 7 | 0 | 7 | 0 | Unit-level — heuristic weights are implementation |
| test_sweep.py | 11 | 6 | 3 | 2 | `…rehashing` monkey-patches `_hash_file`; several `SELECT` to observe |
| test_step3a.py | 20 | 8 | 12 | 0 | Heavy `SELECT`-to-observe; UI is the public surface |
| test_capture.py | 12 | 6 | 5 | 1 | `app.config["ISBN_LOOKUP"] = …` reaches |
| test_bulk_import.py | 9 | 4 | 5 | 0 | Same config-reach pattern |
| test_isbn.py | 10 | 0 | 10 | 0 | Unit-level — checksum algorithm IS the spec |
| test_step4.py | 24 | 8 | 14 | 2 | Heavy `Rung` injection + `SELECT` to observe cache |

**Totals:** 127 tests · 51 plan · 67 impl · 9 both.
(About 40% plan-aligned. The implementation-mirrors are concentrated in
the orchestration layers — sweep, step3a, capture, step4 — exactly the
places the system-test layer below covers.)

## Disposition

- **Keep as-is** — unit tests of genuinely-internal helpers where a
  black-box view would be too coarse: `test_extract.py`, `test_quality.py`,
  `test_isbn.py`, most of `test_db.py`.
- **Replaced by `tests/system/`** — see this directory for the system
  tests that cover the orchestration-layer plan invariants. Existing
  impl-mirror tests in `test_search.py`, `test_web.py`, `test_step3a.py`,
  `test_capture.py`, `test_bulk_import.py`, `test_step4.py`, `test_sweep.py`
  remain as low-level regression coverage but are no longer the
  load-bearing tier for plan invariants.
- **Deleted** (impl-only, no plan value): listed in `DELETIONS.md` in
  this directory at the time the cleanup ran.

## Convention going forward

System tests live in `tests/system/`. Each file ties to one or more plan
sections, quoted at the top. Setup may use direct SQL to seed state where
no public endpoint can seed it (e.g. inserting FTS rows for search
tests), but **assertions go through the public surface only** — HTTP
routes, CLI commands, or top-level Python entry points. See the memory
note `system-tests-for-major-changes` for the rule.
