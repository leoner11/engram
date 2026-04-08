"""Parse conversation export files into structured conversations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConversationMessage:
    role: str
    content: str
    timestamp: str | None = None
    tool_name: str | None = None


@dataclass
class Conversation:
    id: str
    title: str | None
    messages: list[ConversationMessage]
    project: str | None = None
    created_at: str | None = None


class ExportParser:
    """Parse conversation exports into structured Conversation objects."""

    def parse(self, file_path: Path) -> list[Conversation]:
        """Parse a JSON conversation export file."""
        text = file_path.read_text(encoding="utf-8")
        data = json.loads(text)

        if isinstance(data, list):
            return self._parse_conversation_list(data)
        elif isinstance(data, dict):
            # Single conversation
            conv = self._parse_single(data)
            return [conv] if conv else []
        return []

    def _parse_conversation_list(self, data: list[dict]) -> list[Conversation]:
        results = []
        for item in data:
            conv = self._parse_single(item)
            if conv:
                results.append(conv)
        return results

    def _parse_single(self, data: dict) -> Conversation | None:
        messages = []
        raw_messages = data.get("messages", data.get("conversation", []))

        for msg in raw_messages:
            if isinstance(msg, dict):
                role = msg.get("role", "unknown")
                content = msg.get("content", "")

                # Handle content that's a list of blocks (Claude format)
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                messages.append(ConversationMessage(
                                    role="tool_use",
                                    content=json.dumps(block.get("input", {})),
                                    tool_name=block.get("name"),
                                ))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    if text_parts:
                        content = "\n".join(text_parts)
                    else:
                        continue

                if isinstance(content, str) and content.strip():
                    messages.append(ConversationMessage(
                        role=role,
                        content=content,
                        timestamp=msg.get("timestamp"),
                    ))

        if not messages:
            return None

        return Conversation(
            id=data.get("id", data.get("uuid", "")),
            title=data.get("title", data.get("name")),
            messages=messages,
            project=data.get("project"),
            created_at=data.get("created_at", data.get("timestamp")),
        )
