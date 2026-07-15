import Foundation

/// One control in the reader chrome spec (see `readerChromeVM`). `id` names the capability; `bar` is
/// "general" (leading) or "text" (trailing); `overflow` means it collapses into the ⋯ menu; `active`
/// means it is toggled on. A surface renders each control by `id` in its own toolkit.
public struct ReaderControl: Equatable, Sendable, Identifiable {
    public let id: String
    public let bar: String
    public let overflow: Bool
    public let active: Bool
    public init(id: String, bar: String, overflow: Bool, active: Bool) {
        self.id = id; self.bar = bar; self.overflow = overflow; self.active = active
    }
}

/// Reader capabilities a surface declares; the spec decides which controls show. A capability a surface
/// can't back yet (e.g. iOS text-annotation / export) is simply passed `false` — the control stays in
/// the shared spec and lights up on that surface the moment it declares support.
public struct ReaderCaps: Equatable, Sendable {
    public var ready: Bool
    public var search: Bool
    public var star: Bool
    public var resizeText: Bool   // font-size controls A± (EPUB; text reflows)
    public var zoom: Bool         // magnifier zoom out/in + fit-width (PDF; a fixed page zooms, not resizes)
    public var reflow: Bool       // reflow a PDF page to text
    public var markText: Bool     // highlight + underline (selection-anchored text marks)
    public var strike: Bool       // strikethrough
    public var note: Bool         // notes
    public var draw: Bool         // freehand ink (iOS only — web/PWA have no ink)
    public var erase: Bool        // erase marks
    public var annList: Bool      // annotations list
    public var export: Bool       // export annotated copy
    public init(ready: Bool = false, search: Bool = false, star: Bool = false,
                resizeText: Bool = false, zoom: Bool = false, reflow: Bool = false, markText: Bool = false,
                strike: Bool = false, note: Bool = false, draw: Bool = false, erase: Bool = false,
                annList: Bool = false, export: Bool = false) {
        self.ready = ready; self.search = search; self.star = star
        self.resizeText = resizeText; self.zoom = zoom; self.reflow = reflow; self.markText = markText
        self.strike = strike; self.note = note; self.draw = draw; self.erase = erase
        self.annList = annList; self.export = export
    }
}

/// The SHARED reader-chrome "bars" spec — 1:1 with `library-core.js` `readerChromeVM` (golden-tested for
/// parity). Enumerates the reader's control set once; every surface renders it in its own toolkit, so a
/// capability added here appears on all surfaces rather than being hand-mirrored per surface.
/// Capability-driven — no hardcoded format/surface checks. `compact` (phone / narrow width) collapses the
/// annotation + mode-specific controls into the ⋯ overflow; on a regular width they sit inline. See the JS
/// original for the full rationale.
public func readerChromeVM(format: String, caps: ReaderCaps,
                           reflow: Bool = false, draw: Bool = false,
                           compact: Bool = false) -> [ReaderControl] {
    var out: [ReaderControl] = []
    func c(_ id: String, _ bar: String, _ overflow: Bool = false, _ active: Bool = false) {
        out.append(ReaderControl(id: id, bar: bar, overflow: overflow, active: active))
    }
    // Annotation + mode-specific controls: inline on a regular width, collapsed into ⋯ on a phone.
    func tool(_ id: String, _ active: Bool = false) { c(id, "text", compact, active) }

    c("done", "general")
    if caps.ready {
        c("toc", "general")
        if caps.search { c("search", "general") }
        if caps.star { c("star", "general") }

        if caps.resizeText { tool("textSmaller"); tool("textLarger") }        // EPUB: font A±
        if caps.zoom { tool("zoomOut"); tool("zoomIn"); tool("fitWidth") }    // PDF: magnifier ± + fit width
        if caps.reflow { tool("reflow", reflow) }
        c("goto", "text")                   // jump to a page (PDF) / position (EPUB)
        c("theme", "text")                  // a direct cycle button (never in the ⋯)
        if caps.markText { tool("highlight"); tool("underline") }
        if caps.strike { tool("strike") }
        if caps.note { tool("note") }
        if caps.draw { tool("draw", draw) }
        if caps.erase { tool("erase") }
        if caps.export { tool("export") }
        if caps.annList { c("annList", "text", true) }   // secondary — always in ⋯
        c("bookmarkAdd", "text", true)
        c("bookmarkList", "text", true)
    }
    return out
}
