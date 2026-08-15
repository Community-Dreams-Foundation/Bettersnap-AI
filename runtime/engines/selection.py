"""SlotSelectionEngine — implements domain.SelectionEngine (ARCHITECTURE.md §5).

Picks the best-scoring candidate PER SLOT and marks it accepted iff it clears the plan's
acceptance_threshold. NEVER shorts a paid slot: every slot that has at least one candidate
gets exactly one Winner (the best available, even if none cleared the bar) — so the
orchestrator can decide whether to spend retry budget on the not-accepted slots, but the
customer always receives their billable_count of images. Pure logic (no torch, no I/O), so
it is fully unit-testable.
"""
from __future__ import annotations

from domain import ScoredCandidate, Winner


class SlotSelectionEngine:
    def __init__(self, log=lambda m: None):
        self.log = log

    def select(self, scored, plan):
        by_slot: dict[int, list] = {}
        for s in scored:
            slot = s.candidate.slot_id if s.candidate.slot_id is not None else 0
            by_slot.setdefault(slot, []).append(s)

        # Deliver in the plan's slot order when it defines slots; else natural slot order.
        slot_order = [sl.slot_id for sl in plan.slots] if plan.slots else sorted(by_slot)

        winners = []
        for slot in slot_order:
            cands = by_slot.get(slot)
            if not cands:
                continue  # no candidate yet for this slot (orchestrator may still be generating)
            best = max(cands, key=lambda x: x.scores.identity)
            accepted = best.scores.identity >= plan.acceptance_threshold
            winners.append(
                Winner(ScoredCandidate(best.candidate, best.scores, accepted=accepted), slot_id=slot)
            )
        return winners
