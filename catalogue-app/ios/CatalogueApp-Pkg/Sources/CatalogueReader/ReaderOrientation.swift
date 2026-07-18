#if canImport(UIKit)
import UIKit
import Octavo

/// Per-book interface-orientation lock. iOS routes "which orientations are allowed" through the app
/// delegate, so this singleton holds the current mask and the app target's
/// `application(_:supportedInterfaceOrientationsFor:)` returns `ReaderOrientation.shared.mask`. The
/// reader sets it from the book's `orientationLock` on appear and clears it on disappear; setting it
/// also nudges the active window into an allowed orientation (iOS 16+ `requestGeometryUpdate`).
@MainActor
public final class ReaderOrientation {
    public static let shared = ReaderOrientation()
    private init() {}

    /// The currently-allowed orientations. Read by the app delegate; defaults to "all".
    public private(set) var mask: UIInterfaceOrientationMask = .all

    /// Apply a per-book lock (or `.none` to free rotation) and force the window into range.
    public func set(_ lock: OrientationLock) {
        switch lock {
        case .none:      mask = .all
        case .portrait:  mask = .portrait
        case .landscape: mask = .landscape
        }
        guard let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene }).first(where: { $0.activationState == .foregroundActive }) else { return }
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: mask)) { _ in }
        // Ask the top controller to re-evaluate its supported set so the bars/rotation update immediately.
        scene.keyWindow?.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
    }

    /// Restore free rotation (reader closed).
    public func clear() { set(.none) }
}
#endif
