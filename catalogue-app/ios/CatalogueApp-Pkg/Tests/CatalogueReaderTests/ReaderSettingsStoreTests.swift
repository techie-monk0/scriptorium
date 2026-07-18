import XCTest
import Octavo
@testable import CatalogueReader

/// Per-document reading settings persist per publicationId through the octavo `ReaderSettingsStore`
/// port (font size / PDF zoom), keep theme/brightness OUT of the per-document blob (those are global),
/// carry the app-only reflow size, and survive a relaunch (reload from disk).
final class ReaderSettingsStoreTests: XCTestCase {
    private func tempDir() -> URL {
        let d = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
        return d
    }

    func testDefaultsToNil() async throws {
        let store = CatalogueReaderSettingsStore(directory: tempDir())
        let s = try await store.getSettings("holding:7")
        XCTAssertNil(s)
        let reflow = await store.reflowFontPt("holding:7")
        XCTAssertNil(reflow)
    }

    func testSetGetRoundTrip() async throws {
        let store = CatalogueReaderSettingsStore(directory: tempDir())
        try await store.setSettings("holding:7", ReaderSettings(fontPercent: 130))
        let s = try await store.getSettings("holding:7")
        XCTAssertEqual(s?.fontPercent, 130)
    }

    func testThemeIsStrippedButComfortPersistsPerDocument() async throws {
        let store = CatalogueReaderSettingsStore(directory: tempDir())
        try await store.setSettings("a", ReaderSettings(
            fontPercent: 120,
            scaleFactor: 1.5,
            theme: ReaderTheme(bg: "#000", fg: "#fff", isDark: true),
            brightness: 0.5,
            warmth: 0.4))
        let s = try await store.getSettings("a")
        XCTAssertEqual(s?.fontPercent, 120)   // per-document fields kept
        XCTAssertEqual(s?.scaleFactor, 1.5)
        XCTAssertNil(s?.theme)                 // theme is global — never persisted per document
        XCTAssertEqual(s?.brightness, 0.5)     // comfort settings ARE per book
        XCTAssertEqual(s?.warmth, 0.4)
    }

    func testNewPerDocumentFieldsPersist() async throws {
        let store = CatalogueReaderSettingsStore(directory: tempDir())
        try await store.setSettings("a", ReaderSettings(
            fontWeight: .bold, letterSpacing: 0.02, paragraphSpacing: 0.8, hyphenation: true,
            columnCount: .two, maxLineWidthCh: 66,
            pdfFitMode: .fitPage, pdfCropMargins: .auto,
            warmth: 0.4, contrast: 1.3, highlightColor: "#ffd54a", orientationLock: .portrait))
        let s = try await store.getSettings("a")
        XCTAssertEqual(s?.fontWeight, .bold)
        XCTAssertEqual(s?.letterSpacing, 0.02)
        XCTAssertEqual(s?.columnCount, .two)
        XCTAssertEqual(s?.pdfFitMode, .fitPage)
        XCTAssertEqual(s?.pdfCropMargins, .auto)
        XCTAssertEqual(s?.warmth, 0.4)
        XCTAssertEqual(s?.highlightColor, "#ffd54a")
        XCTAssertEqual(s?.orientationLock, .portrait)
    }

    func testReflowModeAndFontPtArePerDocumentAppExtras() async throws {
        let store = CatalogueReaderSettingsStore(directory: tempDir())
        await store.setReflowFontPt("a", 22)
        await store.setReflowMode("a", true)
        await store.setReflowFontPt("b", 14)
        let aPt = await store.reflowFontPt("a")
        let aMode = await store.reflowMode("a")
        let bPt = await store.reflowFontPt("b")
        let bMode = await store.reflowMode("b")
        XCTAssertEqual(aPt, 22)
        XCTAssertEqual(aMode, true)
        XCTAssertEqual(bPt, 14)
        XCTAssertEqual(bMode, false)   // default when unset
    }

    func testSettingsAndReflowCoexistInSameFile() async throws {
        let store = CatalogueReaderSettingsStore(directory: tempDir())
        try await store.setSettings("a", ReaderSettings(fontPercent: 120))
        await store.setReflowFontPt("a", 20)
        let s = try await store.getSettings("a")
        let reflow = await store.reflowFontPt("a")
        XCTAssertEqual(s?.fontPercent, 120)   // setting reflow didn't clobber the octavo blob
        XCTAssertEqual(reflow, 20)
    }

    func testPersistsAcrossInstances() async throws {
        let dir = tempDir()
        let store = CatalogueReaderSettingsStore(directory: dir)
        try await store.setSettings("holding:7", ReaderSettings(fontPercent: 140, scaleFactor: 2.0))
        await store.setReflowFontPt("holding:7", 20)
        // a fresh store over the same directory restores the saved settings (relaunch)
        let reopened = CatalogueReaderSettingsStore(directory: dir)
        let s = try await reopened.getSettings("holding:7")
        let reflow = await reopened.reflowFontPt("holding:7")
        XCTAssertEqual(s?.fontPercent, 140)
        XCTAssertEqual(s?.scaleFactor, 2.0)
        XCTAssertEqual(reflow, 20)
    }
}
