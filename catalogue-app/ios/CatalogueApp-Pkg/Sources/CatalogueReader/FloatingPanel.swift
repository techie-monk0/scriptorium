#if canImport(UIKit)
import SwiftUI
import Postilla

/// A reusable **floating / dockable panel** — the moveability pattern the ink palette pioneered, factored
/// out so the reader's pinned chrome bars reuse it verbatim. It positions its content within the host area
/// per a portable `PanelPlacement` (docked to an edge with a small inset, or free-floating at a normalized
/// point) and hands the content a `PanelDragHandlers` bundle so a `PanelGrip` inside it can drag the panel
/// around. The placement *math* (snap/float, nearest edge) lives in Postilla's `PanelPlacementModel`; this
/// view only renders the result and forwards normalized drag points back to the caller's model.
@MainActor
struct FloatingPanel<Content: View>: View {
    /// Where to place the panel (read from the caller's `PanelPlacementModel`).
    let placement: PanelPlacement
    /// Normalized (`0…1` of the area) live-drag + release, applied by the caller to its model.
    let onDrag: (_ nx: Double, _ ny: Double) -> Void
    let onDragEnd: (_ nx: Double, _ ny: Double) -> Void
    /// The named coordinate space drags are measured in — unique per panel so two panels don't cross-talk.
    var coordinateSpace: String = "FloatingPanelArea"
    @ViewBuilder var content: (_ handlers: PanelDragHandlers) -> Content

    /// The measured panel size, used to keep a floating panel fully on-screen.
    @State private var panelSize: CGSize = .zero

    var body: some View {
        GeometryReader { geo in
            let w = max(geo.size.width, 1), h = max(geo.size.height, 1)
            let handlers = PanelDragHandlers(
                coordinateSpace: coordinateSpace,
                onMove: { onDrag(Double($0.x / w), Double($0.y / h)) },
                onMoveEnd: { onDragEnd(Double($0.x / w), Double($0.y / h)) })
            // Measure the panel in BOTH modes so the clamp always has a current size (the docked→float
            // switch changes the size, and an unmeasured panel must not strand off-screen).
            place(content(handlers)
                    .background(GeometryReader { g in
                        Color.clear.preference(key: PanelSizeKey.self, value: g.size)
                    }),
                  in: geo.size)
                .onPreferenceChange(PanelSizeKey.self) { panelSize = $0 }
        }
        .coordinateSpace(.named(coordinateSpace))
    }

    @ViewBuilder private func place(_ view: some View, in size: CGSize) -> some View {
        switch placement {
        case .docked(let edge):
            view.padding(padEdge(edge), 12)
                .frame(width: size.width, height: size.height, alignment: alignment(for: edge))
        case .floating(let x, let y):
            // Clamp the centre so NO part of the panel — including the move handle — leaves the area.
            view.position(x: clampCentre(x * size.width, panel: panelSize.width, area: size.width),
                          y: clampCentre(y * size.height, panel: panelSize.height, area: size.height))
        }
    }

    /// Keep a panel of `panel` extent centred at `c` fully inside the area (with a small margin); centre it
    /// if it's larger than the area. Before the panel is measured (`panel == 0`), fall back to a safe inner
    /// band so a drag can never strand it — and the move handle — off-screen.
    private func clampCentre(_ c: CGFloat, panel: CGFloat, area: CGFloat) -> CGFloat {
        let margin: CGFloat = 6
        guard panel > 0 else { return min(max(c, area * 0.15), area * 0.85) }
        let half = panel / 2
        let lo = half + margin, hi = area - half - margin
        return lo < hi ? min(max(c, lo), hi) : area / 2
    }

    private func padEdge(_ e: PanelEdge) -> Edge.Set {
        switch e {
        case .top: return .top
        case .bottom: return .bottom
        case .leading: return .leading
        case .trailing: return .trailing
        }
    }

    private func alignment(for e: PanelEdge) -> Alignment {
        switch e {
        case .top: return .top
        case .bottom: return .bottom
        case .leading: return .leading
        case .trailing: return .trailing
        }
    }
}

/// Bubbles a floating panel's measured size up to `FloatingPanel` for on-screen clamping.
private struct PanelSizeKey: PreferenceKey {
    static let defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { value = nextValue() }
}

/// The drag wiring a `FloatingPanel` hands to its content: a coordinate space + move/end callbacks that
/// already normalize to `0…1`. A `PanelGrip` (or any draggable subview) attaches to these.
struct PanelDragHandlers {
    let coordinateSpace: String
    let onMove: (CGPoint) -> Void
    let onMoveEnd: (CGPoint) -> Void
}

/// The standard visual chrome of a floating panel: a built-in **move handle** (`PanelGrip`) on **both
/// ends** of the panel's content, laid out along the panel's `axis` — a **row** when docked top/bottom or
/// floating (grips left & right), a **column** when docked to a side (grips top & bottom) — on the
/// standard material background. Callers supply only their controls; the move handles and arrangement come
/// from the component so every floating panel matches and can be grabbed from either end.
struct PanelChrome<Content: View>: View {
    let handlers: PanelDragHandlers
    let axis: PanelAxis
    @ViewBuilder var content: () -> Content

    var body: some View {
        arranged {
            PanelGrip(handlers: handlers)
            content()
            PanelGrip(handlers: handlers)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(radius: 6, y: 2)
        .fixedSize()   // hug content so the floating panel isn't full-width/height
    }

    @ViewBuilder private func arranged<C: View>(@ViewBuilder _ items: () -> C) -> some View {
        switch axis {
        case .horizontal: HStack(spacing: 10, content: items)
        case .vertical: VStack(spacing: 10, content: items)
        }
    }
}

/// The move handle for a floating panel — drag it to reposition. Reports drags in the panel's coordinate
/// space via the `PanelDragHandlers` the `FloatingPanel` provides (the ink palette's move handle is the
/// same affordance).
struct PanelGrip: View {
    let handlers: PanelDragHandlers
    var systemImage = "arrow.up.and.down.and.arrow.left.and.right"

    var body: some View {
        Image(systemName: systemImage)
            .imageScale(.medium)
            .foregroundStyle(.secondary)
            .frame(width: 30, height: 28)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 2, coordinateSpace: .named(handlers.coordinateSpace))
                    .onChanged { handlers.onMove($0.location) }
                    .onEnded { handlers.onMoveEnd($0.location) }
            )
            .accessibilityLabel(Text("Move"))
    }
}

/// The standard pin toggle for a pinnable panel — the moded half of the floating-panel pattern, so every
/// pinnable bar behaves identically. `pinned == false` is **auto-hide** mode and shows an unfilled `pin`;
/// tapping flips to **floating** mode and a filled `pin.fill`; tapping again returns to auto-hide. The
/// caller renders its content in the nav bar while unpinned and inside a `FloatingPanel` while pinned.
struct PanelPinButton: View {
    @Binding var pinned: Bool
    /// The auto-hide (unpinned) and floating (pinned) symbols — default to SF Symbols `pin` / `pin.fill`.
    var symbol: String = "pin"
    var filledSymbol: String = "pin.fill"

    var body: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) { pinned.toggle() }
        } label: {
            Image(systemName: pinned ? filledSymbol : symbol)
        }
        .accessibilityLabel(Text(pinned ? "Unpin bar (auto-hide)" : "Pin bar (float)"))
    }
}
#endif
