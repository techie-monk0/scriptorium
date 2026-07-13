import SwiftUI

/// Typography port of the PWA's type system. `-apple-system` *is* the iOS system font, so the PWA's
/// 16px/1.45 body maps to Dynamic Type `.body`; counts/progress use tabular figures so digits don't
/// jitter (the PWA uses `font-variant-numeric: tabular-nums`).
public enum Typography {
    public static let body: Font = .body
    public static let title: Font = .title2.weight(.semibold)
    public static let sectionHeader: Font = .headline
    public static let caption: Font = .caption
    /// Use for counts, progress %, ISBNs — anything where digit columns should stay aligned.
    public static let tabular: Font = .body.monospacedDigit()
}

public extension View {
    /// Apply tabular (monospaced) digits — the SwiftUI equivalent of `tabular-nums`.
    func tabularNumbers() -> some View { self.monospacedDigit() }
}
