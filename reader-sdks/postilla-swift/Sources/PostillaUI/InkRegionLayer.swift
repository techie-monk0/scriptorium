import Foundation
import CoreGraphics
import Postilla

/// SEAM: maps a region's content anchor to an on-screen **writing-surface rect**
/// at the *current* layout — the one piece that differs per format and placement
/// mode. Everything above it (the stroke model, the renderer, the host wiring) is
/// shared.
///
///  - **fixed page** resolves trivially: the page's rect in the host view.
///  - **inline box** (#3) first *reserves* a box in the flow so the text wraps,
///    then reports the laid-out rect — for EPUB this is a layout query over the
///    JS bridge (inject a sized placeholder at the CFI, read its client rect).
///  - **detached** (#2) returns the panel's rect (or nil when the panel is shut);
///    only a marker lives in the flow, so reflow never moves the surface.
///
/// A resolver returns `nil` when the region's anchor is not currently laid out
/// (off-screen, a different chapter) — the layer simply skips it that frame.
/// PORT: renders a set of ink `InkRegion`s over a host and clears them — the seam each surface
/// adapts. **PDF** (`PdfInkHost`) hosts fixed-page regions as native `PDFAnnotation`s (PDFKit gives
/// transform/zoom for free). **EPUB** (N4) hosts inline/detached regions via `InkRegionLayer` +
/// `InkRegionResolver` over the WKWebView. So `InkRegionResolver`/`InkRegionLayer` below are the
/// *overlay* host's internals, not the top-level seam — this is.
@MainActor
public protocol InkHost: AnyObject {
    /// (Re-)render exactly `regions` (idempotent: clears the prior set first).
    func render(_ regions: [InkRegion])
    func clear()
}

/// SEAM: maps a region's content anchor to an on-screen **writing-surface rect** at the current
/// layout — the overlay host's per-format/mode piece (EPUB inline/detached). Fixed-page PDF does not
/// use this (it renders through `PDFAnnotation`).
@MainActor
public protocol InkRegionResolver {
    /// The region's surface rect in the host view's coordinate space (top-left
    /// origin, y-down), or `nil` if the anchor isn't on screen right now.
    func surfaceRect(for region: InkRegion) -> CGRect?

    /// Inline regions must reserve a box in the flow so text wraps around them;
    /// fixed/detached are no-ops. Idempotent — safe to call every layout pass.
    func reserveSpace(for region: InkRegion)
}

public extension InkRegionResolver {
    func reserveSpace(for region: InkRegion) {}
}

/// The **general ink layout layer**: draws a set of `InkRegion`s over a host by
/// asking the resolver where each surface currently is and filling its strokes
/// with the shared `InkLayerRenderer`. Format- and mode-agnostic — PDF (fixed),
/// EPUB (inline / detached), and frozen snapshots all flow through this one path,
/// each supplying its own `InkRegionResolver`. The renderer never learns the
/// format; the resolver never learns how strokes are shaped.
@MainActor
public final class InkRegionLayer {
    private let resolver: InkRegionResolver

    public init(resolver: InkRegionResolver) {
        self.resolver = resolver
    }

    /// Render every region whose anchor is currently on screen into `ctx`
    /// (already in the host view's coordinate space, y-down). Off-screen regions
    /// are skipped this pass; call again on scroll/reflow.
    public func draw(
        _ regions: [InkRegion],
        into ctx: CGContext,
        options: FreehandRenderer.Options = .init()
    ) {
        for region in regions {
            resolver.reserveSpace(for: region)
            guard let rect = resolver.surfaceRect(for: region) else { continue }
            ctx.saveGState()
            // Place the region's box, then draw its strokes normalized to that box.
            ctx.translateBy(x: rect.minX, y: rect.minY)
            InkLayerRenderer.draw(region.strokes, in: rect.size, into: ctx, options: options)
            ctx.restoreGState()
        }
    }
}
