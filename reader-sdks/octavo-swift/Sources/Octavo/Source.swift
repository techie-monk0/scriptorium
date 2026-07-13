import Foundation

/// PORT: the byte source the engine reads from. The integrator supplies it; the
/// engine never learns whether bytes came from disk, HTTP, or cloud (the
/// "kDrive doesn't leak" seam). No catalogue route literals live here — they
/// arrive baked into a concrete adapter (e.g. `HttpRangeSource(url:)`).
public protocol Source: Sendable {
    /// Total length of the resource in bytes.
    func length() async throws -> Int

    /// MIME content type, if known (e.g. `application/pdf`).
    func contentType() async throws -> String?

    /// Read a half-open byte window `[range.lowerBound, range.upperBound)`.
    func read(range: Range<Int>) async throws -> Data
}

public extension Source {
    /// Read the whole resource.
    func readAll() async throws -> Data {
        let n = try await length()
        guard n > 0 else { return Data() }
        return try await read(range: 0..<n)
    }
}
