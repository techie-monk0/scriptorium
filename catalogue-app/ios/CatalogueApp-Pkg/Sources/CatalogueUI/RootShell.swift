import SwiftUI
import CatalogueCore
import CatalogueDesign
import CatalogueReader

/// The app shell — a native `TabView` (the recommended nav form from ios_native_plan.md §9), each tab
/// its own `NavigationStack` that resolves `AppRoute` to detail/subject screens. Honors `protocolVisible`
/// so sections gate identically to web/PWA (a phone context hides desktop-only sections).
public struct RootShell: View {
    @Environment(AppModel.self) private var app
    @Environment(\.scenePhase) private var scenePhase

    public init() {}

    @State private var selection = "home"
    @State private var showReader = false
    // App-version handshake surfaced app-wide (not just Settings): a popup when the server was rebuilt
    // under us (.outdated → refresh) or is running stale code (.serverStale → restart it).
    @State private var versionNotice: Notice?

    public var body: some View {
        TabView(selection: $selection) {
            ForEach(tabs, id: \.section.key) { tab in
                NavigationStack {
                    tab.screen
                        .id(app.serverURL)   // server change → fresh screen identity → re-fetch
                        .navigationDestination(for: AppRoute.self) { route in
                            switch route {
                            case .detail(let eid): DetailScreen(eid: eid)
                            case .subject(let name): SubjectScreen(name: name)
                            }
                        }
                        // The shared freshness chip rides in every tab's nav bar (the surface render of
                        // `syncVM`), so status is visible everywhere the catalogue is.
                        .toolbar { ToolbarItem(placement: .automatic) { SyncStatusPill() } }
                }
                .tabItem { Label(tab.section.label, systemImage: tab.section.icon) }
                .tag(tab.section.key)
            }
        }
        // Returning to the foreground revalidates the catalogue (a cheap 304 when unchanged), so new
        // editions/series picked up while backgrounded appear without a relaunch.
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await app.refresh(.foreground) } }
        }
        // "Read" isn't a browsing tab — selecting it opens the reader directly (most-recent open book)
        // and returns you to the tab you were on when you dismiss. No intermediate hub screen.
        .onChange(of: selection) { old, new in
            if new == "read" { showReader = true; selection = old }
        }
        // Surface the app-version handshake app-wide. Fires only on a transition (ok → outdated/stale),
        // so once dismissed it stays dismissed until the status changes again.
        .onChange(of: app.appBuild.status) { _, status in
            versionNotice = versionNoticeFor(status)
        }
        .noticePopup($versionNotice)
        #if canImport(UIKit)
        .fullScreenCover(isPresented: $showReader) {
            ReaderShell(store: app.openSessions, endpoint: app.endpoint, readingStore: app.readingStore,
                        settingsStore: app.settingsStore, historyStore: app.historyStore,
                        starAccessory: { e in e.map { AnyView(StarButton(eid: $0).environment(app)) } ?? AnyView(EmptyView()) })
        }
        #endif
    }

    /// The popup for a handshake status (nil when in sync). `.outdated` offers a data refresh; a native
    /// app can't reload its own code, so `.serverStale` can only tell the user to restart the server.
    private func versionNoticeFor(_ status: AppBuildStatus) -> Notice? {
        switch status {
        case .ok:
            return nil
        case .outdated:
            return Notice(
                icon: "arrow.down.circle",
                title: "The library server was updated",
                message: "Refresh to pull the latest.",
                actions: [
                    NoticeAction("Refresh", prominent: true) {
                        versionNotice = nil
                        Task { await app.forceResync() }
                    },
                    NoticeAction("Later", role: .cancel) { versionNotice = nil },
                ])
        case .serverStale:
            return Notice(
                icon: "exclamationmark.arrow.triangle.2.circlepath",
                title: "Server needs a restart",
                message: "It’s running older code than what’s on disk. Restart it, then pull to refresh.",
                actions: [NoticeAction.close("OK") { versionNotice = nil }])
        }
    }

    private struct Tab { let section: AppSection; let screen: AnyView }

    /// Tabs come from the SHARED `APP_SECTIONS` manifest — label, icon, order and gating live there
    /// (so the Search→Books / Browse→Search rename lands here automatically, in step with web/PWA).
    /// This read-only app implements the reader subset: it maps each section key to its native screen
    /// and omits the editor sections (scan/capture/review) it doesn't provide.
    private var tabs: [Tab] {
        var screens: [String: AnyView] = [
            "home":     AnyView(HomeScreen()),
            "books":    AnyView(SearchScreen()),    // the book finder (metadata)
            "search":   AnyView(BrowseScreen()),    // the cross-entity finder
            "content":  AnyView(ContentScreen()),
            "wishlist": AnyView(WishlistScreen()),
            "settings": AnyView(SettingsScreen()),
        ]
        #if canImport(UIKit)
        screens["read"] = AnyView(ReadingHubScreen())   // reader hub (ReaderShell is UIKit-only)
        #endif
        return APP_SECTIONS.compactMap { section in
            guard let screen = screens[section.key], protocolVisible(section.protocol, app.ctx) else { return nil }
            return Tab(section: section, screen: screen)
        }
    }
}

/// The top-level view an app target embeds. Themes the whole tree from the user's preference and
/// supplies the `AppModel` to the environment.
///
///     @main struct CatalogueApp: App {
///       var body: some Scene { WindowGroup { CatalogueAppRoot() } }
///     }
///
/// The server is resolved from Settings (persisted endpoint) → `CATALOGUE_SERVER` env → default, so
/// the app needs no launch-time configuration.
public struct CatalogueAppRoot: View {
    @State private var app: AppModel

    public init() { _app = State(initialValue: AppModel()) }
    public init(model: AppModel) { _app = State(initialValue: model) }

    public var body: some View {
        ThemedRoot(app.themePref) {
            RootShell().environment(app)
        }
    }
}
