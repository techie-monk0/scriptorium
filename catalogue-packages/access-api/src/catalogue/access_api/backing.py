"""`Backing` — the pluggable storage for a holding's bytes and the file effects a delete performs.

A holding is an abstract record; its *file* is ONE backing of it — the same port-adapter idea as the
DB stores. So the access layer never calls the filesystem directly: writers / `Session` / OrphanSweep
program against this port, and the bytes can live on the local disk (`LocalBacking`, the default), an
object store, or a test fake, with no change to the access layer. The port covers exactly the effects
the entity-API performs: an existence probe and the post-commit `FileOp`s (trash a deleted holding's
file, move a relinked one) — which run AFTER the transaction commits, so a rollback leaves files
intact. Inject a different adapter via `bind(..., backing=...)` or by assigning `acc.backing`.
"""
from __future__ import annotations

import abc
import shutil
from pathlib import Path


class Backing(abc.ABC):
    """Port: the filesystem operations the access layer performs on holding files."""

    @abc.abstractmethod
    def exists(self, path: str) -> bool:
        """Whether a backing object exists at `path`."""

    @abc.abstractmethod
    def run(self, file_ops, trash_dir) -> None:
        """Perform the post-commit `FileOp`s (`trash` → move into `trash_dir`; `move` → to its dest).
        Missing sources are skipped; unknown ops ignored. Non-transactional (runs after commit)."""


class LocalBacking(Backing):
    """The local-filesystem adapter — the historical behavior (trash to a `.trash/` sibling)."""

    def exists(self, path):
        return Path(path).exists()

    def run(self, file_ops, trash_dir):
        for f in file_ops:
            if f.op == "trash":
                src = Path(f.path)
                if src.exists():
                    Path(trash_dir).mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(Path(trash_dir) / src.name))
            elif f.op == "move" and f.dest:
                src = Path(f.path)
                if src.exists():
                    Path(f.dest).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(f.dest))
