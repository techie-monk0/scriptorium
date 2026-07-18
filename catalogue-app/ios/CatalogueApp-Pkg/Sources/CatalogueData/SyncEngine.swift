import Foundation
import Observation
import CatalogueCore
#if canImport(Network)
import Network
#endif

/// Why a refresh fired — policy + telemetry. `pushed` is reserved for the (future) push transport.
public enum SyncReason: String, Sendable, CaseIterable {
    case launch, appear, foreground, online, manual, pushed
}

/// The result of revalidating one resource.
public enum SyncOutcome: Sendable {
    case updated            // new data was fetched + persisted (bump the data revision)
    case unchanged          // 304 / no delta
    case offline            // couldn't reach the server
    case failed(String)     // a real error (surface it)
}

/// One syncable thing — replica / starred / wishlist / `reader:<holdingId>`. Owns its own cursor (ETag
/// or `rev`), how to revalidate, and how to persist. Abstracts Shape-A (ETag snapshot) and Shape-B
/// (rev-cursor delta) behind one call, so the engine drives them identically.
public protocol SyncResource: AnyObject, Sendable {
    var id: String { get }
    func revalidate() async -> SyncOutcome
}

/// A resource backed by an async closure — the common case (the concrete revalidation lives in a store
/// or on `AppModel`; this just names it and gives it an id for the registry / push routing).
public final class ClosureResource: SyncResource, @unchecked Sendable {
    public let id: String
    private let work: @Sendable () async -> SyncOutcome
    public init(id: String, work: @escaping @Sendable () async -> SyncOutcome) { self.id = id; self.work = work }
    public func revalidate() async -> SyncOutcome { await work() }
}

/// How the engine learns it should pull — the swappable strategy (the `ServerEndpoint` pattern applied
/// to change-delivery). Pull-only today: `subscribe` is a no-op, so the engine only refreshes on its
/// triggers. A future `EventStreamTransport` (SSE `/api/v1/events`) implements `subscribe` to call
/// `onChange(resourceId)` on a server ping — dropping in push needs ZERO changes to `SyncEngine`.
public protocol SyncTransport: Sendable {
    func subscribe(_ onChange: @escaping @Sendable (String) -> Void)
}
public extension SyncTransport {
    func subscribe(_ onChange: @escaping @Sendable (String) -> Void) {}
}

/// The pull-only transport (default). No push channel — the engine refreshes on appear/foreground/
/// online/manual only.
public struct PullTransport: SyncTransport {
    public init() {}
}

/// A thread-safe mirror of "can we reach the server right now" — the ADAPTER behind the data layer's
/// `isOffline` predicate. `SyncEngine` is `@MainActor`, but the offline check happens inside async data
/// adapters off the main actor, so they read this lock-guarded flag instead of the actor-isolated
/// `SyncState`. Updated on the main actor whenever the engine's online state changes.
public final class OnlineState: @unchecked Sendable {
    private let lock = NSLock()
    private var _online: Bool
    public init(_ online: Bool = true) { _online = online }
    public var isOnline: Bool { lock.lock(); defer { lock.unlock() }; return _online }
    func set(_ v: Bool) { lock.lock(); _online = v; lock.unlock() }
}

/// Network reachability (`NWPathMonitor`), reported as a simple online/offline signal. The engine uses
/// it to flip the offline chip and to auto-refresh the moment connectivity returns (like the PWA's
/// `online` event).
final class Reachability: @unchecked Sendable {
    var onChange: (@Sendable (Bool) -> Void)?
    #if canImport(Network)
    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "catalogue.reachability")
    private var started = false
    func start() {
        guard !started else { return }
        started = true
        monitor.pathUpdateHandler = { [weak self] path in self?.onChange?(path.status == .satisfied) }
        monitor.start(queue: queue)
    }
    deinit { monitor.cancel() }
    #else
    func start() {}
    #endif
}

/// The **update model** executor — one place that revalidates the registered resources on a trigger,
/// tracks the freshness `SyncState` (→ `syncVM`), and bumps `dataRevision` whenever new data lands so
/// open screens/readers repaint. Transport-agnostic (pull now, push later); single-flights concurrent
/// refreshes so a burst of triggers collapses to one pass per resource.
@MainActor @Observable
public final class SyncEngine {
    /// The lifecycle facts `syncVM` renders (the app augments `exportedAt`/`pendingWrites` from its
    /// domain stores when composing the status chip).
    public private(set) var state = SyncState(online: true)
    /// Monotonic — bumped on every `.updated`. Screens key their recompute on it to live-update.
    public private(set) var dataRevision = 0
    /// A `Sendable` snapshot of `state.online` the off-main data adapters read for their `isOffline`
    /// predicate (they can't touch this main-actor object directly). Kept in lockstep with `state.online`.
    public let onlineState = OnlineState(true)

    private var resources: [String: any SyncResource] = [:]
    private var order: [String] = []
    private var inFlight: Set<String> = []
    private let transport: any SyncTransport
    private let reachability = Reachability()

    public init(transport: any SyncTransport = PullTransport()) {
        self.transport = transport
        reachability.onChange = { [weak self] online in
            Task { @MainActor in self?.reachabilityChanged(online) }
        }
        transport.subscribe { [weak self] id in
            Task { @MainActor in await self?.refresh(.pushed, ids: [id]) }
        }
        reachability.start()
    }

    /// Register a resource (idempotent; re-registering replaces it, keeping order).
    public func register(_ resource: any SyncResource) {
        if resources[resource.id] == nil { order.append(resource.id) }
        resources[resource.id] = resource
    }
    public func unregister(_ id: String) {
        resources[id] = nil
        order.removeAll { $0 == id }
    }

    private func reachabilityChanged(_ online: Bool) {
        let was = state.online
        state.online = online
        onlineState.set(online)
        if online && !was { Task { await refresh(.online) } }   // connectivity returned → pull
    }

    /// Revalidate the targeted resources (all registered, in order, if `ids` is nil). Single-flights per
    /// resource, so overlapping triggers don't double-fetch. Returns whether anything changed.
    @discardableResult
    public func refresh(_ reason: SyncReason, ids: [String]? = nil) async -> Bool {
        let targets = (ids ?? order).filter { resources[$0] != nil && !inFlight.contains($0) }
        guard !targets.isEmpty else { return false }
        targets.forEach { inFlight.insert($0) }
        state.syncing = true
        state.lastError = nil

        var changed = false
        var failure: String?
        var wentOffline = false
        var reached = false     // any resource that answered (200/304) proves the server is reachable
        // Sequential is fine for the handful of small ETag resources; each await releases the main actor
        // during the network wait, so the UI stays responsive. (Parallelize here if the set grows.)
        for id in targets {
            guard let resource = resources[id] else { continue }
            switch await resource.revalidate() {
            case .updated:   changed = true; reached = true
            case .unchanged: reached = true
            case .offline:   wentOffline = true
            case .failed(let e): failure = e
            }
        }

        targets.forEach { inFlight.remove($0) }
        if inFlight.isEmpty { state.syncing = false }
        if changed { dataRevision &+= 1 }
        // A reachable answer clears offline even if connectivity never "changed" (the server was just
        // down, then came back on the same network); a pure connectivity failure sets it.
        if reached { state.online = true } else if wentOffline { state.online = false }
        onlineState.set(state.online)
        state.lastError = failure
        state.lastCheckedAt = ISO8601DateFormatter().string(from: Date())
        return changed
    }
}
