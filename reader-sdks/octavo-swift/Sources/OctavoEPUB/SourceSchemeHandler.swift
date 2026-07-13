#if canImport(WebKit)
import Foundation
import WebKit
import Octavo

/// Serves a WKWebView's custom-scheme requests — `octavo-epub:///book.epub` and epub.js's internal
/// resource fetches — from an injected `Source`. So the EPUB is read THROUGH the Source seam (disk
/// cache / HTTP range), never spilled to a temp file and never via a hardcoded route (decision D4).
/// `Range:` is honored through the pure, unit-tested `EpubRangeResponder`.
///
/// NOTE(device): the `WKURLSchemeTask` lifecycle — main-thread delivery, the stop-before-finish guard,
/// and Swift-6 Sendability of the non-Sendable task — needs simulator verification; it can't run
/// headless. The range/header math that IS unit-tested is `EpubRangeResponder`.
final class SourceSchemeHandler: NSObject, WKURLSchemeHandler, @unchecked Sendable {
    private let source: any Source
    private let contentType: String
    private let lock = NSLock()
    private var live: Set<ObjectIdentifier> = []           // tasks not yet finished/stopped

    init(source: any Source, contentType: String) {
        self.source = source
        self.contentType = contentType
    }

    func webView(_ webView: WKWebView, start task: any WKURLSchemeTask) {
        let id = ObjectIdentifier(task)
        lock.lock(); live.insert(id); lock.unlock()
        let header = task.request.value(forHTTPHeaderField: "Range")
        guard let url = task.request.url else { task.didFailWithError(URLError(.badURL)); return }
        let source = self.source, contentType = self.contentType
        nonisolated(unsafe) let task = task               // only touched on main, gated by `live`
        Task {
            do {
                let total = try await source.length()
                let range = EpubRangeResponder.parse(rangeHeader: header, total: total)
                let served = range ?? 0..<total
                let data = try await source.read(range: served)
                let response = HTTPURLResponse(
                    url: url, statusCode: range != nil ? 206 : 200, httpVersion: "HTTP/1.1",
                    headerFields: EpubRangeResponder.headers(contentType: contentType, total: total,
                                                             served: served, partial: range != nil))!
                await MainActor.run {
                    guard self.consume(id) else { return }    // stopped mid-flight → don't touch task
                    task.didReceive(response); task.didReceive(data); task.didFinish()
                }
            } catch {
                await MainActor.run {
                    guard self.consume(id) else { return }
                    task.didFailWithError(error)
                }
            }
        }
    }

    func webView(_ webView: WKWebView, stop task: any WKURLSchemeTask) {
        lock.lock(); live.remove(ObjectIdentifier(task)); lock.unlock()
    }

    /// Atomically take the task out of the live set; false if it was already stopped.
    private func consume(_ id: ObjectIdentifier) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return live.remove(id) != nil
    }
}
#endif
