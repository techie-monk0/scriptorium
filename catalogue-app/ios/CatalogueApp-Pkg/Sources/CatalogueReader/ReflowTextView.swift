#if canImport(UIKit)
import SwiftUI

/// PDF "reflow to text" reading surface (GoodReader style): the current page's extracted text as
/// formatted paragraphs, comfortable to read on a phone. Themed to the active reading theme + a
/// dedicated reflow font size; the bottom bar turns the page (which re-extracts).
struct ReflowTextView: View {
    let paragraphs: [String]
    let bg: Color
    let fg: Color
    let fontSize: Double
    let pageLabel: String
    let onPrev: () -> Void
    let onNext: () -> Void

    var body: some View {
        ZStack {
            bg.ignoresSafeArea()
            if paragraphs.isEmpty {
                ContentUnavailableView("No text on this page", systemImage: "doc.plaintext",
                                       description: Text("This page has no extractable text — it may be a scan."))
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: fontSize * 0.85) {
                        ForEach(Array(paragraphs.enumerated()), id: \.offset) { _, p in
                            Text(p)
                                .font(.system(size: fontSize))
                                .foregroundStyle(fg)
                                .lineSpacing(fontSize * 0.3)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(.horizontal, 22).padding(.vertical, 18)
                    .frame(maxWidth: 700).frame(maxWidth: .infinity)   // comfortable measure, centered
                }
            }
        }
        .overlay(alignment: .bottom) {
            HStack(spacing: 22) {
                Button(action: onPrev) { Image(systemName: "chevron.left").frame(width: 44, height: 44) }
                if !pageLabel.isEmpty { Text(pageLabel).font(.footnote.monospacedDigit()) }
                Button(action: onNext) { Image(systemName: "chevron.right").frame(width: 44, height: 44) }
            }
            .foregroundStyle(fg)
            .padding(.horizontal, 12)
            .background(.ultraThinMaterial, in: Capsule())
            .padding(.bottom, 12)
        }
    }
}
#endif
