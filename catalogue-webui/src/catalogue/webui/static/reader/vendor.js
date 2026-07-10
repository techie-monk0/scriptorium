/* reader-core — vendored-library loaders (Phase 1 of reader_module_plan.md).
 *
 * The single place that loads the vendored pdf.js / epub.js (+ jszip) and memoises the
 * resulting globals. Shared by reader-core.js and (next sub-step) the PWA reader, so the
 * two readers stop carrying their own near-identical copies of this. No build step — this
 * is a plain <script src> that publishes window.ReaderVendor, matching the repo convention
 * (window.LibraryCore, window.Reader, …).
 */
'use strict';
(function () {
  let _pdfjs = null, _epubFn = null, _freehand = null;

  function loadScript(src) {
    return new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = src; s.onload = res; s.onerror = () => rej(new Error('load ' + src));
      document.head.appendChild(s);
    });
  }
  async function ensurePdfjs() {
    if (_pdfjs) return _pdfjs;
    // PDF.js 5.x ships as an ES module (no more window.pdfjsLib) — dynamic-import it. The file
    // is served with a .js name so Flask hands it a text/javascript MIME (needed for both the
    // import and PDF.js's module worker). Dynamic import() is allowed from this classic script.
    _pdfjs = await import('/static/vendor/pdf.min.js');
    _pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdf.worker.min.js';
    return _pdfjs;
  }
  async function ensureEpub() {
    if (_epubFn) return _epubFn;
    await loadScript('/static/vendor/jszip.min.js');
    await loadScript('/static/vendor/epub.min.js');
    _epubFn = window.ePub;
    return _epubFn;
  }

  async function ensureFreehand() {
    if (_freehand) return _freehand;
    // perfect-freehand ships as an ES module — dynamic-import it and memoise `getStroke`.
    // Used only by the overlay impls to shape captured points into a render outline.
    const mod = await import('/static/vendor/perfect-freehand.min.js');
    _freehand = mod.getStroke || mod.default;
    return _freehand;
  }

  window.ReaderVendor = { loadScript, ensurePdfjs, ensureEpub, ensureFreehand };
})();
