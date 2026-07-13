import Foundation
import ReaderContract

/// What a recognizer was handed.
public enum RecognizerInput: Sendable {
    /// A set of freehand strokes (HWR / ink-to-shape).
    case ink(Ink)
    /// A page region to OCR, addressed by Locator.
    case region(Locator)
}

/// Advisory recognizer output. Any field may be nil; `confidence` is `0...1`.
public struct RecognitionResult: Sendable, Equatable {
    public var text: String?
    public var shape: String?
    public var confidence: Double

    public init(text: String? = nil, shape: String? = nil, confidence: Double = 0) {
        self.text = text
        self.shape = shape
        self.confidence = confidence
    }
}

/// PORT — `recognize(input) -> result` (`postilla.md` §5). The SDK ships the
/// seam and reference adapters, never a hard dependency (Vision/ML Kit/cloud
/// stay behind this).
///
/// Output is **always advisory**: attach it via `apply(_:to:)`, which only ever
/// writes `recognizedText` and **never** touches the raw `ink` (PS-U4).
public protocol Recognizer: Sendable {
    func recognize(_ input: RecognizerInput) async -> RecognitionResult
}

extension Recognizer {
    /// Run recognition on an annotation's ink and attach the text — advisory.
    /// Returns a copy with `recognizedText` set; `ink` is left untouched.
    public func annotate(_ annotation: Annotation) async -> Annotation {
        guard let ink = annotation.ink else { return annotation }
        let result = await recognize(.ink(ink))
        var copy = annotation
        copy.recognizedText = result.text
        return copy
    }
}
