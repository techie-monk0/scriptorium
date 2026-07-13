import Foundation

/// Result of a `pull` — the server's current `rev` and every op (incl.
/// tombstones) newer than the requested `since`.
public struct PullResult: Sendable, Equatable {
    public var rev: Int
    public var ops: [Annotation]

    public init(rev: Int, ops: [Annotation]) {
        self.rev = rev
        self.ops = ops
    }
}

/// Result of a `push` — the server's new `rev` and the ids it accepted.
public struct PushResult: Sendable, Equatable {
    public var rev: Int
    public var applied: [UUID]

    public init(rev: Int, applied: [UUID]) {
        self.rev = rev
        self.applied = applied
    }
}

/// PORT — the sync-of-record seam (`postilla.md` §3). An integrator drops in
/// their own backend (the reference server is the catalogue's `ReaderStateStore`).
///
/// Contract:
/// - `pull(since:)` returns only rows with `rev > since`, **including tombstones**.
/// - `push(ops:)` is an idempotent **LWW** upsert keyed by `Annotation.id`.
/// - Both are **publication-scoped** so opening a book never pulls the world.
///
/// There is deliberately **no** HTTP/URL knowledge here — see the
/// `NoTransportLiteralsTests` guard.
public protocol AnnotationStore: Sendable {
    /// Ops newer than `rev`, scoped to `publicationId`.
    func pull(publicationId: String, since rev: Int) async throws -> PullResult

    /// LWW-merge `ops` into the store; returns the new rev and accepted ids.
    func push(publicationId: String, ops: [Annotation]) async throws -> PushResult
}
