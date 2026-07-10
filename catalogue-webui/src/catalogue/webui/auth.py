"""Pluggable access control — the ONE seam every request passes through to be allowed or
rejected. The auth PROTOCOL is swappable without touching routes or `create_app`:

    create_app → auth.install(app)            # registers a single before_request gate
    install    → provider_from_env()          # picks the provider from CATALOGUE_AUTH
    provider.check() -> Response | None        # None = allow; Response = reject (challenge)
    provider.install_routes(app)               # a provider may add its OWN routes (e.g. /login)

To change how the app is protected, add an `AuthProvider` subclass (e.g. a bearer-token
provider, a Cloudflare-Access JWT verifier, or mTLS) and one branch in `provider_from_env` —
or inject an instance directly: `auth.install(app, MyProvider(...))`. Nothing else changes.
Crucially, a provider owns EVERYTHING it needs (its gate AND any login/logout routes), so the
whole mechanism lives below this seam — routes and `create_app` never learn the protocol.

Why a signed cookie by default (not Basic, not Cloudflare Access):
  • Cloudflare Access 302-redirects every request to a cross-origin login a PWA's fetch()/sync
    can't complete.
  • HTTP Basic has no concept of a session lifetime — an iOS home-screen PWA gets a fresh,
    short-lived session each cold launch, so the browser re-prompts on EVERY open, and the
    native dialog can double-fire when a navigation and a sub-resource race.
  • A same-origin signed cookie auto-attaches to every same-origin request (incl. fetch()/sync,
    so offline launch + the content-index download still work) AND carries an explicit max-age,
    so the PWA stays logged in for weeks across launches and re-auths through a single form.
The seam means that's just today's choice, not a lock-in.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
from urllib.parse import quote

from flask import Response, g, redirect, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


# ── Roles ─────────────────────────────────────────────────────────────────────
# Two tiers, decided per-request by WHICH credential authenticated:
#   • editor — the owner; full read + write (review, edit, merge, delete, capture…).
#   • viewer — a guest; READ-ONLY. Can search/browse/read books, but every mutating
#     request is rejected server-side (see the read-only gate in install()).
# Open access (NoAuth: local dev / the owner's own Mac) is treated as editor.
# The role is exposed to ALL clients via /api/v1/health, so the web UI, the PWA, and a
# native client each inherit the same capabilities without per-client wiring — and even
# a client that ignores it can't write, because the server gate is the real boundary.
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"

# A viewer may only issue these (non-mutating) HTTP methods; everything else is a write.
# Every write in this app is a POST, so method-gating covers all current AND future write
# routes with no per-route bookkeeping.
_VIEWER_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Read-only is NOT the whole story for a viewer (a guest, e.g. a friend): blocking writes still
# leaves every review/curation/ingest/settings PAGE reachable on GET (they just render with edit
# controls hidden). A guest should see only the BROWSE-AND-READ surface, so viewer GETs are
# DEFAULT-DENY — allowed only for the endpoints named here. The win of default-deny: any future
# admin/review page is off-limits to a guest automatically (no new bookkeeping), the inverse of
# the method gate's "every write is a POST" trick. Keys are Flask endpoint names (the view-fn
# name; URL params are irrelevant), so they're stable across path edits. Everything absent —
# /review, /works/detect, /picker, /staging, /reconcile, /capture, /settings, /integrity,
# authority/merge/structure surfaces, the add-book form — is denied to a guest.
_VIEWER_GET_ALLOW = frozenset({
    # The provider's own login/logout (also reachable via check(); listed for clarity).
    "_auth_login", "_auth_logout",
    # Top-level browse/read pages + their back-compat alias and list views.
    "dashboard", "search", "text_search", "library", "people_list", "works_list",
    # By-author/translator + by-subject browse index and its edition cover interstitial.
    "browse_by_author", "edition_coverpage",
    # Entity detail pages and the read-only card/summary fragments those pages lazy-load.
    "edition_detail", "edition_card", "edition_works_summary",
    "holding_card",
    "work_detail", "work_card", "work_summary_card",
    "person_detail", "person_card", "person_treasuryoflives",
    "subject_browse", "subject_card", "subjects_card",
    # Cover/spine art, file streaming, in-app readers, and reading-position read-back.
    "edition_cover", "edition_spine", "holding_preview", "holding_file",
    "edition_read", "holding_read", "holding_position_get",
    # Read-only search / typeahead JSON the browse pages query.
    "library_suggest_person", "library_suggest_subject", "works_search", "editions_search",
    # Consumption + PWA + status APIs (the offline replica/content index are read-only copies).
    "api_library", "api_content", "api_edition", "api_subjects", "api_subject",
    "api_replica", "api_content_index", "api_health", "health",
    "pwa_app", "pwa_manifest", "pwa_sw",
})


def _truthy(v: "str | None") -> bool:
    """A permissive yes for env-var opt-ins: 1/true/yes/on (any case). Everything else
    — unset, empty, 0, false — is no, so the safe default (auth required) is the default."""
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _ct_eq(a: "str | None", b: "str | None") -> bool:
    """Constant-time equality for credentials that may contain non-ASCII characters.
    `hmac.compare_digest` raises on non-ASCII *str* (e.g. an accented passphrase), so
    compare the UTF-8 *bytes* instead — bytes are always accepted."""
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _is_navigation() -> bool:
    """A top-level page load (→ redirect somewhere friendly) vs an API/asset fetch (→ 4xx, so the
    PWA's fetch()/SW reacts without a redirect dance). Trust Sec-Fetch-Mode when present (all
    modern browsers send it); fall back to the Accept header."""
    mode = request.headers.get("Sec-Fetch-Mode")
    if mode:
        return mode == "navigate"
    return "text/html" in request.headers.get("Accept", "")


def current_role() -> str:
    """The authenticated identity's role for THIS request (editor unless a viewer
    credential signed in). Set by a gating provider's check(); absent ⇒ editor (open
    access / NoAuth = the owner)."""
    return getattr(g, "auth_role", ROLE_EDITOR)


def can_edit() -> bool:
    """True when the current request may mutate (review/edit/delete)."""
    return current_role() != ROLE_VIEWER


def can_download() -> bool:
    """True when the current request may download/keep book files (vs view-only).
    A viewer can READ a book inline but must not get download/save affordances."""
    return current_role() != ROLE_VIEWER


class AuthProvider:
    """Base = open access. Subclasses gate by returning a Response from `check()`."""
    name = "none"
    gates = False                       # True ⇒ install() registers the before_request

    def check(self) -> "Response | None":
        """Return None to ALLOW the current request, or a Response to REJECT it
        (e.g. a 401 challenge or a redirect). Reads the request via flask's globals."""
        return None

    def install_routes(self, app) -> None:
        """Register any routes this provider needs (e.g. a `/login` form). Called by
        `install()` so the whole mechanism stays below the seam. Default: none."""
        return None

    def console_banner(self) -> "str | None":
        """A line (or block) printed to the server console at startup so the operator sees how
        to log in. None = print nothing."""
        return None


class NoAuth(AuthProvider):
    name = "none"
    gates = False


class BasicAuth(AuthProvider):
    """HTTP Basic Auth — simple, but with NO session lifetime: the browser re-prompts whenever
    its (often short-lived) per-origin credential cache is gone, which on an iOS standalone PWA
    is every cold launch. Kept for non-PWA / curl use; `CookieTokenAuth` is the PWA default."""
    name = "basic"
    gates = True

    def __init__(self, user: str, password: str, realm: str = "Library", *,
                 viewer_user: "str | None" = None, viewer_pass: "str | None" = None):
        self._user, self._password, self._realm = user, password, realm
        self._viewer = (viewer_user, viewer_pass) if (viewer_user and viewer_pass) else None
        self._hinted = False            # so the per-request hint prints only once per run

    def console_banner(self):
        viewer = (f"     viewer (read-only): {self._viewer[0]} / {self._viewer[1]}\n"
                  if self._viewer else "")
        return ("\n🔐 Library login required (HTTP Basic Auth — your OWN credential, not Cloudflare):\n"
                f"     username: {self._user}\n"
                f"     password: {self._password}\n"
                f"{viewer}"
                "   Enter these when the browser/PWA prompts. (Source: ~/.catalogue-auth)\n")

    def _role_for(self, u: str, pw: str) -> "str | None":
        if _ct_eq(u, self._user) and _ct_eq(pw, self._password):
            return ROLE_EDITOR
        if self._viewer and _ct_eq(u, self._viewer[0]) and _ct_eq(pw, self._viewer[1]):
            return ROLE_VIEWER
        return None

    def check(self):
        a = request.authorization
        role = None
        if a and (a.type or "").lower() == "basic":
            role = self._role_for(a.username or "", a.password or "")
        if role:
            g.auth_role = role
            return None
        # First unauthenticated hit of this run (e.g. the PWA reaching the server): tell the
        # operator on the console exactly what to enter. Once only, so probes don't spam it.
        if not self._hinted:
            self._hinted = True
            print(f"↪ unauthenticated request to {request.path} — log in with username "
                  f"'{self._user}' / password '{self._password}' (from ~/.catalogue-auth).",
                  file=sys.stderr, flush=True)
        return Response("Authentication required.", 401,
                        {"WWW-Authenticate": f'Basic realm="{self._realm}"'})


class CookieTokenAuth(AuthProvider):
    """Session via a signed, timed cookie set by a same-origin `/login` form.

    The cookie is an itsdangerous-signed token (tamper-proof) carrying the username, with a
    server-checked `max_age`. It auto-attaches to every same-origin request — including the
    PWA's `fetch()`/background sync — so the app stays logged in across launches for the whole
    window and only re-auths (through the form, never a native dialog) when it expires.

    The signing key is derived from the password, so rotating the password instantly
    invalidates every outstanding cookie (no separate secret to manage); override with
    CATALOGUE_AUTH_SECRET if you want stable cookies across a password change.
    """
    name = "cookie"
    gates = True
    COOKIE = "lib_auth"
    LOGIN_PATH = "/login"
    LOGOUT_PATH = "/logout"

    def __init__(self, user: str, password: str, *, max_age_days: int = 90,
                 viewer_user: "str | None" = None, viewer_pass: "str | None" = None):
        self._user, self._password = user, password
        self._viewer = (viewer_user, viewer_pass) if (viewer_user and viewer_pass) else None
        self._max_age = max_age_days * 86400
        # Username → role. The cookie carries the username; a valid signature means it was
        # minted by us, so the username is trusted and a plain dict lookup gives the role.
        self._roles = {user: ROLE_EDITOR}
        if self._viewer:
            self._roles[self._viewer[0]] = ROLE_VIEWER
        # Secret folds in BOTH credentials, so rotating EITHER password instantly
        # invalidates every outstanding cookie. Override with CATALOGUE_AUTH_SECRET.
        secret = (os.environ.get("CATALOGUE_AUTH_SECRET") or "").strip() \
            or hashlib.sha256(f"{user}:{password}:{viewer_user}:{viewer_pass}".encode()).hexdigest()
        self._signer = URLSafeTimedSerializer(secret, salt="lib-auth-v1")

    def console_banner(self):
        viewer = (f"     viewer (read-only): {self._viewer[0]} / {self._viewer[1]}\n"
                  if self._viewer else "")
        return ("\n🔐 Library login (signed-cookie session — your OWN credential, not Cloudflare):\n"
                f"     username: {self._user}\n"
                f"     password: {self._password}\n"
                f"{viewer}"
                f"   Log in once at /login; the PWA then stays signed in for {self._max_age // 86400} days.\n"
                "   (Source: ~/.catalogue-auth)\n")

    # ── session helpers ───────────────────────────────────────────────────────
    def _role(self) -> "str | None":
        """The role of the signed-in identity, or None if no valid session cookie."""
        tok = request.cookies.get(self.COOKIE)
        if not tok:
            return None
        try:
            who = self._signer.loads(tok, max_age=self._max_age)
        except (BadSignature, SignatureExpired):
            return None
        return self._roles.get(who or "")

    def _role_for(self, u: str, pw: str) -> "str | None":
        """The username to seal into the cookie if (u, pw) is a known credential, else None."""
        if _ct_eq(u, self._user) and _ct_eq(pw, self._password):
            return self._user
        if self._viewer and _ct_eq(u, self._viewer[0]) and _ct_eq(pw, self._viewer[1]):
            return self._viewer[0]
        return None

    _is_navigation = staticmethod(_is_navigation)   # provider-agnostic helper (defined module-level)

    def _set_session(self, resp: Response, who: str) -> Response:
        resp.set_cookie(self.COOKIE, self._signer.dumps(who), max_age=self._max_age,
                        httponly=True, secure=request.is_secure, samesite="Lax", path="/")
        return resp

    # ── gate ──────────────────────────────────────────────────────────────────
    def check(self):
        if request.path in (self.LOGIN_PATH, self.LOGOUT_PATH):
            return None                              # the form itself is always reachable
        role = self._role()
        if role:
            g.auth_role = role
            return None
        if self._is_navigation():
            nxt = request.full_path.rstrip("?") or "/app"
            return redirect(f"{self.LOGIN_PATH}?next={quote(nxt, safe='')}")
        return Response("Authentication required.", 401)

    # ── routes (kept below the seam: the provider owns its own login UI) ───────
    def install_routes(self, app):
        @app.route(self.LOGIN_PATH, methods=["GET", "POST"], endpoint="_auth_login")
        def _login():
            nxt = _safe_next(request.values.get("next"))
            if request.method == "POST":
                u, pw = request.form.get("username", ""), request.form.get("password", "")
                who = self._role_for(u, pw)
                if who:
                    return self._set_session(redirect(nxt), who)
                return self._login_page(nxt, error=True), 401
            if self._role():
                return redirect(nxt)
            return self._login_page(nxt)

        @app.route(self.LOGOUT_PATH, endpoint="_auth_logout")
        def _logout():
            resp = redirect(self.LOGIN_PATH)
            resp.delete_cookie(self.COOKIE, path="/")
            return resp

    def _login_page(self, nxt: str, error: bool = False) -> str:
        # Self-contained (inline CSS) so it renders even though /static is gated until login.
        msg = '<p class="err">Wrong username or password.</p>' if error else ""
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><title>Library — Sign in</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font:16px/1.4 -apple-system,system-ui,sans-serif;
         background:#0e0f12; color:#e9e9ec; }}
  @media (prefers-color-scheme: light) {{ body {{ background:#f5f5f7; color:#1c1c1e; }} }}
  form {{ width:min(92vw,340px); padding:26px 22px; border-radius:16px;
         background:rgba(127,127,127,.10); box-shadow:0 12px 40px rgba(0,0,0,.25); }}
  h1 {{ font-size:1.15rem; margin:0 0 4px; }}
  p.sub {{ margin:0 0 18px; opacity:.6; font-size:.85rem; }}
  label {{ display:block; font-size:.8rem; opacity:.7; margin:12px 0 4px; }}
  input {{ width:100%; box-sizing:border-box; padding:11px 12px; font-size:1rem;
          border:1px solid rgba(127,127,127,.35); border-radius:10px;
          background:rgba(127,127,127,.08); color:inherit; }}
  button {{ width:100%; margin-top:20px; padding:12px; font-size:1rem; font-weight:600;
           border:0; border-radius:10px; background:#3b82f6; color:#fff; cursor:pointer; }}
  p.err {{ color:#ff6b6b; font-size:.85rem; margin:10px 0 0; }}
</style></head><body>
<form method="post" action="{self.LOGIN_PATH}">
  <h1>Library</h1><p class="sub">Sign in to continue.</p>
  <input type="hidden" name="next" value="{_html_attr(nxt)}">
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autocapitalize="none"
         autocorrect="off" autofocus required>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>{msg}
</form></body></html>"""


def _safe_next(nxt: "str | None") -> str:
    """Only accept a LOCAL path as the post-login destination (no open redirect)."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/\\"):
        return nxt
    return "/app"


def _html_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def provider_from_env() -> AuthProvider:
    """Select the access provider from the environment.

    CATALOGUE_AUTH = none (default) | cookie | basic
      • 'cookie' (or unset but CATALOGUE_AUTH_USER/PASS present) → CookieTokenAuth (PWA default:
        a signed-cookie session that survives launches).
      • 'basic' → HTTP Basic (no session lifetime; for curl / non-PWA use).
    Future protocols register a new name here (or are injected via install(app, provider))."""
    mode = (os.environ.get("CATALOGUE_AUTH") or "").strip().lower()
    user = os.environ.get("CATALOGUE_AUTH_USER")
    pw = os.environ.get("CATALOGUE_AUTH_PASS")
    # Optional second, READ-ONLY credential for a guest (e.g. a friend). Only honored
    # alongside the editor credential; a viewer pair on its own does nothing.
    vuser = os.environ.get("CATALOGUE_VIEWER_USER")
    vpw = os.environ.get("CATALOGUE_VIEWER_PASS")
    if mode == "basic":
        if not (user and pw):
            raise ValueError("CATALOGUE_AUTH=basic requires CATALOGUE_AUTH_USER and CATALOGUE_AUTH_PASS")
        return BasicAuth(user, pw, viewer_user=vuser, viewer_pass=vpw)
    if mode == "cookie" or (not mode and user and pw):
        if not (user and pw):
            raise ValueError("CATALOGUE_AUTH=cookie requires CATALOGUE_AUTH_USER and CATALOGUE_AUTH_PASS")
        return CookieTokenAuth(user, pw, viewer_user=vuser, viewer_pass=vpw)
    if mode in ("", "none"):
        # Default-DENY. Running open is the ONLY way the whole catalogue leaks through the
        # public tunnel (an app started without creds in its env), so it must be a deliberate,
        # explicit act — never a silent fallback. Refuse to start unless the operator opts in
        # with CATALOGUE_ALLOW_OPEN=1 (localhost dev / the hermetic test suite). The blessed
        # launcher, scripts/library-serve.sh, sources CATALOGUE_AUTH_USER/PASS from
        # ~/.catalogue-auth, so the normal path is gated and never trips this.
        if _truthy(os.environ.get("CATALOGUE_ALLOW_OPEN")):
            return NoAuth()
        raise SystemExit(
            "Refusing to start the catalogue web app WITHOUT authentication.\n"
            "  • To expose it (the normal path): set CATALOGUE_AUTH_USER and CATALOGUE_AUTH_PASS\n"
            "    — `bash scripts/library-serve.sh` sources them from ~/.catalogue-auth for you.\n"
            "  • For a localhost-only dev run with no auth: set CATALOGUE_ALLOW_OPEN=1.\n"
            "Why: an app started open behind the public tunnel serves the entire library to anyone.")
    raise ValueError(f"unknown CATALOGUE_AUTH={mode!r} (expected 'none', 'cookie', or 'basic')")


# PWA bootstrap files: no private data, and they MUST stay fetchable even when logged out, so an
# installed client can always pull the latest service worker / manifest. Without this a logged-out
# device deadlocks — the old worker serves a stale shell but can't fetch the new (gated) sw.js, so
# it never updates and never shows the login. The worker then gates the actual data itself.
_PUBLIC_BOOTSTRAP = ("/sw.js", "/manifest.webmanifest", "/static/pwa/icon.svg")


def install(app, provider: "AuthProvider | None" = None) -> AuthProvider:
    """Register the access gate on `app`. Call this BEFORE the DB-open before_request so an
    unauthenticated request is rejected before any connection is opened. Stores the active
    provider on `app.config['AUTH_PROVIDER']`. A non-gating provider (NoAuth) is a no-op."""
    provider = provider or provider_from_env()
    app.config["AUTH_PROVIDER"] = provider
    if provider.gates:
        provider.install_routes(app)                     # the provider's own login/logout UI
        banner = provider.console_banner()
        if banner:
            print(banner, file=sys.stderr, flush=True)   # shown on server startup

        @app.before_request
        def _auth_gate():
            # App CODE/assets carry no catalogue data and must stay fetchable logged-out, so a
            # stale client can always pull the latest worker + JS/CSS and self-heal. Only the
            # DATA (/api/*, /edition/* art, /holding/* files, the /app shell…)
            # is gated — and the client bounces to /login on the resulting 401.
            p = request.path
            if p in _PUBLIC_BOOTSTRAP or p.startswith("/static/"):
                return None
            rejected = provider.check()        # also sets g.auth_role on the allow path
            if rejected is not None:
                return rejected
            # Guest gate (two layers, both server-side — the real boundary even for a client
            # that ignores the role advertised by /api/v1/health):
            #   1. WRITE block — every write in this app is a POST (login/logout excepted,
            #      handled by check above), so blocking non-safe methods covers all current +
            #      future write routes with no per-route bookkeeping.
            #   2. READ scope — a guest sees only the browse-and-read surface; every other GET
            #      (review/curation/ingest/settings pages) is default-denied via _VIEWER_GET_ALLOW.
            if current_role() == ROLE_VIEWER:
                if request.method not in _VIEWER_SAFE_METHODS:
                    return Response("This is a read-only account — changes are disabled.", 403)
                ep = request.endpoint
                # ep is None for an unmatched path — let Flask raise its own 404 untouched.
                if ep is not None and ep not in _VIEWER_GET_ALLOW:
                    if _is_navigation():
                        return redirect("/")          # a stale link / hidden nav bounces home
                    return Response("This account can only browse and read the library.", 403)
            return None
    return provider
