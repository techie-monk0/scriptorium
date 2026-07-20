"""The server build stamp + staleness check — the server half of the app-version handshake.

Why this exists (the bug it prevents): a long-running server process keeps serving CODE and
CACHED TEMPLATES it loaded at startup, even after the files on disk change. A browser, meanwhile,
refetches changed static JS/CSS (the `static_v` cache-buster). So a stale process can hand a client
a MISMATCHED pair — a cached old template wired to fresh JS — and the page silently breaks (this is
exactly how a scanned-PDF reader hung at "Downloading… X/X": the process served the old range-mode
template against the new whole-file engine).

The fix is a version handshake every client already knows how to do (it mirrors the `reader_sync`
contract): the server advertises

  * ``app_build``   — a fingerprint of the code+assets THIS PROCESS is running (stable for the life
                      of the process; changes only when a differently-built process starts). Stamped
                      into every page and returned by ``GET /version`` + ``GET /api/v1/health``. A
                      client compares the build it loaded with against the live one; a difference
                      means the server was restarted with new code, so the open page should reload.
  * ``server_stale`` — True when the running process is behind its OWN code on disk (a ``.py`` file
                      changed since the process started, so a restart is needed for it to take
                      effect). The web app refuses to serve pages while this is True (see
                      ``web.py``'s staleness gate) so a stale server can't serve clients.

"Requires a restart" is scoped to Python source. Templates are served fresh (``TEMPLATES_AUTO_RELOAD``)
and static assets are cache-busted (``static_v``), so changing those does NOT need a restart and does
NOT flip ``server_stale``; only a code change does — which is what "a feature that requires a restart"
means, and it bumps the version automatically because the fingerprint is derived from the files.

## Technical details

The fingerprint is a short SHA-256 over each tracked file's repo-relative path + ``st_mtime_ns``,
sorted for stability. mtime (not content) is deliberate: it captures "the bytes on disk changed since
this process started" cheaply, which is precisely the restart trigger. It is per-process/per-machine,
not reproducible across clones — that is fine, the handshake only ever compares values produced by the
SAME running server. The on-disk fingerprint is recomputed at most every ``ttl`` seconds (a
``before_request`` on every hit must stay cheap). ``__pycache__`` and vendored assets are skipped.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

# Files whose change needs a process RESTART to take effect: Python code AND the data files read into
# module globals at import (the versioned contract descriptors, palette.json, …). Templates + static
# JS/CSS are excluded on purpose — they update live via TEMPLATES_AUTO_RELOAD + static_v.
RESTART_EXTS = (".py", ".json")
# The whole served build — identifies which build a process is running (for the client reload signal).
SOURCE_EXTS = (".py", ".html", ".js", ".css", ".json")
# Directories never worth fingerprinting (bytecode caches; large third-party bundles that carry their
# own version and are cache-busted via static_v).
EXCLUDE_DIRS = ("__pycache__", "vendor")


class BuildStamp:
    """A build fingerprint + staleness check over one or more source roots.

    Instantiated once at import for the running server (``DEFAULT`` below), but it takes its roots as
    an argument so tests — and any other client of this abstract layer — can point it at a temp tree
    and exercise the real logic. ``app_build`` and the startup code fingerprint are captured at
    construction (process start); ``is_stale`` compares the live on-disk code fingerprint against that.
    """

    def __init__(self, roots, *, restart_exts=RESTART_EXTS, source_exts=SOURCE_EXTS,
                 exclude_dirs=EXCLUDE_DIRS, ttl: float = 1.5):
        self.roots = [Path(r) for r in roots]
        self.restart_exts = tuple(restart_exts)
        self.source_exts = tuple(source_exts)
        self.exclude_dirs = tuple(exclude_dirs)
        self.ttl = float(ttl)
        # Captured at construction = what THIS process is running.
        self.app_build = self._fingerprint(self.source_exts)
        self._startup_code = self._fingerprint(self.restart_exts)
        self._cache: tuple[float, str] | None = None   # (monotonic_deadline, code_fingerprint)

    # ── fingerprinting ────────────────────────────────────────────────────────
    def _iter_files(self, exts):
        for root in self.roots:
            if not root.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune excluded / hidden dirs in place so os.walk doesn't descend into them.
                dirnames[:] = [d for d in dirnames
                               if d not in self.exclude_dirs and not d.startswith(".")]
                for name in filenames:
                    if name.endswith(exts):
                        yield Path(dirpath) / name

    def _fingerprint(self, exts) -> str:
        h = hashlib.sha256()
        # Sort by string path so the digest is order-independent across filesystems.
        for path in sorted(self._iter_files(exts), key=str):
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                continue                         # a file vanishing mid-walk just drops from the stamp
            h.update(f"{path}:{mtime}\n".encode("utf-8"))
        return h.hexdigest()[:12]

    def _current_code(self) -> str:
        now = time.monotonic()
        if self._cache is not None and now < self._cache[0]:
            return self._cache[1]
        fp = self._fingerprint(self.restart_exts)
        self._cache = (now + self.ttl, fp)
        return fp

    # ── public API (mirrored by the client handshake helpers) ──────────────────
    def is_stale(self) -> bool:
        """True when the running process's code is older than the code on disk (restart needed)."""
        return self._current_code() != self._startup_code

    def handshake(self) -> dict:
        """The wire payload every client reads: the build this process runs + whether it's stale."""
        return {"app_build": self.app_build, "server_stale": self.is_stale()}

    def verify(self) -> list[str]:
        """Provider-side self-check (mirrors reader_sync_contract.verify): the stamp is well-formed
        and reproducible. Returns a list of problems (empty = healthy)."""
        problems: list[str] = []
        if not self.app_build or len(self.app_build) < 8:
            problems.append(f"app_build looks wrong: {self.app_build!r}")
        if not any(True for _ in self._iter_files(self.restart_exts)):
            problems.append("no restart-tracked (.py) files found under roots "
                            f"{[str(r) for r in self.roots]} — staleness can't be detected")
        # Recomputing with nothing changed must reproduce the startup fingerprint.
        if self._fingerprint(self.restart_exts) != self._startup_code:
            problems.append("code fingerprint is not reproducible for an unchanged tree")
        return problems


_PKG_DIR = Path(__file__).resolve().parent


def loaded_catalogue_roots() -> list[Path]:
    """The on-disk directories of every ``catalogue.*`` package THIS PROCESS has imported — webui,
    db_store, services, contracts, access_api, … — so a restart-requiring change in ANY of them is
    caught, not just in webui. Packages the server never imports (populate / test_kit / a cli-only
    module) are excluded automatically, because they aren't in ``sys.modules``.

    We walk each top-level package's directory (not just the already-loaded module files), so a
    submodule imported lazily inside a request handler, and the data files sitting beside the code
    (the contract JSONs), are covered without having to have been loaded when we snapshot."""
    tops: set[str] = set()
    for name in list(sys.modules):
        parts = name.split(".")
        if len(parts) >= 2 and parts[0] == "catalogue":
            tops.add(f"catalogue.{parts[1]}")
    roots: list[Path] = []
    for top in sorted(tops):
        mod = sys.modules.get(top)
        if mod is None:
            continue
        paths = list(getattr(mod, "__path__", []) or [])
        if paths:
            roots.extend(Path(p) for p in paths)                    # a package → its dir(s)
        else:
            f = getattr(mod, "__file__", None)
            if f:
                roots.append(Path(f).parent)                        # a single-module package
    return roots or [_PKG_DIR]


# The running server's stamp. Built lazily over the FULL set of imported catalogue packages and cached
# for the process. `finalize()` is called at the end of create_app (all eager imports done) so the
# baseline reflects the whole namespace; before that, any access builds it on demand.
DEFAULT: BuildStamp | None = None


def _default() -> BuildStamp:
    global DEFAULT
    if DEFAULT is None:
        DEFAULT = BuildStamp(loaded_catalogue_roots())
    return DEFAULT


def finalize() -> BuildStamp:
    """Capture the build baseline across every catalogue package this process has imported. Idempotent
    (once per process): the baseline is the process's code, which doesn't change under it."""
    return _default()


def build() -> str:
    """The build id this process is running (stable for the process lifetime)."""
    return _default().app_build


def is_stale() -> bool:
    return _default().is_stale()


def handshake() -> dict:
    return _default().handshake()


def verify() -> list[str]:
    return _default().verify()
