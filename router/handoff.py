from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_NO_GIT = "_No git repository detected or `git` is unavailable._"
_MAX_TODO_ITEMS = 12
_SUMMARY_LIMIT = 600


class HandoffManager:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path(__file__).resolve().parents[1] / "HANDOVER.md"

    def read(self) -> str:
        try:
            if not self.path.exists():
                return ""
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def update(
        self,
        *,
        model: str,
        provider: str = "",
        summary: str = "",
        notes: str = "",
    ) -> str:
        previous_todos = self._extract_todos(self.read())
        if notes:
            previous_todos.append(notes.strip())
        content = self._render(model=model, provider=provider, summary=summary, todos=previous_todos[-_MAX_TODO_ITEMS:])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(content, encoding="utf-8")
        except OSError:
            pass
        return content

    def ensure(self, *, model: str = "n/d", provider: str = "", summary: str = "") -> str:
        if self.read().strip():
            return self.read()
        return self.update(model=model, provider=provider, summary=summary)

    def _render(self, *, model: str, provider: str, summary: str, todos: list[str]) -> str:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary_text = summary.strip() or "_No context recorded._"
        if len(summary_text) > _SUMMARY_LIMIT:
            summary_text = summary_text[:_SUMMARY_LIMIT].rstrip() + "..."
        todo_lines = "\n".join(f"- {item}" for item in todos) if todos else "- _No decisions recorded._"
        return (
            "# HANDOVER — Handoff between AI agents\n"
            "\n"
            "## Last Model Used & Timestamp\n"
            f"- Model: `{model}`\n"
            f"- Provider: `{provider or 'n/d'}`\n"
            f"- Timestamp UTC: {timestamp}\n"
            "\n"
            "## Current Task Status / Goal\n"
            f"{summary_text}\n"
            "\n"
            "## Recently Modified Files\n"
            f"{self._git_changes()}\n"
            "\n"
            "## Architectural Decisions & TODO for the Next Agent\n"
            f"{todo_lines}\n"
        )

    def _git_changes(self) -> str:
        git = shutil.which("git")
        if not git:
            return _NO_GIT
        try:
            result = subprocess.run(
                [git, "status", "--short"],
                cwd=self.path.parent,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return _NO_GIT
        output = (result.stdout or "").strip()
        if result.returncode != 0 or not output:
            return "_No changes detected (or not a git repository)._"
        return "```\n" + output + "\n```"

    @staticmethod
    def _extract_todos(content: str) -> list[str]:
        match = re.search(r"##\s+Architectural Decisions.*?\n(.*?)(?=\n##\s|\Z)", content, flags=re.DOTALL)
        if not match:
            return []
        todos: list[str] = []
        for line in match.group(1).splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                item = stripped[2:].strip()
                if item and not item.startswith("_"):
                    todos.append(item)
        return todos


def handover_messages(content: str) -> list[dict[str, Any]]:
    if not content.strip():
        return []
    return [{"role": "system", "content": "## HANDOVER.md — handoff context between agents\n\n" + content}]
