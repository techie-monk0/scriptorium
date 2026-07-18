import Foundation

/// The catalogue reader's server **route map** — every path the reader surface touches, built in one
/// place from a base URL. Previously these were string literals scattered across `ReaderSync`,
/// `BookmarkSync`, `PositionSync`, and `HoldingBytes`; centralizing them means a path (or a query
/// convention like `?holding&since`) is defined once and every frontend hits the identical routes.
public enum ReaderRoutes {

    /// GET `/sync/reader` — marks + bookmarks. `holding` scopes to one copy; `since` requests deltas.
    public static func syncPull(baseURL: URL, holding: Int?, since: Int) -> URL {
        var c = components(baseURL, "/sync/reader")
        var items = [URLQueryItem(name: "since", value: String(since))]
        if let holding { items.append(URLQueryItem(name: "holding", value: String(holding))) }
        c.queryItems = items
        return c.url ?? baseURL
    }

    /// POST `/sync/reader` — upsert a batch of ops.
    public static func syncPush(baseURL: URL) -> URL {
        components(baseURL, "/sync/reader").url ?? baseURL
    }

    /// GET/POST `/holding/<id>/position` — cross-device reading position.
    public static func position(baseURL: URL, holding: Int) -> URL {
        components(baseURL, "/holding/\(holding)/position").url
            ?? baseURL.appendingPathComponent("holding/\(holding)/position")
    }

    /// GET `/holding/<id>/file` — the raw holding bytes (PDF/EPUB).
    public static func holdingFile(baseURL: URL, holding: Int) -> URL {
        components(baseURL, "/holding/\(holding)/file").url
            ?? baseURL.appendingPathComponent("holding/\(holding)/file")
    }

    /// GET `/holding/<id>/read` — the server's web reader (used by the WKWebView EPUB prototype).
    public static func holdingRead(baseURL: URL, holding: Int) -> URL {
        components(baseURL, "/holding/\(holding)/read").url
            ?? baseURL.appendingPathComponent("holding/\(holding)/read")
    }

    /// GET `/holding/<id>/annotated.pdf` — the server-flattened annotated copy (Phase 6 export).
    public static func annotatedPdf(baseURL: URL, holding: Int) -> URL {
        components(baseURL, "/holding/\(holding)/annotated.pdf").url
            ?? baseURL.appendingPathComponent("holding/\(holding)/annotated.pdf")
    }

    /// GET `/holding/<id>/outlined.pdf` — a copy with the authored outline baked into the file (the
    /// "save outline into PDF" export; server bakes via the shared PDF-write mechanism).
    public static func outlinedPdf(baseURL: URL, holding: Int) -> URL {
        components(baseURL, "/holding/\(holding)/outlined.pdf").url
            ?? baseURL.appendingPathComponent("holding/\(holding)/outlined.pdf")
    }

    /// POST `/login` — establish the signed-cookie session.
    public static func login(baseURL: URL) -> URL {
        components(baseURL, "/login").url ?? baseURL.appendingPathComponent("login")
    }

    private static func components(_ baseURL: URL, _ path: String) -> URLComponents {
        var c = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) ?? URLComponents()
        c.path = path
        return c
    }
}
