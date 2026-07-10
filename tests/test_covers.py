"""Book covers by ISBN (catalogue/domain/covers.py) + the /edition/<id>/cover.jpg route.

All offline: a fake opener returns canned image bytes / Google Books JSON, so the source
priority, caching, miss-marking, embedded-EPUB extraction and the placeholder tile are
pinned without network.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from catalogue.db_store import connect
from catalogue.services import covers

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64          # minimal JPEG-ish magic
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    """Keep the repo's real api_key.txt/.kdrive_settings out of these tests, so the
    Google Books URL is deterministic (no &key=)."""
    from catalogue.services import apikeys
    monkeypatch.setattr(apikeys, "file_values", lambda: {})
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)


def _opener(table):
    def op(req, timeout):
        if req.full_url in table:
            return table[req.full_url]
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
    return op


# ── sources ─────────────────────────────────────────────────────────────────────
def test_openlibrary_cover_hit_and_miss():
    url = "https://covers.openlibrary.org/b/isbn/9780861712908-L.jpg?default=false"
    assert covers.openlibrary_cover("9780861712908", opener=_opener({url: JPEG})) == JPEG
    assert covers.openlibrary_cover("9780861712908", opener=_opener({})) is None
    assert covers.openlibrary_cover("", opener=_opener({})) is None


def test_googlebooks_cover_parses_imagelinks():
    api = "https://www.googleapis.com/books/v1/volumes?q=isbn%3A9780861712908"
    thumb = "https://books.google.com/img.jpg"
    js = json.dumps({"items": [{"volumeInfo": {"imageLinks": {"thumbnail": thumb}}}]}).encode()
    out = covers.googlebooks_cover("9780861712908", opener=_opener({api: js, thumb: PNG}))
    assert out == PNG


def test_fetch_cover_priority_ol_then_gb():
    ol = "https://covers.openlibrary.org/b/isbn/X-L.jpg?default=false"
    assert covers.fetch_cover("X", opener=_opener({ol: JPEG})) == (JPEG, "openlibrary")
    api = "https://www.googleapis.com/books/v1/volumes?q=isbn%3AX"
    thumb = "https://t/img.jpg"
    js = json.dumps({"items": [{"volumeInfo": {"imageLinks": {"smallThumbnail": thumb}}}]}).encode()
    assert covers.fetch_cover("X", opener=_opener({api: js, thumb: PNG})) == (PNG, "googlebooks")
    assert covers.fetch_cover("X", opener=_opener({})) is None


# ── title+author search (for books with no ISBN / no ISBN-cover) ─────────────────
def test_openlibrary_by_title_matches_and_gates():
    api = ("https://openlibrary.org/search.json?"
           "title=The+Power+of+Mantra&limit=5&fields=title%2Cauthor_name%2Ccover_i"
           "&author=Lama+Zopa+Rinpoche")
    cov = "https://covers.openlibrary.org/b/id/42-L.jpg?default=false"
    js = json.dumps({"docs": [
        {"title": "Some Other Book", "author_name": ["X"], "cover_i": 1},   # title gate fails
        {"title": "The Power of Mantra", "author_name": ["Lama Zopa Rinpoche"], "cover_i": 42},
    ]}).encode()
    out = covers.openlibrary_cover_by_title("The Power of Mantra", "Lama Zopa Rinpoche",
                                            opener=_opener({api: js, cov: JPEG}))
    assert out == JPEG


def test_title_search_rejects_wrong_title():
    assert covers._title_matches("Tantric Ethics", "Tantric Ethics: Explanation") is True
    assert covers._title_matches("Tantric Ethics", "The Joy of Cooking") is False


def test_fetch_cover_falls_through_to_title_search():
    # ISBN misses everywhere; title search on OL hits.
    ol_isbn = "https://covers.openlibrary.org/b/isbn/Z-L.jpg?default=false"
    gb_isbn = "https://www.googleapis.com/books/v1/volumes?q=isbn:Z"
    api = ("https://openlibrary.org/search.json?"
           "title=My+Book&limit=5&fields=title%2Cauthor_name%2Ccover_i")
    cov = "https://covers.openlibrary.org/b/id/7-L.jpg?default=false"
    js = json.dumps({"docs": [{"title": "My Book", "cover_i": 7}]}).encode()
    table = {api: js, cov: PNG}     # ISBN urls absent → those 404
    out = covers.fetch_cover("Z", title="My Book", opener=_opener(table))
    assert out == (PNG, "openlibrary-title")


# ── provider registry ────────────────────────────────────────────────────────────
def test_providers_registered_in_priority_order():
    names = [p.name for p in covers.providers(covers.COVER)]
    assert names == ["openlibrary", "googlebooks", "openlibrary-title",
                     "googlebooks-title", "embedded", "firstpage"]
    assert next(p for p in covers.providers() if p.name == "embedded").needs_file is True
    # The spine art lives in the SAME registry, filtered out of cover lookups by kind.
    spines = covers.providers(covers.SPINE)
    assert [p.name for p in spines] == ["spine-constructed"]
    assert all(p.kind == covers.COVER for p in covers.providers(covers.COVER))


def test_skip_file_skips_embedded(tmp_path, monkeypatch):
    # an EPUB with an embedded cover, but skip_file=True must NOT use it
    p = _epub_with_cover(tmp_path, declared=True)
    assert covers.fetch_cover(local_path=str(p)) == (JPEG, "embedded")     # normally found
    assert covers.fetch_cover(local_path=str(p), skip_file=True) is None   # skipped


def test_allow_limits_to_named_providers():
    ol = "https://covers.openlibrary.org/b/isbn/X-L.jpg?default=false"
    # only allow google → the OL hit is ignored, GB has nothing → None
    assert covers.fetch_cover("X", opener=_opener({ol: JPEG}), allow={"googlebooks"}) is None


# ── embedded EPUB cover ──────────────────────────────────────────────────────────
def _epub_with_cover(tmp_path, *, declared=True):
    p = tmp_path / "b.epub"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        manifest = ('<item id="cov" href="images/cover.jpg" media-type="image/jpeg" '
                    'properties="cover-image"/>') if declared else ""
        z.writestr("OEBPS/content.opf",
                   f'<package><manifest>{manifest}</manifest></package>')
        z.writestr("OEBPS/images/cover.jpg", JPEG)
    p.write_bytes(buf.getvalue())
    return p


def test_embedded_cover_from_opf_properties(tmp_path):
    p = _epub_with_cover(tmp_path, declared=True)
    assert covers.embedded_cover(str(p)) == JPEG


def test_embedded_cover_fallback_to_named_file(tmp_path):
    p = _epub_with_cover(tmp_path, declared=False)        # no OPF declaration → name match
    assert covers.embedded_cover(str(p)) == JPEG


def test_embedded_cover_skips_non_epub(tmp_path):
    p = tmp_path / "x.pdf"; p.write_bytes(b"%PDF")
    assert covers.embedded_cover(str(p)) is None


# ── first-page render + refresh-on-open ──────────────────────────────────────────
def _png(w, h):
    import struct, zlib
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    def chunk(typ, data):
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def test_image_dims_and_lowres(tmp_path):
    small = tmp_path / "s.png"; small.write_bytes(_png(128, 205))
    big = tmp_path / "b.png"; big.write_bytes(_png(600, 900))
    assert covers.image_dims(str(small)) == (128, 205)
    assert covers.is_lowres(str(small)) is True            # max side 205 < 400
    assert covers.is_lowres(str(big)) is False


def test_refresh_from_file_fills_and_upgrades(tmp_path, monkeypatch):
    cache = str(tmp_path / "cc")
    rendered = _png(600, 900)
    monkeypatch.setattr(covers, "first_page_image", lambda p, **k: rendered)
    book = tmp_path / "x.pdf"; book.write_bytes(b"%PDF")
    # (1) no cover yet → fills from first page
    assert covers.refresh_from_file(cache, "e1", str(book)) == "firstpage"
    assert covers.cached_path(cache, "e1")
    # (2) low-res cover present → upgraded
    covers.write_cache(cache, "e2", _png(128, 205))
    assert covers.refresh_from_file(cache, "e2", str(book)) == "firstpage"
    # (3) good cover present → kept (no render)
    covers.write_cache(cache, "e3", _png(600, 900))
    assert covers.refresh_from_file(cache, "e3", str(book)) is None
    # (4) no local file → nothing
    assert covers.refresh_from_file(cache, "e4", str(tmp_path / "missing.pdf")) is None


def test_firstpage_provider_is_last_and_needs_file():
    p = covers.providers(covers.COVER)[-1]
    assert p.name == "firstpage" and p.needs_file is True


# ── cache + miss marker ──────────────────────────────────────────────────────────
def test_cache_roundtrip_and_miss(tmp_path):
    c = str(tmp_path / "cache")
    assert covers.cached_path(c, "e1") is None
    p = covers.write_cache(c, "e1", PNG)
    assert p.endswith(".png") and covers.cached_path(c, "e1") == p
    covers.mark_miss(c, "e2")
    assert covers.is_missed(c, "e2") is True
    covers.write_cache(c, "e2", JPEG)                     # a hit clears the miss marker
    assert covers.is_missed(c, "e2") is False


# ── placeholder tile ─────────────────────────────────────────────────────────────
def test_placeholder_svg_contains_title_and_author():
    svg = covers.placeholder_svg("The Power of Mantra", "Lama Zopa Rinpoche").decode()
    assert svg.startswith("<svg") and "Mantra" in svg and "Lama Zopa Rinpoche" in svg


# ── spine (constructed, vertical) ─────────────────────────────────────────────────
def test_spine_svg_is_tall_narrow_with_vertical_title_no_author():
    svg = covers.spine_svg("Tantric Ethics").decode()
    assert svg.startswith("<svg")
    assert 'width="64"' in svg and 'height="300"' in svg       # tall + narrow
    assert "rotate(90)" in svg                                 # title set vertically
    assert "Tantric Ethics" in svg
    assert svg.count("<text") == 1                             # title only — no author


def test_spine_svg_uses_given_bg_else_palette():
    assert b'fill="#123456"' in covers.spine_svg("X", bg="#123456")
    # No bg → a deterministic palette colour (same picker as the placeholder tile).
    fallback = covers.spine_svg("X").decode()
    assert ('fill="%s"' % covers._palette_for("X")) in fallback


def test_spine_svg_long_title_truncated():
    svg = covers.spine_svg("Word " * 60).decode()             # far longer than fits
    assert "…" in svg


def _solid_png(rgb, w=40, h=60):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, "PNG")
    return buf.getvalue()


def test_dominant_color_picks_book_colour():
    col = covers.dominant_color(_solid_png((200, 30, 30)))      # mostly red
    assert col and col.startswith("#")
    r = int(col[1:3], 16)
    assert r > col_g(col) and r > col_b(col)                    # stays red-dominant
    assert covers.dominant_color(b"not an image") is None       # undecodable → None


def col_g(c): return int(c[3:5], 16)
def col_b(c): return int(c[5:7], 16)


def test_cover_spine_art_builds_png_from_cover():
    png = covers.cover_spine_art(_solid_png((40, 90, 160), w=120, h=180))
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"             # composited raster ground
    assert covers.cover_spine_art(b"not an image") is None


def test_spine_svg_embeds_art_as_image_and_keeps_vector_title():
    art = covers.cover_spine_art(_solid_png((40, 90, 160), w=120, h=180))
    svg = covers.spine_svg("Deep Book", art_png=art).decode()
    assert svg.startswith("<svg") and "<image" in svg and "data:image/png;base64," in svg
    assert "Deep Book" in svg and "rotate(90)" in svg          # title still crisp vector


def test_fetch_spine_tints_from_cover_through_cover_layer():
    # The spine provider reuses fetch_cover (same layer) to get a colour, then constructs.
    ol = "https://covers.openlibrary.org/b/isbn/S-L.jpg?default=false"
    got = covers.fetch_spine("S", title="Red Book",
                             opener=_opener({ol: _solid_png((180, 20, 20))}))
    assert got and got[1] == "spine-constructed"
    assert got[0].startswith(b"<svg") and b"Red Book" in got[0]


def test_make_spine_with_and_without_cover():
    with_cover = covers.make_spine("Book", _solid_png((30, 80, 150), w=120, h=180))
    assert with_cover.startswith(b"<svg") and b"<image" in with_cover     # cover-derived art
    plain = covers.make_spine("Book")
    assert plain.startswith(b"<svg") and b"<image" not in plain           # palette, no art


def test_fetch_spine_falls_back_to_palette_with_no_cover():
    got = covers.fetch_spine("Z", title="No Cover", opener=_opener({}))   # nothing found
    assert got and got[1] == "spine-constructed" and b"No Cover" in got[0]


def test_spine_svg_cached_as_svg(tmp_path):
    c = str(tmp_path / "cache")
    p = covers.write_cache(c, "spine-e1", covers.spine_svg("Book"))
    assert p.endswith(".svg") and covers.cached_path(c, "spine-e1") == p


# ── route ─────────────────────────────────────────────────────────────────────────
@pytest.fixture
def app(tmp_path):
    from catalogue.webui.web import create_app
    a = create_app(tmp_path / "web.db"); a.testing = True
    return a


def _edition(app, *, isbn=None, title="T"):
    conn = connect(app.config["DB_PATH"])
    eid = conn.execute("INSERT INTO edition (title, isbn) VALUES (?, ?)", (title, isbn)).lastrowid
    conn.commit(); conn.close()
    return eid


def test_route_serves_isbn_cover_then_caches(app, monkeypatch):
    eid = _edition(app, isbn="9780861712908", title="Tantric Ethics")
    calls = {"n": 0}
    def fake_fetch(isbn, **k):
        calls["n"] += 1
        return (JPEG, "openlibrary") if isbn == "9780861712908" else None
    monkeypatch.setattr(covers, "fetch_cover", fake_fetch)
    with app.test_client() as c:
        r1 = c.get(f"/edition/{eid}/cover.jpg")
        r2 = c.get(f"/edition/{eid}/cover.jpg")
    assert r1.status_code == 200 and r1.data == JPEG
    assert r2.status_code == 200 and r2.data == JPEG
    assert calls["n"] == 1                                 # second served from cache, no re-fetch


def test_route_placeholder_when_no_cover(app, monkeypatch):
    eid = _edition(app, isbn=None, title="No Cover Book")
    monkeypatch.setattr(covers, "fetch_cover", lambda *a, **k: None)
    with app.test_client() as c:
        r = c.get(f"/edition/{eid}/cover.jpg")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml"
    assert b"No Cover Book" in r.data


def test_route_serves_spine_and_caches(app, monkeypatch):
    eid = _edition(app, isbn="9780861712908", title="Tantric Ethics")
    calls = {"n": 0}
    def fake_spine(isbn, **k):
        calls["n"] += 1
        return (b'<svg xmlns="http://www.w3.org/2000/svg">spine</svg>', "spine-constructed")
    monkeypatch.setattr(covers, "fetch_spine", fake_spine)
    with app.test_client() as c:
        r1 = c.get(f"/edition/{eid}/spine.svg")
        r2 = c.get(f"/edition/{eid}/spine.svg")
    assert r1.status_code == 200 and r1.mimetype == "image/svg+xml"
    assert b"spine" in r1.data and r2.status_code == 200
    assert calls["n"] == 1                                 # second served from the spine cache


def test_route_spine_reuses_cached_cover_without_network(app, monkeypatch):
    eid = _edition(app, isbn="9780861712908", title="Tantric Ethics")
    # Pre-seed the cover cache for this edition; the spine route must build from it and
    # NOT call the network-backed spine provider.
    covers.write_cache(app.config["COVERS_CACHE"], f"e{eid}",
                       _solid_png((30, 80, 150), w=120, h=180))
    def boom(*a, **k):
        raise AssertionError("fetch_spine should not run when a cover is cached")
    monkeypatch.setattr(covers, "fetch_spine", boom)
    with app.test_client() as c:
        r = c.get(f"/edition/{eid}/spine.svg")
    assert r.status_code == 200 and r.mimetype == "image/svg+xml"
    assert b"<image" in r.data                              # cover-derived art from the cache


# ── normalize_cover: downscale + re-encode policy ─────────────────────────────────
def test_normalize_cover_downscales_oversized():
    from PIL import Image
    buf = io.BytesIO()
    Image.effect_noise((1400, 2100), 50).convert("RGB").save(buf, "JPEG", quality=95)
    raw = buf.getvalue()
    out = covers.normalize_cover(raw)
    w, h = Image.open(io.BytesIO(out)).size
    assert max(w, h) <= covers.COVER_MAX_DIM           # long side capped
    assert h > w                                        # portrait aspect preserved (no crop to square)
    assert out[:3] == b"\xff\xd8\xff" and len(out) < len(raw)


def test_normalize_cover_passes_small_jpeg_through_unchanged():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (120, 60, 180)).save(buf, "JPEG", quality=82)
    raw = buf.getvalue()
    assert covers.normalize_cover(raw) is raw           # already lean + in-spec → untouched object


def test_normalize_cover_leaves_svg_untouched():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg">x</svg>'
    assert covers.normalize_cover(svg) is svg


def test_normalize_cover_never_bloats():
    for raw in (_solid_png((20, 20, 20), 600, 900), _solid_png((255, 255, 255), 50, 50)):
        assert len(covers.normalize_cover(raw)) <= len(raw)


def test_normalize_cover_flattens_transparency_when_downscaling():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (1200, 1800), (200, 30, 30, 128)).save(buf, "PNG")
    out = covers.normalize_cover(buf.getvalue())
    im = Image.open(io.BytesIO(out))
    assert im.mode == "RGB" and max(im.size) <= covers.COVER_MAX_DIM
