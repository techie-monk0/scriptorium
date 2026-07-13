import Foundation
import CoreGraphics
import Postilla

/// The **format-agnostic** ink draw path — the one place every host (PDF, EPUB,
/// the flattened export) turns stored `InkStroke`s into pixels. Given a set of
/// strokes, a canvas size, and a `CGContext`, it fills each stroke's
/// `FreehandRenderer` outline honoring its `InkMode`.
///
/// CoreGraphics-only (no UIKit/PDFKit), so it builds and is tested on macOS and
/// is reusable by every per-platform host. The reuse boundary of the
/// PDF/EPUB ink story lives here: a host only has to (a) pick the canvas size and
/// (b) put the context into this renderer's coordinate space.
///
/// **Coordinate convention:** a **top-left origin, y-down** canvas — what
/// CALayer/UIView/WKWebView use, and what `FreehandRenderer` already assumes
/// (normalized `y = 0` is the top of the page). A host whose context is y-up
/// (e.g. a PDF page) flips the CTM *before* calling in; the renderer itself is
/// format- and orientation-agnostic.
public enum InkLayerRenderer {

    /// Fill every stroke into `ctx` for a `canvas`-sized page. Strokes carry
    /// their own `width`/`color`/`mode`; `options` supplies the shaping
    /// (thinning/streamline) the per-stroke `width` overrides `size` on.
    public static func draw(
        _ strokes: [InkStroke],
        in canvas: CGSize,
        into ctx: CGContext,
        options: FreehandRenderer.Options = .init()
    ) {
        for stroke in strokes {
            let path = FreehandRenderer.path(
                for: stroke,
                canvas: canvas,
                options: shaped(options, for: stroke)
            )
            if path.isEmpty { continue }

            ctx.saveGState()
            switch stroke.mode {
            case .draw:
                ctx.setBlendMode(.normal)
                ctx.setFillColor(cgColor(stroke.color, alpha: 1))
            case .highlight:
                // Multiply so overlapping text stays legible under the tint.
                ctx.setBlendMode(.multiply)
                ctx.setFillColor(cgColor(stroke.color, alpha: 0.4))
            case .erase:
                // The raw points still persist (canonical-ink rule); erase only
                // clears pixels at render time.
                ctx.setBlendMode(.clear)
                ctx.setFillColor(cgColor("#000000", alpha: 1))
            }
            ctx.addPath(path)
            ctx.fillPath()
            ctx.restoreGState()
        }
    }

    // MARK: - Internals

    /// The stroke's own `width` is the perfect-freehand base diameter (`size`);
    /// `thinning`/`streamline` come from the shared options.
    private static func shaped(
        _ options: FreehandRenderer.Options,
        for stroke: InkStroke
    ) -> FreehandRenderer.Options {
        var o = options
        if stroke.width > 0 { o.size = stroke.width }
        return o
    }

    /// Parse a `#rrggbb` (or `rrggbb`) hex into a CGColor; falls back to opaque
    /// black on a malformed string. sRGB initializer is available on iOS+macOS,
    /// so this stays UIKit/AppKit-free.
    private static func cgColor(_ hex: String, alpha: CGFloat) -> CGColor {
        var s = Substring(hex)
        if s.hasPrefix("#") { s = s.dropFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else {
            return CGColor(srgbRed: 0, green: 0, blue: 0, alpha: alpha)
        }
        return CGColor(
            srgbRed: CGFloat((v >> 16) & 0xff) / 255,
            green: CGFloat((v >> 8) & 0xff) / 255,
            blue: CGFloat(v & 0xff) / 255,
            alpha: alpha
        )
    }
}
