"""Extract structured observations from conversations — context-poisoning-safe.

Design principles (for consumption by models of any capability level):
1. Look for CONVERGENCE SIGNALS (user confirmations after discussion), not just keywords.
2. Store observations as structured fact+resolution, not narrative prose.
3. Cap content at ~200 tokens. Narrative invites misinterpretation.
4. Mark observations with confidence and staleness metadata.
5. Never extract from assistant hedging/exploration — only from confirmed outcomes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from engram.journal.parser import Conversation, ConversationMessage
from engram.memory.observations import ObservationManager


# --- Convergence signals: user confirms something after discussion ---

# User explicitly confirming an outcome
CONFIRMATION_PATTERNS = [
    r"\b(?:yes|yep|yeah|yea|right|correct|exactly|perfect|great)\b",
    r"\b(?:let'?s do (?:that|it|this))\b",
    r"\b(?:go ahead|go with (?:that|it|this))\b",
    r"\b(?:sounds good|works for me|that'?s (?:it|right|correct))\b",
    r"\b(?:done|merged|pushed|deployed|shipped|committed)\b",
    r"\b(?:fixed|confirmed|verified|tested|works now)\b",
    r"\b(?:ok(?:ay)?,?\s+(?:let'?s|now|so|next))\b",
]

# Assistant message patterns that indicate actionable knowledge (not hedging)
OUTCOME_PATTERNS = {
    "bugfix": [
        r"\b(?:the (?:bug|issue|problem|error) was)\b",
        r"\b(?:root cause)\b",
        r"\b(?:fixed (?:by|it|the|this))\b",
        r"\b(?:the fix (?:is|was))\b",
    ],
    "decision": [
        r"\b(?:switched (?:to|from))\b",
        r"\b(?:using|chose|picked|selected)\b.*\b(?:instead|over|rather)\b",
        r"\b(?:we'?re going with|going with)\b",
        r"\b(?:the approach (?:is|will be))\b",
    ],
    "discovery": [
        r"\b(?:turns out)\b",
        r"\b(?:the reason (?:is|was))\b",
        r"\b(?:(?:key|important) (?:thing|point|detail|finding))\b",
        r"\b(?:gotcha|caveat|quirk|limitation)\b",
    ],
    "architecture": [
        r"\b(?:architecture|data flow|system design)\b",
        r"\b(?:stack|pipeline|workflow)\b.*\b(?:is|will be|looks like)\b",
    ],
}

STOPWORDS_FOR_KEY = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "to", "for", "of", "with", "in", "on", "at", "by", "from",
    "and", "or", "but", "not", "that", "this", "it", "we",
}

# Max characters for stored observation content (~200 tokens)
MAX_CONTENT_CHARS = 800


class JournalExtractor:
    """Extract observations from conversations — safe for dumb model consumption."""

    def __init__(self, obs_mgr: ObservationManager):
        self.obs_mgr = obs_mgr

    def extract_and_save(
        self,
        conversations: list[Conversation],
        project: str,
        stale_after_days: int = 90,
    ) -> list[int]:
        """Process conversations, extract confirmed observations, save them."""
        obs_ids = []

        for conv in conversations:
            detected = self._detect_observations(conv)
            for obs in detected:
                topic_key = self._generate_topic_key(obs["title"])
                try:
                    obs_id = self.obs_mgr.save(
                        title=obs["title"],
                        content=obs["content"],
                        type=obs["type"],
                        project=project,
                        topic_key=topic_key,
                    )
                    obs_ids.append(obs_id)
                except Exception:
                    pass

        return obs_ids

    def _detect_observations(self, conversation: Conversation) -> list[dict]:
        """Scan for convergence signals: user confirmation following assistant outcome."""
        results = []
        messages = conversation.messages
        seen_topics = set()  # One observation per topic per conversation

        for i, msg in enumerate(messages):
            # Strategy: look at USER messages for confirmation signals
            if msg.role != "user":
                continue
            if len(msg.content) > 500:
                # Long user messages are usually new context, not confirmations
                continue

            if not self._is_confirmation(msg.content):
                continue

            # User confirmed something — look backward for the assistant outcome
            assistant_msg = self._find_preceding_assistant(messages, i)
            if assistant_msg is None:
                continue

            # Check if the assistant message contains an extractable outcome
            obs_type, matched_sentence = self._classify_outcome(assistant_msg.content)
            if obs_type is None:
                continue

            # Build the structured observation
            title = self._build_title(matched_sentence)
            if title in seen_topics:
                continue
            seen_topics.add(title)

            # Also grab the user's original question (before the assistant answer)
            user_question = self._find_preceding_user_question(messages, i)

            content = self._build_structured_content(
                obs_type=obs_type,
                outcome_text=matched_sentence,
                assistant_full=assistant_msg.content,
                user_question=user_question,
                user_confirmation=msg.content,
            )

            results.append({
                "type": obs_type,
                "title": title,
                "content": content,
            })

        return results

    def _is_confirmation(self, text: str) -> bool:
        """Check if a user message is a confirmation signal."""
        text_lower = text.strip().lower()
        # Very short affirmatives
        if text_lower in ("yes", "yep", "yeah", "ok", "okay", "done", "perfect",
                          "great", "thanks", "right", "correct", "exactly"):
            return True
        # Pattern-based
        for pattern in CONFIRMATION_PATTERNS:
            if re.search(pattern, text_lower):
                return True
        return False

    def _find_preceding_assistant(
        self, messages: list[ConversationMessage], user_idx: int
    ) -> ConversationMessage | None:
        """Find the assistant message immediately before this user confirmation."""
        for j in range(user_idx - 1, -1, -1):
            if messages[j].role == "assistant":
                if len(messages[j].content) >= 30:  # Skip tiny acknowledgments
                    return messages[j]
            elif messages[j].role == "user":
                break  # Hit another user message without finding assistant
        return None

    def _find_preceding_user_question(
        self, messages: list[ConversationMessage], confirm_idx: int
    ) -> str | None:
        """Find the user question that started the exchange being confirmed."""
        found_assistant = False
        for j in range(confirm_idx - 1, -1, -1):
            if messages[j].role == "assistant":
                found_assistant = True
            elif messages[j].role == "user" and found_assistant:
                return messages[j].content[:200]
        return None

    def _classify_outcome(self, text: str) -> tuple[str | None, str]:
        """Check if assistant text contains an extractable outcome. Returns (type, sentence)."""
        sentences = re.split(r'(?<=[.!?])\s+|\n', text)

        for obs_type, patterns in OUTCOME_PATTERNS.items():
            for pattern in patterns:
                for sentence in sentences:
                    if re.search(pattern, sentence, re.IGNORECASE):
                        return obs_type, sentence.strip()

        return None, ""

    def _build_title(self, sentence: str) -> str:
        """Build a short assertive title from the outcome sentence."""
        title = sentence.strip()
        if len(title) > 80:
            title = title[:80].rsplit(" ", 1)[0]
        return title

    def _build_structured_content(
        self,
        obs_type: str,
        outcome_text: str,
        assistant_full: str,
        user_question: str | None,
        user_confirmation: str,
    ) -> str:
        """Build structured observation content — fact+resolution, not narrative.

        Format is designed to be unambiguous for any model reading it:
        - WHAT: the factual statement
        - CONTEXT: why this came up (optional)
        - CONFIRMED: how the user confirmed
        - STATUS: verified/active
        """
        parts = []

        outcome_trimmed = outcome_text[:400]
        parts.append(f"WHAT: {outcome_trimmed}")

        if user_question:
            parts.append(f"CONTEXT: {user_question[:200]}")

        confirm_short = user_confirmation.strip()[:100]
        parts.append(f"CONFIRMED BY USER: {confirm_short}")

        parts.append("STATUS: active")

        content = "\n".join(parts)

        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS]

        return content

    def _generate_topic_key(self, title: str) -> str:
        """Generate a stable topic key from title for upsert."""
        words = re.findall(r'[a-z0-9]+', title.lower())
        filtered = [w for w in words if w not in STOPWORDS_FOR_KEY and len(w) > 1]
        key = "-".join(filtered[:6])
        return key[:50] if key else "unknown"


def mark_stale_observations(obs_mgr: ObservationManager, project: str, days: int = 90):
    """Mark observations older than N days as stale.

    Appends staleness warning to content of old observations.
    Idempotent — won't double-mark.
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    rows = obs_mgr.store.conn.execute(
        "SELECT id, content, updated_at FROM observations WHERE project = ? AND updated_at < ?",
        (project, cutoff),
    ).fetchall()

    marked = 0
    for row in rows:
        content = row["content"]
        if "STATUS: stale" in content:
            continue

        if "STATUS: active" in content:
            new_content = content.replace("STATUS: active", "STATUS: stale — verify if still relevant")
        else:
            new_content = content + "\nSTATUS: stale — verify if still relevant"

        obs_mgr.store.conn.execute(
            "UPDATE observations SET content = ?, updated_at = datetime('now') WHERE id = ?",
            (new_content, row["id"]),
        )
        marked += 1

    obs_mgr.store.conn.commit()
    return marked
