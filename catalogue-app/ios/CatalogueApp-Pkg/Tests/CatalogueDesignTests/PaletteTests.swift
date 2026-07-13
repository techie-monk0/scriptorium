import XCTest
import SwiftUI
@testable import CatalogueDesign

/// U7 — Palette port. The hard lockstep guard (committed `Palette.swift` == fresh render of
/// `palette.json`) lives in the Python `tests/test_palette_master.py`, which runs the generator. Here
/// we guard the *Swift* side: hex parsing is exact, every theme defines every token, and the semantic
/// `Tokens` accessors resolve. (Together with the Python drift test this keeps web + iOS in lockstep.)
final class PaletteTests: XCTestCase {

    func testHexRoundTripIsExact() {
        // #1856c5 = the light `link`/`accent` blue.
        let c = parseHex("#1856c5")
        XCTAssertNotNil(c)
        XCTAssertEqual(c!.r, Double(0x18) / 255, accuracy: 1e-9)
        XCTAssertEqual(c!.g, Double(0x56) / 255, accuracy: 1e-9)
        XCTAssertEqual(c!.b, Double(0xc5) / 255, accuracy: 1e-9)
        XCTAssertEqual(c!.a, 1, accuracy: 1e-9)
    }

    func testHexShortAndAlphaForms() {
        XCTAssertEqual(parseHex("#fff"), RGBA(r: 1, g: 1, b: 1))
        XCTAssertEqual(parseHex("#000000ff"), RGBA(r: 0, g: 0, b: 0, a: 1))
        XCTAssertEqual(parseHex("#ffffff00")?.a, 0)
    }

    func testMalformedHexReturnsNil() {
        XCTAssertNil(parseHex("#xyz"))
        XCTAssertNil(parseHex("#12345"))   // 5 digits is not a valid form
        XCTAssertNil(parseHex(""))
    }

    func testEveryThemeDefinesEveryToken() {
        // A token missing from a theme would silently fall back at runtime — guard against it.
        let all = Set(Token.allCases)
        for theme in Theme.allCases {
            let defined = Set(Palette.hex[theme]?.keys ?? [:].keys)
            XCTAssertEqual(defined, all, "theme \(theme) is missing tokens: \(all.subtracting(defined))")
        }
        XCTAssertEqual(Token.allCases.count, 20, "expected the 20 documented tokens")
    }

    func testKnownTokenValuesPortedFromMaster() {
        // Spot-check both themes against palette.json so a bad port is caught even in Swift-only CI.
        XCTAssertEqual(Tokens(.light).hex(.bg), "#fbfcfd")
        XCTAssertEqual(Tokens(.light).hex(.navActiveBg), "#1856c5")
        XCTAssertEqual(Tokens(.dark).hex(.bg), "#0f1318")
        XCTAssertEqual(Tokens(.dark).hex(.warn), "#fbbf24")
        XCTAssertEqual(Tokens(.dark).hex(.surface2), "#212834")
    }

    func testTokensFallBackToDefaultThemeForMissing() {
        // Construct is total: every accessor returns without trapping for both themes.
        for theme in Theme.allCases {
            let t = Tokens(theme)
            for token in Token.allCases { _ = t.color(token) }
            _ = (t.bg, t.fg, t.link, t.brand, t.accent, t.warn, t.ok)
        }
    }

    func testThemePreferenceResolution() {
        XCTAssertEqual(ThemePreference(pref: nil), .auto)
        XCTAssertEqual(ThemePreference(pref: "weird"), .auto)
        XCTAssertEqual(ThemePreference(pref: "dark"), .dark)
        XCTAssertEqual(ThemePreference.auto.resolved(osDark: true), .dark)
        XCTAssertEqual(ThemePreference.auto.resolved(osDark: false), .light)
        XCTAssertEqual(ThemePreference.light.resolved(osDark: true), .light)
        XCTAssertNil(ThemePreference.auto.preferredColorScheme)
        XCTAssertEqual(Theme.default, .light)
    }
}
