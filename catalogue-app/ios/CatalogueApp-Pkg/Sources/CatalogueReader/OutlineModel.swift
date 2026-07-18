import Foundation
import Octavo
import CatalogueReaderWire

/// Pure conversions for authored outlines — the testable core of the editor, kept out of the SwiftUI
/// view. Flattens octavo's nested `TocItem` tree (read from the PDF's own outline) into the flat
/// `[OutlineEntry]` the editor edits and the sync stores, so the editor can seed from the file's
/// embedded TOC. PDF pages only: an entry with no page is skipped (the authored outline is page-anchored).
public enum OutlineModel {
    /// Flatten a nested `TocItem` tree to `level`/`title`/`page` entries, depth → level (1-based).
    public static func entries(from items: [TocItem], level: Int = 1) -> [OutlineEntry] {
        var out: [OutlineEntry] = []
        for it in items {
            if let page = it.locator.locations.page {
                out.append(OutlineEntry(level: level, title: it.title, page: page))
            }
            out.append(contentsOf: entries(from: it.children, level: level + 1))
        }
        return out
    }

    /// What the TOC/editor should show, in **page order** (the default): the authored outline if the
    /// user has one, else the file's embedded outline (flattened). The sort is stable — entries on the
    /// same page keep their original (authoring / document) order, so a normal page-monotonic TOC is
    /// unchanged and hierarchy isn't shuffled.
    public static func display(authored: [OutlineEntry], embedded: [TocItem]) -> [OutlineEntry] {
        let base = authored.isEmpty ? entries(from: embedded) : authored
        return base.enumerated()
            .sorted { ($0.element.page, $0.offset) < ($1.element.page, $1.offset) }
            .map(\.element)
    }
}
