import Foundation
import Octavo

#if canImport(PDFKit)
import PDFKit

#if canImport(UIKit)
import UIKit
public typealias OctavoView = UIView
#elseif canImport(AppKit)
import AppKit
public typealias OctavoView = NSView
#endif

/// `Navigator` over PDFKit. Maps PDF pages ⇄ `Locator`, searches via
/// `PDFSelection`, and reads the TOC from `outlineRoot`. Works on iOS and macOS
/// (PDFKit ships on both).
///
/// Locator convention: `locations.page` is the 1-based human page number;
/// `locations.position` is the 0-based PDFKit page index; `progression` is
/// `pageIndex / pageCount`.
@MainActor
public final class PdfKitNavigator: Navigator {

    public let document: PDFDocument
    public let publicationId: String
    /// The PDFKit host view (its `document` is set on `open`).
    public let pdfView: PDFView

    public private(set) var currentLocation: Locator?
    public var onLocationChanged: (@MainActor (Locator) -> Void)?

    /// Total pages — for a "Go to page 1…N" control.
    public var pageCount: Int { document.pageCount }

    public init(
        document: PDFDocument,
        publicationId: String,
        host: PDFView? = nil
    ) {
        self.document = document
        self.publicationId = publicationId
        self.pdfView = host ?? PDFView()
    }

    /// Convenience init from raw bytes (e.g. fetched via a `Source`).
    public convenience init?(
        data: Data,
        publicationId: String,
        host: PDFView? = nil
    ) {
        guard let doc = PDFDocument(data: data) else { return nil }
        self.init(document: doc, publicationId: publicationId, host: host)
    }

    /// Convenience init from a local file URL.
    public convenience init?(
        url: URL,
        publicationId: String,
        host: PDFView? = nil
    ) {
        guard let doc = PDFDocument(url: url) else { return nil }
        self.init(document: doc, publicationId: publicationId, host: host)
    }

    // MARK: Navigator

    public func open() async throws {
        pdfView.document = document
        if let first = document.page(at: 0) {
            emit(locator(for: first))
        }
    }

    public func goTo(_ locator: Locator) async throws {
        guard let page = resolvePage(for: locator) else {
            throw PdfKitNavigatorError.pageOutOfRange
        }
        pdfView.go(to: page)
        emit(self.locator(for: page, carrying: locator.text))
    }

    public func next() async throws {
        guard let current = currentPage,
              let idx = index(of: current),
              idx + 1 < document.pageCount,
              let page = document.page(at: idx + 1) else { return }
        pdfView.go(to: page)
        emit(locator(for: page))
    }

    public func prev() async throws {
        guard let current = currentPage,
              let idx = index(of: current),
              idx > 0,
              let page = document.page(at: idx - 1) else { return }
        pdfView.go(to: page)
        emit(locator(for: page))
    }

    /// Zoom in / out around the current scale (Preview-style). Setting `scaleFactor` opts out of
    /// `autoScales`, so the user's zoom sticks instead of snapping back to fit-width on relayout.
    public func bigger() async {
        pdfView.autoScales = false
        pdfView.scaleFactor = min(pdfView.scaleFactor * 1.2, pdfView.maxScaleFactor)
    }

    public func smaller() async {
        pdfView.autoScales = false
        pdfView.scaleFactor = max(pdfView.scaleFactor / 1.2, pdfView.minScaleFactor)
    }

    /// Apply a reading theme: tint the page gutter to the theme background, and for a dark theme invert
    /// the page content. iOS `CALayer.filters` is a no-op, so night-invert is a `differenceBlendMode`
    /// white overlay over the page view (inverts everything beneath it, not the app chrome).
    public func applyTheme(_ theme: ReaderTheme) async {
        let (r, g, b) = Self.rgb(theme.bg)
        #if canImport(UIKit)
        pdfView.backgroundColor = UIColor(red: r, green: g, blue: b, alpha: 1)
        setNightInvert(theme.isDark)
        #elseif canImport(AppKit)
        pdfView.backgroundColor = NSColor(red: r, green: g, blue: b, alpha: 1)
        #endif
    }

    private static func rgb(_ hex: String) -> (CGFloat, CGFloat, CGFloat) {
        var s = hex.trimmingCharacters(in: CharacterSet(charactersIn: "# "))
        if s.count == 3 { s = s.map { "\($0)\($0)" }.joined() }
        var v: UInt64 = 0; Scanner(string: s).scanHexInt64(&v)
        return (CGFloat((v >> 16) & 0xff) / 255, CGFloat((v >> 8) & 0xff) / 255, CGFloat(v & 0xff) / 255)
    }

    #if canImport(UIKit)
    private var nightOverlay: UIView?
    private func setNightInvert(_ on: Bool) {
        if on {
            guard nightOverlay == nil else { return }
            let v = UIView(frame: pdfView.bounds)
            v.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            v.isUserInteractionEnabled = false
            v.backgroundColor = .white
            v.layer.compositingFilter = "differenceBlendMode"   // white ⊖ content = inverted content
            pdfView.addSubview(v)
            nightOverlay = v
        } else {
            nightOverlay?.removeFromSuperview(); nightOverlay = nil
        }
    }
    #endif

    /// The extracted text of a 0-based page (PDFKit's embedded/OCR text layer), or nil when the page
    /// has none (a scanned PDF with no text layer). Powers the "reflow to text" reading mode.
    public func pageText(at pageIndex: Int) -> String? {
        guard pageIndex >= 0, pageIndex < document.pageCount,
              let text = document.page(at: pageIndex)?.string, !text.isEmpty else { return nil }
        return text
    }

    /// The extracted text of the page currently on screen (or the first page before one is shown).
    public func currentPageText() -> String? {
        pageText(at: currentPage.flatMap(index(of:)) ?? 0)
    }

    public func search(_ query: String) async throws -> [Locator] {
        guard !query.isEmpty else { return [] }
        let selections = document.findString(query, withOptions: .caseInsensitive)
        return selections.compactMap { selection in
            guard let page = selection.pages.first,
                  let idx = index(of: page) else { return nil }
            let text = Locator.Text(highlight: selection.string)
            return makeLocator(pageIndex: idx, text: text)
        }
    }

    public func outline() -> [TocItem] {
        guard let root = document.outlineRoot else { return [] }
        var items: [TocItem] = []
        for i in 0..<root.numberOfChildren {
            if let child = root.child(at: i) {
                items.append(tocItem(from: child))
            }
        }
        return items
    }

    // MARK: Page ⇄ Locator

    private var currentPage: PDFPage? { pdfView.currentPage ?? document.page(at: 0) }

    private func index(of page: PDFPage) -> Int? {
        let idx = document.index(for: page)
        return idx == NSNotFound ? nil : idx
    }

    private func resolvePage(for locator: Locator) -> PDFPage? {
        if let pos = locator.locations.position, pos >= 0, pos < document.pageCount {
            return document.page(at: pos)
        }
        if let human = locator.locations.page {
            let idx = human - 1
            if idx >= 0, idx < document.pageCount { return document.page(at: idx) }
        }
        if let prog = locator.locations.progression, document.pageCount > 0 {
            let idx = min(max(Int(prog * Double(document.pageCount)), 0),
                          document.pageCount - 1)
            return document.page(at: idx)
        }
        return nil
    }

    private func locator(for page: PDFPage, carrying text: Locator.Text? = nil) -> Locator {
        let idx = index(of: page) ?? 0
        return makeLocator(pageIndex: idx, text: text)
    }

    private func makeLocator(pageIndex: Int, text: Locator.Text?) -> Locator {
        let progression = document.pageCount > 0
            ? Double(pageIndex) / Double(document.pageCount)
            : 0
        return Locator(
            publicationId: publicationId,
            format: .pdf,
            locations: .init(
                page: pageIndex + 1,
                progression: progression,
                position: pageIndex
            ),
            text: text
        )
    }

    private func tocItem(from outline: PDFOutline) -> TocItem {
        let pageIndex = outline.destination?.page
            .flatMap { index(of: $0) } ?? 0
        var children: [TocItem] = []
        for i in 0..<outline.numberOfChildren {
            if let child = outline.child(at: i) {
                children.append(tocItem(from: child))
            }
        }
        return TocItem(
            title: outline.label ?? "",
            locator: makeLocator(pageIndex: pageIndex, text: nil),
            children: children
        )
    }

    private func emit(_ locator: Locator) {
        currentLocation = locator
        onLocationChanged?(locator)
    }
}

public enum PdfKitNavigatorError: Error, Equatable {
    case pageOutOfRange
    case undecodable
}

// MARK: - Façade convenience

public extension Octavo {
    /// Open a PDF reading session from raw bytes, wiring a `PdfKitNavigator`.
    @MainActor
    @discardableResult
    static func open(
        pdfData data: Data,
        publicationId: String,
        host: PDFView? = nil,
        readingStore: ReadingStore? = nil,
        capabilities: Capabilities = .init(),
        decorations: DecorationHost? = nil
    ) async throws -> Reader {
        guard let nav = PdfKitNavigator(
            data: data, publicationId: publicationId, host: host
        ) else {
            throw PdfKitNavigatorError.undecodable
        }
        return try await Octavo.open(
            navigator: nav,
            publicationId: publicationId,
            readingStore: readingStore,
            capabilities: capabilities,
            decorations: decorations
        )
    }
}

#endif // canImport(PDFKit)
