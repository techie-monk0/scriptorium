import SwiftUI
import CatalogueCore
import CatalogueDesign

/// The canonical **BookCover** component — a 2:3 poster (height = `BOOK_COVER_ASPECT` × width, the
/// shared cover contract) resolved from a server-relative art handle, with a themed placeholder while
/// it loads (and a fallback if it fails — the server always answers, but offline it may not). This is
/// the SwiftUI implementation of the cross-surface BookCover; web/PWA implement the same contract in DOM.
public struct BookCover: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    let path: String?
    var width: CGFloat = Spacing.coverWidth

    public init(path: String?, width: CGFloat = Spacing.coverWidth) { self.path = path; self.width = width }

    public var body: some View {
        let height = width * CGFloat(BOOK_COVER_ASPECT)
        CachedImage(url: app.url(path)) { phase in
            switch phase {
            case .success(let image):
                image.resizable().scaledToFill()
            case .empty:
                tokens.surface2.overlay(ProgressView())
            case .failure:
                tokens.surface2.overlay(Image(systemName: "book.closed").foregroundStyle(tokens.muted))
            @unknown default:
                tokens.surface2
            }
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(tokens.cardBorder))
    }
}

/// Back-compat alias — `BookCover` is the canonical name; existing call sites used `CoverImage`.
public typealias CoverImage = BookCover

/// The **StarButton** — the cross-surface star TOGGLE (web/PWA paint the same affordance in DOM). It
/// reads the live highlight from `AppModel.isStarred` (so a star set anywhere reflects on every cover
/// at once) and fires the optimistic `toggleStar` (which executes the shared `starredRequest` mapper).
/// `onCover` pins it as a legible overlay chip; otherwise it's a plain inline control (panes/toolbar).
public struct StarButton: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    let eid: Int
    var onCover: Bool = false
    public init(eid: Int, onCover: Bool = false) { self.eid = eid; self.onCover = onCover }

    public var body: some View {
        let on = app.isStarred(eid)
        Button {
            Task { await app.toggleStar(eid) }
        } label: {
            Image(systemName: on ? "star.fill" : "star")
                .font(onCover ? .footnote.weight(.semibold) : .body)
                .foregroundStyle(on ? tokens.accent : (onCover ? .white : tokens.muted))
                .padding(onCover ? 5 : 0)
                .background(onCover ? AnyView(Circle().fill(.black.opacity(0.45))) : AnyView(Color.clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(on ? "Starred" : "Star")
        .accessibilityAddTraits(on ? [.isSelected, .isButton] : .isButton)
    }
}

/// A small corner pill — used for the Recent rail's "New" badge (a freshly-added book).
public struct NewBadge: View {
    @Environment(\.tokens) private var tokens
    let text: String
    public init(_ text: String) { self.text = text }
    public var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.bold)).foregroundStyle(.white)
            .padding(.horizontal, 5).padding(.vertical, 2)
            .background(Capsule().fill(tokens.accent))
    }
}

/// The **SeriesCover** component — a box-set tile drawn per the active `SeriesCoverStyle`, sized
/// RELATIVE to a `BookCover` via the style's shared box ratios so the two always read as a family.
/// This is the SwiftUI implementation of the cross-surface SeriesCover + style enum (web/PWA draw the
/// same `collage`/`cover`/`fan` styles in DOM). `style` is a `SERIES_COVER_STYLES` key.
public struct SeriesCover: View {
    @Environment(\.tokens) private var tokens
    let covers: [String?]                 // volume cover handles, in volume order
    var style: String
    var bookWidth: CGFloat = Spacing.coverWidth

    public init(covers: [String?], style: String, bookWidth: CGFloat = Spacing.coverWidth) {
        self.covers = covers; self.style = style; self.bookWidth = bookWidth
    }

    public var body: some View {
        let spec = seriesCoverStyleSpec(style) ?? seriesCoverStyleSpec(SERIES_COVER_DEFAULT)!
        let w = bookWidth * CGFloat(spec.wRatio)
        let h = bookWidth * CGFloat(BOOK_COVER_ASPECT) * CGFloat(spec.hRatio)
        Group {
            switch spec.key {
            case "collage": collage(w, h)
            case "fan":     fan(w, h)
            default:        single(w, h)        // "cover"
            }
        }
        // Root the whole set on the shelf baseline (covers stand on the rail, growing up/behind).
        .frame(width: w, height: h, alignment: .bottom)
    }

    // One representative cover with two card-edges stacked behind it — all standing on the shelf.
    private func single(_ w: CGFloat, _ h: CGFloat) -> some View {
        ZStack(alignment: .bottom) {
            RoundedRectangle(cornerRadius: 6).fill(tokens.surface2).frame(width: w - 14, height: h - 10).offset(x: 10)
            RoundedRectangle(cornerRadius: 6).fill(tokens.surface2).frame(width: w - 8, height: h - 5).offset(x: 5)
            art(covers.first ?? nil, width: w - 18)
        }
    }

    // 2×2 mosaic of the first volumes' covers.
    private func collage(_ w: CGFloat, _ h: CGFloat) -> some View {
        let four = Array(covers.prefix(4))
        let cw = (w - 3) / 2, ch = (h - 3) / 2
        return VStack(spacing: 3) {
            HStack(spacing: 3) { cell(four[safe: 0], cw, ch); cell(four[safe: 1], cw, ch) }
            HStack(spacing: 3) { cell(four[safe: 2], cw, ch); cell(four[safe: 3], cw, ch) }
        }
    }

    // Cover-flow STACK: a sharp foreground cover with neighbours fanned out behind it, every cover
    // ROOTED on the shelf baseline (bottom-aligned) so the set stands on the rail like books, the
    // back covers peeking up/out behind the front one.
    private func fan(_ w: CGFloat, _ h: CGFloat) -> some View {
        let mid = covers.count / 2
        return ZStack(alignment: .bottom) {
            if let l = covers[safe: mid - 2] { art(l, width: w * 0.46).offset(x: -w * 0.34).opacity(0.55).blur(radius: 1.1) }
            if let r = covers[safe: mid + 2] { art(r, width: w * 0.46).offset(x: w * 0.34).opacity(0.55).blur(radius: 1.1) }
            if let l = covers[safe: mid - 1] { art(l, width: w * 0.54).offset(x: -w * 0.24).opacity(0.8).blur(radius: 0.5) }
            if let r = covers[safe: mid + 1] { art(r, width: w * 0.54).offset(x: w * 0.24).opacity(0.8).blur(radius: 0.5) }
            art(covers[safe: mid] ?? covers.first ?? nil, width: w * 0.6)
        }
    }

    private func cell(_ path: String??, _ w: CGFloat, _ h: CGFloat) -> some View {
        let p = path ?? nil
        return AsyncImageCell(path: p).frame(width: w, height: h).clipShape(RoundedRectangle(cornerRadius: 3))
    }
    private func art(_ path: String?, width: CGFloat) -> some View {
        BookCover(path: path, width: width)
    }
}

/// A plain cover image filling its frame (no fixed aspect) — a collage cell.
private struct AsyncImageCell: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    let path: String?
    var body: some View {
        CachedImage(url: app.url(path)) { phase in
            if case .success(let img) = phase { img.resizable().scaledToFill() } else { tokens.surface2 }
        }
    }
}

private extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}

/// A poster tile: cover + title + by-line — the shared Shelf tile (cover mode). On a shelf rail
/// (`magnifies: true`) the whole tile blooms anchored at ITS bottom — which sits on the rail's bottom
/// baseline (the row is bottom-aligned) — so it grows UP and SIDEWAYS off the shelf, never below it,
/// into the rail's reserved top headroom (the SwiftUI analogue of the web track's flex-end + top padding).
public struct CardTile: View {
    @Environment(\.tokens) private var tokens
    let card: Card
    var width: CGFloat = Spacing.coverWidth
    var magnifies: Bool = false
    var showTitle: Bool = false          // Shelf config: caption below the cover (off by default)

    public var body: some View {
        VStack(alignment: .leading, spacing: Spacing.xs) {
            CoverImage(path: card.coverUrl, width: width)
                // Star TOGGLE (bottom-trailing) + the Recent rail's "New" badge (top-leading), overlaid
                // on the cover so the affordance + highlight ride along everywhere a cover is shown.
                .overlay(alignment: .bottomTrailing) { StarButton(eid: card.eid, onCover: true).padding(4) }
                .overlay(alignment: .topLeading) {
                    if (card.badge ?? "") == "New" { NewBadge("New").padding(4) }
                }
            if showTitle {
                Text(card.displayTitle ?? card.title)
                    .font(Typography.caption).foregroundStyle(tokens.fg)
                    .lineLimit(2)
                if let by = card.by, !by.isEmpty {
                    Text(by).font(Typography.caption).foregroundStyle(tokens.muted).lineLimit(1)
                }
            }
        }
        .frame(width: width)
        .shelfMagnify(magnifies)
    }
}

/// A horizontally scrolling shelf of cards (an Apple-Music/Netflix rail).
public struct ShelfRow: View {
    @Environment(\.tokens) private var tokens
    let title: String
    let cards: [Card]
    var onTap: (Card) -> Void = { _ in }

    public var body: some View {
        VStack(alignment: .leading, spacing: Spacing.sm) {
            Text(title).font(Typography.sectionHeader).foregroundStyle(tokens.fg)
                .padding(.horizontal, Spacing.lg)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: Spacing.md) {
                    ForEach(cards) { card in
                        Button { onTap(card) } label: { CardTile(card: card) }
                            .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, Spacing.lg)
            }
        }
    }
}

public extension View {
    /// macOS-Dock-style shelf magnification: EACH tile scales by its own distance from the rail's
    /// centre (a cosine falloff over half the viewport), so every cover blooms smoothly and in order as
    /// it scrolls toward the middle and eases back at the edges — anchored at its bottom so it grows
    /// UPWARD into the rail's headroom. This is the SwiftUI analogue of shelf.js `magnify` (cursor/centre
    /// distance, origin center-bottom). Driven by `.visualEffect` so it recomputes continuously while
    /// scrolling — unlike `.scrollTransition(.interactive)`, which snaps all visible tiles to one value
    /// (the "some covers expand, some don't" bug). Amount = `Spacing.shelfMagnify`. No-op when inactive.
    @ViewBuilder func shelfMagnify(_ active: Bool = true) -> some View {
        if active {
            visualEffect { content, proxy in
                content.scaleEffect(shelfMagnifyScale(proxy), anchor: .bottom)
            }
        } else {
            self
        }
    }
}

/// The Dock-falloff scale for a tile at `proxy`, from its centre's distance to the scroll viewport's
/// centre: 1 + amp·½(1+cos(π·d/R)) within R (half the viewport width), 1 beyond. Returns 1 if the
/// enclosing scroll view can't be resolved (e.g. not yet laid out).
@inline(__always) func shelfMagnifyScale(_ proxy: GeometryProxy) -> CGFloat {
    guard let viewport = proxy.bounds(of: .scrollView(axis: .horizontal)) else { return 1 }
    let tileMidX = proxy.frame(in: .scrollView(axis: .horizontal)).midX
    let d = abs(tileMidX - viewport.midX)
    let r = viewport.width / 2
    guard r > 0, d < r else { return 1 }
    let falloff = 0.5 * (1 + cos(Double.pi * Double(d) / Double(r)))
    return 1 + CGFloat(Double(Spacing.shelfMagnify) * falloff)
}

/// The upward headroom a magnifying shelf rail reserves above its covers so a bloom grows into space
/// instead of being clipped by the scroll bounds — the SwiftUI analogue of the web track's top padding.
public extension CGFloat { static var shelfBloomHeadroom: CGFloat { Spacing.coverWidth * 1.5 * Spacing.shelfMagnify + 8 } }

/// A non-fatal status strip — the renderer's view of a view-model's `offline`/`error`/`empty` fields.
public struct StatusBanner: View {
    @Environment(\.tokens) private var tokens
    let text: String
    var isWarning: Bool = false
    public var body: some View {
        Text(text)
            .font(Typography.caption)
            .foregroundStyle(isWarning ? tokens.warn : tokens.muted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Spacing.md)
    }
}

/// The shared **freshness chip** — the Tier-3 SwiftUI render of `SyncStatusVM` (web/PWA paint the same
/// spec in DOM). A tinted dot from the spec's `tone`, plus the label whenever there's something to say
/// (hidden when simply "Live", to keep the bar quiet). One spec decided in `syncVM`, three surfaces.
public struct SyncStatusPill: View {
    @Environment(AppModel.self) private var app
    @Environment(\.tokens) private var tokens
    public init() {}
    public var body: some View {
        let s = app.syncStatus
        HStack(spacing: Spacing.xs) {
            Circle().fill(tone(s.tone)).frame(width: 7, height: 7)
                .opacity(s.state == "syncing" ? 0.5 : 1)
            if s.state != "live" { Text(s.label).font(Typography.caption).foregroundStyle(tokens.muted) }
        }
        .accessibilityLabel(s.detail.map { "\(s.label), \($0)" } ?? s.label)
    }
    private func tone(_ t: String) -> Color {
        switch t {
        case "ok": return tokens.ok
        case "warn", "error": return tokens.warn      // no distinct error token; warn carries attention
        default: return tokens.muted                  // "muted" (syncing)
        }
    }
}

/// Pull-to-refresh for a catalogue screen: a manual pull triggers a `.manual` refresh through the shared
/// engine. One modifier so every list/scroll screen gets the identical portable gesture (the surface
/// render of the shared pull-to-refresh component). Screens key their recompute on `app.dataRevision`
/// so the new data repaints when it lands.
public struct CatalogueRefreshable: ViewModifier {
    @Environment(AppModel.self) private var app
    public func body(content: Content) -> some View {
        content.refreshable { await app.refresh(.manual) }
    }
}
public extension View {
    func catalogueRefreshable() -> some View { modifier(CatalogueRefreshable()) }
}
