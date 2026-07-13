import XCTest
@testable import CatalogueCore

/// U3 — Ref/URL mapping. `refFromUrl` parses web URLs → neutral refs (same precedence as
/// library-core.js); `NativeNav.hrefFor` maps a ref to a native route, returning nil for work/person
/// exactly where the PWA adapter returns null.
final class RefsTests: XCTestCase {
    func testRefFromUrlPrecedence() {
        XCTAssertEqual(refFromUrl("/library?eid=42"), .edition(42))
        XCTAssertEqual(refFromUrl("/edition/7"), .edition(7))
        XCTAssertEqual(refFromUrl("/work/3"), .work(3))
        XCTAssertEqual(refFromUrl("/person/9"), .person(9))
        XCTAssertEqual(refFromUrl("/subject/5"), .subject(id: 5, q: nil))
        XCTAssertEqual(refFromUrl("https://example.org/x"), .url("https://example.org/x"))
        XCTAssertNil(refFromUrl(nil))
        XCTAssertNil(refFromUrl(""))
    }

    func testEidQueryWinsOverPath() {
        // The `eid=` query is checked before the path patterns (library-core.js order).
        XCTAssertEqual(refFromUrl("/anything?eid=11&x=1"), .edition(11))
    }

    func testNativeNavMapsRefsAndNullsWorkPerson() {
        let nav = NativeNav()
        XCTAssertEqual(nav.hrefFor(.edition(42)), "/library?eid=42")
        XCTAssertEqual(nav.hrefFor(.subject(id: 5, q: nil)), "/subject/5")
        XCTAssertEqual(nav.hrefFor(.url("https://x")), "https://x")
        XCTAssertNil(nav.hrefFor(.work(3)))     // no native destination (matches PWA null)
        XCTAssertNil(nav.hrefFor(.person(9)))
        XCTAssertNil(nav.hrefFor(nil))
    }

    func testSubjectByQueryFallsBackToSearch() {
        // No id → a filtered search route (the '/' stays, it's allowed in a query value).
        XCTAssertEqual(NativeNav().hrefFor(.subject(id: nil, q: "Buddhism/Emptiness")),
                       "/search?subject=Buddhism/Emptiness")
        XCTAssertEqual(Ref.subject(id: nil, q: "Zen").route, .search(subject: "Zen"))
    }

    func testArtAndSubjectHelpers() {
        XCTAssertEqual(artFor(42).coverUrl, "/edition/42/cover.jpg")
        XCTAssertEqual(artFor(42).spineUrl, "/edition/42/spine.svg")
        XCTAssertEqual(subjectTopLevel("Buddhism/Emptiness"), "Buddhism")
        XCTAssertTrue(isUnderSubject("Buddhism/Emptiness", "Buddhism"))
        XCTAssertTrue(isUnderSubject("Buddhism", "Buddhism"))
        XCTAssertFalse(isUnderSubject("Buddhismx", "Buddhism"))
    }

    func testReadHref() {
        XCTAssertEqual(NativeNav().readHref(holdingId: 7), "/holding/7/file")
        XCTAssertEqual(NativeNav().readHref(holdingId: nil), "#")
    }
}
