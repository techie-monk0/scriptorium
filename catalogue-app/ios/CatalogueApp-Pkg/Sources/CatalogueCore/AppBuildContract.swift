import Foundation
import os

/// The app-version handshake — the native mirror of `static/js/app-version.js` (and a sibling of
/// `ReaderSyncContract`, which versions the reader WIRE; this versions the SERVER BUILD).
///
/// The server advertises `{ app_build, server_stale }` on `GET /api/v1/health` (and `GET /version`).
///   * `app_build`   identifies the build the server process is running. The app records the FIRST
///                   build it sees from a server as its baseline; if a later probe reports a DIFFERENT
///                   build the server was restarted/redeployed underneath us (→ `.outdated`, refresh).
///   * `server_stale` is True when the server is running older code than what's on disk (a restart is
///                   pending) — surfaced as `.serverStale` so the operator knows to restart it.
///
/// Forgiving like the reader_sync check: a server that omits these fields (older build) reads as
/// `.ok`, never a false alarm.
public enum AppBuildStatus: String, Equatable, Sendable {
    case ok            // in sync with the server build
    case outdated      // server build changed since we connected — client should refresh
    case serverStale   // server is running stale code — it needs a restart
}

public enum AppBuildContract {
    /// Pure decision, byte-for-byte the same rule as app-version.js `classify()`:
    /// `server_stale` wins; else a live build that differs from our baseline is `.outdated`; else ok.
    public static func classify(live: String?, serverStale: Bool?, baseline: String?) -> AppBuildStatus {
        if serverStale == true { return .serverStale }
        if let base = baseline, let live, live != base { return .outdated }
        return .ok
    }
}

/// Stateful tracker the app feeds each `/api/v1/health` (or `/version`) response. It latches the first
/// build seen as the baseline and reports the current status. Value type + no framework dependency so
/// it's trivially unit-testable and reusable by any surface.
public struct AppBuildWatcher: Equatable, Sendable {
    public private(set) var baseline: String?
    public private(set) var status: AppBuildStatus = .ok

    public init() {}

    /// Update from a raw (build, stale) pair. Returns the new status.
    @discardableResult
    public mutating func update(live: String?, serverStale: Bool?) -> AppBuildStatus {
        if baseline == nil, let live { baseline = live }        // first build observed = the baseline
        status = AppBuildContract.classify(live: live, serverStale: serverStale, baseline: baseline)
        return status
    }

    /// Convenience: update straight from a decoded `Health`.
    @discardableResult
    public mutating func update(_ health: Health) -> AppBuildStatus {
        update(live: health.appBuild, serverStale: health.serverStale)
    }

    /// Drop the baseline so the NEXT `update` re-latches to whatever the server is running now — i.e.
    /// "I've accepted the current build". Used after the user acts on an `.outdated` prompt (refreshes
    /// against the new server) so the notice clears instead of nagging until relaunch.
    public mutating func reset() {
        baseline = nil
        status = .ok
    }
}

/// Warn-once logger, same ergonomics as `ReaderSyncContract.check(_:)`, for call sites that only want
/// a log line (the visible Notice is built at the UI layer from `AppBuildWatcher.status`).
public enum AppBuildLog {
    private static let log = Logger(subsystem: "catalogue.app", category: "app-version")
    private static let lock = NSLock()
    nonisolated(unsafe) private static var warnedFor: AppBuildStatus? = nil

    public static func note(_ status: AppBuildStatus, live: String?, baseline: String?) {
        guard status != .ok else { return }
        lock.lock(); defer { lock.unlock() }
        guard warnedFor != status else { return }
        warnedFor = status
        log.warning("app-version \(status.rawValue, privacy: .public): live=\(live ?? "none", privacy: .public) baseline=\(baseline ?? "none", privacy: .public)")
    }
}
