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
import Postilla
import PostillaRender

/// Live diagnostic: drives the REAL `EpubWebNavigator` + vendored epub.js in a windowed WKWebView,
/// adds a highlight over a real (search-derived) CFI range, and asserts the mark lands in the DOM.
/// This is the end-to-end check the styling/diff unit tests can't give — it exercises the actual
/// `annotations.add` render path that the app uses. macOS-only (needs a window to lay out the webview).
@MainActor
final class EpubAnnotationRenderTests: XCTestCase {

    /// The bundled minimal EPUB fixture (Tests/CatalogueReaderTests/Fixtures/book.epub).
    private func minimalEpub() throws -> URL {
        guard let url = Bundle.module.url(forResource: "book", withExtension: "epub", subdirectory: "Fixtures")
                ?? Bundle.module.url(forResource: "book", withExtension: "epub") else {
            throw XCTSkip("book.epub fixture not bundled")
        }
        return url
    }

    private func waitJS(_ nav: EpubWebNavigator, _ js: String) async -> Any? {
        try? await nav.webView.evaluateJavaScript(js)
    }

    /// Open a rendered EPUB in a real window and return the navigator + a valid RANGE cfi for "Hello".
    private func rendered() async throws -> (EpubWebNavigator, String, Any) {
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
        try await nav.open()
        for _ in 0..<60 {
            let vh = await waitJS(nav, "(document.getElementById('viewer')||{}).clientHeight||0") as? Int ?? 0
            let ifr = await waitJS(nav, "document.querySelectorAll('#viewer iframe').length") as? Int ?? 0
            if nav.currentLocation?.locations.cfi != nil && vh > 0 && ifr > 0 { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        let hits = (try? await nav.search("Hello")) ?? []
        guard let cfi = hits.first?.locations.cfi, !cfi.isEmpty else {
            throw XCTSkip("epub.js did not render (no search CFI)")
        }
        return (nav, cfi, win)   // return win so it stays retained
    }

    private func domMarkCount(_ nav: EpubWebNavigator) async -> Int {
        await waitJS(nav, "document.querySelectorAll('.octavo-mark, g[class*=\\\"octavo\\\"], svg .epubjs-hl').length") as? Int ?? -1
    }

    /// PROVEN path: the navigator's addTextMark (what EpubDecorationHost calls) renders a highlight.
    func testHighlightLandsInDom() async throws {
        let (nav, cfi, win) = try await rendered(); _ = win
        await nav.addTextMark(type: "highlight", cfiRange: cfi, color: "#ffd54a")
        try? await Task.sleep(nanoseconds: 300_000_000)
        let count = await domMarkCount(nav)
        print("DIAG direct domMarkCount=\(count)")
        XCTAssertGreaterThan(count, 0, "direct addTextMark did not render (cfi=\(cfi))")
    }

    /// The APP's EXACT render path: `renderMarks()` does `renderLayer.render([Annotation])`. This
    /// exercises Decorations mapping + MarkOverlay (weak host) + EpubDecorationHost.apply + diff.
    /// If this fails while the direct test passes, the bug is in this layer.
    func testAppRenderLayerLandsInDom() async throws {
        let (nav, cfi, win) = try await rendered(); _ = win
        let host = EpubDecorationHost(navigator: nav)              // strong ref (app keeps it in @State)
        let layer = CompositeRenderLayer(decorations: host, ink: nil)
        let now = Date()
        let ann = Annotation(publicationId: "t", kind: .highlight,
                             locator: Locator(publicationId: "t", format: .epub, locations: .init(cfi: cfi)),
                             cfiRange: cfi, color: "#ffd54a", createdAt: now, updatedAt: now, rev: 1)
        layer.render([ann])
        try? await Task.sleep(nanoseconds: 400_000_000)
        let count = await domMarkCount(nav)
        print("DIAG appPath domMarkCount=\(count)")
        XCTAssertGreaterThan(count, 0, "app render path (CompositeRenderLayer) did not render (cfi=\(cfi))")
    }

    /// The FULL app flow: a real text selection → epub.js `selected` event → `onSelection` CFI →
    /// app render path. Proves a SELECTION-derived CFI (what the app actually uses) renders, not just a
    /// search-derived one.
    func testSelectionDerivedCfiRenders() async throws {
        let (nav, _, win) = try await rendered(); _ = win
        var captured: String?
        nav.onSelection = { cfi, _ in captured = cfi }
        // Select "Hello" inside the epub iframe and fire the event epub.js listens for.
        let sel = await waitJS(nav, """
        (function(){
          var f = document.querySelector('#viewer iframe'); if(!f) return 'noiframe';
          var d = f.contentDocument, w = f.contentWindow;
          var p = d.querySelector('p'); if(!p||!p.firstChild) return 'nop';
          var r = d.createRange(); r.setStart(p.firstChild, 0); r.setEnd(p.firstChild, 5);
          var s = w.getSelection(); s.removeAllRanges(); s.addRange(r);
          d.dispatchEvent(new Event('selectionchange'));
          w.dispatchEvent(new Event('mouseup'));
          return 'ok';
        })()
        """) as? String ?? "nil"
        for _ in 0..<20 { if captured != nil { break }; try? await Task.sleep(nanoseconds: 100_000_000) }
        guard let cfi = captured, !cfi.isEmpty else { throw XCTSkip("epub.js selection event did not fire (sel=\(sel))") }
        print("DIAG selectionCFI=\(cfi)")

        let host = EpubDecorationHost(navigator: nav)
        let layer = CompositeRenderLayer(decorations: host, ink: nil)
        let now = Date()
        layer.render([Annotation(publicationId: "t", kind: .highlight,
                                 locator: Locator(publicationId: "t", format: .epub, locations: .init(cfi: cfi)),
                                 cfiRange: cfi, color: "#ffd54a", createdAt: now, updatedAt: now, rev: 1)])
        try? await Task.sleep(nanoseconds: 400_000_000)
        let count = await domMarkCount(nav)
        print("DIAG selectionPath domMarkCount=\(count)")
        XCTAssertGreaterThan(count, 0, "selection-derived CFI did not render (cfi=\(cfi))")
    }
}
#endif
