import Foundation
import os

/// The `catalogue.reader_sync` wire-contract version this client was built for.
///
/// The server publishes a versioned descriptor (`db_store/reader_sync_contract.json`) and advertises
/// its live version as `contract_version` on every `/sync/reader` response (and at
/// `GET /sync/reader/contract`). The transports read that and call `check(_:)`, so a server/client
/// drift surfaces once in the log instead of silently mis-syncing. This lives in the neutral wire layer
/// (not postilla — `AnnotationStore` is the generic seam) because it is the catalogue wire's own version.
public enum ReaderSyncContract {
    /// The contract version this layer was written against.
    public static let builtFor = 1

    /// Compatible iff the server advertises a version at least as new as ours. A missing
    /// `contract_version` (nil) means the server predates the contract — treated as incompatible so it
    /// is noticed. A newer server (additive) is fine.
    public static func compatible(_ serverVersion: Int?) -> Bool {
        guard let v = serverVersion else { return false }
        return v >= builtFor
    }

    private static let log = Logger(subsystem: "catalogue.reader", category: "sync-contract")
    private static let lock = NSLock()
    nonisolated(unsafe) private static var warned = false

    /// Warn once if the server's advertised version is missing or older than what we built for.
    public static func check(_ serverVersion: Int?) {
        guard !compatible(serverVersion) else { return }
        lock.lock(); defer { lock.unlock() }
        guard !warned else { return }
        warned = true
        let got = serverVersion.map(String.init) ?? "none"
        log.warning("reader_sync contract mismatch: server=\(got, privacy: .public), built-for=\(builtFor, privacy: .public)")
    }
}
