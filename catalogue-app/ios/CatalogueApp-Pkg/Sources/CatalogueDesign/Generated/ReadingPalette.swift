// GENERATED from theme/palette.json by `python -m catalogue.webui.theme.gen` — DO NOT edit by hand.
// In-book reader themes (White/Sepia/Gray/Night); master is theme/palette.json `reading_themes`.
// Regenerate with `python -m catalogue.webui.theme.gen`; tests/test_palette_master.py guards drift.
import SwiftUI

/// A selectable in-book reading theme (one per `reading_themes` block in the master).
public enum ReadingTheme: String, CaseIterable, Sendable {
    case white
    case sepia
    case gray
    case night

    public static let `default`: ReadingTheme = .white

    /// The OS colour scheme this reading theme paints as (drives PDF night-invert).
    public var colorScheme: ColorScheme {
        switch self {
        case .white: return .light
        case .sepia: return .light
        case .gray: return .light
        case .night: return .dark
        }
    }

    public var isDark: Bool { colorScheme == .dark }
}

/// Every reading-theme token (raw value = the canonical token name shared with reader-themes.css).
public enum ReadingToken: String, CaseIterable, Sendable {
    case readerBg = "reader-bg"
    case readerFg = "reader-fg"
    case readerChromeBg = "reader-chrome-bg"
    case readerChromeFg = "reader-chrome-fg"
    case readerAccent = "reader-accent"
}

public enum ReadingPalette {
    /// token → hex ("#rrggbb"), per reading theme — ported 1:1 from the master.
    public static let hex: [ReadingTheme: [ReadingToken: String]] = [
        .white: [
            .readerBg: "#ffffff",
            .readerFg: "#11151b",
            .readerChromeBg: "#f2f3f5",
            .readerChromeFg: "#11151b",
            .readerAccent: "#1856c5",
        ],
        .sepia: [
            .readerBg: "#f4ecd8",
            .readerFg: "#5b4636",
            .readerChromeBg: "#e8ddc4",
            .readerChromeFg: "#5b4636",
            .readerAccent: "#9a6b3f",
        ],
        .gray: [
            .readerBg: "#d9d9de",
            .readerFg: "#1b1b1f",
            .readerChromeBg: "#c7c7cd",
            .readerChromeFg: "#1b1b1f",
            .readerAccent: "#3a4452",
        ],
        .night: [
            .readerBg: "#0c0d10",
            .readerFg: "#c9ccd1",
            .readerChromeBg: "#1b1b1f",
            .readerChromeFg: "#c9ccd1",
            .readerAccent: "#7db4ff",
        ],
    ]
}
