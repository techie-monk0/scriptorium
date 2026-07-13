import SwiftUI

#if canImport(UIKit)
import UIKit
public typealias PlatformImage = UIImage
#elseif canImport(AppKit)
import AppKit
public typealias PlatformImage = NSImage
#endif

extension Image {
    /// Bridge a decoded platform image (UIKit/AppKit) into a SwiftUI `Image`.
    init(platformImage: PlatformImage) {
        #if canImport(UIKit)
        self.init(uiImage: platformImage)
        #else
        self.init(nsImage: platformImage)
        #endif
    }
}

/// Process-wide **cover cache + loader**. The reason `CachedImage` exists instead of SwiftUI's
/// `AsyncImage`:
///
///  • `AsyncImage` keeps NO decoded-image cache, so every time a cell scrolls back into view it
///    re-issues the network GET — the "Books keeps re-fetching covers I already have" bug.
///  • `AsyncImage` ties its request to the view's lifecycle; when a cover scrolls off-screen (or the
///    view is recomposed) mid-load its task is cancelled and never retried — the "some Home covers
///    stay blank" bug, made worse by Home's eager horizontal rails firing a burst of loads at once.
///
/// `CoverLoader` fixes both: an in-memory `NSCache` answers repeat requests with zero network, and
/// the fetch runs as a de-duplicated unstructured `Task` that completes (and fills the cache) even if
/// the view that kicked it off goes away — so the next appearance is an instant cache hit.
final class CoverCache: @unchecked Sendable {
    static let shared = CoverCache()
    private let cache = NSCache<NSURL, PlatformImage>()

    init() { cache.countLimit = 500 }

    func image(for url: URL) -> PlatformImage? { cache.object(forKey: url as NSURL) }
    func store(_ image: PlatformImage, for url: URL) { cache.setObject(image, forKey: url as NSURL) }
}

actor CoverLoader {
    static let shared = CoverLoader()
    private var inFlight: [URL: Task<PlatformImage?, Never>] = [:]
    private let session: URLSession

    init() {
        let cfg = URLSessionConfiguration.default
        // Content-addressed art (`/edition/<id>/cover.jpg`) is effectively immutable, so prefer the
        // on-disk cache when present — this survives app launches even when the server sends no
        // Cache-Control headers. `httpCookieStorage` defaults to `.shared`, so the signed session
        // cookie rides along exactly as it did through `URLSession.shared`.
        cfg.requestCachePolicy = .returnCacheDataElseLoad
        cfg.urlCache = URLCache(memoryCapacity: 16 << 20, diskCapacity: 256 << 20, diskPath: "covers")
        session = URLSession(configuration: cfg)
    }

    /// Return the decoded cover for `url`, sharing a single in-flight request across concurrent
    /// callers. The load is unstructured on purpose: a caller cancelling (its view scrolled away)
    /// must not abort the fetch, or the cover would never populate for the next appearance.
    func image(for url: URL) async -> PlatformImage? {
        if let cached = CoverCache.shared.image(for: url) { return cached }
        if let existing = inFlight[url] { return await existing.value }

        let task = Task<PlatformImage?, Never> { [session] in
            guard let (data, response) = try? await session.data(from: url) else { return nil }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) { return nil }
            guard let image = PlatformImage(data: data) else { return nil }
            CoverCache.shared.store(image, for: url)
            return image
        }
        inFlight[url] = task
        let result = await task.value
        inFlight[url] = nil
        return result
    }
}

/// Drop-in replacement for `AsyncImage` that renders through `CoverLoader` (in-memory cache +
/// de-duplicated, lifecycle-independent fetch + retry-on-reappear). The closure receives the same
/// `AsyncImagePhase` as `AsyncImage`, so call sites only swap the type name.
struct CachedImage<Content: View>: View {
    let url: URL?
    @ViewBuilder let content: (AsyncImagePhase) -> Content
    @State private var phase: AsyncImagePhase = .empty

    var body: some View {
        content(phase)
            // `.task(id:)` restarts on URL change AND re-runs each time the view reappears — so a
            // cover that failed/was cancelled the first time gets another shot, and a cover already
            // in the cache paints instantly (the synchronous peek below) with no network.
            .task(id: url) { await load() }
    }

    private func load() async {
        guard let url else { phase = .empty; return }
        if let cached = CoverCache.shared.image(for: url) {
            phase = .success(Image(platformImage: cached))
            return
        }
        if case .success = phase {} else { phase = .empty }   // keep a prior image while refreshing
        if let image = await CoverLoader.shared.image(for: url) {
            phase = .success(Image(platformImage: image))
        } else {
            phase = .failure(URLError(.cannotDecodeContentData))
        }
    }
}
