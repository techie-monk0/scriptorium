import SwiftUI
import CatalogueUI

/// The runnable app bundle — the thin `@main` wrapper around `CatalogueAppRoot` (which lives in the
/// `CatalogueUI` library and themes + hosts the whole TabView). This is the ONLY code that must live
/// in an app target rather than a SwiftPM library; everything below it is the tested packages.
///
/// The server address is **not** configured here — it's chosen in the app's Settings (an abstract
/// `ServerEndpoint`: LAN / tunnel / NAS / …, persisted). For first run / CI you can still seed it with
/// the `CATALOGUE_SERVER` env var (Edit Scheme → Run → Environment Variables).
@main
struct CatalogueAppMain: App {
    var body: some Scene {
        WindowGroup {
            CatalogueAppRoot()
        }
    }
}
