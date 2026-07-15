import XCTest
@testable import CatalogueReader

/// Per-document reading settings (font size / PDF zoom / reflow size) persist per publicationId,
/// merge field-wise (one control doesn't clobber another), and survive a relaunch (reload from disk).
final class ReaderSettingsStoreTests: XCTestCase {
    private func tempDir() -> URL {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    func testDefaultsToAllNil() async throws {
        let store = ReaderSettingsStore(directory: tempDir())
        let s = await store.get("holding:7")
        XCTAssertNil(s.epubFontPct)
        XCTAssertNil(s.pdfScale)
        XCTAssertNil(s.reflowFontPt)
    }

    func testSetGetRoundTrip() async throws {
        let store = ReaderSettingsStore(directory: tempDir())
        await store.update("holding:7", ReaderSettings(epubFontPct: 130))
        let s = await store.get("holding:7")
        XCTAssertEqual(s.epubFontPct, 130)
    }

    func testFieldWiseMergeDoesNotClobber() async throws {
        let store = ReaderSettingsStore(directory: tempDir())
        await store.update("a", ReaderSettings(epubFontPct: 120))
        await store.update("a", ReaderSettings(pdfScale: 1.5))       // set zoom alone
        await store.update("a", ReaderSettings(reflowFontPt: 22))    // set reflow alone
        let s = await store.get("a")
        XCTAssertEqual(s.epubFontPct, 120)   // still there
        XCTAssertEqual(s.pdfScale, 1.5)
        XCTAssertEqual(s.reflowFontPt, 22)
    }

    func testIsPerDocument() async throws {
        let store = ReaderSettingsStore(directory: tempDir())
        await store.update("a", ReaderSettings(epubFontPct: 90))
        await store.update("b", ReaderSettings(epubFontPct: 200))
        let a = await store.get("a"); let b = await store.get("b")
        XCTAssertEqual(a.epubFontPct, 90)
        XCTAssertEqual(b.epubFontPct, 200)
    }

    func testPersistsAcrossInstances() async throws {
        let dir = tempDir()
        let store = ReaderSettingsStore(directory: dir)
        await store.update("holding:7", ReaderSettings(epubFontPct: 140, pdfScale: 2.0, reflowFontPt: 20))
        // a fresh store over the same directory restores the saved settings (relaunch)
        let reopened = ReaderSettingsStore(directory: dir)
        let s = await reopened.get("holding:7")
        XCTAssertEqual(s.epubFontPct, 140)
        XCTAssertEqual(s.pdfScale, 2.0)
        XCTAssertEqual(s.reflowFontPt, 20)
    }
}
