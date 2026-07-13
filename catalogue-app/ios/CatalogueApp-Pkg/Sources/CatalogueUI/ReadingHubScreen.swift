#if canImport(UIKit)
import SwiftUI
import CatalogueCore
import CatalogueReader

/// The "Read" tab — a reading hub. Lists the currently-open books (the reader tabs, from
/// `OpenSessionsStore`) and opens the multi-book `ReaderShell` on tap; a prominent "Continue reading"
/// resumes the most recent. Adding `read` to the shared `APP_SECTIONS` manifest is what makes this a
/// primary tab on iOS (and pushes `Text`/`content` into the ⋯ overflow) — the same manifest web/PWA read.
struct ReadingHubScreen: View {
    @Environment(AppModel.self) private var app
    @State private var books: [OpenBook] = []
    @State private var toOpen: OpenBook?

    var body: some View {
        Group {
            if books.isEmpty {
                ContentUnavailableView("No open books", systemImage: "book",
                    description: Text("Open a book from Home or Books to start reading — it’ll appear here."))
            } else {
                List {
                    if let recent = books.first {
                        Section {
                            Button { toOpen = recent } label: {
                                Label { Text("Continue reading").font(.headline) }
                                    icon: { Image(systemName: "book.fill") }
                            }
                        }
                    }
                    Section("Open books") {
                        ForEach(books) { b in
                            Button { toOpen = b } label: {
                                HStack {
                                    Text(b.title).lineLimit(1)
                                    Spacer()
                                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Read")
        .onAppear { Task { await refresh() } }
        .refreshable { await refresh() }
        .fullScreenCover(item: $toOpen) { book in
            ReaderShell(open: book, store: app.openSessions, endpoint: app.endpoint,
                        readingStore: app.readingStore,
                        starAccessory: { e in e.map { AnyView(StarButton(eid: $0).environment(app)) }
                            ?? AnyView(EmptyView()) })
        }
    }

    private func refresh() async { books = await app.openSessions.list() }
}
#endif
