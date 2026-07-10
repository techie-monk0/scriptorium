"""Content signature — the opaque "is this the same content?" token.

A holding's `content_hash` column stores a signature in *wire form* (a short string).
NO code outside this module should parse that string — not the `t:`/`b:` prefix, not
the hash algorithm, not the length. Callers use the `Signature` value object:

    sig = signature.of(text, text_status, byte_hash)   # build from extracted content
    sig = signature.parse(holding.content_hash)        # wrap a stored wire value
    if sig and sig.is_text: ...                         # ask, don't inspect the string
    if sig.matches(other): ...                          # compare, don't `==` the wire

This is the abstraction the rest of the system (and, if a signature is ever surfaced
to a client/front end, that client) depends on. The encoding stays swappable here:
change the prefix scheme or the digest and nothing else needs to know.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Optional

# A text layer is trustworthy enough to fingerprint by content only when the extractor
# vouched for it; too little text falls back to the byte digest.
_TRUSTWORTHY = {"native", "ocr_good"}
_MIN_TEXT = 64

# Wire-format prefixes — PRIVATE to this module. Nothing else may reference them.
_TEXT = "t:"
_BYTE = "b:"


def _norm_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


@dataclass(frozen=True)
class Signature:
    """Opaque content signature. Construct via `of`/`parse`; compare via `matches`;
    persist via `wire`. Never branch on the contents of `wire` outside this module."""
    wire: str

    @property
    def is_text(self) -> bool:
        """True when derived from a trustworthy text layer — i.e. stable across a
        container re-encode or annotation (the cloud-resync case). False when it is
        only a byte digest, which a re-encode invalidates."""
        return self.wire.startswith(_TEXT)

    def matches(self, other: "Signature | str | None") -> bool:
        """Same content as `other` (a Signature, a stored wire string, or None)."""
        if other is None:
            return False
        return self.wire == (other.wire if isinstance(other, Signature) else other)

    def __str__(self) -> str:
        return self.wire


def of(text: Optional[str], text_status: Optional[str],
       byte_hash: Optional[str]) -> Optional[Signature]:
    """Build a signature: text-based when the text layer is trustworthy and long
    enough (stable across annotation/re-save), else a byte digest. None when neither
    basis is available."""
    if text and text_status in _TRUSTWORTHY:
        norm = _norm_text(text)
        if len(norm) >= _MIN_TEXT:
            return Signature(_TEXT + hashlib.sha256(norm.encode("utf-8")).hexdigest())
    return Signature(_BYTE + byte_hash) if byte_hash else None


def parse(wire: Optional[str]) -> Optional[Signature]:
    """Wrap a stored wire-form value (e.g. a `content_hash` column) as a Signature.
    None passes through, so callers can `parse` straight off a nullable column."""
    return Signature(wire) if wire else None
