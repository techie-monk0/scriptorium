import Foundation
import CatalogueCore

/// One open reading session — a reader "tab": the holding to reopen + its display title (+ the edition
/// for the star toggle). The full `Holding` is carried so a tab reopens with no replica lookup; the
/// reading POSITION is NOT duplicated here — it stays in `CatalogueReadingStore` (restored on mount).
public struct OpenBook: Codable, Equatable, Identifiable, Sendable {
    public var pubId: String
    public var holding: Holding
    public var title: String
    public var eid: Int?
    public var id: String { pubId }

    public init(holding: Holding, title: String, eid: Int? = nil) {
        self.pubId = "holding:\(holding.holdingId)"   // matches ReaderView.pubId
        self.holding = holding
        self.title = title
        self.eid = eid
    }
}

/// The set of currently-open books + which one is active — the reader "tabs" model (PDF Expert style).
/// Ordered most-recent-first; the active pointer defaults to the front. JSON-persisted in Application
/// Support so tabs + the most-recent default survive relaunches. This is per-device UI state (like the
/// reading theme), not synced.
public actor OpenSessionsStore {
    private struct Persisted: Codable { var books: [OpenBook] = []; var activeId: String? = nil }
    private let fileURL: URL
    private var state: Persisted?

    public init(fileURL: URL? = nil) {
        self.fileURL = fileURL ?? Self.defaultURL()
    }

    private static func defaultURL() -> URL {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("open-sessions.json")
    }

    private func loaded() -> Persisted {
        if let s = state { return s }
        let s = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode(Persisted.self, from: $0) } ?? Persisted()
        state = s
        return s
    }

    private func persist(_ s: Persisted) {
        state = s
        if let data = try? JSONEncoder().encode(s) { try? data.write(to: fileURL, options: .atomic) }
    }

    /// The open tabs, most-recent-first.
    public func list() -> [OpenBook] { loaded().books }

    /// The active tab id — the explicit pointer, or the front of the list (most recent) as the default.
    public func activeId() -> String? { loaded().activeId ?? loaded().books.first?.pubId }

    /// Open (or re-focus) a book: move it to the front and make it active. Idempotent by `pubId`.
    @discardableResult
    public func open(_ book: OpenBook) -> String {
        var s = loaded()
        s.books.removeAll { $0.pubId == book.pubId }
        s.books.insert(book, at: 0)
        s.activeId = book.pubId
        persist(s)
        return book.pubId
    }

    /// Make an already-open tab active (no-op if it isn't open).
    public func activate(_ pubId: String) {
        var s = loaded()
        guard s.books.contains(where: { $0.pubId == pubId }) else { return }
        s.activeId = pubId
        persist(s)
    }

    /// Close a tab; if it was active, fall back to the front of what remains.
    public func close(_ pubId: String) {
        var s = loaded()
        s.books.removeAll { $0.pubId == pubId }
        if s.activeId == pubId { s.activeId = s.books.first?.pubId }
        persist(s)
    }
}
