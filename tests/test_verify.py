"""Tests for the pluggable verification pass (catalogue/verify.py) + bdrc helpers."""
from __future__ import annotations

import pytest

from catalogue.db_store import init_db, fold_key
from catalogue.services.work_canonical_resolver import ResolverResult
from catalogue.services import verify, bdrc


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "verify.db")
    yield conn
    conn.close()


class FakeResolver:
    """Canned resolver — no network. Used to drive BdrcVerifier in tests."""
    def __init__(self, persons=None, works=None):
        self.persons, self.works = persons or {}, works or {}

    def resolve_person(self, conn, text, scheme=None, *, offline=False):
        return self.persons.get(text)

    def resolve_work(self, conn, text, scheme=None, *, offline=False):
        return self.works.get(text)


def _bdrc(persons=None, works=None):
    return [verify.BdrcVerifier(resolver=FakeResolver(persons, works))]


def _person(db, name):
    pid = db.execute("INSERT INTO person (primary_name) VALUES (?)", (name,)).lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (pid, name, fold_key(name)))
    return pid


def _work(db, title):
    wid = db.execute("INSERT INTO work DEFAULT VALUES").lastrowid
    db.execute("INSERT INTO work_alias (work_id, text, scheme, normalized_key) "
               "VALUES (?, ?, 'english', ?)", (wid, title, fold_key(title)))
    return wid


# ── bdrc entity-type helpers ──────────────────────────────────────────────────
def test_bdrc_entity_type():
    assert bdrc.is_person_id("bdr:P4954")
    assert not bdrc.is_person_id("bdr:WA0RTI1253")
    assert bdrc.is_work_id("bdr:WA0RTI1253") and bdrc.is_work_id("bdr:MW123")
    assert not bdrc.is_work_id("bdr:P4954")
    assert bdrc.entity_type("bdr:G42") == "other" and bdrc.entity_type(None) is None


# ── BDRC person hits are PROVISIONAL → queued, never auto-applied ──────────────
# BLMP is a fuzzy name search (ranks works above persons, surfaces collaborators —
# the Sajjana/Nagarjuna trap), so a BDRC person hit must be human-confirmed. The
# precision auto-apply path is Wikidata (see test_custom_verifier_plugs_in and
# tests/test_person_status.py).
def test_bdrc_person_hit_is_queued_not_applied(db):
    pid = _person(db, "Nagarjuna")
    vs = _bdrc(persons={"Nagarjuna": ResolverResult(
        "Nāgārjuna", "bdrc", "bdr:P4954", ["Klu sgrub"], "bdrc")})
    assert verify.verify_person(db, vs, pid) == "candidate"
    # NOT auto-applied: external_id stays null, status stays provisional.
    row = db.execute("SELECT external_id, verification_status FROM person WHERE id=?",
                     (pid,)).fetchone()
    assert row == (None, "provisional")
    # A person_authority review item was enqueued with the candidate id.
    q = db.execute("SELECT payload_json FROM review_queue "
                   "WHERE item_type='person_authority'").fetchall()
    assert len(q) == 1 and "bdr:P4954" in q[0][0]


def test_bdrc_person_candidate_not_requeued(db):
    pid = _person(db, "Tsongkhapa")
    vs = _bdrc(persons={"Tsongkhapa": ResolverResult("Tsongkhapa", "bdrc", "bdr:P64", [], "bdrc")})
    assert verify.verify_person(db, vs, pid) == "candidate"
    assert verify.verify_person(db, vs, pid) == "candidate"   # second run
    assert db.execute("SELECT COUNT(*) FROM review_queue "
                      "WHERE item_type='person_authority'").fetchone()[0] == 1


def test_verify_work_attaches_canonical_number(db):
    wid = _work(db, "Heart Sutra")
    vs = _bdrc(works={"Heart Sutra": ResolverResult(
        "Prajñāpāramitāhṛdaya", "toh", "531", [], "84000")})
    assert verify.verify_work(db, vs, wid) == "matched"
    assert db.execute("SELECT canonical_system, canonical_number FROM work WHERE id=?",
                      (wid,)).fetchone() == ("toh", "531")


# ── precision guards (the WA0RTI1253 disaster) ────────────────────────────────
def test_person_rejects_wrong_entity_type(db):
    pid = _person(db, "Je Tsongkhapa, Gavin Kilty")
    vs = _bdrc(persons={"Je Tsongkhapa, Gavin Kilty": ResolverResult(
        "Some Work", "bdrc", "bdr:WA0RTI1253", [], "bdrc")})       # work id for a person
    assert verify.verify_person(db, vs, pid) == "unmatched"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] is None


def test_person_rejects_nonoverlapping_name(db):
    pid = _person(db, "Seventh Karmapa Choedrak Gyatso")
    vs = _bdrc(persons={"Seventh Karmapa Choedrak Gyatso": ResolverResult(
        "Completely Different Person", "bdrc", "bdr:P9999", [], "bdrc")})
    assert verify.verify_person(db, vs, pid) == "unmatched"


def test_work_rejects_person_id(db):
    wid = _work(db, "Heart Sutra")
    vs = _bdrc(works={"Heart Sutra": ResolverResult("Heart Sutra", "bdrc", "bdr:P8280", [], "bdrc")})
    assert verify.verify_work(db, vs, wid) == "unmatched"


# ── pluggability ──────────────────────────────────────────────────────────────
def test_custom_verifier_plugs_in(db):
    """Any object with name + verify() can be chained; first match wins."""
    class WikidataStub:
        name = "wikidata"
        def verify(self, db, kind, text):
            if kind == "person" and text == "Some Author":
                return verify.Match("Q12345", "wikidata", "Some Author", [], self.name)
            return None
    pid = _person(db, "Some Author")
    assert verify.verify_person(db, [WikidataStub()], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] == "Q12345"


def test_verifier_chain_order(db):
    """Verifiers are tried in order; the first to return a Match wins."""
    class Always:
        name = "always"
        def verify(self, db, kind, text):
            return verify.Match("FIRST", "x", text, [], self.name)
    class Never:
        name = "never"
        def verify(self, db, kind, text):
            return None
    pid = _person(db, "X")
    assert verify.verify_person(db, [Never(), Always()], pid) == "matched"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (pid,)).fetchone()[0] == "FIRST"


# ── purge ─────────────────────────────────────────────────────────────────────
def test_purge_clears_wrongtype_but_keeps_shared_ids(db):
    # A shared external_id is now a MERGE signal (person_dedup), NOT a false
    # positive — purge must leave it intact and only clear the wrong-type id.
    a = _person(db, "A"); b = _person(db, "B"); c = _person(db, "C")
    db.execute("UPDATE person SET external_id='wikidata:Q100' WHERE id=?", (a,))
    db.execute("UPDATE person SET external_id='wikidata:Q100' WHERE id=?", (b,))  # shared → keep
    db.execute("UPDATE person SET external_id='bdr:WA42' WHERE id=?", (c,))       # wrong type
    out = verify.purge_suspect_matches(db)
    assert out["person_wrongtype"] == 1
    assert "person_collision" not in out                       # collision-purge removed
    # both shared-id rows survive; only the wrong-type id is cleared
    assert db.execute("SELECT external_id FROM person WHERE id=?", (a,)).fetchone()[0] == "wikidata:Q100"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (b,)).fetchone()[0] == "wikidata:Q100"
    assert db.execute("SELECT external_id FROM person WHERE id=?", (c,)).fetchone()[0] is None


def test_purge_removes_verify_added_aliases(db):
    # _add_canonical_aliases wrote scheme='other' junk; purge drops it but keeps
    # the parent's seed alias.
    pid = _person(db, "Candrakirti")                 # seed alias scheme='english'
    for junk in ("śrīcakraśambarahomavidhi", "bodhipaddhati-nāma"):
        db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
                   "VALUES (?, ?, 'other', ?)", (pid, junk, fold_key(junk)))
    # a person whose ONLY alias is 'other' must keep it (no seed to fall back on)
    lone = db.execute("INSERT INTO person (primary_name) VALUES ('Lone')").lastrowid
    db.execute("INSERT INTO person_alias (person_id, text, scheme, normalized_key) "
               "VALUES (?, 'Lone', 'other', ?)", (lone, fold_key("Lone")))

    out = verify.purge_suspect_matches(db)
    assert out["person_aliases"] == 2
    schemes = {r[0] for r in db.execute("SELECT scheme FROM person_alias WHERE person_id=?", (pid,))}
    assert schemes == {"english"}                    # junk gone, seed kept
    assert db.execute("SELECT COUNT(*) FROM person_alias WHERE person_id=?", (lone,)).fetchone()[0] == 1


# ── offline safety ────────────────────────────────────────────────────────────
def test_offline_live_resolver_makes_no_network_and_caches_nothing(db):
    pid = _person(db, "Some Tibetan Author")
    vs = verify.default_verifiers(offline=True)        # real BdrcVerifier + LiveResolver
    assert verify.verify_person(db, vs, pid) == "unmatched"
    assert db.execute("SELECT COUNT(*) FROM resolver_cache").fetchone()[0] == 0


# ── BDRC ElasticSearch verifier (opt-in, --bdrc-over-blmp) ─────────────────────
class _FakeES:
    """Canned BdrcElasticSearch — returns prebuilt hits, no network."""
    def __init__(self, hits):
        self._hits = hits
    def person_search(self, name):
        return self._hits


def test_bdrc_es_exact_match_auto_binds():
    es = _FakeES([{"id": "bdr:P719", "score": 24.5,
                   "labels": ["paN chen blo bzang chos kyi rgyal mtshan/", "Panchen Lama 04"]}])
    m = verify.BdrcESVerifier(es=es).verify(None, "person", "paN chen blo bzang chos kyi rgyal mtshan")
    assert m is not None and m.number == "bdr:P719" and not m.provisional and m.verifier == "bdrc-es"


def test_bdrc_es_non_exact_returns_none():
    # top hit shares only a token — must NOT bind (the BLMP-noise lesson)
    es = _FakeES([{"id": "bdr:P9622", "score": 69.0, "labels": ["aschoff, jurgen c."]}])
    assert verify.BdrcESVerifier(es=es).verify(None, "person", "Losang Choephel Ganchenpa") is None


def test_bdrc_es_ambiguous_homonyms_return_none():
    es = _FakeES([{"id": "bdr:P1", "score": 30, "labels": ["Jeffrey Hopkins"]},
                  {"id": "bdr:P2", "score": 29, "labels": ["Jeffrey Hopkins"]}])
    assert verify.BdrcESVerifier(es=es).verify(None, "person", "Jeffrey Hopkins") is None


def test_bdrc_es_distinct_homonyms_refuse_even_with_one_bare_label():
    # The Gendün Gyatso case: a bare Tibetan personal name shared by several BDRC
    # persons. As long as the search surfaces ≥2 DISTINCT persons whose label exactly
    # matches the bare name, the verifier must refuse — never pick one namesake.
    es = _FakeES([
        {"id": "bdr:P1GS147791", "score": 187, "labels": ["yongs 'dzin dge 'dun rgya mtsho/",
                                                           "dge 'dun rgya mtsho/"]},
        {"id": "bdr:P4229", "score": 187, "labels": ["rdzi rgya dge 'dun rgya mtsho/",
                                                     "dge 'dun rgya mtsho/"]},
        {"id": "bdr:P84", "score": 187, "labels": ["tA la'i bla ma 02 dge 'dun rgya mtsho/"]},
    ])
    assert verify.BdrcESVerifier(es=es).verify(None, "person", "dge 'dun rgya mtsho") is None


def test_bdrc_es_same_person_multiple_label_rows_still_binds():
    # One person surfacing in two hit rows (same id) is NOT a homonym → still binds.
    es = _FakeES([
        {"id": "bdr:P7641", "score": 40, "labels": ["Kamalashila"]},
        {"id": "bdr:P7641", "score": 38, "labels": ["ka ma la shI la", "Kamalashila"]},
    ])
    m = verify.BdrcESVerifier(es=es).verify(None, "person", "Kamalashila")
    assert m is not None and m.number == "bdr:P7641"


def test_bdrc_es_search_size_large_enough_for_homonyms():
    # Regression for the truncation bug: the default ES client must request enough
    # hits that homonyms are visible to the guard (size=4 hid them).
    assert verify.BdrcESVerifier().es.size >= 20


def test_bdrc_es_offline_and_work_noop():
    es = _FakeES([{"id": "bdr:P719", "score": 9, "labels": ["Tenzin Gyatso"]}])
    assert verify.BdrcESVerifier(es=es, offline=True).verify(None, "person", "Tenzin Gyatso") is None
    assert verify.BdrcESVerifier(es=es).verify(None, "work", "Tenzin Gyatso") is None


def test_bdrc_es_swallows_transport_error():
    class Boom:
        def person_search(self, name): raise RuntimeError("network down")
    assert verify.BdrcESVerifier(es=Boom()).verify(None, "person", "X") is None


def test_default_verifiers_bdrc_over_blmp_swaps_person_path():
    default = verify.default_verifiers()
    assert any(isinstance(v, verify.BdrcVerifier) for v in default)
    assert not any(isinstance(v, verify.BdrcESVerifier) for v in default)
    swapped = verify.default_verifiers(bdrc_over_blmp=True)
    assert any(isinstance(v, verify.BdrcESVerifier) for v in swapped)
    blmp = [v for v in swapped if isinstance(v, verify.BdrcVerifier)]
    assert blmp and blmp[0].kinds == ("work",)          # BLMP kept for WORKS only


def test_bdrc_verifier_work_only_ignores_persons(db):
    v = verify.BdrcVerifier(resolver=FakeResolver(
        persons={"X": ResolverResult("X", "bdrc", "bdr:P4954", [], "bdrc")}),
        kinds=("work",))
    assert v.verify(db, "person", "X") is None


def test_bdrc_elasticsearch_parses_msearch_response():
    canned = {"responses": [{"hits": {"hits": [
        {"_id": "P719", "_score": 24.5, "_source": {
            "prefLabel_bo_x_ewts": ["paN chen blo bzang chos kyi rgyal mtshan/"],
            "altLabel_en": ["Panchen Lama 04"]}}]}}]}
    hits = bdrc.BdrcElasticSearch(transport=lambda body: canned).person_search("anything")
    assert hits[0]["id"] == "bdr:P719" and "Panchen Lama 04" in hits[0]["labels"]


# ── ingest-time scoped verify (verify_promotion / verify_persons / verify_works) ──
class _HardStub:
    """A verifier that returns a HARD (non-provisional) hit for a known name."""
    name = "stub"

    def __init__(self, table):
        self.table = table   # {query_name: Match}

    def verify(self, db, kind, text):
        return self.table.get(text)


def test_verify_promotion_binds_only_the_created_rows(db):
    # Two persons exist, but the "promotion" only created `made`.
    other = _person(db, "Unrelated Person")
    made = _person(db, "Atisha")
    db.commit()
    vs = [_HardStub({"Atisha": verify.Match("Q320150", "wikidata", "Atisha", [], "stub")})]

    class _Res:                       # duck-types promote.PromotionResult
        created_person_ids = [made]
        work_ids = []
    out = verify.verify_promotion(db, _Res(), verifiers=vs)
    assert out["person"]["matched"] == 1
    assert db.execute("SELECT external_id FROM person WHERE id=?", (made,)).fetchone()[0] \
        == "Q320150"
    # the row outside the promotion set is untouched
    assert db.execute("SELECT external_id FROM person WHERE id=?", (other,)).fetchone()[0] \
        is None


def test_verify_persons_scoped_to_ids(db):
    a = _person(db, "Known Author")
    b = _person(db, "Skip Me")
    db.commit()
    vs = [_HardStub({"Known Author": verify.Match("Q1", "wikidata", "Known Author", [], "stub")})]
    verify.verify_persons(db, [a], verifiers=vs)
    assert db.execute("SELECT external_id FROM person WHERE id=?", (a,)).fetchone()[0] == "Q1"
    assert db.execute("SELECT verification_status FROM person WHERE id=?", (b,)).fetchone()[0] \
        == "provisional"          # b was not in the id list, so never touched


def test_extensions_query_forms_superset_of_baseline(db):
    # INVARIANT: extensions must never DROP a query form baseline would have tried,
    # otherwise extensions can lose a match baseline made. Regression: "Sakya Pandita"
    # — extended honorific stripping reduces it to "Sakya" (clan/school, not the
    # person), and with no alias/translit form that was the ONLY query, so the exact
    # authority hit Q982008 baseline found was lost. The basic-stripped fallback fixes it.
    for name in ["Sakya Pandita", "Acharya Nagarjuna", "Geshe Sonam Rinchen",
                 "Tenzin Gyatso", "Jane Smith"]:
        pid = _person(db, name)
        db.commit()
        base = set(map(fold_key, verify._person_query_forms(db, pid, name, extensions=False)))
        ext = set(map(fold_key, verify._person_query_forms(db, pid, name, extensions=True)))
        assert base <= ext, f"extensions dropped a baseline form for {name!r}: {base - ext}"


def test_sakya_pandita_extensions_keeps_full_name_form(db):
    pid = _person(db, "Sakya Pandita")
    db.commit()
    forms = verify._person_query_forms(db, pid, "Sakya Pandita", extensions=True)
    assert "Sakya Pandita" in forms      # the un-over-stripped form survives
    assert "Sakya" in forms              # and the extended-stripped form is still tried
