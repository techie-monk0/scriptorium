#!/usr/bin/env bash
# Bring the catalogue server up. Two modes:
#   • default  — server on 0.0.0.0:8000 + the named Cloudflare tunnel, for a SYNC SESSION with the
#                offline-first PWA (https://your-domain.example/app). Requires auth (won't expose open).
#   • --local  — server on 127.0.0.1:8000 ONLY: no LAN, no tunnel, no login. A private local run.
# Ctrl-C stops everything (the server, and the tunnel in default mode).
#
# Usage:
#   bash scripts/library-serve.sh            # server (0.0.0.0:8000) + public tunnel; auth REQUIRED
#   bash scripts/library-serve.sh --local    # server on 127.0.0.1:8000 only — no LAN, no tunnel, no auth
#   bash scripts/library-serve.sh --perflog  # add [PERF] per-request tracing (combine with either)
#   bash scripts/library-serve.sh --force    # if :8000 is busy, kill the old server without asking
#   (wrap in `caffeinate -s` to keep the Mac awake while syncing; does NOT stop lid-close sleep.)
set -euo pipefail

# Repo root = parent of this script's dir, so the server runs wherever the repo is cloned.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
# The DB is user-supplied and lives outside git. Honor a pre-set $CATALOGUE_DB
# (or $CATALOGUE_DATA_DIR, read by the app); otherwise default to the repo-local
# catalogue-db/ dir. See db_store.default_db_path().
export CATALOGUE_DB="${CATALOGUE_DB:-$REPO/catalogue-db/catalogue.db}"
# The app now runs inside the uv workspace (post-reorg); make sure uv is on PATH.
export PATH="$HOME/.local/bin:$PATH"

# ── arguments ────────────────────────────────────────────────────────────────
LOCAL=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --local)   LOCAL=1 ;;
    --perflog) export CATALOGUE_PERFLOG=1 ;;   # server emits [PERF] lines → stderr
    --force)   FORCE=1 ;;                       # kill a stale :8000 server without the confirm prompt
    -h|--help) sed -n '7,12p' "$0" | sed 's/^#\s\{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (accepts --local, --perflog, --force)" >&2; exit 2 ;;
  esac
done

# ── mode: bind scope + auth ──────────────────────────────────────────────────
if [ "$LOCAL" = 1 ]; then
  # Loopback only — unreachable from the LAN or the internet — so run OPEN (no login friction).
  # auth.py fail-closes without credentials; CATALOGUE_ALLOW_OPEN=1 is the sanctioned localhost
  # opt-in that lets it start open on purpose.
  HOST="127.0.0.1"
  export CATALOGUE_ALLOW_OPEN=1
  # "Open on loopback" is private ONLY if nothing bridges it out. The named tunnel forwards the
  # public internet to 127.0.0.1:8000 — exactly where we bind — so a live connector would expose
  # this UNAUTHENTICATED server to the world. Refuse rather than silently leak.
  if pgrep -f "cloudflared tunnel run library" >/dev/null 2>&1; then
    echo "⛔ --local runs WITHOUT auth, but the 'library' Cloudflare tunnel is up and would forward"
    echo "   the public internet straight to it (→ 127.0.0.1:8000). Stop the tunnel first:"
    echo "     launchctl bootout gui/\$(id -u)/com.example.catalogue.tunnel   # disable the auto-respawn agent"
    echo "     pkill -f 'cloudflared tunnel run library'                  # kill the live connector"
    exit 1
  fi
  echo "  --local: 127.0.0.1 only (no LAN, no tunnel); running WITHOUT auth (safe on loopback)."
else
  # Public path: gate the tunnel with a signed-cookie login (auth.py default; set CATALOGUE_AUTH=basic
  # for old-style HTTP Basic). Put credentials in ~/.catalogue-auth (chmod 600):
  #     export CATALOGUE_AUTH_USER=you
  #     export CATALOGUE_AUTH_PASS=a-long-random-passphrase
  # Sourced here so they're set for the Flask process.
  HOST="0.0.0.0"
  # Log the REAL client IP (CF-Connecting-IP / X-Forwarded-For) instead of the tunnel's 127.0.0.1,
  # so you can see who's hitting the public URL (e.g. a scraper the auth gate is 401ing).
  export CATALOGUE_ACCESS_LOG=1
  [ -f "$HOME/.catalogue-auth" ] && source "$HOME/.catalogue-auth"
  if [ -z "${CATALOGUE_AUTH_USER:-}" ] || [ -z "${CATALOGUE_AUTH_PASS:-}" ]; then
    echo "⛔ CATALOGUE_AUTH_USER/PASS not set — refusing to expose an UNAUTHENTICATED app at"
    echo "   https://your-domain.example. Create ~/.catalogue-auth (see the header of this script),"
    echo "   or run a private localhost-only server:  bash scripts/library-serve.sh --local"
    exit 1
  fi
fi
[ -n "${CATALOGUE_PERFLOG:-}" ] && echo "  [PERF] tracing on (server [PERF] lines → stderr)"

# ── free port 8000 ───────────────────────────────────────────────────────────
# A leftover server on :8000 would make the new Flask process die with "Address already in use"
# (and, in default mode, the tunnel would keep forwarding to the STALE one). If busy, ASK first.
if lsof -ti:8000 >/dev/null 2>&1; then
  echo "⚠️  Port 8000 is already in use (probably a previous server):"
  lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null | sed 's/^/      /'
  if [ "$FORCE" = 1 ]; then
    ans="y"; echo "    --force: killing the old server without asking."
  else
    printf "    Kill it and restart? [y/N] "; read -r ans
  fi
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    pkill -f "catalogue.webui.web" 2>/dev/null || true
    lsof -ti:8000 2>/dev/null | xargs -r kill 2>/dev/null || true
    sleep 1
    if lsof -ti:8000 >/dev/null 2>&1; then   # stubborn → force
      lsof -ti:8000 2>/dev/null | xargs -r kill -9 2>/dev/null || true; sleep 1
    fi
    echo "    ✓ stopped the old server."
  else
    echo "    Left it running — the new server can't start while :8000 is taken. Exiting."
    exit 1
  fi
fi

# ── run ──────────────────────────────────────────────────────────────────────
# Flask server (no reloader). Backgrounded; killed when this script exits.
uv run python -c "from catalogue.webui.web import create_app; create_app().run(host='$HOST', port=8000, threaded=True, use_reloader=False)" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM

if [ "$LOCAL" = 1 ]; then
  echo "▶ server pid $SERVER_PID on http://localhost:8000  (localhost only — no tunnel)"
  echo "  Ctrl-C to stop."
  wait "$SERVER_PID"
else
  echo "▶ server pid $SERVER_PID on http://localhost:8000  →  https://your-domain.example/app"
  echo "  Ctrl-C to stop the server + tunnel."
  # Clear a stray connector from a previous run (a duplicate is harmless, just tidy), then run
  # the tunnel in the foreground so Ctrl-C tears everything down via the trap.
  pkill -f "cloudflared tunnel run" 2>/dev/null || true
  exec cloudflared tunnel run library
fi
