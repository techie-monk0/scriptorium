#if canImport(UIKit)
import SwiftUI
import PencilKit
import Postilla

/// PencilKit capture over the EPUB WKWebView. Unlike the PDF canvas (which normalizes to a fixed page),
/// EPUB ink must anchor to the **block under the stroke**, so this reports the finished `PKStroke` plus
/// its start point (in canvas = web-view = host coordinates); the host resolves the block CFI + rect at
/// that point and normalizes the stroke to it. `.pencilOnly` so a finger still pages/scrolls the book.
///
/// NOTE(device): capture over a WKWebView + the block anchor need on-device verification.
@MainActor
struct EpubInkCanvas: UIViewRepresentable {
    var color: String
    var width: Double
    let onStroke: (PKStroke, CGPoint) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> PKCanvasView {
        let canvas = PKCanvasView()
        canvas.backgroundColor = .clear
        canvas.isOpaque = false
        canvas.drawingPolicy = .pencilOnly
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
        var parent: EpubInkCanvas
        private var lastCount = 0
        init(_ parent: EpubInkCanvas) { self.parent = parent }

        func canvasViewDrawingDidChange(_ canvasView: PKCanvasView) {
            let strokes = canvasView.drawing.strokes
            guard strokes.count > lastCount else { lastCount = strokes.count; return }
            for stroke in strokes[lastCount...] {
                let start = stroke.path.count > 0 ? stroke.path[0].location : .zero
                parent.onStroke(stroke, start)
            }
            canvasView.drawing = PKDrawing()   // our renderer owns the pixels now
            lastCount = 0
        }
    }

    private static func uiColor(_ hex: String) -> UIColor {
        var s = Substring(hex); if s.hasPrefix("#") { s = s.dropFirst() }
        guard s.count == 6, let v = UInt32(s, radix: 16) else { return .systemBlue }
        return UIColor(red: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                       blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
}
#endif
