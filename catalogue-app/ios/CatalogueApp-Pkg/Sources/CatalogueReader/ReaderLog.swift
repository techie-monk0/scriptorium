import os

/// Console instrumentation for the annotation render/restore path — so a "mark didn't stick on tab
/// switch" can be diagnosed on-device (where PDFKit repaint / epub.js paint timing isn't observable in a
/// unit test). Filter Console.app by subsystem `catalogue.reader`, category `annotations`; the trace on
/// reopen shows whether marks were pulled → rendered → added to the page/host, pinpointing the break.
enum ReaderLog {
    static let annotations = Logger(subsystem: "catalogue.reader", category: "annotations")
    /// The authored-outline editor seam — its SwiftUI sheet isn't headless-observable, so trace
    /// open/save/bake here. Filter Console.app by subsystem `catalogue.reader`, category `outline`.
    static let outline = Logger(subsystem: "catalogue.reader", category: "outline")
}
