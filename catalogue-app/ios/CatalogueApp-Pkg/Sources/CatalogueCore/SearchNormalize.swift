import Foundation

/// Diacritic + case + DIGRAPH fold, mirroring `library-core.js` `fold()` (and the server's `fold_key`):
/// NFKD-decompose, strip the combining-marks block (U+0300…U+036F), lowercase, then collapse IAST/
/// phonetic digraphs (sh→s, ch→c, …) so Śāntideva / Shantideva / Santideva → `santideva` and the
/// Bodhicaryāvatāra / Bodhicharyāvatāra spellings fold the same. (Offline content/FTS search uses its
/// own non-digraph normalizer to match the FTS index — that lives with the content index.)
private let _digraphs: [(String, String)] = [("sh","s"),("ch","c"),("ph","p"),("th","t"),("kh","k"),
                                              ("gh","g"),("jh","j"),("bh","b"),("dh","d"),("rh","r")]
private func _foldBase(_ s: String?) -> String {
    let nfkd = (s ?? "").decomposedStringWithCompatibilityMapping
    let stripped = String(String.UnicodeScalarView(
        nfkd.unicodeScalars.filter { !(0x0300...0x036F).contains($0.value) }))
    return stripped.lowercased()
}
private func _digraph(_ t: String) -> String {
    var out = t
    for (src, dst) in _digraphs { out = out.replacingOccurrences(of: src, with: dst) }
    return out
}
public func fold(_ s: String?) -> String { _digraph(_foldBase(s)) }

/// `fold` + whitespace collapse — mirrors the server's replica fold
/// (`export_replica._fold`: NFKD, casefold≈lowercase, `" ".join(split())`). Used for matching a query
/// against the replica's editions offline so accent/case/spacing don't affect lookup.
public func foldCollapsed(_ s: String?) -> String {
    fold(s).split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
}

// Shared regnal-ordinal tables (mirror of names.py _WORD_ORD / _ROMAN_VAL).
private let _ordWord: [String: Int] = [
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "twentyfirst": 21,
    "twentysecond": 22, "twentythird": 23, "twentyfourth": 24, "twentyfifth": 25]
private let _romanVal: [String: Int] = [
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11,
    "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
    "xxi": 21, "xxii": 22, "xxiii": 23, "xxiv": 24, "xxv": 25]
private func _ordinalValue(_ tok: String) -> Int? {
    let t = String(tok.reversed().drop(while: { $0 == "." }).reversed())
    if let suf = ["st", "nd", "rd", "th"].first(where: { t.hasSuffix($0) }) {  // 14th
        let digits = String(t.dropLast(suf.count))
        if !digits.isEmpty, digits.count <= 2, digits.allSatisfy({ $0.isNumber }), let n = Int(digits) { return n }
    }
    if let n = _ordWord[t] { return n }
    if t.count >= 2, let n = _romanVal[t] { return n }   // 2+ char roman; single letters = initials
    return nil
}

/// Name match key: `fold` then map each regnal-ordinal token (14th / Fourteenth / XIV) to `#<n>` — the
/// shared canonicalization (mirror of `library-core.js` nameKey) so name matching agrees across surfaces.
public func nameKey(_ s: String?) -> String {
    // Detect ordinals on the diacritic-stripped (PRE-digraph) token (the digraph collapse would turn
    // '14th'→'14t' / 'fourteenth'→'fourteent' and break detection); non-ordinal tokens then digraph.
    _foldBase(s).split(whereSeparator: { $0.isWhitespace })
        .map { tok -> String in let t = String(tok); return _ordinalValue(t).map { "#\($0)" } ?? _digraph(t) }
        .joined(separator: " ")
}
