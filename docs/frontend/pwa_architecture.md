# How the phone app and the Mac talk to each other

*A plain-English map of the library system, for future reference. No tech background needed.*

---

## The big idea (read this first)

Think of it like a **library building** and a **pocket notebook**:

- **Your Mac** is the library building. It holds the *real* catalogue — every book record, and
  the actual book files. It runs a little program (the "server") that hands out information when
  asked.
- **The phone app** (the icon you added to your home screen) is a **pocket copy**. When it can
  reach the Mac, it photocopies the latest catalogue into itself. After that, it can work on its
  own — even with no internet — because it's reading its own pocket copy.
- The phone reaches the Mac through a **secure doorway on the internet** (the web address
  `your-domain.example`), and that doorway is **locked with a password**.

So the phone is *offline-first*: it only needs the Mac now and then, to top up its pocket copy or
to fetch a book you haven't saved yet.

```
   PHONE  (a pocket copy)            THE INTERNET                 YOUR MAC  (the real library)
  ┌───────────────────┐         ┌───────────────────┐         ┌──────────────────────────┐
  │  the app you added │  asks   │   Cloudflare      │ private │  the "server" program    │
  │  to the home screen│ ──────▶ │  = the locked     │ tunnel  │  + the real catalogue     │
  │                    │ ◀────── │    front door     │ ◀─────▶ │    and the book files     │
  │  keeps its own copy│  gets   │  (your-domain.example) │         │                          │
  └───────────────────┘         └───────────────────┘         └──────────────────────────┘
```

The rest of this document zooms into three things: **(1)** how the phone gets its data,
**(2)** what still works with no internet, and **(3)** how the login and web address work.
A **"when something breaks"** section is at the end.

---

## The cast (who's who)

| Name | Plain meaning |
|---|---|
| **The server** | A small program on your Mac that answers questions like "give me the catalogue" or "give me this book." Started with `scripts/library-serve.sh`. |
| **The phone app (PWA)** | A web app you "Add to Home Screen" so it behaves like a normal app and can work offline. |
| **The pocket copy ("replica")** | A lightweight copy of the catalogue *information* (titles, authors, subjects…) stored on the phone. Not the book files — just the cards. |
| **A book file** | The actual PDF/EPUB. Big. Only saved on the phone when you open it (or choose to save it). |
| **The "search-inside-books" pack** | One big download of the *text* of every book, so you can search words *inside* books with no internet. Optional, opt-in. |
| **Cloudflare + the tunnel** | The locked front door on the internet (`your-domain.example`) and the private pipe from it to your Mac. |
| **The login** | A username + password that the *server* checks. Your own, set on the Mac — **not** your Cloudflare account. |

---

## (1) How the phone gets its data

When the phone can reach the Mac, it asks: *"Anything new in the catalogue?"* If yes, it
downloads the latest pocket copy and saves it. That's it — a top-up.

```
  PHONE (has internet)                                  MAC (server running)
     │                                                      │
     │   "what's the latest catalogue?"                     │
     │ ───────────────────────────────────────────────────▶│
     │                                                      │  looks it up
     │            here's the pocket copy (titles, authors…) │
     │ ◀───────────────────────────────────────────────────│
     │                                                      │
     ▼  saves it on the phone
  Now the phone can browse & search on its own, internet or not.
```

Things to know, in plain terms:

- This top-up is **small and quick** — it's just the catalogue cards, not the books.
- It happens when you open the app while the Mac is reachable. If nothing changed, it skips the
  download (it's smart about that).
- **New books you add on the Mac don't appear on the phone until the next top-up.**
- **Book files and the search-inside pack are separate, bigger downloads** (next sections),
  fetched only when you ask for them.

---

## (2) What works with no internet

Because the phone keeps its own pocket copy (and any books/packs you've saved), a lot works with
the Mac asleep or no signal:

```
  ON THE PHONE, WITH NO INTERNET:

    WORKS on its own                        NEEDS the Mac reachable
    ----------------------------------      ----------------------------------
    ✓ Browse the shelves / subjects          ✗ Open a book you've NEVER opened
    ✓ Search titles, authors, subjects         (its file isn't on the phone yet)
    ✓ Re-read books you've opened before     ✗ See brand-new books added on the Mac
    ✓ Search INSIDE books — *if* you've         (until the next top-up)
      downloaded the search-inside pack      ✗ First-time download of a book or
    ✓ Scan ISBNs (they queue up and            the search-inside pack
      send themselves later)
```

How the two "big" things get onto the phone:

- **A book to read offline:** the first time you open a book *while online*, the phone quietly
  saves its file. After that it opens with no internet. (Never-opened books aren't on the phone,
  so they can't open offline.)
- **The search-inside-books pack:** in the app's **Settings → Enable offline content search**,
  the phone downloads the text of the whole library once and stores it. Then "search inside
  books" works offline. It's large, so it's optional and only downloads when you ask.

```
  Reading a book offline:                 Search-inside pack:
    open it once online  ──▶ saved          Settings ▸ Enable  ──▶ one big download
    later: opens offline                    later: search inside books works offline
```

---

## (3) The login and the web address (who can get in)

The phone reaches the Mac over the public address **`your-domain.example`**. You don't want the
whole internet poking at your library, so there's a lock.

```
   Someone on the internet
          │  goes to your-domain.example
          ▼
   ┌────────────────────┐     wrong / no password  ┌─────────────────────────┐
   │  Cloudflare door    │ ───────────────────────▶ │  asks for username +    │
   │  + private tunnel   │                          │  password (try again)   │
   │  to your Mac        │     correct password     └─────────────────────────┘
   └─────────┬──────────┘ ───────────────┐
             │                            ▼
             │ private tunnel      lets the request reach the Mac
             ▼
        YOUR MAC (server) ── checks the password, then answers
```

Plain facts:

- **`your-domain.example`** is your own web address (registered at Cloudflare). It always points to the
  same place, which matters: it means the phone app keeps its saved copy between uses. (A random
  temporary address would make the app forget everything each time.)
- **The tunnel** is a private pipe from Cloudflare to your Mac. Your Mac is never directly exposed
  to the internet — everything comes through the locked door.
- **The login is YOUR library password, not Cloudflare's.** It lives on the Mac in a file called
  `~/.catalogue-auth`, and the server **prints it in the Terminal when it starts** so you always
  know what to type. Enter it once per device; the phone remembers it.
- The Mac only needs to be **on and running the server** when you want to top up, fetch a book, or
  download the search pack. The rest of the time the phone happily uses its own copy.

```
  WHO CAN GET IN
    no password ......... turned away at the door
    your password ....... allowed through to the Mac
    Mac asleep/off ...... nobody gets through (phone still works offline)
```

---

## When something breaks (plain fixes)

| What you see | What it means | What to do |
|---|---|---|
| **"You appear to be offline"** but the Mac is on | The app couldn't reach the server — usually the server isn't running, or the door is still asking for a login the app hasn't passed. | Make sure `scripts/library-serve.sh` is running on the Mac. Reopen the app; enter the username/password when asked. |
| **It keeps asking for a username/password on every open** | Most likely the old Cloudflare *Access* gate is still on, giving you a **second, separate** prompt (see the next row). The app's own login is a signed-cookie session that should keep you in for ~90 days (rotating the Mac password also ends it early). | Delete the Cloudflare Access app (next row) so there's only one login. Then sign in once on the app's own `/login` page (credentials from `~/.catalogue-auth`); the cookie keeps you signed in. |
| **Web page redirects to a "Cloudflare Access" login** | The old Cloudflare *Access* gate is still switched on (it fights with the app). | In Cloudflare → Zero Trust → Access → Applications, **delete** the `your-domain.example` application. The simple username/password takes over. |
| **The search-inside download stopped when I switched screens** | (Already fixed.) The download now keeps running in the background. | Go back to **Settings** — it shows the live progress; it resumes/continues. |
| **The whole address stopped working** | The Mac is asleep / off, or the launcher isn't running (nothing auto-starts). | Wake the Mac and run `scripts/library-serve.sh` again. The phone keeps working offline meanwhile. |
| **I added books on the Mac but the phone doesn't show them** | The phone hasn't topped up its copy yet. | Open the app while the Mac is reachable; it syncs the latest catalogue. |
| **A book won't open on the phone with no internet** | That book's file was never saved on the phone. | Open it once while online (that saves it), then it opens offline. |

---

## Where the pieces live (for rebuilding later)

If you ever need to set this up again, the details are in **`docs/USAGE.md`** ("Remote access").
In short:

- **Start it:** `scripts/library-serve.sh` (starts the server + the tunnel together).
- **The login:** `~/.catalogue-auth` on the Mac (username + password).
- **The web address + tunnel settings:** `~/.cloudflared/config.yml` (points `your-domain.example` at
  the server). The address is a Cloudflare-registered domain, so there was no fiddly DNS to set up.
- **The app + server code:** in this project; the phone app is served at `…/app`.

The deeper "how the app is built" details (shared between web, phone, and a future iPhone-native
app) are in **`private/frontend/frontend_contract.md`** — only needed if someone is changing the code.
