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
    private var applied: [Mark] = []

    public init(navigator: EpubWebNavigator) { self.navigator = navigator }

    /// One epub.js text mark: its type + CFI anchor (its identity) plus colour. `Equatable` on all
    /// three, but identity for the diff is `(type, cfiRange)` — see `diff`.
    struct Mark: Equatable { let type: String; let cfiRange: String; let color: String? }

    public func apply(_ decorations: [Decoration]) {
        let next: [Mark] = decorations.compactMap { d in
            guard let type = Self.epubType(d.style), let cfi = d.cfiRange, !cfi.isEmpty else { return nil }
            return Mark(type: type, cfiRange: cfi, color: d.color)
        }
        // DIFF instead of clear-all-then-re-add. `apply` runs on EVERY page turn (the host re-renders
        // on each relocation), but epub.js already re-injects its own annotations when a section
        // renders — so removing and re-adding every mark each time only churns epub.js and can race a
        // just-rendered mark back off the page (which is why marks "didn't show"). Only touch epub.js
        // for marks that actually appeared or disappeared.
        let (toAdd, toRemove) = Self.diff(from: applied, to: next)
        applied = next
        for m in toRemove { Task { await navigator.removeTextMark(type: m.type, cfiRange: m.cfiRange) } }
        for m in toAdd { Task { await navigator.addTextMark(type: m.type, cfiRange: m.cfiRange, color: m.color) } }
    }

    public func clear() {
        let toRemove = applied
        applied = []
        for m in toRemove {
            Task { await navigator.removeTextMark(type: m.type, cfiRange: m.cfiRange) }
        }
    }

    /// Pure set-diff keyed by `(type, cfiRange)` (a mark's identity): what's in `to` but not `from` is
    /// added; what's in `from` but not `to` is removed. `nonisolated` so it's unit-testable off-actor.
    nonisolated static func diff(from: [Mark], to: [Mark]) -> (add: [Mark], remove: [Mark]) {
        func key(_ m: Mark) -> String { m.type + "\u{1}" + m.cfiRange }
        let fromKeys = Set(from.map(key))
        let toKeys = Set(to.map(key))
        let add = to.filter { !fromKeys.contains(key($0)) }
        let remove = from.filter { !toKeys.contains(key($0)) }
        return (add, remove)
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
