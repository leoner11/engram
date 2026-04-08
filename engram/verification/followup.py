"""Generate follow-up prompts for incomplete patches."""

from __future__ import annotations

from engram.verification.verifier import VerificationResult, Verdict, MissingNode


class FollowUpGenerator:
    """Generate structured follow-up prompts for agents."""

    def generate(self, result: VerificationResult) -> str:
        """Generate a follow-up prompt from a verification result."""
        if result.verdict == Verdict.STRUCTURALLY_COMPLETE:
            return (
                f"✓ Patch is STRUCTURALLY COMPLETE. "
                f"{result.stats['touched_count']} affected nodes verified, all were updated."
            )

        lines = []
        lines.append("⚠ Your patch is INCOMPLETE. The following nodes were affected by your change")
        lines.append("but were not included in the patch:")
        lines.append("")

        # Group by confidence
        high = [m for m in result.missing_nodes if m.confidence == "high"]
        medium = [m for m in result.missing_nodes if m.confidence == "medium"]
        low = [m for m in result.missing_nodes if m.confidence == "low"]

        idx = 1

        if high:
            lines.append("## Missing updates (HIGH confidence — almost certainly needs fixing):")
            lines.append("")
            for m in high:
                lines.append(f"{idx}. {m.file_path}::{m.node_id.split('::')[-1]} (lines {m.line_start}-{m.line_end})")
                lines.append(f"   WHY: {m.reason}")
                lines.append(f"   LIKELY FIX: {self._suggest_fix(m)}")
                lines.append("")
                idx += 1

        if medium:
            lines.append("## Missing updates (MEDIUM confidence — likely needs fixing):")
            lines.append("")
            for m in medium:
                lines.append(f"{idx}. {m.file_path}::{m.node_id.split('::')[-1]} (lines {m.line_start}-{m.line_end})")
                lines.append(f"   WHY: {m.reason}")
                lines.append(f"   LIKELY FIX: {self._suggest_fix(m)}")
                lines.append("")
                idx += 1

        if low:
            lines.append("## Possibly affected (LOW confidence — review if relevant):")
            lines.append("")
            for m in low:
                lines.append(f"{idx}. {m.node_id} (lines {m.line_start}-{m.line_end})")
                lines.append(f"   WHY: {m.reason}")
                lines.append("")
                idx += 1

        lines.append("Please review and update these files.")
        return "\n".join(lines)

    def _suggest_fix(self, missing: MissingNode) -> str:
        """Generate a one-line fix suggestion."""
        edge = missing.edge_kind
        ct = missing.change_type

        if edge == "CALLS":
            if "SIGNATURE" in ct:
                return "Update the call to match the new function signature."
            elif "RENAME" in ct:
                return "Update the call to use the new function name."
            return "Review the call site for behavioral changes."

        if edge == "EXTENDS":
            if "SIGNATURE" in ct:
                return "Update super() call or overridden method to match new parent signature."
            return "Review inherited behavior for breaking changes."

        if edge == "USES_TYPE":
            if "FIELD_ADDITION" in ct:
                return "Add handling for the new field."
            if "FIELD_REMOVAL" in ct:
                return "Remove references to the deleted field."
            return "Review type usage for compatibility."

        if edge == "IMPORTS":
            if "RENAME" in ct:
                return "Update the import to use the new name."
            if "DELETION" in ct:
                return "Remove or replace the import of the deleted symbol."
            return "Review the import statement."

        return "Review this code for compatibility with the changes."
