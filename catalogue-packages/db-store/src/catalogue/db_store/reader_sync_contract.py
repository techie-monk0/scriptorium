"""The catalogue's reader-sync contract — a *versioned, language-neutral descriptor* of the
GET/POST ``/sync/reader`` wire surface (bookmarks + annotations) that every reader client speaks.

Like the external read-contract (``external_contract.py``), this exists so consumers stay consistent
with the catalogue WITHOUT being coupled to its code. Today three clients encode the same wire shape
independently — the web reader, the PWA, and the native app's ``postilla`` ``AnnotationStore``
adapter. This makes that shape a single published artifact instead of a docstring:

  * ``reader_sync_contract.json`` — the machine-readable spec of what each version guarantees
    (endpoints, the pull-row records, the push ops, cursor/auth/conflict semantics); and
  * the API advertises the running version (see ``api_version_payload``), so a client learns the
    version the *live* server provides and asserts it is >= the version it was built for.

A consumer's handshake is a few lines it owns (no import of this module): read the advertised
version, assert compatibility, and rely on the declared fields. This module is the *provider* side —
``verify()`` proves the running store code actually honours the published descriptor (every declared
record field exists on its dataclass; every declared op field is a real ``apply_*`` parameter), so
the catalogue can never ship a descriptor that lies. See docs/frontend/sync_architecture.md.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

CONTRACT_PATH = Path(__file__).parent / "reader_sync_contract.json"


def descriptor() -> dict:
    """The published contract descriptor (parsed ``reader_sync_contract.json``)."""
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


CONTRACT_VERSION: int = descriptor()["version"]


def api_version_payload() -> dict:
    """The bit the API mixes into its /sync/reader responses (or a /sync/reader/contract endpoint)
    so clients can read the live wire-contract version: ``{"contract_version": N}``."""
    return {"contract_version": CONTRACT_VERSION}


def _dataclass_fields(dotted: str) -> "set[str]":
    """Field names of a ``module.Class`` dataclass referenced by the descriptor's ``source``."""
    module_name, _, cls_name = dotted.rpartition(".")
    mod = __import__(module_name, fromlist=[cls_name])
    cls = getattr(mod, cls_name)
    return {f.name for f in dataclasses.fields(cls)}


def _method_params(dotted: str) -> "set[str]":
    """Parameter names of a ``module.Class.method`` referenced by the descriptor's ``applies_via``."""
    module_name, cls_name, meth_name = dotted.rsplit(".", 2)
    mod = __import__(module_name, fromlist=[cls_name])
    meth = getattr(getattr(mod, cls_name), meth_name)
    return {p for p in inspect.signature(meth).parameters if p != "self"}


def verify() -> "list[str]":
    """Provider-side truthfulness check: does the running store code honour the published descriptor?

    Confirms (a) every field the descriptor declares for each pull-row *record* really exists on its
    ``reader_state`` dataclass, and (b) every field each push *op* declares is a real parameter of the
    ``apply_*`` method that applies it. This closes the gap a docstring leaves open: a future edit
    that drops ``ink`` from ``Annotation`` — or renames ``apply_annotation``'s ``cfi_range`` param —
    fails loudly here instead of silently breaking every reader client. Returns human-readable
    mismatches; an empty list means conformant."""
    d = descriptor()
    problems: list[str] = []

    for name, spec in d.get("records", {}).items():
        try:
            have = _dataclass_fields(spec["source"])
        except (ImportError, AttributeError, TypeError) as exc:
            problems.append(f"record {name!r}: cannot resolve source {spec.get('source')!r} ({exc})")
            continue
        missing = [f for f in spec.get("fields", {}) if f not in have]
        if missing:
            problems.append(f"record {name!r} ({spec['source']}) is missing declared fields: {missing}")

    for name, spec in d.get("ops", {}).items():
        try:
            have = _method_params(spec["applies_via"])
        except (ImportError, AttributeError, ValueError) as exc:
            problems.append(f"op {name!r}: cannot resolve applies_via {spec.get('applies_via')!r} ({exc})")
            continue
        # `type`/`id` are wire-level (the discriminator + identity); the rest must be real params.
        wire_only = {"type"}
        missing = [f for f in spec.get("fields", []) if f not in have and f not in wire_only]
        if missing:
            problems.append(
                f"op {name!r} ({spec['applies_via']}) declares fields with no matching parameter: {missing}")

    return problems
