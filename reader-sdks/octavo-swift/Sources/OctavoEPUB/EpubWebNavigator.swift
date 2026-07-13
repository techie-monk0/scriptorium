import Foundation
import Octavo

#if canImport(WebKit)
import WebKit

/// `Navigator` over a WKWebView + epub.js host (decision: epub.js-in-WKWebView, for CFI parity with
/// the PWA). The book is served through the **`Source` seam** by `SourceSchemeHandler` on a custom
/// scheme (no temp file, D4); epub.js/jszip + the bridge are **inlined** into the host page so only the
/// book is fetched. Swift drives the page via `window.octavoBridge.*` and receives `relocated`/`ready`
/// events on the `octavoBridge` message handler. The CFI is the Locator location.
///
/// NOTE(device): the live WKWebView + epub.js flow (load → ready → relocate, search, goTo) needs
/// simulator verification with a real EPUB and the **vendored `epub.min.js`/`jszip.min.js`** dropped
/// into `Sources/OctavoEPUB/Resources/` (copy from the web vendor). The pure range-serving math
/// (`EpubRangeResponder`) and this file's compile are what's checked headless.
@MainActor
public final class EpubWebNavigator: NSObject, Navigator {

    public let webView: WKWebView
    public let publicationId: String
    public let source: Source

    public private(set) var currentLocation: Locator?
    public var onLocationChanged: (@MainActor (Locator) -> Void)?
    /// An empty-area tap zone from the content: "prev" / "next" / "toggle" (chrome). The host wires
    /// these to page turns / chrome toggle — tap handling lives in the content so links keep working.
    public var onTapZone: (@MainActor (String) -> Void)?
    /// An external link (http/mailto/tel) tapped in the content — the host opens it (e.g. in Safari).
    public var onExternalLink: (@MainActor (URL) -> Void)?
    /// Fired just before an IN-CONTENT link jump, so the host can record a "back" target (the current
    /// location is still the pre-jump one at this point).
    public var onWillJump: (@MainActor () -> Void)?

    /// Seek to a fraction (0…1) of the book — for a "Go to position" control.
    public func goToFraction(_ f: Double) async {
        _ = try? await webView.evaluateJavaScript("window.octavoBridge.gotoFraction(\(max(0, min(1, f))))")
    }

    private static let bridgeName = "octavoBridge"
    private static let scheme = "octavo-epub"
    private static let bookURL = "octavo-epub:///book.epub"

    /// Runs INSIDE each frame (forMainFrameOnly:false). A clean, un-moved tap on a link is reported to
    /// native (external → open; internal → `gotoHref` → epubGo); a blank tap → `tap` (toggle the bars).
    /// Moved touches are ignored so the native swipe owns paging and text selection isn't broken.
    /// Coordinated with the bridge's fallback via `document.__octavoTapBound` (only one binds).
    private static let tapScript = """
    (function(){"use strict";
      if (document.__octavoTapBound) return; document.__octavoTapBound = true;
      function post(m){ try { window.webkit.messageHandlers.octavoBridge.postMessage(m); } catch(e){} }
      function linkAt(t){ return (t && t.closest) ? t.closest("a[href]") : null; }
      function href(a){ return a.getAttribute("href") || ""; }
      function nav(a){ var h = href(a);
        if(/^(https?:|mailto:|tel:)/i.test(h)) post({type:"link", href:h});
        else if(h && h.charAt(0)!=="#") post({type:"gotoHref", href:h}); }
      var sx=0, sy=0, moved=false;
      document.addEventListener("touchstart", function(e){ if(e.touches&&e.touches.length){ sx=e.touches[0].clientX; sy=e.touches[0].clientY; moved=false; } }, {passive:true});
      document.addEventListener("touchmove", function(e){ if(e.touches&&e.touches.length&&(Math.abs(e.touches[0].clientX-sx)>8||Math.abs(e.touches[0].clientY-sy)>8)) moved=true; }, {passive:true});
      document.addEventListener("touchend", function(e){
        if(moved) return;
        var a = linkAt(e.target);
        if(a){ e.preventDefault(); if(e.stopPropagation) e.stopPropagation(); nav(a); }
        // a blank tap toggles the bars via a NATIVE SwiftUI tap gesture — not posted here (would
        // double-toggle against it).
      }, {passive:false});
      document.addEventListener("click", function(e){ var a = linkAt(e.target); if(!a) return; e.preventDefault(); nav(a); }, false);
    })();
    """

    private let schemeHandler: SourceSchemeHandler
    private var readyCont: CheckedContinuation<Void, Never>?
    private var cachedToc: [TocItem] = []

    public init(
        source: Source,
        publicationId: String,
        configuration: WKWebViewConfiguration = .init()
    ) {
        self.source = source
        self.publicationId = publicationId
        let handler = SourceSchemeHandler(source: source, contentType: "application/epub+zip")
        configuration.setURLSchemeHandler(handler, forURLScheme: Self.scheme)
        self.schemeHandler = handler
        // PRIMARY tap/link handling: injected into EVERY frame so it runs INSIDE epub.js's content
        // iframes (a host->iframe listener doesn't reliably fire in a WKWebView). See `tapScript`.
        configuration.userContentController.addUserScript(
            WKUserScript(source: Self.tapScript, injectionTime: .atDocumentEnd, forMainFrameOnly: false))
        self.webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        configuration.userContentController.add(self, name: Self.bridgeName)
    }

    /// Break the message-handler retain cycle before release (deinit can't touch the main-actor view).
    public func tearDown() {
        webView.configuration.userContentController.removeScriptMessageHandler(forName: Self.bridgeName)
    }

    // MARK: Navigator

    public func open() async throws {
        let html = Self.hostHTML(bookURL: Self.bookURL)
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            self.readyCont = cont
            webView.loadHTMLString(html, baseURL: URL(string: "\(Self.scheme):///"))
        }
        // 'ready' arrived (toc cached); ensure an initial location was emitted even if `relocated`
        // hasn't fired yet.
        if currentLocation == nil {
            emit(Locator(publicationId: publicationId, format: .epub, locations: .init(progression: 0)))
        }
    }

    public func goTo(_ locator: Locator) async throws {
        guard let cfi = locator.locations.cfi, !cfi.isEmpty else { return }
        _ = try? await webView.evaluateJavaScript("window.octavoBridge.display(\(Self.jsString(cfi)))")
    }

    public func next() async throws {
        _ = try? await webView.evaluateJavaScript("window.octavoBridge.next()")
    }

    public func prev() async throws {
        _ = try? await webView.evaluateJavaScript("window.octavoBridge.prev()")
    }

    /// Font size, delegated to epub.js (`rendition.themes.fontSize`) via the bridge — the EPUB reading
    /// of the shared "resize" verb (PDF maps the same call to zoom).
    public func bigger() async {
        _ = try? await webView.evaluateJavaScript("window.octavoBridge.bigger()")
    }

    public func smaller() async {
        _ = try? await webView.evaluateJavaScript("window.octavoBridge.smaller()")
    }

    /// Recolour the book via epub.js themes (bg/fg). Re-applied by the bridge on every relocation so a
    /// page turn doesn't flash the default colours.
    public func applyTheme(_ theme: ReaderTheme) async {
        _ = try? await webView.evaluateJavaScript(
            "window.octavoBridge.setTheme(\(Self.jsString(theme.bg)), \(Self.jsString(theme.fg)))")
    }

    public func search(_ query: String) async throws -> [Locator] {
        guard !query.isEmpty else { return [] }
        let result = try await webView.callAsyncJavaScript(
            "return await window.octavoBridge.search(q)",
            arguments: ["q": query], in: nil, contentWorld: .page)
        guard let arr = result as? [[String: Any]] else { return [] }
        return arr.compactMap { item in
            guard let cfi = item["cfi"] as? String else { return nil }
            let excerpt = item["excerpt"] as? String
            return Locator(publicationId: publicationId, format: .epub,
                           locations: .init(cfi: cfi),
                           text: excerpt.map { Locator.Text(highlight: $0) })
        }
    }

    public func outline() -> [TocItem] { cachedToc }

    // MARK: Text marks (driven by the app's EpubDecorationHost; the bridge JS detail stays here)

    /// Add a highlight/underline over a CFI range via epub.js `annotations`.
    public func addTextMark(type: String, cfiRange: String, color: String?) async {
        _ = try? await webView.evaluateJavaScript(
            "window.octavoBridge.addMark(\(Self.jsString(type)), \(Self.jsString(cfiRange)), \(Self.jsString(color ?? "")))")
    }

    public func removeTextMark(type: String, cfiRange: String) async {
        _ = try? await webView.evaluateJavaScript(
            "window.octavoBridge.removeMark(\(Self.jsString(type)), \(Self.jsString(cfiRange)))")
    }

    // MARK: Internals

    private func emit(_ locator: Locator) {
        currentLocation = locator
        onLocationChanged?(locator)
    }

    /// Inline the bundled scripts into a self-contained host page; only the book is fetched (via the
    /// custom scheme). Missing vendored assets yield empty scripts → a runtime no-op until dropped in.
    private static func hostHTML(bookURL: String) -> String {
        func asset(_ name: String) -> String {
            guard let u = Bundle.module.url(forResource: name, withExtension: "js"),
                  let s = try? String(contentsOf: u, encoding: .utf8) else { return "" }
            return s
        }
        return """
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
        <style>html,body,#viewer{margin:0;height:100%;width:100%;background:#fff}</style>
        <script>\(asset("jszip.min"))</script>
        <script>\(asset("epub.min"))</script>
        </head><body><div id="viewer"></div>
        <script>\(asset("epub-bridge"))</script>
        <script>window.octavoOpen(\(jsString(bookURL)));</script>
        </body></html>
        """
    }

    /// A safe JS string literal (quoted + escaped) for embedding a value into evaluated JS.
    private static func jsString(_ s: String) -> String {
        guard let data = try? JSONEncoder().encode(s) else { return "\"\"" }
        return String(decoding: data, as: UTF8.self)
    }

    private static func parseToc(_ raw: Any?, publicationId: String) -> [TocItem] {
        guard let arr = raw as? [[String: Any]] else { return [] }
        return arr.compactMap { item in
            guard let title = item["title"] as? String else { return nil }
            return TocItem(title: title,
                           locator: Locator(publicationId: publicationId, format: .epub,
                                            locations: .init(cfi: item["cfi"] as? String)))
        }
    }
}

extension EpubWebNavigator: WKScriptMessageHandler {
    public func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "ready":
            cachedToc = Self.parseToc(body["toc"], publicationId: publicationId)
            readyCont?.resume(); readyCont = nil
        case "relocated":
            emit(Locator(publicationId: publicationId, format: .epub,
                         locations: .init(cfi: body["cfi"] as? String,
                                          progression: body["progression"] as? Double)))
        case "tap":
            onTapZone?("toggle")               // a blank-area tap → toggle the native bars
        case "gotoHref":
            // Internal link tapped in a content frame — the frame can't reach `rendition`, so hop back
            // to the host frame's robust resolver. Record a back target first (we're still at the origin).
            if let href = body["href"] as? String {
                onWillJump?()
                Task { _ = try? await webView.evaluateJavaScript("window.octavoBridge.go(\(Self.jsString(href)))") }
            }
        case "link":
            if let href = body["href"] as? String, let url = URL(string: href) { onExternalLink?(url) }
        case "error":
            readyCont?.resume(); readyCont = nil      // unblock open() even on a load error (v1)
        default:
            break
        }
    }
}

// MARK: - Façade convenience

public extension Octavo {
    /// Open an EPUB reading session, wiring an `EpubWebNavigator` over the `Source` seam.
    @MainActor
    @discardableResult
    static func open(
        epubSource source: Source,
        publicationId: String,
        readingStore: ReadingStore? = nil,
        capabilities: Capabilities = .init(),
        decorations: DecorationHost? = nil
    ) async throws -> Reader {
        let nav = EpubWebNavigator(source: source, publicationId: publicationId)
        return try await Octavo.open(
            navigator: nav,
            publicationId: publicationId,
            readingStore: readingStore,
            capabilities: capabilities,
            decorations: decorations
        )
    }
}

#endif // canImport(WebKit)
