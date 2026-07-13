#if canImport(UIKit)
import SwiftUI
import PencilKit
import PDFKit
import Postilla
import PostillaUI

/// PencilKit capture surface laid over the PDF page. **PencilKit is input-only** (the canonical-ink
/// rule): a finished stroke is converted to a normalized `InkStroke` — page-relative `0…1` with the
/// N0 per-point timestamp from `PKStrokePoint.timeOffset` — handed back via `onStroke`, and the
/// PencilKit drawing is then cleared so the stroke renders through our `FreehandRenderer` /
/// `PdfInkHost` (identical on web / iOS / export), never PencilKit's native ink.
///
/// Palm rejection is PencilKit-native: `drawingPolicy = .pencilOnly` lets a finger scroll the PDF and
/// only the Pencil draws.
///
/// NOTE(device): the canvas⇄PDF-page coordinate mapping and the stroke-finished lifecycle need
/// on-device Apple-Pencil verification (handwriting_TODO #4) — they can't be exercised headless. The
/// decision logic that *is* testable lives in `InkCapture`.
@MainActor
struct PencilKitInkCanvas: UIViewRepresentable {
    let pdfView: PDFView
    var color: String
    var width: Double
    let onStroke: (InkStroke) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> PKCanvasView {
        let canvas = PKCanvasView()
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.drawingPolicy = .pencilOnly                    // palm rejection (finger scrolls)
        canvas.delegate = context.coordinator
        canvas.tool = PKInkingTool(.pen, color: Self.uiColor(color), width: width)
        return canvas
    }

    func updateUIView(_ canvas: PKCanvasView, context: Context) {
        canvas.tool = PKInkingTool(.pen, color: Self.uiColor(color), width: width)
        context.coordinator.parent = self
    }

    @MainActor
    final class Coordinator: NSObject, PKCanvasViewDelegate {
        var parent: PencilKitInkCanvas
        private var lastCount = 0
        init(_ parent: PencilKitInkCanvas) { self.parent = parent }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            let strokes = canvasView.drawing.strokes
            guard strokes.count > lastCount else { lastCount = strokes.count; return }
            guard let page = parent.pdfView.currentPage else { lastCount = strokes.count; return }
            // The canvas overlays the PDFView, so a stroke's points are in PDFView coordinates; the
            // page's rect there is the 0…1 normalization box (`InkCanvas.strokeFrom`).
            let pageRect = parent.pdfView.convert(page.bounds(for: .cropBox), from: page)
            for stroke in strokes[lastCount...] {
                parent.onStroke(InkCanvas.strokeFrom(stroke, in: pageRect, color: parent.color))
            }
            canvasView.drawing = PKDrawing()                  // our renderer owns the pixels now
            lastCount = 0
        }
    }

    private static func uiColor(_ hex: String) -> UIColor {
        var s = Substring(hex); if s.hasPrefix("#") { s = s.dropFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return .systemRed }
        return UIColor(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                       blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
}
#endif
