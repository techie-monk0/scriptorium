/* octavo EPUB bridge — runs INSIDE the WKWebView page that hosts epub.js. The Swift
 * `EpubWebNavigator` drives it via `window.octavoBridge.*` (evaluateJavaScript) and receives events
 * on the `octavoBridge` WKScriptMessageHandler. It mirrors the web/PWA epub.js pipeline so the CFIs
 * are identical across bindings (the OS-P parity gate). The book + its internal resources are fetched
 * from the `octavo-epub://` custom scheme, served by `SourceSchemeHandler` from the injected Source. */
(function () {
  "use strict";

  function post(msg) {
    try { window.webkit.messageHandlers.octavoBridge.postMessage(msg); } catch (e) {}
  }

  var book = null, rendition = null, toc = [], fontPct = 100, theme = null;

  function flattenToc(items, out) {
    (items || []).forEach(function (it) {
      out.push({ title: (it.label || "").trim(), cfi: it.href });
      if (it.subitems && it.subitems.length) flattenToc(it.subitems, out);
    });
    return out;
  }

  // Robust internal-link navigation — mirrors reader-core's `epubGo`. Real books' TOC hrefs don't
  // always match a spine href directly (a nav doc in another folder yields '../text/x.xhtml'; leading
  // './' or '/'; url-encoding) and a `display()` that can't resolve just rejects, so the link "does
  // nothing". Try the href as-is (keeps the #fragment), then resolve a spine section by looser keys,
  // finally by basename across the spine.
  function epubGo(href) {
    if (!rendition || !href) return;
    var p; try { p = rendition.display(href); } catch (e) { p = Promise.reject(e); }
    Promise.resolve(p).catch(function () {
      var bare = href.split("#")[0];
      var cands = [bare, bare.replace(/^\.\//, ""), bare.replace(/^\/+/, ""),
                   bare.replace(/^(\.\.\/)+/, ""), decodeURIComponent(bare)];
      for (var i = 0; i < cands.length; i++) {
        var sec = null; try { sec = book.spine.get(cands[i]); } catch (e) {}
        if (sec) { try { rendition.display(sec.index).catch(function () {}); } catch (e) {} return; }
      }
      var basefile = bare.split("/").pop();
      var items = (book.spine && book.spine.spineItems) || [];
      for (var j = 0; j < items.length; j++) {
        var s = items[j];
        if (((s.href || "").split("/").pop() === basefile) || ((s.url || "").split("/").pop() === basefile)) {
          try { rendition.display(s.index).catch(function () {}); } catch (e) {} return;
        }
      }
    });
  }

  // FALLBACK tap binder (top frame → the section's same-origin iframe document). The PRIMARY path is a
  // WKUserScript injected into every frame (EpubWebNavigator); this covers the case where that script
  // doesn't run in the content iframe (srcdoc/write). Coordinated by `document.__octavoTapBound` so
  // exactly one path binds. Only handles LINKS + a blank tap (toggle); PAGING is the native swipe. Link
  // navigation uses epubGo directly (this runs in the top frame, where rendition lives).
  function bindTaps(doc, win) {
    if (!doc || doc.__octavoTapBound) return;
    doc.__octavoTapBound = true;
    var sx = 0, sy = 0, moved = false;
    doc.addEventListener("touchstart", function (e) {
      if (e.touches && e.touches.length) { sx = e.touches[0].clientX; sy = e.touches[0].clientY; moved = false; }
    }, { passive: true });
    doc.addEventListener("touchmove", function (e) {
      if (e.touches && e.touches.length &&
          (Math.abs(e.touches[0].clientX - sx) > 8 || Math.abs(e.touches[0].clientY - sy) > 8)) moved = true;
    }, { passive: true });
    doc.addEventListener("touchend", function (e) {
      if (moved) return;                                         // swipe/scroll → native; ignore
      var a = (e.target && e.target.closest) ? e.target.closest("a[href]") : null;
      if (a) {
        var href = a.getAttribute("href") || "";
        e.preventDefault(); if (e.stopPropagation) e.stopPropagation();
        if (/^(https?:|mailto:|tel:)/i.test(href)) post({ type: "link", href: href });
        else if (href && href.charAt(0) !== "#") epubGo(href);
      }
      // a blank tap toggles the bars via a NATIVE SwiftUI tap gesture — not posted here.
    }, { passive: false });
    doc.addEventListener("click", function (e) {                 // trackpad/mouse fallback for links
      var a = (e.target && e.target.closest) ? e.target.closest("a[href]") : null;
      if (!a) return;
      var href = a.getAttribute("href") || ""; e.preventDefault();
      if (/^(https?:|mailto:|tel:)/i.test(href)) post({ type: "link", href: href });
      else if (href && href.charAt(0) !== "#") epubGo(href);
    }, false);
  }

  // Swift calls this once the page + epub.js have loaded, passing the book's custom-scheme URL.
  window.octavoOpen = function (url) {
    try {
      book = ePub(url);
      rendition = book.renderTo("viewer", {
        width: "100%", height: "100%", flow: "paginated", spread: "none"
      });
      // Fallback binding per section (primary is the injected WKUserScript; see bindTaps).
      rendition.hooks.content.register(function (contents) {
        try { bindTaps(contents.document, contents.window); } catch (err) {}
      });
      rendition.display();
      rendition.on("relocated", function (loc) {
        // Re-assert theme + font: a newly-displayed section starts from its own stylesheet defaults.
        if (theme) window.octavoBridge.setTheme(theme.bg, theme.fg);
        if (fontPct !== 100) window.octavoBridge.setFont();
        var start = loc && loc.start;
        post({ type: "relocated",
               cfi: start ? start.cfi : null,
               progression: start ? start.percentage : null });
      });
      book.ready
        .then(function () { return book.loaded.navigation; })
        .then(function (nav) {
          var items = (nav && nav.toc) || (book.navigation && book.navigation.toc) || [];
          toc = flattenToc(items, []);
          post({ type: "ready", toc: toc });
        })
        .catch(function (e) {
          // Navigation failed to parse — still report ready (empty TOC) so the reader isn't blocked.
          post({ type: "ready", toc: [], error: String(e) });
        });
    } catch (e) {
      post({ type: "error", message: String(e) });
    }
  };

  // Command surface the Swift navigator drives.
  window.octavoBridge = {
    // Navigate to a TOC href / CFI / percentage. epub.js `display` accepts any of these; swallow a
    // rejection (a bad target) so a mis-resolved TOC link is a no-op, not an unhandled promise.
    display: function (target) {
      if (rendition && target != null && target !== "") {
        try { var p = rendition.display(target); if (p && p.catch) p.catch(function () {}); } catch (e) {}
      }
    },
    // Robust internal-link navigation, called by native when the injected WKUserScript (which can't
    // reach `rendition` from the content frame) reports an internal-link tap.
    go: function (href) { epubGo(href); },

    // Seek to a fraction (0..1) of the book — for "Go to position". Uses generated locations if present,
    // else falls back to the spine item at that fraction (approximate, but no expensive generation).
    gotoFraction: function (f) {
      if (!book || !rendition) return;
      try {
        if (book.locations && book.locations.length && book.locations.length()) {
          var cfi = book.locations.cfiFromPercentage(f);
          if (cfi) { rendition.display(cfi); return; }
        }
        var items = (book.spine && book.spine.spineItems) || [];
        if (items.length) {
          var idx = Math.min(items.length - 1, Math.max(0, Math.floor(f * items.length)));
          rendition.display(items[idx].index);
        }
      } catch (e) {}
    },
    next: function () { if (rendition) rendition.next(); },
    prev: function () { if (rendition) rendition.prev(); },
    outline: function () { return toc; },

    // Font size. `themes.fontSize` alone only sizes <body>, so a book that hard-codes font-size on its
    // text elements changes only spacing (not the glyphs). We ALSO inject a stylesheet into every
    // rendered section forcing the common text elements to inherit the scaled base — so letters resize.
    setFont: function () {
      if (!rendition) return fontPct;
      var css = "html,body{font-size:" + fontPct + "% !important;}" +
        "p,li,dd,dt,blockquote,span,a,em,strong,i,b,small,sub,sup,td,th,cite,q,figcaption,label" +
        "{font-size:inherit !important;}";
      try {
        rendition.themes.fontSize(fontPct + "%");          // future sections + triggers relayout
        (rendition.getContents() || []).forEach(function (c) {
          try {
            var doc = c && c.document; if (!doc) return;
            var prev = doc.getElementById("octavo-font"); if (prev) prev.remove();
            var st = doc.createElement("style"); st.id = "octavo-font"; st.textContent = css;
            (doc.head || doc.documentElement).appendChild(st);
          } catch (e) {}
        });
      } catch (e) {}
      return fontPct;
    },
    bigger: function () { fontPct = Math.min(250, fontPct + 10); return window.octavoBridge.setFont(); },
    smaller: function () { fontPct = Math.max(60, fontPct - 10); return window.octavoBridge.setFont(); },

    // Reading theme (bg/fg). epub.js `themes.override(property, value, priority)` injects the property
    // into every rendered section's body — the SAME form the web reader-core uses (the earlier
    // (selector, rulesObject) form silently did nothing). Stored so `relocated` can re-assert it.
    setTheme: function (bg, fg) {
      theme = { bg: bg, fg: fg };
      if (!rendition) return;
      try {
        rendition.themes.override("color", fg, true);
        rendition.themes.override("background", bg, true);
      } catch (e) {}
    },

    // Text marks (highlight/underline/strikethrough) over a CFI range — epub.js `annotations`.
    // `EpubDecorationHost` drives these from postilla `Decoration`s (carrying the cfiRange).
    addMark: function (type, cfiRange, color) {
      if (!rendition || !cfiRange) return;
      var styles = type === "highlight" ? { fill: color || "#ffd54a" }
                                        : { stroke: color || "#1565c0" };
      try { rendition.annotations.add(type, cfiRange, {}, undefined, "octavo-mark", styles); } catch (e) {}
    },
    removeMark: function (type, cfiRange) {
      if (!rendition || !cfiRange) return;
      try { rendition.annotations.remove(cfiRange, type); } catch (e) {}
    },

    // Full-text search across the spine → [{cfi, excerpt}] (epub.js per-section `find`).
    search: function (query) {
      if (!book || !query) return Promise.resolve([]);
      var results = [];
      return book.ready.then(function () {
        return Promise.all(book.spine.spineItems.map(function (item) {
          return item.load(book.load.bind(book))
            .then(function () {
              (item.find(query) || []).forEach(function (m) {
                results.push({ cfi: m.cfi, excerpt: m.excerpt });
              });
              item.unload();
            })
            .catch(function () {});
        }));
      }).then(function () { return results; });
    }
  };
})();
