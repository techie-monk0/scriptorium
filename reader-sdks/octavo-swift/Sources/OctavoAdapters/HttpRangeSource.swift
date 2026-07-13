import Foundation
import Octavo

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Reference `Source` over HTTP range requests, with the 1MB-chunk lesson baked
/// in: a large `read(range:)` is split into `chunkSize` windows, each fetched
/// with a `Range: bytes=start-end` header and reassembled in order.
///
/// No host route literal lives here — the full URL (the integrator's own
/// per-resource endpoint) is injected at construction.
public final class HttpRangeSource: Source, @unchecked Sendable {
    public let url: URL
    public let chunkSize: Int
    private let session: URLSession

    /// Default 1MB chunking (the lesson from the PWA range-serving work).
    public static let defaultChunkSize = 1 * 1024 * 1024

    public init(
        url: URL,
        chunkSize: Int = HttpRangeSource.defaultChunkSize,
        session: URLSession = .shared
    ) {
        precondition(chunkSize > 0, "chunkSize must be positive")
        self.url = url
        self.chunkSize = chunkSize
        self.session = session
    }

    // MARK: Pure range math (unit-testable without the network)

    /// Inclusive HTTP `Range:` header value for a half-open byte window.
    public static func rangeHeader(for range: Range<Int>) -> String {
        "bytes=\(range.lowerBound)-\(range.upperBound - 1)"
    }

    /// Split a half-open window into `<= chunkSize` half-open sub-windows.
    public static func chunkRanges(
        for range: Range<Int>,
        chunkSize: Int
    ) -> [Range<Int>] {
        guard !range.isEmpty else { return [] }
        var chunks: [Range<Int>] = []
        var pos = range.lowerBound
        while pos < range.upperBound {
            let end = min(pos + chunkSize, range.upperBound)
            chunks.append(pos..<end)
            pos = end
        }
        return chunks
    }

    // MARK: Source

    public func length() async throws -> Int {
        // Prefer HEAD Content-Length; fall back to a 1-byte ranged GET whose
        // Content-Range total is the length.
        var head = URLRequest(url: url)
        head.httpMethod = "HEAD"
        if let (_, response) = try? await session.data(for: head),
           let http = response as? HTTPURLResponse,
           let len = http.value(forHTTPHeaderField: "Content-Length"),
           let n = Int(len), n > 0 {
            return n
        }

        var probe = URLRequest(url: url)
        probe.setValue("bytes=0-0", forHTTPHeaderField: "Range")
        let (_, response) = try await session.data(for: probe)
        if let http = response as? HTTPURLResponse,
           let contentRange = http.value(forHTTPHeaderField: "Content-Range"),
           let total = Self.totalLength(fromContentRange: contentRange) {
            return total
        }
        throw HttpRangeSourceError.lengthUnavailable
    }

    public func contentType() async throws -> String? {
        var head = URLRequest(url: url)
        head.httpMethod = "HEAD"
        if let (_, response) = try? await session.data(for: head),
           let http = response as? HTTPURLResponse,
           let ct = http.value(forHTTPHeaderField: "Content-Type") {
            return ct
        }
        return nil
    }

    public func read(range: Range<Int>) async throws -> Data {
        guard range.lowerBound >= 0, range.upperBound >= range.lowerBound else {
            throw HttpRangeSourceError.invalidRange(range)
        }
        var result = Data()
        for chunk in Self.chunkRanges(for: range, chunkSize: chunkSize) {
            var req = URLRequest(url: url)
            req.setValue(Self.rangeHeader(for: chunk), forHTTPHeaderField: "Range")
            let (data, response) = try await session.data(for: req)
            if let http = response as? HTTPURLResponse,
               http.statusCode != 206, http.statusCode != 200 {
                throw HttpRangeSourceError.badStatus(http.statusCode)
            }
            result.append(data)
        }
        return result
    }

    /// Parse the total from a `Content-Range: bytes start-end/total` header.
    static func totalLength(fromContentRange header: String) -> Int? {
        guard let slash = header.lastIndex(of: "/") else { return nil }
        let totalPart = header[header.index(after: slash)...]
            .trimmingCharacters(in: .whitespaces)
        guard totalPart != "*" else { return nil }
        return Int(totalPart)
    }
}

public enum HttpRangeSourceError: Error, Equatable {
    case invalidRange(Range<Int>)
    case lengthUnavailable
    case badStatus(Int)
}
