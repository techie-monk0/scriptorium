"""Back-compat shim — the file effects now live behind the `Backing` port (backing.py).

Historically this module held `trash_files`; the access layer now performs on-disk effects through
an injectable `Backing` (default `LocalBacking`), so the bytes can live anywhere. This thin delegator
remains for any direct caller; new code uses `access.backing.run(file_ops, trash_dir)`.
"""
from __future__ import annotations

from .backing import LocalBacking


def trash_files(trash_dir, file_ops) -> None:
    """Deprecated: delegates to `LocalBacking().run`. Prefer `access.backing.run(...)`."""
    LocalBacking().run(file_ops, trash_dir)
