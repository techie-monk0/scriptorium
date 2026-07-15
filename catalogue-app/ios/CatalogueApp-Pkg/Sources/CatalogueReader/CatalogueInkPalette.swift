import Postilla
import CatalogueDesign

/// The **composition root** for the pencil palette: the Postilla SDK defaults with this client's
/// `CatalogueDesign` colours added on top (additive — the SDK never hard-codes the choice). A different
/// host would supply its own here. Pure (no UIKit), so it also compiles under `swift test`.
///
/// Colours are pulled from the design tokens rather than re-typed, so the pencil palette tracks the
/// app's palette and stays inside the drift-tested colour system.
enum CatalogueInk {
    static var palette: InkPalette {
        let t = Tokens(.default)
        return InkPalette.default.extending([
            InkSwatch(t.hex(.brand), name: "Brand"),
            InkSwatch(t.hex(.accent), name: "Accent"),
            InkSwatch(t.hex(.warn), name: "Amber"),
            InkSwatch(t.hex(.ok), name: "Green"),
        ])
    }
}
