import SwiftUI
import CatalogueCore
import CatalogueData
import CatalogueDesign
import CatalogueReader

/// A pushed destination within a navigation stack.
public enum AppRoute: Hashable, Sendable {
    case detail(eid: Int)
    case subject(name: String)   // subject PATH (e.g. "Buddhism/Emptiness") — drives the shared subjectVM
}

// ── Home ──────────────────────────────────────────────────────────────────────
/// Netflix-style splash. The rails (count, order, contents, the Series set) are computed by the
/// shared Tier-2 presenter `LibraryCore.homeVM` from the cached replica + local reading history —
/// the SAME composition the web/PWA use — so this view only PAINTS the view-model. See
/// private/plans/frontend_tiers_and_home_upgrade.md.
public struct HomeScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    @State private var home: HomeVM?
    @State private var loading = true
    // Cached homeVM inputs so a star toggle can recompute the rails synchronously (no re-fetch): the
    // replica + recently-opened ids don't change when you star, only `app.starredIds` does.
    @State private var replica: Replica?
    @State private var recentIds: [Int] = []

    public init() {}

    public var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Spacing.xl) {
                if loading { ProgressView().padding(Spacing.xl) }
                else if home?.empty ?? true {
                    StatusBanner(text: "No books in the catalogue yet. Use Scan or Capture to add some.")
                }
                ForEach(home?.rails ?? [], id: \.identity) { rail in
                    HomeRailView(rail: rail)
                }
            }
            .padding(.vertical, Spacing.lg)
        }
        .navigationTitle("Library")
        .catalogueRefreshable()
        .task { await load() }
        // Star a book anywhere → recompute the rails from the SAME Tier-2 homeVM so the Starred rail
        // appears/updates/vanishes live. Stable HomeRail.identity keeps the other rails' scroll intact.
        .onChange(of: app.starredIds) { _, ids in
            guard let replica else { return }
            home = LibraryCore.homeVM(replica, recentIds: recentIds, starredIds: ids)
        }
        // New catalogue data landed (a background/foreground/manual refresh found a changed replica) →
        // rebuild the rails so a new edition / series member appears without a relaunch.
        .onChange(of: app.dataRevision) { _, _ in Task { await load() } }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        // The replica is the shared cache the whole app serves from (offline-first); Home reads it
        // directly to compute homeVM. loadedReplica() serves the cached copy instantly and kicks a
        // background revalidation (starred rides along as a registered sync resource).
        guard let replica = await app.loadedReplica() else { home = nil; return }
        let recents = await recentEditionIds(replica)
        self.replica = replica; self.recentIds = recents
        home = LibraryCore.homeVM(replica, recentIds: recents, starredIds: app.starredIds)
    }

    /// Turn the local reading history (octavo `ReadingStore`, keyed `holding:<id>`) into recently-opened
    /// EDITION ids by mapping each holding back through the replica — the input the "Recently opened" rail needs.
    private func recentEditionIds(_ replica: Replica) async -> [Int] {
        var holdingToEid: [Int: Int] = [:]
        for e in replica.editions { for h in e.holdings { holdingToEid[h.holdingId] = e.editionId } }
        let recents = (try? await app.readingStore.recent(24)) ?? []
        var seen = Set<Int>(); var ids: [Int] = []
        for loc in recents {
            guard let tail = loc.publicationId.split(separator: ":").last,
                  let hid = Int(tail), let eid = holdingToEid[hid], !seen.contains(eid) else { continue }
            seen.insert(eid); ids.append(eid)
        }
        return ids
    }
}

/// One home rail, dispatched by `kind`. recent/added/subject are cover rails; series is a rail of
/// set-tiles that expand a volume drawer in place (mirrors web `Shelf.renderSeriesRail`).
private struct HomeRailView: View {
    let rail: HomeRail
    var body: some View {
        if rail.kind == "series" {
            SeriesRail(rail: rail)
        } else {
            // Subject rails carry an id → the header deep-links to the descendant-inclusive subject page.
            ShelfRowLinks(title: rail.title, cards: rail.cards,
                          headerRoute: rail.kind == "subject" ? AppRoute.subject(name: rail.title) : nil)
        }
    }
}

/// A horizontal cover rail whose tiles push the edition detail; an optional header route deep-links
/// the title (subject rails → the subject page).
private struct ShelfRowLinks: View {
    @Environment(\.tokens) private var tokens
    @Environment(AppModel.self) private var app
    let title: String
    let cards: [Card]
    var headerRoute: AppRoute? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.sm) {
            Group {
                if let headerRoute {
                    NavigationLink(value: headerRoute) {
                        HStack(spacing: Spacing.xs) {
                            Text(title).font(Typography.sectionHeader)
                            Image(systemName: "chevron.right").font(.caption).foregroundStyle(tokens.muted)
                        }
                    }.buttonStyle(.plain)
                } else {
                    Text(title).font(Typography.sectionHeader)
                }
            }
            .foregroundStyle(tokens.fg).padding(.horizontal, Spacing.lg)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .bottom, spacing: Spacing.md) {
                    ForEach(cards) { card in
                        NavigationLink(value: AppRoute.detail(eid: card.eid)) { CardTile(card: card, magnifies: true, showTitle: app.shelfTitles) }
                            .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, Spacing.lg).padding(.top, .shelfBloomHeadroom)
            }
        }
    }
}

/// The "Series" rail: one set-tile per series (box-set cover + volume count). Tapping a set toggles
/// an in-place drawer of its volumes, one open at a time — the native analogue of the web set rail.
private struct SeriesRail: View {
    @Environment(\.tokens) private var tokens
    @Environment(AppModel.self) private var app
    let rail: HomeRail
    @State private var openSet: String?

    var body: some View {
        // Read the active style HERE so changing it in Settings re-renders the whole rail (the tiles
        // get the new style as a value, not via their own environment read).
        let style = app.seriesCoverStyle
        VStack(alignment: .leading, spacing: Spacing.sm) {
            Text(rail.title).font(Typography.sectionHeader).foregroundStyle(tokens.fg)
                .padding(.horizontal, Spacing.lg)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .bottom, spacing: Spacing.md) {
                    ForEach(rail.sets) { set in
                        Button {
                            withAnimation(.snappy) { openSet = (openSet == set.name) ? nil : set.name }
                        } label: { SeriesSetTile(set: set, style: style, isOpen: openSet == set.name) }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, Spacing.lg).padding(.top, .shelfBloomHeadroom)
            }
            if let name = openSet, let set = rail.sets.first(where: { $0.name == name }) {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .bottom, spacing: Spacing.md) {
                        ForEach(set.cards) { card in
                            NavigationLink(value: AppRoute.detail(eid: card.eid)) { CardTile(card: card, magnifies: true, showTitle: app.shelfTitles) }
                                .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, Spacing.lg).padding(.top, .shelfBloomHeadroom)
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }
}

/// A box-set tile: a `SeriesCover` drawn in the user's active style (sized off the book-cover via the
/// shared ratio), a volume-count badge, and an open chevron when expanded.
private struct SeriesSetTile: View {
    @Environment(\.tokens) private var tokens
    let set: HomeSet
    let style: String
    let isOpen: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            SeriesCover(covers: set.cards.map(\.coverUrl), style: style)
                .overlay(alignment: .bottomTrailing) {
                    Text("\(set.count)")
                        .font(Typography.caption).foregroundStyle(.white)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.black.opacity(0.7), in: Capsule()).padding(4)
                }
                .overlay(alignment: .topTrailing) {
                    if isOpen {
                        Image(systemName: "chevron.up.circle.fill")
                            .foregroundStyle(.white, tokens.accent).padding(4)
                    }
                }
            Text(set.name).font(Typography.caption).foregroundStyle(tokens.fg).lineLimit(2)
            Text("\(set.count) vols").font(Typography.caption).foregroundStyle(tokens.muted)
        }
        .frame(width: Spacing.coverWidth * CGFloat(seriesCoverStyleSpec(style)?.wRatio ?? 1))
        .shelfMagnify()
    }
}

// ── Search (metadata) ───────────────────────────────────────────────────────--
public struct SearchScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    @State private var q = ""
    @State private var vm: SearchVM?

    public init() {}

    public var body: some View {
        List {
            if let vm {
                if vm.offline { StatusBanner(text: "Offline — showing cached results.", isWarning: true) }
                if let e = vm.error { StatusBanner(text: e, isWarning: true) }
                if vm.empty && !vm.q.isEmpty { StatusBanner(text: "No books match “\(vm.q)”.") }
                ForEach(vm.cards) { card in
                    NavigationLink(value: AppRoute.detail(eid: card.eid)) { CardRow(card: card) }
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            SwipeStarAction(eid: card.eid)
                        }
                }
            }
        }
        .navigationTitle(sectionFor("books")?.label ?? "Books")   // the book finder (shared manifest)
        .searchable(text: $q, prompt: "Title, author, ISBN…")
        .catalogueRefreshable()
        // Re-run when the query changes OR new catalogue data lands (dataRevision), so results reflect a
        // newly-synced edition without leaving the screen. Replica-served, so re-running is cheap.
        .task(id: "\(q)\u{1}\(app.dataRevision)") { vm = await LibraryCore.searchVM(app.platform, q) }
    }
}

/// A compact one-line result row (cover thumb + title + by). The trailing star shows whether the
/// edition is starred and toggles it in place — the listing-pane affordance (also swipe-to-star).
struct CardRow: View {
    @Environment(\.tokens) private var tokens
    let card: Card
    var body: some View {
        HStack(spacing: Spacing.md) {
            CoverImage(path: card.coverUrl, width: 36)
            VStack(alignment: .leading, spacing: 2) {
                Text(card.displayTitle ?? card.title).font(Typography.body).foregroundStyle(tokens.fg).lineLimit(1)
                if let by = card.by, !by.isEmpty { Text(by).font(Typography.caption).foregroundStyle(tokens.muted).lineLimit(1) }
            }
            Spacer(minLength: Spacing.sm)
            StarButton(eid: card.eid)
        }
    }
}

/// The swipe-to-star list action — a fast listing-pane toggle that reads/writes the shared starred set.
struct SwipeStarAction: View {
    @Environment(AppModel.self) private var app
    let eid: Int
    var body: some View {
        let on = app.isStarred(eid)
        Button { Task { await app.toggleStar(eid) } } label: {
            Label(on ? "Unstar" : "Star", systemImage: on ? "star.slash" : "star.fill")
        }.tint(.yellow)
    }
}

// ── Search (one box + a 4-way mode selector) ────────────────────────────────--
/// The cross-entity finder: a single text box plus a segmented selector that switches WHICH of the
/// shared `SEARCH_FIELDS` to search — Edition (title or number) · Work · Person · Subject/Series. Each
/// mode scopes the shared `browseReplica` to that one group, so the matching is identical across
/// surfaces; only the displayed group changes.
public struct BrowseScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    @State private var q = ""
    @State private var field = SEARCH_FIELDS.first?.key ?? "book_title"
    @State private var vm: BrowseVM?

    public init() {}

    /// The display label + the `browseReplica` group key for each `SEARCH_FIELDS` mode.
    private func label(_ key: String) -> String {
        switch key {
        case "book_title": return "Edition"; case "work_title": return "Work"
        case "person": return "Person"; default: return "Subject/Series"
        }
    }
    private func group(_ key: String) -> String {
        switch key {
        case "book_title": return "editions"; case "work_title": return "works"
        case "person": return "people"; default: return "subjects"
        }
    }
    private var prompt: String {
        switch field {
        case "book_title": return "Edition title or number…"; case "work_title": return "Work title…"
        case "person": return "Author or translator…"; default: return "Subject or series…"
        }
    }

    public var body: some View {
        VStack(spacing: 0) {
            Picker("Search by", selection: $field) {
                ForEach(SEARCH_FIELDS) { f in Text(label(f.key)).tag(f.key) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, Spacing.lg).padding(.vertical, Spacing.sm)
            List {
                if let vm {
                    if vm.offline { StatusBanner(text: "Offline.", isWarning: true) }
                    if let e = vm.error { StatusBanner(text: "Search runs on the offline library; it isn’t loaded yet. (\(e))", isWarning: true) }
                    if vm.empty && !vm.q.isEmpty { StatusBanner(text: "No \(label(field).lowercased()) matches “\(vm.q)”.") }
                    ForEach(vm.groups, id: \.label) { g in
                        ForEach(Array(g.hits.enumerated()), id: \.offset) { _, hit in BrowseHitRow(hit: hit) }
                    }
                }
            }
        }
        .navigationTitle(sectionFor("search")?.label ?? "Search")   // the cross-entity finder (shared manifest)
        .catalogueRefreshable()
        .searchable(text: $q, prompt: prompt)
        .task(id: "\(field)\u{1}\(q)\u{1}\(app.dataRevision)") { vm = await LibraryCore.browseVM(app.platform, q, only: group(field)) }
    }
}

struct BrowseHitRow: View {
    @Environment(\.tokens) private var tokens
    let hit: BrowseHitVM
    var body: some View {
        if case .edition(let eid)? = hit.ref {
            NavigationLink(value: AppRoute.detail(eid: eid)) { label }
        } else if case .subject? = hit.ref {
            NavigationLink(value: AppRoute.subject(name: hit.label)) { label }
        } else { label }
    }
    private var label: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(hit.label).font(Typography.body).foregroundStyle(tokens.fg)
            if !hit.sublabel.isEmpty { Text(hit.sublabel).font(Typography.caption).foregroundStyle(tokens.muted) }
        }
    }
}

// ── Content (full-text) ────────────────────────────────────────────────────--
public struct ContentScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    @State private var q = ""
    @State private var vm: ContentVM?

    public init() {}

    public var body: some View {
        List {
            if let vm {
                if !vm.available { StatusBanner(text: "Full-text search isn’t available offline.", isWarning: true) }
                if let e = vm.error { StatusBanner(text: e, isWarning: true) }
                if vm.empty && !vm.q.isEmpty && vm.available { StatusBanner(text: "No passages match “\(vm.q)”.") }
                ForEach(vm.books) { book in
                    NavigationLink(value: AppRoute.detail(eid: book.eid)) {
                        VStack(alignment: .leading, spacing: Spacing.xs) {
                            Text(book.title).font(Typography.body).foregroundStyle(tokens.fg)
                            ForEach(Array(book.snippets.prefix(3).enumerated()), id: \.offset) { _, s in
                                Text(snippet(s)).font(Typography.caption).foregroundStyle(tokens.muted).lineLimit(2)
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Text")
        .catalogueRefreshable()
        .searchable(text: $q, prompt: "Search inside books")
        .task(id: q) { vm = await LibraryCore.contentVM(app.platform, q) }
    }

    /// Render the server's `[match]…[/match]` markers as a plain highlighted run.
    private func snippet(_ s: String) -> String {
        s.replacingOccurrences(of: "[match]", with: "›").replacingOccurrences(of: "[/match]", with: "‹")
    }
}

// ── Detail ─────────────────────────────────────────────────────────────────--
public struct DetailScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    let eid: Int
    @State private var vm: DetailVM?

    public init(eid: Int) { self.eid = eid }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.lg) {
                if let vm {
                    if vm.missing { StatusBanner(text: "This edition is no longer in the library.") }
                    if vm.offline { StatusBanner(text: "Offline.", isWarning: true) }
                    BookDetailsPane(vm: vm)
                } else { ProgressView() }
            }
            .padding(Spacing.lg)
        }
        .navigationTitle("Book")
        .catalogueRefreshable()
        // Re-fetch when the edition changes or new catalogue data lands (keeps a re-holdinged / retitled
        // edition current); replica-served, so cheap.
        .task(id: "\(eid)\u{1}\(app.dataRevision)") { vm = await LibraryCore.detailVM(app.platform, eid) }
    }
}

/// The selected book's detail pane — composed of the named sections from the shared contract
/// (`BOOK_DETAIL_SECTIONS`): EditionBasics / Holdings / WorksInThisEdition / Connections. The SwiftUI
/// implementation of the cross-surface pane (web renders the same sections server-side; PWA a subset).
/// Each section reads `detailVM` and hides itself when empty.
public struct BookDetailsPane: View {
    let vm: DetailVM
    public init(vm: DetailVM) { self.vm = vm }
    public var body: some View {
        VStack(alignment: .leading, spacing: Spacing.lg) {
            ForEach(BOOK_DETAIL_SECTIONS) { section in
                switch section.key {
                case "basics":      EditionBasics(vm: vm)
                case "holdings":    Holdings(vm: vm, label: section.label)
                case "works":       WorksInThisEdition(vm: vm, label: section.label)
                case "connections": Connections(vm: vm, label: section.label)
                default:            EmptyView()
                }
            }
        }
    }
}

/// EditionBasics — cover, title, by-line, the Published line (publisher · year), and ISBNs.
struct EditionBasics: View {
    @Environment(\.tokens) private var tokens
    let vm: DetailVM
    private var published: String { [vm.publisher, vm.year.map(String.init)].compactMap { $0 }.joined(separator: " · ") }
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.md) {
            HStack(alignment: .top, spacing: Spacing.lg) {
                CoverImage(path: vm.coverUrl, width: 120)
                    // Detail-pane cover also carries the toggle, so it's starrable from here too.
                    .overlay(alignment: .bottomTrailing) { StarButton(eid: vm.eid, onCover: true).padding(4) }
                VStack(alignment: .leading, spacing: Spacing.sm) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(vm.title).font(Typography.title).foregroundStyle(tokens.fg)
                        Spacer(minLength: Spacing.sm)
                        StarButton(eid: vm.eid)
                    }
                    Text(vm.by).font(Typography.body).foregroundStyle(tokens.muted)
                    if !published.isEmpty { Text(published).font(Typography.caption).foregroundStyle(tokens.muted) }
                }
            }
            MetaSection("ISBN", vm.isbns)
            // The edition's Buddhist tradition (auto-hides when unset).
            MetaSection("Tradition", vm.tradition.map { [$0] } ?? [])
        }
    }
}

/// Holdings — the openable copies, each a Read control.
struct Holdings: View {
    @Environment(\.tokens) private var tokens
    let vm: DetailVM
    let label: String
    var body: some View {
        if !vm.holdings.isEmpty {
            VStack(alignment: .leading, spacing: Spacing.sm) {
                Text(label).font(Typography.sectionHeader).foregroundStyle(tokens.subtleFg)
                ForEach(vm.holdings) { h in ReadButton(holding: h, title: vm.title, eid: vm.eid) }
            }
        }
    }
}

/// WorksInThisEdition — the works contained in this edition.
struct WorksInThisEdition: View {
    @Environment(\.tokens) private var tokens
    let vm: DetailVM
    let label: String
    var body: some View {
        if !vm.workTitles.isEmpty {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text("\(label) (\(vm.workTitles.count))").font(Typography.sectionHeader).foregroundStyle(tokens.subtleFg)
                ForEach(vm.workTitles, id: \.self) { Text($0).font(Typography.body).foregroundStyle(tokens.fg) }
            }
        }
    }
}

/// Connections — cross-links: translators, subjects, and OTHER EDITIONS of the contained works (FRBR
/// siblings, navigable to their detail). The web also links authors to person pages; the offline
/// clients stay name-only there (no person screen).
struct Connections: View {
    @Environment(\.tokens) private var tokens
    let vm: DetailVM
    let label: String
    var body: some View {
        if !vm.translators.isEmpty || !vm.subjects.isEmpty || !vm.connections.isEmpty {
            VStack(alignment: .leading, spacing: Spacing.sm) {
                Text(label).font(Typography.sectionHeader).foregroundStyle(tokens.subtleFg)
                MetaSection("Translators", vm.translators)
                MetaSection("Subjects", vm.subjects)
                if !vm.connections.isEmpty {
                    VStack(alignment: .leading, spacing: Spacing.xs) {
                        Text("Other editions").font(Typography.caption).foregroundStyle(tokens.subtleFg)
                        ForEach(vm.connections) { c in
                            NavigationLink(value: AppRoute.detail(eid: c.eid)) {
                                Text(c.title).font(Typography.body).foregroundStyle(tokens.link).lineLimit(1)
                            }
                        }
                    }
                }
            }
        }
    }
}

/// Opens the holding in the in-app reader — presents `ReaderView`, which hosts the octavo engine and
/// persists position via the app's `CatalogueReadingStore`.
struct ReadButton: View {
    @Environment(AppModel.self) private var app
    let holding: Holding
    let title: String
    var eid: Int? = nil                     // the edition this copy belongs to (for the reader's star toggle)
    @State private var showReader = false
    var body: some View {
        Button { showReader = true } label: {
            Label("Read \(holding.kind.map { "(\($0))" } ?? "")", systemImage: "book")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .disabled(!holding.hasFile)
        #if canImport(UIKit)
        .fullScreenCover(isPresented: $showReader) {
            // The native multi-book reader shell (tabs) — the SAME reader the Read tab opens, so tapping
            // a book and tapping Read are identical. Opening this copy focuses/adds its tab.
            ReaderShell(open: OpenBook(holding: holding, title: title, eid: eid),
                        store: app.openSessions, endpoint: app.endpoint, readingStore: app.readingStore,
                        starAccessory: { e in e.map { AnyView(StarButton(eid: $0).environment(app)) } ?? AnyView(EmptyView()) })
        }
        #endif
    }
}

struct MetaSection: View {
    @Environment(\.tokens) private var tokens
    let title: String
    let values: [String]
    init(_ title: String, _ values: [String]) { self.title = title; self.values = values }
    var body: some View {
        if !values.isEmpty {
            VStack(alignment: .leading, spacing: Spacing.xs) {
                Text(title).font(Typography.sectionHeader).foregroundStyle(tokens.subtleFg)
                Text(values.joined(separator: " · ")).font(Typography.body).foregroundStyle(tokens.fg)
            }
        }
    }
}

// ── Subject ────────────────────────────────────────────────────────────────--
/// A subject page — now the SAME shape as web/PWA: it renders the shared `subjectVM` (from the replica,
/// offline-first, like Home) through the SDUI section registry — breadcrumbs + a rail per child topic
/// (+ a leftover rail), or a single grid when the subject has no children. No more flat text list.
public struct SubjectScreen: View {
    @Environment(AppModel.self) private var app
    let name: String
    @State private var sections: [PageSection]?
    @State private var leaf: String = ""

    public init(name: String) { self.name = name }

    public var body: some View {
        ScrollView {
            if let sections {
                SectionsView(sections: sections).padding(.vertical, Spacing.lg)
            } else {
                ProgressView().padding(Spacing.xl)
            }
        }
        .navigationTitle(leaf.isEmpty ? name : leaf)
        .catalogueRefreshable()
        .task { await load() }
        .onChange(of: app.dataRevision) { _, _ in Task { await load() } }
    }

    private func load() async {
        guard let replica = await app.loadedReplica() else { return }
        let vm = LibraryCore.subjectVM(replica, name)
        leaf = vm.leaf
        sections = subjectSections(vm)
    }
}

/// SDUI-lite RENDER REGISTRY (iOS): renders a `[PageSection]` by dispatching each section's `type` to a
/// native component — the parallel of web/PWA's `shelf.js`/`library-ui-dom.js`. Reused by any screen
/// that produces sections (Subject today; Home/Browse next).
struct SectionsView: View {
    let sections: [PageSection]
    var body: some View {
        LazyVStack(alignment: .leading, spacing: Spacing.lg) {
            ForEach(sections) { section in
                switch section.type {
                case "crumbs": SubjectCrumbs(crumbs: section.crumbs)
                case "rail":
                    ShelfRowLinks(title: section.title ?? "", cards: section.cards,
                                  headerRoute: section.subject.map { AppRoute.subject(name: $0) })
                case "grid":
                    SectionGrid(title: section.title ?? "", cards: section.cards)
                default: EmptyView()
                }
            }
        }
    }
}

private struct SubjectCrumbs: View {
    @Environment(\.tokens) private var tokens
    let crumbs: [SubjectCrumb]
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Spacing.xs) {
                ForEach(Array(crumbs.enumerated()), id: \.offset) { i, c in
                    if i > 0 { Image(systemName: "chevron.right").font(.caption2).foregroundStyle(tokens.muted) }
                    NavigationLink(value: AppRoute.subject(name: c.name)) {
                        Text(c.label).font(Typography.caption).foregroundStyle(tokens.link)
                    }.buttonStyle(.plain)
                }
            }.padding(.horizontal, Spacing.lg)
        }
    }
}

private struct SectionGrid: View {
    @Environment(\.tokens) private var tokens
    @Environment(AppModel.self) private var app
    let title: String
    let cards: [Card]
    private let cols = [GridItem(.adaptive(minimum: 92), spacing: Spacing.md)]
    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.sm) {
            if !title.isEmpty {
                Text(title).font(Typography.sectionHeader).foregroundStyle(tokens.fg).padding(.horizontal, Spacing.lg)
            }
            LazyVGrid(columns: cols, alignment: .leading, spacing: Spacing.md) {
                ForEach(cards) { card in
                    NavigationLink(value: AppRoute.detail(eid: card.eid)) {
                        CardTile(card: card, magnifies: false, showTitle: app.shelfTitles)
                    }.buttonStyle(.plain)
                }
            }.padding(.horizontal, Spacing.lg)
        }
    }
}

// ── Settings ───────────────────────────────────────────────────────────────--
public struct SettingsScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    @State private var vm: SettingsVM?
    @State private var serverText = ""
    @State private var status: ConnState = .unknown
    @State private var checking = false
    @State private var username = ""
    @State private var password = ""
    @State private var authBusy = false
    @State private var authError: String?

    enum ConnState { case unknown, ok(String), fail(String), invalid }

    public init() {}

    public var body: some View {
        Form {
            Section {
                TextField("https://library.example  or  192.168.1.10:8000", text: $serverText)
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif
                    .autocorrectionDisabled()
                switch app.authState {
                case .signedIn(let user):
                    HStack { Text("Signed in"); Spacer(); Text(user).foregroundStyle(tokens.muted) }
                    Button("Sign out", role: .destructive) { Task { await app.signOut() } }
                case .anonymous, .unknown:
                    TextField("Username", text: $username)
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        #endif
                        .autocorrectionDisabled()
                    SecureField("Password", text: $password).onSubmit(signIn)
                    HStack {
                        Button("Sign in", action: signIn)
                            .disabled(authBusy || serverText.isEmpty || username.isEmpty || password.isEmpty)
                        Spacer()
                        if authBusy || checking { ProgressView() }
                        else if let authError {
                            Label(authError, systemImage: "exclamationmark.triangle.fill")
                                .font(Typography.caption).foregroundStyle(tokens.warn)
                        } else { statusLabel }
                    }
                }
            } header: { Text("Server") } footer: {
                Text("The catalogue server and your library login (same username/password as the web app). Sign in connects to the server and logs in together. Use your Cloudflare tunnel (https://library.example) from anywhere, or the Mac's LAN address (e.g. http://192.168.1.10:8000) on the same Wi-Fi — http:// (not https://) for a LAN address.")
            }

            if let vm {
                Section {
                    Picker("Theme", selection: Binding(get: { vm.theme }, set: { app.setTheme($0); reload() })) {
                        ForEach(vm.themeOptions, id: \.value) { Text($0.label).tag($0.value) }
                    }
                    Picker("Shelves", selection: Binding(get: { vm.shelfArt }, set: { app.setShelfArt($0); reload() })) {
                        ForEach(vm.shelfOptions, id: \.value) { Text($0.label).tag($0.value) }
                    }
                    Picker("Series covers", selection: Binding(get: { vm.seriesCoverStyle },
                                                               set: { app.setSeriesCoverStyle($0); reload() })) {
                        ForEach(vm.seriesCoverStyles) { Text($0.label).tag($0.key) }
                    }
                    Toggle("Titles on shelves", isOn: Binding(get: { vm.shelfTitles },
                                                              set: { app.setShelfTitles($0); reload() }))
                } header: { Text("Appearance") }
            }
        }
        .navigationTitle("Settings")
        .task { serverText = app.serverURL.absoluteString; reload(); await testConnection() }
    }

    @ViewBuilder private var statusLabel: some View {
        switch status {
        case .unknown: EmptyView()
        case .ok(let host): Label(host, systemImage: "checkmark.circle.fill").font(Typography.caption).foregroundStyle(tokens.ok)
        case .fail(let host): Label("Can’t reach \(host)", systemImage: "exclamationmark.triangle.fill").font(Typography.caption).foregroundStyle(tokens.warn)
        case .invalid: Label("Invalid address", systemImage: "xmark.circle.fill").font(Typography.caption).foregroundStyle(tokens.warn)
        }
    }

    /// Hit `/api/v1/health` so the user gets immediate confirmation the address works.
    private func testConnection() async {
        guard let api = app.api else { return }
        checking = true
        defer { checking = false }
        let host = app.serverURL.host ?? app.serverURL.absoluteString
        do { status = (try await api.health().ok) ? .ok(host) : .fail(host) }
        catch { status = .fail(host) }
    }

    /// Connect + sign in in one step: apply the typed server address (rebuilding the API for it), then
    /// POST the credential to it. On success clear the password field and probe the server.
    private func signIn() {
        guard !serverText.isEmpty, !username.isEmpty, !password.isEmpty else { return }
        guard app.setServer(serverText) else { authError = "Invalid address"; return }
        serverText = app.serverURL.absoluteString
        authBusy = true; authError = nil
        Task {
            let ok = await app.signIn(username: username, password: password)
            authBusy = false
            if ok { password = ""; authError = nil; await testConnection() }
            else { authError = "Wrong login, or can’t reach server" }
        }
    }

    private func reload() { vm = LibraryCore.settingsVM(app.platform) }
}

// ── Wishlist ────────────────────────────────────────────────────────────────────
/// Books wanted but not yet owned. Every backend call routes through the SHARED command mapper
/// (`LibraryCore.wishlistRequest`, executed by `CatalogueAPI.wishlistExec`) and the list renders via
/// the SHARED `LibraryCore.wishlistVM` — the SAME Tier-2 web + PWA use, so no endpoint or message is
/// hardcoded here. A book the resolver can't identify still appears, badged ("Add details" / "Choose
/// edition"), so nothing is silently lost. The last payload is cached in prefs for offline view.
public struct WishlistScreen: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    @State private var vm: WishlistVM?
    @State private var itemsById: [Int: WishlistItemRow] = [:]   // raw rows, for suspected candidates
    @State private var loading = true
    @State private var title = ""
    @State private var author = ""
    @State private var isbn = ""
    @State private var note: String?
    #if os(iOS)
    @State private var showScanner = false
    #endif

    public init() {}

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Spacing.lg) {
                addForm
                if loading { ProgressView().padding(Spacing.xl) }
                else if vm?.empty ?? true {
                    StatusBanner(text: "Your wishlist is empty. Add a book you want above.")
                }
                ForEach(vm?.groups ?? [], id: \.kind) { group in
                    VStack(alignment: .leading, spacing: Spacing.sm) {
                        Text("\(group.title) (\(group.cards.count))")
                            .font(Typography.sectionHeader).foregroundStyle(tokens.fg)
                        ForEach(group.cards) { card in
                            WishlistRow(card: card, suspects: suspectCandidates(card.id),
                                        onDelete: { await act(.remove(id: card.id)) },
                                        onConfirm: { eid in await act(.confirm(id: card.id, editionId: eid)) },
                                        onDecline: { await act(.decline(id: card.id)) })
                        }
                    }
                }
            }
            .padding(Spacing.lg)
        }
        .navigationTitle("Wishlist")
        .task { await load() }
        #if os(iOS)
        .sheet(isPresented: $showScanner) {
            if #available(iOS 16.0, *) {
                ISBNScannerSheet(
                    onCode: { code in showScanner = false; Task { await addScanned(code) } },
                    onCancel: { showScanner = false })
            } else {
                Text("Scanning needs iOS 16+.").padding()
            }
        }
        #endif
    }

    #if os(iOS)
    /// Add an ISBN captured by the in-app camera scanner (source "scan"). The server validates it.
    private func addScanned(_ code: String) async {
        note = "Adding scanned book…"
        await submitAdd(["isbn": .string(code), "source": .string("scan")])
    }
    #endif

    private var addForm: some View {
        VStack(alignment: .leading, spacing: Spacing.sm) {
            TextField("Title (or use ISBN)", text: $title).textFieldStyle(.roundedBorder)
            TextField("Author (optional)", text: $author).textFieldStyle(.roundedBorder)
            TextField("ISBN-13 (optional)", text: $isbn).textFieldStyle(.roundedBorder)
                #if os(iOS)
                .keyboardType(.numberPad)
                #endif
            HStack {
                Button("Add to wishlist") { Task { await add() } }
                    .buttonStyle(.borderedProminent)
                #if os(iOS)
                Button { showScanner = true } label: { Label("Scan", systemImage: "barcode.viewfinder") }
                    .buttonStyle(.bordered)
                #endif
                if let note { Text(note).font(Typography.caption).foregroundStyle(tokens.muted) }
            }
        }
        .padding(Spacing.md)
    }

    private func add() async {
        let i = isbn.trimmingCharacters(in: .whitespaces)
        let t = title.trimmingCharacters(in: .whitespaces)
        let a = author.trimmingCharacters(in: .whitespaces)
        guard !i.isEmpty || !t.isEmpty else { note = "Enter a title or an ISBN."; return }
        note = "Adding…"
        // Build the SHARED add-body shape; the message comes from the SHARED mapper.
        var body: [String: JSONValue] = ["source": .string("manual")]
        if i.isEmpty { body["title"] = .string(t); if !a.isEmpty { body["author"] = .string(a) } }
        else { body["isbn"] = .string(i) }
        await submitAdd(body)
        title = ""; author = ""; isbn = ""
    }

    /// Add via the shared adapter, message via the SHARED `wishlistAddMessage` (one wording everywhere).
    private func submitAdd(_ body: [String: JSONValue]) async {
        do {
            let resp = try await app.api?.addWishlist(body: body)
            note = LibraryCore.wishlistAddMessage(resp ?? WishlistAddResponse())
            await load()
        } catch { note = "Could not add — check the connection." }
    }

    /// Any non-add action (remove / confirm / decline) through the shared intent→request mapper.
    private func act(_ action: WishlistAction) async {
        try? await app.api?.wishlistAct(action)
        await load()
    }

    /// Decode a suspected item's candidate editions (raw `candidates` JSON) for the confirm buttons.
    private func suspectCandidates(_ id: Int) -> [SuspectCandidate] {
        (itemsById[id]?.candidates ?? []).compactMap { jv in
            guard let cid = jv["id"]?.intValue else { return nil }
            return SuspectCandidate(id: cid, title: jv["title"]?.stringValue ?? "edition #\(cid)",
                                    forms: jv["forms"]?.arrayValue?.compactMap { $0.stringValue } ?? [])
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        if let api = app.api, let payload = try? await api.wishlist() {
            if let data = try? CatalogueJSON.encoder.encode(payload) {
                app.prefs.set("wishlist", String(decoding: data, as: UTF8.self))
            }
            itemsById = Dictionary(payload.items.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            vm = LibraryCore.wishlistVM(payload)
            return
        }
        // Offline: fall back to the cached payload so the wishlist still shows.
        if let cached = app.prefs.get("wishlist"),
           let payload = try? CatalogueJSON.decode(WishlistPayload.self, from: Data(cached.utf8)) {
            itemsById = Dictionary(payload.items.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
            vm = LibraryCore.wishlistVM(payload)
        } else {
            vm = nil   // nothing cached + offline → the empty banner (vm?.empty ?? true)
        }
    }
}

/// A candidate catalogue edition for a `suspected` item ("is this the same book you already own?").
struct SuspectCandidate: Identifiable, Equatable { let id: Int; let title: String; let forms: [String] }

/// One wishlist row — cover, title + status badge + meta, a remove action, and (for a `suspected`
/// item) confirm/decline buttons over the candidate editions.
private struct WishlistRow: View {
    @Environment(\.tokens) private var tokens
    let card: WishlistCard
    let suspects: [SuspectCandidate]
    let onDelete: () async -> Void
    let onConfirm: (Int) async -> Void
    let onDecline: () async -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            HStack(alignment: .top, spacing: Spacing.md) {
                CachedImage(url: card.coverUrl.flatMap(URL.init(string:))) { phase in
                    if case .success(let img) = phase {
                        img.resizable().aspectRatio(contentMode: .fill)
                    } else { Rectangle().fill(tokens.muted.opacity(0.2)) }
                }
                .frame(width: 44, height: 60).clipShape(RoundedRectangle(cornerRadius: 3))
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: Spacing.xs) {
                        Text(card.title).font(Typography.caption.bold()).foregroundStyle(tokens.fg).lineLimit(2)
                        if !card.badge.isEmpty {
                            Text(card.badge).font(Typography.caption)
                                .padding(.horizontal, 6).padding(.vertical, 1)
                                .background(tokens.muted.opacity(0.2)).clipShape(Capsule())
                                .foregroundStyle(tokens.muted)
                        }
                    }
                    let meta = [card.by, card.publisher, card.year.map(String.init)]
                        .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
                    if !meta.isEmpty { Text(meta).font(Typography.caption).foregroundStyle(tokens.muted).lineLimit(1) }
                }
                Spacer(minLength: 0)
                Button(role: .destructive) { Task { await onDelete() } } label: { Image(systemName: "xmark") }
                    .buttonStyle(.borderless).foregroundStyle(tokens.muted)
            }
            if card.status == "suspected" {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Might already be in your library — is it one of these?")
                        .font(Typography.caption).foregroundStyle(tokens.muted)
                    ForEach(suspects) { s in
                        Button { Task { await onConfirm(s.id) } } label: {
                            Label(s.forms.isEmpty ? s.title : "\(s.title) (\(s.forms.joined(separator: ", ")))",
                                  systemImage: "checkmark.circle")
                        }.buttonStyle(.bordered).controlSize(.small)
                    }
                    Button("No, different book") { Task { await onDecline() } }
                        .buttonStyle(.borderless).controlSize(.small)
                }
                .padding(.leading, 56)
            }
        }
        .padding(Spacing.sm)
    }
}
