import Foundation

/// The sync engine's observable state — the raw facts the freshness UI is decided from. Impure engines
/// (per surface) produce this; `syncVM` turns it into the surface-agnostic status spec.
public struct SyncState: Equatable, Sendable {
    public var online: Bool
    public var syncing: Bool
    public var lastError: String?
    public var exportedAt: String?      // the replica's `exported_at` stamp (ISO), for the offline chip
    public var lastCheckedAt: String?   // when we last revalidated (advisory; not shown by syncVM)
    public var pendingWrites: Int       // outbox depth (unsynced local writes)

    public init(online: Bool = false, syncing: Bool = false, lastError: String? = nil,
                exportedAt: String? = nil, lastCheckedAt: String? = nil, pendingWrites: Int = 0) {
        self.online = online; self.syncing = syncing; self.lastError = lastError
        self.exportedAt = exportedAt; self.lastCheckedAt = lastCheckedAt; self.pendingWrites = pendingWrites
    }
}

/// The freshness status spec a surface renders (chip label + tone + whether manual pull is allowed).
/// 1:1 with `library-core.js` `syncVM` (golden-tested for parity).
public struct SyncStatusVM: Equatable, Sendable {
    public let state: String        // "live" | "syncing" | "offline" | "error"
    public let label: String
    public let tone: String         // "ok" | "warn" | "error" | "muted"
    public let detail: String?
    public let canPull: Bool
    public init(state: String, label: String, tone: String, detail: String?, canPull: Bool) {
        self.state = state; self.label = label; self.tone = tone; self.detail = detail; self.canPull = canPull
    }
}

/// SHARED freshness spec — maps `SyncState` → the chip every surface paints. Parity-locked to the JS
/// `syncVM`, so the wording/tone/pull-gate stay identical across web/PWA/iOS (and Kotlin later).
public func syncVM(_ s: SyncState) -> SyncStatusVM {
    let day = s.exportedAt.map { String($0.prefix(10)) }        // YYYY-MM-DD, like the PWA chip
    let canPull = s.online && !s.syncing
    func vm(_ state: String, _ label: String, _ tone: String, _ detail: String?) -> SyncStatusVM {
        SyncStatusVM(state: state, label: label, tone: tone, detail: detail, canPull: canPull)
    }
    if s.syncing { return vm("syncing", "Syncing…", "muted", nil) }
    if let e = s.lastError, !e.isEmpty { return vm("error", "Sync failed", "error", e) }
    if !s.online { return vm("offline", day.map { "Offline · \($0)" } ?? "Offline", "warn",
                             s.pendingWrites > 0 ? "\(s.pendingWrites) unsynced" : nil) }
    return vm("live", "Live", "ok", s.pendingWrites > 0 ? "\(s.pendingWrites) syncing" : nil)
}
