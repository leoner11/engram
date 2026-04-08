"""Parse unified diffs into structured FileDiff objects."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    added_lines: list[int] = field(default_factory=list)
    removed_lines: list[int] = field(default_factory=list)

    @property
    def modified_lines(self) -> list[int]:
        """Union of added and removed line numbers."""
        return sorted(set(self.added_lines + self.removed_lines))


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    hunks: list[DiffHunk]
    is_new: bool = False
    is_deleted: bool = False
    is_renamed: bool = False

    @property
    def path(self) -> str:
        """Best available path (new_path preferred)."""
        return self.new_path or self.old_path or ""

    @property
    def all_modified_lines(self) -> list[int]:
        """All modified line numbers across all hunks (new file numbering)."""
        lines = []
        for hunk in self.hunks:
            lines.extend(hunk.added_lines)
        return sorted(set(lines))

    @property
    def all_removed_lines(self) -> list[int]:
        """All removed line numbers (old file numbering)."""
        lines = []
        for hunk in self.hunks:
            lines.extend(hunk.removed_lines)
        return sorted(set(lines))


HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


class DiffParser:
    """Parse unified diffs into structured objects."""

    def parse(self, diff_text: str) -> list[FileDiff]:
        """Parse a unified diff string."""
        if not diff_text.strip():
            return []

        files: list[FileDiff] = []
        lines = diff_text.splitlines()
        i = 0

        while i < len(lines):
            line = lines[i]

            # Git diff header
            if line.startswith("diff --git"):
                file_diff, i = self._parse_git_diff(lines, i)
                if file_diff:
                    files.append(file_diff)
            # Standard unified diff header
            elif line.startswith("--- "):
                file_diff, i = self._parse_unified_header(lines, i)
                if file_diff:
                    files.append(file_diff)
            else:
                i += 1

        return files

    def parse_file(self, diff_path: Path) -> list[FileDiff]:
        return self.parse(diff_path.read_text())

    def parse_stdin(self) -> list[FileDiff]:
        return self.parse(sys.stdin.read())

    def _parse_git_diff(self, lines: list[str], start: int) -> tuple[FileDiff | None, int]:
        """Parse a git diff block starting at 'diff --git ...'"""
        i = start + 1
        old_path = None
        new_path = None
        is_new = False
        is_deleted = False
        is_renamed = False

        # Read metadata lines until we hit --- or next diff
        while i < len(lines):
            line = lines[i]
            if line.startswith("--- "):
                break
            if line.startswith("diff --git"):
                # Next file — no hunks for this one (binary or empty)
                return None, i
            if line.startswith("new file"):
                is_new = True
            elif line.startswith("deleted file"):
                is_deleted = True
            elif line.startswith("rename from"):
                is_renamed = True
            elif line.startswith("rename to"):
                is_renamed = True
            elif line.startswith("Binary files"):
                return None, i + 1
            i += 1

        if i >= len(lines):
            return None, i

        # Parse --- and +++ lines
        if lines[i].startswith("--- "):
            old_path = self._strip_prefix(lines[i][4:])
            if old_path == "/dev/null":
                old_path = None
                is_new = True
            i += 1

        if i < len(lines) and lines[i].startswith("+++ "):
            new_path = self._strip_prefix(lines[i][4:])
            if new_path == "/dev/null":
                new_path = None
                is_deleted = True
            i += 1

        # Parse hunks
        hunks, i = self._parse_hunks(lines, i)

        return FileDiff(
            old_path=old_path, new_path=new_path, hunks=hunks,
            is_new=is_new, is_deleted=is_deleted, is_renamed=is_renamed,
        ), i

    def _parse_unified_header(self, lines: list[str], start: int) -> tuple[FileDiff | None, int]:
        """Parse standard unified diff starting at '--- '"""
        i = start
        old_path = self._strip_prefix(lines[i][4:])
        if old_path == "/dev/null":
            old_path = None
        i += 1

        new_path = None
        if i < len(lines) and lines[i].startswith("+++ "):
            new_path = self._strip_prefix(lines[i][4:])
            if new_path == "/dev/null":
                new_path = None
            i += 1

        hunks, i = self._parse_hunks(lines, i)

        return FileDiff(
            old_path=old_path, new_path=new_path, hunks=hunks,
            is_new=old_path is None, is_deleted=new_path is None,
        ), i

    def _parse_hunks(self, lines: list[str], start: int) -> tuple[list[DiffHunk], int]:
        """Parse hunk headers and bodies."""
        hunks = []
        i = start

        while i < len(lines):
            line = lines[i]
            if line.startswith("diff --git") or line.startswith("--- "):
                break

            match = HUNK_HEADER_RE.match(line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2)) if match.group(2) else 1
                new_start = int(match.group(3))
                new_count = int(match.group(4)) if match.group(4) else 1
                i += 1

                added = []
                removed = []
                old_line = old_start
                new_line = new_start

                while i < len(lines):
                    l = lines[i]
                    if l.startswith("@@") or l.startswith("diff --git") or l.startswith("--- "):
                        break
                    if l.startswith("+"):
                        added.append(new_line)
                        new_line += 1
                    elif l.startswith("-"):
                        removed.append(old_line)
                        old_line += 1
                    elif l.startswith(" "):
                        old_line += 1
                        new_line += 1
                    elif l.startswith("\\"):
                        pass  # "\ No newline at end of file"
                    else:
                        break
                    i += 1

                hunks.append(DiffHunk(
                    old_start=old_start, old_count=old_count,
                    new_start=new_start, new_count=new_count,
                    added_lines=added, removed_lines=removed,
                ))
            else:
                i += 1

        return hunks, i

    def _strip_prefix(self, path: str) -> str:
        """Strip a/ or b/ prefix from git diff paths."""
        path = path.strip()
        if path.startswith("a/") or path.startswith("b/"):
            return path[2:]
        return path
