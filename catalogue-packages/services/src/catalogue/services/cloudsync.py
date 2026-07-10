"""kDrive (Infomaniak Drive) "Lite Sync" placeholder awareness.

When the library lives in a kDrive folder set to online-only, files on disk are
dehydrated PLACEHOLDERS: they report a real size but their content is all zeros until
the kDrive client downloads them. Reading the bytes directly (e.g. Flask `send_file`)
returns zeros — a blank PDF / empty EPUB — and a plain POSIX read does NOT trigger a
download (kDrive hydrates via its LiteSync system extension on a GUI open, not on a
server read). So the web app must DETECT these and tell the operator, rather than serve
emptiness.

kDrive marks each file with two xattrs:
  com.infomaniak.drive.desktopclient.litesync.status    'O' = online-only (dehydrated)
  com.infomaniak.drive.desktopclient.litesync.pinstate  'U' = unpinned (eviction allowed)
`request_download` is an EXPERIMENTAL trigger: it sets pinstate to the value Finder's
"Make available offline" uses, which makes kDrive download the file. The pinned value is
configured once it's been observed on a real pinned file (see PINSTATE_PINNED)."""
from __future__ import annotations

import os
import subprocess

STATUS_XATTR = "com.infomaniak.drive.desktopclient.litesync.status"
PINSTATE_XATTR = "com.infomaniak.drive.desktopclient.litesync.pinstate"

# Observed value of `status` for a dehydrated (content-not-downloaded) file.
_STATUS_ONLINE_ONLY = b"O"
# The `pinstate` byte that means "always keep on this device" (what triggers a download).
# Unknown until observed on a file pinned via Finder → set via `set_pinned_value` /
# configured here once known. None ⇒ request_download is a safe no-op.
PINSTATE_PINNED: bytes | None = None


def _getxattr(path: str, name: str):
    """Raw xattr bytes, or None. os.getxattr is LINUX-ONLY; on macOS (no os.getxattr)
    fall back to the `xattr` CLI (`-px` → hex)."""
    fn = getattr(os, "getxattr", None)
    if fn is not None:
        try:
            return fn(path, name)
        except OSError:
            return None
    try:
        r = subprocess.run(["xattr", "-px", name, path], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return bytes.fromhex("".join(r.stdout.split()))
    except Exception:
        return None


def _setxattr(path: str, name: str, value: bytes) -> bool:
    """Write an xattr. os.setxattr is LINUX-ONLY; on macOS use `xattr -wx` (hex)."""
    fn = getattr(os, "setxattr", None)
    if fn is not None:
        try:
            fn(path, name, value)
            return True
        except OSError:
            return False
    try:
        r = subprocess.run(["xattr", "-wx", name, value.hex(), path], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def is_online_only(path) -> bool:
    """True if `path` is a kDrive online-only placeholder (content not downloaded).

    Prefers kDrive's `status` xattr ('O'); falls back to the tell-tale of a dehydrated
    placeholder — a non-empty file whose first block is all zeros — when the xattr is
    absent (other OS / non-kDrive mounts simply return False)."""
    if not path:
        return False
    st = _getxattr(path, STATUS_XATTR)
    if st is not None:
        return st.strip() == _STATUS_ONLINE_ONLY
    try:
        if os.path.getsize(path) <= 0:
            return False
        with open(path, "rb") as f:
            head = f.read(4096)
        return bool(head) and not any(head)
    except OSError:
        return False


def is_fully_local(path) -> bool:
    """Cheap (no subprocess) check that a file's CONTENT is fully on local disk: its allocated
    blocks cover (most of) its logical size. A kDrive on-demand / partially-hydrated 'smart sync'
    file (one that returns real bytes but fetches each region on read) has far fewer allocated
    blocks than its size, so this is False for it — the signal to make a fully-local copy once so
    page-range reads don't stall. Errs toward True (serve directly, no copy) on any uncertainty,
    so an ordinary filesystem is never penalised. Distinct from `is_online_only`, which catches a
    fully-dehydrated placeholder that reads as zeros."""
    try:
        st = os.stat(path)
    except OSError:
        return True
    size = st.st_size
    if size <= 0:
        return True
    allocated = getattr(st, "st_blocks", None)   # 512-byte units; absent on some platforms
    if allocated is None:
        return True
    # Generous slack (50%) so only a clearly-under-allocated file (a dehydrated/partial cloud
    # placeholder) is treated as not-local; a normal file has blocks ≈ size (ratio ~1.0).
    return allocated * 512 >= size * 0.5


def request_download(path) -> bool:
    """EXPERIMENTAL: ask kDrive to download (hydrate) an online-only file by setting its
    pinstate to PINSTATE_PINNED — the same state Finder's "Make available offline" sets.
    Returns True if the xattr was written (not that the download finished). No-op (False)
    until PINSTATE_PINNED is known, or off-kDrive. Unofficial: kDrive may ignore an
    externally-set value, so callers must not assume success."""
    if PINSTATE_PINNED is None or not path:
        return False
    return _setxattr(path, PINSTATE_XATTR, PINSTATE_PINNED)


def pinstate(path):
    """Raw pinstate xattr bytes (or None) — for decoding the 'pinned' value off a file
    the operator has made available offline in Finder."""
    return _getxattr(path, PINSTATE_XATTR)
