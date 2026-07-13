// In-app ISBN barcode scanner for the wishlist (iOS only — VisionKit's DataScannerViewController).
// Guarded by `os(iOS)` so the macOS test target (and `swift test`) excludes it entirely — VisionKit
// imports on macOS but DataScannerViewController does not exist there. The WishlistScreen call sites
// are guarded the same way and fall back to manual entry off-iOS.
//
// This is the in-app half of the "Both" scanning decision: the catalogue app can scan an EAN-13 ISBN
// straight into the wishlist (source "scan"), while the companion isbn-scanner app remains the path
// for CIP (copyright-page) pages. The recognized 13-digit code is handed to the server, which
// normalizes + checksum-validates it (same contract as a typed ISBN), so no validation lives here.
#if os(iOS)
import SwiftUI
import VisionKit

/// Whether live data scanning is usable on this device (real iPhone w/ camera; false on Simulator).
enum WishlistScanAvailability {
    @MainActor static var isAvailable: Bool {
        DataScannerViewController.isSupported && DataScannerViewController.isAvailable
    }
}

/// A full-screen EAN-13 barcode scanner. Calls `onCode` once with the first 13-digit payload, then
/// the presenting sheet dismisses. No multi-item / high-frame-rate — one book at a time.
@available(iOS 16.0, *)
struct ISBNScannerView: UIViewControllerRepresentable {
    let onCode: (String) -> Void

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let vc = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.ean13])],
            qualityLevel: .balanced,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true)
        vc.delegate = context.coordinator
        return vc
    }

    func updateUIViewController(_ vc: DataScannerViewController, context: Context) {
        try? vc.startScanning()
    }

    func makeCoordinator() -> Coordinator { Coordinator(onCode: onCode) }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        private let onCode: (String) -> Void
        private var fired = false                 // one-shot: ignore the stream after the first hit
        init(onCode: @escaping (String) -> Void) { self.onCode = onCode }

        func dataScanner(_ scanner: DataScannerViewController,
                         didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            handle(addedItems)
        }
        func dataScanner(_ scanner: DataScannerViewController, didTapOn item: RecognizedItem) {
            handle([item])
        }

        private func handle(_ items: [RecognizedItem]) {
            guard !fired else { return }
            for case let .barcode(barcode) in items {
                if let value = barcode.payloadStringValue, value.count == 13 {
                    fired = true
                    onCode(value)
                    return
                }
            }
        }
    }
}

/// The sheet the WishlistScreen presents: the camera scanner with a Cancel button, or a short note
/// when scanning isn't available (Simulator / no camera).
@available(iOS 16.0, *)
struct ISBNScannerSheet: View {
    let onCode: (String) -> Void
    let onCancel: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            if WishlistScanAvailability.isAvailable {
                ISBNScannerView(onCode: onCode).ignoresSafeArea()
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "barcode.viewfinder").font(.largeTitle)
                    Text("Barcode scanning needs a physical iPhone.")
                        .multilineTextAlignment(.center).font(.callout)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Button("Cancel", action: onCancel)
                .padding(10).background(.ultraThinMaterial, in: Capsule()).padding()
        }
    }
}
#endif
