"""Per-concern DB access API modules.

Each module here is the *single* access layer for one bounded slice of the
schema: it owns that slice's vocabulary and invariants behind a small typed
surface, owns its own DDL (an idempotent `ensure_schema`), returns frozen
dataclasses, and commits nothing — the caller owns the transaction. No code
outside a module touches that module's tables directly.

This keeps each concern's supersede/repoint/validate logic in one place and lets
clients depend on a narrow, typed API instead of hand-written SQL.

Modules:
  * scan_ocr — scan / OCR digitization provenance (see
    docs/access/scan_ocr_provenance_model.md).
  * external_deps — external-tool dependencies (the "flag" + purge-guard + resolve/supersede
    forwarding) so a consumed edition is un-deletable (see
    docs/access/external_tool_dependency_contract.md).
  * tool_policy — the capability/restriction executor + `ToolRegistry` that turns a Capability on
    an edition into the combined Restriction its dependent tools impose (impls in integrations/).

The package __init__ deliberately does NOT eagerly import submodules: each owns
distinct tables, so clients import only the slice they need —
`from catalogue.access_api import scan_ocr`.

The gateway (`bind`/`Access`) is the entry point clients use; per-concern slices like
`scan_ocr` stay lazy submodules so importing the package doesn't pull every concern.
"""
from .gateway import Access, bind, bind_conn, system_access, system_conn

__all__ = ["Access", "bind", "bind_conn", "system_access", "system_conn"]
