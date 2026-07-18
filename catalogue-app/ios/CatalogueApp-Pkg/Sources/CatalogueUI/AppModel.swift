import Foundation
import SwiftUI
import CatalogueCore
import CatalogueData
import CatalogueDesign
import CatalogueReader

/// The app-wide environment object. The **server endpoint** (an abstract reachability strategy — LAN /
/// tunnel / NAS / …, user-chosen in Settings and persisted) is the master from which the API, the
/// reader's byte fetch, and relative art handles all flow. Screens read this via
/// `@Environment(AppModel.self)`.
@MainActor @Observable
public final class AppModel {
    public static let serverKey = "serverEndpoint"
    /// Default endpoint when nothing is set — overridden by the persisted choice, then `CATALOGUE_SERVER`.
    public static let defaultEndpoint: any ServerEndpoint =
        LocalNetworkEndpoint(baseURL: URL(string: "http://127.0.0.1:8000")!)

    /// The current reachability strategy. Changing it (via `setServer`/`setEndpoint`) rebuilds the
    /// API/platform; `serverURL` is its base URL.
    public private(set) var endpoint: any ServerEndpoint
    public var serverURL: URL { endpoint.baseURL }
    public private(set) var platform: Platform
    /// The live API (nil when a test injects a custom platform) — for the catalogue-specific subject
    /// endpoints (`subjects`/`subject`) that aren't part of the neutral `DataPort`.
    public private(set) var api: CatalogueAPI?
    public let prefs: PrefsPort
    /// The reader position-of-record (octavo ReadingStore) the in-app reader persists Locators into.
    public let readingStore: CatalogueReadingStore
    /// Per-document reading settings (octavo ReaderSettingsStore — font size / zoom), shared like
    /// `readingStore` so one file-backed instance serves every reader tab (restored on open, auto-saved
    /// on change through `Octavo.open(settingsStore:)`).
    public let settingsStore = CatalogueReaderSettingsStore()
    /// Per-document back/jump history (persisted so the "Back to …" pill survives reopen).
    public let historyStore = ReaderHistoryStore()
    /// The open reading sessions ("tabs") — which books are open + the active one, shared by every
    /// `ReaderShell` presentation so opening a second book adds a tab rather than a disjoint reader.
    public let openSessions = OpenSessionsStore()
    /// The device-local replica cache (Search/Browse/Detail are served from it, offline-first, like the
    /// PWA). `replicaBox` is the synchronous snapshot the platform's `OfflineFirstData` reads.
    public private(set) var replicaStore: ReplicaStore
    private let replicaBox = ReplicaBox()
    /// The unified update model: ETag-conditional revalidation of the catalogue resources (replica +
    /// starred) on appear/foreground/online/manual, with a push seam for later. Screens read
    /// `syncStatus` for the freshness chip and key their recompute on `dataRevision` so newly-synced
    /// data repaints live (fixing the "new editions invisible until relaunch" staleness).
    public let syncEngine = SyncEngine()
    /// Unsynced local writes (the reader outbox depth), surfaced in the freshness chip.
    public private(set) var pendingWrites = 0
    /// Read-only probes over the shared reader outbox files (the same `annotations.json` /
    /// `bookmarks.json` the reader's `LocalAnnotationStore` / `LocalBookmarkStore` write). Queried on each
    /// sync trigger to surface the total unsynced-write count; depends only on the `OutboxProbe`
    /// abstraction, never on the reader's concrete stores. Add a probe here to fold a new outbox in.
    private let outboxes: [any OutboxProbe] = [LocalAnnotationStore(), LocalBookmarkStore(), LocalOutlineStore()]
    public var themePref: ThemePreference
    public var shelfArt: String
    public var seriesCoverStyle: String      // active SeriesCover style (SERIES_COVER_STYLES key)
    public var shelfTitles: Bool             // show titles below covers on a Shelf (default off)
    public var ctx: ProtocolContext

    /// The starred-edition set (the shared starred list), held in memory like the PWA — a sibling input to
    /// `homeVM` (NOT in the replica, so a toggle stays cheap) AND the one source the cover star overlay
    /// reads its highlight from. `starredIds` keeps newest-starred-first order for the Starred rail;
    /// `starredLookup` is the O(1) membership cache kept in sync.
    public private(set) var starredIds: [Int] = []
    private var starredLookup: Set<Int> = []

    /// Sign-in state for the CURRENT endpoint (drives the Settings account UI). The server gates on a
    /// signed-cookie session (see `Auth.swift`): `.signedIn` when a credential is stored for this host
    /// (the persistent cookie is usually still valid, else refreshed silently on the next 401);
    /// `.anonymous` otherwise; `.unknown` only before an endpoint is resolved / for injected test platforms.
    public enum AuthState: Equatable { case unknown, anonymous, signedIn(String) }
    public private(set) var authState: AuthState = .unknown
    /// Owns the credential + single-flight cookie re-auth for `endpoint`; rebuilt whenever the endpoint
    /// changes. `CatalogueAPI`'s `onUnauthorized` hook is wired to `reauthenticate()` on this.
    private var authSession: AuthSession?

    private let injectedPlatform: Platform?

    /// Resolve the endpoint: the persisted choice (Settings) wins, then the `CATALOGUE_SERVER` env var
    /// (inferred), then the built-in default.
    public static func resolveEndpoint(_ prefs: PrefsPort) -> any ServerEndpoint {
        if let s = prefs.get(serverKey), let data = s.data(using: .utf8),
           let d = try? JSONDecoder().decode(ServerEndpointDescriptor.self, from: data),
           let e = d.make() { return e }
        if let s = ProcessInfo.processInfo.environment["CATALOGUE_SERVER"], let e = ServerEndpoints.infer(from: s) { return e }
        return defaultEndpoint
    }

    public init(platform: Platform? = nil, prefs: PrefsPort? = nil) {
        let p = prefs ?? UserDefaultsPrefs()
        self.prefs = p
        self.readingStore = CatalogueReadingStore()
        self.injectedPlatform = platform
        let e = AppModel.resolveEndpoint(p)
        self.endpoint = e
        if let platform {
            self.platform = platform
            self.api = nil
            self.replicaStore = ReplicaStore(api: nil)
        } else {
            // Cookie-auth session for this endpoint (credential loaded from the keychain if signed in
            // before), and the API wired to refresh it silently on a 401.
            let account = e.baseURL.host ?? e.baseURL.absoluteString
            let creds = Keychain.load(account: account)
            let auth = AuthSession(endpoint: e, credentials: creds)
            self.authSession = auth
            self.authState = creds.map { AuthState.signedIn($0.username) } ?? .anonymous
            let a = CatalogueAPI(endpoint: e, onUnauthorized: { await auth.reauthenticate() })
            self.api = a
            self.replicaStore = ReplicaStore(api: a)
            // Offline-first: Search/Browse/Detail served from the cached replica (a synchronous
            // snapshot via replicaBox), with the live API only as the fallback. Browse has no live
            // endpoint, so without this it would throw AdapterUnsupported — the replica path is the
            // real implementation. Mirrors the PWA's "load the replica, then serve from it".
            let box = replicaBox
            // Reachability predicate: read the engine's thread-safe online flag so full-text Content
            // search short-circuits to "unavailable" offline instead of hanging on the dead server, and
            // the Tier-2 VMs paint the offline state. (Search/Browse/Detail already prefer the replica.)
            let online = syncEngine.onlineState
            let isOffline: @Sendable () -> Bool = { !online.isOnline }
            self.platform = LivePlatform(
                data: OfflineFirstData(live: a, replica: { box.value }, isOffline: isOffline),
                prefs: p, isOffline: isOffline)
        }
        self.themePref = ThemePreference(pref: p.get("theme"))
        self.shelfArt = p.get("shelfArt") == "spine" ? "spine" : "cover"
        let set0 = p.get("setStyle")
        self.seriesCoverStyle = SERIES_COVER_STYLES.contains { $0.key == set0 } ? set0! : SERIES_COVER_DEFAULT
        self.shelfTitles = p.get("shelfTitles") == "on"
        self.ctx = ProtocolContext(local: false, desktop: false)
        if injectedPlatform == nil { registerSyncResources() }   // live app only (tests inject a platform)
    }

    /// Register the catalogue's Shape-A (ETag) resources with the engine. The closures read the CURRENT
    /// `replicaStore`/`api` at call time, so they survive an endpoint switch without re-registration.
    private func registerSyncResources() {
        syncEngine.register(ClosureResource(id: "replica") { [weak self] in
            await self?.revalidateReplicaOutcome() ?? .unchanged })
        syncEngine.register(ClosureResource(id: "starred") { [weak self] in
            await self?.revalidateStarredOutcome() ?? .unchanged })
    }

    /// Resolve a server-relative path (`/edition/…`) against the endpoint; pass through absolute URLs.
    public func url(_ path: String?) -> URL? {
        guard let path, !path.isEmpty else { return nil }
        if path.hasPrefix("http://") || path.hasPrefix("https://") { return URL(string: path) }
        return URL(string: path, relativeTo: serverURL)
    }

    /// Set the endpoint from a typed address (kind inferred: `https://…` → tunnel, else LAN). Returns
    /// false on a bad URL. For explicit control of kind/auth-headers, use `setEndpoint`.
    @discardableResult
    public func setServer(_ text: String) -> Bool {
        guard let e = ServerEndpoints.infer(from: text) else { return false }
        setEndpoint(e); return true
    }

    /// Point the app at a specific endpoint strategy: persist its descriptor, rebuild API/platform.
    /// Screens keyed on `serverURL` re-fetch automatically. (No-op rebuild for an injected platform.)
    public func setEndpoint(_ e: any ServerEndpoint) {
        if let data = try? JSONEncoder().encode(e.descriptor), let s = String(data: data, encoding: .utf8) {
            prefs.set(AppModel.serverKey, s)
        }
        endpoint = e
        if injectedPlatform == nil {
            let account = e.baseURL.host ?? e.baseURL.absoluteString
            let creds = Keychain.load(account: account)
            let auth = AuthSession(endpoint: e, credentials: creds)
            authSession = auth
            authState = creds.map { AuthState.signedIn($0.username) } ?? .anonymous
            let a = CatalogueAPI(endpoint: e, onUnauthorized: { await auth.reauthenticate() })
            api = a
            replicaStore = ReplicaStore(api: a)
            replicaBox.set(nil)                       // a different server → drop the old replica
            let box = replicaBox
            let online = syncEngine.onlineState
            let isOffline: @Sendable () -> Bool = { !online.isOnline }
            platform = LivePlatform(
                data: OfflineFirstData(live: a, replica: { box.value }, isOffline: isOffline),
                prefs: prefs, isOffline: isOffline)
        }
    }

    // ── sign-in (cookie session) ─────────────────────────────────────────────────
    /// Sign in against the current server: POST the credential to `/login` (via the endpoint's
    /// `AuthSession`), and on success persist it in the keychain so the cookie can be re-minted silently
    /// when it expires. Returns whether the credential was accepted (`false` on a wrong password or a
    /// server that can't be reached).
    @discardableResult
    public func signIn(username: String, password: String) async -> Bool {
        guard let authSession else { return false }
        let creds = Credentials(username: username, password: password)
        do { try await authSession.logIn(creds) } catch { return false }
        Keychain.save(creds, account: serverURL.host ?? serverURL.absoluteString)
        authState = .signedIn(username)
        return true
    }

    /// Sign out: forget the credential + delete the session cookie for this host, so the next request
    /// is anonymous (and the server re-challenges).
    public func signOut() async {
        await authSession?.clear()
        Keychain.delete(account: serverURL.host ?? serverURL.absoluteString)
        if let host = serverURL.host {
            let store = HTTPCookieStorage.shared
            for c in store.cookies ?? [] where c.domain == host || c.domain == "." + host {
                store.deleteCookie(c)
            }
        }
        authState = .anonymous
    }

    /// The best-available replica for an immediate paint: the in-memory box, else the on-disk cache
    /// (offline-first). Also kicks a background revalidation (`.appear`) so a stale/absent copy refreshes
    /// — screens repaint when `dataRevision` bumps. Search/Browse/Detail go through the platform's
    /// `OfflineFirstData`; Home reads this directly for `homeVM`.
    public func loadedReplica() async -> Replica? {
        if replicaBox.value == nil { replicaBox.set(await replicaStore.cached()) }
        if api != nil { Task { await syncEngine.refresh(.appear); await refreshPendingWrites() } }
        return replicaBox.value
    }
    /// Force a replica revalidation (used by the manual pull / Settings). Routes through the engine so a
    /// change bumps `dataRevision`; returns the current snapshot.
    @discardableResult
    public func refreshReplica() async -> Replica? {
        _ = await syncEngine.refresh(.manual, ids: ["replica"])
        return replicaBox.value
    }

    // ── unified update model surface ─────────────────────────────────────────────
    /// The freshness chip spec, composed from the engine's lifecycle state + the replica stamp + the
    /// local outbox depth (the domain facts the engine doesn't own). Rendered by `SyncStatusPill`.
    public var syncStatus: SyncStatusVM {
        var s = syncEngine.state
        s.exportedAt = replicaBox.value?.exportedAt
        s.pendingWrites = pendingWrites
        return syncVM(s)
    }
    /// Bumped whenever new data lands; screens key their recompute on it to live-update.
    public var dataRevision: Int { syncEngine.dataRevision }
    /// Trigger a catalogue refresh (replica + starred) for a reason (appear/foreground/online/manual),
    /// and re-read the outbox depth (marks may have flushed on reconnect, or been queued while offline).
    @discardableResult
    public func refresh(_ reason: SyncReason) async -> Bool {
        let changed = await syncEngine.refresh(reason)
        await refreshPendingWrites()
        return changed
    }
    /// Update the surfaced unsynced-writes count (called by the reader outbox).
    public func setPendingWrites(_ n: Int) { pendingWrites = n }
    /// Re-read the reader outbox depth through the `OutboxProbe` abstraction and publish it to the
    /// freshness chip. Cheap (a small JSON read); called on every sync trigger + on catalogue-screen
    /// appear, so returning from an offline reading session shows the pending count without a manual poll.
    public func refreshPendingWrites() async {
        var total = 0
        for probe in outboxes { total += await probe.pendingWriteCount() }
        pendingWrites = total
    }

    /// Revalidate the replica; publish the snapshot and report whether it changed.
    private func revalidateReplicaOutcome() async -> SyncOutcome {
        do {
            let (changed, replica) = try await replicaStore.revalidate()
            replicaBox.set(replica)
            return changed ? .updated : .unchanged
        } catch { return Self.outcome(for: error) }
    }
    /// Revalidate the shared starred set (no ETag on the wire yet, so change is detected by comparison).
    private func revalidateStarredOutcome() async -> SyncOutcome {
        guard let api else { return .unchanged }
        do {
            let payload = try await api.starred()
            guard payload.editions != starredIds else { return .unchanged }
            publishStarred(payload.editions)
            return .updated
        } catch { return Self.outcome(for: error) }
    }
    /// Map a fetch error to the sync outcome — connectivity failures read as `.offline` (flip the chip);
    /// anything else is a real `.failed` to surface.
    private static func outcome(for error: Error) -> SyncOutcome {
        if let u = error as? URLError,
           [.notConnectedToInternet, .cannotConnectToHost, .networkConnectionLost,
            .timedOut, .cannotFindHost, .dataNotAllowed].contains(u.code) {
            return .offline
        }
        return .failed((error as NSError).localizedDescription)
    }

    // ── starred editions ────────────────────────────────────────────────────────
    /// Whether an edition is currently starred — the cover overlay's highlight source (live, so a
    /// toggle anywhere reflects everywhere the cover shows).
    public func isStarred(_ eid: Int) -> Bool { starredLookup.contains(eid) }

    private func publishStarred(_ ids: [Int]) { starredIds = ids; starredLookup = Set(ids) }

    /// Load the shared starred set (fetched at launch + after a star changes elsewhere). No-op when a
    /// test injects a platform (no live API).
    @discardableResult
    public func refreshStarred() async -> [Int] {
        guard let api else { return starredIds }
        if let payload = try? await api.starred() { publishStarred(payload.editions) }
        return starredIds
    }

    /// Toggle a star with an optimistic update (rolled back on failure). The shared
    /// `LibraryCore.starredRequest` mapper is executed by the API adapter, so iOS issues the exact
    /// request web/PWA do; each write returns the fresh list, which becomes the authoritative order.
    public func toggleStar(_ eid: Int) async {
        guard let api else { return }
        let want = !starredLookup.contains(eid)
        let prev = starredIds
        publishStarred(want ? [eid] + starredIds.filter { $0 != eid } : starredIds.filter { $0 != eid })
        do { publishStarred(try await api.setStarred(eid, want).editions) }
        catch { publishStarred(prev) }
    }

    /// The endpoint the reader uses to fetch holding bytes with the same auth as the metadata API.
    public func holdingBytes() -> HoldingBytes { HoldingBytes(endpoint: endpoint) }

    /// Persist a theme choice (`auto` removes the key → follow OS), mirroring `settingsVM` semantics.
    public func setTheme(_ value: String) {
        if value == "light" || value == "dark" { prefs.set("theme", value) } else { prefs.remove("theme") }
        themePref = ThemePreference(pref: prefs.get("theme"))
    }
    public func setShelfArt(_ value: String) {
        let v = value == "spine" ? "spine" : "cover"
        prefs.set("shelfArt", v); shelfArt = v
    }
    /// Persist the SeriesCover style (mirrors the web's `setStyle` pref — the shared neutral key).
    public func setSeriesCoverStyle(_ value: String) {
        let v = SERIES_COVER_STYLES.contains { $0.key == value } ? value : SERIES_COVER_DEFAULT
        prefs.set("setStyle", v); seriesCoverStyle = v
    }
    /// Persist whether Shelves show titles below covers (neutral `shelfTitles` pref; default off).
    public func setShelfTitles(_ on: Bool) {
        prefs.set("shelfTitles", on ? "on" : "off"); shelfTitles = on
    }
}
