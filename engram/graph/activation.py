"""Activation rules: which edge kinds propagate which change types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChangeType(str, Enum):
    BODY_MODIFICATION = "BODY_MODIFICATION"
    SIGNATURE_MODIFICATION = "SIGNATURE_MODIFICATION"
    FIELD_ADDITION = "FIELD_ADDITION"
    FIELD_REMOVAL = "FIELD_REMOVAL"
    FIELD_TYPE_CHANGE = "FIELD_TYPE_CHANGE"
    RENAME = "RENAME"
    DELETION = "DELETION"
    ADDITION = "ADDITION"


class EdgeKind(str, Enum):
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    USES_TYPE = "USES_TYPE"
    DEFINES = "DEFINES"
    EXPORTS = "EXPORTS"
    EXTENDS = "EXTENDS"
    API_BRIDGE = "API_BRIDGE"


@dataclass
class ActivationRule:
    edge_kind: EdgeKind
    condition: str | None = None


# Which edge kinds propagate for each change type
ACTIVATION_RULES: dict[ChangeType, list[ActivationRule]] = {
    ChangeType.BODY_MODIFICATION: [
        ActivationRule(EdgeKind.CALLS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.SIGNATURE_MODIFICATION: [
        ActivationRule(EdgeKind.CALLS),
        ActivationRule(EdgeKind.USES_TYPE, condition="usage_pattern != 'passthrough'"),
        ActivationRule(EdgeKind.IMPORTS),
        ActivationRule(EdgeKind.EXTENDS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.FIELD_ADDITION: [
        ActivationRule(EdgeKind.USES_TYPE, condition="usage_pattern == 'exhaustive'"),
        ActivationRule(EdgeKind.EXTENDS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.FIELD_REMOVAL: [
        ActivationRule(EdgeKind.USES_TYPE, condition="accessed_fields includes removed"),
        ActivationRule(EdgeKind.EXTENDS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.FIELD_TYPE_CHANGE: [
        ActivationRule(EdgeKind.USES_TYPE, condition="accessed_fields includes changed"),
        ActivationRule(EdgeKind.EXTENDS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.RENAME: [
        ActivationRule(EdgeKind.CALLS),
        ActivationRule(EdgeKind.USES_TYPE),
        ActivationRule(EdgeKind.IMPORTS),
        ActivationRule(EdgeKind.DEFINES),
        ActivationRule(EdgeKind.EXPORTS),
        ActivationRule(EdgeKind.EXTENDS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.DELETION: [
        ActivationRule(EdgeKind.CALLS),
        ActivationRule(EdgeKind.USES_TYPE),
        ActivationRule(EdgeKind.IMPORTS),
        ActivationRule(EdgeKind.DEFINES),
        ActivationRule(EdgeKind.EXPORTS),
        ActivationRule(EdgeKind.EXTENDS),
        ActivationRule(EdgeKind.API_BRIDGE),
    ],
    ChangeType.ADDITION: [
        ActivationRule(EdgeKind.IMPORTS),      # What does the neighborhood import?
        ActivationRule(EdgeKind.EXTENDS),      # What base classes/interfaces exist?
        ActivationRule(EdgeKind.DEFINES),      # What does the parent module expose?
        ActivationRule(EdgeKind.API_BRIDGE),   # Cross-language contracts to honor
    ],
}
