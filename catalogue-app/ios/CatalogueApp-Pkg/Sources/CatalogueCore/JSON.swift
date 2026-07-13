import Foundation

/// The canonical JSON coders for the `/api/v1` contract. `convertFromSnakeCase` lets every model use
/// camelCase properties without hand-written CodingKeys (`edition_id` ⇄ `editionId`). Centralized so
/// the API client, the replica store, and the decode tests all agree byte-for-byte.
public enum CatalogueJSON {
    public static var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }
    public static var encoder: JSONEncoder {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }

    public static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decoder.decode(type, from: data)
    }
}

/// Ergonomic read-only navigation of a decoded `JSONValue` tree — used by the golden-parity tests to
/// assert specific paths of the JS view-model output without retrofitting CodingKeys onto every model.
public extension JSONValue {
    subscript(_ key: String) -> JSONValue? { if case .object(let o) = self { return o[key] }; return nil }
    subscript(_ index: Int) -> JSONValue? { if case .array(let a) = self, a.indices.contains(index) { return a[index] }; return nil }
    var stringValue: String? { if case .string(let s) = self { return s }; return nil }
    var intValue: Int? {
        switch self { case .int(let i): return i; case .double(let d): return Int(d); default: return nil }
    }
    var doubleValue: Double? {
        switch self { case .double(let d): return d; case .int(let i): return Double(i); default: return nil }
    }
    var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    var arrayValue: [JSONValue]? { if case .array(let a) = self { return a }; return nil }
    var isNull: Bool { if case .null = self { return true }; return false }
}
