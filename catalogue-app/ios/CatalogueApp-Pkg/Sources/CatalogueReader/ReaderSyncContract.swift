import Foundation
import os

/// The `catalogue.reader_sync` wire-contract version this client was built for.
///
/// The server publishes a versioned descriptor (`db_store/reader_sync_contract.json`) and advertises
/// its live version as `contract_version` on every `/sync/reader` response (and at
/// `GET /sync/reader/contract`). The `ReaderSync` / `BookmarkSync` adapters read that and call
/// `check(_:)`, so a server/client drift surfaces once in the log instead of silently mis-syncing.
/// This does NOT belong in postilla — `AnnotationStore` is the generic seam; this is the catalogue
/// adapter's own knowledge of the catalogue's wire contract.
enum ReaderSyncContract {
    /// The contract version these adapters were written against.
    static let builtFor = 1

    /// Compatible iff the server advertises a version at least as new as ours. A missing
    /// `contract_version` (nil) means the server predates the contract — treated as incompatible so
    /// it is noticed. A newer server (additive) is fine.
    static func compatible(_ serverVersion: Int?) -> Bool {
        guard let v = serverVersion else { return false }
        return v >= builtFor
    }

    private static let log = Logger(subsystem: "catalogue.reader", category: "sync-contract")
    private static let lock = NSLock()
    nonisolated(unsafe) private static var warned = false

    /// Warn once if the server's advertised version is missing or older than what we built for.
    static func check(_ serverVersion: Int?) {
        guard !compatible(serverVersion) else { return }
        lock.lock(); defer { lock.unlock() }
        guard !warned else { return }
        warned = true
        let got = serverVersion.map(String.init) ?? "none"
        log.warning("reader_sync contract mismatch: server=\(got, privacy: .public), built-for=\(builtFor, privacy: .public)")
    }
}
