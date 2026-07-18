import Foundation
import Octavo

/// The catalogue-app's concrete `octavo` **`ReaderSettingsStore`** — the sibling of
/// `CatalogueReadingStore`, persisting the per-document reading settings (font size / zoom) to a JSON
/// file keyed by `publicationId` ("holding:<id>"). octavo's `Octavo.open(settingsStore:)` restores
/// these on open and auto-persists every change (font A±, PDF zoom, incl. pinch) through the navigator's
/// `apply` / `onSettingsChanged` — so the reader no longer hand-rolls save/restore (which is what
/// silently dropped PDF magnification).
///
/// Two deliberate scoping choices:
/// - **Theme stays global.** Reading theme is an app-wide `@AppStorage` preference, not per book, so
///   `setSettings` strips `theme` before persisting and never restores it — otherwise the SDK's
///   per-document theme would fight the global one. (Everything else octavo models — font, spacing,
///   margins, columns, PDF fit/crop, warmth, brightness, orientation, highlight colour — is per book.)
/// - **`reflowFontPt` / `reflowMode` are app-only.** PDF reflow-to-text isn't an octavo engine setting,
///   so it is persisted here via dedicated methods, alongside the octavo blob in the same file.
public actor CatalogueReaderSettingsStore: ReaderSettingsStore {
    private struct Entry: Codable {
        var settings: ReaderSettings
        var reflowFontPt: Double? = nil
        var reflowMode: Bool? = nil
    }
    private let fileURL: URL
    private var entries: [String: Entry]?

    public init(directory: URL? = nil) {
        let dir = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("catalogue-app", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        self.fileURL = dir.appendingPathComponent("reader-settings.json")
    }

    private func loaded() -> [String: Entry] {
        if let entries { return entries }
        let e = (try? Data(contentsOf: fileURL))
            .flatMap { try? JSONDecoder().decode([String: Entry].self, from: $0) } ?? [:]
        entries = e
        return e
    }

    private func persist(_ e: [String: Entry]) {
        entries = e
        if let data = try? JSONEncoder().encode(e) { try? data.write(to: fileURL, options: .atomic) }
    }

    // MARK: ReaderSettingsStore (octavo port) — font size / zoom, per document

    public func getSettings(_ publicationId: String) async throws -> ReaderSettings? {
        loaded()[publicationId]?.settings
    }

    public func setSettings(_ publicationId: String, _ settings: ReaderSettings) async throws {
        var e = loaded()
        var entry = e[publicationId] ?? Entry(settings: .defaults)
        // Theme is a global app preference — keep it out of the per-document blob so the SDK's
        // per-document restore can't override the global reading theme.
        var s = settings
        s.theme = nil
        entry.settings = s
        e[publicationId] = entry
        persist(e)
    }

    // MARK: App extras — PDF reflow-to-text (mode + font size); not octavo engine settings

    public func reflowFontPt(_ publicationId: String) -> Double? {
        loaded()[publicationId]?.reflowFontPt
    }

    public func setReflowFontPt(_ publicationId: String, _ pt: Double) {
        mutateEntry(publicationId) { $0.reflowFontPt = pt }
    }

    public func reflowMode(_ publicationId: String) -> Bool {
        loaded()[publicationId]?.reflowMode ?? false
    }

    public func setReflowMode(_ publicationId: String, _ on: Bool) {
        mutateEntry(publicationId) { $0.reflowMode = on }
    }

    private func mutateEntry(_ publicationId: String, _ mutate: (inout Entry) -> Void) {
        var e = loaded()
        var entry = e[publicationId] ?? Entry(settings: .defaults)
        mutate(&entry)
        e[publicationId] = entry
        persist(e)
    }
}
