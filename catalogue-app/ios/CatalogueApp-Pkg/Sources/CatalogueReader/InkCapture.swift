import Foundation
import Postilla
import Octavo

/// The **testable core** of ink capture: turn a finished stroke set captured at a `Locator` into the
/// structured `kind:.ink` `Annotation` (the sync-of-record, not a file blob). The PencilKit surface
/// (`PencilKitInkCanvas`) is the untestable UI shell that produces the `Ink`; everything decision-
/// making lives here so it can be unit-tested without a device.
enum InkCapture {
    /// One finished stroke set → one ink annotation at `locator`. Returns nil for empty ink (nothing
    /// to store), so a stray tap doesn't create a blank mark.
    static func annotation(ink: Ink, locator: Locator, publicationId: String,
                           rev: Int, now: Date) -> Annotation? {
        guard !ink.strokes.isEmpty, ink.strokes.contains(where: { !$0.points.isEmpty }) else {
            return nil
        }
        return Annotation(publicationId: publicationId, kind: .ink, locator: locator, ink: ink,
                          createdAt: now, updatedAt: now, rev: rev)
    }
}
