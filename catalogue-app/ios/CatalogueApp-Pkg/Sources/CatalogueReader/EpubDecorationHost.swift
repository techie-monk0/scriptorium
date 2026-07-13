#if canImport(WebKit)   // EPUB host rides on OctavoEPUB (WebKit), available on macOS → swift-test'able
import Foundation
import Octavo
import OctavoEPUB

/// The EPUB adapter of octavo's `DecorationHost` — renders text marks as epub.js annotations over the
/// WKWebView, driven by postilla's `MarkOverlay`/`Decorations` (the same driver as `PdfDecorationHost`,
/// a different engine). Each `Decoration` carries its precise `cfiRange` (the §5 anchor); ink is not a
/// `Decoration` (it renders through the ink host). Strikethrough/note are skipped on EPUB for now
/// (epub.js has no native strikethrough; notes are a separate surface).
@MainActor
public final class EpubDecorationHost: DecorationHost {
    private let navigator: EpubWebNavigator
    private var applied: [(type: String, cfiRange: String)] = []

    public init(navigator: EpubWebNavigator) { self.navigator = navigator }

    public func apply(_ decorations: [Decoration]) {
        clear()
        for d in decorations {
            guard let type = Self.epubType(d.style),
                  let cfi = d.cfiRange, !cfi.isEmpty else { continue }
            applied.append((type, cfi))
            Task { await navigator.addTextMark(type: type, cfiRange: cfi, color: d.color) }
        }
    }

    public func clear() {
        let toRemove = applied
        applied = []
        for m in toRemove {
            Task { await navigator.removeTextMark(type: m.type, cfiRange: m.cfiRange) }
        }
    }

    /// The epub.js annotation type for a style, or nil to skip (strikethrough/note unsupported here).
    /// `nonisolated` — a pure mapping with no actor state, so it's callable from sync test code.
    nonisolated static func epubType(_ style: Decoration.Style) -> String? {
        switch style {
        case .highlight: return "highlight"
        case .underline: return "underline"
        case .strikethrough, .note: return nil
        }
    }
}
#endif
