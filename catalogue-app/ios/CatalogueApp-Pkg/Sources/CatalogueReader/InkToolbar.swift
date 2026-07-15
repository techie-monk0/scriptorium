#if canImport(UIKit)
import SwiftUI
import Postilla
import CatalogueDesign

/// The pencil tool palette shown while drawing. It is a thin *renderer* of two portable SDK specs:
/// `InkToolController` (tool/colour/width/undo state) and `InkPaletteController` (the palette's layout,
/// arrangement, axis, and placement). It holds **no** layout policy of its own — it walks
/// `palette.layout` and lays items out along `palette.axis`, and the move handle reports normalized-ready
/// drag points upward. Android reimplements this surface in Compose over the same two specs.
///
/// Styled from `CatalogueDesign` tokens (a locally-built `Tokens`, as in `CatalogueInk`).
@MainActor
struct InkToolbar: View {
    @Binding var tool: InkToolController
    let palette: InkPaletteController
    let canUndo: Bool
    let canRedo: Bool
    let onUndo: () -> Void
    let onRedo: () -> Void
    let onDone: () -> Void
    /// Drag of the move handle, in the shared reader coordinate space (`InkToolbar.coordinateSpace`).
    let onMove: (CGPoint) -> Void
    let onMoveEnd: (CGPoint) -> Void

    /// The coordinate space the reader must name on the palette's container so drag points are relative
    /// to the reader area (the placement controller expects normalized area coordinates).
    static let coordinateSpace = "InkPaletteArea"

    private let tokens = Tokens(.default)

    var body: some View {
        axisStack {
            ForEach(Array(palette.layout.groups.enumerated()), id: \.offset) { gi, group in
                if gi > 0 { separator }
                ForEach(Array(group.items.enumerated()), id: \.offset) { _, entry in
                    item(entry)
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(radius: 6, y: 2)
        .fixedSize()   // hug content so a floating palette isn't full-width
    }

    // MARK: item dispatch — the abstract vocabulary → concrete controls

    @ViewBuilder private func item(_ entry: InkPaletteItem) -> some View {
        switch entry {
        case .tool(let t): toolButton(t)
        case .colors: if tool.tool != .eraser { colorRow }   // eraser has no colour
        case .width: widthControl
        case .undo:
            Button(action: onUndo) { Image(systemName: "arrow.uturn.backward") }.disabled(!canUndo)
        case .redo:
            Button(action: onRedo) { Image(systemName: "arrow.uturn.forward") }.disabled(!canRedo)
        case .done:
            Button("Done", action: onDone).font(.body.weight(.semibold))
        case .moveHandle: moveHandle
        }
    }

    // MARK: layout primitives (follow the SDK axis)

    @ViewBuilder private func axisStack<C: View>(@ViewBuilder content: () -> C) -> some View {
        switch palette.axis {
        case .horizontal: HStack(spacing: 12) { content() }
        case .vertical: VStack(spacing: 12) { content() }
        }
    }

    @ViewBuilder private var separator: some View {
        switch palette.axis {
        case .horizontal: Divider().frame(height: 22)
        case .vertical: Divider().frame(width: 30)
        }
    }

    // MARK: pieces

    @ViewBuilder private func toolButton(_ t: InkTool, system: String? = nil) -> some View {
        let active = tool.tool == t
        Button { tool.select(tool: t) } label: {
            Image(systemName: icon(t))
                .imageScale(.large)
                .frame(width: 34, height: 30)
                .background(active ? tokens.color(.accent).opacity(0.22) : .clear,
                            in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .foregroundStyle(active ? tokens.color(.accent) : tokens.color(.fg))
        }
        .accessibilityLabel(Text(name(t)))
        .accessibilityAddTraits(active ? .isSelected : [])
    }

    @ViewBuilder private var colorRow: some View {
        axisStack {
            ForEach(tool.palette.swatches, id: \.hex) { sw in swatch(sw) }
        }
    }

    @ViewBuilder private func swatch(_ sw: InkSwatch) -> some View {
        let selected = tool.color.lowercased() == sw.hex.lowercased()
        Button { tool.select(color: sw.hex) } label: {
            Circle()
                .fill(Color(hex: sw.hex))
                .frame(width: 22, height: 22)
                .overlay(Circle().stroke(tokens.color(.fg).opacity(selected ? 0.9 : 0.15),
                                         lineWidth: selected ? 2.5 : 1))
        }
        .accessibilityLabel(Text(sw.name ?? sw.hex))
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    @ViewBuilder private var widthControl: some View {
        switch palette.axis {
        case .horizontal:
            HStack(spacing: 8) {
                Image(systemName: "lineweight").foregroundStyle(.secondary)
                Slider(value: widthBinding, in: widthRange).frame(width: 110)
            }
        case .vertical:
            VStack(spacing: 6) {
                Button { step(+1) } label: { Image(systemName: "plus.circle") }
                Circle().fill(tokens.color(.fg))
                    .frame(width: previewDot, height: previewDot).frame(width: 22, height: 22)
                Button { step(-1) } label: { Image(systemName: "minus.circle") }
            }
        }
    }

    private var moveHandle: some View {
        Image(systemName: "arrow.up.and.down.and.arrow.left.and.right")
            .imageScale(.medium)
            .foregroundStyle(.secondary)
            .frame(width: 30, height: 28)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 2, coordinateSpace: .named(Self.coordinateSpace))
                    .onChanged { onMove($0.location) }
                    .onEnded { onMoveEnd($0.location) }
            )
            .accessibilityLabel(Text("Move palette"))
    }

    // MARK: width helpers

    private var widthBinding: Binding<Double> {
        Binding(get: { tool.width }, set: { tool.setWidth($0) })
    }

    private var widthRange: ClosedRange<Double> {
        switch tool.tool {
        case .highlighter: return 6...30
        case .eraser: return 8...48
        default: return 1...12
        }
    }

    private func step(_ dir: Double) {
        let s = (widthRange.upperBound - widthRange.lowerBound) / 10
        let next = min(max(tool.width + dir * s, widthRange.lowerBound), widthRange.upperBound)
        tool.setWidth(next)
    }

    private var previewDot: CGFloat {
        let span = widthRange.upperBound - widthRange.lowerBound
        let frac = span > 0 ? (tool.width - widthRange.lowerBound) / span : 0
        return 6 + CGFloat(frac) * 14
    }

    // MARK: naming

    private func icon(_ t: InkTool) -> String {
        switch t {
        case .pen: return "pencil.tip"
        case .highlighter: return "highlighter"
        case .eraser: return "eraser"
        case .select: return "hand.point.up.left"
        }
    }

    private func name(_ t: InkTool) -> String {
        switch t {
        case .pen: return "Pen"
        case .highlighter: return "Highlighter"
        case .eraser: return "Eraser"
        case .select: return "Select"
        }
    }
}
#endif
