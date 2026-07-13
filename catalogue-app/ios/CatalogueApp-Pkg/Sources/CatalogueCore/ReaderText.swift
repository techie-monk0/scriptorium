import Foundation

/// PDF "reflow to text": raw page text → paragraphs. The Swift port of `library-core.js`
/// `reflowPageText` (golden-tested for 1:1 parity). De-hyphenates line-wrapped words, joins
/// intra-paragraph line breaks into single spaces, and splits paragraphs on blank lines. Heuristic
/// only — it reflows text, it does not reconstruct layout.
public func reflowPageText(_ raw: String) -> [String] {
    let text = raw
        .replacingOccurrences(of: "\r\n", with: "\n")
        .replacingOccurrences(of: "\r", with: "\n")
        .replacingOccurrences(of: "\u{00ad}", with: "")            // soft hyphens
    var paras: [String] = []
    var cur = ""
    var joinNext = false

    // Matches the JS class [A-Za-zÀ-ɏ] (ASCII + Latin-1 Supplement + Latin Extended-A/B start).
    func isLatinLetter(_ c: Character) -> Bool {
        guard c.unicodeScalars.count == 1, let v = c.unicodeScalars.first?.value else { return false }
        return (v >= 65 && v <= 90) || (v >= 97 && v <= 122) || (v >= 0x00C0 && v <= 0x024F)
    }
    func flush() {
        let collapsed = cur.split(whereSeparator: { $0 == " " || $0 == "\t" }).joined(separator: " ")
        if !collapsed.isEmpty { paras.append(collapsed) }
        cur = ""; joinNext = false
    }

    for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
        let content = line.trimmingCharacters(in: CharacterSet(charactersIn: " \t"))
        if content.isEmpty { flush(); continue }                    // blank line → paragraph break
        if cur.isEmpty { cur = content }
        else if joinNext { cur += content }                         // glued after a de-hyphenated word
        else { cur += " " + content }                               // wrapped line → single space
        if cur.hasSuffix("-"), cur.count >= 2,
           isLatinLetter(cur[cur.index(cur.endIndex, offsetBy: -2)]) {
            cur.removeLast(); joinNext = true
        } else {
            joinNext = false
        }
    }
    flush()
    return paras
}
