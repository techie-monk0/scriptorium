import SwiftUI

/// A parsed colour. Kept as a pure value so hex parsing is testable without round-tripping through
/// a platform `Color`/`UIColor` (which is awkward to introspect on macOS during `swift test`).
public struct RGBA: Equatable, Sendable {
    public let r, g, b, a: Double
    public init(r: Double, g: Double, b: Double, a: Double = 1) { self.r = r; self.g = g; self.b = b; self.a = a }
}

/// Parse a `#rgb` / `#rrggbb` / `#rrggbbaa` hex string into linear 0…1 components. Returns nil on
/// malformed input so the caller decides the fallback (Tokens falls back to the default theme).
public func parseHex(_ hex: String) -> RGBA? {
    var s = Substring(hex)
    if s.first == "#" { s = s.dropFirst() }
    let chars = Array(s)
    func byte(_ lo: Int, _ hi: Int) -> Double? {
        guard let v = UInt8(String(chars[lo...hi]), radix: 16) else { return nil }
        return Double(v) / 255.0
    }
    switch chars.count {
    case 3:  // #rgb → expand each nibble
        guard let r = UInt8(String(chars[0]), radix: 16),
              let g = UInt8(String(chars[1]), radix: 16),
              let b = UInt8(String(chars[2]), radix: 16) else { return nil }
        return RGBA(r: Double(r * 17) / 255, g: Double(g * 17) / 255, b: Double(b * 17) / 255)
    case 6:
        guard let r = byte(0, 1), let g = byte(2, 3), let b = byte(4, 5) else { return nil }
        return RGBA(r: r, g: g, b: b)
    case 8:
        guard let r = byte(0, 1), let g = byte(2, 3), let b = byte(4, 5), let a = byte(6, 7) else { return nil }
        return RGBA(r: r, g: g, b: b, a: a)
    default:
        return nil
    }
}

public extension Color {
    /// A `Color` from a palette hex string. Falls back to a debug magenta on malformed input rather
    /// than crashing — a bad token should be visible, not fatal.
    init(hex: String) {
        let c = parseHex(hex) ?? RGBA(r: 1, g: 0, b: 1)
        self.init(.sRGB, red: c.r, green: c.g, blue: c.b, opacity: c.a)
    }
}

/// Semantic colour accessors for a resolved `Theme`. Every accessor maps to a `Token` in the
/// generated `Palette`; a token missing from the active theme falls back to the default theme (so a
/// half-themed surface never produces an undefined colour — mirrors the web `:root` fallback).
public struct Tokens: Sendable {
    public let theme: Theme
    public init(_ theme: Theme) { self.theme = theme }

    /// The hex string for a token in the active theme (default-theme fallback if absent).
    public func hex(_ token: Token) -> String {
        Palette.hex[theme]?[token] ?? Palette.hex[Theme.default]?[token] ?? "#ff00ff"
    }
    public func color(_ token: Token) -> Color { Color(hex: hex(token)) }

    public var bg: Color { color(.bg) }
    public var fg: Color { color(.fg) }
    public var muted: Color { color(.muted) }
    public var border: Color { color(.border) }
    public var surface: Color { color(.surface) }
    public var surface2: Color { color(.surface2) }
    public var link: Color { color(.link) }
    public var brand: Color { color(.brand) }
    public var navHover: Color { color(.navHover) }
    public var navActiveBg: Color { color(.navActiveBg) }
    public var navActiveFg: Color { color(.navActiveFg) }
    public var cardBorder: Color { color(.cardBorder) }
    public var subtleFg: Color { color(.subtleFg) }
    public var btnBg: Color { color(.btnBg) }
    public var btnFg: Color { color(.btnFg) }
    public var btnBorder: Color { color(.btnBorder) }
    public var btnHoverBg: Color { color(.btnHoverBg) }
    public var accent: Color { color(.accent) }
    public var ok: Color { color(.ok) }
    public var warn: Color { color(.warn) }
}
