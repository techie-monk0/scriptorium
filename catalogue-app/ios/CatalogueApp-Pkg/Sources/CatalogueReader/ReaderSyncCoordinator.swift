import Foundation
import Observation
import CatalogueReaderWire

/// A store whose cross-device reconcile can be **awaited** — the seam the `ReaderSyncCoordinator` drives.
///
/// The three reader `Local*Store`s already implement `reconcileWithRemote` (merge the server's copy in,
/// flush the outbox); it was only ever called fire-and-forget from `pull`/`push`, so nothing could tell
/// *when* it finished — which is why cross-device bookmarks/outline surfaced only on the *next* panel
/// open. This protocol exposes that same, existing work as an awaitable call so a completed reconcile can
/// become observable UI state. It adds **no** merge/persist/outbox logic — it forwards to what's there.
protocol RemoteReconcilable: Sendable {
    /// Reconcile this publication with the server (fold in other devices, flush the outbox) and return
    /// only once the merged result is durable locally.
    func reconcileNow(publicationId: String) async
}

extension LocalAnnotationStore: RemoteReconcilable {
    func reconcileNow(publicationId: String) async { await reconcileWithRemote(publicationId: publicationId) }
}
extension LocalBookmarkStore: RemoteReconcilable {
    func reconcileNow(publicationId: String) async { await reconcileWithRemote(publicationId: publicationId) }
}
extension LocalOutlineStore: RemoteReconcilable {
    func reconcileNow(publicationId: String) async { await reconcileWithRemote(publicationId: publicationId) }
}

/// The reader's per-document **refresh driver** — the "general layer" that turns the stores' background
/// reconcile into observable UI state, so every synced resource (marks, bookmarks, outline) refreshes
/// through one place instead of each hand-rolling its own fragile poll.
///
/// It does two things and nothing more:
/// 1. **awaits** every resource's `reconcileNow` (the merge/flush the stores already do), and
/// 2. publishes a single `phase` (drives the global "Refreshing…" chip) plus a `generation` counter the
///    view keys its re-populate on — so a landed reconcile pushes into the live view (bookmarks/outline
///    appear on the FIRST open, and update in place if the panel is already open), rather than waiting
///    for the next poll/open.
///
/// A new syncable resource joins by conforming to `RemoteReconcilable` and being added to `resources`;
/// it then gets the chip and the on-open refresh for free.
@MainActor
@Observable
final class ReaderSyncCoordinator {
    /// The two-step lifecycle. `.checking` = asking the server whether anything changed (the cheap
    /// probe); `.fetching` = pulling + merging because it did. `.synced`/`.failed` are resting states.
    enum Phase: Equatable { case idle, checking, fetching, synced, failed }

    /// Aggregate sync state for this document — drives the global chip and the Contents refresh dialog.
    private(set) var phase: Phase = .idle
    /// Incremented each time a sync actually FETCHES new data. The view watches this to re-run its
    /// populate pipeline; a "nothing changed" check does NOT bump it (there's nothing to repaint).
    private(set) var generation = 0

    private let resources: [any RemoteReconcilable]
    /// The cheap change-probe: server revs for a pubId, or nil if it couldn't be reached. When absent
    /// (tests/previews with no server), the coordinator skips the gate and always fetches.
    private let revCheck: (@Sendable (String) async -> HoldingRevs?)?
    /// Per-book cursor of the last revs we merged; compared against the probe to decide fetch-or-skip.
    private let cursors: ReaderSyncCursorStore?
    private var inFlight = false

    init(resources: [any RemoteReconcilable],
         revCheck: (@Sendable (String) async -> HoldingRevs?)? = nil,
         cursors: ReaderSyncCursorStore? = nil) {
        self.resources = resources
        self.revCheck = revCheck
        self.cursors = cursors
    }

    /// Two-step sync for `pubId`: probe whether anything changed (`.checking`); if so, reconcile every
    /// resource and record the new cursor (`.fetching`), then bump `generation`; if not, resolve without
    /// touching the stores or the view. Overlapping calls coalesce into the one in-flight pass. Never
    /// throws — a failed probe is treated as "assume changed" so we still fetch rather than go stale.
    func refresh(pubId: String) async {
        guard !resources.isEmpty, !inFlight else { return }
        inFlight = true
        defer { inFlight = false }

        // Step 1 — CHECK. No probe wired → always fetch (preserves the un-gated behavior for tests).
        phase = .checking
        let serverRevs = await revCheck?(pubId)
        let mustFetch: Bool
        if let serverRevs, let cursors {
            let seen = await cursors.lastSeen(pubId)
            mustFetch = serverRevs.hasChanges(since: seen)
        } else {
            mustFetch = true
        }
        guard mustFetch else { phase = .synced; return }   // nothing new — don't disturb the view

        // Step 2 — FETCH. Reconcile every resource concurrently, then advance the cursor.
        phase = .fetching
        await withTaskGroup(of: Void.self) { group in
            for r in resources {
                group.addTask { await r.reconcileNow(publicationId: pubId) }
            }
        }
        if let serverRevs, let cursors { await cursors.record(pubId, serverRevs) }
        phase = .synced
        generation &+= 1
    }
}
