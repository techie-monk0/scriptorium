"""Storage provider seam — the ONE place a cloud backend (kDrive, …) is known.

The rest of the app depends on the abstract `StoragePort`, never on a concrete provider.
A provider answers, for a local library path:

  • can I serve/locate this file?            covers()
  • give me a CLIENT-OPENABLE reference      locator()  -> StorageRef
        a provider-relative path (so a NATIVE client can open via its Files provider)
        AND an opaque deep-link URL (for a web/PWA handoff). Either may be None.
  • (optional, for the eventual BookFileService migration) raw bytes / placeholder?

Swapping kDrive for another cloud = one new `StoragePort` implementation; NO client code
and NO export code change (they speak the neutral `StorageRef`). A provider is the ONLY
module allowed to know a provider's URL shape — exactly as `cloudsync` is the only module
that knows kDrive's xattr names. The deep-link template lives here as an overridable
constant, never as a literal in business or client code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from . import cloudsync as _cloudsync
from . import webdav as _webdav


@dataclass(frozen=True)
class StorageRef:
    """A client-openable reference to a file, provider-neutral on the wire.

    `relpath`  — provider-relative path; lets a NATIVE client open via its Files provider.
    `open_url` — opaque deep link for a web/PWA handoff (a tap that opens the file with no
                 Mac involved). May be None until a `FileIdResolver` is wired; the client
                 then falls back to streaming from the server.
    The client treats BOTH as opaque — it must not parse them (that's the provider seam)."""
    provider: str
    relpath: Optional[str] = None
    open_url: Optional[str] = None

    def as_dict(self) -> dict:
        return {"provider": self.provider, "relpath": self.relpath, "open_url": self.open_url}


# ── Abstraction (what the app depends on) ──────────────────────────────────────
class FileIdResolver(ABC):
    """Resolves a provider-relative path to the numeric ids a deep link needs. Swappable
    by mechanism (kDrive REST API / WebDAV PROPFIND / xattr) — isolating HOW an id is found
    from the URL SHAPE the provider owns. Returns None when it can't resolve (→ no open_url)."""

    @abstractmethod
    def resolve(self, relpath: str, *, drive_id: str) -> Optional[tuple]:
        """-> (file_id, dir_id, kind) for `relpath`, or None when unresolvable."""


class NullFileIdResolver(FileIdResolver):
    """Default until a real resolver lands: never resolves, so `open_url` stays None and the
    client falls back to streaming. Keeps the rest of the seam shippable today."""

    def resolve(self, relpath: str, *, drive_id: str) -> Optional[tuple]:
        return None


class StoragePort(ABC):
    """The abstract storage backend the app depends on. Concrete providers (kDrive, …)
    implement `covers`/`locator`; the byte/placeholder methods default to the existing
    module functions so a provider need not reimplement them (used by the future
    BookFileService migration, not by the replica export)."""

    name: str = "storage"

    @abstractmethod
    def covers(self, local_path: str) -> bool:
        """True if this provider backs `local_path` (i.e. can locate/serve it)."""

    @abstractmethod
    def locator(self, local_path: str) -> Optional[StorageRef]:
        """A client-openable `StorageRef` for `local_path`, or None if not covered."""

    def fetch_bytes(self, local_path: str) -> Optional[bytes]:
        return _webdav.fetch_local(local_path)

    def is_placeholder(self, local_path: str) -> bool:
        return _cloudsync.is_online_only(local_path)


# ── kDrive implementation (the ONLY module that knows kDrive's open-URL shape) ──
class KDriveProvider(StoragePort):
    """Infomaniak kDrive provider. Builds a Mac-independent open link from config + a
    per-file id resolved by a swappable `FileIdResolver`. `DEFAULT_BASE`/`OPEN_TEMPLATE`
    are provider constants (overridable via config) — the single sanctioned home for
    kDrive's link shape, not business-logic hardcoding."""

    name = "kdrive"
    DEFAULT_BASE = "https://ksuite.infomaniak.com"
    # Verified portable on Mac + phone (2026-06-17): the `all` context works on both.
    OPEN_TEMPLATE = ("{base}/all/kdrive/app/drive/{drive_id}"
                     "/files/{dir_id}/preview/{kind}/{file_id}")

    def __init__(self, mount, *, drive_id: Optional[str], base: Optional[str] = None,
                 template: Optional[str] = None, resolver: Optional[FileIdResolver] = None):
        self._mount = mount
        self._drive_id = drive_id
        self._base = (base or self.DEFAULT_BASE).rstrip("/")
        self._template = template or self.OPEN_TEMPLATE
        self._resolver = resolver or NullFileIdResolver()

    def covers(self, local_path: str) -> bool:
        return bool(self._mount) and self._mount.covers(local_path)

    def locator(self, local_path: str) -> Optional[StorageRef]:
        if not self.covers(local_path):
            return None
        relpath = self._mount.remote_path_for(local_path)
        return StorageRef(self.name, relpath=relpath, open_url=self._open_url(relpath))

    def _open_url(self, relpath: Optional[str]) -> Optional[str]:
        if not (relpath and self._drive_id):
            return None
        ids = self._resolver.resolve(relpath, drive_id=self._drive_id)
        if not ids:
            return None
        file_id, dir_id, kind = ids
        return self._template.format(base=self._base, drive_id=self._drive_id,
                                     dir_id=dir_id, kind=kind, file_id=file_id)

    def fetch_bytes(self, local_path: str) -> Optional[bytes]:
        if not self.covers(local_path):
            return None
        try:
            return self._mount.fetch(local_path)
        except _webdav.WebDAVError:
            return None


# ── Service surface (provider-agnostic factory) ────────────────────────────────
def _drive_id_from_config(settings_path: Optional[str] = None) -> Optional[str]:
    """kDrive numeric drive id: explicit `KDRIVE_DRIVE_ID`, else parsed from the WebDAV
    host (`https://2451995.connect.kdrive…` -> `2451995`)."""
    s = _webdav._read_settings(settings_path)
    explicit = _webdav._cfg("KDRIVE_DRIVE_ID", s)
    if explicit:
        return explicit
    host = (_webdav._cfg("KDRIVE_WEBDAV_URL", s) or "").split("://")[-1].split("/")[0]
    head = host.split(".")[0]
    return head if head.isdigit() else None


def default_provider(*, settings_path: Optional[str] = None,
                     resolver: Optional[FileIdResolver] = None) -> Optional[StoragePort]:
    """The configured storage provider, or None when no cloud is configured (callers no-op).
    Today: kDrive from the WebDAV mount + drive id, with a `NullFileIdResolver` until a real
    resolver is wired (so `open_url` is None and clients fall back to streaming)."""
    mounts = _webdav.load_mounts()
    if not mounts:
        return None
    s = _webdav._read_settings(settings_path)
    return KDriveProvider(
        mounts[0], drive_id=_drive_id_from_config(settings_path),
        base=_webdav._cfg("KDRIVE_OPEN_URL_BASE", s),
        template=_webdav._cfg("KDRIVE_OPEN_URL_TEMPLATE", s),
        resolver=resolver,
    )
