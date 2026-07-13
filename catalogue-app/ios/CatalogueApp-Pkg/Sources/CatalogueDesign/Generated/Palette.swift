// GENERATED from theme/palette.json by `python -m catalogue.webui.theme.gen` — DO NOT edit by hand.
// The language-neutral master is theme/palette.json; change a colour there and regenerate
// (`python -m catalogue.webui.theme.gen`). tests/test_palette_master.py asserts this file
// equals a fresh render, so a hand-edit here (or a palette.json change without regen) fails CI.
import SwiftUI

/// A selectable colour theme (one per palette in the master). `auto` (follow OS) is a runtime
/// preference, not a Theme; the renderer maps `auto` → the OS scheme before picking a Theme.
public enum Theme: String, CaseIterable, Sendable {
    case light
    case dark

    /// The default theme (matches palette.json `default`).
    public static let `default`: Theme = .light

    /// The OS colour scheme this theme paints as.
    public var colorScheme: ColorScheme {
        switch self {
        case .light: return .light
        case .dark: return .dark
        }
    }
}

/// Every colour token (raw value = the canonical token name shared with the web `tokens.css`).
public enum Token: String, CaseIterable, Sendable {
    case bg
    case fg
    case muted
    case border
    case surface
    case surface2 = "surface-2"
    case link
    case brand
    case navHover = "nav-hover"
    case navActiveBg = "nav-active-bg"
    case navActiveFg = "nav-active-fg"
    case cardBorder = "card-border"
    case subtleFg = "subtle-fg"
    case btnBg = "btn-bg"
    case btnFg = "btn-fg"
    case btnBorder = "btn-border"
    case btnHoverBg = "btn-hover-bg"
    case accent
    case ok
    case warn
}

public enum Palette {
    /// token → hex ("#rrggbb"), per theme — the single source of colour truth, ported 1:1.
    public static let hex: [Theme: [Token: String]] = [
        .light: [
            .bg: "#fbfcfd",
            .fg: "#11151b",
            .muted: "#5b6573",
            .border: "#cdd4dd",
            .surface: "#ffffff",
            .surface2: "#eef1f6",
            .link: "#1856c5",
            .brand: "#16202c",
            .navHover: "#e4ecfb",
            .navActiveBg: "#1856c5",
            .navActiveFg: "#ffffff",
            .cardBorder: "#cdd4dd",
            .subtleFg: "#38424f",
            .btnBg: "#eef1f6",
            .btnFg: "#11151b",
            .btnBorder: "#b9c1cc",
            .btnHoverBg: "#e1e6ee",
            .accent: "#1856c5",
            .ok: "#15803d",
            .warn: "#b45309",
        ],
        .dark: [
            .bg: "#0f1318",
            .fg: "#eef1f5",
            .muted: "#aab3c0",
            .border: "#2c343f",
            .surface: "#181d24",
            .surface2: "#212834",
            .link: "#7db4ff",
            .brand: "#dbe4f0",
            .navHover: "#232c38",
            .navActiveBg: "#2f6bd4",
            .navActiveFg: "#ffffff",
            .cardBorder: "#2c343f",
            .subtleFg: "#b6c0cd",
            .btnBg: "#252d39",
            .btnFg: "#eef1f5",
            .btnBorder: "#3a4452",
            .btnHoverBg: "#303a48",
            .accent: "#7db4ff",
            .ok: "#4ade80",
            .warn: "#fbbf24",
        ],
    ]
}
