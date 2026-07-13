import Foundation

/// A live reading session: a `Navigator` wired to a `ReadingStore`,
/// `Capabilities`, and an optional `DecorationHost` seam. Returned by
/// `Octavo.open(...)`.
@MainActor
public final class Reader {
    public let navigator: Navigator
    public let readingStore: ReadingStore?
    public let capabilities: Capabilities
    /// The decoration seam the extension plugs into.
    public weak var decorations: DecorationHost?

    init(
        navigator: Navigator,
        readingStore: ReadingStore?,
        capabilities: Capabilities,
        decorations: DecorationHost?
    ) {
        self.navigator = navigator
        self.readingStore = readingStore
        self.capabilities = capabilities
        self.decorations = decorations
    }

    /// Register a location-change observer. Chains with any persistence wired by
    /// `Octavo.open` so the host can react without clobbering auto-save.
    public func onLocationChanged(_ handler: @escaping @MainActor (Locator) -> Void) {
        let existing = navigator.onLocationChanged
        navigator.onLocationChanged = { loc in
            existing?(loc)
            handler(loc)
        }
    }

    public var currentLocation: Locator? { navigator.currentLocation }

    public func goTo(_ locator: Locator) async throws {
        try await navigator.goTo(locator)
    }

    public func next() async throws { try await navigator.next() }
    public func prev() async throws { try await navigator.prev() }

    /// Resize the reading medium — PDF zoom, EPUB font (see `Navigator.bigger/smaller`).
    public func bigger() async { await navigator.bigger() }
    public func smaller() async { await navigator.smaller() }

    /// Apply a reading theme (background/foreground/night) — see `Navigator.applyTheme`.
    public func applyTheme(_ theme: ReaderTheme) async { await navigator.applyTheme(theme) }

    public func search(_ query: String) async throws -> [Locator] {
        try await navigator.search(query)
    }

    public func outline() -> [TocItem] { navigator.outline() }
}

/// Façade. The core entry point wires a `Navigator` (built by an engine target —
/// `OctavoPDFKit` / `OctavoEPUB`) into a `Reader`, auto-restores the saved
/// position, and auto-persists on location change.
///
/// Engine targets add convenience overloads (e.g. `Octavo.open(pdf:host:…)`) so
/// the integrator never imports a concrete navigator type.
public enum Octavo {

    /// Open a reading session over an already-constructed `Navigator`.
    ///
    /// - If `readingStore` is supplied, the last saved position is restored on
    ///   open and every subsequent location change is persisted.
    @MainActor
    @discardableResult
    public static func open(
        navigator: Navigator,
        publicationId: String? = nil,
        readingStore: ReadingStore? = nil,
        capabilities: Capabilities = .init(),
        decorations: DecorationHost? = nil
    ) async throws -> Reader {
        let reader = Reader(
            navigator: navigator,
            readingStore: readingStore,
            capabilities: capabilities,
            decorations: decorations
        )

        // Auto-persist position on change.
        if let store = readingStore, let pubId = publicationId {
            navigator.onLocationChanged = { loc in
                Task { try? await store.setPosition(pubId, loc) }
            }
        }

        try await navigator.open()

        // Restore a saved position, if any.
        if let store = readingStore, let pubId = publicationId,
           let saved = try? await store.getPosition(pubId) {
            try? await navigator.goTo(saved)
        }

        return reader
    }
}
