"""Apply retrieval feedback boosts to traversal results."""

from __future__ import annotations

from engram.graph.activation import ChangeType
from engram.graph.traversal import AffectedNode
from engram.verification.feedback import RetrievalFeedback


class FeedbackBooster:
    """Apply feedback-based priority boosts to traversal results."""

    def __init__(self, feedback: RetrievalFeedback):
        self.feedback = feedback

    def apply_boosts(
        self,
        affected_nodes: list[AffectedNode],
        change_types: set[ChangeType],
    ) -> list[AffectedNode]:
        """
        Apply feedback-based priority boosts and re-sort.

        Historically-missed nodes get a gentle priority bump:
        priority *= (1 + boost/100)
        """
        ct_strings = {ct.value for ct in change_types}
        boost_map = self.feedback.get_boost_map(ct_strings)

        if not boost_map:
            return affected_nodes

        for node in affected_nodes:
            boost = boost_map.get(node.node_id, 0)
            if boost > 0:
                node.priority *= (1 + boost / 100)

        # Re-sort by priority
        affected_nodes.sort(key=lambda n: -n.priority)
        return affected_nodes
