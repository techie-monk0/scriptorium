#if canImport(UIKit)
import UIKit
import WebKit
import Postilla
import PostillaRender
import OctavoEPUB

/// The EPUB adapter of `InkHost` — draws ink over the epub.js WKWebView. EPUB is **paginated**, so each
/// ink region anchors to a block CFI; on render (and on every page turn) the block's on-screen rect is
/// queried from the navigator and the strokes are drawn there through the shared `InkRegionLayer` + the
/// injected `InkRenderer` engine. A region whose CFI isn't on the current page is skipped that pass.
/// Best-effort: reflow re-places ink on the next relocation.
///
/// NOTE(device): the CFI→rect mapping and the overlay↔iframe alignment need on-device tuning.
@MainActor
public final class EpubInkHost: InkHost {
    private let navigator: EpubWebNavigator
    private let overlay: EpubInkOverlayView
    private var regions: [InkRegion] = []

    /// `renderer` is the swappable ink engine (default: `FreehandInkRenderer`).
    public init(navigator: EpubWebNavigator, renderer: any InkRenderer = FreehandInkRenderer()) {
        self.navigator = navigator
        self.overlay = EpubInkOverlayView(renderer: renderer)
        overlay.isUserInteractionEnabled = false      // ink display only; capture is a separate canvas
        overlay.backgroundColor = .clear
        overlay.frame = navigator.webView.bounds
        overlay.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        navigator.webView.addSubview(overlay)
    }

    public func render(_ regions: [InkRegion]) {
        // Fixed-page ink belongs to the PDF host; EPUB hosts inline/detached (CFI-anchored) regions.
        self.regions = regions.filter { $0.placement != .fixedPage }
        Task { await refresh() }
    }

    public func clear() {
        regions = []
        overlay.update(regions: [], rects: [:])
    }

    /// Re-query each region's block rect for the current page and redraw. Call on pull + on relocation.
    public func refresh() async {
        var rects: [String: CGRect] = [:]
        for r in regions {
            guard let cfi = r.cfiRange ?? r.anchor.locations.cfi, !cfi.isEmpty else { continue }
            if let rect = await navigator.rect(forCfi: cfi) { rects[r.id] = rect }
        }
        overlay.update(regions: regions, rects: rects)
    }
}

/// A transparent overlay that fills ink regions via the shared `InkRegionLayer`, reading each region's
/// current on-screen rect from a cache the host refreshes per page.
final class EpubInkOverlayView: UIView {
    private let renderer: any InkRenderer
    private var regions: [InkRegion] = []
    private var rects: [String: CGRect] = [:]

    init(renderer: any InkRenderer) {
        self.renderer = renderer
        super.init(frame: .zero)
    }
    @available(*, unavailable) required init?(coder: NSCoder) { fatalError("init(coder:) unsupported") }

    func update(regions: [InkRegion], rects: [String: CGRect]) {
        self.regions = regions
        self.rects = rects
        setNeedsDisplay()
    }

    override func draw(_ rect: CGRect) {
        guard let ctx = UIGraphicsGetCurrentContext() else { return }
        InkRegionLayer(resolver: CachedInkResolver(rects: rects), renderer: renderer).draw(regions, into: ctx)
    }
}

/// An `InkRegionResolver` backed by a per-page rect cache (the host queries the JS bridge async, then
/// the overlay draws synchronously from the cache).
@MainActor
final class CachedInkResolver: InkRegionResolver {
    private let rects: [String: CGRect]
    init(rects: [String: CGRect]) { self.rects = rects }
    func surfaceRect(for region: InkRegion) -> CGRect? { rects[region.id] }
}
#endif
