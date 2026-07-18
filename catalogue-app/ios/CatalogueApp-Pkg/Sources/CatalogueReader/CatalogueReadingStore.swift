import Foundation
import Octavo

/// The catalogue-app's concrete `octavo` `ReadingStore` — persists an octavo **`Locator`** per
/// publication to a JSON file (the iOS analogue of the reader sync-of-record's position record). Last
/// position is restored on open and saved on every location change (octavo auto-wires this);
/// `recent(n)` feeds the Home "Recently opened" shelf. This is the port octavo's engine reads/writes;
/// the engine never learns it's a file.
public actor CatalogueReadingStore: ReadingStore {
    private struct Entry: Codable {
        var locator: Locator
        var openedAt: Date
        /// The furthest position ever reached (by `progression`), distinct from the current one —
        /// Kindle-style "furthest read" so a scroll-back doesn't lose how far you'd got.
        var furthest: Locator? = nil
    }
    private let fileURL: URL
    private var entries: [String: Entry]?

    public init(directory: URL? = nil) {
        let dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("catalogue-app", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        self.fileURL = dir.appendingPathComponent("reading-positions.json")
    }

    private func loaded() -> [String: Entry] {
        if let entries { return entries }
        let e = (try? Data(contentsOf: fileURL)).flatMap { try? JSONDecoder().decode([String: Entry].self, from: $0) } ?? [:]
        entries = e
        return e
    }

    private func persist(_ e: [String: Entry]) {
        entries = e
        if let data = try? JSONEncoder().encode(e) { try? data.write(to: fileURL, options: .atomic) }
    }

    public func getPosition(_ publicationId: String) async throws -> Locator? {
        loaded()[publicationId]?.locator
    }

    public func setPosition(_ publicationId: String, _ locator: Locator) async throws {
        var e = loaded()
        let prev = e[publicationId]
        // Advance "furthest read" only forward (by progression); never let a scroll-back retract it.
        let furthest: Locator? = {
            guard let existing = prev?.furthest else { return locator }
            let a = existing.locations.progression ?? 0, b = locator.locations.progression ?? 0
            return b > a ? locator : existing
        }()
        e[publicationId] = Entry(locator: locator, openedAt: Date(), furthest: furthest)
        persist(e)
    }

    /// The furthest position ever reached for a publication (for a "jump ahead" affordance on reopen).
    public func furthest(_ publicationId: String) async throws -> Locator? {
        loaded()[publicationId]?.furthest
    }

    public func recent(_ n: Int) async throws -> [Locator] {
        loaded().values.sorted { $0.openedAt > $1.openedAt }.prefix(n).map(\.locator)
    }
}
