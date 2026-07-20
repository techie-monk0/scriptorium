import Foundation
import CatalogueReaderWire

/// Per-book **sync cursor** — the last `HoldingRevs` the reader successfully merged for each copy,
/// persisted to a JSON file. The change-probe (`ReaderRevCheck`) is compared against this: if the
/// server's revs are higher than what's recorded here, something changed elsewhere and the coordinator
/// does the full pull; if they match, it skips the network entirely. Recorded only AFTER a successful
/// fetch, so a dropped/failed sync doesn't falsely mark the book up to date.
///
/// A plain actor over a JSON file, mirroring the other `Local*Store`s. Pure Foundation → unit-testable.
public actor ReaderSyncCursorStore {
    private let fileURL: URL
    private var state: [String: HoldingRevs]?

    public init(fileURL: URL? = nil) {
        self.fileURL = fileURL ?? Self.defaultURL()
    }

    private static func defaultURL() -> URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("reader-sync-cursors.json")
    }

    private func loaded() -> [String: HoldingRevs] {
        if let state { return state }
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode([String: HoldingRevs].self, from: $0) } ?? [:]
        state = s
        return s
    }

    private func persist(_ s: [String: HoldingRevs]) {
        state = s
        if let data = try? JSONEncoder().encode(s) { try? data.write(to: fileURL, options: .atomic) }
    }

    /// What we last merged for this copy (`.zero` if never — so a first open always reads as changed).
    public func lastSeen(_ publicationId: String) -> HoldingRevs {
        loaded()[publicationId] ?? .zero
    }

    /// Record the revs we just merged, so the next probe can short-circuit when nothing has changed.
    public func record(_ publicationId: String, _ revs: HoldingRevs) {
        var s = loaded()
        s[publicationId] = revs
        persist(s)
    }
}
