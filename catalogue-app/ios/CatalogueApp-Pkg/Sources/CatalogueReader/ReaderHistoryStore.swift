import Foundation
import Octavo

/// Per-document back/jump history (the origins of TOC/search/link/bookmark jumps), persisted so the
/// "Back to …" affordance survives closing and reopening a book — modelled on `CatalogueReadingStore`
/// (same actor + JSON-in-Application-Support pattern, same `publicationId` key). Bounded to the most
/// recent `limit` origins so it can't grow without bound. Back-stack only (no forward stack), to keep
/// restore unambiguous.
public actor ReaderHistoryStore {
    private let fileURL: URL
    private let limit: Int
    private var entries: [String: [Locator]]?

    public init(directory: URL? = nil, limit: Int = 20) {
        let dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("catalogue-app", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        self.fileURL = dir.appendingPathComponent("reader-history.json")
        self.limit = max(1, limit)
    }

    private func loaded() -> [String: [Locator]] {
        if let entries { return entries }
        let e = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode([String: [Locator]].self, from: $0) } ?? [:]
        entries = e
        return e
    }

    private func persist(_ e: [String: [Locator]]) {
        entries = e
        if let data = try? JSONEncoder().encode(e) { try? data.write(to: fileURL, options: .atomic) }
    }

    /// The saved back-stack for a publication (oldest → newest jump origin), or empty.
    public func get(_ publicationId: String) -> [Locator] {
        loaded()[publicationId] ?? []
    }

    /// Replace the back-stack for a publication, keeping only the most recent `limit` entries.
    public func set(_ publicationId: String, _ stack: [Locator]) {
        var e = loaded()
        e[publicationId] = Array(stack.suffix(limit))
        persist(e)
    }
}
