import Foundation

/// Pure HTTP-range serving math for `SourceSchemeHandler` — parse a `Range:` header into a half-open
/// byte window clamped to the resource, and build the response headers. No WebKit, no I/O, so it's
/// fully unit-testable (mirrors `HttpRangeSource`'s static range math). This is the verifiable core of
/// the EPUB byte-serving seam (D4); the `WKURLSchemeTask` plumbing around it is the thin shell.
public enum EpubRangeResponder {

    /// Parse a `bytes=…` Range header into a half-open `[lower, upper)` window clamped to `total`.
    /// Returns nil for a missing / whole-resource / unsatisfiable request → the caller serves a 200
    /// full body. Supports `bytes=start-end`, `bytes=start-` (open end), and `bytes=-N` (suffix).
    public static func parse(rangeHeader header: String?, total: Int) -> Range<Int>? {
        guard total > 0, let header, header.hasPrefix("bytes=") else { return nil }
        let spec = header.dropFirst("bytes=".count)
        guard let dash = spec.firstIndex(of: "-") else { return nil }
        let startStr = spec[..<dash].trimmingCharacters(in: .whitespaces)
        let endStr = spec[spec.index(after: dash)...].trimmingCharacters(in: .whitespaces)

        if startStr.isEmpty {                               // suffix: bytes=-N → last N bytes
            guard let n = Int(endStr), n > 0 else { return nil }
            return max(0, total - n)..<total
        }
        guard let start = Int(startStr), start >= 0, start < total else { return nil }
        let endInclusive: Int
        if endStr.isEmpty {
            endInclusive = total - 1                        // bytes=start-
        } else {
            guard let e = Int(endStr) else { return nil }
            endInclusive = min(e, total - 1)                // clamp past-EOF ends
        }
        guard endInclusive >= start else { return nil }
        return start..<(endInclusive + 1)
    }

    /// Response headers for a (possibly partial) read. `Accept-Ranges` advertises range support so
    /// epub.js / WebKit will issue ranged reads against large books.
    public static func headers(contentType: String, total: Int,
                               served: Range<Int>, partial: Bool) -> [String: String] {
        var h = ["Content-Type": contentType,
                 "Content-Length": String(served.count),
                 "Accept-Ranges": "bytes"]
        if partial {
            h["Content-Range"] = "bytes \(served.lowerBound)-\(served.upperBound - 1)/\(total)"
        }
        return h
    }
}
