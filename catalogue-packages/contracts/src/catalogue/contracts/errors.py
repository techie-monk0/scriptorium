"""Error taxonomy — shared across package + client/server boundaries.

Every access-API failure is one of these, each with a stable `http_status` so the webui,
the PWA, the CLI, and a future Swift client all map failures the same way. No behavior
beyond carrying the status. See docs/access/entity_api_model.md §4.
"""
from __future__ import annotations


class CatalogueError(Exception):
    """Base for every access-API error. Subclasses set the HTTP mapping for clients."""
    http_status: int = 500


class NotFound(CatalogueError):
    """A referenced entity does not exist (or was deleted)."""
    http_status = 404


class ValidationError(CatalogueError):
    """Input failed validation/normalization (bad ISBN, missing required field, …)."""
    http_status = 422


class Conflict(CatalogueError):
    """The write conflicts with current state."""
    http_status = 409


class IntegrityViolation(Conflict):
    """A write would leave the graph inconsistent (dangling ref, illegal orphan, …).
    Raised by the IntegrityGate as a precondition of `apply`."""


class StaleWrite(Conflict):
    """Optimistic-concurrency lost-update: the row's `rev` changed since the plan was
    built. The caller should re-read and retry."""
