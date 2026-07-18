#if canImport(UIKit)
import SwiftUI
import Combine
import UniformTypeIdentifiers
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
    private let outline: any OutlineStore
    private let settingsStore: CatalogueReaderSettingsStore
    private let historyStore: ReaderHistoryStore
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
    // Pin-to-float for the two reader bars. Unpinned = the native nav-bar toolbar (auto-hides with the
    // chrome). Pinned = the bar's controls float as a moveable `FloatingPanel` (same portable
    // `PanelPlacementModel` as the ink palette), starting near its usual corner.
    @State private var generalBarPinned = false
    @State private var documentBarPinned = false
    @State private var generalBarPanel = PanelPlacementModel(placement: .floating(x: 0.24, y: 0.07))
    @State private var documentBarPanel = PanelPlacementModel(placement: .floating(x: 0.76, y: 0.07))
    // Shared across the palette + both bars so panels docked to the same edge pack instead of overlapping.
    @StateObject private var dockCoordinator = DockCoordinator()
    @State private var showNoteEntry = false
    @State private var noteDraft = ""
    @State private var noteAnchor: (page: Int, point: [Double])?   // page index (0-based) + top-left point
    @State private var noteAnchorCfi: String?                      // EPUB: the selection CFI a note anchors to
    @State private var noteAnchorText: String?                     // EPUB: the selected quote (Web Annotation TextQuote)
    @State private var noteEditing: Annotation?                    // the existing note being edited (vs a new one)
    @State private var showImportNotes = false                     // Web Annotation import file picker
    @State private var noteMarks: [Annotation] = []                // notes render as an `MarkerOverlay` layer, not marks
    @State private var epubRelocateToken = 0                       // bumped on EPUB relocation to re-place note markers
    @State private var epubSelection: (cfiRange: String, text: String)?   // EPUB: last text selection
    @State private var pdfSelectionActive = false                         // PDF: a non-empty text selection exists
    @State private var shareItem: ShareItem?          // the annotated PDF to share (Phase 6)
    // The unified "Contents" panel (PDF-Expert style): one popup with segmented Bookmarks | Outline tabs.
    // Bookmarks (list + Add) and the authored-outline editor (entries + Save / Save into PDF) live here.
    enum ContentsTab: Int, Hashable { case bookmarks, outline }
    @State private var showContents = false
    @State private var contentsTab: ContentsTab = .bookmarks
    @State private var contentsEditing = false        // Edit/Done toggle: tap-to-jump vs edit (PDF-Expert style)
    // Naming a bookmark before it's saved (instead of a default "Page N"), and renaming an existing one.
    @State private var showBookmarkNameDialog = false
    @State private var bookmarkNameDraft = ""
    @State private var pendingBookmarkLocator: Locator?
    @State private var renamingBookmark: Bookmark?    // non-nil → the name dialog renames this one
    // Focus the title field of a just-added outline entry so the user names it immediately.
    @FocusState private var focusedOutlineEntry: Int?
    // The editor's testable core: every operation emits a structured `outline.*` console line (to stdout
    // AND the unified log) that tests assert and a developer reads in Console.
    @State private var editor = OutlineEditorModel(trace: { line in
        print(line); ReaderLog.outline.info("\(line, privacy: .public)")
    })
    @State private var exportMessage: String?         // brief export failure notice
    @Binding private var showChrome: Bool     // Books-style: center-tap toggles the bars; owned by ReaderShell
    @AppStorage("readerTheme") private var readerThemeRaw = "auto"   // "auto" follows the device theme
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.horizontalSizeClass) private var hSizeClass   // compact (phone) → tools collapse into ⋯
    @Environment(\.openURL) private var openURL
    @State private var showSettings = false
    @State private var showToc = false
    @State private var tocItems: [TocItem] = []
    @State private var showSearch = false
    @State private var searchQuery = ""
    @State private var searchResults: [Locator] = []
    @State private var reflowMode = false                     // PDF: read the page's text as paragraphs
    @State private var reflowParagraphs: [String] = []
    @State private var bookmarkItems: [Bookmark] = []
    @State private var showClearBookmarks = false
    @State private var themeToast: String?          // brief "Sepia"/"Night"… flash on theme cycle
    @State private var themeToastToken = UUID()
    @State private var backStack: [Locator] = []    // jump origins (pushed on goTo, NOT on page turns)
    @State private var backPill: String?            // "Back to Page 12" — Apple-Books-style persistent pill
    @State private var showGoto = false
    @State private var gotoPage = 1                 // PDF: target page
    @State private var gotoFraction = 0.0           // EPUB: target position (0…1)
    // Per-document reading settings (font size / zoom / reflow size) — remembered per book via
    // `settingsStore`, restored on open. (Reading THEME stays global — see `readerThemeRaw`.)
    @State private var reflowFontPt = 18.0     // PDF reflow-to-text font pt (per-document)
    // A stable per-view-identity id for log correlation (survives struct recreation; new per tab remount).
    @State private var vid = String(UUID().uuidString.prefix(4))
    // Host-applied reading settings (not the content engine): mirrored from the navigator so SwiftUI can
    // paint the warmth overlay, drive screen brightness, and lock orientation. Synced on open + change.
    @State private var hostWarmth = 0.0
    @State private var hostBrightness: Double?
    @State private var hostOrientation: OrientationLock = .none
    @State private var hostHighlightColor: String?
    @State private var savedSystemBrightness: Double?   // restored on close
    // Furthest-read affordance: a Kindle-style pill offered when we open behind the furthest spot reached.
    @State private var furthestLocator: Locator?
    @State private var furthestPill: String?
    @Environment(\.dismiss) private var dismiss

    /// `annotations` defaults to **`ReaderSync`** over the endpoint (marks persist + sync via
    /// `/sync/reader`); inject an `InMemoryAnnotationStore` in tests/previews for a local-only store.
    public init(holding: Holding, title: String, endpoint: any ServerEndpoint, readingStore: CatalogueReadingStore,
                showChrome: Binding<Bool> = .constant(true),
                annotations: (any AnnotationStore)? = nil, bookmarks: (any BookmarkStore)? = nil,
                settingsStore: CatalogueReaderSettingsStore = CatalogueReaderSettingsStore(),
                historyStore: ReaderHistoryStore = ReaderHistoryStore(),
                topBarAccessory: AnyView = AnyView(EmptyView())) {
        self.holding = holding; self.title = title; self.endpoint = endpoint
        self.readingStore = readingStore
        self.settingsStore = settingsStore
        self.historyStore = historyStore
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
        // Local-first authored outline (persistent TOC): durable + synced through /sync/reader like
        // bookmarks; baked into the file only on the explicit "Save into PDF" action.
        self.outline = LocalOutlineStore(remote: OutlineSync(baseURL: endpoint.baseURL,
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
      ZStack {
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
                            PDFViewContainer(pdfView: pdfNavigator.pdfView)
                                .ignoresSafeArea(edges: .bottom)
                                // Once the PDF view is actually on-screen and laid out, re-assert the restored
                                // position and start honoring scroll — a go(to:) before layout can be dropped,
                                // and its layout-time page-0 churn would otherwise clobber the saved place.
                                .onAppear {
                                    Task {
                                        try? await Task.sleep(nanoseconds: 400_000_000)
                                        ReaderLog.annotations.info("[\(vid, privacy: .public)] pdf.onAppear render+settle marksById=\(marksById.count)")
                                        // ORDER MATTERS: add ALL marks first (text host re-tiles, then ink is
                                        // added with no repaint of its own), THEN settle() re-tiles once so both
                                        // text and ink paint. Doing settle first re-tiles before the marks exist.
                                        renderMarks()
                                        pdfNavigator.settle()   // re-assert position + final re-tile with all marks present
                                    }
                                }
                                // Notes float above the page as `MarkerOverlay` markers (drag / tap-to-expand /
                                // long-press-delete), tracking the page as it scrolls. Overlaid so the layer
                                // matches the PDF view's coordinate space exactly. Hidden while drawing.
                                .overlay {
                                    if !drawMode {
                                        PdfNoteLayer(pdfView: pdfNavigator.pdfView, notes: noteMarks, canEdit: true,
                                                     onPersist: { note in Task { await persistNoteMove(note) } },
                                                     onDelete: { note in Task { await deleteNote(note) } },
                                                     onEdit: { note in editNote(note) })
                                    }
                                }
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
                            .overlay {
                                if !drawMode {
                                    EpubNoteLayer(navigator: epubNavigator, notes: noteMarks,
                                                  relocateToken: epubRelocateToken, canEdit: true,
                                                  onPersist: { note in Task { await persistNoteMove(note) } },
                                                  onDelete: { note in Task { await deleteNote(note) } },
                                                  onEdit: { note in editNote(note) })
                                }
                            }
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
                            // A blank tap dismisses any active selection (so the mark bar closes and
                            // paging is unblocked) and toggles the bars.
                            if epubSelection != nil { epubSelection = nil }
                            withAnimation(.easeInOut(duration: 0.2)) { showChrome.toggle() }
                        })
                } else if let errorText {
                    ContentUnavailableView("Couldn’t open", systemImage: "exclamationmark.triangle", description: Text(errorText))
                } else {
                    ProgressView("Opening…")
                }
            }
            // Warm/blue-light overlay: an amber tint multiplied over the page (whites warm, blacks stay
            // black — the Night-Shift look). Below the chrome/pills, never intercepts touches.
            .overlay {
                if hostWarmth > 0 {
                    Color(red: 1.0, green: 0.58, blue: 0.0)
                        .opacity(hostWarmth * 0.35)
                        .blendMode(.multiply)
                        .allowsHitTesting(false)
                        .ignoresSafeArea()
                }
            }
            .overlay(alignment: .bottom) {
                if reader != nil && showChrome && !locLabel.isEmpty && !reflowMode && !drawMode {
                    locationBadge.allowsHitTesting(false)   // read-only page/percent, never blocks scroll
                }
            }
            // The draw palette + pinned bars are NOT overlaid on the content here — they float in a
            // full-window layer above the NavigationStack (see `floatingPanelsLayer`), so `FloatingPanel`
            // sees the true device size + safe insets and can dock against the real screen edges.
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
                // Kindle-style "furthest read": offered when we open behind the furthest spot ever reached
                // (local, unlike the cross-device resume pill). Tap to jump ahead; ✕ to dismiss.
                if let furthestPill, resumePill == nil {
                    HStack(spacing: 10) {
                        Button { takeFurthest() } label: {
                            Label(furthestPill, systemImage: "forward.end.alt").font(.footnote.weight(.medium))
                        }.buttonStyle(.plain)
                        Divider().frame(height: 16)
                        Button { withAnimation { self.furthestPill = nil } } label: { Image(systemName: "xmark").font(.caption2) }
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
                // Selection-driven mark bar: selecting text surfaces the SAME annotation controls the
                // toolbar shows for this format — rendered from the shared chrome spec's `selectionAction`
                // subset (highlight/underline/strike/note), so the bar and the toolbar can't drift when a
                // mark is added/removed. PDF and EPUB both get it; each shows only the marks its
                // capabilities back (EPUB has no strikethrough), and the icons come from the same
                // `ReaderIcons` source the toolbar uses.
                if reader != nil, !drawMode, !reflowMode, hasTextSelection, !selectionMarkControls.isEmpty {
                    selectionMarkBar
                }
            }
            // A PDF text selection drives the same bar (EPUB's arrives via `onSelection`). PDFKit posts
            // this whenever the selection changes; treat a non-empty selection as active.
            .onReceive(NotificationCenter.default.publisher(for: .PDFViewSelectionChanged)) { _ in
                onPdfSelectionChanged()
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
                // A pinned bar leaves the nav bar entirely and floats (rendered in an overlay below).
                ToolbarItem(placement: .topBarLeading) {
                    if !generalBarPinned { HStack(spacing: 8) { barItems(.general) }.imageScale(.small) }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if !documentBarPinned { HStack(spacing: 8) { barItems(.document) }.imageScale(.small) }
                }
            }
            .sheet(isPresented: $showSettings) {
                ReaderSettingsSheet(
                    initial: reader?.settings ?? .defaults,
                    isPDF: pdfNavigator != nil,
                    reflowMode: reflowMode,
                    apply: { settings in Task { await reader?.apply(settings) } },
                    onToggleReflow: { toggleReflow() })
            }
            .sheet(isPresented: $showToc) { tocSheet }
            .sheet(isPresented: $showSearch) { searchSheet }
            .sheet(isPresented: $showContents) { contentsSheet }
            .alert(noteEditing == nil ? "Add Note" : "Edit Note", isPresented: $showNoteEntry) {
                TextField("Note", text: $noteDraft)
                Button(noteEditing == nil ? "Add" : "Save") { Task { await commitNote() } }
                Button("Cancel", role: .cancel) { noteEditing = nil }
            } message: { Text(noteEditing == nil ? "A note anchored to the selected text." : "Edit the note text.") }
            .sheet(item: $shareItem) { item in ActivityView(items: [item.url]) }
            .fileImporter(isPresented: $showImportNotes, allowedContentTypes: [.json]) { result in
                if case .success(let url) = result { Task { await importNotes(from: url) } }
            }
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
                if phase == .active { Task { await pullMarks(reset: false); await flushBookmarks() } }
                else { Task { await pushPosition() } }   // backgrounding → mirror position for other devices
            }
            .onReceive(Timer.publish(every: 45, on: .main, in: .common).autoconnect()) { _ in
                Task { await pullMarks(reset: false); await flushBookmarks(); await pushPosition() }
            }
            .onDisappear {
                ReaderLog.annotations.info("[\(vid, privacy: .public)] onDisappear pub=\(pubId, privacy: .public)")
                epubNavigator?.tearDown()                 // break the WKWebView message-handler cycle
                pdfNavigator?.tearDown()                  // stop the PDF scale/page observers
                restoreHostState()                        // undo brightness override + orientation lock
                Task { await pushPosition() }             // closing the book → mirror final position
            }
        }
        floatingPanelsLayer   // full-window, above the nav bar (empty areas stay touch-transparent)
      }
    }

    /// The pinned bars + draw palette, floated in one layer that spans the whole window (over the nav bar
    /// and into the safe area) so `FloatingPanel` positions against the true device geometry. It only
    /// intercepts touches where a panel actually is — the reader below stays interactive.
    @ViewBuilder private var floatingPanelsLayer: some View {
        ZStack {
            if drawMode && (pdfNavigator != nil || epubNavigator != nil) {
                paletteFloating.transition(.opacity)
            }
            if generalBarPinned { floatingBar(.general) }
            if documentBarPinned { floatingBar(.document) }
        }
        .ignoresSafeArea()
    }

    private func open() async {
        ReaderLog.annotations.info("[\(vid, privacy: .public)] open() pub=\(pubId, privacy: .public)")
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
            // LOCAL setup first — none of this touches the network, so the reader paints (marks, theme,
            // settings, position affordances) immediately, even offline / with a dead server.
            await pullMarks(reset: true)   // local-first (reconciles the server in the background)
            await applyReadingTheme()      // theme is a local computation — apply it NOW
            await restoreSettings()        // per-document reflow mode/size (font/zoom restored by octavo)
            observeSettings()              // sync + apply host settings (warmth/brightness/orientation), observe changes
            await restoreHistory()         // persisted back/jump stack (local file)
            await checkFurthest()          // furthest-read affordance (local reading store)
            // ADVISORY cross-device resume is the ONLY networked step — never let it gate the display.
            // Run it detached so a slow/timing-out server can't block anything above (it only sets a pill).
            Task { await checkResume() }
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
            settingsStore: settingsStore,
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
            settingsStore: settingsStore,
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
            if self.epubNavigator != nil {
                self.epubSelection = nil   // a page change makes any pending selection stale
                self.renderMarks(); self.epubRelocateToken &+= 1
            }
        }
        if let loc = reader?.currentLocation { locLabel = Self.label(for: loc) }
    }

    /// Mirror host-applied settings (warmth / brightness / orientation / highlight colour) from the
    /// navigator into SwiftUI state, and apply the device-level ones. Chains after octavo's auto-persist,
    /// so a settings-panel change flows: apply → onSettingsChanged → persist AND repaint here.
    /// PDF selection changed. Deferred off any in-flight SwiftUI update: PDFKit can post
    /// `PDFViewSelectionChanged` synchronously during the view's own layout (makeUIView /
    /// layoutDocumentView), so mutating `pdfSelectionActive` inline would publish mid-update.
    private func onPdfSelectionChanged() {
        Task { @MainActor in
            let active = (pdfNavigator?.pdfView.currentSelection?.string?.isEmpty == false)
            if active != pdfSelectionActive {
                withAnimation(.easeInOut(duration: 0.15)) { pdfSelectionActive = active }
            }
        }
    }

    private func observeSettings() {
        // Defer host-setting application off any in-flight SwiftUI update: `onSettingsChanged` can fire
        // from the navigator during layout (a PDFView notification posted inside makeUIView), and mutating
        // host `@State` there triggers "Publishing changes from within view updates". A tick later is fine.
        reader?.onSettingsChanged { s in Task { @MainActor in applyHostSettings(s) } }
        applyHostSettings(reader?.settings ?? .defaults)
    }

    private func applyHostSettings(_ s: ReaderSettings) {
        hostWarmth = s.warmth ?? 0
        hostHighlightColor = s.highlightColor
        // Screen brightness: remember the system value once, so we can restore it when the book closes.
        hostBrightness = s.brightness
        if let b = s.brightness {
            if savedSystemBrightness == nil { savedSystemBrightness = Double(UIScreen.main.brightness) }
            UIScreen.main.brightness = CGFloat(max(0, min(1, b)))
        }
        // Orientation lock (per book).
        let lock = s.orientationLock ?? .none
        if lock != hostOrientation { hostOrientation = lock; ReaderOrientation.shared.set(lock) }
    }

    /// Restore device state the reader overrode (called on close).
    private func restoreHostState() {
        if let b = savedSystemBrightness { UIScreen.main.brightness = CGFloat(b); savedSystemBrightness = nil }
        ReaderOrientation.shared.clear()
    }

    /// The active reading theme. "auto" follows the device: light → White, dark → Night.
    private var readingTheme: ReadingTheme {
        if readerThemeRaw == "auto" { return systemIsDark ? .night : .white }
        return ReadingTheme(rawValue: readerThemeRaw) ?? .default
    }

    /// Whether the DEVICE is in dark mode. Read from the screen trait, not just SwiftUI's
    /// `@Environment(\.colorScheme)` — a `.fullScreenCover`-presented reader doesn't reliably inherit the
    /// device appearance into its environment, so on a tab remount `colorScheme` can read `.light` even in
    /// device dark mode (which flipped "auto" to the light reading theme). The screen trait is the truth.
    private var systemIsDark: Bool {
        colorScheme == .dark || UIScreen.main.traitCollection.userInterfaceStyle == .dark
    }
    private func readingHex(_ t: ReadingToken) -> String { ReadingPalette.hex[readingTheme]?[t] ?? "#ffffff" }
    /// The default colour for a new highlight — the per-book `highlightColor` setting, else the app default.
    private var highlightHex: String { hostHighlightColor ?? "#ffd54a" }

    // MARK: PDF reflow-to-text (GoodReader style) — PDF-only; EPUB is already reflowable.

    private func toggleReflow() {
        reflowMode.toggle()
        if reflowMode { updateReflow() }
        let id = pubId, on = reflowMode
        Task { await settingsStore.setReflowMode(id, on) }
    }

    /// Re-extract the current PDF page's text and reflow it into paragraphs (shared Tier-2 `reflowPageText`).
    private func updateReflow() {
        reflowParagraphs = reflowPageText(pdfNavigator?.currentPageText() ?? "")
    }

    private func biggerText() {
        if reflowMode {
            reflowFontPt = min(reflowFontPt + 2, 32); saveReflowFontPt()
        } else {
            // EPUB font size (the only text-resize surface — PDF uses zoom) goes through the navigator's
            // resize verb, which records the new size and auto-persists it via `Octavo.open(settingsStore:)`.
            Task { await reader?.bigger() }
        }
    }
    private func smallerText() {
        if reflowMode {
            reflowFontPt = max(reflowFontPt - 2, 12); saveReflowFontPt()
        } else {
            Task { await reader?.smaller() }
        }
    }

    /// Restore this document's app-only reading settings on open. Font size (EPUB) and zoom (PDF) are
    /// restored by octavo via `Octavo.open(settingsStore:)`; only the PDF reflow-to-text size lives
    /// outside the octavo model, so it is restored here. Reading THEME stays global (see
    /// `applyReadingTheme`); position is restored by octavo too.
    private func restoreSettings() async {
        if let rp = await settingsStore.reflowFontPt(pubId) { reflowFontPt = rp }
        // Reflow-to-text is PDF-only and its ON/OFF is app state; restore the mode too (not just its size).
        if pdfNavigator != nil, await settingsStore.reflowMode(pubId) {
            reflowMode = true
            updateReflow()
        }
    }

    /// Load the persisted back/jump stack so the "Back to …" pill survives a reopen.
    private func restoreHistory() async {
        let saved = await historyStore.get(pubId)
        if !saved.isEmpty { backStack = saved; refreshBackPill() }
    }

    private func persistHistory() {
        let id = pubId, stack = backStack
        Task { await historyStore.set(id, stack) }
    }

    /// Offer a Kindle-style "jump ahead" when we opened behind the furthest spot ever reached (and no
    /// cross-device resume is already being offered).
    private func checkFurthest() async {
        guard resumePill == nil,
              let f = try? await readingStore.furthest(pubId),
              let fp = f.locations.progression,
              let cur = reader?.currentLocation?.locations.progression,
              fp > cur + 0.02 else { return }
        furthestLocator = f
        withAnimation { furthestPill = "Furthest read · " + Self.label(for: f) }
    }

    private func takeFurthest() {
        guard let f = furthestLocator else { return }
        withAnimation { furthestPill = nil }
        jump(to: f)
    }

    /// Persist the PDF reflow-to-text font size (the one per-document setting octavo doesn't model —
    /// EPUB font and PDF zoom auto-persist through the navigator).
    private func saveReflowFontPt() {
        let id = pubId, pt = reflowFontPt
        Task { await settingsStore.setReflowFontPt(id, pt) }
    }

    // MARK: PDF magnifier zoom (PDF-only; EPUB uses font A± above). Setting `scaleFactor` opts out of
    // PDFView `autoScales`, so a chosen zoom sticks instead of snapping back on relayout.

    private func zoomPdf(by factor: Double) {
        guard let v = pdfNavigator?.pdfView else { return }
        v.autoScales = false
        let target = v.scaleFactor * CGFloat(factor)
        v.scaleFactor = min(max(target, v.minScaleFactor), v.maxScaleFactor)
    }

    /// Fit the current page's *width* to the viewport (Preview "fit width"): `scaleFactor` maps page points
    /// to view points, so the fill factor is viewWidth / pageWidth.
    private func fitPdfWidth() {
        guard let v = pdfNavigator?.pdfView, let page = v.currentPage else { return }
        let pageWidth = page.bounds(for: v.displayBox).width
        guard pageWidth > 0, v.bounds.width > 0 else { return }
        v.autoScales = false
        let target = (v.bounds.width - 8) / pageWidth   // small inset so the page isn't flush to the edges
        v.scaleFactor = min(max(target, v.minScaleFactor), v.maxScaleFactor)
    }

    /// Resolve the persisted reading theme to concrete colours (composition root: `ReadingPalette` is
    /// named only here, never in octavo) and hand them to the engine.
    private func applyReadingTheme() async {
        let t = readingTheme
        let hex = ReadingPalette.hex[t] ?? [:]
        ReaderLog.annotations.info("[\(vid, privacy: .public)] 🎨 applyReadingTheme raw=\(readerThemeRaw, privacy: .public) env=\(colorScheme == .dark ? "dark" : "light", privacy: .public) screen=\(UIScreen.main.traitCollection.userInterfaceStyle == .dark ? "dark" : "light", privacy: .public) → theme=\(String(describing: t), privacy: .public) isDark=\(t.isDark)")
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
                // Don't page while text is selected: dragging a selection HANDLE is a horizontal drag
                // too, and would otherwise turn the page mid-selection (the "tap-to-select flips the
                // page" bug). A tap or a page turn clears `epubSelection`, so paging resumes after.
                guard reader != nil, epubSelection == nil,
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
    /// The annotation vocabulary (highlight/underline/strike/note/draw/erase) is uniform across formats;
    /// each format *excludes* what it can't back yet — EPUB omits strike/note/erase (no CFI impl yet) and
    /// export; PDF omits text-resize. On iPad these sit inline; on a phone (`compact`) they collapse to ⋯.
    private var chromeControls: [ReaderControl] {
        let pdf = pdfNavigator != nil
        let canAnnotate = reader?.capabilities.canAnnotate == true
        let caps = ReaderCaps(
            ready: reader != nil,
            search: reader?.capabilities.canSearch == true,
            star: true,
            resizeText: !pdf,                       // EPUB: font A± (text reflows)
            zoom: pdf,                              // PDF: magnifier zoom ± + fit-width (no font resize)
            reflow: pdf,                            // reflow-to-text is PDF-only
            markText: canAnnotate,                  // highlight + underline: both formats (PDF quads / EPUB CFI)
            strike: canAnnotate && pdf,             // strikethrough stays PDF-only (epub.js has no strike style)
            note: canAnnotate,                      // notes: both formats — a CFI-anchored `MarkerOverlay` marker
            draw: canAnnotate,                      // ink: both formats on iOS
            erase: false,                           // ink eraser lives in the draw palette; text marks toggle off
            annList: false,                         // iOS has no annotations-list surface yet
            export: (reader?.capabilities.canExport == true) && pdf,
            editOutline: canAnnotate && pdf)        // author the persistent PDF outline (editor-only, PDF)
        return readerChromeVM(format: pdf ? "pdf" : "epub", caps: caps,
                              reflow: reflowMode, draw: drawMode, compact: hSizeClass == .compact)
    }

    // MARK: Selection-driven mark bar — the SAME shared spec as the toolbar (its `selectionAction` subset)

    /// The text-selection marks for this format, from the shared chrome spec (highlight/underline/strike/
    /// note, gated by capability). The bar and the toolbar both derive from `chromeControls`, so a mark
    /// added to the spec appears in both with the same icon and never drifts.
    private var selectionMarkControls: [ReaderControl] { chromeControls.filter { $0.selectionAction } }

    /// Is there a live text selection to mark? EPUB reports it via `onSelection`; PDF via PDFKit's
    /// selection-changed notification.
    private var hasTextSelection: Bool { epubSelection != nil || pdfSelectionActive }

    /// The floating mark bar shown while text is selected. Renders the spec subset by `id` — the icon from
    /// the same `ReaderIcons` source the toolbar uses — plus a dismiss button.
    private var selectionMarkBar: some View {
        // Capture the EPUB selection once so a stray webview tap that clears `epubSelection` can't null it
        // out before an async mark action reads it (PDF reads its live selection at tap time instead).
        let epubSel = epubSelection
        return HStack(spacing: 16) {
            ForEach(selectionMarkControls) { c in
                Button { performSelectionMark(c.id, epubSel: epubSel) } label: {
                    Image(systemName: ReaderIcons.sf(c.id, active: c.active))
                }
                .accessibilityLabel(Self.selectionMarkTitle(c.id))
            }
            Button { dismissTextSelection() } label: { Image(systemName: "xmark") }
                .foregroundStyle(.secondary)
                .accessibilityLabel("Dismiss")
        }
        .font(.footnote.weight(.medium))
        .padding(.horizontal, 16).padding(.vertical, 10)
        .background(.ultraThinMaterial, in: Capsule())
        .shadow(radius: 4, y: 2)
        .padding(.top, 8)
        .transition(.move(edge: .top).combined(with: .opacity))
    }

    /// Dispatch a selection mark by shared `id`, routing to the format-appropriate action. Highlight/
    /// underline use the captured EPUB selection when present (else the PDF quad path); strike is PDF-only
    /// (never surfaces for EPUB); note anchors to whichever selection is live.
    private func performSelectionMark(_ id: String, epubSel: (cfiRange: String, text: String)?) {
        switch id {
        case "highlight":
            if let s = epubSel { Task { await addEpubTextMark(.highlight, s) } } else { Task { await addTextMark(.highlight) } }
        case "underline":
            if let s = epubSel { Task { await addEpubTextMark(.underline, s) } } else { Task { await addTextMark(.underline) } }
        case "strike":
            Task { await addTextMark(.strikeout) }
        case "note":
            beginNote(epubSel)
        default:
            break
        }
    }

    /// Dismiss the current text selection (the bar's ✕) — clears whichever format's selection is live so
    /// the bar hides and paging resumes.
    private func dismissTextSelection() {
        if epubSelection != nil { epubSelection = nil; Task { await epubNavigator?.clearSelection() } }
        if pdfSelectionActive { pdfNavigator?.pdfView.clearSelection(); pdfSelectionActive = false }
    }

    /// A short human title for a selection-mark control. Falls back to the capitalized id so a mark newly
    /// added to the spec still shows a label (its icon already comes from the shared config).
    private static func selectionMarkTitle(_ id: String) -> String {
        switch id {
        case "highlight": return "Highlight"
        case "underline": return "Underline"
        case "strike": return "Strikethrough"
        case "note": return "Note"
        default: return id.prefix(1).uppercased() + id.dropFirst()
        }
    }

    /// A bar (leading/trailing) control, dispatched by its shared `id` to a native SwiftUI subcomponent.
    /// The ACTION is per-`id`; the ICON is sourced uniformly from the shared `ReaderIcons` config (its
    /// toggled-on variant used when `c.active`), so an icon is changed once in `library-core.js`.
    @ViewBuilder private func barControl(_ c: ReaderControl) -> some View {
        let icon = Image(systemName: ReaderIcons.sf(c.id, active: c.active))
        switch c.id {
        case "done":
            Button { dismiss() } label: { icon }.accessibilityLabel("Done")
        case "toc":
            Button { tocItems = reader?.outline() ?? []; showToc = true } label: { icon }
                .accessibilityLabel("Contents")
        case "search":
            Button { showSearch = true } label: { icon }.accessibilityLabel("Search")
        case "star":
            topBarAccessory
        case "textSmaller":
            Button { smallerText() } label: { icon }.accessibilityLabel("Smaller text")
        case "textLarger":
            Button { biggerText() } label: { icon }.accessibilityLabel("Larger text")
        case "zoomOut":
            Button { zoomPdf(by: 1 / 1.25) } label: { icon }.accessibilityLabel("Zoom out")
        case "zoomIn":
            Button { zoomPdf(by: 1.25) } label: { icon }.accessibilityLabel("Zoom in")
        case "fitWidth":
            Button { fitPdfWidth() } label: { icon }.accessibilityLabel("Fit width")
        case "reflow":
            Button { toggleReflow() } label: { icon }.accessibilityLabel("Reflow to text")
        case "goto":
            Button { prepareGoto(); showGoto = true } label: { icon }.accessibilityLabel("Go to")
        case "theme":
            Button { cycleTheme() } label: { icon }.accessibilityLabel("Reading theme")
        case "undo":
            Button { undoInk() } label: { icon }.disabled(!ink.canUndo).accessibilityLabel("Undo")
        case "redo":
            Button { redoInk() } label: { icon }.disabled(!ink.canRedo).accessibilityLabel("Redo")
        // Annotation vocabulary — rendered inline on a regular width (iPad); on a phone the spec marks
        // these `overflow` and they render as menu rows in `menuControl` instead.
        case "highlight":
            Button { Task { await doHighlight() } } label: { icon }.accessibilityLabel("Highlight")
        case "underline":
            Button { Task { await doUnderline() } } label: { icon }.accessibilityLabel("Underline")
        case "strike":
            Button { Task { await addTextMark(.strikeout) } } label: { icon }.accessibilityLabel("Strikethrough")
        case "note":
            Button { beginNote() } label: { icon }.accessibilityLabel("Note")
        case "draw":
            Button { toggleDraw() } label: { icon }.accessibilityLabel("Draw")
        case "erase":
            Button { Task { await eraseTextMarks() } } label: { icon }.accessibilityLabel("Erase marks")
        case "export":
            Button { Task { await exportAnnotatedPdf() } } label: { icon }.accessibilityLabel("Share annotated PDF")
        default:
            EmptyView()
        }
    }

    // MARK: reader bars — pin to float (same `FloatingPanel` pattern as the ink palette)

    /// Which of the two reader bars.
    private enum BarKind { case general, document }

    /// A bar's controls plus its pin toggle — rendered either in the nav-bar toolbar (unpinned) or inside
    /// the floating panel (pinned), so the two paths never drift.
    @ViewBuilder private func barItems(_ bar: BarKind) -> some View {
        switch bar {
        case .general:
            ForEach(chromeControls.filter { $0.bar == "general" }) { barControl($0) }
        case .document:
            ForEach(chromeControls.filter { $0.bar == "text" && !$0.overflow }) { barControl($0) }
            let overflow = chromeControls.filter { $0.overflow }
            if !overflow.isEmpty || !noteMarks.isEmpty { overflowMenu(overflow) }
        }
        // The standard pin toggle from the reusable floating-panel component (pin ⇄ pin.fill).
        PanelPinButton(pinned: pinnedBinding(bar),
                       symbol: ReaderIcons.sf("pin"), filledSymbol: ReaderIcons.sf("pin", active: true))
    }

    private func pinnedBinding(_ bar: BarKind) -> Binding<Bool> {
        switch bar {
        case .general: return $generalBarPinned
        case .document: return $documentBarPinned
        }
    }

    /// A pinned bar, floated via the reusable `FloatingPanel` + `PanelChrome` — dockable/moveable exactly
    /// like the ink palette, driven by that bar's portable `PanelPlacementModel`. `PanelChrome` supplies
    /// the move handle and lays the controls out along the panel's axis (a row when docked top/bottom or
    /// floating; a column when docked to a side); `FloatingPanel` keeps it fully on-screen.
    @ViewBuilder private func floatingBar(_ bar: BarKind) -> some View {
        let model = bar == .general ? generalBarPanel : documentBarPanel
        // One panel per edge: a docked panel is centred on its edge; a drag that would snap onto an edge
        // another panel already holds stays floating instead (see settleBarDock).
        FloatingPanel(placement: model.placement,
                      onDrag: { nx, ny in updateBarPanel(bar) { $0.drag(toX: nx, y: ny) } },
                      onDragEnd: { nx, ny in settleBarDock(bar, nx: nx, ny: ny) },
                      coordinateSpace: bar == .general ? "reader-bar-general" : "reader-bar-document",
                      dock: dockCoordinator,
                      dockID: barDockID(bar), dockAnchor: .center, dockOrder: 0) { handlers in
            PanelChrome(handlers: handlers, axis: model.axis) {
                barItems(bar)
            }
            .imageScale(.small)   // grip keeps its own scale; the controls inherit this
        }
        .transition(.opacity)
    }

    private func updateBarPanel(_ bar: BarKind, _ mutate: (inout PanelPlacementModel) -> Void) {
        switch bar {
        case .general: mutate(&generalBarPanel)
        case .document: mutate(&documentBarPanel)
        }
    }

    private func barDockID(_ bar: BarKind) -> String {
        bar == .general ? "reader-bar-general" : "reader-bar-document"
    }

    /// Finish a bar drag: dock/float via the model, then enforce one-panel-per-edge — if it snapped onto an
    /// edge another panel already holds, keep it floating at the drop point instead of stacking.
    private func settleBarDock(_ bar: BarKind, nx: Double, ny: Double) {
        updateBarPanel(bar) { $0.endDrag(atX: nx, y: ny) }
        let placement = (bar == .general ? generalBarPanel : documentBarPanel).placement
        if case .docked(let edge) = placement, dockCoordinator.isOccupied(edge, excluding: barDockID(bar)) {
            updateBarPanel(bar) { $0.drag(toX: nx, y: ny) }
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
            // The full per-document reading-settings panel (typography / layout / PDF fit-crop / comfort).
            Button { showSettings = true } label: { Label("Reading Settings…", systemImage: "textformat.size") }
            // Pen colour / width / eraser now live in the on-screen `InkToolbar` while drawing.
            // Notes as an open Web Annotation file — export what's here, or import someone else's.
            Divider()
            if !noteMarks.isEmpty {
                Button { Task { await exportNotes() } } label: { Label("Export Notes…", systemImage: "square.and.arrow.up") }
            }
            Button { showImportNotes = true } label: { Label("Import Notes…", systemImage: "square.and.arrow.down") }
        } label: { Image(systemName: "ellipsis.circle") }
    }

    /// The ⋯ overflow rows. Text label is per-`id`; the icon comes from the shared `ReaderIcons` config.
    @ViewBuilder private func menuControl(_ c: ReaderControl) -> some View {
        let sym = ReaderIcons.sf(c.id, active: c.active)
        switch c.id {
        case "undo":
            Button { undoInk() } label: { Label("Undo", systemImage: sym) }.disabled(!ink.canUndo)
        case "redo":
            Button { redoInk() } label: { Label("Redo", systemImage: sym) }.disabled(!ink.canRedo)
        case "bookmarkList":
            // Single entry to the unified panel — Bookmarks + Outline tabs; Add Bookmark / Edit Outline
            // live inside it (the `bookmarkAdd` / `editOutline` spec controls render nothing standalone
            // on iOS, folded here per the user's "one popup" request).
            Button { openContents(.bookmarks) } label: { Label("Contents", systemImage: sym) }
        case "highlight":
            Button { Task { await doHighlight() } } label: { Label("Highlight", systemImage: sym) }
        case "underline":
            Button { Task { await doUnderline() } } label: { Label("Underline", systemImage: sym) }
        case "strike":
            Button { Task { await addTextMark(.strikeout) } } label: { Label("Strikethrough", systemImage: sym) }
        case "note":
            Button { beginNote() } label: { Label("Note", systemImage: sym) }
        case "erase":
            Button { Task { await eraseTextMarks() } } label: { Label("Erase Marks", systemImage: sym) }
        case "export":
            Button { Task { await exportAnnotatedPdf() } } label: { Label("Share Annotated PDF", systemImage: sym) }
        // `bookmarkAdd` + `editOutline` are folded into the unified Contents panel (see `bookmarkList`),
        // so they render nothing as standalone ⋯ rows on iOS.
        case "bookmarkAdd", "editOutline":
            EmptyView()
        case "draw":
            Button { toggleDraw() } label: { Label(c.active ? "Stop Drawing" : "Draw", systemImage: sym) }
        // Mode-specific rows that collapse here on a phone (inline on iPad via `barControl`).
        case "textSmaller":
            Button { smallerText() } label: { Label("Smaller Text", systemImage: sym) }
        case "textLarger":
            Button { biggerText() } label: { Label("Larger Text", systemImage: sym) }
        case "zoomOut":
            Button { zoomPdf(by: 1 / 1.25) } label: { Label("Zoom Out", systemImage: sym) }
        case "zoomIn":
            Button { zoomPdf(by: 1.25) } label: { Label("Zoom In", systemImage: sym) }
        case "fitWidth":
            Button { fitPdfWidth() } label: { Label("Fit Width", systemImage: sym) }
        case "reflow":
            Button { toggleReflow() } label: { Label(c.active ? "Stop Reflow" : "Reflow to Text", systemImage: sym) }
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
        persistHistory()   // the back-stack survives closing/reopening the book
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

    /// Open the unified Contents panel on `tab`, loading BOTH the bookmark list and the outline draft
    /// (both stores are local-first, so this returns instantly and reconciles in the background).
    private func openContents(_ tab: ContentsTab) {
        contentsTab = tab
        contentsEditing = false            // open in view/jump mode; Edit reveals the editing affordances
        renamingBookmark = nil
        showContents = true
        Task {
            await reloadBookmarks()
            let authored = (try? await outline.pull(publicationId: pubId)) ?? []
            editor.open(authored: authored, embedded: reader?.outline() ?? [])   // emits `outline.open …`
        }
    }

    /// (Re)load the synced bookmark list into `bookmarkItems`, in **page order** (the default, matching
    /// the outline) — by the locator's page for a PDF, else reading fraction for an EPUB.
    private func reloadBookmarks() async {
        if let result = try? await bookmarks.pull(publicationId: pubId, since: 0) {
            bookmarkItems = result.ops.filter { $0.deletedAt == nil }
                .sorted { Self.bookmarkOrder($0) < Self.bookmarkOrder($1) }
        }
    }

    /// Reading-order sort key: the PDF page number when present (so bookmarks order by page like the
    /// outline), else the EPUB progress fraction. Both are monotonic in reading order within one book.
    private static func bookmarkOrder(_ bm: Bookmark) -> Double {
        if let page = bm.locator?.locations.page { return Double(page) }
        return bm.fraction ?? 0
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

    /// The page/position line shown in small font under a bookmark's name (e.g. "Page 42" / "37%").
    private static func bookmarkPageLabel(_ bm: Bookmark) -> String {
        if let loc = bm.locator { let s = label(for: loc); if !s.isEmpty { return s } }
        if let f = bm.fraction { return "\(Int((f * 100).rounded()))%" }
        return ""
    }

    /// The unified Contents popup (PDF-Expert style): one sheet, a segmented Bookmarks | Outline picker,
    /// and a tab-adaptive toolbar (Add Bookmark on Bookmarks; Save / Save into PDF on Outline).
    private var contentsSheet: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("View", selection: $contentsTab) {
                    Text("Bookmarks").tag(ContentsTab.bookmarks)
                    Text("Outline").tag(ContentsTab.outline)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal).padding(.vertical, 8)
                Divider()
                switch contentsTab {
                case .bookmarks:
                    bookmarksListContent
                case .outline:
                    if contentsEditing { outlineEditorContent } else { outlineViewContent }
                }
            }
            .navigationTitle("Contents")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { contentsToolbar }
            // Leaving the Outline tab while editing → sync the outline (so a tab switch never drops edits).
            .onChange(of: contentsTab) { oldTab, _ in
                if oldTab == .outline && contentsEditing { syncOutline() }
            }
            .confirmationDialog("Clear all bookmarks for this book?", isPresented: $showClearBookmarks,
                                titleVisibility: .visible) {
                Button("Clear All", role: .destructive) { Task { await clearBookmarks() } }
            }
            .alert(renamingBookmark == nil ? "Name this bookmark" : "Rename bookmark",
                   isPresented: $showBookmarkNameDialog) {
                TextField("Name", text: $bookmarkNameDraft)
                Button(renamingBookmark == nil ? "Add" : "Rename") { Task { await commitBookmarkName() } }
                Button("Cancel", role: .cancel) { pendingBookmarkLocator = nil; renamingBookmark = nil }
            } message: {
                Text("A name helps you find it later.")
            }
        }
    }

    /// Toolbar: Close (which also syncs a pending outline edit) + the Edit/Done toggle + per-tab actions.
    @ToolbarContentBuilder private var contentsToolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) { Button("Close") { closeContents() } }
        ToolbarItem(placement: .topBarTrailing) {
            Button(contentsEditing ? "Done" : "Edit") { toggleEditing() }
        }
        if contentsTab == .bookmarks {
            ToolbarItemGroup(placement: .bottomBar) {
                Button { beginAddBookmark() } label: { Label("Add Bookmark", systemImage: "bookmark") }
                Spacer()
                if contentsEditing && !bookmarkItems.isEmpty {
                    Button("Clear All", role: .destructive) { showClearBookmarks = true }
                }
            }
        } else {
            ToolbarItem(placement: .bottomBar) {
                Button { Task { await saveOutlineIntoPdf() } } label: {
                    Label("Save into PDF", systemImage: "square.and.arrow.down")
                }
            }
        }
    }

    /// Enter/leave edit mode. Leaving edit on the Outline tab syncs the outline (Done = apply).
    private func toggleEditing() {
        if contentsEditing { syncOutline() }   // was editing → apply on Done
        contentsEditing.toggle()
    }

    /// Close the panel, applying a pending outline edit first so Close never silently drops edits.
    private func closeContents() {
        syncOutline()
        showContents = false
    }

    /// Push the current outline draft to the store (only meaningful while editing the Outline tab).
    private func syncOutline() {
        guard contentsEditing, contentsTab == .outline else { return }
        let entries = editor.cleaned()             // emits `outline.save …`
        Task { try? await outline.push(publicationId: pubId, entries: entries) }
    }

    @ViewBuilder private var bookmarksListContent: some View {
        if bookmarkItems.isEmpty {
            ContentUnavailableView("No bookmarks", systemImage: "bookmark",
                                   description: Text("Add one with “Add Bookmark” below while reading."))
        } else {
            List {
                ForEach(bookmarkItems) { bm in
                    Button {
                        // View mode → jump; Edit mode → rename (tap opens the name dialog for this one).
                        if contentsEditing { beginRenameBookmark(bm) }
                        else { if let loc = bm.locator { jump(to: loc) }; showContents = false }
                    } label: {
                        let name = Self.bookmarkLabel(bm)
                        let page = Self.bookmarkPageLabel(bm)
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(name)
                                if !page.isEmpty, page != name {       // page in small font (no date); skip if redundant
                                    Text(page).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            if contentsEditing {
                                Spacer()
                                Image(systemName: "pencil").foregroundStyle(.secondary)   // Edit mode → tap renames
                            }
                        }
                    }
                }
                .onDelete { idx in
                    let targets = idx.map { bookmarkItems[$0] }
                    Task { for bm in targets { await deleteBookmark(bm) } }
                }
            }
            .environment(\.editMode, .constant(contentsEditing ? .active : .inactive))
        }
    }

    /// The Outline tab in VIEW mode — tap a row to jump to its page (PDF-Expert "Contents"); page in
    /// small font. Edit reveals `outlineEditorContent` for add/rename/reorder/delete.
    @ViewBuilder private var outlineViewContent: some View {
        if editor.entries.isEmpty {
            ContentUnavailableView("No outline", systemImage: "list.bullet.indent",
                                   description: Text("Tap Edit to add contents entries."))
        } else {
            List {
                ForEach(editor.entries.indices, id: \.self) { i in
                    Button {
                        jump(to: Locator(publicationId: pubId, format: .pdf,
                                         locations: .init(page: editor.entries[i].page)))
                        showContents = false
                    } label: {
                        HStack {
                            if editor.entries[i].level > 1 {
                                Spacer().frame(width: CGFloat((editor.entries[i].level - 1) * 16))
                            }
                            Text(editor.entries[i].title.isEmpty ? "Untitled" : editor.entries[i].title)
                            Spacer()
                            Text("Page \(editor.entries[i].page)").font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
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
        ReaderLog.annotations.info("[\(vid, privacy: .public)] pullMarks[\(pubId, privacy: .public)] reset=\(reset) pulled=\(result.ops.count) total=\(marksById.count)")
        renderMarks()
    }

    /// Drain the bookmark outbox on the same cadence as marks (open / foreground / poll). A `pull` folds
    /// in another device's bookmarks AND flushes anything made offline — so an offline bookmark reaches
    /// the server on reconnect without waiting for the bookmark-list panel to open. Result is ignored
    /// (the list re-reads from the store when the panel opens); this call is purely the sync tick.
    private func flushBookmarks() async {
        guard reader != nil else { return }
        _ = try? await bookmarks.pull(publicationId: pubId, since: 0)
    }

    /// Re-render the live (non-tombstoned) marks from the merged set. Preserves the current reading
    /// position — a cross-device mark appearing must never reposition the reader.
    private func renderMarks(caller: String = #function) {
        marks = marksById.values.filter { !$0.isTombstone }
        // Notes render as a native SwiftUI `MarkerOverlay` layer (drag/expand/delete), not through the
        // decoration host — so keep them out of the mark set the host paints.
        noteMarks = marks.filter { $0.kind == .note }
        let hosted = marks.filter { $0.kind != .note }
        let hl = hosted.filter { $0.kind == .highlight }.count
        let ul = hosted.filter { $0.kind == .underline }.count
        let ink = hosted.filter { $0.kind == .ink }.count
        ReaderLog.annotations.info("[\(vid, privacy: .public)] renderMarks(from \(caller, privacy: .public)): hosted=\(hosted.count) (hl=\(hl) ul=\(ul) ink=\(ink)) notes=\(noteMarks.count) marksById=\(marksById.count) layer=\(self.renderLayer != nil)")
        renderLayer?.render(hosted)   // marks + ink, via the per-format rendering layer
        // The render layer adds text marks (which re-tile) and THEN ink (which doesn't repaint itself), so
        // a fresh/restored view leaves ink — and marks added before layout — unpainted. Re-tile once here
        // so both paint. Skipped while drawing (live strokes repaint on their own; a re-tile per stroke janks).
        if let pv = pdfNavigator?.pdfView, !drawMode { pv.layoutDocumentView() }
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

    /// Format-polymorphic highlight/underline: EPUB anchors to the selection's `cfiRange` (reflows with the
    /// text), PDF to per-line quads. The shared `highlight`/`underline` controls route here so one menu row
    /// works on both formats.
    private func doHighlight() async {
        if epubNavigator != nil { if let sel = epubSelection { await addEpubTextMark(.highlight, sel) } }
        else { await addTextMark(.highlight) }
    }
    private func doUnderline() async {
        if epubNavigator != nil { if let sel = epubSelection { await addEpubTextMark(.underline, sel) } }
        else { await addTextMark(.underline) }
    }

    /// EPUB text mark from the current selection — anchored by **cfiRange** (not quads), rendered via
    /// `EpubDecorationHost` (epub.js annotations). Highlight/underline only; strike/note are PDF-only.
    /// **Toggles**: re-applying the same kind to the same selection removes it instead of stacking.
    /// Create an EPUB highlight/underline over `sel` (the selection is passed IN from the bar button,
    /// captured when the bar was shown — so a stray webview tap that clears `epubSelection` can't null
    /// it out before this runs). Then clear the webview selection (PDF does the analogous clearSelection)
    /// so the blue selection doesn't linger. The locator falls back to the selection CFI if the current
    /// reading position isn't known yet — the `cfiRange` is what anchors an EPUB mark, not the locator.
    private func addEpubTextMark(_ kind: AnnotationKind, _ sel: (cfiRange: String, text: String)) async {
        guard !sel.cfiRange.isEmpty else { return }
        epubSelection = nil
        await epubNavigator?.clearSelection()
        let existing = marks.filter { $0.kind == kind && !$0.isTombstone && $0.cfiRange == sel.cfiRange }
        if !existing.isEmpty { await toggleOff(existing); return }
        let now = Date()
        let loc = currentLocation ?? Locator(publicationId: pubId, format: .epub, locations: .init(cfi: sel.cfiRange))
        let mark = Annotation(publicationId: pubId, kind: kind, locator: loc,
                              cfiRange: sel.cfiRange,
                              color: kind == .highlight ? highlightHex : "#ff3b30",
                              createdAt: now, updatedAt: now, rev: rev + 1)
        marksById[mark.id] = mark
        renderMarks()   // optimistic
        ink.record(.added([mark.id]))
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)
    }

    /// Anchor a text mark (highlight/underline/strikeout) to the current selection's per-line quads and
    /// push it. One `Annotation` per page the selection spans. **Toggles**: if the selection already
    /// carries this kind (overlapping quads on the page), it's removed instead of stacking another. The
    /// structured store — not the file — is the source of truth, so the mark reaches web/export in the
    /// same `rect` quad shape.
    private func addTextMark(_ kind: AnnotationKind) async {
        let groups = selectionQuads()
        guard !groups.isEmpty else { return }
        // Toggle off: existing marks of this kind whose quads intersect the selection on the same page.
        let existing = marks.filter { m -> Bool in
            guard m.kind == kind, !m.isTombstone, let mq = m.quads else { return false }
            return groups.contains { g in
                guard m.locator.locations.page == g.page + 1 else { return false }
                let selRects = g.quads.map { NormRect(x: $0[0], y: $0[1], w: $0[2], h: $0[3]) }
                return mq.contains { q in
                    let mr = NormRect(x: q[0], y: q[1], w: q[2], h: q[3])
                    return selRects.contains { $0.intersects(mr) }
                }
            }
        }
        if !existing.isEmpty {
            pdfNavigator?.pdfView.clearSelection()
            await toggleOff(existing)
            return
        }
        let now = Date()
        var created: [Annotation] = []
        for g in groups {
            let loc = Locator(publicationId: pubId, format: .pdf, locations: .init(page: g.page + 1))
            let mark = Annotation(publicationId: pubId, kind: kind, locator: loc,
                                  quads: g.quads,
                                  color: kind == .highlight ? highlightHex : "#ff3b30",
                                  createdAt: now, updatedAt: now, rev: rev + 1)
            marksById[mark.id] = mark; created.append(mark)
        }
        pdfNavigator?.pdfView.clearSelection()
        renderMarks()   // optimistic
        ink.record(.added(created.map(\.id)))
        for m in created { _ = try? await annotations.push(publicationId: pubId, ops: [m]) }
        await pullMarks(reset: false)
    }

    /// Tombstone marks as one undo step — the toggle-off path for text marks (and general mark removal).
    private func toggleOff(_ hits: [Annotation]) async {
        guard !hits.isEmpty else { return }
        let now = Date()
        var ids: [UUID] = []
        for var m in hits {
            m.deletedAt = now; m.updatedAt = now; m.rev = m.rev + 1
            marksById[m.id] = m; ids.append(m.id)
        }
        renderMarks()   // optimistic
        ink.record(.removed(ids))
        for id in ids where marksById[id] != nil {
            _ = try? await annotations.push(publicationId: pubId, ops: [marksById[id]!])
        }
    }

    /// Begin adding a note anchored to the current selection — PDF by page + the first line's top-left
    /// point, EPUB by the selection's CFI — then prompt for text.
    private func beginNote(_ captured: (cfiRange: String, text: String)? = nil) {
        noteEditing = nil
        noteDraft = ""
        if epubNavigator != nil {
            guard let sel = captured ?? epubSelection, !sel.cfiRange.isEmpty else { return }
            epubSelection = nil
            Task { await epubNavigator?.clearSelection() }
            noteAnchor = nil; noteAnchorCfi = sel.cfiRange
            noteAnchorText = sel.text.isEmpty ? nil : sel.text   // the quote → Web Annotation TextQuote selector
            showNoteEntry = true
            return
        }
        guard let g = selectionQuads().first, let first = g.quads.first else { return }
        noteAnchor = (page: g.page, point: [first[0], first[1]]); noteAnchorCfi = nil; noteAnchorText = nil
        showNoteEntry = true
    }

    /// Open the text editor for an EXISTING note (the marker popover's "Edit").
    private func editNote(_ note: Annotation) {
        noteEditing = note
        noteDraft = note.noteText ?? ""
        noteAnchor = nil; noteAnchorCfi = nil
        showNoteEntry = true
    }

    private func commitNote() async {
        guard !noteDraft.isEmpty else { return }
        let now = Date()
        // Editing an existing note: update its text in place.
        if let existing = noteEditing {
            var m = existing
            m.noteText = noteDraft; m.updatedAt = now; m.rev = rev + 1
            marksById[m.id] = m; noteEditing = nil
            renderMarks()
            _ = try? await annotations.push(publicationId: pubId, ops: [m])
            await pullMarks(reset: false)
            return
        }
        // A new note: EPUB anchors by CFI (+ a zero offset within the block); PDF by page + point.
        let mark: Annotation
        if let cfi = noteAnchorCfi {
            // Anchor the note to its CFI and keep the quoted text — the two together are the Web Annotation
            // (FragmentSelector + TextQuoteSelector) so an exported note re-anchors in another reader.
            let loc = Locator(publicationId: pubId, format: .epub, locations: .init(cfi: cfi),
                              text: noteAnchorText.map { Locator.Text(highlight: $0) })
            mark = Annotation(publicationId: pubId, kind: .note, locator: loc, cfiRange: cfi,
                              region: [0, 0], color: "#ffd54a", noteText: noteDraft,
                              createdAt: now, updatedAt: now, rev: rev + 1)
            epubSelection = nil; noteAnchorText = nil
        } else if let anchor = noteAnchor {
            let loc = Locator(publicationId: pubId, format: .pdf, locations: .init(page: anchor.page + 1))
            mark = Annotation(publicationId: pubId, kind: .note, locator: loc, region: anchor.point,
                              color: "#ffd54a", noteText: noteDraft, createdAt: now, updatedAt: now, rev: rev + 1)
            pdfNavigator?.pdfView.clearSelection()
        } else { return }
        marksById[mark.id] = mark
        renderMarks()
        ink.record(.added([mark.id]))
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)
    }

    /// Persist a note the user dragged to a new spot. The `NoteAnchor` adapter already updated the note's
    /// location fields (PDF page+point / EPUB CFI+offset); the reader just stamps the revision and pushes —
    /// one path for both formats.
    private func persistNoteMove(_ moved: Annotation) async {
        guard marksById[moved.id] != nil else { return }
        var m = moved
        m.updatedAt = Date(); m.rev = rev + 1
        marksById[m.id] = m
        renderMarks()
        _ = try? await annotations.push(publicationId: pubId, ops: [m])
    }

    /// Export this book's notes as a **W3C Web Annotation** JSON array — an open, standard-shaped file for
    /// getting notes out (or into a Readium-style tool). Not automatic interop with other apps: EPUB has no
    /// note-exchange every viewer implements, so this is a portable format, not a guarantee.
    private func exportNotes() async {
        let anns = noteMarks.compactMap { $0.webAnnotation() }
        guard !anns.isEmpty else { exportMessage = "No notes to export yet."; return }
        do {
            let enc = JSONEncoder()
            enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            let data = try enc.encode(anns)
            let safe = title.replacingOccurrences(of: "/", with: "-")
            let out = FileManager.default.temporaryDirectory
                .appendingPathComponent("\(safe.isEmpty ? "book" : safe)-notes.json")
            try data.write(to: out, options: .atomic)
            shareItem = ShareItem(url: out)
        } catch { exportMessage = "Couldn’t write the notes file." }
    }

    /// Import notes from a Web Annotation JSON file (an array, or a single object). Each anchored note is
    /// merged in and pushed to the store like any local edit.
    private func importNotes(from url: URL) async {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else { exportMessage = "Couldn’t read that file."; return }
        let dec = JSONDecoder()
        let list: [WebAnnotation]
        if let arr = try? dec.decode([WebAnnotation].self, from: data) { list = arr }
        else if let one = try? dec.decode(WebAnnotation.self, from: data) { list = [one] }
        else { exportMessage = "That isn’t a Web Annotation file."; return }
        let now = Date()
        var imported = 0
        for w in list {
            guard var m = Annotation(webAnnotation: w, publicationId: pubId) else { continue }
            m.updatedAt = now; m.rev = rev + 1
            marksById[m.id] = m
            _ = try? await annotations.push(publicationId: pubId, ops: [m])
            imported += 1
        }
        if imported > 0 { renderMarks() } else { exportMessage = "No importable notes found in that file." }
    }

    /// Tombstone a note (long-press → Delete) so the removal propagates to the server and other devices.
    private func deleteNote(_ note: Annotation) async {
        guard var m = marksById[note.id] else { return }
        let now = Date()
        m.deletedAt = now; m.updatedAt = now; m.rev = m.rev + 1
        marksById[m.id] = m
        renderMarks()
        _ = try? await annotations.push(publicationId: pubId, ops: [m])
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

    /// The draw palette — the SAME floating-panel component as the pinned bars: `FloatingPanel`
    /// (positioning + on-screen clamping) wrapping `PanelChrome` (dual move grips + axis + background),
    /// with `InkToolbar` supplying only the palette's items. So the palette inherits identical chrome.
    private var paletteFloating: some View {
        FloatingPanel(placement: palette.placement,
                      onDrag: { palette.drag(toX: $0, y: $1) },
                      onDragEnd: { nx, ny in
                          palette.endDrag(atX: nx, y: ny)
                          if case .docked(let edge) = palette.placement,
                             dockCoordinator.isOccupied(edge, excluding: "ink-palette") {
                              palette.drag(toX: nx, y: ny)   // edge taken → stay floating
                          }
                      },
                      coordinateSpace: "ink-palette",
                      dock: dockCoordinator, dockID: "ink-palette", dockAnchor: .center, dockOrder: 0) { handlers in
            PanelChrome(handlers: handlers, axis: palette.axis) {
                InkToolbar(tool: $ink, palette: palette, canUndo: ink.canUndo, canRedo: ink.canRedo,
                           onUndo: undoInk, onRedo: redoInk, onDone: { drawMode = false })
            }
        }
    }

    /// A finished stroke from the canvas. An eraser stroke deletes the ink it crosses; a pen/highlighter
    /// stroke is persisted as a `kind:.ink` annotation and re-rendered through `PdfInkHost` (the
    /// canonical renderer) — the same record web/export use. Both are recorded for undo.
    private func addInk(_ stroke: InkStroke) async {
        ReaderLog.annotations.info("[\(vid, privacy: .public)] ✍️ addInk mode=\(String(describing: stroke.mode), privacy: .public) points=\(stroke.points.count)")
        if stroke.mode == .erase { await eraseInk(with: stroke); return }
        guard let locator = currentLocation,
              let mark = InkCapture.annotation(ink: Ink(strokes: [stroke]), locator: locator,
                                               publicationId: pubId, rev: rev + 1, now: Date())
        else { ReaderLog.annotations.info("[\(vid, privacy: .public)] ✍️ addInk ABORTED (no locator/annotation)"); return }
        marksById[mark.id] = mark
        ReaderLog.annotations.info("[\(vid, privacy: .public)] ✍️ addInk DRAW → mark added, rendering (marksById=\(marksById.count))")
        renderMarks()   // optimistic: show immediately, even offline
        ink.record(.added([mark.id]))
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
        ReaderLog.annotations.info("[\(vid, privacy: .public)] 🧹 eraseInk page=\(page) hits=\(hits.count) inkMarks=\(marks.filter { $0.kind == .ink && !$0.isTombstone }.count)")
        guard !hits.isEmpty else { return }
        let now = Date()
        var removed: [UUID] = []
        var tombstones: [Annotation] = []
        for var m in hits {
            m.deletedAt = now; m.updatedAt = now; m.rev = m.rev + 1
            marksById[m.id] = m; removed.append(m.id); tombstones.append(m)
        }
        // OPTIMISTIC: repaint immediately, BEFORE the network. `annotations.push` awaits the server flush,
        // which on a flaky connection times out for ~30-60s — doing it first left the erase unpainted that
        // whole time (the redraw was blocked behind the timeout). `addInk` already renders before pushing.
        ReaderLog.annotations.info("[\(vid, privacy: .public)] 🧹 eraseInk tombstoned=\(removed.count) → rendering (before push)")
        renderMarks()
        ink.record(.removed(removed))
        for m in tombstones { _ = try? await annotations.push(publicationId: pubId, ops: [m]) }
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
        // Bring the edit that was just undone/redone into view — but only when it's off-screen, so an
        // in-view edit doesn't cause a disorienting jump. The mutation carries only ids; resolve the
        // representative mark from the store (tombstones are kept, so a removal still has its locator).
        if let target = ids.lazy.compactMap({ marksById[$0] }).first {
            await reveal(target)
        }
    }

    /// Scroll `mark`'s location into view iff it isn't already visible. PDF: compare the mark's page
    /// against `visiblePages`. EPUB: a nil block rect means the anchor CFI isn't on the current page.
    /// Navigation goes through `goTo` directly (not `jump`) so it doesn't push the back stack — an
    /// undo/redo reveal is not a user jump.
    private func reveal(_ mark: Annotation) async {
        if let pdfView = pdfNavigator?.pdfView, let doc = pdfView.document {
            let idx = mark.locator.locations.position
                ?? mark.locator.locations.page.map { max(0, $0 - 1) }
            guard let i = idx, i >= 0, i < doc.pageCount, let page = doc.page(at: i) else { return }
            guard !pdfView.visiblePages.contains(page) else { return }   // already on-screen
            try? await reader?.goTo(mark.locator)
        } else if let nav = epubNavigator {
            // The precise anchor for EPUB ink is `cfiRange`; the mark's `locator` only holds the
            // page-start CFI captured at draw time.
            guard let cfi = mark.cfiRange ?? mark.locator.locations.cfi, !cfi.isEmpty else { return }
            guard await nav.rect(forCfi: cfi) == nil else { return }      // already on the current page
            try? await reader?.goTo(Locator(publicationId: pubId, format: .epub,
                                            locations: .init(cfi: cfi)))
        }
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

    // MARK: Authored outline (persistent PDF TOC) — edit → sync → optionally bake into the file

    /// The Outline tab's content — an editable list of entries seeded from the authored outline (or, if
    /// none yet, the file's own embedded outline), with add/delete/reorder. The Contents panel supplies
    /// the nav bar + the Save / Save-into-PDF toolbar (see `contentsToolbar`).
    @ViewBuilder private var outlineEditorContent: some View {
        List {
            Section {
                ForEach(editor.entries.indices, id: \.self) { i in
                    HStack(spacing: 8) {
                        if editor.entries[i].level > 1 {
                            Spacer().frame(width: CGFloat((editor.entries[i].level - 1) * 16))
                        }
                        TextField("Title", text: Binding(
                            get: { i < editor.entries.count ? editor.entries[i].title : "" },
                            set: { editor.setTitle($0, at: i) }))
                            .focused($focusedOutlineEntry, equals: i)
                        Stepper(value: Binding(
                            get: { i < editor.entries.count ? editor.entries[i].page : 1 },
                            set: { editor.setPage($0, at: i) }),
                            in: 1...1_000_000) { EmptyView() }
                            .labelsHidden()
                        Text("Page \(editor.entries[i].page)").font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
                .onDelete { editor.delete(at: $0) }
                .onMove { editor.move(from: $0, to: $1) }
                Button {
                    editor.add(atPage: pdfNavigator?.currentLocation?.locations.page ?? 1)
                    focusedOutlineEntry = editor.entries.count - 1   // jump the cursor into the new title
                } label: {
                    Label("Add Entry", systemImage: "plus")
                }
            } footer: {
                Text("Each entry points at a page. Save syncs the outline to your other devices; Save into PDF writes a copy any reader (Preview, Acrobat) shows.")
            }
        }
        .environment(\.editMode, .constant(.active))   // outline tab is always editable (move/delete handles)
    }

    /// Sync the outline, then ask the server to bake it into a PDF copy and share it.
    private func saveOutlineIntoPdf() async {
        try? await outline.push(publicationId: pubId, entries: editor.cleaned())
        var req = URLRequest(url: ReaderRoutes.outlinedPdf(baseURL: endpoint.baseURL, holding: holding.holdingId))
        endpoint.authorize(&req)
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            let outcome = editor.bakeResult(status: code, bytes: data.count)   // emits `outline.bake …`
            guard outcome == "ok" else {
                exportMessage = outcome == "empty" ? "Add an outline entry first, then save into the PDF."
                    : (outcome == "forbidden" ? "You need edit access to save into the PDF."
                       : "The server couldn’t produce the file (\(code)).")
                return
            }
            let safe = title.replacingOccurrences(of: "/", with: "-")
            let out = FileManager.default.temporaryDirectory
                .appendingPathComponent("\(safe.isEmpty ? "book" : safe)-outlined.pdf")
            try data.write(to: out, options: .atomic)
            showContents = false
            shareItem = ShareItem(url: out)
        } catch {
            exportMessage = "Couldn’t reach the server to save into the PDF."
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
        ink.record(.added([mark.id]))
        renderMarks()   // optimistic
        _ = try? await annotations.push(publicationId: pubId, ops: [mark])
        await pullMarks(reset: false)
    }

    /// Begin adding a bookmark at the current location — captures the spot and opens a name dialog
    /// pre-filled with the page (e.g. "Page 42") so the user can rename it before it's saved.
    private func beginAddBookmark() {
        guard let locator = currentLocation else { return }
        renamingBookmark = nil
        pendingBookmarkLocator = locator
        let page = Self.label(for: locator)
        bookmarkNameDraft = page.isEmpty ? "Bookmark" : page
        showBookmarkNameDialog = true
    }

    /// Begin renaming an existing bookmark (Edit mode → tap a row) — the same dialog, pre-filled.
    private func beginRenameBookmark(_ bm: Bookmark) {
        renamingBookmark = bm
        pendingBookmarkLocator = nil
        bookmarkNameDraft = Self.bookmarkLabel(bm)
        showBookmarkNameDialog = true
    }

    /// Commit the name dialog: rename the target bookmark, or create a new one at the captured spot
    /// (falls back to the page if left blank). Both sync via the `BookmarkStore` and refresh the list.
    private func commitBookmarkName() async {
        let typed = bookmarkNameDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let now = Date()
        let op: Bookmark
        if let bm = renamingBookmark {
            var updated = bm
            updated.label = typed.isEmpty ? nil : typed
            updated.updatedAt = now; updated.rev = bm.rev + 1
            op = updated
        } else if let locator = pendingBookmarkLocator {
            op = Bookmark(publicationId: pubId, locator: locator,
                          fraction: locator.locations.progression,
                          label: typed.isEmpty ? nil : typed,
                          createdAt: now, updatedAt: now, rev: rev + 1)
        } else { return }
        renamingBookmark = nil; pendingBookmarkLocator = nil
        _ = try? await bookmarks.push(publicationId: pubId, ops: [op])
        await reloadBookmarks()
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
