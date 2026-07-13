import Foundation
import ReaderContract
@testable import Postilla

/// Shared fixtures for the Postilla unit suite.
enum Fix {
    static let pub = "pub-1"

    static func locator(page: Int = 42) -> Locator {
        Locator(publicationId: pub, format: .pdf, locations: .init(page: page, progression: 0.5))
    }

    static func date(_ s: TimeInterval) -> Date {
        Date(timeIntervalSince1970: s)
    }

    /// A deterministic UUID from a small int (so merges are reproducible).
    static func uuid(_ n: Int) -> UUID {
        UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", n))!
    }

    static func highlight(
        _ n: Int,
        updatedAt: TimeInterval,
        rev: Int = 0,
        color: String = "#ffd54f",
        deleted: TimeInterval? = nil,
        pub publicationId: String = pub
    ) -> Annotation {
        Annotation(
            id: uuid(n),
            publicationId: publicationId,
            kind: .highlight,
            locator: locator(),
            color: color,
            createdAt: date(0),
            updatedAt: date(updatedAt),
            deletedAt: deleted.map(date),
            rev: rev
        )
    }

    static func inkStroke() -> InkStroke {
        InkStroke(
            points: [
                InkPoint(x: 0.1, y: 0.2, pressure: 0.5),
                InkPoint(x: 0.3, y: 0.4, pressure: 0.75),
            ],
            width: 4,
            color: "#ff0000",
            mode: .draw
        )
    }
}
