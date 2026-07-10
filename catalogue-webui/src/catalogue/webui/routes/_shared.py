"""Leaf helpers shared across more than one route module.

Kept deliberately tiny: only the lookups that two otherwise-independent areas
both need live here, so neither has to import the other. Each reads the
request-scoped DB handle off `flask.g`, matching the closures they replaced.
"""
from __future__ import annotations

import json

from flask import g


def _acc(db=None):
    """A system Access over the request DB handle (defaults to `g.db`) — engine-routed reads/writes
    for the webui routes. Read paths run live; writes stage on the connection and the route commits."""
    from catalogue.access_api import system_conn
    return system_conn(db if db is not None else g.db)


class AppContext:
    """Carrier for the handful of helpers that cross route-module boundaries.

    `create_app` builds one and threads it through every `register(app, ctx)`.
    A module that *produces* a cross-area helper assigns it as an attribute
    (e.g. `ctx.capture_one_json = ...`); a module that *consumes* one reads it.
    Producers are registered before their consumers (see `create_app`).
    """

    # Capture's JSON ingest path, reused by the PWA `/api/v1/capture` endpoint.
    capture_one_json = None


def person_name(pid: int) -> str:
    """A person's primary name, or a stable `person #N` placeholder."""
    p = _acc().persons.reads.get(pid)
    return p.primary_name if p else f"person #{pid}"


def work_title(wid: int) -> str:
    """A work's display title: prefer the English alias, else ANY alias (a
    Tibetan/Sanskrit-only work has no English yet — show its native title rather
    than 'work #N')."""
    return _acc().works.reads.alias_title(wid) or f"work #{wid}"


def review_backlog_counts(db) -> dict:
    """Per-tab Review backlog counts, shared by the home hub badge and the Review
    tab strip so they never drift. **Books = every edition with an UNAPPLIED
    detection, single-work AND multi-work** (one work_detection row per edition);
    Works/People/Subjects come from their own queues."""
    from catalogue.services import work_review as WR, picker as P, subjects as S
    books = sum(1 for pj in _acc(db).editions.reads.detection_payloads()
                if not json.loads(pj).get("applied"))
    return {"books": books, "works": WR.count_incomplete(db),
            "people": P.count_unresolved(db, "person"),
            "subjects": S.count_uncurated(db)}


def authority_url(ext_id):
    """Map a namespaced authority id to its public web page, for click-through on
    the review card and person view. Unknown/blank → None."""
    if not ext_id:
        return None
    if ext_id.startswith("bdr:"):
        return f"https://purl.bdrc.io/resource/{ext_id.split(':', 1)[1]}"
    if ext_id.startswith("wikidata:"):
        return f"https://www.wikidata.org/wiki/{ext_id.split(':', 1)[1]}"
    if ext_id.startswith("viaf:"):
        return f"https://viaf.org/viaf/{ext_id.split(':', 1)[1]}"
    if ext_id.startswith("dila:"):
        return f"https://authority.dila.edu.tw/person/?fromInner={ext_id.split(':', 1)[1]}"
    return None
