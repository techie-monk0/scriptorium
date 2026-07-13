import SwiftUI
import CatalogueDesign

/// The resolved design tokens, injected into the environment so any view can theme itself
/// (`@Environment(\.tokens)`), and recomputed when the OS scheme or the theme pref changes.
private struct TokensKey: EnvironmentKey { static let defaultValue = Tokens(.light) }
public extension EnvironmentValues {
    var tokens: Tokens {
        get { self[TokensKey.self] }
        set { self[TokensKey.self] = newValue }
    }
}

/// Resolves `ThemePreference` (auto/light/dark) against the OS scheme, injects `Tokens`, pins
/// `.preferredColorScheme`, and tints controls with the palette `link`. Apply once at the root.
public struct ThemedRoot<Content: View>: View {
    private let pref: ThemePreference
    private let content: Content
    @Environment(\.colorScheme) private var scheme

    public init(_ pref: ThemePreference, @ViewBuilder content: () -> Content) {
        self.pref = pref; self.content = content()
    }

    public var body: some View {
        let theme = pref.resolved(osDark: scheme == .dark)
        let tokens = Tokens(theme)
        content
            .environment(\.tokens, tokens)
            .tint(tokens.link)
            .background(tokens.bg.ignoresSafeArea())
            .preferredColorScheme(pref.preferredColorScheme)
    }
}
