#if canImport(UIKit)
import SwiftUI
import WebKit
import CatalogueCore
import CatalogueData

/// PROTOTYPE (behind the `useWebEpubReader` flag) — host the SHARED web reader inside a `WKWebView`.
///
/// It loads the Flask reader page `/holding/<id>/read`, which runs the exact same `reader-core` chrome
/// + engine that web and PWA use — so the EPUB reader is *literally* the same UI on every surface,
/// impossible to diverge. Native's only jobs are hosting the web view and injecting the app's session
/// cookie so the page (and its `/sync/reader` + `/holding/*/file` subresource requests) authenticates.
///
/// Boundaries / caveats (why this stays a prototype until device-verified):
///   • ONLINE‑ONLY — it loads from the server; the native octavo reader remains the offline path.
///   • Auth relies on a cookie‑session sign‑in; header‑only auth won't reach subresources.
///   • EPUB is view‑only (no native ink), which is exactly what makes full web‑hosting clean here.
@MainActor
public struct WebEpubReaderView: View {
    private let holding: Holding
    private let endpoint: any ServerEndpoint
    @Environment(\.dismiss) private var dismiss

    public init(holding: Holding, endpoint: any ServerEndpoint) {
        self.holding = holding
        self.endpoint = endpoint
    }

    private var readerURL: URL {
        // Absolute path from the host root — a base URL with a "/app" prefix must not become
        // "/app/holding/<id>/read" (404). Same reasoning as HoldingBytes.
        var comps = URLComponents(url: endpoint.baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        comps.path = "/holding/\(holding.holdingId)/read"
        comps.query = nil; comps.fragment = nil
        return comps.url ?? endpoint.baseURL.appendingPathComponent("holding/\(holding.holdingId)/read")
    }

    public var body: some View {
        WebReaderContainer(url: readerURL, baseURL: endpoint.baseURL)
            .ignoresSafeArea()
            // The web reader has its own ‹ back (to library); give a native dismiss too so the sheet
            // always closes even if the page fails to load.
            .overlay(alignment: .topTrailing) {
                Button { dismiss() } label: {
                    Image(systemName: "xmark.circle.fill").font(.title2).symbolRenderingMode(.hierarchical)
                }
                .padding(10)
                .accessibilityLabel("Close")
            }
    }
}

/// The `WKWebView` wrapper: copy the app's session cookies for the endpoint host into the web view's
/// cookie store, then load the reader page.
private struct WebReaderContainer: UIViewRepresentable {
    let url: URL
    let baseURL: URL

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let web = WKWebView(frame: .zero, configuration: config)
        web.allowsBackForwardNavigationGestures = false

        let cookies = HTTPCookieStorage.shared.cookies(for: baseURL) ?? []
        if cookies.isEmpty {
            web.load(URLRequest(url: url))
        } else {
            let store = config.websiteDataStore.httpCookieStore
            let group = DispatchGroup()
            for c in cookies { group.enter(); store.setCookie(c) { group.leave() } }
            group.notify(queue: .main) { web.load(URLRequest(url: url)) }
        }
        return web
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
#endif
