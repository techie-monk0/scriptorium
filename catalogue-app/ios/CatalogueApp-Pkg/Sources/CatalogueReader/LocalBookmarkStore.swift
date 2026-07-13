import Foundation
import Postilla
import Octavo

/// Local-first bookmark store: persists bookmarks to a JSON file so they survive reader reopens even
/// offline (exactly like the reading position via `CatalogueReadingStore`), and best-effort mirrors
/// to/from a remote `BookmarkStore` (the server `/sync/reader`, i.e. `BookmarkSync`). This fixes
/// bookmarks vanishing between opens when the server sync can't complete. LWW merge via `BookmarkMerge`.
public actor LocalBookmarkStore: BookmarkStore {
    private let fileURL: URL
    private let remote: (any BookmarkStore)?
    private var store: [String: [String: Bookmark]]?   // pubId → (bookmark uuid → Bookmark)

    public init(fileURL: URL? = nil, remote: (any BookmarkStore)? = nil) {
        self.fileURL = fileURL ?? Self.defaultURL()
        self.remote = remote
    }

    private static func defaultURL() -> URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("bookmarks.json")
    }

    private func loaded() -> [String: [String: Bookmark]] {
        if let s = store { return s }
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode([String: [String: Bookmark]].self, from: $0) } ?? [:]
        store = s
        return s
    }

    private func persist(_ s: [String: [String: Bookmark]]) {
        store = s
        if let data = try? JSONEncoder().encode(s) { try? data.write(to: fileURL, options: .atomic) }
    }

    private func merge(_ incoming: [Bookmark], into pub: inout [String: Bookmark]) {
        for b in incoming {
            let key = b.id.uuidString
            if let existing = pub[key] {
                if BookmarkMerge.wins(b, over: existing) { pub[key] = b }
            } else {
                pub[key] = b
            }
        }
    }

    public func pull(publicationId: String, since rev: Int) async throws -> BookmarkPullResult {
        var s = loaded()
        var pub = s[publicationId] ?? [:]
        // Best-effort: fold in whatever the server has, so a bookmark made on another device shows up.
        if let remote, let r = try? await remote.pull(publicationId: publicationId, since: 0) {
            merge(r.ops, into: &pub); s[publicationId] = pub; persist(s)
        }
        let ops = Array(pub.values)
        return BookmarkPullResult(rev: ops.map(\.rev).max() ?? 0, ops: ops)
    }

    public func push(publicationId: String, ops: [Bookmark]) async throws -> PushResult {
        var s = loaded()
        var pub = s[publicationId] ?? [:]
        merge(ops, into: &pub)
        s[publicationId] = pub
        persist(s)                                        // durable BEFORE the network — never lost
        if let remote { _ = try? await remote.push(publicationId: publicationId, ops: ops) }
        return PushResult(rev: pub.values.map(\.rev).max() ?? 0, applied: ops.map(\.id))
    }
}
