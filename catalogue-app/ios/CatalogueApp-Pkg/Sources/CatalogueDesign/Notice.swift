import SwiftUI

/// A **notice** — the app's single empty / error / placeholder state, expressed as surface-agnostic
/// DATA rather than a hand-rolled view. Every screen that needs to say "there's nothing here" or
/// "this failed" builds a `Notice` and renders it with `NoticeView`, so the states stay visually
/// identical and can't drift apart. A notice with a dismissing action is what a modal presentation
/// (a `fullScreenCover` / `sheet` with no nav chrome) uses so it can never become a dead-end.
///
/// The spec is the abstraction; `NoticeView` is the iOS adapter. Keeping the icon/title/message as
/// plain values (and the button role as `NoticeRole`, not SwiftUI's `ButtonRole`) means another
/// surface — or a test — can consume the same spec without importing the renderer.
public struct Notice {
    /// SF Symbol name for the large glyph.
    public var icon: String
    public var title: String
    /// Optional secondary line under the title.
    public var message: String?
    /// Buttons under the message. Empty → a plain, actionless notice (a drop-in for the bare
    /// `ContentUnavailableView` screens used before); one `.close` → a modal gets its way out.
    public var actions: [NoticeAction]

    public init(icon: String, title: String, message: String? = nil, actions: [NoticeAction] = []) {
        self.icon = icon
        self.title = title
        self.message = message
        self.actions = actions
    }
}

/// One button on a `Notice`. `handler` runs on tap; `role` maps to the platform button role.
public struct NoticeAction: Identifiable {
    public let id = UUID()
    public var title: String
    public var role: NoticeRole
    /// Draw as the filled, primary button (the obvious way forward from an empty state).
    public var prominent: Bool
    public var handler: () -> Void

    public init(_ title: String, role: NoticeRole = .normal, prominent: Bool = false,
                handler: @escaping () -> Void) {
        self.title = title
        self.role = role
        self.prominent = prominent
        self.handler = handler
    }

    /// The standard "get me out of here" action for a modal notice (a `fullScreenCover` / `sheet`
    /// with no system chrome). Prominent, cancel-role, labelled "Close" by default.
    public static func close(_ title: String = "Close", handler: @escaping () -> Void) -> NoticeAction {
        NoticeAction(title, role: .cancel, prominent: true, handler: handler)
    }
}

/// Surface-agnostic button role. Kept separate from SwiftUI's `ButtonRole` so a `Notice` carries no
/// framework type; the renderer maps it (a hypothetical web/PWA renderer would map it to its own).
public enum NoticeRole: Sendable, Equatable {
    case normal, cancel, destructive

    /// The SwiftUI role this maps to (nil = a default button).
    public var buttonRole: ButtonRole? {
        switch self {
        case .normal:      return nil
        case .cancel:      return .cancel
        case .destructive: return .destructive
        }
    }
}

/// Renders a `Notice` as the app's standard empty / error state. A thin wrapper over the native
/// `ContentUnavailableView`, so it inherits the platform look, spacing and Dynamic Type — with a
/// data-driven API and a row of actions on top. With no actions it looks exactly like the bare
/// `ContentUnavailableView` the screens used before this component existed.
public struct NoticeView: View {
    private let notice: Notice

    public init(_ notice: Notice) { self.notice = notice }

    /// Convenience: build the notice inline at the call site.
    public init(icon: String, title: String, message: String? = nil, actions: [NoticeAction] = []) {
        self.init(Notice(icon: icon, title: title, message: message, actions: actions))
    }

    public var body: some View {
        ContentUnavailableView {
            Label(notice.title, systemImage: notice.icon)
        } description: {
            if let message = notice.message { Text(message) }
        } actions: {
            ForEach(notice.actions) { NoticeButton(action: $0) }
        }
    }
}

/// One notice button, split out so the prominent/plain style branch is a clean `@ViewBuilder`
/// (the two button styles are different concrete types, so they can't share one expression).
private struct NoticeButton: View {
    let action: NoticeAction

    @ViewBuilder var body: some View {
        if action.prominent {
            Button(action.title, role: action.role.buttonRole, action: action.handler)
                .buttonStyle(.borderedProminent)
        } else {
            Button(action.title, role: action.role.buttonRole, action: action.handler)
                .buttonStyle(.bordered)
        }
    }
}

// MARK: - Popup presentation

/// The same `Notice`, rendered as a **bounded, centered card** instead of a region-filling state —
/// for a notice presented as an interruption (a popup / alert-like overlay) rather than an inline
/// "this panel is empty" placeholder. Sized to its content (capped width), material background, so it
/// floats over whatever is behind it. Pair with `NoticeOverlay` (or `.noticePopup`) for the scrim.
public struct NoticeCard: View {
    private let notice: Notice

    public init(_ notice: Notice) { self.notice = notice }

    public var body: some View {
        VStack(spacing: Spacing.md) {
            Image(systemName: notice.icon)
                .font(.largeTitle)
                .foregroundStyle(.secondary)
            Text(notice.title)
                .font(.headline)
                .multilineTextAlignment(.center)
            if let message = notice.message {
                Text(message)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            if !notice.actions.isEmpty {
                HStack(spacing: Spacing.sm) {
                    ForEach(notice.actions) { NoticeButton(action: $0) }
                }
                .padding(.top, Spacing.xs)
            }
        }
        .padding(Spacing.xl)
        .frame(maxWidth: 320)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .shadow(radius: 20, y: 8)
        .padding(Spacing.xl)
    }
}

/// A `Notice` presented as a **dismissible popup**: a dimmed scrim (tap anywhere outside the card to
/// dismiss) with a centered `NoticeCard`. `onDismiss` runs on a scrim tap — the "tap outside" gesture
/// — while the card's own buttons run their own handlers (e.g. a `.close` that also dismisses). Use
/// this inside a presentation whose background you've made clear (so the scrim dims the screen behind
/// it), or over any content via `.noticePopup`.
public struct NoticeOverlay: View {
    private let notice: Notice
    private let onDismiss: () -> Void

    public init(_ notice: Notice, onDismiss: @escaping () -> Void) {
        self.notice = notice
        self.onDismiss = onDismiss
    }

    public var body: some View {
        ZStack {
            Color.black.opacity(0.32)
                .ignoresSafeArea()
                .contentShape(Rectangle())          // the whole scrim is the "outside" tap target
                .onTapGesture(perform: onDismiss)
            NoticeCard(notice)                      // taps on the card don't reach the scrim → no dismiss
        }
    }
}

public extension View {
    /// Present `notice` as a dismissible popup over this view: a dimmed scrim (tap outside to dismiss)
    /// with a centered card. Setting the binding drives it; a scrim tap clears the binding, and a
    /// `.close`/cancel action in the notice can too. The reusable "popup type overlay" for any screen.
    func noticePopup(_ notice: Binding<Notice?>) -> some View {
        overlay {
            if let value = notice.wrappedValue {
                NoticeOverlay(value) { notice.wrappedValue = nil }
                    .transition(.opacity)
            }
        }
    }
}
