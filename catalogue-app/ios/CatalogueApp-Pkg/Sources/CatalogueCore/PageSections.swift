import Foundation

/// SDUI-lite: a page expressed as an ordered list of SECTIONS. Every surface renders these through a
/// small component registry keyed by `type` ("crumbs" | "rail" | "grid"), so the page SHAPE is defined
/// once (in the shared Tier-2 layer) and each surface only provides the per-toolkit renderer. Mirrors
/// `library-core.js`, golden-locked.
public struct PageSection: Equatable, Sendable, Identifiable {
    public var type: String                 // "crumbs" | "rail" | "grid"
    public var title: String?               // section header (rail/grid)
    public var subject: String?             // subject path → header deep-link (rail/grid)
    public var cards: [Card]                // tiles (rail/grid)
    public var crumbs: [SubjectCrumb]       // breadcrumbs (crumbs)
    public var id: String { "\(type):\(title ?? ""):\(subject ?? "")" }

    public init(type: String, title: String? = nil, subject: String? = nil,
                cards: [Card] = [], crumbs: [SubjectCrumb] = []) {
        self.type = type; self.title = title; self.subject = subject
        self.cards = cards; self.crumbs = crumbs
    }
}

/// Turn the rich `subjectVM` into sections — breadcrumbs + a rail per child (+ a leftover rail), or a
/// single grid when the subject has no children. 1:1 with `library-core.js` `subjectSections`.
public func subjectSections(_ vm: SubjectVM) -> [PageSection] {
    var out: [PageSection] = []
    if !vm.crumbs.isEmpty { out.append(PageSection(type: "crumbs", crumbs: vm.crumbs)) }
    if !vm.children.isEmpty {
        for ch in vm.children {
            out.append(PageSection(type: "rail", title: ch.leaf, subject: ch.name, cards: ch.books))
        }
        if !vm.leftover.isEmpty {
            out.append(PageSection(type: "rail", title: vm.leaf, subject: vm.name, cards: vm.leftover))
        }
    } else {
        out.append(PageSection(type: "grid", title: vm.leaf, subject: vm.name, cards: vm.books))
    }
    return out
}
