#if canImport(UIKit)
import SwiftUI
import UIKit
import Octavo

/// The reader settings panel — the single surface exposing every per-document reading control. Each
/// control edits a local copy of `ReaderSettings` and calls `apply`, which routes to
/// `reader.apply(...)`: the navigator applies the engine fields, records them, and fires
/// `onSettingsChanged`, so the change both renders and auto-persists (font/zoom/typography/PDF fit/crop),
/// while the host picks up warmth/brightness/orientation/highlight. Reflow-to-text is app-only, so it is
/// a separate binding.
@MainActor
struct ReaderSettingsSheet: View {
    let isPDF: Bool
    let apply: (ReaderSettings) -> Void
    let reflowMode: Bool
    let onToggleReflow: () -> Void

    @State private var s: ReaderSettings
    @Environment(\.dismiss) private var dismiss

    init(initial: ReaderSettings, isPDF: Bool, reflowMode: Bool,
         apply: @escaping (ReaderSettings) -> Void, onToggleReflow: @escaping () -> Void) {
        self.isPDF = isPDF
        self.reflowMode = reflowMode
        self.apply = apply
        self.onToggleReflow = onToggleReflow
        _s = State(initialValue: initial)
    }

    var body: some View {
        NavigationStack {
            Form {
                if isPDF { pdfSection } else { epubTypographySection; epubLayoutSection }
                comfortSection
                behaviourSection
            }
            .navigationTitle("Reading Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: EPUB

    private var epubTypographySection: some View {
        Section("Text") {
            stepper("Font size", intBind(\.fontPercent, 100), 60...250, step: 10, suffix: "%")
            picker("Weight", enumBind(\.fontWeight, .normal), FontWeight.allCasesForUI)
            picker("Alignment", enumBind(\.textAlign, .publisher), ReaderTextAlign.allCasesForUI)
            slider("Line spacing", doubleBind(\.lineHeight, 1.4), 1.0...2.2, step: 0.05)
            slider("Letter spacing", doubleBind(\.letterSpacing, 0), -0.05...0.3, step: 0.01)
            slider("Word spacing", doubleBind(\.wordSpacing, 0), -0.1...1.0, step: 0.05)
            slider("Paragraph spacing", doubleBind(\.paragraphSpacing, 0), 0...2.0, step: 0.1)
            slider("First-line indent", doubleBind(\.firstLineIndent, 0), 0...4.0, step: 0.25)
            Toggle("Hyphenation", isOn: boolBind(\.hyphenation, false))
            slider("Contrast", doubleBind(\.contrast, 1.0), 1.0...2.0, step: 0.05)
        }
    }

    private var epubLayoutSection: some View {
        Section("Layout") {
            picker("Columns", enumBind(\.columnCount, .auto), ColumnCount.allCasesForUI)
            slider("Side margins", intBindD(\.marginPercent, 0), 0...20, step: 1)
            slider("Top/bottom margins", intBindD(\.marginVerticalPercent, 0), 0...20, step: 1)
            slider("Line width", intBindD(\.maxLineWidthCh, 100), 30...100, step: 1)
        }
    }

    // MARK: PDF

    private var pdfSection: some View {
        Section("Page") {
            picker("Fit", enumBind(\.pdfFitMode, .fitWidth), PdfFitMode.allCasesForUI)
            Toggle("Trim margins", isOn: cropBind)
            Toggle("Reflow to text", isOn: Binding(get: { reflowMode }, set: { _ in onToggleReflow() }))
        }
    }

    // MARK: Shared

    private var comfortSection: some View {
        Section("Comfort") {
            slider("Warmth", doubleBind(\.warmth, 0), 0...1.0, step: 0.05)
            slider("Brightness", doubleBind(\.brightness, Double(UIScreen.main.brightness)), 0...1.0, step: 0.05)
            highlightColorRow
        }
    }

    private var behaviourSection: some View {
        Section("Behaviour") {
            picker("Lock rotation", enumBind(\.orientationLock, .none), OrientationLock.allCasesForUI)
        }
    }

    private var highlightColorRow: some View {
        let swatches = ["#ffd54a", "#a5d6a7", "#90caf9", "#f48fb1", "#ffab91"]
        return HStack {
            Text("Highlight")
            Spacer()
            ForEach(swatches, id: \.self) { hex in
                Circle().fill(Color(hex: hex)).frame(width: 24, height: 24)
                    .overlay(Circle().strokeBorder(.primary, lineWidth: (s.highlightColor ?? "#ffd54a") == hex ? 2 : 0))
                    .onTapGesture { s.highlightColor = hex; apply(s) }
            }
        }
    }

    // MARK: Control builders

    private func slider(_ title: String, _ value: Binding<Double>, _ range: ClosedRange<Double>, step: Double) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.subheadline)
            Slider(value: value, in: range, step: step)
        }
    }

    private func stepper(_ title: String, _ value: Binding<Int>, _ range: ClosedRange<Int>, step: Int, suffix: String) -> some View {
        Stepper("\(title): \(value.wrappedValue)\(suffix)", value: value, in: range, step: step)
    }

    private func picker<T: LabeledOption>(_ title: String, _ value: Binding<T>, _ options: [T]) -> some View {
        Picker(title, selection: value) {
            ForEach(options, id: \.self) { Text($0.uiLabel).tag($0) }
        }
    }

    // MARK: Bindings — each writes the field then re-applies the whole current settings (idempotent merge)

    private func intBind(_ kp: WritableKeyPath<ReaderSettings, Int?>, _ def: Int) -> Binding<Int> {
        Binding(get: { s[keyPath: kp] ?? def }, set: { s[keyPath: kp] = $0; apply(s) })
    }
    private func intBindD(_ kp: WritableKeyPath<ReaderSettings, Int?>, _ def: Int) -> Binding<Double> {
        Binding(get: { Double(s[keyPath: kp] ?? def) }, set: { s[keyPath: kp] = Int($0.rounded()); apply(s) })
    }
    private func doubleBind(_ kp: WritableKeyPath<ReaderSettings, Double?>, _ def: Double) -> Binding<Double> {
        Binding(get: { s[keyPath: kp] ?? def }, set: { s[keyPath: kp] = $0; apply(s) })
    }
    private func boolBind(_ kp: WritableKeyPath<ReaderSettings, Bool?>, _ def: Bool) -> Binding<Bool> {
        Binding(get: { s[keyPath: kp] ?? def }, set: { s[keyPath: kp] = $0; apply(s) })
    }
    private func enumBind<T>(_ kp: WritableKeyPath<ReaderSettings, T?>, _ def: T) -> Binding<T> {
        Binding(get: { s[keyPath: kp] ?? def }, set: { s[keyPath: kp] = $0; apply(s) })
    }
    /// Crop is a `.none`/`.auto` enum surfaced as a toggle.
    private var cropBind: Binding<Bool> {
        Binding(get: { (s.pdfCropMargins ?? .none) == .auto },
                set: { s.pdfCropMargins = $0 ? .auto : CropMode.none; apply(s) })
    }
}

/// A settings enum that can list itself and label itself for a SwiftUI picker.
protocol LabeledOption: Hashable {
    static var allCasesForUI: [Self] { get }
    var uiLabel: String { get }
}

extension FontWeight: LabeledOption {
    static var allCasesForUI: [FontWeight] { [.normal, .medium, .bold] }
    var uiLabel: String { rawValue.capitalized }
}
extension ReaderTextAlign: LabeledOption {
    static var allCasesForUI: [ReaderTextAlign] { [.publisher, .left, .justified, .center] }
    var uiLabel: String { self == .publisher ? "Default" : rawValue.capitalized }
}
extension ColumnCount: LabeledOption {
    static var allCasesForUI: [ColumnCount] { [.auto, .one, .two] }
    var uiLabel: String { self == .auto ? "Auto" : (self == .one ? "1" : "2") }
}
extension PdfFitMode: LabeledOption {
    static var allCasesForUI: [PdfFitMode] { [.fitWidth, .fitPage, .fitHeight, .actual] }
    var uiLabel: String {
        switch self {
        case .fitWidth: return "Width"; case .fitPage: return "Page"
        case .fitHeight: return "Height"; case .actual: return "Actual"; case .custom: return "Custom"
        }
    }
}
extension OrientationLock: LabeledOption {
    static var allCasesForUI: [OrientationLock] { [.none, .portrait, .landscape] }
    var uiLabel: String { self == .none ? "Off" : rawValue.capitalized }
}
#endif
