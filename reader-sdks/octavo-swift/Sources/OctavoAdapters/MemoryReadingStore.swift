import Foundation
import Octavo

/// In-memory `ReadingStore` for tests and examples. `recent(n)` orders by
/// recency (most recently set first).
public actor MemoryReadingStore: ReadingStore {
    private var positions: [String: Locator] = [:]
    /// Publication ids, most-recently-touched first.
    private var order: [String] = []

    public init() {}

    public func getPosition(_ publicationId: String) async throws -> Locator? {
        positions[publicationId]
    }

    public func setPosition(_ publicationId: String, _ locator: Locator) async throws {
        positions[publicationId] = locator
        order.removeAll { $0 == publicationId }
        order.insert(publicationId, at: 0)
    }

    public func recent(_ n: Int) async throws -> [Locator] {
        guard n > 0 else { return [] }
        return order.prefix(n).compactMap { positions[$0] }
    }
}
