import Foundation

/// A single captured ink sample. Coordinates are **page/CFI-relative, `0...1`**
/// (the canonical-ink rule, `postilla.md` §4). Encodes as a compact
/// `[x, y, pressure]` JSON array for byte-parity with the web binding — never as
/// a keyed object.
public struct InkPoint: Equatable, Sendable {
    public var x: Double
    public var y: Double
    public var pressure: Double
    /// Optional capture time in **milliseconds from the stroke's first sample**
    /// (PencilKit `PKStrokePoint.timeOffset`×1000; web `event.timeStamp −
    /// strokeStart`). Carried for **online** handwriting recognition (MyScript /
    /// ML Kit use stroke *dynamics* — order + timing — not the rendered image).
    /// Additive on the wire (see the codec below): omitted when nil, so existing
    /// ink and the byte-parity goldens are unchanged.
    public var t: Double?

    public init(x: Double, y: Double, pressure: Double = 1.0, t: Double? = nil) {
        self.x = x
        self.y = y
        self.pressure = pressure
        self.t = t
    }

    /// True when both coordinates sit inside the normalized page space.
    public var isNormalized: Bool {
        (0.0...1.0).contains(x) && (0.0...1.0).contains(y)
    }

    /// Clamp x/y/pressure back into `0...1` (defensive; capture should already
    /// produce normalized points). `t` is carried through unchanged.
    public func normalized() -> InkPoint {
        InkPoint(
            x: min(max(x, 0), 1),
            y: min(max(y, 0), 1),
            pressure: min(max(pressure, 0), 1),
            t: t
        )
    }
}

extension InkPoint: Codable {
    /// Wire form is a compact array `[x, y, pressure?, t?]`. Both trailing fields
    /// are optional so older ink decodes: `[x,y]` → full pressure, no time;
    /// `[x,y,pressure]` → no time; `[x,y,pressure,t]` → timed.
    public init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        x = try c.decode(Double.self)
        y = try c.decode(Double.self)
        pressure = c.isAtEnd ? 1.0 : try c.decode(Double.self)
        t = c.isAtEnd ? nil : try c.decode(Double.self)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.unkeyedContainer()
        try c.encode(x)
        try c.encode(y)
        try c.encode(pressure)
        // Additive: only emit the 4th slot when timed, so untimed ink stays
        // byte-identical to the pre-timestamp goldens.
        if let t { try c.encode(t) }
    }
}

/// How a stroke composites. `draw` is opaque pen; `highlight` is multiply-tint;
/// `erase` removes pixels at render time (the raw points still persist).
public enum InkMode: String, Codable, Sendable {
    case draw
    case highlight
    case erase
}

/// One pen stroke: a polyline of pressure samples plus styling. The raw points
/// are the source of truth; rendering is `FreehandRenderer`'s job.
public struct InkStroke: Codable, Equatable, Sendable {
    public var points: [InkPoint]
    public var width: Double
    public var color: String
    public var mode: InkMode

    public init(
        points: [InkPoint],
        width: Double,
        color: String,
        mode: InkMode = .draw
    ) {
        self.points = points
        self.width = width
        self.color = color
        self.mode = mode
    }

    public var isNormalized: Bool { points.allSatisfy { $0.isNormalized } }
}

/// The persisted ink payload of an `Annotation`. Never a platform stroke object
/// (no `PKDrawing`) — only raw normalized points.
public struct Ink: Codable, Equatable, Sendable {
    public var strokes: [InkStroke]

    public init(strokes: [InkStroke]) {
        self.strokes = strokes
    }

    public var isNormalized: Bool { strokes.allSatisfy { $0.isNormalized } }
}

extension Ink {
    /// Deterministic (sorted-key) JSON — used for web byte-parity goldens.
    public func canonicalJSONData() throws -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try enc.encode(self)
    }

    public static func from(jsonData data: Data) throws -> Ink {
        try JSONDecoder().decode(Ink.self, from: data)
    }
}
