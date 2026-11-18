from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "skills" / "registry.json"
_registry_cache: list[dict[str, Any]] | None = None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_registry() -> list[dict[str, Any]]:
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    skills: list[dict[str, Any]] = []
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        for item in data.get("skills", []):
            if not isinstance(item, dict) or not item.get("name") or not item.get("script"):
                continue
            script = (ROOT / str(item["script"])).resolve()
            try:
                script.relative_to(ROOT)
            except ValueError:
                continue
            if script.suffix != ".py" or not script.is_file():
                continue
            skills.append({**item, "_script_path": str(script)})
    except Exception:
        skills = []
    _registry_cache = skills
    return skills


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)


def user_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(_content_text(m.get("content")) for m in messages if str(m.get("role", "")).lower() == "user")


def match_skill(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not _env_bool("SKILLS_ENABLED", True):
        return None
    text = user_text(messages)
    if not text.strip():
        return None
    lowered = text.lower()
    skills = load_registry()
    for skill in skills:
        name = str(skill["name"]).lower()
        if f"/skill:{name}" in lowered:
            return skill
    for skill in skills:
        for trigger in skill.get("triggers", []):
            if str(trigger).lower() in lowered:
                return skill
    return None


def _run_script(skill: dict[str, Any], prompt: str) -> str:
    script = str(skill["_script_path"])
    args = [str(a) for a in skill.get("args", [])]
    timeout = max(1, _env_int("SKILL_TIMEOUT_SECONDS", int(skill.get("timeout", 60))))
    try:
        result = subprocess.run(
            [sys.executable, script, *args],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return f"[skill {skill['name']} timed out after {timeout}s]"
    except OSError as exc:
        return f"[skill {skill['name']} failed to start: {exc}]"
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        error = (result.stderr or "").strip()[-2000:]
        return (output + "\n" if output else "") + f"[skill {skill['name']} exited {result.returncode}]\n{error}".strip()
    return output or f"[skill {skill['name']} produced no output]"


def skill_response(skill_name: str, content: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-skill-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"local-skill/{skill_name}",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def try_handle(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    skill = match_skill(messages)
    if skill is None:
        return None
    name = str(skill["name"])
    content = _run_script(skill, user_text(messages))
    return name, skill_response(name, content)


__all__ = ["load_registry", "match_skill", "skill_response", "try_handle", "user_text"]
