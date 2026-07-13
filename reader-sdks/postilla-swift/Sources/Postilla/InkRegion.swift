import Foundation
import Octavo

/// How an ink writing-surface relates to the laid-out content. The three modes
/// generalize the strategies real readers actually use, so one data model serves
/// all of them:
///
///  - `fixedPage` (#4) — the surface **is** the page; strokes are page
///    coordinates. PDF, fixed-layout EPUB, or a frozen snapshot. Always stable,
///    never reflows. The fallback that "just works".
///  - `inlineBox` (#3) — a rectangle **reserved in the text flow**; the renderer
///    makes room and the text wraps around it (the "Active Canvas" model). The
///    box rides with its paragraph through reflow.
///  - `detached` (#2) — an **off-page canvas**; only a small marker sits in the
///    flow at the anchor, and the surface opens in a panel (the "sticky note"
///    model). Immune to reflow because it was never positioned against the page.
///
/// In every mode the strokes are normalized `0…1` to the **region's own surface
/// box**, never to the page — which is exactly what lets a single stroke payload
/// render in all three: only the box that box resolves to differs.
public enum InkPlacement: Equatable, Sendable {
    case fixedPage
    /// `aspect` = box height ÷ width; the reserved box spans the column width.
    case inlineBox(aspect: Double)
    /// `aspect` = intrinsic height ÷ width of the popup canvas.
    case detached(aspect: Double)
}

/// A handwriting region — the format-neutral unit the layout layer resolves and
/// `InkLayerRenderer` draws. It carries:
///   - **where** it belongs: a content-relative `Locator` anchor (+ an optional
///     `cfiRange` when the note is *about a passage* rather than a point);
///   - **how** its surface is placed (`InkPlacement`);
///   - the **strokes**, normalized `0…1` to the surface box.
///
/// It projects onto the existing `annotation` row for storage: `anchor` →
/// `page`/`cfi`, `cfiRange` → `cfi_range`, `placement` → a small mode tag, the
/// box → `rect`, `strokes` → `ink`. So this is a typed view over what the DB
/// already holds, not a new store.
public struct InkRegion: Equatable, Sendable, Identifiable {
    public var id: String
    public var anchor: Locator
    public var cfiRange: String?
    public var placement: InkPlacement
    public var strokes: [InkStroke]

    public init(
        id: String,
        anchor: Locator,
        cfiRange: String? = nil,
        placement: InkPlacement,
        strokes: [InkStroke]
    ) {
        self.id = id
        self.anchor = anchor
        self.cfiRange = cfiRange
        self.placement = placement
        self.strokes = strokes
    }
}

public extension Annotation {
    /// Project an **ink** annotation onto an `InkRegion` for the placement/host layer — the typed
    /// view over the stored row. Returns nil for a non-ink annotation or one with no strokes.
    /// `placement` defaults to `.fixedPage` (PDF / frozen); an EPUB host supplies
    /// `.inlineBox`/`.detached` (persisted placement lands with EPUB ink in N4).
    func inkRegion(placement: InkPlacement = .fixedPage) -> InkRegion? {
        guard kind == .ink, let ink, !ink.strokes.isEmpty else { return nil }
        return InkRegion(id: id.uuidString, anchor: locator, cfiRange: cfiRange,
                         placement: placement, strokes: ink.strokes)
    }
}
