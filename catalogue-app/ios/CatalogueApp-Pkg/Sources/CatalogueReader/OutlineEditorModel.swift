import Foundation
import Observation
import Octavo
import CatalogueReaderWire

/// The testable core of the authored-outline editor, lifted out of the SwiftUI sheet so its behavior can
/// be exercised headlessly across scenarios. It owns the working draft and, on every operation, emits a
/// **structured console line** through an injectable `trace` sink (default `print`). Those are the exact
/// lines a developer sees in the console AND a test asserts — so the editor's decisions (which source
/// seeds the draft, what gets dropped on save, how a bake outcome is classified) are pinned even though
/// the SwiftUI rendering itself is only observable in the simulator.
///
/// Line format (stable — tests match on it): `outline.<event> key=value …`.
@Observable
public final class OutlineEditorModel {
    public private(set) var entries: [OutlineEntry] = []
    @ObservationIgnored private let trace: (String) -> Void

    public init(trace: @escaping (String) -> Void = { print($0) }) { self.trace = trace }

    /// Seed the draft: the authored outline if the user has one, else the file's embedded TOC (flattened),
    /// else empty. Emits which source won.
    public func open(authored: [OutlineEntry], embedded: [TocItem]) {
        entries = OutlineModel.display(authored: authored, embedded: embedded)
        let source = !authored.isEmpty ? "authored" : (embedded.isEmpty ? "empty" : "embedded")
        trace("outline.open source=\(source) authored=\(authored.count) embedded=\(embedded.count) draft=\(entries.count)")
    }

    public func add(atPage page: Int) {
        entries.append(OutlineEntry(level: 1, title: "", page: max(1, page)))
        trace("outline.add page=\(max(1, page)) total=\(entries.count)")
    }

    public func delete(at offsets: IndexSet) {
        entries.remove(atOffsets: offsets)
        trace("outline.delete total=\(entries.count)")
    }

    public func move(from source: IndexSet, to destination: Int) {
        entries.move(fromOffsets: source, toOffset: destination)
        trace("outline.move total=\(entries.count)")
    }

    public func setTitle(_ title: String, at i: Int) {
        guard entries.indices.contains(i) else { return }
        entries[i].title = title
    }

    public func setPage(_ page: Int, at i: Int) {
        guard entries.indices.contains(i) else { return }
        entries[i].page = max(1, page)
    }

    /// The entries to persist — blank-title rows dropped. Emits the count saved vs dropped.
    public func cleaned() -> [OutlineEntry] {
        let kept = entries.filter { !$0.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        trace("outline.save entries=\(kept.count) dropped=\(entries.count - kept.count)")
        return kept
    }

    /// Classify + emit a bake (Save-into-PDF) result. Returns the user-facing outcome so the view can
    /// message it, and traces the same classification for tests/console.
    @discardableResult
    public func bakeResult(status: Int, bytes: Int) -> String {
        let outcome: String
        switch status {
        case 200: outcome = "ok"
        case 409: outcome = "empty"
        case 403: outcome = "forbidden"
        default:  outcome = "error"
        }
        trace("outline.bake status=\(status) bytes=\(bytes) outcome=\(outcome)")
        return outcome
    }
}
