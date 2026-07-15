#if canImport(WebKit)
import XCTest
import WebKit
#if canImport(UIKit)
import UIKit
#elseif canImport(AppKit)
import AppKit
#endif
@testable import CatalogueReader
import Octavo
import OctavoEPUB
import OctavoAdapters

/// DIAGNOSTIC (temporary): localize where the text-selection → `selected` → `onSelection` chain
/// breaks in a live WKWebView. It arms counters at every link — WebKit `selectionchange` on the
/// iframe document, epub.js `Contents` "selected" emit, the rendition relay, and the Swift
/// `onSelection` callback — then makes a real programmatic selection and (phase 2) a synthetic
/// `selectionchange` dispatch, and prints which counters moved. Not an assertion suite; it always
/// "passes" and reports via DIAG lines.
@MainActor
final class EpubSelectionDiagTests: XCTestCase {

    private func minimalEpub() throws -> URL {
        guard let url = Bundle.module.url(forResource: "book", withExtension: "epub", subdirectory: "Fixtures")
                ?? Bundle.module.url(forResource: "book", withExtension: "epub") else {
            throw XCTSkip("book.epub fixture not bundled")
        }
        return url
    }

    private func js(_ nav: EpubWebNavigator, _ script: String) async -> Any? {
        try? await nav.webView.evaluateJavaScript(script)
    }

    func testSelectionChainDiag() async throws {
        let epub = try minimalEpub()
        let nav = EpubWebNavigator(source: FileSource(url: epub), publicationId: "t")
        let frame = CGRect(x: 0, y: 0, width: 400, height: 600)
        nav.webView.frame = frame
        #if canImport(UIKit)
        let win = UIWindow(frame: frame)
        let vc = UIViewController(); vc.view.addSubview(nav.webView)
        win.rootViewController = vc; win.makeKeyAndVisible()
        #elseif canImport(AppKit)
        let win = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
        win.contentView = nav.webView; win.makeKeyAndOrderFront(nil)
        #endif
        defer { _ = win }

        var swiftSelected = 0
        var lastCfi = ""
        nav.onSelection = { cfi, _ in swiftSelected += 1; lastCfi = cfi }

        try await nav.open()
        for _ in 0..<60 {
            let ifr = await js(nav, "document.querySelectorAll('#viewer iframe').length") as? Int ?? 0
            if nav.currentLocation?.locations.cfi != nil && ifr > 0 { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }

        // Phase 0: arm the counters at every link of the chain.
        let armed = await js(nav, """
        (function(){
          var f = document.querySelector('#viewer iframe'); if(!f) return 'noiframe';
          var d = f.contentDocument, w = f.contentWindow;
          window.__d = d;   // identity check: is this still the live document later?
          window.__diag = { ifrSel:0, topSel:0, contSel:0, rendSel:0, err:'' };
          try { d.addEventListener('selectionchange', function(){ window.__diag.ifrSel++; }); } catch(e){ window.__diag.err += 'd:'+e+';'; }
          try { document.addEventListener('selectionchange', function(){ window.__diag.topSel++; }); } catch(e){}
          try {
            var r = window.__octavoRendition;
            if (!r) { window.__diag.err += 'norend;'; }
            else {
              r.on('selected', function(){ window.__diag.rendSel++; });
              var cs = r.getContents() || [];
              if (!cs.length) window.__diag.err += 'nocontents;';
              for (var i = 0; i < cs.length; i++) {
                (function(c){ try { c.on('selected', function(){ window.__diag.contSel++; }); } catch(e){ window.__diag.err += 'c:'+e+';'; } })(cs[i]);
              }
            }
          } catch(e){ window.__diag.err += 'r:'+e+';'; }
          return 'armed';
        })()
        """) as? String ?? "nil"
        print("DIAG armed=\(armed)")

        // Phase 1: REAL programmatic selection (WebKit fires selectionchange for this per spec).
        let sel1 = await js(nav, """
        (function(){
          var f = document.querySelector('#viewer iframe');
          var d = f.contentDocument, w = f.contentWindow;
          var p = d.querySelector('p'); if(!p||!p.firstChild) return 'nop';
          var r = d.createRange(); r.setStart(p.firstChild, 0); r.setEnd(p.firstChild, 5);
          var s = w.getSelection(); s.removeAllRanges(); s.addRange(r);
          return 'selected:' + String(w.getSelection());
        })()
        """) as? String ?? "nil"
        try? await Task.sleep(nanoseconds: 900_000_000)   // > epub.js 250ms debounce
        let diag1 = await js(nav, "JSON.stringify(window.__diag)") as? String ?? "nil"
        print("DIAG phase1 sel=\(sel1) counters=\(diag1) swiftSelected=\(swiftSelected)")

        // Phase 2: same-call probe — attach a listener and synchronously dispatch a synthetic
        // selectionchange built in the IFRAME's own realm, so cross-call document identity and
        // cross-realm Event construction are both ruled out. Also checks whether the document object
        // the arm phase saw is still the live one.
        let diag2 = await js(nav, """
        (function(){
          var f = document.querySelector('#viewer iframe');
          var d = f.contentDocument, w = f.contentWindow;
          var out = { sameDoc: false, syncHit: 0, hasSel: String(w.getSelection()) };
          try { out.sameDoc = !!(window.__d && window.__d === d); } catch(e) {}
          window.__d = d;
          d.addEventListener('selectionchange', function(){ out.syncHit++; });
          d.dispatchEvent(new w.Event('selectionchange'));
          return JSON.stringify(out);
        })()
        """) as? String ?? "nil"
        try? await Task.sleep(nanoseconds: 400_000_000)
        let diag2b = await js(nav, "JSON.stringify(window.__diag)") as? String ?? "nil"
        print("DIAG phase2 sameCall=\(diag2) counters=\(diag2b) swiftSelected=\(swiftSelected) lastCfi=\(lastCfi)")

        // Phase 3: discriminate CROSS-REALM vs IN-REALM listeners. (a) host-frame listener for a
        // custom event + dispatch (name-agnostic pure DOM), (b) the identical attach+dispatch run
        // wholly inside the iframe's own realm via w.eval, (c) an in-realm selectionchange listener
        // + a real selection change — the exact thing epub.js needs, minus the cross-frame hop.
        let diag3 = await js(nav, """
        (function(){
          var f = document.querySelector('#viewer iframe');
          var d = f.contentDocument, w = f.contentWindow;
          var out = { crossFoo: 0, dispatchRet: null, inRealmFoo: -1, inRealmSelArmed: false, err: '' };
          try {
            d.addEventListener('foo', function(){ out.crossFoo++; });
            out.dispatchRet = d.dispatchEvent(new w.Event('foo'));
          } catch (e) { out.err += 'x:' + e + ';'; }
          try {
            out.inRealmFoo = w.eval("(function(){ var n=0; document.addEventListener('foo2', function(){n++;}); document.dispatchEvent(new Event('foo2')); return n; })()");
          } catch (e) { out.err += 'e:' + e + ';'; }
          try {
            w.eval("window.__selN = 0; document.addEventListener('selectionchange', function(){ window.__selN++; });");
            out.inRealmSelArmed = true;
            var p = d.querySelector('p');
            var s = w.getSelection(); s.removeAllRanges();
            var r = d.createRange(); r.setStart(p.firstChild, 0); r.setEnd(p.firstChild, 11);
            s.addRange(r);
          } catch (e) { out.err += 's:' + e + ';'; }
          return JSON.stringify(out);
        })()
        """) as? String ?? "nil"
        try? await Task.sleep(nanoseconds: 900_000_000)
        let selN = await js(nav, "(function(){ var w = document.querySelector('#viewer iframe').contentWindow; return w.__selN; })()") as? Int ?? -1
        let diag3b = await js(nav, "JSON.stringify(window.__diag)") as? String ?? "nil"
        print("DIAG phase3 probes=\(diag3) inRealmSelectionchangeCount=\(selN) counters=\(diag3b) swiftSelected=\(swiftSelected)")
    }
}
#endif
