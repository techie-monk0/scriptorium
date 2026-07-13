import SwiftUI

/// Spacing scale (pt) — a small, named ramp so screens don't sprinkle magic numbers. Roughly the
/// PWA's 4/8/12/16/24/32 rhythm.
public enum Spacing {
    public static let xs: CGFloat = 4
    public static let sm: CGFloat = 8
    public static let md: CGFloat = 12
    public static let lg: CGFloat = 16
    public static let xl: CGFloat = 24
    public static let xxl: CGFloat = 32

    /// Cover-poster width (PWA `shelf.css` ≈140px) and spine width (≈46px).
    public static let coverWidth: CGFloat = 140
    public static let spineWidth: CGFloat = 46

    /// macOS-Dock shelf magnification: how much a tile blooms as it nears a rail's centre (the
    /// SwiftUI analogue of shelf.js `magnify`, MAX≈1.25). Interaction constant — see the Tier-2.5
    /// note in private/plans/frontend_tiers_and_home_upgrade.md (ideally promoted to palette.json).
    public static let shelfMagnify: CGFloat = 0.25
}

/// The user's theme *preference* — the neutral `theme` pref from `settingsVM` (`auto|light|dark`),
/// where `auto` means follow the OS. Resolution to a concrete `Theme` lives here (pure) so the app
/// can decide `.preferredColorScheme` before the first view.
public enum ThemePreference: String, CaseIterable, Sendable {
    case auto, light, dark

    public init(pref: String?) {
        switch pref {
        case "light": self = .light
        case "dark": self = .dark
        default: self = .auto   // anything else (incl. nil / the removed key) = follow OS
        }
    }

    /// Resolve to a concrete `Theme` given the current OS scheme (used when `self == .auto`).
    public func resolved(osDark: Bool) -> Theme {
        switch self {
        case .light: return .light
        case .dark: return .dark
        case .auto: return osDark ? .dark : .light
        }
    }

    /// The SwiftUI `.preferredColorScheme` value — nil for `auto` (let the OS drive).
    public var preferredColorScheme: ColorScheme? {
        switch self {
        case .auto: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }
}
