#if canImport(UIKit)
import SwiftUI
import Combine
import UIKit
import PDFKit
import WebKit
import Postilla
import OctavoEPUB

// The reader's note markers, built on the portable `MarkerOverlay` component. Every note — PDF or EPUB —
// renders with the SAME marker view and the SAME interaction model (`MarkerOverlayModel`: drag to move, tap
// to open an anchored popover, long-press to delete). The only per-format part is a `NoteAnchor` adapter
// that maps the note's document location (a PDF page point; an EPUB block CFI) to/from a viewport anchor
// and back into an annotation to persist. `PdfNoteAnchor` and `EpubNoteAnchor` are the two adapters — the
// same relationship `PdfDecorationHost`/`EpubDecorationHost` have to octavo's `DecorationHost`.
//
// NOTE(device): like `EpubInkHost`, the page↔viewport / CFI↔rect alignment needs on-device tuning.

// MARK: - Shared visuals

/// The note marker glyph — a small round badge, the iOS twin of the web reader's 🅝 pin.
struct NoteBadge: View {
    var color: Color
    var body: some View {
        Image(systemName: "note.text")
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(.black.opacity(0.75))
            .frame(width: 26, height: 26)
            .background(color, in: Circle())
            .overlay(Circle().strokeBorder(.black.opacity(0.18)))
            .shadow(radius: 1.5, y: 1)
            .accessibilityLabel(Text("Note"))
    }
}

/// The anchored callout shown when a note marker is tapped: the note text on a sticky-note **yellow** card,
/// plus an Edit affordance on an editable surface. Delete lives in the long-press context menu, not here.
/// Text/controls use fixed dark tints since the paper colour is constant in light and dark mode.
///
/// The yellow is the CONTENT's own background only — NOT `presentationBackground`, which would tint the
/// whole popover presentation surface and bleed past the note on a compact width.
struct NoteDetailPopover: View {
    let text: String
    let canEdit: Bool
    let onEdit: () -> Void
    /// Sticky-note yellow — the note card's paper.
    private let paper = Color(red: 1.0, green: 0.95, blue: 0.66)
    private let ink = Color.black.opacity(0.85)

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(text.isEmpty ? "Empty note" : text)
                .font(.callout)
                .foregroundStyle(text.isEmpty ? Color.black.opacity(0.4) : ink)
                .frame(maxWidth: 260, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            if canEdit {
                Button { onEdit() } label: { Label("Edit", systemImage: "pencil") }
                    .font(.footnote.weight(.medium))
                    .tint(Color(red: 0.5, green: 0.38, blue: 0.0))   // stays readable on yellow
            }
        }
        .padding(14)
        .frame(minWidth: 150)
        .background(paper, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

// MARK: - The adapter: NoteAnchor (+ its two format implementations)

/// A note's placement adapter, built on the portable `MarkerOverlayModel`. It owns the overlay's interaction
/// state — so drag-to-move, tap-to-open and long-press are identical for every format — and leaves ONLY
/// the format-specific bits to a subclass: how the note's document location maps to and from a viewport
/// anchor. This is the "derives from `MarkerOverlayModel`" seam: the adapter wraps the generic model rather
/// than reinventing the gestures. `PdfNoteAnchor` and `EpubNoteAnchor` implement the differences.
@MainActor
class NoteAnchor: ObservableObject {
    /// The shared interaction model (drag/expand/menu state + normalized anchor). The marker binds to this.
    @Published var model: MarkerOverlayModel
    /// The note being placed — kept in sync by the marker so an edit/move is reflected.
    var note: Annotation

    init(note: Annotation, canEdit: Bool) {
        self.note = note
        let start = PanelPoint(x: -1, y: -1)   // off-screen until the first `retrack` resolves it
        self.model = canEdit ? MarkerOverlayModel(anchor: start) : .readOnly(anchor: start)
    }

    // MARK: shared behavior

    /// Re-resolve the on-screen anchor after a scroll / page turn (a no-op mid-drag: `place` ignores it).
    func retrack(in area: CGSize) {
        if let a = viewportAnchor(in: area) { model.place(at: a) }
    }

    /// Small helper for subclasses clamping a normalized offset.
    func clamp01(_ v: Double) -> Double { min(max(v, 0), 1) }

    // MARK: format-specific — override in the PDF / EPUB adapters

    /// Does re-placing need an async data refresh each time? EPUB re-queries its block rect; PDF is a pure
    /// `convert`, so it places synchronously on every scroll frame (no per-frame `Task`).
    var needsPreparePerTrack: Bool { false }

    /// Prepare any async placement data for the current page (EPUB queries its block rect). Default no-op.
    func prepare() async {}

    /// The note's viewport anchor (0…1 of `area`), or nil when it's not on the visible page.
    func viewportAnchor(in area: CGSize) -> PanelPoint? { nil }

    /// Is the note on the page currently shown? (drives marker visibility)
    var isOnCurrentPage: Bool { true }

    /// Turn a drop (normalized viewport anchor) into the moved annotation to persist — its location fields
    /// updated, but NOT `updatedAt`/`rev` (the reader owns the revision). Nil cancels the move.
    func moved(toDrop anchor: PanelPoint, in area: CGSize) async -> Annotation? { nil }
}

/// PDF adapter — a note anchored to a page + a normalized top-left point on that page.
@MainActor
final class PdfNoteAnchor: NoteAnchor {
    private let pdfView: PDFView
    init(note: Annotation, pdfView: PDFView, canEdit: Bool) {
        self.pdfView = pdfView
        super.init(note: note, canEdit: canEdit)
    }
    override func viewportAnchor(in area: CGSize) -> PanelPoint? {
        PdfNoteGeometry.viewportAnchor(for: note, in: pdfView)
    }
    override var isOnCurrentPage: Bool { PdfNoteGeometry.isVisible(note, in: pdfView) }
    override func moved(toDrop anchor: PanelPoint, in area: CGSize) async -> Annotation? {
        guard let (page, region) = PdfNoteGeometry.pageRegion(forViewportAnchor: anchor, in: pdfView) else { return nil }
        var m = note
        m.locator = Locator(publicationId: note.publicationId, format: .pdf, locations: .init(page: page + 1))
        m.region = region
        return m
    }
}

/// EPUB adapter — a note anchored to a block CFI + a normalized offset within that block. Reflow-stable:
/// the block rect is re-queried each page turn (the same `rect(forCfi:)` the ink host uses).
@MainActor
final class EpubNoteAnchor: NoteAnchor {
    private let navigator: EpubWebNavigator
    /// The note's block rect on the current page (webView coords), or nil when it's not on this page.
    private var blockRect: CGRect?
    init(note: Annotation, navigator: EpubWebNavigator, canEdit: Bool) {
        self.navigator = navigator
        super.init(note: note, canEdit: canEdit)
    }
    override var needsPreparePerTrack: Bool { true }
    override func prepare() async {
        guard let cfi = note.cfiRange ?? note.locator.locations.cfi, !cfi.isEmpty else { blockRect = nil; return }
        blockRect = await navigator.rect(forCfi: cfi)
    }
    override var isOnCurrentPage: Bool { blockRect != nil }
    override func viewportAnchor(in area: CGSize) -> PanelPoint? {
        guard let rect = blockRect, area.width > 1, area.height > 1 else { return nil }
        let off = note.region ?? [0, 0]
        let pt = CGPoint(x: rect.minX + (off.first ?? 0) * rect.width,
                         y: rect.minY + (off.count > 1 ? off[1] : 0) * rect.height)
        return PanelPoint(x: Double(pt.x / area.width), y: Double(pt.y / area.height))
    }
    override func moved(toDrop anchor: PanelPoint, in area: CGSize) async -> Annotation? {
        let point = CGPoint(x: anchor.x * Double(area.width), y: anchor.y * Double(area.height))
        guard let hit = await navigator.cfiAtPoint(point), hit.rect.width > 1, hit.rect.height > 1 else { return nil }
        var m = note
        m.cfiRange = hit.cfi
        m.region = [clamp01((point.x - hit.rect.minX) / hit.rect.width),
                    clamp01((point.y - hit.rect.minY) / hit.rect.height)]
        blockRect = hit.rect
        return m
    }
}

// MARK: - The one marker view (format-agnostic)

/// The single note marker — a badge that drags to move, taps to open its note, long-presses to delete. It
/// is identical for every format; the `NoteAnchor` it's handed supplies only the placement. Holds the
/// anchor as `@StateObject` so its interaction state survives view updates, and re-tracks whenever the
/// `trackingToken` (bumped by the layer on scroll / page turn) or the area changes.
struct NoteMarker: View {
    let note: Annotation
    let area: CGSize
    let trackingToken: Int
    let canEdit: Bool
    let onPersist: (Annotation) -> Void
    let onDelete: (Annotation) -> Void
    let onEdit: (Annotation) -> Void
    @StateObject private var anchor: NoteAnchor

    init(note: Annotation, area: CGSize, trackingToken: Int, canEdit: Bool,
         makeAnchor: @escaping () -> NoteAnchor,
         onPersist: @escaping (Annotation) -> Void,
         onDelete: @escaping (Annotation) -> Void,
         onEdit: @escaping (Annotation) -> Void) {
        self.note = note; self.area = area; self.trackingToken = trackingToken; self.canEdit = canEdit
        self.onPersist = onPersist; self.onDelete = onDelete; self.onEdit = onEdit
        _anchor = StateObject(wrappedValue: makeAnchor())
    }

    var body: some View {
        MarkerOverlay(
            model: Binding(get: { anchor.model }, set: { anchor.model = $0 }),
            onMoved: { p in Task { if let m = await anchor.moved(toDrop: p, in: area) { onPersist(m) } } },
            menuActions: canEdit
                ? [MarkerOverlayAction(title: "Delete", systemImage: "trash", role: .destructive) { onDelete(note) }]
                : [],
            coordinateSpace: "note-\(note.id)",
            icon: { NoteBadge(color: Color(hex: note.color ?? "#ffd54a")) },
            detail: { NoteDetailPopover(text: note.noteText ?? "", canEdit: canEdit) { onEdit(note) } })
            .opacity(anchor.isOnCurrentPage ? 1 : 0)
            .allowsHitTesting(anchor.isOnCurrentPage)
            .onAppear { track() }
            .onChange(of: trackingToken) { track() }                 // scroll (PDF) / page turn (EPUB)
            .onChange(of: area) { track() }                          // rotation / resize
            // Reflect an edit/move of the same note (identity is stable, so the StateObject persists).
            .onChange(of: note) { anchor.note = note; track() }
    }

    /// Re-place the marker at the note's current on-screen spot. PDF resolves synchronously — a fast
    /// `convert` on every scroll frame — while EPUB must re-query its block rect first, so it prepares
    /// async then places.
    private func track() {
        if anchor.needsPreparePerTrack {
            Task { await anchor.prepare(); anchor.retrack(in: area) }
        } else {
            anchor.retrack(in: area)
        }
    }
}

// MARK: - The two thin layers

/// The PDF note layer — one `NoteMarker` per note over the `PDFView`. Attach as an `.overlay` of the PDF
/// container so its coordinate space matches the view. Turns PDFKit's scroll/zoom notifications into a
/// tracking token the markers re-place on.
struct PdfNoteLayer: View {
    let pdfView: PDFView
    let notes: [Annotation]
    let canEdit: Bool
    let onPersist: (Annotation) -> Void
    let onDelete: (Annotation) -> Void
    let onEdit: (Annotation) -> Void

    @StateObject private var scroll = PdfScrollObserver()

    var body: some View {
        GeometryReader { geo in
            ZStack {
                ForEach(notes) { note in
                    NoteMarker(note: note, area: geo.size, trackingToken: scroll.tick, canEdit: canEdit,
                               makeAnchor: { PdfNoteAnchor(note: note, pdfView: pdfView, canEdit: canEdit) },
                               onPersist: onPersist, onDelete: onDelete, onEdit: onEdit)
                }
            }
        }
        .onAppear { scroll.attach(to: pdfView) }
        .onDisappear { scroll.detach() }
        // A coarse backstop (page flip / zoom) in case the scroll-view KVO can't attach.
        .onReceive(NotificationCenter.default.publisher(for: .PDFViewPageChanged, object: pdfView)) { _ in scroll.bump() }
        .onReceive(NotificationCenter.default.publisher(for: .PDFViewScaleChanged, object: pdfView)) { _ in scroll.bump() }
    }
}

/// Watches a `PDFView`'s internal scroll view so note markers follow the page **continuously** as it
/// scrolls or zooms. `PDFViewPageChanged` only fires when the current page flips — far too coarse to track
/// a drag-scroll — so we KVO the scroll view's `contentOffset`/`zoomScale` and publish a tick on each
/// change. It fires only while actually scrolling, so idle cost is zero.
@MainActor
final class PdfScrollObserver: ObservableObject {
    @Published private(set) var tick = 0
    private var observations: [NSKeyValueObservation] = []
    private var attached = false

    func attach(to pdfView: PDFView) {
        guard !attached, let sv = Self.scrollView(in: pdfView) else { return }
        attached = true
        observations = [
            sv.observe(\.contentOffset, options: [.new]) { [weak self] _, _ in
                MainActor.assumeIsolated { self?.tick &+= 1 }        // contentOffset changes on the main thread
            },
            sv.observe(\.zoomScale, options: [.new]) { [weak self] _, _ in
                MainActor.assumeIsolated { self?.tick &+= 1 }
            },
        ]
    }

    /// Coarse nudge from a notification (page flip / zoom) — a backstop when KVO couldn't attach.
    func bump() { tick &+= 1 }

    func detach() {
        observations.forEach { $0.invalidate() }
        observations = []
        attached = false
    }

    /// The first descendant `UIScrollView` — PDFView hosts its document view inside a private one.
    private static func scrollView(in view: UIView) -> UIScrollView? {
        if let sv = view as? UIScrollView { return sv }
        for sub in view.subviews { if let sv = scrollView(in: sub) { return sv } }
        return nil
    }
}

/// The EPUB note layer — one `NoteMarker` per note over the WKWebView. The `relocateToken` (bumped by the
/// host on every page turn) drives the block-rect re-query inside each marker.
struct EpubNoteLayer: View {
    let navigator: EpubWebNavigator
    let notes: [Annotation]
    let relocateToken: Int
    let canEdit: Bool
    let onPersist: (Annotation) -> Void
    let onDelete: (Annotation) -> Void
    let onEdit: (Annotation) -> Void

    var body: some View {
        GeometryReader { geo in
            ZStack {
                ForEach(notes) { note in
                    NoteMarker(note: note, area: geo.size, trackingToken: relocateToken, canEdit: canEdit,
                               makeAnchor: { EpubNoteAnchor(note: note, navigator: navigator, canEdit: canEdit) },
                               onPersist: onPersist, onDelete: onDelete, onEdit: onEdit)
                }
            }
        }
    }
}

// MARK: - PDF coordinate math

/// Pure page ⇄ viewport math for a PDF note (0-based page + normalized top-left point ⇄ normalized point
/// in `pdfView.bounds`). Kept apart from the adapter so the coordinate conventions live in one place.
@MainActor
enum PdfNoteGeometry {
    private static func pageIndex(_ note: Annotation) -> Int? {
        note.locator.locations.position ?? note.locator.locations.page.map { max(0, $0 - 1) }
    }

    /// A note's normalized anchor within the PDF view, or nil if its page isn't in the document.
    static func viewportAnchor(for note: Annotation, in pdfView: PDFView) -> PanelPoint? {
        guard let region = note.region, region.count >= 2, let doc = pdfView.document,
              let i = pageIndex(note), i >= 0, i < doc.pageCount, let page = doc.page(at: i) else { return nil }
        let b = page.bounds(for: .cropBox)
        let pagePt = CGPoint(x: b.minX + region[0] * b.width,
                             y: b.minY + b.height - region[1] * b.height)   // top-left normalized → page space
        let v = pdfView.convert(pagePt, from: page)
        let size = pdfView.bounds.size
        guard size.width > 1, size.height > 1 else { return nil }
        return PanelPoint(x: Double(v.x / size.width), y: Double(v.y / size.height))
    }

    /// Inverse map for a drop: a normalized view anchor → (0-based page index, region top-left). Uses the
    /// nearest page so a drag that strays past the page edge still lands somewhere sensible.
    static func pageRegion(forViewportAnchor a: PanelPoint, in pdfView: PDFView) -> (page: Int, region: [Double])? {
        let size = pdfView.bounds.size
        guard size.width > 1, size.height > 1, let doc = pdfView.document else { return nil }
        let v = CGPoint(x: a.x * Double(size.width), y: a.y * Double(size.height))
        guard let page = pdfView.page(for: v, nearest: true) else { return nil }
        let b = page.bounds(for: .cropBox)
        let p = pdfView.convert(v, to: page)
        let x = (p.x - b.minX) / b.width
        let y = (b.minY + b.height - p.y) / b.height
        return (doc.index(for: page), [min(max(Double(x), 0), 1), min(max(Double(y), 0), 1)])
    }

    /// Is the note's page currently on screen? (don't draw markers for far-off pages)
    static func isVisible(_ note: Annotation, in pdfView: PDFView) -> Bool {
        guard let doc = pdfView.document, let i = pageIndex(note),
              i >= 0, i < doc.pageCount, let page = doc.page(at: i) else { return false }
        return pdfView.visiblePages.contains(page)
    }
}
#endif
