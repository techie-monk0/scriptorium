#if canImport(UIKit)
import SwiftUI
import Combine
import PDFKit
import PencilKit
import WebKit
import Octavo
import OctavoPDFKit
import OctavoEPUB
import OctavoAdapters
import Postilla
import PostillaRender
import CatalogueCore
import CatalogueData
import CatalogueDesign
import CatalogueReaderWire

/// The in-app reader screen — a thin SwiftUI HOST of the octavo engine + the postilla annotation seam
/// (ios_native_plan.md §5). It resolves the holding's bytes (via `HoldingBytes`) and routes by format:
/// **PDF → `PdfKitNavigator`** (PDFView, ink + highlights), **EPUB → `EpubWebNavigator`** (epub.js in a
/// WKWebView, highlights via `EpubDecorationHost`). Both call `Octavo.open(...)` wiring the catalogue
/// `ReadingStore` (position restore/persist) and a `DecorationHost` so postilla marks render; marks
/// pull from the `AnnotationStore` (the structured sync-of-record).
@MainActor
public struct ReaderView: View {
    private let holding: Holding
    private let title: String
    private let endpoint: any ServerEndpoint
    private let readingStore: CatalogueReadingStore
    private let annotations: any AnnotationStore
    private let bookmarks: any BookmarkStore
    // A host-injected top-bar accessory (ports-and-adapters: the reader core stays ignorant of what it
    // is — the catalogue layer drops in the star toggle here, a test/preview injects nothing).
    private let topBarAccessory: AnyView

    @State private var pdfNavigator: PdfKitNavigator?
    @State private var epubNavigator: EpubWebNavigator?
    @State private var reader: Reader?
    @State private var decorationHost: (any DecorationHost)?   // retained (MarkOverlay holds it weakly)
    // The per-format annotation rendering layer (marks + ink). PDF and EPUB each get a
    // `CompositeRenderLayer` built from their own hosts + ink placement; a different engine is a
    // different `ReaderRenderLayer` conformer.
    @State private var renderLayer: (any ReaderRenderLayer)?
    @State private var marks: [Annotation] = []
    @State private var marksById: [UUID: Annotation] = [:]   // full mark set keyed by id (delta-merged)
    @State private var resumeLocator: Locator?               // a further position from another device
    @State private var resumePill: String?                   // "Resume · Page 42 (another device)"
    @State private var errorText: String?
    @State private var locLabel = ""          // "Page 12" / "38%" — updated on every relocation
    @State private var rev = 0
    @State private var drawMode = false
    // Portable tool/colour/width state, owned by the Postilla SDK (`InkToolController`) — no PencilKit
    // here. The palette is the SDK defaults + this client's CatalogueDesign colours (additive; see
    // `CatalogueInk`). Android reimplements the capture surface, not this logic.
    @State private var ink = InkToolController(palette: CatalogueInk.palette)
    // Portable palette layout/arrangement/moveability (Postilla SDK). Starts docked to the bottom; the
    // move handle drags it to any edge or free-floating. This view only renders the spec.
    @State private var palette = InkPaletteController()
    @State private var showNoteEntry = false
    @State private var noteDraft = ""
    @State private var noteAnchor: (page: Int, point: [Double])?   // page index (0-based) + top-left point
    @State private var epubSelection: (cfiRange: String, text: String)?   // EPUB: last text selection
    @State private var shareItem: ShareItem?          // the annotated PDF to share (Phase 6)
    @State private var exportMessage: String?         // brief export failure notice
    @Binding private var showChrome: Bool     // Books-style: center-tap toggles the bars; owned by ReaderShell
    @AppStorage("readerTheme") private var readerThemeRaw = "auto"   // "auto" follows the device theme
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.openURL) private var openURL
    @State private var showToc = false
    @State private var tocItems: [TocItem] = []
    @State private var showSearch = false
    @State private var searchQuery = ""
    @State private var searchResults: [Locator] = []
    @State private var reflowMode = false                     // PDF: read the page's text as paragraphs
    @State private var reflowParagraphs: [String] = []
    @State private var showBookmarks = false
    @State private var bookmarkItems: [Bookmark] = []
    @State private var showClearBookmarks = false
    @State private var themeToast: String?          // brief "Sepia"/"Night"… flash on theme cycle
    @State private var themeToastToken = UUID()
    @State private var backStack: [Locator] = []    // jump origins (pushed on goTo, NOT on page turns)
    @State private var backPill: String?            // "Back to Page 12" — Apple-Books-style persistent pill
    @State private var showGoto = false
    @State private var gotoPage = 1                 // PDF: target page
    @State private var gotoFraction = 0.0           // EPUB: target position (0…1)
    @AppStorage("readerReflowFontPt") private var reflowFontPt = 18.0
    @Environment(\.dismiss) private var dismiss

    /// `annotations` defaults to **`ReaderSync`** over the endpoint (marks persist + sync via
    /// `/sync/reader`); inject an `InMemoryAnnotationStore` in tests/previews for a local-only store.
    public init(holding: Holding, title: String, endpoint: any ServerEndpoint, readingStore: CatalogueReadingStore,
                showChrome: Binding<Bool> = .constant(true),
                annotations: (any AnnotationStore)? = nil, bookmarks: (any BookmarkStore)? = nil,
                topBarAccessory: AnyView = AnyView(EmptyView())) {
        self.holding = holding; self.title = title; self.endpoint = endpoint
        self.readingStore = readingStore
        self._showChrome = showChrome
        // Local-first: marks persist to a device file (survive relaunch even offline) AND mirror to the
        // server via ReaderSync, with an outbox that flushes offline marks on reconnect.
        self.annotations = annotations ?? LocalAnnotationStore(
            remote: ReaderSync(baseURL: endpoint.baseURL, authorize: { endpoint.authorize(&$0) }))
        // Local-first: bookmarks persist to a device file (survive reopens even offline) AND mirror to
        // the server via BookmarkSync — position does the same, which is why it survived when bookmarks
        // (server-only, before this) did not.
        self.bookmarks = bookmarks ?? LocalBookmarkStore(remote: BookmarkSync(baseURL: endpoint.baseURL,
                                                                              authorize: { endpoint.authorize(&$0) }))
        self.topBarAccessory = topBarAccessory
    }

    private var pubId: String { "holding:\(holding.holdingId)" }
    private var currentLocation: Locator? { pdfNavigator?.currentLocation ?? epubNavigator?.currentLocation }
    /// Mirrors the reading position to `/holding/<id>/position` so another device can offer to resume.
    private var positionSync: PositionSync {
        PositionSync(baseURL: endpoint.baseURL, authorize: { endpoint.authorize(&$0) })
    }

    public var body: some View {
        NavigationStack {
            Group {
                if let pdfNavigator {
                    if reflowMode {
                        ReflowTextView(paragraphs: reflowParagraphs,
                                       bg: Color(hex: readingHex(.readerBg)),
                                       fg: Color(hex: readingHex(.readerFg)),
                                       fontSize: reflowFontPt, pageLabel: locLabel,
                                       onPrev: { goPrev() }, onNext: { goNext() })
                    } else {
                        ZStack {
                            PDFViewContainer(pdfView: pdfNavigator.pdfView).ignoresSafeArea(edges: .bottom)
                            if drawMode {
                                // PencilKit captures; we render via FreehandRenderer/PdfInkHost (canonical ink).
                                PencilKitInkCanvas(pdfView: pdfNavigator.pdfView, color: ink.color, width: ink.width,
                                                   mode: ink.mode ?? .draw,
                                                   onStroke: { stroke in Task { await addInk(stroke) } })
                                    .ignoresSafeArea(edges: .bottom)
                            }
                        }
                        // QuickLook/Preview-style: a single tap toggles the bars (PDF has no page-turn
                        // tap-zones like EPUB, so without this the chrome could get stuck hidden). Not
                        // while drawing — PencilKit owns taps then. TapGesture ignores scroll/pan.
                        .simultaneousGesture(TapGesture().onEnded {
                            if !drawMode { withAnimation(.easeInOut(duration: 0.2)) { showChrome.toggle() } }
                        })
                    }
                } else if let epubNavigator {
                    // ALL EPUB touch handling — link nav, swipe-to-page, left/right tap-zones, and
                    // centre-tap to toggle the bars — lives in the content (epub-bridge). A SwiftUI
                    // gesture over the WKWebView competes for the same touches and suppresses the
                    // in-iframe ones, so we deliberately attach none here.
                    ZStack {
                        WebViewContainer(webView: epubNavigator.webView)
                            .ignoresSafeArea(edges: .bottom)
                        if drawMode {
                            // Ink capture over the book; strokes anchor to the block under them (CFI).
                            EpubInkCanvas(color: ink.color, width: ink.width,
                                          onStroke: { stroke, start in Task { await addEpubInk(stroke, startAt: start) } })
                                .ignoresSafeArea(edges: .bottom)
                        }
                    }
                        .simultaneousGesture(pageSwipe)     // native swipe = paging
                        // Native tap = toggle the bars — the SAME proven mechanism PDF uses (a captured
                        // JS→binding toggle didn't fire). Simultaneous, so it doesn't block the web view;
                        // in-content JS still handles links.
                        .simultaneousGesture(TapGesture().onEnded {
                            withAnimation(.easeInOut(duration: 0.2)) { showChrome.toggle() }
                        })
                } else if let errorText {
                    ContentUnavailableView("Couldn’t open", systemImage: "exclamationmark.triangle", description: Text(errorText))
                } else {
                    ProgressView("Opening…")
                }
            }
            .overlay(alignment: .bottom) {
                if reader != nil && showChrome && !locLabel.isEmpty && !reflowMode && !drawMode {
                    locationBadge.allowsHitTesting(false)   // read-only page/percent, never blocks scroll
                }
            }
            .overlay {
                // The pencil tool palette — only while drawing on a PDF. A thin renderer of the portable
                // `InkToolController` + `InkPaletteController`; the move handle repositions it (any edge
                // or floating), Done exits draw mode.
                if drawMode && (pdfNavigator != nil || epubNavigator != nil) {
                    GeometryReader { geo in positionedPalette(in: geo.size) }
                        .coordinateSpace(.named(InkToolbar.coordinateSpace))
                        .transition(.opacity)
                }
            }
            .overlay {
                if let themeToast {
                    Text(themeToast)
                        .font(.headline)
                        .padding(.horizontal, 22).padding(.vertical, 12)
                        .background(.ultraThinMaterial, in: Capsule())
                        .transition(.opacity)
                        .allowsHitTesting(false)
                }
            }
            .overlay(alignment: .bottom) {
                // Apple-Books-style transient "Back to …" pill: appears after a JUMP (TOC/search/link/
                // bookmark/go-to), tap to return to where you were. Auto-hides; page turns never show it.
                if let backPill {
                    HStack(spacing: 10) {
                        Button { goBack() } label: {
                            Label(backPill, systemImage: "arrow.uturn.backward").font(.footnote.weight(.medium))
                        }.buttonStyle(.plain)
                        Divider().frame(height: 16)
                        Button { dismissBackPill() } label: { Image(systemName: "xmark").font(.caption2) }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(.ultraThinMaterial, in: Capsule())
                    .shadow(radius: 4, y: 2)
                    .padding(.bottom, 64)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .overlay(alignment: .top) {
                // Advisory cross-device resume: appears only when the server position (another device) is
                // ahead of where we opened. Tap to jump (records a back target); ✕ to ignore. Never auto-jumps.
                if let resumePill {
                    HStack(spacing: 10) {
                        Button { takeResume() } label: {
                            Label(resumePill, systemImage: "arrow.forward.to.line").font(.footnote.weight(.medium))
                        }.buttonStyle(.plain)
                        Divider().frame(height: 16)
                        Button { withAnimation { self.resumePill = nil } } label: { Image(systemName: "xmark").font(.caption2) }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(.ultraThinMaterial, in: Capsule())
                    .shadow(radius: 4, y: 2)
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .overlay(alignment: .top) {
                // EPUB text marks are selection-driven (the model web uses): selecting text surfaces a
                // small Highlight/Underline bar. epub.js has no native strikethrough/note, so those stay
                // PDF-only for now.
                if epubSelection != nil && epubNavigator != nil {
                    HStack(spacing: 16) {
                        Button { Task { await addEpubTextMark(.highlight) } } label: {
                            Label("Highlight", systemImage: "highlighter")
                        }
                        Button { Task { await addEpubTextMark(.underline) } } label: {
                            Label("Underline", systemImage: "underline")
                        }
                        Button { epubSelection = nil } label: { Image(systemName: "xmark") }
                            .foregroundStyle(.secondary)
                    }
                    .font(.footnote.weight(.medium))
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(.ultraThinMaterial, in: Capsule())
                    .shadow(radius: 4, y: 2)
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .sheet(isPresented: $showGoto) { gotoSheet }
            .toolbar(showChrome ? .visible : .hidden, for: .navigationBar)
            .navigationTitle("")                    // no title between the bars (the tab strip names the book)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                // The two bars render from the SHARED chrome spec (LibraryCore.readerChromeVM), the same
                // control set every surface renders — so iOS can't silently miss a capability. General
                // (leading) = navigation/find/identity; text (trailing) = size/reflow + a ⋯ overflow.
                // Each bar is ONE toolbar item holding an HStack — so SwiftUI never auto-collapses the
                // group into its own second ⋯ (a single tap on our ⋯ opens the menu). `.imageScale(.small)`
                // keeps the symbols compact on a phone.
                ToolbarItem(placement: .topBarLeading) {
                    HStack(spacing: 8) {
                        ForEach(chromeControls.filter { $0.bar == "general" }) { barControl($0) }
                    }
                    .imageScale(.small)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 8) {
                        ForEach(chromeControls.filter { $0.bar == "text" && !$0.overflow }) { barControl($0) }
                        // EPUB has no shared "draw" control (the chrome gates it to PDF); the EPUB ink
                        // toggle is a local iOS affordance so it needs no shared-spec/goldens change.
                        if epubNavigator != nil, reader?.capabilities.canAnnotate == true {
                            Button { toggleDraw() } label: {
                                Image(systemName: drawMode ? "pencil.tip.crop.circle.fill" : "pencil.tip.crop.circle")
                            }.accessibilityLabel("Draw")
                        }
                        let overflow = chromeControls.filter { $0.overflow }
                        if !overflow.isEmpty { overflowMenu(overflow) }
                    }
                    .imageScale(.small)
                }
            }
            .sheet(isPresented: $showToc) { tocSheet }
            .sheet(isPresented: $showSearch) { searchSheet }
            .sheet(isPresented: $showBookmarks) { bookmarksSheet }
            .alert("Add Note", isPresented: $showNoteEntry) {
                TextField("Note", text: $noteDraft)
                Button("Add") { Task { await commitNote() } }
                Button("Cancel", role: .cancel) {}
            } message: { Text("A note anchored to the selected text.") }
            .sheet(item: $shareItem) { item in ActivityView(items: [item.url]) }
            .alert("Couldn’t export", isPresented: Binding(get: { exportMessage != nil },
                                                           set: { if !$0 { exportMessage = nil } })) {
                Button("OK", role: .cancel) {}
            } message: { Text(exportMessage ?? "") }
            .onChange(of: readerThemeRaw) { Task { await applyReadingTheme() } }
            .onChange(of: colorScheme) { if readerThemeRaw == "auto" { Task { await applyReadingTheme() } } }
            .task { await open() }
            // Reader freshness (Shape-B delta sync): pick up marks another device added — on return to
            // foreground and via a light poll while the book stays open. Delta pull (`since: rev`),
            // merged in place, so the current page never moves.
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await pullMarks(reset: false) } }
                else { Task { await pushPosition() } }   // backgrounding → mirror position for other devices
            }
            .onReceive(Timer.publish(every: 45, on: .main, in: .common).autoconnect()) { _ in
                Task { await pullMarks(reset: false); await pushPosition() }
            }
            .onDisappear {
                epubNavigator?.tearDown()                 // break the WKWebView message-handler cycle
                Task { await pushPosition() }             // closing the book → mirror final position
            }
        }
    }

    private func open() async {
        showChrome = true   // always start a freshly-opened book with the bars visible
        guard holding.hasFile else { errorText = "This copy has no file."; return }
        let bytes = HoldingBytes(endpoint: endpoint)
        do {
            let url = try await bytes.fileURL(for: holding)
            if url.pathExtension.lowercased() == "epub" {
                try await openEpub(url: url)
            } else if let nav = PdfKitNavigator(url: url, publicationId: pubId) {
                try await openPdf(nav)
            } else {
                // Bytes downloaded but PDFKit couldn't decode them — most often a stale/poisoned cache
                // entry from a failed download. Evict so a reopen re-fetches, and say so plainly rather
                // than blaming the file.
                await bytes.evict(for: holding)
                errorText = "Couldn’t read this file as a PDF. It may have failed to download — reopen to retry."
                return
            }
            await pullMarks(reset: true)
            await checkResume()          // offer to jump to a further position from another device
            await applyReadingTheme()
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
        }
    }

    private func openPdf(_ nav: PdfKitNavigator) async throws {
        pdfNavigator = nav
        let host = PdfDecorationHost(pdfView: nav.pdfView)
        decorationHost = host
        // The PDF rendering layer: PDFKit decoration host + fixed-page ink host (default ink engine).
        renderLayer = CompositeRenderLayer(decorations: host,
                                           ink: PdfInkHost(pdfView: nav.pdfView),
                                           inkPlacement: .fixedPage)
        reader = try await Octavo.open(
            navigator: nav, publicationId: pubId, readingStore: readingStore,
            capabilities: .init(canAnnotate: true, canExport: true, canSearch: true),
            decorations: host)
        observeLocation()
    }

    private func openEpub(url: URL) async throws {
        let nav = EpubWebNavigator(source: FileSource(url: url), publicationId: pubId)
        nav.onTapZone = { zone in
            switch zone {
            case "prev": goPrev()
            case "next": goNext()
            default: withAnimation(.easeInOut(duration: 0.2)) { showChrome.toggle() }
            }
        }
        nav.onExternalLink = { linkURL in openURL(linkURL) }
        nav.onWillJump = { pushBackTarget() }   // in-content link → record a back target
        nav.onSelection = { cfi, text in epubSelection = (cfiRange: cfi, text: text) }
        epubNavigator = nav
        let host = EpubDecorationHost(navigator: nav)
        decorationHost = host
        // The EPUB rendering layer: epub.js decoration host + CFI-anchored ink overlay host.
        renderLayer = CompositeRenderLayer(decorations: host,
                                           ink: EpubInkHost(navigator: nav),
                                           inkPlacement: .inlineBox(aspect: 1))
        reader = try await Octavo.open(
            navigator: nav, publicationId: pubId, readingStore: readingStore,
            capabilities: .init(canAnnotate: true, canSearch: true),
            decorations: host)
        observeLocation()
    }

    /// Mirror the navigator's reading position into `locLabel` (and seed it with the current one). The
    /// closure chains after octavo's auto-persist, so this never clobbers position save.
    private func observeLocation() {
        reader?.onLocationChanged { loc in
            self.locLabel = Self.label(for: loc)
            if self.reflowMode { self.updateReflow() }   // page turned in reflow mode → re-extract
            // EPUB is paginated: re-place CFI-anchored marks/ink on the newly displayed page.
            if self.epubNavigator != nil { self.renderMarks() }
        }
        if let loc = reader?.currentLocation { locLabel = Self.label(for: loc) }
    }

    /// The active reading theme. "auto" follows the device: light → White, dark → Night.
    private var readingTheme: ReadingTheme {
        if readerThemeRaw == "auto" { return colorScheme == .dark ? .night : .white }
        return ReadingTheme(rawValue: readerThemeRaw) ?? .default
    }
    private func readingHex(_ t: ReadingToken) -> String { ReadingPalette.hex[readingTheme]?[t] ?? "#ffffff" }

    // MARK: PDF reflow-to-text (GoodReader style) — PDF-only; EPUB is already reflowable.

    private func toggleReflow() {
        reflowMode.toggle()
        if reflowMode { updateReflow() }
    }

    /// Re-extract the current PDF page's text and reflow it into paragraphs (shared Tier-2 `reflowPageText`).
    private func updateReflow() {
        reflowParagraphs = reflowPageText(pdfNavigator?.currentPageText() ?? "")
    }

    private func biggerText() {
        if reflowMode { reflowFontPt = min(reflowFontPt + 2, 32) } else { Task { await reader?.bigger() } }
    }
    private func smallerText() {
        if reflowMode { reflowFontPt = max(reflowFontPt - 2, 12) } else { Task { await reader?.smaller() } }
    }

    /// Resolve the persisted reading theme to concrete colours (composition root: `ReadingPalette` is
    /// named only here, never in octavo) and hand them to the engine.
    private func applyReadingTheme() async {
        let t = readingTheme
        let hex = ReadingPalette.hex[t] ?? [:]
        await reader?.applyTheme(ReaderTheme(bg: hex[.readerBg] ?? "#ffffff",
                                             fg: hex[.readerFg] ?? "#000000",
                                             isDark: t.isDark))
    }

    private static func label(for loc: Locator) -> String {
        if let page = loc.locations.page { return "Page \(page)" }
        if let prog = loc.locations.progression { return "\(Int((prog * 100).rounded()))%" }
        return ""
    }

    // MARK: Page turning (the shared reader contract's next/prev, wired to native input — the iOS
    // analogue of the web reader-core `attachSwipe` + prev/next buttons).

    private func goNext() { Task { try? await reader?.next() } }
    private func goPrev() { Task { try? await reader?.prev() } }

    /// EPUB paging is a NATIVE horizontal swipe (this worked reliably over the WKWebView; in-content JS
    /// handles only links + blank-tap-to-toggle, and ignores moved touches, so the two don't fight).
    private var pageSwipe: some Gesture {
        DragGesture(minimumDistance: 40)
            .onEnded { value in
                guard reader != nil,
                      abs(value.translation.width) > abs(value.translation.height),
                      abs(value.translation.width) > 60 else { return }
                if value.translation.width < 0 { goNext() } else { goPrev() }
            }
    }

    /// A small read-only page/percent pill (Books/Preview both show one). Shown only with the chrome.
    private var locationBadge: some View {
        Text(locLabel)
            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.bottom, 8)
    }

    // MARK: Reader chrome — rendered from the SHARED spec (LibraryCore.readerChromeVM)

    /// This surface's declared capabilities → the shared spec → the ordered control list for both bars.
    /// A capability iOS can't back yet (text-annotation, export) is passed `false`; the control stays in
    /// the spec and lights up here the moment iOS declares support.
    private var chromeControls: [ReaderControl] {
        let pdf = pdfNavigator != nil
        let caps = ReaderCaps(
            ready: reader != nil,
            search: reader?.capabilities.canSearch == true,
            star: true,
            annotate: (reader?.capabilities.canAnnotate == true) && pdf,
            annotateText: (reader?.capabilities.canAnnotate == true) && pdf,   // selection-anchored marks
            export: (reader?.capabilities.canExport == true) && pdf,          // share annotated.pdf
            reflow: pdf
        )
        return readerChromeVM(format: pdf ? "pdf" : "epub", caps: caps, reflow: reflowMode, draw: drawMode)
    }

    /// A bar (leading/trailing) control, dispatched by its shared `id` to a native SwiftUI subcomponent.
    @ViewBuilder private func barControl(_ c: ReaderControl) -> some View {
        switch c.id {
        case "done":
            Button { dismiss() } label: { Image(systemName: "chevron.left") }.accessibilityLabel("Done")
        case "toc":
            Button { tocItems = reader?.outline() ?? []; showToc = true } label: {
                Image(systemName: "list.bullet")
            }.accessibilityLabel("Contents")
        case "search":
            Button { showSearch = true } label: { Image(systemName: "magnifyingglass") }.accessibilityLabel("Search")
        case "star":
            topBarAccessory
        case "textSmaller":
            Button { smallerText() } label: { Text("A").font(.footnote) }.accessibilityLabel("Smaller text")
        case "textLarger":
            Button { biggerText() } label: { Text("A").font(.title3) }.accessibilityLabel("Larger text")
        case "reflow":
            Button { toggleReflow() } label: {
                Image(systemName: c.active ? "doc.richtext.fill" : "doc.plaintext")
            }.accessibilityLabel("Reflow to text")
        case "goto":
            Button { prepareGoto(); showGoto = true } label: { Image(systemName: "arrow.forward.to.line") }
                .accessibilityLabel("Go to")
        case "theme":
            Button { cycleTheme() } label: { Image(systemName: "circle.lefthalf.filled") }
                .accessibilityLabel("Reading theme")
        default:
            EmptyView()
        }
    }

    // Cycle Auto → White → Sepia → Gray → Night on tap; flash the chosen name briefly, then fade.
    private static let themeCycle = ["auto", "white", "sepia", "gray", "night"]
    private func cycleTheme() {
        let idx = Self.themeCycle.firstIndex(of: readerThemeRaw) ?? 0
        readerThemeRaw = Self.themeCycle[(idx + 1) % Self.themeCycle.count]
        Task { await applyReadingTheme() }
        let token = UUID(); themeToastToken = token
        withAnimation { themeToast = readerThemeRaw.capitalized }
        Task {
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            if themeToastToken == token { withAnimation { themeToast = nil } }
        }
    }

    /// The ⋯ overflow: the spec's overflow controls as menu rows (+ the pen palette while drawing).
    private func overflowMenu(_ controls: [ReaderControl]) -> some View {
        Menu {
            ForEach(controls) { menuControl($0) }
            // Pen colour / width / eraser now live in the on-screen `InkToolbar` while drawing.
        } label: { Image(systemName: "ellipsis.circle") }
    }

    @ViewBuilder private func menuControl(_ c: ReaderControl) -> some View {
        switch c.id {
        case "bookmarkAdd":
            Button { Task { await addBookmark() } } label: { Label("Add Bookmark", systemImage: "bookmark") }
        case "bookmarkList":
            Button { Task { await openBookmarkList() } } label: { Label("Bookmarks", systemImage: "bookmark.circle") }
        case "highlight":
            Button { Task { await addHighlight() } } label: { Label("Highlight", systemImage: "highlighter") }
        case "underline":
            Button { Task { await addTextMark(.underline) } } label: { Label("Underline", systemImage: "underline") }
        case "strike":
            Button { Task { await addTextMark(.strikeout) } } label: { Label("Strikethrough", systemImage: "strikethrough") }
        case "note":
            Button { beginNote() } label: { Label("Note", systemImage: "note.text") }
        case "erase":
            Button { Task { await eraseTextMarks() } } label: { Label("Erase Marks", systemImage: "eraser.line.dashed") }
        case "export":
            Button { Task { await exportAnnotatedPdf() } } label: { Label("Share Annotated PDF", systemImage: "square.and.arrow.up") }
        case "draw":
            Button { toggleDraw() } label: {
                Label(c.active ? "Stop Drawing" : "Draw", systemImage: "pencil.tip.crop.circle")
            }
        default:
            EmptyView()
        }
    }

    // MARK: Navigation history — "back to where I was reading" (Option A: record a back target on every
    // JUMP, never on page turns, so it's the jump ORIGIN — not the previous page).

    /// Jump to a locator (TOC entry / search hit / bookmark / go-to) — records the current spot as a
    /// back target first, so the transient pill can return you.
    private func jump(to locator: Locator) {
        performJump { try? await reader?.goTo(locator) }
    }

    /// Run a jump, first pushing the current location as a back target (and flashing the pill).
    private func performJump(_ go: @escaping () async -> Void) {
        pushBackTarget()
        Task { await go() }
    }

    private func pushBackTarget() {
        guard let cur = reader?.currentLocation else { return }
        backStack.append(cur)
        refreshBackPill()
    }

    /// The pill reflects the top of the back stack and PERSISTS (no timer — Apple Books / Kindle keep
    /// the "Back" affordance up until you use it, jump again, or dismiss it; a timer hides it before
    /// you've even read the page).
    private func refreshBackPill() {
        withAnimation { backPill = backStack.last.map { "Back to " + Self.label(for: $0) } }
    }

    /// Tap the pill → return to the last jump origin. Uses `goTo` DIRECTLY (not `jump`) so it doesn't
    /// record itself as a new back target. Then the pill retargets the next origin (or hides).
    private func goBack() {
        guard let target = backStack.popLast() else { return }
        Task { try? await reader?.goTo(target) }
        refreshBackPill()
    }

    /// The pill's ✕ — drop the current back target without navigating.
    private func dismissBackPill() {
        _ = backStack.popLast()
        refreshBackPill()
    }

    // MARK: Go to page / position

    private func prepareGoto() {
        if let pdf = pdfNavigator {
            gotoPage = pdf.currentLocation?.locations.page ?? 1
            _ = pdf   // (pageCount read in the sheet)
        } else {
            gotoFraction = reader?.currentLocation?.locations.progression ?? 0
        }
    }

    private func doGoto() {
        if pdfNavigator != nil {
            let page = gotoPage
            performJump { try? await reader?.goTo(Locator(publicationId: pubId, format: .pdf,
                                                          locations: .init(page: page))) }
        } else if let epub = epubNavigator {
            let f = gotoFraction
            performJump { await epub.goToFraction(f) }
        }
    }

    private var gotoSheet: some View {
        NavigationStack {
            Form {
                if let pdf = pdfNavigator {
                    Section {
                        HStack {
                            Text("Page")
                            TextField("1", value: $gotoPage, format: .number)
                                .keyboardType(.numberPad)
                                .multilineTextAlignment(.trailing)
                                .frame(maxWidth: .infinity)
                            Text("of \(pdf.pageCount)").foregroundStyle(.secondary)
                        }
                        Button("Go") {
                            gotoPage = min(max(1, gotoPage), pdf.pageCount)   // clamp typed input
                            doGoto(); showGoto = false
                        }
                    } header: { Text("Go to page") }
                } else {
                    Section("Go to position") {
                        Slider(value: $gotoFraction, in: 0...1)
                        Text("\(Int((gotoFraction * 100).rounded()))%").font(.footnote).foregroundStyle(.secondary)
                        Button("Go") { doGoto(); showGoto = false }
                    }
                }
            }
            .navigationTitle("Go To").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarLeading) { Button("Cancel") { showGoto = false } } }
        }
        .presentationDetents([.height(220)])
    }

    // MARK: Bookmarks list (the `bookmarkList` chrome control) — view + jump + delete, from the synced
    // BookmarkStore. The web/PWA `bmList` panel's iOS counterpart.

    private func openBookmarkList() async {
        if let result = try? await bookmarks.pull(publicationId: pubId, since: 0) {
            bookmarkItems = result.ops.filter { $0.deletedAt == nil }
                .sorted { ($0.fraction ?? 0) < ($1.fraction ?? 0) }
        }
        showBookmarks = true
    }

    private func deleteBookmark(_ bm: Bookmark) async {
        var tomb = bm
        tomb.deletedAt = Date(); tomb.updatedAt = Date(); tomb.rev = bm.rev + 1
        _ = try? await bookmarks.push(publicationId: pubId, ops: [tomb])
        bookmarkItems.removeAll { $0.id == bm.id }
    }

    /// Clear every bookmark for this book — tombstones them all so the removal propagates to the server
    /// (and thence to the other surfaces), not just the local copy.
    private func clearBookmarks() async {
        let now = Date()
        let tombs = bookmarkItems.map { bm -> Bookmark in
            var t = bm; t.deletedAt = now; t.updatedAt = now; t.rev = bm.rev + 1; return t
        }
        if !tombs.isEmpty { _ = try? await bookmarks.push(publicationId: pubId, ops: tombs) }
        bookmarkItems = []
    }

    private static func bookmarkLabel(_ bm: Bookmark) -> String {
        if let l = bm.label, !l.isEmpty { return l }
        if let loc = bm.locator { let s = label(for: loc); if !s.isEmpty { return s } }
        if let f = bm.fraction { return "\(Int((f * 100).rounded()))%" }
        return "Bookmark"
    }

    private var bookmarksSheet: some View {
        NavigationStack {
            Group {
                if bookmarkItems.isEmpty {
                    ContentUnavailableView("No bookmarks", systemImage: "bookmark",
                                           description: Text("Add one with “Add Bookmark” while reading."))
                } else {
                    List {
                        ForEach(bookmarkItems) { bm in
                            Button {
                                if let loc = bm.locator { jump(to: loc) }
                                showBookmarks = false
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(Self.bookmarkLabel(bm))
                                    Text(bm.createdAt.formatted(date: .abbreviated, time: .shortened))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .onDelete { idx in
                            let targets = idx.map { bookmarkItems[$0] }
                            Task { for bm in targets { await deleteBookmark(bm) } }
                        }
                    }
                }
            }
            .navigationTitle("Bookmarks")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !bookmarkItems.isEmpty {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Clear All", role: .destructive) { showClearBookmarks = true }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) { Button("Close") { showBookmarks = false } }
            }
            .confirmationDialog("Clear all bookmarks for this book?", isPresented: $showClearBookmarks,
                                titleVisibility: .visible) {
                Button("Clear All", role: .destructive) { Task { await clearBookmarks() } }
            }
        }
    }

    // MARK: Table of contents (reader.outline() → tap to jump)

    private struct TocRow: Identifiable { let id: Int; let depth: Int; let item: TocItem }

    /// Flatten the nested outline into indented rows with a stable positional id.
    private var tocRows: [TocRow] {
        var out: [TocRow] = []
        var i = 0
        func walk(_ items: [TocItem], _ depth: Int) {
            for it in items {
                out.append(TocRow(id: i, depth: depth, item: it)); i += 1
                walk(it.children, depth + 1)
            }
        }
        walk(tocItems, 0)
        return out
    }

    private var tocSheet: some View {
        NavigationStack {
            Group {
                if tocRows.isEmpty {
                    ContentUnavailableView("No contents", systemImage: "list.bullet",
                                           description: Text("This book has no table of contents."))
                } else {
                    List(tocRows) { row in
                        Button { jump(to: row.item.locator); showToc = false } label: {
                            Text(row.item.title.isEmpty ? "Untitled" : row.item.title)
                                .padding(.leading, CGFloat(row.depth) * 16)
                        }
                    }
                }
            }
            .navigationTitle("Contents")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Close") { showToc = false } } }
        }
    }

    // MARK: In-book search (reader.search() → tap a hit to jump)

    private var searchSheet: some View {
        NavigationStack {
            List {
                ForEach(Array(searchResults.enumerated()), id: \.offset) { _, loc in
                    Button { jump(to: loc); showSearch = false } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            if let hit = loc.text?.highlight, !hit.isEmpty {
                                Text(hit).lineLimit(2)
                            }
                            let where_ = Self.label(for: loc)
                            if !where_.isEmpty {
                                Text(where_).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
            .overlay {
                if searchResults.isEmpty {
                    ContentUnavailableView("Search the book", systemImage: "magnifyingglass",
                                           description: Text("Type a query and press Search."))
                }
            }
            .searchable(text: $searchQuery, placement: .navigationBarDrawer(displayMode: .always))
            .onSubmit(of: .search) { runSearch() }
            .navigationTitle("Search")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Close") { showSearch = false } } }
        }
    }

    private func runSearch() {
        let query = searchQuery
        guard !query.trimmingCharacters(in: .whitespaces).isEmpty else { searchResults = []; return }
        Task { searchResults = (try? await reader?.search(query)) ?? [] }
    }

    /// Pull the publication's marks from the sync-of-record and (re-)render them. `reset` pulls the FULL
    /// set (on open); otherwise it pulls only the delta `since` the last known `rev` and merges — so a
    /// highlight/ink/text-box made on ANOTHER device shows up on foreground/poll WITHOUT re-fetching the
    /// whole set or re-downloading the PDF, and WITHOUT moving the current page. Marks are an overlay, so
    /// this only composites new records on top; the file never moves. Text marks go through
    /// `MarkOverlay`/`DecorationHost`; PDF ink renders through `PdfInkHost` (EPUB ink is N4 slice 4).
    private func pullMarks(reset: Bool) async {
        guard reader != nil else { return }   // don't poll before the book is open
        guard let result = try? await annotations.pull(publicationId: pubId, since: reset ? 0 : rev) else { return }
        if reset { marksById.removeAll() }
        for op in result.ops { marksById[op.id] = op }   // upsert deltas (tombstones kept, filtered on render)
        rev = max(rev, result.rev)
        renderMarks()
    }

    /// Re-render the live (non-tombstoned) marks from the merged set. Preserves the current reading
    /// position — a cross-device mark appearing must never reposition the reader.
    private func renderMarks() {
        marks = marksById.values.filter { !$0.isTombstone }
        renderLayer?.render(marks)   // marks + ink, via the per-format rendering layer
    }

    // MARK: Cross-device reading position (Shape-C, advisory resume)

    /// On open, if the server's position (set by another device) is AHEAD of where this device restored
    /// to, offer a non-intrusive "Resume" pill. Never auto-jumps — the reader stays where the local
    /// store put it until the user chooses.
    private func checkResume() async {
        guard let server = await positionSync.pull(publicationId: pubId), let target = server.locator,
              isAhead(server: target, serverFraction: server.fraction, local: reader?.currentLocation)
        else { return }
        resumeLocator = target
        let where_ = target.locations.page.map { "Page \($0)" }
            ?? (server.fraction ?? target.locations.progression).map { "\(Int(($0 * 100).rounded()))%" }
            ?? "a later spot"
        withAnimation { resumePill = "Resume · \(where_) (another device)" }
    }

    /// Whether the server position is meaningfully past the local one (a later PDF page, or >1% further
    /// through an EPUB). A device that only ever reads here matches the server, so no prompt appears.
    private func isAhead(server: Locator, serverFraction: Double?, local: Locator?) -> Bool {
        if let sp = server.locations.page { return sp > (local?.locations.page ?? 0) }
        let sf = serverFraction ?? server.locations.progression ?? 0
        return sf > (local?.locations.progression ?? 0) + 0.01
    }

    /// Mirror the current reading position to the server (best-effort; LWW). Fired on background/close/
    /// poll so another device sees fresh progress.
    private func pushPosition() async {
        guard let loc = reader?.currentLocation else { return }
        await positionSync.push(publicationId: pubId, locator: loc, fraction: loc.locations.progression)
    }

    /// Tap the resume pill → jump to the other device's spot (records a back target so "Back" returns).
    private func takeResume() {
        if let t = resumeLocator { jump(to: t) }
        withAnimation { resumePill = nil }
    }

    /// Per-line quads for the current PDF text selection, grouped by page (0-based index). Each line's
    /// PDFKit page-space bounds is normalized to a top-left `[x,y,w,h]` via the shared `PageGeometry`
    /// (the same wire shape web/server use). Empty when there's no text selection.
    private func selectionQuads() -> [(page: Int, quads: [[Double]])] {
        guard let pdf = pdfNavigator?.pdfView, let doc = pdf.document,
              let sel = pdf.currentSelection else { return [] }
        var byPage: [Int: [[Double]]] = [:]
        for line in sel.selectionsByLine() {
            guard let page = line.pages.first else { continue }
            let idx = doc.index(for: page)
            let b = page.bounds(for: .cropBox)
            let r = line.bounds(for: page)
            guard r.width > 0, r.height > 0 else { continue }
            let quad = PageGeometry.topLeftQuad(x: r.minX, y: r.minY, w: r.width, h: r.height,
                                                pageMinX: b.minX, pageMinY: b.minY,
                                                pageWidth: b.width, pageHeight: b.height)
            byPage[idx, default: []].append(quad)
        }
        return byPage.map { (page: $0.key, quads: $0.value) }.sorted { $0.page < $1.page }
    }

    private func addHighlight() async { await addTextMark(.highlight) }

    /// EPUB text mark from the current selection — anchored by **cfiRange** (not quads), rendered via
    /// `EpubDecorationHost` (epub.js annotations). Highlight/underline only; strike/note are PDF-only.
    private func addEpubTextMark(_ kind: AnnotationKind) async {
        guard let sel = epubSelection, !sel.cfiRange.isEmpty,
              let loc = currentLocation else { return }
        let now = Date()
        let mark = Annotation(publicationId: pubId, kind: kind, locator: loc,
                              cfiRange: sel.cfiRange,
                              color: kind == .highlight ? "#ffd54a" : "#ff3b30",
                              createdAt: now, updatedAt: now, rev: rev + 1)
        marksById[mark.id] = mark
        epubSelection = nil
        renderMarks()   // optimistic
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)
    }

    /// Anchor a text mark (highlight/underline/strikeout) to the current selection's per-line quads and
    /// push it. One `Annotation` per page the selection spans. The structured store — not the file — is
    /// the source of truth, so the mark reaches web/export in the same `rect` quad shape.
    private func addTextMark(_ kind: AnnotationKind) async {
        let groups = selectionQuads()
        guard !groups.isEmpty else { return }
        let now = Date()
        var created: [Annotation] = []
        for g in groups {
            let loc = Locator(publicationId: pubId, format: .pdf, locations: .init(page: g.page + 1))
            let mark = Annotation(publicationId: pubId, kind: kind, locator: loc,
                                  quads: g.quads,
                                  color: kind == .highlight ? "#ffd54a" : "#ff3b30",
                                  createdAt: now, updatedAt: now, rev: rev + 1)
            marksById[mark.id] = mark; created.append(mark)
        }
        pdfNavigator?.pdfView.clearSelection()
        renderMarks()   // optimistic
        for m in created { _ = try? await annotations.push(publicationId: pubId, ops: [m]) }
        await pullMarks(reset: false)
    }

    /// Note: capture the selection's first-line top-left as the anchor point, then prompt for text.
    private func beginNote() {
        guard let g = selectionQuads().first, let first = g.quads.first else { return }
        noteAnchor = (page: g.page, point: [first[0], first[1]])
        noteDraft = ""
        showNoteEntry = true
    }

    private func commitNote() async {
        guard let anchor = noteAnchor, !noteDraft.isEmpty else { return }
        let now = Date()
        let loc = Locator(publicationId: pubId, format: .pdf, locations: .init(page: anchor.page + 1))
        let mark = Annotation(publicationId: pubId, kind: .note, locator: loc,
                              region: anchor.point, color: "#ffd54a", noteText: noteDraft,
                              createdAt: now, updatedAt: now, rev: rev + 1)
        marksById[mark.id] = mark
        pdfNavigator?.pdfView.clearSelection()
        renderMarks()
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)
    }

    /// Erase text marks whose quads intersect the current selection (object-erase for marks).
    private func eraseTextMarks() async {
        guard let g = selectionQuads().first else { return }
        let page = g.page + 1
        let selRects = g.quads.map { NormRect(x: $0[0], y: $0[1], w: $0[2], h: $0[3]) }
        let hits = marks.filter { m -> Bool in
            guard [.highlight, .underline, .strikeout].contains(m.kind), !m.isTombstone,
                  m.locator.locations.page == page, let mq = m.quads else { return false }
            return mq.contains { q in
                let mr = NormRect(x: q[0], y: q[1], w: q[2], h: q[3])
                return selRects.contains { $0.intersects(mr) }
            }
        }
        guard !hits.isEmpty else { return }
        let now = Date()
        for var m in hits {
            m.deletedAt = now; m.updatedAt = now; m.rev = m.rev + 1
            marksById[m.id] = m
            _ = try? await annotations.push(publicationId: pubId, ops: [m])
        }
        pdfNavigator?.pdfView.clearSelection()
        renderMarks()
    }

    /// Enter/leave draw mode. Entering defaults to the pen (so the toolbar opens on a sane tool).
    private func toggleDraw() {
        drawMode.toggle()
        if drawMode { ink.select(tool: .pen) }
    }

    /// Render the palette at its current placement within the reader area. Docked = pinned to an edge
    /// with a small inset; floating = centred at the normalized point. Drag maths (normalize by area
    /// size, snap/float) live in the portable `InkPaletteController`; this only positions the view.
    @ViewBuilder private func positionedPalette(in size: CGSize) -> some View {
        let bar = InkToolbar(
            tool: $ink, palette: palette, canUndo: ink.canUndo, canRedo: ink.canRedo,
            onUndo: undoInk, onRedo: redoInk, onDone: { drawMode = false },
            onMove: { pt in palette.drag(toX: pt.x / max(size.width, 1), y: pt.y / max(size.height, 1)) },
            onMoveEnd: { pt in palette.endDrag(atX: pt.x / max(size.width, 1), y: pt.y / max(size.height, 1)) }
        )
        switch palette.placement {
        case .docked(let edge):
            bar.padding(padEdge(edge), 12)
                .frame(width: size.width, height: size.height, alignment: alignment(for: edge))
        case .floating(let x, let y):
            bar.position(x: x * size.width, y: y * size.height)
        }
    }

    private func padEdge(_ e: InkPaletteEdge) -> Edge.Set {
        switch e {
        case .top: return .top
        case .bottom: return .bottom
        case .leading: return .leading
        case .trailing: return .trailing
        }
    }

    private func alignment(for e: InkPaletteEdge) -> Alignment {
        switch e {
        case .top: return .top
        case .bottom: return .bottom
        case .leading: return .leading
        case .trailing: return .trailing
        }
    }

    /// A finished stroke from the canvas. An eraser stroke deletes the ink it crosses; a pen/highlighter
    /// stroke is persisted as a `kind:.ink` annotation and re-rendered through `PdfInkHost` (the
    /// canonical renderer) — the same record web/export use. Both are recorded for undo.
    private func addInk(_ stroke: InkStroke) async {
        if stroke.mode == .erase { await eraseInk(with: stroke); return }
        guard let locator = currentLocation,
              let mark = InkCapture.annotation(ink: Ink(strokes: [stroke]), locator: locator,
                                               publicationId: pubId, rev: rev + 1, now: Date())
        else { return }
        marksById[mark.id] = mark; renderMarks()   // optimistic: show immediately, even offline
        ink.record(.added(mark.id))
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)              // reconcile with server (no-op when offline)
    }

    /// Object-eraser: tombstone every ink mark on the current page whose bounding box the eraser stroke
    /// crosses. Vector delete (not a pixel clear) so it round-trips through the store like any edit and
    /// is undoable. A slight inset makes the eraser forgiving without being grabby.
    private func eraseInk(with eraser: InkStroke) async {
        guard let page = currentLocation?.locations.page else { return }
        let hitBox = PageGeometry.bounds(of: eraser.points).inset(by: 0.01)
        let hits = marks.filter { m -> Bool in
            guard m.kind == .ink, !m.isTombstone, m.locator.locations.page == page, let mInk = m.ink
            else { return false }
            return mInk.strokes.contains { hitBox.intersects(PageGeometry.bounds(of: $0.points)) }
        }
        guard !hits.isEmpty else { return }
        let now = Date()
        var removed: [UUID] = []
        for var m in hits {
            m.deletedAt = now; m.updatedAt = now; m.rev = m.rev + 1
            marksById[m.id] = m; removed.append(m.id)
            _ = try? await annotations.push(publicationId: pubId, ops: [m])
        }
        renderMarks()
        ink.record(.removed(removed))
    }

    private func undoInk() { if let m = ink.undo() { Task { await apply(m) } } }
    private func redoInk() { if let m = ink.redo() { Task { await apply(m) } } }

    /// Apply an undo/redo mutation to the store: tombstone or restore the named ink marks, bump `rev` so
    /// the change wins LWW, push, and repaint.
    private func apply(_ mutation: InkMutation) async {
        let now = Date()
        let (ids, deleted): ([UUID], Date?) = {
            switch mutation {
            case .tombstone(let ids): return (ids, now)
            case .restore(let ids): return (ids, nil)
            }
        }()
        for id in ids {
            guard var m = marksById[id] else { continue }
            m.deletedAt = deleted; m.updatedAt = now; m.rev = m.rev + 1
            marksById[id] = m
            _ = try? await annotations.push(publicationId: pubId, ops: [m])
        }
        renderMarks()
    }

    /// Download the server-flattened annotated PDF (`GET /holding/<id>/annotated.pdf` — reuses the tested
    /// PyMuPDF + perfect-freehand flatten, works over the tunnel) and present the iOS share sheet. The
    /// route is editor-only and returns 409 when the copy has no PDF-anchored marks yet.
    private func exportAnnotatedPdf() async {
        let url = ReaderRoutes.annotatedPdf(baseURL: endpoint.baseURL, holding: holding.holdingId)
        var req = URLRequest(url: url)
        endpoint.authorize(&req)
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else {
                exportMessage = code == 409
                    ? "Add a highlight, note, or ink to this copy first, then export."
                    : (code == 403 ? "You need edit access to export." : "The server couldn’t produce the file (\(code)).")
                return
            }
            let safe = title.replacingOccurrences(of: "/", with: "-")
            let out = FileManager.default.temporaryDirectory
                .appendingPathComponent("\(safe.isEmpty ? "book" : safe)-annotated.pdf")
            try data.write(to: out, options: .atomic)
            shareItem = ShareItem(url: out)
        } catch {
            exportMessage = "Couldn’t reach the server to export."
        }
    }

    /// Persist an EPUB ink stroke: resolve the block CFI + rect under the stroke's start point, normalize
    /// the stroke to that block (so it reflows with the text), and store it as a `kind:.ink` annotation
    /// anchored by `cfiRange`. `EpubInkHost` re-places it on each page turn.
    private func addEpubInk(_ stroke: PKStroke, startAt point: CGPoint) async {
        guard let nav = epubNavigator, let loc = currentLocation,
              let hit = await nav.cfiAtPoint(point) else { return }
        var s = InkCanvas.strokeFrom(stroke, in: hit.rect, color: ink.color, mode: ink.mode ?? .draw)
        s.width = ink.width
        let now = Date()
        let mark = Annotation(publicationId: pubId, kind: .ink, locator: loc, cfiRange: hit.cfi,
                              ink: Ink(strokes: [s]), createdAt: now, updatedAt: now, rev: rev + 1)
        marksById[mark.id] = mark
        ink.record(.added(mark.id))
        renderMarks()   // optimistic
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)
    }

    /// Drop a bookmark at the current location (synced via the `BookmarkStore`). A bookmark *panel*
    /// (list / jump / rename) is a follow-on; this is the create + sync path.
    private func addBookmark() async {
        guard let locator = currentLocation else { return }
        let now = Date()
        let bm = Bookmark(publicationId: pubId, locator: locator,
                          fraction: locator.locations.progression,
                          createdAt: now, updatedAt: now, rev: rev + 1)
        _ = try? await bookmarks.push(publicationId: pubId, ops: [bm])
    }
}

/// Embeds octavo's PDFKit host view (the navigator owns it; we just place it).
private struct PDFViewContainer: UIViewRepresentable {
    let pdfView: PDFView
    func makeUIView(context: Context) -> PDFView {
        // Preview-style: one continuous vertically-scrolling column with pinch-to-zoom (autoScales
        // fits width; the user pinches from there).
        pdfView.displayMode = .singlePageContinuous
        pdfView.displayDirection = .vertical
        pdfView.usePageViewController(false)
        pdfView.autoScales = true
        return pdfView
    }
    func updateUIView(_ uiView: PDFView, context: Context) {}
}

/// Embeds octavo's EPUB host (the `EpubWebNavigator`'s WKWebView running epub.js).
private struct WebViewContainer: UIViewRepresentable {
    let webView: WKWebView
    func makeUIView(context: Context) -> WKWebView { webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

/// A file to hand to the iOS share sheet (the exported annotated PDF).
private struct ShareItem: Identifiable {
    let id = UUID()
    let url: URL
}

/// Wraps `UIActivityViewController` for SwiftUI presentation (share / save / print the annotated PDF).
private struct ActivityView: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ vc: UIActivityViewController, context: Context) {}
}
#endif
