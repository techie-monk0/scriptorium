"""The library's single source of readable book bytes.

Everything that wants to *read* a holding's file — the in-app reader, the OS-open
launcher, the cover / first-page renderers — goes through `BookFileService`. It
answers one question: "give me a local path whose REAL bytes are on disk, ready
to serve." Whether those bytes were already local, or are a kDrive online-only
placeholder that had to be pulled from the cloud over WebDAV into a cache, is an
implementation detail that lives *below* this layer. Callers above it never touch
`cloudsync`, `webdav`, or the cache directory directly.

The service is deliberately framework-agnostic — it returns a plain `Resolution`
value object, never a Flask response — so the web layer (and any CLI) can decide
how to present each outcome.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from catalogue.services import cloudsync as cloudsync_mod
from catalogue.services import perf
from catalogue.services import webdav as webdav_mod


class Status(Enum):
    READY = "ready"                  # real bytes are on disk at `path`; serve it
    NOT_DOWNLOADED = "not_downloaded"  # cloud-only placeholder; bytes unavailable now
    MISSING = "missing"              # no such holding, no path, or file is gone


@dataclass(frozen=True)
class Resolution:
    """What the service hands back to its caller. Use the `is_*` flags to branch;
    `path` is meaningful only when `is_ready`."""

    status: Status
    path: str | None = None
    download_name: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status is Status.READY

    @property
    def is_not_downloaded(self) -> bool:
        return self.status is Status.NOT_DOWNLOADED

    @property
    def is_missing(self) -> bool:
        return self.status is Status.MISSING


class BookFileService:
    """Resolves a holding (or a raw stored path) to readable local bytes.

    Construct once with the WebDAV cache directory; pass a live DB connection per
    call so it stays request-scoped and never holds its own handle.
    """

    def __init__(self, webdav_cache: str):
        self._cache = webdav_cache

    # ── Path bookkeeping ──────────────────────────────────────────────────
    def stored_path(self, db, hid: int) -> str | None:
        """The absolute path recorded for a holding (original file, else archival
        PDF), or None if the holding/file doesn't exist on disk. Stored paths are
        often repo-root-relative; we resolve to absolute so both the existence
        check here and the eventual send agree on the same file."""
        from catalogue.access_api import system_conn
        row = system_conn(db).holdings.reads.paths_of(hid)
        if not row:
            return None
        path = row[0] or row[1]
        if not path:
            return None
        path = os.path.abspath(path)
        return path if os.path.exists(path) else None

    # ── The main entry point ──────────────────────────────────────────────
    def resolve(self, db, hid: int) -> Resolution:
        """Resolve a holding to something servable. A cloud online-only placeholder
        is transparently pulled to the local cache first; only if that fetch is
        impossible (WebDAV unconfigured / offline) do we report NOT_DOWNLOADED."""
        path = self.stored_path(db, hid)
        if not path:
            return Resolution(Status.MISSING)
        return self.resolve_path(path)

    def resolve_path(self, path: str) -> Resolution:
        """Resolve an already-known absolute path (used by the guarded reconcile
        viewer, which addresses pending files by path rather than holding id)."""
        if not os.path.exists(path):
            return Resolution(Status.MISSING)
        # Cloud online-only placeholder (kDrive Lite Sync &c.): the local bytes are
        # all zeros until downloaded, so serving it directly streams a blank PDF /
        # empty EPUB. Pull the REAL bytes over WebDAV into the cache and serve those.
        online = perf.timed(lambda: cloudsync_mod.is_online_only(path), "is_online_only (xattr)")
        if online:
            perf.log(f"online-only placeholder → WebDAV fetch: {path}")
            cached = perf.timed(lambda: webdav_mod.fetch_to_cache(path, self._cache),
                                "fetch_to_cache (WebDAV)")
            if not cached:
                return Resolution(Status.NOT_DOWNLOADED)
            return Resolution(Status.READY, path=cached,
                              download_name=os.path.basename(path))
        # On-demand / partially-hydrated cloud file (kDrive 'smart sync'): reads return real bytes
        # but each region is fetched on access, so serving page-range reads off the original stalls
        # ~1s/range. Pull it fully local ONCE and serve the cache copy. Already-local files (fully
        # allocated) are served directly — no redundant copy (we never duplicate a downloaded file).
        local = cloudsync_mod.is_fully_local(path)
        perf.log(f"is_fully_local={local}: {path}")
        if not local:
            perf.log("not fully local → copying to cache once (kDrive on-demand fix)")
            cached = perf.timed(lambda: webdav_mod.copy_to_cache(path, self._cache),
                                "copy_to_cache (full local read)")
            if cached:
                return Resolution(Status.READY, path=cached,
                                  download_name=os.path.basename(path))
        return Resolution(Status.READY, path=path)

    # ── Read-only probe (no fetch) ────────────────────────────────────────
    def readable_now(self, path: str | None) -> str | None:
        """A path whose REAL bytes are on disk *right now* with no download: the
        hydrated original, else a previously-fetched WebDAV cache copy, else None.
        For callers (cover/first-page render) that must never trigger a fetch."""
        if not path:
            return None
        if os.path.exists(path) and not cloudsync_mod.is_online_only(path):
            return path
        return webdav_mod.cached_file(path, self._cache)
