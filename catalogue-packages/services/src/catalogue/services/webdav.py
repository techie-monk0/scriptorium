"""Generic WebDAV fetch + local→remote path mapping.

Pulls the REAL bytes of a file from a WebDAV server when the local copy is only a
cloud-sync placeholder (e.g. kDrive Lite Sync online-only stubs read as zeros). The
client is provider-agnostic — plain HTTP GET + Basic auth, which every standard WebDAV
server speaks (Infomaniak kDrive, Nextcloud, ownCloud, Apache mod_dav, …). A `Mount`
maps a local directory subtree onto one server + remote sub-path, so a local absolute
path resolves to a remote URL. Infomaniak kDrive is the first configured provider
(`load_mounts`), but adding another is just another Mount in the config.

Credentials never live in code or the committed config: they come from env vars or the
git-ignored `.kdrive_settings` (a `export KEY=VALUE` shell file). Failure mode mirrors
isbn.py — return None / raise a single WebDAVError the caller catches; never crash a
request."""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# Injectable HTTP for tests: (Request, timeout) -> bytes. Default hits the network.
OpenerFn = Callable[[urllib.request.Request, float], bytes]

SETTINGS_FILE = os.environ.get("KDRIVE_SETTINGS_FILE", ".kdrive_settings")


class WebDAVError(RuntimeError):
    """Any WebDAV fetch failure (network, auth, 404). Caught by the None-returning
    wrappers so a placeholder file degrades to the 'not downloaded' message, never a 500."""


def _default_opener(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class WebDAVClient:
    """A standard WebDAV server reachable at `base_url`, with optional Basic auth."""

    def __init__(self, base_url: str, *, user: Optional[str] = None,
                 password: Optional[str] = None, opener: Optional[OpenerFn] = None,
                 timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self._user, self._password = user, password
        self._opener = opener or _default_opener
        self.timeout = timeout

    def _url(self, remote_path: str) -> str:
        # Percent-encode each segment (keep '/'); WebDAV paths can hold spaces & unicode.
        quoted = urllib.parse.quote(remote_path.lstrip("/"), safe="/")
        return f"{self.base_url}/{quoted}"

    def _request(self, remote_path: str, method: str = "GET") -> urllib.request.Request:
        req = urllib.request.Request(self._url(remote_path), method=method)
        if self._user is not None:
            tok = base64.b64encode(f"{self._user}:{self._password or ''}".encode()).decode()
            req.add_header("Authorization", f"Basic {tok}")
        return req

    def fetch(self, remote_path: str) -> bytes:
        """GET the file's bytes. Raises WebDAVError on any failure."""
        try:
            return self._opener(self._request(remote_path), self.timeout)
        except urllib.error.HTTPError as e:
            raise WebDAVError(f"{e.code} fetching {remote_path}") from e
        except Exception as e:
            raise WebDAVError(f"fetch failed for {remote_path}: {e}") from e


class Mount:
    """Maps a local directory subtree onto a WebDAV server. `local_root` is the on-disk
    folder that corresponds to the server's `remote_root` (default = server root)."""

    def __init__(self, local_root: str, client: WebDAVClient, *, remote_root: str = "",
                 name: str = "webdav"):
        self.local_root = os.path.normpath(local_root)
        self.client = client
        self.remote_root = remote_root.strip("/")
        self.name = name

    def covers(self, local_path: str) -> bool:
        try:
            np = os.path.normpath(local_path)
        except Exception:
            return False
        return np == self.local_root or np.startswith(self.local_root + os.sep)

    def remote_path_for(self, local_path: str) -> Optional[str]:
        if not self.covers(local_path):
            return None
        rel = os.path.relpath(os.path.normpath(local_path), self.local_root)
        rel = "/".join(rel.split(os.sep))                       # OS sep → URL sep
        return f"{self.remote_root}/{rel}" if self.remote_root else rel

    def fetch(self, local_path: str) -> bytes:
        rp = self.remote_path_for(local_path)
        if rp is None:
            raise WebDAVError(f"{local_path} is not under mount {self.name} ({self.local_root})")
        return self.client.fetch(rp)


# ── configuration ──────────────────────────────────────────────────────────────
def _read_settings(path: str = None) -> dict:
    """Parse a `export KEY=VALUE` shell file (the git-ignored .kdrive_settings) into a
    dict. Missing file → empty. Quotes stripped; comments/blank lines ignored."""
    out: dict = {}
    try:
        for line in Path(path or SETTINGS_FILE).read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _cfg(key: str, settings: dict) -> Optional[str]:
    """env wins over the settings file (so a shell that sourced it still overrides), then
    the shared secret store (api_key.txt) so kDrive creds can live in the one file too."""
    if os.environ.get(key) or settings.get(key):
        return os.environ.get(key) or settings.get(key)
    try:
        from . import apikeys
        return apikeys.get(key)
    except Exception:
        return None


def load_mounts(*, opener: Optional[OpenerFn] = None, settings_path: str = None) -> list:
    """Build the configured WebDAV mounts. Currently the Infomaniak kDrive mount from
    KDRIVE_WEBDAV_URL/USER/PASS (+ KDRIVE_LOCAL_ROOT, the local folder mapped to the
    WebDAV root). Returns [] when unconfigured, so callers no-op cleanly."""
    s = _read_settings(settings_path)
    url = _cfg("KDRIVE_WEBDAV_URL", s)
    local_root = _cfg("KDRIVE_LOCAL_ROOT", s)
    if not url or not local_root:
        return []
    client = WebDAVClient(url, user=_cfg("KDRIVE_WEBDAV_USER", s),
                          password=_cfg("KDRIVE_WEBDAV_PASS", s), opener=opener)
    remote_root = _cfg("KDRIVE_REMOTE_ROOT", s) or ""
    return [Mount(local_root, client, remote_root=remote_root, name="kdrive")]


def fetch_local(local_path: str, *, mounts: list = None,
                opener: Optional[OpenerFn] = None) -> Optional[bytes]:
    """The bytes for a local path, fetched from whichever mount covers it. None if no
    mount covers it or the fetch fails (logged-as-None, never raises)."""
    for m in (mounts if mounts is not None else load_mounts(opener=opener)):
        if m.covers(local_path):
            try:
                return m.fetch(local_path)
            except WebDAVError:
                return None
    return None


def cache_target(local_path: str, cache_dir: str) -> str:
    """The cache file path a `local_path` maps to (whether or not it exists yet)."""
    key = hashlib.sha256(os.path.normpath(local_path).encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_dir, key + os.path.splitext(local_path)[1])


def cached_file(local_path: str, cache_dir: str) -> Optional[str]:
    """The already-downloaded cache file for `local_path`, or None — a cheap existence
    check (no fetch). Lets callers read a file the viewer previously downloaded."""
    p = cache_target(local_path, cache_dir)
    return p if os.path.exists(p) and os.path.getsize(p) > 0 else None


# One lock per cache target so a burst of reader range requests for the same file triggers a
# SINGLE copy (the others wait, then find it cached) instead of N concurrent full reads.
_copy_locks_guard = threading.Lock()
_copy_locks: "dict[str, threading.Lock]" = {}


def _copy_lock(dest: str) -> threading.Lock:
    with _copy_locks_guard:
        lk = _copy_locks.get(dest)
        if lk is None:
            lk = _copy_locks[dest] = threading.Lock()
        return lk


def copy_to_cache(local_path: str, cache_dir: str, *, chunk: int = 1 << 20) -> Optional[str]:
    """Stream a LOCAL file's real bytes into the cache once (reused on later calls), returning the
    cache path. Unlike `fetch_to_cache` (which pulls a zero-placeholder over WebDAV), this reads
    the local file directly — for an on-demand / partially-hydrated cloud file that returns real
    bytes but fetches each region on read, the full sequential read pulls it all local in one pass,
    so subsequent page-range serves come from the fully-local cache copy and don't stall. Returns
    None on failure (caller falls back to the original path)."""
    dest = cache_target(local_path, cache_dir)
    with _copy_lock(dest):
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = dest + ".part"
            with open(local_path, "rb") as src, open(tmp, "wb") as out:
                while True:
                    b = src.read(chunk)
                    if not b:
                        break
                    out.write(b)
            if os.path.getsize(tmp) <= 0:
                os.unlink(tmp)
                return None
            os.replace(tmp, dest)
            return dest
        except OSError:
            return None


def fetch_to_cache(local_path: str, cache_dir: str, *, mounts: list = None,
                   opener: Optional[OpenerFn] = None) -> Optional[str]:
    """Fetch `local_path`'s real bytes once into `cache_dir` and return the cached file
    path (reused on later calls). Lets the viewer serve big PDFs via send_file (range
    requests) and the re-hash read real content. None if it can't be fetched."""
    dest = cache_target(local_path, cache_dir)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    data = fetch_local(local_path, mounts=mounts, opener=opener)
    if not data:
        return None
    os.makedirs(cache_dir, exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return dest
