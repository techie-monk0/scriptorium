import Foundation

/// A neutral reading-theme value the host hands the navigator. octavo defines only the *shape*
/// (background / foreground hex + a dark flag) — it never imports the catalogue palette; the concrete
/// colours (from `ReadingPalette`) are resolved at the composition root and passed in.
public struct ReaderTheme: Sendable, Equatable {
    public var bg: String
    public var fg: String
    public var isDark: Bool
    public init(bg: String, fg: String, isDark: Bool) {
        self.bg = bg; self.fg = fg; self.isDark = isDark
    }
}

/// An entry in a publication's table of contents.
public struct TocItem: Sendable, Equatable {
    public var title: String
    public var locator: Locator
    public var children: [TocItem]

    public init(title: String, locator: Locator, children: [TocItem] = []) {
        self.title = title
        self.locator = locator
        self.children = children
    }
}

/// PROTOCOL: the only per-platform piece. Everything above it is shared. A
/// Navigator opens a publication, moves through it, searches it, and reports
/// where it is via `onLocationChanged`.
///
/// Invariant (`OS-U5`): after `goTo(loc)` the next emitted location resolves to
/// the same `Locator` (page/CFI/progression consistency).
///
/// `@MainActor` because concrete engines drive UIKit/AppKit/WebKit views.
@MainActor
public protocol Navigator: AnyObject {
    /// Open the publication and emit the initial location.
    func open() async throws

    /// Navigate to a locator; emits `onLocationChanged`.
    func goTo(_ locator: Locator) async throws

    /// Advance one page/screen.
    func next() async throws

    /// Go back one page/screen.
    func prev() async throws

    /// Increase / decrease the reading size. The one "resize" verb the web `ctrl` also unifies:
    /// concrete engines map it to their medium — PDF zoom, EPUB font size. Optional (default no-op).
    func bigger() async
    func smaller() async

    /// Apply a reading theme (background/foreground/night). PDF tints/inverts the page; EPUB recolours
    /// via epub.js. Optional (default no-op) — the web `ctrl.setTheme` analogue.
    func applyTheme(_ theme: ReaderTheme) async

    /// Full-text search; results are ordered Locators.
    func search(_ query: String) async throws -> [Locator]

    /// The publication's table of contents (may be empty).
    func outline() -> [TocItem]

    /// The current reading position, if open.
    var currentLocation: Locator? { get }

    /// Called whenever the reading position changes.
    var onLocationChanged: (@MainActor (Locator) -> Void)? { get set }
}

public extension Navigator {
    // Resize is optional: engines that have no notion of it (or a fixed layout) inherit a no-op.
    func bigger() async {}
    func smaller() async {}
    // Theming is optional too: an engine that can't recolour inherits a no-op.
    func applyTheme(_ theme: ReaderTheme) async {}
}
