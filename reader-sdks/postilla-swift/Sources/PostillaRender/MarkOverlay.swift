import Foundation
import ReaderContract
import Postilla

/// Drives an Octavo `DecorationHost` from a set of annotations — the
/// highlight/underline/strikeout/note layer. Pure coordination (no UIKit), so it
/// builds on macOS; the actual drawing is the host's job.
@MainActor
public final class MarkOverlay {
    private weak var host: DecorationHost?

    public init(host: DecorationHost) {
        self.host = host
    }

    /// Re-render the mark decorations for `annotations` (ink + tombstones are
    /// skipped — see `Decorations`).
    public func render(_ annotations: [Annotation]) {
        host?.apply(Decorations.decorations(for: annotations))
    }

    /// Pin a transient highlight at a locator (integration hook §4.2:
    /// `goTo(locator)` + ephemeral decoration). The id is caller-owned so it can
    /// be cleared independently.
    public func pinEphemeral(id: String, at locator: Locator, color: String? = nil) {
        host?.apply([Decoration(id: id, locator: locator, style: .highlight, color: color)])
    }

    public func clear() {
        host?.clear()
    }
}
