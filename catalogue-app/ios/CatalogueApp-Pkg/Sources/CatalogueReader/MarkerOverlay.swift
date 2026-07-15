#if canImport(UIKit)
import SwiftUI
import Postilla

/// One action in an `MarkerOverlay`'s long-press context menu. `role: .destructive` renders in red (Delete),
/// matching the platform. `systemImage` is optional — omit it for a plain text row.
struct MarkerOverlayAction: Identifiable {
    let id = UUID()
    let title: String
    var systemImage: String? = nil
    var role: ButtonRole? = nil
    let perform: () -> Void
}

/// A reusable **icon pinned on the content** — a small marker at a normalized point in the host area that
/// can be (1) **dragged** to move, (2) **tapped** to open an anchored detail popover, and (3)
/// **long-pressed** for a context menu. The note marker is the first consumer, but this knows nothing
/// about notes / PDF / EPUB: it's the icon sibling of `FloatingPanel`, driven by the portable
/// `MarkerOverlayModel` whose *moves* (clamp + state machine) live in Postilla. This view only renders the
/// result and reports normalized drag points back into the bound model, then persists via `onMoved`.
///
/// The three affordances are gated by the model (`canMove` / `canExpand` / `canShowMenu`) so a read-only
/// surface can allow tap-to-view while forbidding drag + delete.
@MainActor
struct MarkerOverlay<Icon: View, Detail: View>: View {
    /// The interaction state + anchor, driven by this view's gestures (the caller owns the storage).
    @Binding var model: MarkerOverlayModel
    /// Called with the committed anchor after a drag ends, so the caller can persist the moved position.
    var onMoved: (PanelPoint) -> Void = { _ in }
    /// Context-menu rows shown on long-press. Empty (or `canShowMenu == false`) ⇒ no menu.
    var menuActions: [MarkerOverlayAction] = []
    /// A per-instance coordinate space so two overlays don't cross-talk during drags.
    var coordinateSpace: String = "MarkerOverlayArea"
    /// The marker itself (e.g. the note badge).
    @ViewBuilder var icon: () -> Icon
    /// The anchored detail popover shown while expanded (e.g. the note text).
    @ViewBuilder var detail: () -> Detail

    /// The icon's centre (points) captured when the current drag began, so the move tracks by translation
    /// and the icon doesn't jump under the finger on the first frame (the `FloatingPanel` idiom).
    @State private var dragStartCentre: CGPoint?

    var body: some View {
        GeometryReader { geo in
            let w = max(geo.size.width, 1), h = max(geo.size.height, 1)
            let centre = CGPoint(x: model.anchor.x * w, y: model.anchor.y * h)
            icon()
                .contentShape(Rectangle())
                .onTapGesture { model.tap() }
                .gesture(model.canMove ? dragGesture(centre: centre, w: w, h: h) : nil)
                .markerOverlayMenu(enabled: model.canShowMenu && !menuActions.isEmpty,
                                 actions: menuActions) { model.dismiss() }
                .popover(isPresented: Binding(get: { model.isExpanded },
                                              set: { if !$0 { model.dismiss() } })) {
                    detail().presentationCompactAdaptation(.popover)
                }
                // Place the icon at its normalized anchor via the SAME native-alignment + offset idiom as
                // `FloatingPanel` — the icon keeps its natural size, so ONLY the icon is hit-testable and
                // the rest of the area passes touches through to the content (scroll/tap) beneath.
                .fixedSize()
                .frame(width: w, height: h, alignment: .center)
                .offset(x: centre.x - w / 2, y: centre.y - h / 2)
        }
        .coordinateSpace(.named(coordinateSpace))
    }

    /// Translation-based drag: seed from the icon's centre at drag-start, add the finger's translation,
    /// and feed the normalized result into the model (which clamps it). Persist on release.
    private func dragGesture(centre: CGPoint, w: CGFloat, h: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .named(coordinateSpace))
            .onChanged { v in
                let start = dragStartCentre ?? centre
                if dragStartCentre == nil { dragStartCentre = start }
                model.beginDrag()
                model.drag(toX: Double((start.x + v.translation.width) / w),
                           y: Double((start.y + v.translation.height) / h))
            }
            .onEnded { v in
                let start = dragStartCentre ?? centre
                let committed = model.endDrag(atX: Double((start.x + v.translation.width) / w),
                                              y: Double((start.y + v.translation.height) / h))
                dragStartCentre = nil
                onMoved(committed)
            }
    }
}

private extension View {
    /// Attach the long-press context menu only when it's enabled and non-empty — an empty `.contextMenu`
    /// would still arm a long-press that opens nothing.
    @ViewBuilder
    func markerOverlayMenu(enabled: Bool, actions: [MarkerOverlayAction],
                         onFire: @escaping () -> Void) -> some View {
        if enabled {
            self.contextMenu {
                ForEach(actions) { a in
                    Button(role: a.role) { a.perform(); onFire() } label: {
                        if let s = a.systemImage { Label(a.title, systemImage: s) } else { Text(a.title) }
                    }
                }
            }
        } else {
            self
        }
    }
}
#endif
