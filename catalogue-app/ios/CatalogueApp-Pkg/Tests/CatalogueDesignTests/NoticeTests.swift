import XCTest
import SwiftUI
@testable import CatalogueDesign

/// The `Notice` spec is the abstract layer behind the app's one empty/error state (`NoticeView`
/// renders it). These guard the pure data + role mapping headlessly under `swift test`; the SwiftUI
/// render is exercised by the app build + the reader/UI screens that consume it.
final class NoticeTests: XCTestCase {

    func testDefaultsAreEmpty() {
        let n = Notice(icon: "book", title: "No document open")
        XCTAssertNil(n.message)
        XCTAssertTrue(n.actions.isEmpty)              // actionless → drop-in for a bare empty state
    }

    func testRoleMapsToSwiftUIButtonRole() {
        XCTAssertNil(NoticeRole.normal.buttonRole)
        XCTAssertEqual(NoticeRole.cancel.buttonRole, .cancel)
        XCTAssertEqual(NoticeRole.destructive.buttonRole, .destructive)
    }

    func testCloseActionIsAProminentCancel() {
        let close = NoticeAction.close { }
        XCTAssertEqual(close.title, "Close")
        XCTAssertEqual(close.role, .cancel)
        XCTAssertTrue(close.prominent)                // reads as the way forward out of the modal
    }

    func testCloseActionTitleIsOverridable() {
        XCTAssertEqual(NoticeAction.close("Done") { }.title, "Done")
    }

    func testActionHandlerFires() {
        var fired = false
        let n = Notice(icon: "book", title: "No document open",
                       actions: [.close { fired = true }])
        XCTAssertEqual(n.actions.count, 1)
        n.actions[0].handler()                        // the tap path a modal wires to dismiss()
        XCTAssertTrue(fired)
    }

    func testActionsAreIdentifiable() {
        let a = NoticeAction("A") { }
        let b = NoticeAction("B") { }
        XCTAssertNotEqual(a.id, b.id)                 // stable ids so ForEach doesn't collapse rows
    }

    /// Render-path smoke: rasterize each presentation of a full notice (icon + message + a Close
    /// action) so a crash or bad view tree in the SwiftUI `body` fails here, headlessly — not only
    /// on a device. Exercises `NoticeView` (inline), `NoticeCard` (popup card) and `NoticeOverlay`
    /// (scrim + card).
    @MainActor func testEveryPresentationRasterizes() {
        let notice = Notice(icon: "book", title: "No document open",
                            message: "Open a book from Home or Books to start reading.",
                            actions: [.close { }])
        XCTAssertNotNil(ImageRenderer(content: NoticeView(notice)).cgImage)
        XCTAssertNotNil(ImageRenderer(content: NoticeCard(notice)).cgImage)
        XCTAssertNotNil(ImageRenderer(content: NoticeOverlay(notice, onDismiss: { })).cgImage)
    }
}
