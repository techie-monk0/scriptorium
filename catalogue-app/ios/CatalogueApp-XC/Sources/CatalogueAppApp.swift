import SwiftUI
import UIKit
import CatalogueUI
import CatalogueReader

/// The runnable app bundle — the thin `@main` wrapper around `CatalogueAppRoot` (which lives in the
/// `CatalogueUI` library and themes + hosts the whole TabView). This is the ONLY code that must live
/// in an app target rather than a SwiftPM library; everything below it is the tested packages.
///
/// The server address is **not** configured here — it's chosen in the app's Settings (an abstract
/// `ServerEndpoint`: LAN / tunnel / NAS / …, persisted). For first run / CI you can still seed it with
/// the `CATALOGUE_SERVER` env var (Edit Scheme → Run → Environment Variables).
@main
struct CatalogueAppMain: App {
    // iOS routes the allowed orientations through the app delegate; the per-book reader lock lives in
    // `ReaderOrientation.shared` (set by the reader) and is surfaced to UIKit here.
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            CatalogueAppRoot()
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        MainActor.assumeIsolated { ReaderOrientation.shared.mask }
    }
}
