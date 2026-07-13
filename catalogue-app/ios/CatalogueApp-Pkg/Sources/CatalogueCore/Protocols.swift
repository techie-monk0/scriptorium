import Foundation

/// A runtime capability context — mirror of the server's `{local, desktop}`. `local` = running on the
/// machine that hosts the catalogue; `desktop` = a desktop-class client (large screen, not a phone).
public struct ProtocolContext: Equatable, Sendable, Codable {
    public var local: Bool
    public var desktop: Bool
    public init(local: Bool = false, desktop: Bool = false) { self.local = local; self.desktop = desktop }
}

/// A named visibility gate deciding whether a section/menu-item shows, from a `ProtocolContext`. Mirror
/// of `catalogue/domain/protocols.py` (and `library-core.js` `PROTOCOLS`) so web/PWA/native gate
/// identically. The built-in `default` is always visible; an unknown name is treated as `default`.
public enum AppProtocol: String, CaseIterable, Sendable {
    case `default`
    case local
    case desktop
}

public func protocolVisible(_ name: String?, _ ctx: ProtocolContext) -> Bool {
    switch AppProtocol(rawValue: name ?? "default") ?? .default {
    case .default: return true
    case .local: return ctx.local
    case .desktop: return ctx.desktop
    }
}
