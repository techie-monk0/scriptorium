import Foundation

/// PORT: where reading position lives. The integrator supplies it (their
/// storage, their sync backend); the SDK ships the contract, not an opinion
/// about where state lives.
public protocol ReadingStore: Sendable {
    /// The last saved position for a publication, or `nil` if none/corrupt.
    func getPosition(_ publicationId: String) async throws -> Locator?

    /// Persist the current position for a publication.
    func setPosition(_ publicationId: String, _ locator: Locator) async throws

    /// The `n` most-recently-touched positions, newest first.
    func recent(_ n: Int) async throws -> [Locator]
}
