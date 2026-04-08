"""Change type anticipation: prompt → set of anticipated change types."""

from __future__ import annotations

from engram.graph.activation import ChangeType


KEYWORD_MAP: list[tuple[list[str], set[ChangeType]]] = [
    (["rename", "renaming", "refactor name", "move"],
     {ChangeType.RENAME}),
    (["add field", "add property", "add column", "add attribute", "new field",
      "add a field", "add a property", "add a column", "adding field", "adding a field",
      "field to"],
     {ChangeType.FIELD_ADDITION}),
    (["remove", "delete", "drop", "removing", "deleting"],
     {ChangeType.DELETION, ChangeType.FIELD_REMOVAL}),
    (["change type", "change return", "change parameter", "change signature", "modify signature"],
     {ChangeType.SIGNATURE_MODIFICATION}),
    (["fix", "bug", "update logic", "refactor", "patch", "correct"],
     {ChangeType.BODY_MODIFICATION}),
    (["add function", "create", "implement", "new"],
     {ChangeType.ADDITION}),
    (["add support for", "handle", "extend", "support", "integrate"],
     {ChangeType.FIELD_ADDITION, ChangeType.SIGNATURE_MODIFICATION, ChangeType.BODY_MODIFICATION}),
]

SAFE_DEFAULT = {ChangeType.BODY_MODIFICATION, ChangeType.SIGNATURE_MODIFICATION}


def anticipate_change_types(
    prompt: str,
    change_hints: list[str] | None = None,
    explicit_change_type: str | None = None,
) -> set[ChangeType]:
    """
    Three-tier cascade:
    1. If explicit_change_type → parse and return
    2. If change_hints → keyword match on hints
    3. Else → keyword match on prompt
    4. Nothing matched → SAFE_DEFAULT
    """
    # Tier 1: Explicit override
    if explicit_change_type:
        try:
            return {ChangeType(explicit_change_type)}
        except ValueError:
            pass

    # Tier 2: Agent hints
    if change_hints:
        result = set()
        for hint in change_hints:
            result |= _match_keywords(hint)
        if result:
            return result

    # Tier 3: Prompt keywords
    result = _match_keywords(prompt)
    if result:
        return result

    # Tier 4: Safe default
    return SAFE_DEFAULT


def _match_keywords(text: str) -> set[ChangeType]:
    """Case-insensitive substring matching against keyword map."""
    text_lower = text.lower()
    result: set[ChangeType] = set()
    for keywords, change_types in KEYWORD_MAP:
        for keyword in keywords:
            if keyword in text_lower:
                result |= change_types
                break
    return result
