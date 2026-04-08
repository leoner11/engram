"""Three-pass seed selection: FTS5 → graph boost → feedback."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from engram.graph.store import GraphStore


STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "must", "can", "could",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "if", "then", "else", "when", "where", "why", "how", "what", "which",
    "all", "each", "every", "some", "any", "no", "only", "just",
    "up", "out", "about", "also", "very", "too", "here", "there",
}

ACTION_VERBS = {
    "fix", "add", "remove", "delete", "change", "update", "modify", "refactor",
    "rename", "move", "create", "implement", "handle", "support", "extend",
    "patch", "correct", "improve", "optimize", "debug", "resolve",
}


@dataclass
class SeedCandidate:
    node_id: str
    score: float
    match_reason: str
    pass_scores: dict = field(default_factory=dict)


def split_identifier(name: str) -> list[str]:
    """
    Split camelCase and snake_case identifiers into words.
    "handleStripeWebhook" → ["handle", "stripe", "webhook"]
    "create_event" → ["create", "event"]
    """
    parts = name.split("_")
    words = []
    for part in parts:
        tokens = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', part)
        tokens = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', tokens)
        words.extend(t.lower() for t in tokens.split("_") if t)
    return words


def extract_prompt_terms(prompt: str) -> list[str]:
    """
    Extract search terms from a prompt with priority ordering.

    Returns terms in priority order:
    1. Quoted strings (user explicitly highlighted)
    2. Non-action nouns/identifiers (the "what")
    3. Action verbs (the "how" — useful for change type, less for seeds)
    """
    terms: list[str] = []

    # Extract quoted strings first
    quoted = re.findall(r'["\']([^"\']+)["\']', prompt)
    for q in quoted:
        terms.append(q.lower())

    # Remove quoted sections for remaining extraction
    cleaned = re.sub(r'["\'][^"\']+["\']', '', prompt)

    # Split on whitespace and punctuation (preserve case for identifier splitting)
    raw_original = re.split(r'[\s\-.,;:!?\'"()\[\]{}]+', cleaned)

    # Separate entity terms from action verbs
    entity_terms: list[str] = []
    action_terms: list[str] = []

    for t_orig in raw_original:
        if not t_orig:
            continue
        t = t_orig.lower()
        # Also split on underscores for raw token classification
        sub_parts = t.split("_")
        for sp in sub_parts:
            if not sp or sp in STOPWORDS or len(sp) <= 1:
                continue
            if sp in ACTION_VERBS:
                action_terms.append(sp)
            else:
                entity_terms.append(sp)

        # Expand camelCase/snake_case on the original-case token
        parts = split_identifier(t_orig)
        if len(parts) > 1:
            for p in parts:
                lp = p.lower()
                if lp not in STOPWORDS and len(lp) > 1 and lp not in entity_terms and lp not in action_terms:
                    entity_terms.append(lp)

    # Also add the full lowered token for multi-word identifiers like save_order
    for t_orig in raw_original:
        if not t_orig:
            continue
        t = t_orig.lower()
        if "_" in t or (any(c.isupper() for c in t_orig[1:])):
            if t not in entity_terms and t not in action_terms:
                entity_terms.insert(0, t)  # Full identifier gets high priority

    # Priority order: quoted > entities > actions
    terms.extend(entity_terms)
    terms.extend(action_terms)

    # Deduplicate preserving order
    return list(dict.fromkeys(terms))


class SeedSelector:
    """
    Three-pass seed selection pipeline.

    Pass 1: FTS5 ranked search over node_index
    Pass 2: Graph-aware co-occurrence boosting
    Pass 3: Historical feedback integration
    Then: dynamic seed count based on confidence distribution
    """

    def __init__(self, store: GraphStore):
        self.store = store

    def select(
        self,
        prompt: str,
        explicit_seeds: list[str] | None = None,
        max_seeds: int = 6,
    ) -> list[SeedCandidate]:
        """
        Main entry point. Returns ranked seed candidates.

        If explicit_seeds provided, validate and return them.
        Otherwise, run the three-pass pipeline.
        """
        if explicit_seeds:
            return self._validate_explicit(explicit_seeds)

        terms = extract_prompt_terms(prompt)
        if not terms:
            return self._fallback_top_connected(max_seeds)

        # Pass 1: FTS5 search
        candidates = self._pass1_fts5(terms, candidate_limit=20)
        if not candidates:
            # Fallback 1a: File path matching (user mentioned a filename)
            candidates = self._pass1_filepath(terms, prompt, candidate_limit=10)
        if not candidates:
            # Fallback 1b: substring matching (backward compat)
            candidates = self._pass1_fallback(terms, candidate_limit=15)
        if not candidates:
            # Last resort: most-connected exported nodes
            return self._fallback_top_connected(max_seeds)

        # Pass 2: Graph co-occurrence boost
        candidates = self._pass2_graph_boost(candidates)

        # Pass 3: Historical feedback
        candidates = self._pass3_feedback(candidates, terms)

        # Compute final scores and sort
        for c in candidates:
            c.score = sum(c.pass_scores.values())
        candidates.sort(key=lambda c: -c.score)

        # Dynamic seed count
        return self._dynamic_cutoff(candidates, max_seeds)

    def find_candidates(
        self,
        prompt: str,
        limit: int = 15,
    ) -> list[SeedCandidate]:
        """
        Return the full ranked candidate list without dynamic cutoff.

        Used by engram_find_nodes to show the orchestrator all candidate seeds
        so it can make an informed choice before committing to a full query.

        Unlike select(), this:
        - Never truncates to max_seeds
        - Returns up to `limit` candidates
        - Includes pass score breakdown so the caller can see why each node scored
        - Falls back gracefully at every stage same as select()
        """
        terms = extract_prompt_terms(prompt)
        if not terms:
            return self._fallback_top_connected(limit)

        # Pass 1
        candidates = self._pass1_fts5(terms, candidate_limit=limit * 2)
        if not candidates:
            candidates = self._pass1_filepath(terms, prompt, candidate_limit=limit)
        if not candidates:
            candidates = self._pass1_fallback(terms, candidate_limit=limit * 2)
        if not candidates:
            return self._fallback_top_connected(limit)

        # Pass 2 + 3
        candidates = self._pass2_graph_boost(candidates)
        candidates = self._pass3_feedback(candidates, terms)

        # Final scoring — no cutoff
        for c in candidates:
            c.score = sum(c.pass_scores.values())
        candidates.sort(key=lambda c: -c.score)

        return candidates[:limit]

    def _pass1_filepath(self, terms: list[str], prompt: str, candidate_limit: int = 10) -> list[SeedCandidate]:
        """
        Match file paths mentioned in the prompt.

        Handles patterns like:
        - "fix the bug in webhooks.py"
        - "update src/api/views.py"
        - "the EventList component" → might match EventList.tsx

        Returns non-FILE nodes from matched files as seed candidates.
        """
        # Extract potential file references from the raw prompt
        file_refs = self._extract_file_references(prompt)
        if not file_refs:
            return []

        candidates = []
        all_nodes = self.store.get_all_nodes()

        # Group all non-FILE nodes by their file_path
        nodes_by_file: dict[str, list] = {}
        for node_id, node in all_nodes.items():
            if node.kind != "FILE":
                nodes_by_file.setdefault(node.file_path, []).append(node)

        for file_path, nodes in nodes_by_file.items():
            file_lower = file_path.lower()
            file_name = file_path.rsplit("/", 1)[-1].lower()
            file_stem = file_name.rsplit(".", 1)[0]

            for ref in file_refs:
                ref_lower = ref.lower()
                matched = False

                # Exact filename match: "webhooks.py"
                if ref_lower == file_name:
                    matched = True
                # Stem match: "webhooks" matches "webhooks.py"
                elif ref_lower == file_stem:
                    matched = True
                # Path suffix match: "api/views.py" matches "src/api/views.py"
                elif file_lower.endswith(ref_lower):
                    matched = True

                if matched:
                    for node in nodes:
                        in_degree = self.store.get_in_degree(node.id)
                        export_mult = 1.3 if node.is_exported else 1.0
                        score = 8.0 * export_mult * max(math.log2(in_degree + 1), 1.0)

                        candidates.append(SeedCandidate(
                            node_id=node.id,
                            score=0,
                            match_reason="filepath",
                            pass_scores={"fts5": score},
                        ))

        candidates.sort(key=lambda c: -c.pass_scores["fts5"])
        return candidates[:candidate_limit]

    def _extract_file_references(self, prompt: str) -> list[str]:
        """Extract potential file path references from a prompt string."""
        refs = []

        # Pattern 1: explicit paths with extensions
        # Matches: "webhooks.py", "src/api/views.py", "useEvents.ts", "EventList.tsx"
        path_pattern = re.findall(r'(?:[\w./\\-]+\.(?:py|tsx|jsx|ts|js|dart|go|rs|java))', prompt)
        refs.extend(path_pattern)

        # Pattern 2: "in <filename>" or "the <filename>" without extension
        # Matches: "in webhooks", "the EventList component"
        in_pattern = re.findall(r'(?:in|the|from|at)\s+([A-Z][\w]+|[\w]+\.[\w]+)', prompt)
        refs.extend(in_pattern)

        return list(dict.fromkeys(refs))  # Deduplicate preserving order

    def _fallback_top_connected(self, max_seeds: int) -> list[SeedCandidate]:
        """
        Last resort: return the most-connected exported nodes.

        Used when no terms match anything. Better than returning nothing —
        gives the agent the project's most important entry points with a warning.
        """
        all_nodes = self.store.get_all_nodes()
        scored = []

        for node_id, node in all_nodes.items():
            if node.kind == "FILE":
                continue
            if not node.is_exported:
                continue
            in_degree = self.store.get_in_degree(node_id)
            scored.append((node_id, in_degree))

        scored.sort(key=lambda x: -x[1])
        top = scored[:max_seeds]

        return [
            SeedCandidate(
                node_id=nid,
                score=float(degree),
                match_reason="top_connected_fallback",
                pass_scores={"fallback": float(degree)},
            )
            for nid, degree in top
        ]

    # --- Pass 1: FTS5 Search ---

    def _pass1_fts5(self, terms: list[str], candidate_limit: int = 20) -> list[SeedCandidate]:
        """
        Ranked FTS5 search over node_index.

        BM25 column weights: node_id=0, name=10, signature=5, docstring=4,
        decorators=2, source_preview=1.
        """
        safe_terms = [t for t in terms if re.match(r'^[a-zA-Z0-9_]+$', t)]
        if not safe_terms:
            return []

        fts_query = " OR ".join(safe_terms)

        try:
            rows = self.store.conn.execute(
                """
                SELECT node_id,
                       bm25(node_index, 0, 10.0, 5.0, 4.0, 2.0, 1.0) as bm25_score
                FROM node_index
                WHERE node_index MATCH ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (fts_query, candidate_limit),
            ).fetchall()
        except Exception:
            return []

        candidates = []
        for row in rows:
            node_id = row["node_id"]
            bm25 = abs(row["bm25_score"])

            node = self.store.get_node(node_id)
            if node is None or node.kind == "FILE":
                continue

            export_mult = 1.3 if node.is_exported else 1.0
            fts_score = bm25 * export_mult

            candidates.append(SeedCandidate(
                node_id=node_id,
                score=0,
                match_reason="fts5",
                pass_scores={"fts5": fts_score},
            ))

        return candidates

    def _pass1_fallback(self, terms: list[str], candidate_limit: int = 15) -> list[SeedCandidate]:
        """
        Fallback substring matching when FTS5 index doesn't exist or returns nothing.
        Preserves current v0 behavior for backward compatibility.
        """
        all_nodes = self.store.get_all_nodes()
        scored: list[SeedCandidate] = []

        for node_id, node in all_nodes.items():
            if node.kind == "FILE":
                continue

            match_score = 0.0
            node_name_lower = node.name.lower()
            node_words = split_identifier(node.name)

            for term in terms:
                if term in node_name_lower:
                    match_score += 10
                elif term in node_words:
                    match_score += 5
                else:
                    sig = (node.signature or "").lower()
                    doc = (node.docstring or "").lower()
                    if term in sig or term in doc:
                        match_score += 2

            if match_score > 0:
                export_mult = 1.3 if node.is_exported else 1.0
                scored.append(SeedCandidate(
                    node_id=node_id,
                    score=0,
                    match_reason="fallback_substring",
                    pass_scores={"fts5": match_score * export_mult},
                ))

        scored.sort(key=lambda c: -c.pass_scores["fts5"])
        return scored[:candidate_limit]

    # --- Pass 2: Graph Co-occurrence Boost ---

    def _pass2_graph_boost(self, candidates: list[SeedCandidate]) -> list[SeedCandidate]:
        """
        Boost candidates that are graph-neighbors of other candidates.

        If two candidates are within 2 hops, both get boosted — they're
        likely part of the same subsystem. Isolated candidates get penalized.
        """
        NEIGHBOR_BOOST_MULT = 0.3  # Each neighbor adds 30% of FTS5 score
        ISOLATION_PENALTY = 0.7

        candidate_ids = {c.node_id for c in candidates}
        neighbor_counts: dict[str, int] = {c.node_id: 0 for c in candidates}

        for c in candidates:
            # 1-hop neighbors
            one_hop: set[str] = set()
            for edge in self.store.get_edges_from(c.node_id):
                one_hop.add(edge.target_id)
            for edge in self.store.get_edges_to(c.node_id):
                one_hop.add(edge.source_id)

            # Direct candidate neighbors
            direct = one_hop & candidate_ids - {c.node_id}
            neighbor_counts[c.node_id] += len(direct)

            # 2-hop: check if any 1-hop neighbor connects to another candidate
            for hop1_id in one_hop:
                if hop1_id in candidate_ids:
                    continue
                found = False
                for edge in self.store.get_edges_from(hop1_id):
                    if edge.target_id in candidate_ids and edge.target_id != c.node_id:
                        neighbor_counts[c.node_id] += 1
                        found = True
                        break
                if not found:
                    for edge in self.store.get_edges_to(hop1_id):
                        if edge.source_id in candidate_ids and edge.source_id != c.node_id:
                            neighbor_counts[c.node_id] += 1
                            break

        for c in candidates:
            nc = neighbor_counts[c.node_id]
            fts5_base = c.pass_scores.get("fts5", 0)
            if nc > 0:
                # Multiplicative boost: graph connectivity amplifies lexical relevance
                # A node with fts5=0 stays at ~0 even with many neighbors
                boost_mult = 1.0 + (NEIGHBOR_BOOST_MULT * min(nc, 5))
                c.pass_scores["graph"] = fts5_base * (boost_mult - 1.0)
            else:
                c.pass_scores["fts5"] *= ISOLATION_PENALTY
                c.pass_scores["graph"] = 0.0

        return candidates

    # --- Pass 3: Historical Feedback ---

    def _pass3_feedback(self, candidates: list[SeedCandidate], terms: list[str]) -> list[SeedCandidate]:
        """
        Boost candidates based on historical signals:
        1. File-path affinity from seed_history
        2. Previously missed nodes from retrieval_feedback
        """
        FILE_AFFINITY_BOOST = 3.0
        MISSED_NODE_BOOST = 5.0

        # Signal 1: File-path affinity
        try:
            affinity_paths: dict[str, int] = {}
            rows = self.store.conn.execute(
                "SELECT file_paths FROM seed_history ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            for row in rows:
                try:
                    paths = json.loads(row["file_paths"])
                    for p in paths:
                        parts = p.rsplit("/", 1)
                        directory = parts[0] if len(parts) > 1 else ""
                        affinity_paths[directory] = affinity_paths.get(directory, 0) + 1
                except (json.JSONDecodeError, TypeError):
                    continue

            if affinity_paths:
                max_count = max(affinity_paths.values())
                for c in candidates:
                    node = self.store.get_node(c.node_id)
                    if node:
                        parts = node.file_path.rsplit("/", 1)
                        directory = parts[0] if len(parts) > 1 else ""
                        count = affinity_paths.get(directory, 0)
                        if count > 0:
                            c.pass_scores["feedback"] = c.pass_scores.get("feedback", 0) + \
                                FILE_AFFINITY_BOOST * (count / max_count)
        except Exception:
            pass

        # Signal 2: Previously missed nodes
        try:
            for c in candidates:
                row = self.store.conn.execute(
                    "SELECT COUNT(*) as c FROM retrieval_feedback WHERE missed_node = ?",
                    (c.node_id,),
                ).fetchone()
                missed_count = row["c"] if row else 0
                if missed_count > 0:
                    c.pass_scores["feedback"] = c.pass_scores.get("feedback", 0) + \
                        MISSED_NODE_BOOST * min(missed_count, 3)
        except Exception:
            pass

        return candidates

    # --- Dynamic Seed Count ---

    def _dynamic_cutoff(self, candidates: list[SeedCandidate], max_seeds: int) -> list[SeedCandidate]:
        """
        Use confidence gap analysis instead of fixed count.

        1. Top score 2x+ the second → 1 seed
        2. Take candidates until score drops below 40% of top
        3. Hard cap: max_seeds
        """
        if not candidates:
            return []

        if len(candidates) == 1:
            return candidates[:1]

        top_score = candidates[0].score
        if top_score <= 0:
            return candidates[:1]

        # Clear single target
        if candidates[0].score >= 2.0 * candidates[1].score:
            return candidates[:1]

        # Confidence gap
        threshold = top_score * 0.4
        selected = [candidates[0]]

        for c in candidates[1:]:
            if c.score >= threshold and len(selected) < max_seeds:
                selected.append(c)
            else:
                break

        return selected

    # --- Explicit Seeds ---

    def _validate_explicit(self, seed_ids: list[str]) -> list[SeedCandidate]:
        """Validate explicit seed IDs and return as candidates."""
        result = []
        for seed_id in seed_ids:
            node = self.store.get_node(seed_id)
            if node:
                result.append(SeedCandidate(
                    node_id=seed_id,
                    score=1000.0,
                    match_reason="explicit",
                    pass_scores={"explicit": 1000.0},
                ))
        return result

    # --- Post-query Recording ---

    def record_selection(self, prompt: str, terms: list[str], selected: list[SeedCandidate]):
        """Record seed selection in seed_history for future feedback."""
        query_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        seed_ids = [c.node_id for c in selected]
        file_paths = []
        for c in selected:
            node = self.store.get_node(c.node_id)
            if node:
                file_paths.append(node.file_path)

        try:
            self.store.conn.execute(
                """INSERT INTO seed_history (query_hash, prompt_terms, seed_ids, file_paths)
                   VALUES (?, ?, ?, ?)""",
                (query_hash, json.dumps(terms), json.dumps(seed_ids), json.dumps(file_paths)),
            )
            self.store.conn.commit()
        except Exception:
            pass


# --- FTS5 Index Population (called at build time) ---

def populate_node_index(store: GraphStore):
    """Populate the FTS5 node_index table from current nodes."""
    try:
        store.conn.execute("DELETE FROM node_index")
    except Exception:
        return  # Table doesn't exist

    all_nodes = store.get_all_nodes()
    for node_id, node in all_nodes.items():
        if node.kind == "FILE":
            continue

        # Expand name so "handleStripeWebhook" also indexes as "handle stripe webhook"
        name_expanded = node.name + " " + " ".join(split_identifier(node.name))

        source_preview = (node.full_source or "")[:300]
        decorators_text = " ".join(node.decorators) if node.decorators else ""

        store.conn.execute(
            """INSERT INTO node_index (node_id, name, signature, docstring, decorators, source_preview)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                name_expanded,
                node.signature or "",
                (node.docstring or "")[:500],
                decorators_text,
                source_preview,
            ),
        )
    store.conn.commit()


# --- Legacy compat wrapper ---

def select_seeds(
    prompt: str,
    store: GraphStore,
    explicit_seeds: list[str] | None = None,
    max_seeds: int = 3,
) -> list[SeedCandidate]:
    """
    Legacy wrapper for backward compatibility.
    Delegates to SeedSelector.select().
    """
    selector = SeedSelector(store)
    return selector.select(prompt, explicit_seeds=explicit_seeds, max_seeds=max_seeds)