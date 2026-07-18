#if canImport(UIKit)
import SwiftUI
import Postilla

/// Gap (points) kept between a docked panel and the safe-area edge.
private let panelEdgeMargin: Double = 6

/// Collects the currently-docked floating panels (id → edge + measured size + anchor + order) and resolves
/// their non-overlapping along-edge offsets via the portable `DockLane`. One instance is shared by all the
/// panels in a reader, so two panels docked to the same edge pack along it (side-by-side on top/bottom,
/// stacked on a side) instead of hiding each other.
@MainActor final class DockCoordinator: ObservableObject {
    private struct Reg: Equatable { var edge: PanelEdge; var size: CGSize; var anchor: DockAnchor; var order: Int }
    @Published private var regs: [String: Reg] = [:]

    /// Report a panel's dock state; pass `edge: nil` when it's floating or gone (drops it from every lane).
    ///
    /// The mutation is DEFERRED to the next main-actor tick: callers report from `onPreferenceChange` /
    /// `onChange`, which SwiftUI runs *inside* the view-update cycle, and mutating the `@Published regs`
    /// there triggers "Publishing changes from within view updates is not allowed" (and undefined
    /// behaviour — e.g. dropped repaints). Deferring publishes the change cleanly after the update; the
    /// guard keeps it idempotent so a settle can't loop.
    func report(id: String, edge: PanelEdge?, size: CGSize, anchor: DockAnchor, order: Int) {
        let next: Reg? = edge.map { Reg(edge: $0, size: size, anchor: anchor, order: order) }
        Task { @MainActor in
            if let next {
                if regs[id] != next { regs[id] = next }
            } else if regs[id] != nil {
                regs[id] = nil
            }
        }
    }

    /// A docked panel's leading offset along its edge (0 = lane start). With one panel per edge (enforced
    /// by the host via `isOccupied`), each panel uses its own anchor — `.center` centres it on the edge.
    func offset(for id: String, laneLength: CGFloat) -> CGFloat {
        guard let me = regs[id] else { return 0 }
        let horizontal = (me.edge == .top || me.edge == .bottom)
        let items = regs.filter { $0.value.edge == me.edge }.map { key, r in
            DockItem(id: key, extent: Double(horizontal ? r.size.width : r.size.height),
                     anchor: r.anchor, order: r.order)
        }
        return CGFloat(DockLane.offsets(items, laneLength: Double(laneLength))[id] ?? 0)
    }

    /// Is `edge` already taken by a DIFFERENT docked panel? The host uses this to keep two panels off the
    /// same edge — a panel that would snap onto an occupied edge stays floating instead.
    func isOccupied(_ edge: PanelEdge, excluding id: String) -> Bool {
        regs.contains { $0.key != id && $0.value.edge == edge }
    }
}

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
    /// Shared dock coordinator + this panel's identity/anchor/order in it, so docked panels pack along a
    /// shared edge instead of overlapping.
    @ObservedObject var dock: DockCoordinator
    let dockID: String
    var dockAnchor: DockAnchor = .start
    var dockOrder: Int = 0
    @ViewBuilder var content: (_ handlers: PanelDragHandlers) -> Content

    /// The measured panel size, used to keep the panel fully on-screen.
    @State private var panelSize: CGSize = .zero
    /// The panel's centre (points) captured when the current drag began, so the drag tracks by translation
    /// and the panel doesn't jump so its centre snaps under the grip on the first frame.
    @State private var dragStartCentre: CGPoint?

    var body: some View {
        GeometryReader { geo in
            let w = max(geo.size.width, 1), h = max(geo.size.height, 1)
            let handlers = PanelDragHandlers(
                coordinateSpace: coordinateSpace,
                // Drag by TRANSLATION from where it began — a continuous move from the docked spot to the
                // drop point, with no jump and no dependence on the (changing) placement mid-drag.
                onMove: { t in
                    let start = dragStartCentre ?? currentCentre(in: geo.size)
                    if dragStartCentre == nil { dragStartCentre = start }
                    onDrag(Double((start.x + t.width) / w), Double((start.y + t.height) / h))
                },
                onMoveEnd: { t in
                    let start = dragStartCentre ?? currentCentre(in: geo.size)
                    onDragEnd(Double((start.x + t.width) / w), Double((start.y + t.height) / h))
                    dragStartCentre = nil
                })
            content(handlers)
                .fixedSize()
                .background(GeometryReader { g in
                    Color.clear.preference(key: PanelSizeKey.self, value: g.size)
                })
                .onPreferenceChange(PanelSizeKey.self) { size in
                    if size.width > 0, size.height > 0 { panelSize = size }
                    dock.report(id: dockID, edge: dockEdge, size: panelSize, anchor: dockAnchor, order: dockOrder)
                }
                // Position by NATIVE edge alignment (docked) or centre (floating) + an offset — SwiftUI
                // aligns the real edge, so NO panel-size math is needed and the near edge can't hang off.
                // Both cases are the same two modifiers, so a docked→floating switch only changes values
                // (never the view structure) and the in-flight drag gesture survives.
                .frame(width: geo.size.width, height: geo.size.height, alignment: placementAlignment)
                .offset(placementOffset(in: geo.size))
        }
        .coordinateSpace(.named(coordinateSpace))
        .onChange(of: dockEdge) { _, edge in
            dock.report(id: dockID, edge: edge, size: panelSize, anchor: dockAnchor, order: dockOrder)
        }
        .onDisappear { dock.report(id: dockID, edge: nil, size: .zero, anchor: dockAnchor, order: dockOrder) }
    }

    /// The edge this panel is docked to (nil while floating) — drives its dock-lane registration.
    private var dockEdge: PanelEdge? {
        if case .docked(let e) = placement { return e } else { return nil }
    }

    /// Native alignment for the placement — an edge for docked (which also centres the panel on the cross
    /// axis for free), centre for floating.
    private var placementAlignment: Alignment {
        switch placement {
        case .docked(.leading):  return .leading
        case .docked(.trailing): return .trailing
        case .docked(.top):      return .top
        case .docked(.bottom):   return .bottom
        case .floating:          return .center
        }
    }

    /// Offset from the aligned spot: the small edge gap for docked, or the centre delta for floating.
    private func placementOffset(in area: CGSize) -> CGSize {
        let m = CGFloat(panelEdgeMargin)
        switch placement {
        case .docked(.leading):  return CGSize(width: m, height: 0)
        case .docked(.trailing): return CGSize(width: -m, height: 0)
        case .docked(.top):      return CGSize(width: 0, height: m)
        case .docked(.bottom):   return CGSize(width: 0, height: -m)
        case .floating(let x, let y):
            let cx = clampFloat(x * area.width, panel: panelSize.width, area: area.width)
            let cy = clampFloat(y * area.height, panel: panelSize.height, area: area.height)
            return CGSize(width: cx - area.width / 2, height: cy - area.height / 2)   // centre → (cx, cy)
        }
    }

    /// Keep a floating centre so the panel stays fully on-screen (best-effort before the size is measured).
    private func clampFloat(_ c: CGFloat, panel: CGFloat, area: CGFloat) -> CGFloat {
        let m = CGFloat(panelEdgeMargin)
        guard panel > 0 else { return min(max(c, area * 0.15), area * 0.85) }
        let half = panel / 2, lo = half + m, hi = area - half - m
        return lo < hi ? min(max(c, lo), hi) : area / 2
    }

    /// The panel's current centre in points — seeds the translation drag so there's no jump.
    private func currentCentre(in area: CGSize) -> CGPoint {
        let pw = panelSize.width, ph = panelSize.height, m = CGFloat(panelEdgeMargin)
        switch placement {
        case .docked(.leading):  return CGPoint(x: m + pw / 2, y: area.height / 2)
        case .docked(.trailing): return CGPoint(x: area.width - m - pw / 2, y: area.height / 2)
        case .docked(.top):      return CGPoint(x: area.width / 2, y: m + ph / 2)
        case .docked(.bottom):   return CGPoint(x: area.width / 2, y: area.height - m - ph / 2)
        case .floating(let x, let y):
            return CGPoint(x: clampFloat(x * area.width, panel: pw, area: area.width),
                           y: clampFloat(y * area.height, panel: ph, area: area.height))
        }
    }
}

/// Bubbles a floating panel's measured size up to `FloatingPanel` for on-screen clamping.
private struct PanelSizeKey: PreferenceKey {
    static let defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { value = nextValue() }
}

/// The drag wiring a `FloatingPanel` hands to its content: a coordinate space + move/end callbacks that
/// take the drag's **translation** (delta in points from where it began). A `PanelGrip` (or any draggable
/// subview) attaches to these; `FloatingPanel` turns the translation into the panel's new position.
struct PanelDragHandlers {
    let coordinateSpace: String
    let onMove: (CGSize) -> Void
    let onMoveEnd: (CGSize) -> Void
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

    // `AnyLayout` switches row⇄column WITHOUT changing subview identity — so when the axis flips mid-drag
    // (a side-dock becoming a float), the grip the finger is holding survives and the drag continues.
    @ViewBuilder private func arranged<C: View>(@ViewBuilder _ items: () -> C) -> some View {
        let layout = axis == .horizontal
            ? AnyLayout(HStackLayout(spacing: 10))
            : AnyLayout(VStackLayout(spacing: 10))
        layout { items() }
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
                    .onChanged { handlers.onMove($0.translation) }
                    .onEnded { handlers.onMoveEnd($0.translation) }
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
