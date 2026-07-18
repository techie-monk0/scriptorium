#if canImport(UIKit)
import SwiftUI
import CatalogueCore
import CatalogueData

/// The multi-book reader container (PDF Expert style): a **disappearable top tab bar** of open books
/// sitting above the ACTIVE book's `ReaderView`. Only the active book is mounted — one live engine; a
/// tab switch remounts via `.id(activeId)`, restoring position from the reading store. The tab bar
/// shares the reader's `showChrome`, so it hides on a center-tap and reappears with the chrome.
@MainActor
public struct ReaderShell: View {
    private let store: OpenSessionsStore
    private let endpoint: any ServerEndpoint
    private let readingStore: CatalogueReadingStore
    private let settingsStore: CatalogueReaderSettingsStore
    private let historyStore: ReaderHistoryStore
    private let starAccessory: (Int?) -> AnyView

    @State private var opening: OpenBook?          // the book this presentation was opened for
    @State private var books: [OpenBook] = []
    @State private var activeId: String?
    @State private var showChrome = true
    @Environment(\.dismiss) private var dismiss

    /// `open` may be nil — then the shell just shows the current active (most-recent) open book, or an
    /// empty state if none. That's the "Read tab → straight to the reader" path.
    public init(open book: OpenBook? = nil,
                store: OpenSessionsStore,
                endpoint: any ServerEndpoint,
                readingStore: CatalogueReadingStore,
                settingsStore: CatalogueReaderSettingsStore,
                historyStore: ReaderHistoryStore,
                starAccessory: @escaping (Int?) -> AnyView = { _ in AnyView(EmptyView()) }) {
        self.store = store
        self.endpoint = endpoint
        self.readingStore = readingStore
        self.settingsStore = settingsStore
        self.historyStore = historyStore
        self.starAccessory = starAccessory
        self._opening = State(initialValue: book)
    }

    private var active: OpenBook? { books.first { $0.pubId == activeId } ?? books.first }

    public var body: some View {
        VStack(spacing: 0) {
            if showChrome && books.count > 1 { tabBar }
            if let book = active {
                // `.id` ties the reader's lifetime to the active book: switching tabs tears down the
                // previous engine (WKWebView/PDFView) and mounts the next — one live engine at a time.
                ReaderView(holding: book.holding, title: book.title, endpoint: endpoint,
                           readingStore: readingStore, showChrome: $showChrome,
                           settingsStore: settingsStore, historyStore: historyStore,
                           topBarAccessory: starAccessory(book.eid))
                    .id(book.pubId)
            } else {
                ContentUnavailableView("No book open", systemImage: "book",
                    description: Text("Open a book from Home or Books — it’ll appear here."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task {
            if let opening { await store.open(opening) }   // focus (or add) the book this presentation opened
            await refresh()
        }
    }

    private var tabBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(books) { b in
                    let isActive = b.pubId == activeId
                    HStack(spacing: 6) {
                        Text(b.title).lineLimit(1).font(.footnote.weight(isActive ? .semibold : .regular))
                        Button { close(b) } label: { Image(systemName: "xmark").font(.caption2) }
                            .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .frame(maxWidth: 200)
                    .background(isActive ? Color.accentColor.opacity(0.22) : Color.gray.opacity(0.14),
                                in: Capsule())
                    .contentShape(Capsule())
                    .onTapGesture { activate(b) }
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
        }
        .background(.ultraThinMaterial)
    }

    private func refresh() async {
        books = await store.list()
        activeId = await store.activeId()
    }

    private func activate(_ b: OpenBook) {
        Task { await store.activate(b.pubId); await refresh() }
    }

    private func close(_ b: OpenBook) {
        Task {
            await store.close(b.pubId)
            await refresh()
            if books.isEmpty { dismiss() }   // closed the last tab → leave the reader
        }
    }
}
#endif
