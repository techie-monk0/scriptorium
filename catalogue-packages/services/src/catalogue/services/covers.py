"""Book cover art — fetched by ISBN (no book download), like a music app pulling album art.

Priority: Open Library Covers → Google Books → the EPUB's own embedded cover (fetched over
WebDAV; EPUBs only, never big PDFs). When nothing is found the caller renders a title+author
text tile. Covers are cached on disk (cache_path/write_cache) so shelves render instantly and
offline after the first fetch. Network is injectable (opener) and every failure returns None
— never raises — so a missing cover never breaks a page."""
from __future__ import annotations

import html
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

OpenerFn = Callable[[urllib.request.Request, float], bytes]

# Only fetch an EPUB over WebDAV for its embedded cover up to this size (placeholders still
# report their real size on disk, so we can gate before fetching). Big PDFs are skipped.
EMBED_MAX_BYTES = 12 * 1024 * 1024


def _open(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get(url: str, opener: Optional[OpenerFn], timeout: float = 8.0) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "library-cataloging/1.0"})
        return (opener or _open)(req, timeout)
    except Exception:
        return None


def _looks_image(b: Optional[bytes]) -> bool:
    return bool(b) and (b[:3] == b"\xff\xd8\xff"                       # JPEG
                        or b[:8] == b"\x89PNG\r\n\x1a\n"               # PNG
                        or b[:6] in (b"GIF87a", b"GIF89a"))            # GIF


def _is_svg(b: Optional[bytes]) -> bool:
    return bool(b) and b[:200].lstrip().startswith(b"<svg")


def _looks_art(b: Optional[bytes]) -> bool:
    """A provider's result is usable art — a raster image OR a constructed SVG (spines)."""
    return _looks_image(b) or _is_svg(b)


def image_ext(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if _is_svg(b):
        return ".svg"
    return ".jpg"


# ── sources ─────────────────────────────────────────────────────────────────────
def openlibrary_cover(isbn: str, *, opener: Optional[OpenerFn] = None, size: str = "L") -> Optional[bytes]:
    """Open Library Covers by ISBN (keyless). `default=false` → 404 instead of a blank
    1px when there's no cover, so we don't cache placeholders."""
    if not isbn:
        return None
    url = f"https://covers.openlibrary.org/b/isbn/{urllib.parse.quote(isbn)}-{size}.jpg?default=false"
    data = _get(url, opener)
    return data if _looks_image(data) else None


def _gb_url(params: dict) -> str:
    """Google Books volumes URL; adds &key= when GOOGLE_BOOKS_API_KEY is configured (the
    keyless endpoint has a tiny daily quota — a key lifts it and unblocks GB)."""
    from . import apikeys
    key = apikeys.get("GOOGLE_BOOKS_API_KEY")
    if key:
        params = {**params, "key": key}
    return "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(params)


def googlebooks_cover(isbn: str, *, opener: Optional[OpenerFn] = None) -> Optional[bytes]:
    """Google Books volume image (by ISBN). Uses an API key if configured."""
    if not isbn:
        return None
    js = _get(_gb_url({"q": f"isbn:{isbn}"}), opener)
    if not js:
        return None
    try:
        items = json.loads(js).get("items") or []
        links = (items[0].get("volumeInfo", {}) or {}).get("imageLinks", {}) if items else {}
    except Exception:
        return None
    url = links.get("thumbnail") or links.get("smallThumbnail")
    if not url:
        return None
    data = _get(url.replace("http://", "https://"), opener)
    return data if _looks_image(data) else None


def _norm_title(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_matches(query: str, cand: str) -> bool:
    """Guard against grabbing the wrong book's cover from a title search: accept only when
    one title contains the other, or their word sets overlap strongly (Jaccard ≥ 0.6)."""
    q, c = _norm_title(query), _norm_title(cand)
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    qs, cs = set(q.split()), set(c.split())
    return bool(qs and cs) and len(qs & cs) / len(qs | cs) >= 0.6


def _author_ok(author: str, cand_authors) -> bool:
    """If we know the author, require one of its ≥3-char tokens to appear in the
    candidate's author(s); if the source gave no author, don't block on it."""
    if not author:
        return True
    toks = [t for t in _norm_title(author).split() if len(t) >= 3]
    blob = _norm_title(" ".join(cand_authors or []))
    return (not toks) or (not blob) or any(t in blob for t in toks)


def openlibrary_cover_by_title(title: str, author: str = "", *,
                               opener: Optional[OpenerFn] = None, size: str = "L") -> Optional[bytes]:
    """Open Library search by title (+author) → the first matching doc's cover. For books
    with NO ISBN, or whose ISBN has no cover. Title/author gated to avoid mismatches."""
    if not title:
        return None
    q = {"title": title, "limit": 5, "fields": "title,author_name,cover_i"}
    if author:
        q["author"] = author
    js = _get("https://openlibrary.org/search.json?" + urllib.parse.urlencode(q), opener)
    if not js:
        return None
    try:
        docs = json.loads(js).get("docs") or []
    except Exception:
        return None
    for d in docs:
        if d.get("cover_i") and _title_matches(title, d.get("title", "")) \
                and _author_ok(author, d.get("author_name")):
            b = _get(f"https://covers.openlibrary.org/b/id/{d['cover_i']}-{size}.jpg?default=false",
                     opener)
            if _looks_image(b):
                return b
    return None


def googlebooks_cover_by_title(title: str, author: str = "", *,
                               opener: Optional[OpenerFn] = None) -> Optional[bytes]:
    """Google Books search by title (+author) → first matching volume's image."""
    if not title:
        return None
    qstr = f"intitle:{title}" + (f" inauthor:{author}" if author else "")
    js = _get(_gb_url({"q": qstr, "maxResults": 5}), opener)
    if not js:
        return None
    try:
        items = json.loads(js).get("items") or []
    except Exception:
        return None
    for it in items:
        vi = it.get("volumeInfo", {}) or {}
        links = vi.get("imageLinks", {}) or {}
        link = links.get("thumbnail") or links.get("smallThumbnail")
        if link and _title_matches(title, vi.get("title", "")) \
                and _author_ok(author, vi.get("authors")):
            b = _get(link.replace("http://", "https://"), opener)
            if _looks_image(b):
                return b
    return None


def _cover_from_epub_bytes(data: bytes) -> Optional[bytes]:
    """Pull the cover image out of EPUB bytes: OPF `properties=cover-image`, else
    `<meta name=cover>` → manifest item, else any file named like a cover."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return None
    names = z.namelist()
    opf = next((n for n in names if n.lower().endswith(".opf")), None)
    if opf:
        try:
            xml = z.read(opf).decode("utf-8", "replace")
        except Exception:
            xml = ""
        base = opf.rsplit("/", 1)[0] if "/" in opf else ""
        href = None
        m = (re.search(r'<item[^>]*properties="[^"]*cover-image[^"]*"[^>]*href="([^"]+)"', xml)
             or re.search(r'<item[^>]*href="([^"]+)"[^>]*properties="[^"]*cover-image', xml))
        if m:
            href = m.group(1)
        if not href:
            mid = re.search(r'<meta[^>]*name="cover"[^>]*content="([^"]+)"', xml)
            if mid:
                mh = re.search(r'<item[^>]*id="%s"[^>]*href="([^"]+)"' % re.escape(mid.group(1)), xml) \
                    or re.search(r'<item[^>]*href="([^"]+)"[^>]*id="%s"' % re.escape(mid.group(1)), xml)
                if mh:
                    href = mh.group(1)
        if href:
            href = html.unescape(href)
            full = os.path.normpath((base + "/" + href) if base else href).replace("\\", "/")
            try:
                return z.read(full)
            except KeyError:
                pass
    for n in names:
        if re.search(r'cover[^/]*\.(jpe?g|png)$', n, re.I):
            try:
                return z.read(n)
            except KeyError:
                pass
    return None


def embedded_cover(local_path: str, *, mounts=None, opener: Optional[OpenerFn] = None,
                   max_bytes: int = EMBED_MAX_BYTES) -> Optional[bytes]:
    """The EPUB's embedded cover, read from disk if hydrated else fetched over WebDAV.
    EPUBs only and only up to max_bytes (big PDFs would need the whole file + rendering —
    skipped). Returns image bytes or None."""
    if not local_path or not local_path.lower().endswith(".epub"):
        return None
    try:
        if os.path.exists(local_path) and os.path.getsize(local_path) > max_bytes:
            return None
    except OSError:
        pass
    from . import cloudsync
    data = None
    if os.path.exists(local_path) and not cloudsync.is_online_only(local_path):
        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except OSError:
            data = None
    if data is None:
        from . import webdav
        data = webdav.fetch_local(local_path, mounts=mounts, opener=None)
    if not data:
        return None
    img = _cover_from_epub_bytes(data)
    return img if _looks_image(img) else None


# ── provider registry ────────────────────────────────────────────────────────────
# Each art source is a separate CoverProvider implementation behind one interface.
# Providers declare which KIND of art they produce — a front COVER or a book SPINE —
# so both kinds share ONE registry and ONE fetch loop; `fetch_cover`/`fetch_spine`
# just filter the same `_PROVIDERS` list by kind. `needs_file=True` marks providers
# that read the book file (so a page render can skip them with skip_file=True). Add a
# source by writing a provider and decorating it with @provider — no caller change
# needed; a real spine-image source (should one ever exist) is just another
# kind=SPINE provider ahead of the constructed one.

COVER = "cover"
SPINE = "spine"


@dataclass
class CoverRequest:
    """What we know about a book for cover lookup."""
    isbn: Optional[str] = None
    title: Optional[str] = None
    author: str = ""
    local_path: Optional[str] = None       # for the embedded-cover provider
    mounts: object = None                  # WebDAV mounts for the embedded provider


class CoverProvider:
    """One way to get book art. `fetch` returns image/SVG bytes or None; never raises."""
    name = "base"
    kind = COVER                           # which art this produces: COVER or SPINE
    needs_file = False                     # True = inspects the book file (may download)

    def fetch(self, req: "CoverRequest", *, opener: Optional[OpenerFn] = None) -> Optional[bytes]:
        raise NotImplementedError


_PROVIDERS: list = []


def provider(cls):
    """Class decorator: instantiate and register a CoverProvider (order = priority)."""
    _PROVIDERS.append(cls())
    return cls


def providers(kind: Optional[str] = None) -> list:
    """The registered providers, in priority order (for tests / manual iteration);
    pass a `kind` (COVER/SPINE) to filter to that art kind."""
    return [p for p in _PROVIDERS if kind is None or p.kind == kind]


@provider
class OpenLibraryIsbn(CoverProvider):
    name = "openlibrary"
    def fetch(self, req, *, opener=None):
        return openlibrary_cover(req.isbn, opener=opener) if req.isbn else None


@provider
class GoogleBooksIsbn(CoverProvider):
    name = "googlebooks"
    def fetch(self, req, *, opener=None):
        return googlebooks_cover(req.isbn, opener=opener) if req.isbn else None


@provider
class OpenLibraryTitle(CoverProvider):
    name = "openlibrary-title"
    def fetch(self, req, *, opener=None):
        return openlibrary_cover_by_title(req.title, req.author, opener=opener) if req.title else None


@provider
class GoogleBooksTitle(CoverProvider):
    name = "googlebooks-title"
    def fetch(self, req, *, opener=None):
        return googlebooks_cover_by_title(req.title, req.author, opener=opener) if req.title else None


@provider
class EmbeddedEpub(CoverProvider):
    name = "embedded"
    needs_file = True
    def fetch(self, req, *, opener=None):
        return embedded_cover(req.local_path, mounts=req.mounts, opener=opener) \
            if req.local_path else None


def first_page_image(path: str, *, max_width: int = 600, page: int = 1) -> Optional[bytes]:
    """Render `page` (1-based; default the first) of a PDF/EPUB to PNG via PyMuPDF — a real
    cover for files we can read. Reads `path` directly (never downloads); returns None if
    absent, online-only (zeros), unreadable, or `page` is out of range. The caller passes
    an already-LOCAL path (hydrated or WebDAV-cached)."""
    if not path or not os.path.exists(path):
        return None
    try:
        import fitz
        doc = fitz.open(path)
        try:
            idx = max(1, int(page or 1)) - 1           # 1-based → 0-based, clamp to ≥ first
            if doc.page_count < 1 or idx >= doc.page_count:
                return None
            pg = doc.load_page(idx)
            zoom = min(2.5, max_width / max(1.0, pg.rect.width))
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pix.tobytes("png")
        finally:
            doc.close()
    except Exception:
        return None


@provider
class FirstPageRender(CoverProvider):
    """Last resort: render the file's first page. Only fires when the file is already
    local (req.local_path set to a readable file) — never triggers a download."""
    name = "firstpage"
    needs_file = True
    def fetch(self, req, *, opener=None):
        return first_page_image(req.local_path) if req.local_path else None


@provider
class ConstructedSpine(CoverProvider):
    """Build a book spine from metadata, tinted by the book's own cover colour. There is
    no real spine-image source, so this always succeeds. The cover used for the tint is
    fetched through the SAME cover layer (`fetch_cover`) — not a private fetch path — so a
    new cover source benefits spines for free. Registered last; a future real-spine-image
    provider would sit ahead of it (kind=SPINE) and win when it has a hit."""
    name = "spine-constructed"
    kind = SPINE
    def fetch(self, req, *, opener=None):
        got = fetch_cover(req.isbn, title=req.title, author=req.author,
                          local_path=req.local_path, mounts=req.mounts,
                          opener=opener, skip_file=(req.local_path is None))
        return make_spine(req.title or "Untitled", cover_bytes=(got[0] if got else None))


def _fetch(kind: str, isbn: Optional[str], *, title: Optional[str], author: str,
           local_path: Optional[str], mounts, opener: Optional[OpenerFn],
           skip_file: bool, allow: Optional[set]) -> Optional[tuple]:
    """Walk the registered providers of one art `kind` in order; return (bytes,
    provider_name) for the first usable hit, else None. `skip_file=True` skips providers
    that read the book file (use on page renders). `allow` limits to a set of names."""
    req = CoverRequest(isbn=isbn, title=title, author=author,
                       local_path=local_path, mounts=mounts)
    for p in _PROVIDERS:
        if p.kind != kind:
            continue
        if skip_file and p.needs_file:
            continue
        if allow is not None and p.name not in allow:
            continue
        try:
            b = p.fetch(req, opener=opener)
        except Exception:
            b = None
        if b and _looks_art(b):
            return b, p.name
    return None


def fetch_cover(isbn: Optional[str] = None, *, title: Optional[str] = None,
                author: str = "", local_path: Optional[str] = None, mounts=None,
                opener: Optional[OpenerFn] = None, skip_file: bool = False,
                allow: Optional[set] = None) -> Optional[tuple]:
    """First usable front-cover image from the COVER providers, as (bytes, name), or None.
    Whatever provider wins, its bytes are run through `prepare_cover` (trim baked frame +
    normalise) before returning — so every consumer caches frameless, lean art, and neither the
    route nor the bulk CLI needs to know how a cover is adjusted or where it came from."""
    got = _fetch(COVER, isbn, title=title, author=author, local_path=local_path,
                 mounts=mounts, opener=opener, skip_file=skip_file, allow=allow)
    return (prepare_cover(got[0]), got[1]) if got else None


def fetch_spine(isbn: Optional[str] = None, *, title: Optional[str] = None,
                author: str = "", local_path: Optional[str] = None, mounts=None,
                opener: Optional[OpenerFn] = None, skip_file: bool = True,
                allow: Optional[set] = None) -> Optional[tuple]:
    """A book spine (SVG bytes) from the SPINE providers, as (bytes, name). The constructed
    provider always succeeds, so this returns None only if every spine provider is filtered
    out. `skip_file` defaults True — a render shouldn't trigger a download for spine colour."""
    return _fetch(SPINE, isbn, title=title, author=author, local_path=local_path,
                  mounts=mounts, opener=opener, skip_file=skip_file, allow=allow)


# ── on-disk cache ────────────────────────────────────────────────────────────────
def cached_path(cache_dir: str, key: str) -> Optional[str]:
    """Existing cached art for `key` (any image ext, or .svg for spines), or None."""
    for ext in (".jpg", ".png", ".gif", ".svg"):
        p = os.path.join(cache_dir, key + ext)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None


def write_cache(cache_dir: str, key: str, data: bytes) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    _clear_miss(cache_dir, key)
    # Drop any existing cover of a DIFFERENT extension, else cached_path (which checks
    # .jpg before .png) could keep serving a stale one when the format changes
    # (e.g. upgrading a .jpg thumbnail with a .png first-page render).
    for ext in (".jpg", ".png", ".gif", ".svg"):
        old = os.path.join(cache_dir, key + ext)
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass
    dest = os.path.join(cache_dir, key + image_ext(data))
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return dest


def is_missed(cache_dir: str, key: str) -> bool:
    """A prior lookup found no cover → serve the placeholder without re-hitting the
    network on every render. The bulk CLI re-tries misses (and adds embedded covers)."""
    return os.path.exists(os.path.join(cache_dir, key + ".miss"))


def mark_miss(cache_dir: str, key: str) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    open(os.path.join(cache_dir, key + ".miss"), "w").close()


def _clear_miss(cache_dir: str, key: str) -> None:
    try:
        os.remove(os.path.join(cache_dir, key + ".miss"))
    except OSError:
        pass


clear_miss = _clear_miss     # public alias

# Every art file for one edition is keyed by its integer id: the cover `e<id>`, the
# derived spine `spine-e<id>`, plus the `.miss` marker and operator pin. SQLite recycles
# primary keys, so a deleted edition's id is later handed to a fresh import — and without
# busting these on delete that new book inherits the old book's cover (the Lisa-Jewell bug).
_ART_EXTS = (".jpg", ".png", ".gif", ".svg", ".miss", ".part")


def edition_art_keys(eid) -> tuple:
    """The cache keys holding edition <eid>'s art: its cover and its derived spine."""
    return (f"e{eid}", f"spine-e{eid}")


def purge_edition_art(cache_dir: Optional[str], pinned_dir: Optional[str], eid) -> None:
    """Delete EVERY cached/derived/pinned file for edition <eid> — cover, spine, miss
    marker, and operator pin — so a later id-reuse can't serve the deleted book's art.
    Call from every edition-delete path. Dirs may be None (skip that store); never raises."""
    targets = [(cache_dir, k) for k in edition_art_keys(eid) if cache_dir]
    if pinned_dir:
        targets.append((pinned_dir, f"e{eid}"))
    for d, key in targets:
        for ext in _ART_EXTS:
            try:
                os.remove(os.path.join(d, key + ext))
            except OSError:
                pass


def image_dims(path: str):
    """(width, height) of a cached JPEG/PNG from its header, or None — no PIL dependency."""
    import struct
    try:
        with open(path, "rb") as f:
            data = f.read(0x10000)
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", data[16:24])
        except struct.error:
            return None
    if data[:2] == b"\xff\xd8":                          # JPEG: walk to a SOF marker
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return (w, h)
            try:
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
            except struct.error:
                break
    return None


def is_lowres(path: str, *, min_dim: int = 400) -> bool:
    """True if a cached cover is small enough to be worth replacing with a first-page
    render (e.g. an Open Library thumbnail) — its larger side is under min_dim px."""
    d = image_dims(path)
    return bool(d) and max(d) < min_dim


def refresh_from_file(cache_dir: str, key: str, local_path: str, *, min_dim: int = 400) -> Optional[str]:
    """Called when a book becomes locally readable (opened/downloaded): derive its cover
    from the file's first page IF it has none yet or only a low-res one. Clears the miss
    marker. Returns 'firstpage' if it (re)wrote a cover, else None. Never downloads."""
    if not local_path or not os.path.exists(local_path):
        return None
    existing = cached_path(cache_dir, key)
    if existing and not is_lowres(existing, min_dim=min_dim):
        return None                                      # already a good cover — keep it
    img = first_page_image(local_path)
    if not img:
        return None
    img = prepare_cover(img)                             # trim a page-render's flat margin + normalise
    write_cache(cache_dir, key, img)                     # also clears the miss marker
    return "firstpage"


# ── normalise an uploaded / cached cover (downscale + re-encode) ─────────────────
# A cover is only ever shown in a small 2:3 portrait tile — ≤ ~240px CSS, so ~720px tall
# even on a 3× retina phone. Anything larger is pure download weight. We cap the long side
# and re-encode to a lean JPEG, but ONLY when that actually saves bytes (flat-art PNGs can
# be smaller than any JPEG) — so the step never bloats a cover. Aspect ratio is preserved
# (covers are ~99% portrait already; the tile's object-fit:cover handles the rare oddball),
# EXIF orientation is baked in before metadata is dropped (phone uploads), and transparency
# is flattened onto white. Pure (bytes→bytes); on any failure — no Pillow, undecodable, SVG —
# the original bytes pass straight through, so normalising can never lose a cover.
COVER_MAX_DIM = 1000              # longest side after downscale (retina headroom over a 240px tile)
COVER_JPEG_QUALITY = 82
_COVER_RECOMPRESS_OVER = 300_000  # leave in-spec JPEGs byte-for-byte (above our own ~250KB output,
                                  # so a normalised cover is never needlessly re-encoded); rework heavier ones


def normalize_cover(data: bytes, *, max_dim: int = COVER_MAX_DIM,
                    quality: int = COVER_JPEG_QUALITY,
                    recompress_over: int = _COVER_RECOMPRESS_OVER) -> bytes:
    """Downscale an oversized cover and/or re-encode a bloated one to a lean JPEG, returning
    new bytes — or the original bytes unchanged when it's already lean, isn't a raster image
    (SVG/junk), Pillow is missing, or re-encoding wouldn't save space."""
    if not _looks_image(data):                       # SVG / non-raster → leave to the caller
        return data
    is_jpeg = data[:3] == b"\xff\xd8\xff"
    try:
        from PIL import Image, ImageOps
    except Exception:
        return data                                  # Pillow absent (as in dominant_color): store raw
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))   # honour camera orientation
        im.load()
    except Exception:
        return data
    oversize = max(im.size) > max_dim
    if is_jpeg and not oversize and len(data) <= recompress_over:
        return data                                  # already a small, in-spec JPEG — don't recompress
    try:
        if oversize:
            im.thumbnail((max_dim, max_dim), Image.LANCZOS)
        if im.mode in ("RGBA", "LA", "P"):           # flatten any transparency onto white
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        out = buf.getvalue()
    except Exception:
        return data
    # Only adopt the re-encode when it genuinely helps; a downscale always does.
    if not oversize and len(out) >= len(data):
        return data
    return out


# ── auto-trim a baked-in publisher frame ─────────────────────────────────────────
# Many catalogue covers (Open Library scans especially) ship with a flat, uniform margin
# baked into the image — a white (or solid-colour) frame around the artwork. With the tile's
# object-fit:contain that frame is no longer cropped away by the layout, so it shows. We trim
# it ONCE here, at fetch time, so the cached bytes are already frameless and the render stays a
# plain send_file (nothing runs per view or on the device). Conservative + never-loses: the
# border colour is taken from the four corners and only trimmed when they AGREE (a real frame —
# art that bleeds to any edge makes the corners disagree and is left alone), and a trim that
# would eat into the artwork (shrink either side past TRIM_KEEP_MIN of the original) is refused.
# Pure (bytes→bytes); non-raster/undecodable/Pillow-missing all pass straight through.
TRIM_TOLERANCE = 12      # 0–255 per channel: how far from the corner colour still counts as border
TRIM_KEEP_MIN = 0.55     # refuse a trim shrinking either dimension below this fraction (anti over-crop)


def trim_uniform_border(data: bytes, *, tolerance: int = TRIM_TOLERANCE,
                        keep_min: float = TRIM_KEEP_MIN) -> bytes:
    """Crop a flat, uniform border (a publisher's white frame) off a raster cover, returning
    new PNG bytes — or the ORIGINAL bytes unchanged when there's no clearly-uniform border,
    the image is blank, Pillow is missing, or the trim would eat into the artwork."""
    if not _looks_image(data):                       # SVG / non-raster → nothing to trim
        return data
    try:
        from PIL import Image, ImageChops
    except Exception:
        return data                                  # Pillow absent → store raw (as normalize_cover does)
    try:
        rgb = Image.open(io.BytesIO(data)).convert("RGB")
        rgb.load()
    except Exception:
        return data
    w, h = rgb.size
    if w < 8 or h < 8:
        return data
    # Border colour = the average of the four corners; only trim when they AGREE within
    # tolerance. Disagreement means the artwork reaches an edge → there is no uniform frame.
    corners = [rgb.getpixel((0, 0)), rgb.getpixel((w - 1, 0)),
               rgb.getpixel((0, h - 1)), rgb.getpixel((w - 1, h - 1))]
    bg_color = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
    if any(max(abs(c[i] - bg_color[i]) for i in range(3)) > tolerance for c in corners):
        return data
    # Bounding box of everything that differs from the border by more than tolerance, taking
    # the per-pixel MAX across channels (a single-channel difference still counts as content).
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg_color)).split()
    mono = diff[0]
    for band in diff[1:]:
        mono = ImageChops.lighter(mono, band)
    bbox = mono.point(lambda p: 255 if p > tolerance else 0).getbbox()
    if not bbox:
        return data                                  # whole image is the border colour (blank) → leave it
    left, top, right, bottom = bbox
    if right - left >= w and bottom - top >= h:
        return data                                  # content already fills the frame → no border
    if right - left < keep_min * w or bottom - top < keep_min * h:
        return data                                  # would cut into the art → too aggressive, skip
    try:
        buf = io.BytesIO()
        rgb.crop(bbox).save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        return data


def prepare_cover(data: bytes) -> bytes:
    """The cover-art adjustment pipeline run on EVERY cover before it's cached, whatever its
    source (ISBN provider, embedded EPUB, first-page render, operator upload/pin): trim a baked
    publisher frame, then downscale/re-encode to a lean JPEG. Source-agnostic and pure
    (bytes→bytes) — any step that can't improve the image passes the bytes through untouched, so
    preparing a cover never loses or corrupts it; non-raster (SVG) bytes pass straight through."""
    return normalize_cover(trim_uniform_border(data))


# ── title/author placeholder tile (no cover found) ───────────────────────────────
_PALETTE = ["#3a4a6b", "#5b3a4a", "#3a5b4a", "#4a3a5b", "#5b4f3a", "#3a5358", "#53383a"]


def _palette_for(title: str) -> str:
    """Deterministic spine/tile colour for a title (stable per book)."""
    return _PALETTE[sum(map(ord, title or "")) % len(_PALETTE)]


def _luminance(hexcolor: str) -> float:
    """Perceived luminance 0–1 of a #rrggbb colour (for picking light vs dark text)."""
    try:
        r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    except (ValueError, IndexError):
        return 0.0
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def dominant_color(data: bytes) -> Optional[str]:
    """The representative spine colour of a cover image, as #rrggbb — the most common
    quantised colour, skipping near-white/near-black (covers often have a white frame),
    then deepened a touch so a spine reads as a spine. None if PIL is missing or the bytes
    aren't a decodable raster. Pure (operates on bytes), so it never couples the spine
    cache to the cover cache — see the spine route."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((48, 48))
        q = im.quantize(colors=8).convert("RGB")
        counts = sorted(q.getcolors(48 * 48) or [], reverse=True)   # (count, (r,g,b)) desc
    except Exception:
        return None
    best = None
    for _count, rgb in counts:
        r, g, b = rgb
        if min(r, g, b) > 224 or max(r, g, b) < 26:                 # near-white / near-black
            continue
        best = rgb
        break
    if best is None and counts:
        best = counts[0][1]
    if best is None:
        return None
    r, g, b = (int(c * 0.82) for c in best)                         # deepen for spine feel
    return f"#{r:02x}{g:02x}{b:02x}"


def _fill_crop(im, w: int, h: int):
    """Resize `im` to cover a w×h box (object-fit: cover) then centre-crop to it."""
    from PIL import Image
    iw, ih = im.size
    s = max(w / iw, h / ih)
    nw, nh = max(1, round(iw * s)), max(1, round(ih * s))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _distinctiveness(im_small):
    """Per-pixel 'stands out from the rest of the cover' score (numpy float map): colour
    distance from the cover's BACKGROUND colour (median of a border ring — covers have
    margins), boosted by saturation and local edge detail. High where a figure / photo /
    emblem departs from the ground; low across the flat background and plain text."""
    import numpy as np
    from PIL import ImageFilter
    rgb = np.asarray(im_small.convert("RGB"), dtype="float32")
    sh, sw = rgb.shape[:2]
    ring = max(2, int(min(sh, sw) * 0.06))
    border = np.concatenate([rgb[:ring].reshape(-1, 3), rgb[-ring:].reshape(-1, 3),
                             rgb[:, :ring].reshape(-1, 3), rgb[:, -ring:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
    dist /= (dist.max() or 1.0)
    sat = np.asarray(im_small.convert("HSV"), dtype="float32")[:, :, 1] / 255.0
    edge = np.asarray(im_small.convert("L").filter(ImageFilter.FIND_EDGES),
                      dtype="float32") / 255.0
    return dist * (0.4 + 0.6 * sat) + 0.2 * edge


def _square_in(cx, cy, side, w, h):
    """A `side`-square box centred near (cx,cy), clamped inside w×h → (x0,y0,x1,y1)."""
    side = min(side, w, h)
    x0 = int(max(0, min(cx - side / 2, w - side)))
    y0 = int(max(0, min(cy - side / 2, h - side)))
    return x0, y0, x0 + int(side), y0 + int(side)


def _best_blob(score):
    """Bounding box (x0,y0,x1,y1) of the largest high-score BLOB in a 2-D score map,
    squared with ~15% padding; None if nothing significant. Connected components via OpenCV
    when present (isolates a real object, not just the busiest cell), else a smoothed-grid
    peak window."""
    import numpy as np
    sh, sw = score.shape
    if score.max() <= 0:
        return None
    try:
        import cv2
        sm = cv2.GaussianBlur(score, (0, 0), sigmaX=max(1.0, min(sh, sw) * 0.02))
        mask = (sm > sm.mean() + 0.6 * sm.std()).astype("uint8")
        n, lbl, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        best, best_s = None, -1.0
        for k in range(1, n):
            if stats[k, cv2.CC_STAT_AREA] < sh * sw * 0.01:    # ignore specks
                continue
            v = float(sm[lbl == k].sum())                      # rank by total distinctiveness
            if v > best_s:
                best_s, best = v, k
        if best is not None:
            x, y = stats[best, cv2.CC_STAT_LEFT], stats[best, cv2.CC_STAT_TOP]
            w, h = stats[best, cv2.CC_STAT_WIDTH], stats[best, cv2.CC_STAT_HEIGHT]
            return _square_in(x + w / 2, y + h / 2, max(w, h) * 1.15, sw, sh)
    except Exception:
        pass
    gy, gx = min(14, sh), min(14, sw)                          # numpy fallback: smoothed peak
    ch, cw = sh // gy, sw // gx
    if ch < 1 or cw < 1:
        return None
    grid = score[:ch * gy, :cw * gx].reshape(gy, ch, gx, cw).sum(axis=(1, 3))
    pad = np.pad(grid, 1, mode="edge")
    sm = sum(pad[i:i + gy, j:j + gx] for i in range(3) for j in range(3))
    iy, ix = np.unravel_index(int(np.argmax(sm)), sm.shape)
    return _square_in((ix + 0.5) * cw, (iy + 0.5) * ch, min(sw, sh) * 0.6, sw, sh)


def _text_boxes(im) -> list:
    """Word bounding boxes (l,t,w,h in `im` pixels) from Tesseract — the project's OCR
    engine, used here purely as a TEXT DETECTOR. Empty list if the binary is absent or it
    finds nothing; never raises. Lets the spine feature real imagery, not the cover's title
    (which the spine already renders as crisp text)."""
    import os
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("tesseract"):
        return []
    try:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.png")
            im.convert("RGB").save(p)
            out = subprocess.run(["tesseract", p, "stdout", "tsv"], capture_output=True,
                                 text=True, timeout=30).stdout
    except Exception:
        return []
    rows = out.splitlines()
    if not rows:
        return []
    boxes = []
    for ln in rows[1:]:
        c = ln.split("\t")
        if len(c) < 12:
            continue
        try:
            if c[0] != "5" or float(c[10]) < 40 or not c[11].strip():   # word level, conf≥40
                continue
            boxes.append((int(c[6]), int(c[7]), int(c[8]), int(c[9])))
        except (ValueError, IndexError):
            continue
    return boxes


def _text_mask(im, shape):
    """Boolean mask (shape = (h,w) of the score map) marking where Tesseract found text,
    scaled from a 640px detection pass with a little padding."""
    import numpy as np
    sh, sw = shape
    mask = np.zeros((sh, sw), dtype=bool)
    med = im.convert("RGB").copy()
    med.thumbnail((640, 640))
    mw, mh = med.size
    boxes = _text_boxes(med)
    if not boxes or mw == 0 or mh == 0:
        return mask
    sx, sy = sw / mw, sh / mh
    for (l, t, w, h) in boxes:
        x0, y0 = max(0, int(l * sx) - 2), max(0, int(t * sy) - 2)
        x1, y1 = min(sw, int((l + w) * sx) + 2), min(sh, int((t + h) * sy) + 2)
        mask[y0:y1, x0:x1] = True
    return mask


def _salient_crop(im):
    """Crop the cover's most DISTINCTIVE region for the spine. Prefers a real IMAGE: text is
    detected (Tesseract) and excluded from the distinctiveness map, and the largest non-text
    blob (a figure / photo / emblem) is taken. Only if the cover is essentially text-only —
    no non-text region left — does it fall back to the most distinctive crop INCLUDING text.
    Squared-off, mapped back to full resolution. Returns a PIL image, or None."""
    try:
        import numpy as np
        small = im.convert("RGB").copy()
        small.thumbnail((220, 220))
        score = _distinctiveness(small)
    except Exception:
        return None
    sh, sw = score.shape
    if min(sh, sw) < 16:
        return None
    nontext = score.copy()
    nontext[_text_mask(im, (sh, sw))] = 0.0                    # exclude title/blurb text
    box = _best_blob(nontext) or _best_blob(score)             # prefer image, else most-distinctive
    if box is None:
        return None
    fx, fy = im.size[0] / sw, im.size[1] / sh                  # small→full scale
    x0, y0, x1, y1 = box
    return im.crop((int(x0 * fx), int(y0 * fy), int(x1 * fx), int(y1 * fy)))


def cover_spine_art(data: bytes, *, width: int = 64, height: int = 300,
                    scale: int = 2) -> Optional[bytes]:
    """Build the spine's raster ground FROM the cover (Tier-1.5): a blurred, darkened
    fill carrying the cover's colour + composition across the top, and a sharp crop of
    the cover's hero region across the foot — so the spine reads as derived from the art.
    Returns PNG bytes (embedded by `spine_svg`), or None if PIL is missing / undecodable."""
    try:
        from PIL import Image, ImageFilter, ImageEnhance, ImageDraw
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    W, H = width * scale, height * scale
    split = int(H * 0.62)
    try:
        top = _fill_crop(im, W, split).filter(ImageFilter.GaussianBlur(max(2, W * 0.16)))
        top = ImageEnhance.Brightness(top).enhance(0.58)
        top = ImageEnhance.Color(top).enhance(1.12)            # keep the colour identity
        motif = _salient_crop(im) or im
        foot = _fill_crop(motif, W, H - split)
        canvas = Image.new("RGB", (W, H))
        canvas.paste(top, (0, 0))
        canvas.paste(foot, (0, split))
        ImageDraw.Draw(canvas).line([(0, split), (W, split)], fill=(255, 255, 255), width=scale)
    except Exception:
        return None
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def spine_svg(title: str, bg: Optional[str] = None,
              art_png: Optional[bytes] = None) -> bytes:
    """A book spine: a tall, narrow tile with the title set vertically (reads top-to-bottom).
    No author — a thin spine reads cleaner with just the title. With `art_png` (built by
    `cover_spine_art`) the ground is the cover-derived art — blurred colour up top, a sharp
    hero crop at the foot — with white text over a scrim; otherwise `bg` tints a flat ground
    (sampled from the cover by `dominant_color`, else the deterministic palette so a spine
    renders with no cover at all). Pure SVG (the art embedded as base64), scales crisply —
    the spine analogue of `placeholder_svg`."""
    title = (title or "Untitled").strip()
    W, H = 64, 300

    def _clip(s, n):
        s = s.strip()
        return s if len(s) <= n else s[:n - 1].rstrip() + "…"

    if art_png:
        import base64
        b64 = base64.b64encode(art_png).decode("ascii")
        cy, fg = 96, "#ffffff"                                 # title in the top (art) band
        t = _clip(title, 21)                                   # ~top 62% of the spine
        ground = (f'<defs><linearGradient id="sc" x1="0" y1="0" x2="0" y2="1">'
                  f'<stop offset="0" stop-color="#000" stop-opacity="0.5"/>'
                  f'<stop offset="0.62" stop-color="#000" stop-opacity="0.06"/>'
                  f'</linearGradient></defs>'
                  f'<image href="data:image/png;base64,{b64}" width="{W}" height="{H}" '
                  f'preserveAspectRatio="xMidYMid slice"/>'
                  f'<rect width="{W}" height="{int(H * 0.62)}" fill="url(#sc)"/>')
    else:
        bg = bg or _palette_for(title)
        cy, fg = 150, "#ffffff" if _luminance(bg) < 0.6 else "#1a1a1a"
        t = _clip(title, H // 9)                               # ~one glyph per 9px tall
        ground = f'<rect width="{W}" height="{H}" fill="{bg}"/>'

    title_el = (f'<text transform="translate(32 {cy}) rotate(90)" text-anchor="middle" '
                f'dominant-baseline="central" fill="{fg}" font-size="17" font-weight="600" '
                f'font-family="Georgia,serif">{html.escape(t)}</text>')
    # preserveAspectRatio="none" makes the SVG stretch to fill its <img> box on every
    # browser (iOS Safari ignores CSS object-fit on SVG images and would otherwise
    # letterbox a wide/thin spine) — so the artwork fills the whole tile width.
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}" preserveAspectRatio="none">'
           f'{ground}'
           # subtle 3D: lit left edge, shadowed right edge, capped top & foot
           f'<rect width="3" height="{H}" fill="#ffffff" opacity="0.18"/>'
           f'<rect x="{W - 4}" width="4" height="{H}" fill="#000000" opacity="0.22"/>'
           f'<rect width="{W}" height="4" fill="#ffffff" opacity="0.14"/>'
           f'<rect y="{H - 4}" width="{W}" height="4" fill="#000000" opacity="0.16"/>'
           f'{title_el}</svg>')
    return svg.encode("utf-8")


def make_spine(title: str, cover_bytes: Optional[bytes] = None) -> bytes:
    """Build a spine SVG from a title and (optionally) the book's cover bytes — cover-derived
    art when given, palette otherwise. The one place that turns a cover into a spine, shared
    by the provider (cover from the network) and callers that already hold the cover bytes
    (the route / a warm pass reusing the cover cache — no re-fetch)."""
    art = cover_spine_art(cover_bytes) if cover_bytes else None
    bg = dominant_color(cover_bytes) if cover_bytes else None
    return spine_svg(title or "Untitled", bg=bg, art_png=art)


def placeholder_svg(title: str, author: str = "") -> bytes:
    """A music-app-style 'no art' tile: title + author on a colour picked deterministically
    from the title (stable per book). Pure SVG, no deps, scales crisply."""
    title = (title or "Untitled").strip()
    author = (author or "").strip()
    bg = _palette_for(title)

    def _wrap(text, width, limit):
        words, lines, cur = text.split(), [], ""
        for w in words:
            if len(cur) + len(w) + 1 > width:
                lines.append(cur); cur = w
                if len(lines) >= limit:
                    break
            else:
                cur = (cur + " " + w).strip()
        if cur and len(lines) < limit:
            lines.append(cur)
        if len(lines) == limit and len(" ".join(lines)) < len(text):
            lines[-1] = lines[-1].rstrip(".,") + "…"
        return lines

    lines = _wrap(title, 18, 4)
    ty = 150 - (len(lines) - 1) * 16
    spans = "".join(
        f'<tspan x="150" dy="{0 if i == 0 else 32}">{html.escape(l)}</tspan>'
        for i, l in enumerate(lines))
    author_el = (f'<text x="150" y="380" text-anchor="middle" fill="#ffffffcc" '
                 f'font-size="18" font-family="system-ui,sans-serif">'
                 f'{html.escape(author[:40])}</text>') if author else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" '
           f'viewBox="0 0 300 450"><rect width="300" height="450" fill="{bg}"/>'
           f'<rect x="0" y="0" width="300" height="6" fill="#ffffff33"/>'
           f'<text y="{ty}" text-anchor="middle" fill="#fff" font-size="26" '
           f'font-weight="700" font-family="Georgia,serif">{spans}</text>'
           f'{author_el}</svg>')
    return svg.encode("utf-8")
