import XCTest
@testable import Postilla

/// A mock recognizer satisfying the port contract — returns canned text.
struct MockLatinRecognizer: Recognizer {
    let text: String
    func recognize(_ input: RecognizerInput) async -> RecognitionResult {
        switch input {
        case .ink:
            return RecognitionResult(text: text, confidence: 0.9)
        case .region:
            return RecognitionResult(text: nil, confidence: 0)
        }
    }
}

/// PS-U4 — Recognizer port + advisory contract.
final class RecognizerPortTests: XCTestCase {

    func testMockSatisfiesPort() async {
        let r = MockLatinRecognizer(text: "hello")
        let out = await r.recognize(.ink(Ink(strokes: [Fix.inkStroke()])))
        XCTAssertEqual(out.text, "hello")
        XCTAssertEqual(out.confidence, 0.9, accuracy: 1e-9)
    }

    /// Recognition attaches as `recognizedText` and is advisory — the raw `ink`
    /// is never mutated.
    func testAdvisoryNeverMutatesInk() async {
        let r = MockLatinRecognizer(text: "namaste")
        let original = Annotation(
            id: Fix.uuid(1),
            publicationId: Fix.pub,
            kind: .ink,
            locator: Fix.locator(),
            ink: Ink(strokes: [Fix.inkStroke()]),
            createdAt: Fix.date(0),
            updatedAt: Fix.date(0)
        )
        let recognized = await r.annotate(original)
        XCTAssertEqual(recognized.recognizedText, "namaste")
        XCTAssertEqual(recognized.ink, original.ink, "raw ink must be untouched")
        XCTAssertNil(original.recognizedText, "original is not mutated in place")
    }

    /// A bad/empty result leaves a nil recognizedText and intact ink.
    func testBadResultDoesNotDestroyInk() async {
        struct Empty: Recognizer {
            func recognize(_ input: RecognizerInput) async -> RecognitionResult {
                RecognitionResult(text: nil, confidence: 0)
            }
        }
        let ann = Annotation(
            id: Fix.uuid(2),
            publicationId: Fix.pub,
            kind: .ink,
            locator: Fix.locator(),
            ink: Ink(strokes: [Fix.inkStroke()]),
            createdAt: Fix.date(0),
            updatedAt: Fix.date(0)
        )
        let out = await Empty().annotate(ann)
        XCTAssertNil(out.recognizedText)
        XCTAssertEqual(out.ink, ann.ink)
    }
}
