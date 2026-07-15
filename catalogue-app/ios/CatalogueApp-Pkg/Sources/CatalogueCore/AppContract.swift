import Foundation

// Tier-2 shared CONTRACT — a 1:1 port of the enumerations in `library-core.js`. These are the
// single source of truth for cross-surface UI structure: the nav sections and the cover-component
// geometry/style enum. Each frontend (web/PWA/SwiftUI) renders these its own way, but the keys,
// labels, icons, order, gating, and box ratios all come from HERE — so a rename/reorder/resize
// lands everywhere at once. Parity-locked against the JS source via goldens.json (AppContractTests).

// ── App sections (nav manifest) ────────────────────────────────────────────────
/// One app section's metadata. A surface maps `key` → its own route/screen; everything else is shared.
public struct AppSection: Equatable, Sendable, Identifiable {
    public let key: String
    public let label: String
    public let icon: String           // SF Symbol name (native draws it; web/PWA render it as SVG)
    public let `protocol`: String     // visibility gate (see PROTOCOLS): default | local | desktop
    public var id: String { key }
}

/// The canonical, ordered list of the app's sections. `books` = the book finder; `search` = the
/// cross-entity finder; `content` = in-book full-text. Mirror of `LibraryCore.APP_SECTIONS`.
public let APP_SECTIONS: [AppSection] = [
    .init(key: "home",     label: "Home",     icon: "house",               protocol: "default"),
    .init(key: "books",    label: "Books",    icon: "books.vertical",      protocol: "default"),
    .init(key: "read",     label: "Read",     icon: "book",                protocol: "default"),
    .init(key: "search",   label: "Search",   icon: "magnifyingglass",     protocol: "default"),
    .init(key: "content",  label: "Text",     icon: "doc.text",            protocol: "default"),
    .init(key: "ask",      label: "Ask",      icon: "text.bubble",         protocol: "default"),
    .init(key: "review",   label: "Review",   icon: "checklist",           protocol: "desktop"),
    .init(key: "scan",     label: "Scan",     icon: "viewfinder",          protocol: "desktop"),
    .init(key: "capture",  label: "Capture",  icon: "camera",              protocol: "default"),
    .init(key: "wishlist", label: "Wishlist", icon: "star",                protocol: "default"),
    .init(key: "settings", label: "Settings", icon: "slider.horizontal.3", protocol: "default"),
]

/// Section metadata by key (a surface maps the key to its own route/screen).
public func sectionFor(_ key: String) -> AppSection? { APP_SECTIONS.first { $0.key == key } }

// ── Cover component contract (geometry + style enum) ───────────────────────────
/// A book cover is a 2:3 poster — height = `BOOK_COVER_ASPECT` × width.
public let BOOK_COVER_ASPECT: Double = 1.5

/// One SeriesCover style: its key, label, and box size as a RATIO of the book-cover width/height,
/// so a SeriesCover always reads as a sized-up sibling of a BookCover on every surface.
public struct SeriesCoverStyleSpec: Equatable, Sendable, Identifiable {
    public let key: String
    public let label: String
    public let wRatio: Double         // box width  ÷ book-cover width
    public let hRatio: Double         // box height ÷ book-cover height
    public var id: String { key }
}

/// The SeriesCover styles, in pick order. The per-toolkit SeriesCover view IMPLEMENTS each style;
/// only this enum (keys/labels/ratios/default) is shared. Mirror of `LibraryCore.SERIES_COVER_STYLES`.
public let SERIES_COVER_STYLES: [SeriesCoverStyleSpec] = [
    .init(key: "collage", label: "Collage",      wRatio: 1.23, hRatio: 0.95),
    .init(key: "cover",   label: "Single cover", wRatio: 1.07, hRatio: 0.95),
    .init(key: "fan",     label: "Cover stack",  wRatio: 1.50, hRatio: 0.95),
]
public let SERIES_COVER_DEFAULT = "fan"
public func seriesCoverStyleSpec(_ key: String) -> SeriesCoverStyleSpec? { SERIES_COVER_STYLES.first { $0.key == key } }

// ── Search-screen component contract ───────────────────────────────────────────
/// One Search INPUT field — a typeahead box with a suggestion picker. `suggest` is the suggest
/// endpoint; `picks` is what choosing a suggestion resolves to (edition/work/person/subject).
public struct SearchField: Equatable, Sendable, Identifiable {
    public let key: String
    public let label: String
    public let suggest: String
    public let picks: String
    public var id: String { key }
}
/// The four search finders — book / work / person / subject. Mirror of `LibraryCore.SEARCH_FIELDS`.
public let SEARCH_FIELDS: [SearchField] = [
    .init(key: "book_title", label: "Book title", suggest: "/editions/search",         picks: "edition"),
    .init(key: "work_title", label: "Work title", suggest: "/works/search",            picks: "work"),
    .init(key: "person",     label: "Person",     suggest: "/library/suggest/person",  picks: "person"),
    .init(key: "subject",    label: "Subject",     suggest: "/library/suggest/subject", picks: "subject"),
]

/// One section of the BookDetailsPane. Each surface renders the section keyed by `key` from `detailVM`.
public struct BookDetailSection: Equatable, Sendable, Identifiable {
    public let key: String
    public let label: String
    public var id: String { key }
}
/// The detail-pane sections, in order. Mirror of `LibraryCore.BOOK_DETAIL_SECTIONS`.
public let BOOK_DETAIL_SECTIONS: [BookDetailSection] = [
    .init(key: "basics",      label: "Edition Basics"),
    .init(key: "holdings",    label: "Holdings"),
    .init(key: "works",       label: "Works In This Edition"),
    .init(key: "connections", label: "Connections"),
]
