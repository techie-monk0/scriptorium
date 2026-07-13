import Foundation

/// What the host permits/supports for this reading session. Supplied by the
/// integrator; the engine reads but never mutates it.
public struct Capabilities: Sendable, Equatable {
    public var canAnnotate: Bool
    public var canExport: Bool
    public var canSearch: Bool

    public init(
        canAnnotate: Bool = false,
        canExport: Bool = false,
        canSearch: Bool = true
    ) {
        self.canAnnotate = canAnnotate
        self.canExport = canExport
        self.canSearch = canSearch
    }
}
