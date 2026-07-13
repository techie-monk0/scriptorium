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
    public var annotate: Bool
    public var annotateText: Bool
    public var export: Bool
    public var reflow: Bool
    public init(ready: Bool = false, search: Bool = false, star: Bool = false, annotate: Bool = false,
                annotateText: Bool = false, export: Bool = false, reflow: Bool = false) {
        self.ready = ready; self.search = search; self.star = star; self.annotate = annotate
        self.annotateText = annotateText; self.export = export; self.reflow = reflow
    }
}

/// The SHARED reader-chrome "bars" spec — 1:1 with `library-core.js` `readerChromeVM` (golden-tested for
/// parity). Enumerates the reader's control set once; every surface renders it in its own toolkit, so a
/// capability added here appears on all surfaces rather than being hand-mirrored per surface.
public func readerChromeVM(format: String, caps: ReaderCaps,
                           reflow: Bool = false, draw: Bool = false) -> [ReaderControl] {
    let pdf = format == "pdf", epub = format == "epub"
    var out: [ReaderControl] = []
    func c(_ id: String, _ bar: String, _ overflow: Bool = false, _ active: Bool = false) {
        out.append(ReaderControl(id: id, bar: bar, overflow: overflow, active: active))
    }
    c("done", "general")
    if caps.ready {
        c("toc", "general")
        if caps.search { c("search", "general") }
        if caps.star { c("star", "general") }

        if epub || (pdf && reflow) { c("textSmaller", "text"); c("textLarger", "text") }
        if pdf && caps.reflow { c("reflow", "text", false, reflow) }
        c("goto", "text")                   // jump to a page (PDF) / position (EPUB)
        c("theme", "text")                  // a direct cycle button (not in the ⋯), same as web
        c("bookmarkAdd", "text", true)
        c("bookmarkList", "text", true)
        if caps.annotate && pdf { c("highlight", "text", true); c("draw", "text", true, draw) }
        if caps.annotateText && pdf {
            c("underline", "text", true); c("strike", "text", true)
            c("note", "text", true); c("erase", "text", true)
        }
        if caps.annotate && pdf { c("annList", "text", true) }
        if caps.export && pdf { c("export", "text", true) }
    }
    return out
}
