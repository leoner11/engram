"""Bridge suggestion engine: scan indexed nodes for implicit cross-language connections.

Detects patterns like:
  - Django/Flask render() calls → template files
  - render_template(), TemplateResponse(), get_template() → template files
  - TypeScript/JS fetch('/api/...') → Python view nodes serving that route
  - @api_view / APIRouter decorators ↔ frontend fetch calls

Returns BridgeSuggestion objects that agents can confirm to auto-generate engram.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from engram.graph.store import GraphStore


@dataclass
class BridgeSuggestion:
    from_node: str
    to_file: str                    # Target file path (template, api file, etc.)
    to_node: str | None             # Specific node if resolvable
    bridge_type: str                # "view_template" | "api_call" | "component_api"
    confidence: float               # 0.0 - 1.0
    reason: str                     # Human-readable: "render() call found: 'jobs/list.html'"
    bridge_name: str = ""           # Auto-generated name for engram.yaml


# --- Regex patterns for each bridge type ---

# Django: render(request, "path/to/template.html") or render(request, 'path/to/template.html')
_DJANGO_RENDER = re.compile(
    r"""render\s*\(\s*\w+\s*,\s*['"]([^'"]+\.html)['"]""",
    re.MULTILINE,
)

# Django: TemplateResponse(request, "path/to/template.html")
_TEMPLATE_RESPONSE = re.compile(
    r"""TemplateResponse\s*\(\s*\w+\s*,\s*['"]([^'"]+\.html)['"]""",
    re.MULTILINE,
)

# Flask: render_template("path/to/template.html")
_FLASK_RENDER = re.compile(
    r"""render_template\s*\(\s*['"]([^'"]+\.html)['"]""",
    re.MULTILINE,
)

# Jinja2: get_template("path/to/template.html")
_GET_TEMPLATE = re.compile(
    r"""get_template\s*\(\s*['"]([^'"]+\.html)['"]""",
    re.MULTILINE,
)

# JS/TS: fetch('/api/some/path') or axios.get('/api/some/path')
_FETCH_API = re.compile(
    r"""(?:fetch|axios\.(?:get|post|put|patch|delete))\s*\(\s*['"`](/api/[^'"`\s?#]+)""",
    re.MULTILINE,
)

# Django URL patterns: path('some/path/', views.some_view)
_URL_PATH = re.compile(
    r"""path\s*\(\s*['"]([^'"]+)['"]\s*,\s*(?:\w+\.)?(\w+)""",
    re.MULTILINE,
)

_TEMPLATE_PATTERNS = [
    (_DJANGO_RENDER, "django_render"),
    (_TEMPLATE_RESPONSE, "template_response"),
    (_FLASK_RENDER, "flask_render"),
    (_GET_TEMPLATE, "get_template"),
]


def suggest_bridges(root: Path, store: GraphStore) -> list[BridgeSuggestion]:
    """
    Scan all indexed nodes for implicit cross-language connections.
    Returns suggestions sorted by confidence descending.
    """
    suggestions: list[BridgeSuggestion] = []

    all_nodes = store.get_all_nodes()

    # Build a quick lookup: template path → exists on disk
    template_cache: dict[str, bool] = {}

    def _template_exists(template_path: str) -> tuple[bool, str]:
        """Check if template exists; return (exists, resolved_path)."""
        if template_path in template_cache:
            return template_cache[template_path], template_path

        # Try direct path
        if (root / template_path).exists():
            template_cache[template_path] = True
            return True, template_path

        # Try under common template dirs
        for prefix in ["templates/", "app/templates/", "src/templates/"]:
            candidate = prefix + template_path
            if (root / candidate).exists():
                template_cache[template_path] = True
                return True, candidate

        template_cache[template_path] = False
        return False, template_path

    # Build URL-to-view map from urls.py files for API bridge detection
    url_to_view: dict[str, str] = {}  # url_pattern → node_id
    for node_id, node in all_nodes.items():
        if node.kind == "FILE":
            continue
        filename = Path(node.file_path).name
        if filename in ("urls.py", "router.py", "routes.py") and node.full_source:
            for match in _URL_PATH.finditer(node.full_source):
                url_pat, view_name = match.group(1), match.group(2)
                # Find matching view node
                for nid, n in all_nodes.items():
                    if n.name == view_name and "view" in n.file_path.lower():
                        url_to_view[url_pat] = nid
                        break

    # Scan each node for template render calls
    for node_id, node in all_nodes.items():
        if node.kind == "FILE" or not node.full_source:
            continue

        # --- Template bridge detection ---
        for pattern, pattern_name in _TEMPLATE_PATTERNS:
            for match in pattern.finditer(node.full_source):
                template_path = match.group(1)
                exists, resolved = _template_exists(template_path)

                confidence = 0.95 if exists else 0.60
                reason = f"{pattern_name} call found: '{template_path}'"
                if exists:
                    reason += " (file verified on disk)"
                else:
                    reason += " (file not found — may be generated or in different root)"

                # Auto-generate bridge name from node and template
                node_name = node_id.split("::")[-1] if "::" in node_id else node.name
                tmpl_stem = Path(template_path).stem
                bridge_name = f"{node_name}-{tmpl_stem}"

                suggestions.append(BridgeSuggestion(
                    from_node=node_id,
                    to_file=resolved,
                    to_node=None,
                    bridge_type="view_template",
                    confidence=confidence,
                    reason=reason,
                    bridge_name=bridge_name,
                ))

        # --- API call bridge detection (JS/TS → Python view) ---
        if node.language in ("typescript", "javascript"):
            for match in _FETCH_API.finditer(node.full_source):
                api_path = match.group(1)

                # Try to match to a known URL pattern
                matched_view = None
                for url_pat, view_node_id in url_to_view.items():
                    # Simple prefix match — url_pat like "students/" vs "/api/students/"
                    if url_pat.strip("/") in api_path:
                        matched_view = view_node_id
                        break

                if matched_view:
                    node_name = node_id.split("::")[-1] if "::" in node_id else node.name
                    view_name = matched_view.split("::")[-1]
                    suggestions.append(BridgeSuggestion(
                        from_node=node_id,
                        to_file=all_nodes[matched_view].file_path,
                        to_node=matched_view,
                        bridge_type="api_call",
                        confidence=0.80,
                        reason=f"fetch('{api_path}') matches URL pattern → {matched_view}",
                        bridge_name=f"{node_name}-{view_name}-api",
                    ))

    # Deduplicate: same (from_node, to_file) pair → keep highest confidence
    seen: dict[tuple[str, str], BridgeSuggestion] = {}
    for s in suggestions:
        key = (s.from_node, s.to_file)
        if key not in seen or s.confidence > seen[key].confidence:
            seen[key] = s

    result = sorted(seen.values(), key=lambda s: -s.confidence)
    return result


def suggestions_to_yaml(suggestions: list[BridgeSuggestion], confirmed: list[int] | None = None) -> str:
    """
    Convert confirmed suggestions to engram.yaml bridge declarations.

    confirmed: list of indices into suggestions to include. None = include all.
    """
    to_include = (
        [suggestions[i] for i in confirmed if i < len(suggestions)]
        if confirmed is not None
        else suggestions
    )

    if not to_include:
        return ""

    lines = ["bridges:"]
    for s in to_include:
        lines.append(f"  - name: \"{s.bridge_name}\"")
        lines.append(f"    from:")
        lines.append(f"      node: \"{s.from_node}\"")
        if s.to_node:
            lines.append(f"    to:")
            lines.append(f"      node: \"{s.to_node}\"")
        else:
            lines.append(f"    to:")
            lines.append(f"      files: [\"{s.to_file}\"]")
        lines.append(f"    bidirectional: true")
        lines.append("")

    return "\n".join(lines)


def merge_into_config(root: Path, new_yaml: str) -> str:
    """
    Merge new bridge declarations into existing engram.yaml.
    If no engram.yaml exists, returns the new_yaml as-is.
    Returns the final yaml string (caller should write to disk).
    """
    existing_path = root / "engram.yaml"

    if not existing_path.exists():
        return new_yaml

    import yaml
    existing = yaml.safe_load(existing_path.read_text(encoding="utf-8")) or {}
    new_config = yaml.safe_load(new_yaml) or {}

    existing_bridges = existing.get("bridges", [])
    new_bridges = new_config.get("bridges", [])

    # Deduplicate by bridge name
    existing_names = {b.get("name") for b in existing_bridges}
    for bridge in new_bridges:
        if bridge.get("name") not in existing_names:
            existing_bridges.append(bridge)

    existing["bridges"] = existing_bridges
    return yaml.dump(existing, default_flow_style=False, sort_keys=False)
