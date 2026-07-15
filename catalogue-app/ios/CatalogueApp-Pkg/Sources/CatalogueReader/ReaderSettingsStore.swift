import Foundation

/// Per-document reading settings — the layout/fit choices that genuinely differ from book to book
/// (a large-print novel vs a dense scanned PDF), so they are remembered PER book rather than globally.
/// Persisted to a JSON file keyed by `publicationId` ("holding:<id>") — the same key
/// `CatalogueReadingStore` uses for the reading position. Reading THEME stays a global preference
/// (see `ReaderView`'s `@AppStorage`), not here. Device-local, not synced.
public struct ReaderSettings: Codable, Equatable, Sendable {
    /// EPUB font size as an epub.js percent (e.g. 120). `nil` → the engine's default (100%).
    public var epubFontPct: Int?
    /// PDFKit `scaleFactor` the book was left at. `nil` → fit-to-width on open.
    public var pdfScale: Double?
    /// PDF reflow-to-text font size, in points. `nil` → the default (18pt).
    public var reflowFontPt: Double?

    public init(epubFontPct: Int? = nil, pdfScale: Double? = nil, reflowFontPt: Double? = nil) {
        self.epubFontPct = epubFontPct
        self.pdfScale = pdfScale
        self.reflowFontPt = reflowFontPt
    }
}

/// A tiny per-publication settings file store, modelled on `CatalogueReadingStore` (same actor +
/// JSON-in-Application-Support pattern, same `publicationId` key). Reads restore on open; writes are a
/// field-wise merge so setting one control (say PDF zoom) never clobbers another (say EPUB font).
public actor ReaderSettingsStore {
    private let fileURL: URL
    private var entries: [String: ReaderSettings]?

    public init(directory: URL? = nil) {
        let dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("catalogue-app", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        self.fileURL = dir.appendingPathComponent("reader-settings.json")
    }

    private func loaded() -> [String: ReaderSettings] {
        if let entries { return entries }
        let e = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode([String: ReaderSettings].self, from: $0) } ?? [:]
        entries = e
        return e
    }

    private func persist(_ e: [String: ReaderSettings]) {
        entries = e
        if let data = try? JSONEncoder().encode(e) { try? data.write(to: fileURL, options: .atomic) }
    }

    /// The saved settings for a publication (an all-`nil` `ReaderSettings` if none saved yet).
    public func get(_ publicationId: String) -> ReaderSettings {
        loaded()[publicationId] ?? ReaderSettings()
    }

    /// Field-wise merge: only the non-`nil` fields of `change` overwrite; `nil` leaves the stored
    /// value untouched. So `update(pub, ReaderSettings(pdfScale: 1.5))` sets zoom alone.
    public func update(_ publicationId: String, _ change: ReaderSettings) {
        var e = loaded()
        var s = e[publicationId] ?? ReaderSettings()
        if let v = change.epubFontPct { s.epubFontPct = v }
        if let v = change.pdfScale { s.pdfScale = v }
        if let v = change.reflowFontPt { s.reflowFontPt = v }
        e[publicationId] = s
        persist(e)
    }
}
