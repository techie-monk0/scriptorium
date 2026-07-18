import XCTest
@testable import CatalogueReader
import CatalogueReaderWire
import Octavo

/// Unit tests for the pure outline conversions the editor seeds from (nested embedded TOC → flat
/// authored entries; display picks authored-over-embedded).
final class OutlineModelTests: XCTestCase {
    private func toc(_ title: String, page: Int, _ children: [TocItem] = []) -> TocItem {
        TocItem(title: title,
                locator: Locator(publicationId: "holding:7", format: .pdf, locations: .init(page: page)),
                children: children)
    }

    func testFlattenNestedTocToLeveledEntries() {
        let items = [toc("Part I", page: 1, [toc("Chapter 1", page: 2), toc("Chapter 2", page: 8)]),
                     toc("Part II", page: 20)]
        XCTAssertEqual(OutlineModel.entries(from: items),
                       [OutlineEntry(level: 1, title: "Part I", page: 1),
                        OutlineEntry(level: 2, title: "Chapter 1", page: 2),
                        OutlineEntry(level: 2, title: "Chapter 2", page: 8),
                        OutlineEntry(level: 1, title: "Part II", page: 20)])
    }

    func testEntriesWithoutAPageAreSkipped() {
        let noPage = TocItem(title: "EPUB-ish",
                             locator: Locator(publicationId: "holding:7", format: .epub,
                                              locations: .init(cfi: "epubcfi(/6/4!/4)")))
        XCTAssertEqual(OutlineModel.entries(from: [noPage]), [])
    }

    func testDisplayPrefersAuthoredOverEmbedded() {
        let embedded = [toc("Embedded", page: 1)]
        let authored = [OutlineEntry(level: 1, title: "Mine", page: 3)]
        XCTAssertEqual(OutlineModel.display(authored: authored, embedded: embedded), authored)
        XCTAssertEqual(OutlineModel.display(authored: [], embedded: embedded),
                       [OutlineEntry(level: 1, title: "Embedded", page: 1)])
    }

    func testDisplaySortsByPageAndIsStableOnTies() {
        // authored out of page order → shown in page order
        let authored = [OutlineEntry(level: 1, title: "Three", page: 3),
                        OutlineEntry(level: 1, title: "One", page: 1),
                        OutlineEntry(level: 1, title: "TwoB", page: 2),   // ties on page 2 keep this order…
                        OutlineEntry(level: 2, title: "TwoA", page: 2)]
        XCTAssertEqual(OutlineModel.display(authored: authored, embedded: []).map(\.title),
                       ["One", "TwoB", "TwoA", "Three"])
    }
}
