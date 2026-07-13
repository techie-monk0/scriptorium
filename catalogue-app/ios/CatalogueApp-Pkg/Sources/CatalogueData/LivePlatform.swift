import Foundation
import CatalogueCore

/// `PrefsPort` backed by `UserDefaults` (the native analogue of the web adapter's `localStorage`).
public final class UserDefaultsPrefs: PrefsPort, @unchecked Sendable {
    private let defaults: UserDefaults
    public init(_ defaults: UserDefaults = .standard) { self.defaults = defaults }
    public func get(_ key: String) -> String? { defaults.string(forKey: key) }
    public func set(_ key: String, _ value: String) { defaults.set(value, forKey: key) }
    public func remove(_ key: String) { defaults.removeObject(forKey: key) }
}

/// The live `Platform` the SwiftUI app supplies to the Tier-2 presenter: API for data, native route
/// mapping, UserDefaults prefs, and an offline predicate (a reachability hook). Mirrors the
/// `{data, nav, prefs, isOffline}` adapter object the web supplies — same presenter runs above it.
public struct LivePlatform: Platform {
    public let data: DataPort
    public let nav: NavPort
    public let prefs: PrefsPort
    private let offlineProvider: @Sendable () -> Bool

    public init(api: CatalogueAPI, prefs: PrefsPort, nav: NavPort = NativeNav(),
                isOffline: @escaping @Sendable () -> Bool = { false }) {
        self.data = api
        self.prefs = prefs
        self.nav = nav
        self.offlineProvider = isOffline
    }

    /// Supply any `DataPort` (e.g. `OfflineFirstData` — replica-served Search/Browse/Detail with a
    /// live fallback), keeping the same nav/prefs/offline adapter shape.
    public init(data: DataPort, prefs: PrefsPort, nav: NavPort = NativeNav(),
                isOffline: @escaping @Sendable () -> Bool = { false }) {
        self.data = data
        self.prefs = prefs
        self.nav = nav
        self.offlineProvider = isOffline
    }

    public func isOffline() -> Bool { offlineProvider() }
}

/// A tiny thread-safe holder for the latest replica snapshot, so `OfflineFirstData`'s synchronous
/// `() -> Replica?` provider can read it off the `ReplicaStore` actor without awaiting.
public final class ReplicaBox: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: Replica?
    public init() {}
    public var value: Replica? { lock.lock(); defer { lock.unlock() }; return _value }
    public func set(_ v: Replica?) { lock.lock(); _value = v; lock.unlock() }
}
