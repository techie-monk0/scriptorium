import Foundation
import Octavo

/// Reference `Source` over a local file URL — the zero-transfer native fast
/// path. `read(range:)` returns an exact byte window via a seeking file handle.
public struct FileSource: Source {
    public let url: URL

    public init(url: URL) {
        self.url = url
    }

    public func length() async throws -> Int {
        let values = try url.resourceValues(forKeys: [.fileSizeKey])
        return values.fileSize ?? 0
    }

    public func contentType() async throws -> String? {
        switch url.pathExtension.lowercased() {
        case "pdf": return "application/pdf"
        case "epub": return "application/epub+zip"
        default: return nil
        }
    }

    public func read(range: Range<Int>) async throws -> Data {
        guard range.lowerBound >= 0, range.upperBound >= range.lowerBound else {
            throw FileSourceError.invalidRange(range)
        }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        try handle.seek(toOffset: UInt64(range.lowerBound))
        let data = try handle.read(upToCount: range.count) ?? Data()
        return data
    }
}

public enum FileSourceError: Error, Equatable {
    case invalidRange(Range<Int>)
}
