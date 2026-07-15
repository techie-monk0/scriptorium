import Foundation

/// The reader-chrome icon set — SF Symbol names keyed by the control `id` from `readerChromeVM`. Loaded
/// from `reader-icons.json`, which is generated from `library-core.js`'s `READER_ICONS` (the single
/// cross-surface source of truth: web/PWA read the `web` glyphs, iOS reads these `sf` names). Change an
/// icon in `library-core.js` and regenerate (`Tools/gen_goldens.mjs`) — no per-surface drift.
public enum ReaderIcons {
    public struct Icon: Decodable, Sendable {
        public let sf: String
        public let sfActive: String?
        public let web: String
    }

    /// The parsed config, id → icon. Empty only if the bundled resource is missing/corrupt.
    public static let all: [String: Icon] = {
        guard let url = Bundle.module.url(forResource: "reader-icons", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let map = try? JSONDecoder().decode([String: Icon].self, from: data)
        else { return [:] }
        return map
    }()

    /// The SF Symbol for a control, honouring its toggled-on variant when `active`. Falls back to a
    /// neutral symbol so a missing/renamed id renders *something* rather than crashing.
    public static func sf(_ id: String, active: Bool = false) -> String {
        guard let icon = all[id] else { return "questionmark" }
        return (active ? icon.sfActive : nil) ?? icon.sf
    }
}
