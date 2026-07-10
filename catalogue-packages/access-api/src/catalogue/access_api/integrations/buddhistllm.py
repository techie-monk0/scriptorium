"""BuddhistLLM — the first external-tool dependency impl (tool id "buddhistllm").

Its RAG corpus cites editions by the opaque `pub_id` and looks up everything else live, so its only
load-bearing rule is PURGE → DISALLOW (the token must stay frozen; stability S1). A merge is fine as
long as the cited loser forwards to the winner (force_redirect); withdraw/display edits are harmless
(they propagate into citations via live-join). See docs/access/external_tool_dependency_contract.md
§"First implementation".
"""
from __future__ import annotations

from catalogue.contracts import Capability, ExternalToolDependency, Restriction, Severity


class BuddhistLLMDependency(ExternalToolDependency):
    tool = "buddhistllm"

    def restrict(self, capability: "Capability", entity) -> "Restriction":
        if capability is Capability.PURGE:
            return Restriction(Severity.DISALLOW, "cited by BuddhistLLM; id/pub_id must stay frozen (S1)")
        if capability is Capability.MERGE:
            return Restriction(
                Severity.WARN, "cited by BuddhistLLM; the loser will forward to the winner",
                force_redirect=True)
        if capability is Capability.IDENTITY_EDIT:
            return Restriction(
                Severity.WARN, "cited by BuddhistLLM; a content change should fork a new pub_id")
        return Restriction()   # WITHDRAW, DISPLAY_EDIT, SPLIT → ALLOW (no objection)
