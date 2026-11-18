from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Classification:
    route: str
    score: int
    estimated_tokens: int
    reasons: tuple[str, ...] = field(default_factory=tuple)


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


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"text", "input_text", "output_text"}:
                    parts.append(str(item.get("text", "")))
                elif item_type in {"image_url", "input_image"}:
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def estimate_tokens(messages: Iterable[dict[str, Any]], extra_payload: dict[str, Any] | None = None) -> int:
    total_chars = 0
    for message in messages:
        total_chars += len(_content_to_text(message.get("content")))
        for key in ("name", "tool_call_id"):
            value = message.get(key)
            if value:
                total_chars += len(str(value))
        total_chars += 16

    if extra_payload:
        total_chars += len(json.dumps(extra_payload, ensure_ascii=False, default=str, sort_keys=True))

    return max(1, math.ceil(total_chars / CHARS_PER_TOKEN))


def _keyword_score(text: str) -> tuple[int, list[str]]:
    lowered = text.lower()
    score = 0
    reasons: list[str] = []

    strong_terms = {
        "architecture": 18,
        "architect": 18,
        "distributed": 18,
        "microservices": 18,
        "concurrency": 16,
        "race condition": 20,
        "deadlock": 20,
        "security": 16,
        "vulnerability": 22,
        "exploit": 18,
        "authentication": 14,
        "authorization": 14,
        "cryptograph": 18,
        "migration": 14,
        "database schema": 14,
        "production incident": 22,
        "root cause": 20,
        "debug": 10,
        "refactor": 12,
        "optimize": 10,
        "performance": 10,
        "benchmark": 12,
        "algorithm": 14,
        "complexity": 10,
        "code review": 10,
        "design": 8,
        "implement": 4,
        "fix": 4,
    }
    medium_terms = {
        "api": 5,
        "endpoint": 5,
        "async": 6,
        "await": 4,
        "docker": 6,
        "kubernetes": 8,
        "terraform": 8,
        "ci/cd": 7,
        "testing": 5,
        "unit test": 6,
        "integration test": 7,
        "typescript": 3,
        "python": 3,
        "rust": 4,
        "golang": 4,
        "react": 3,
        "sql": 4,
        "redis": 5,
        "postgres": 5,
        "mysql": 5,
        "nginx": 5,
        "linux": 3,
        "windows": 3,
        "powershell": 3,
        "bash": 3,
        "git": 2,
    }

    for term, weight in strong_terms.items():
        if term in lowered:
            score += weight
            reasons.append(f"strong keyword: {term}")

    for term, weight in medium_terms.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered):
            score += weight
            reasons.append(f"technical keyword: {term}")

    if re.search(r"```|\bdef\b|\bclass\b|\bfunction\b|\bimport\b|\bselect\b", lowered):
        score += 5
        reasons.append("code-like content")

    return min(score, 100), reasons[:12]


FLAGSHIP_TERMS = (
    "architecture",
    "architect",
    "microservices",
    "distributed system",
    "race condition",
    "deadlock",
    "security",
    "vulnerability",
    "exploit",
    "cryptograph",
    "authentication",
    "authorization",
    "production incident",
    "root cause",
    "complex refactor",
)


def message_chars(messages: Iterable[dict[str, Any]]) -> int:
    return sum(len(_content_to_text(message.get("content"))) for message in messages)


def messages_text(messages: Iterable[dict[str, Any]]) -> str:
    return "\n".join(_content_to_text(message.get("content")) for message in messages)


def classify_and_route(messages: list[dict[str, Any]], extra_payload: dict[str, Any] | None = None) -> Classification:
    text = messages_text(messages)
    total_chars = message_chars(messages)
    estimated_tokens = estimate_tokens(messages, extra_payload)
    keyword_score, keyword_reasons = _keyword_score(text)

    score = keyword_score
    reasons = list(keyword_reasons)

    local_max_chars = _env_int("ROUTER_LOCAL_MAX_CHARS", 4000)
    economy_max_chars = _env_int("ROUTER_ECONOMY_MAX_CHARS", 15000)

    if total_chars > economy_max_chars:
        score += 35
        reasons.append("context exceeds economy character budget")
    elif total_chars > local_max_chars:
        score += 15
        reasons.append("context exceeds local character budget")

    if len(messages) >= 8:
        score += 10
        reasons.append("long conversation history")
    elif len(messages) >= 4:
        score += 5
        reasons.append("multi-turn conversation")

    if any(str(message.get("role", "")).lower() in {"system", "developer"} for message in messages):
        score += 5
        reasons.append("system/developer instructions present")

    score = min(score, 100)

    local_max_score = _env_int("ROUTER_LOCAL_MAX_SCORE", 25)
    economy_max_score = _env_int("ROUTER_ECONOMY_MAX_SCORE", 65)
    allow_flagship = _env_bool("ROUTER_ALLOW_FLAGSHIP", True)
    lowered = text.lower()

    flagship_hit = next((term for term in FLAGSHIP_TERMS if term in lowered), None)
    if flagship_hit:
        reasons.append(f"flagship keyword: {flagship_hit}")

    if flagship_hit or total_chars > economy_max_chars or score > economy_max_score:
        route = "flagship" if allow_flagship else "economy"
        if route == "economy":
            reasons.append("flagship routing disabled; capped at economy")
    elif total_chars > local_max_chars or score > local_max_score:
        route = "economy"
    else:
        route = "local"

    return Classification(route=route, score=score, estimated_tokens=estimated_tokens, reasons=tuple(reasons))


def use_free_pool(route: str) -> bool:
    return route in {"local", "economy"} and _env_bool("FREE_POOL_ENABLED", True)


def classify(messages: list[dict[str, Any]], extra_payload: dict[str, Any] | None = None) -> Classification:
    return classify_and_route(messages, extra_payload)


def route_from_header(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    aliases = {
        "local": "local",
        "ollama": "local",
        "cheap": "economy",
        "economy": "economy",
        "standard": "economy",
        "flagship": "flagship",
        "premium": "flagship",
        "best": "flagship",
    }
    return aliases.get(normalized)


def estimate_cost_usd(model: str, usage: dict[str, Any] | None) -> float:
    if not usage:
        return 0.0
    costs = _load_costs()
    normalized = model.lower()
    match = costs.get(normalized)
    if match is None:
        for key, value in costs.items():
            if key in normalized or normalized in key:
                match = value
                break
    if match is None:
        return 0.0
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return (prompt_tokens * float(match.get("input", 0.0)) + completion_tokens * float(match.get("output", 0.0))) / 1_000_000


def estimate_savings_usd(model: str, tokens: int) -> float:
    if tokens <= 0:
        return 0.0
    costs = _load_costs()
    normalized = model.lower()
    match = costs.get(normalized)
    if match is None:
        for key, value in costs.items():
            if key in normalized or normalized in key:
                match = value
                break
    if match is None:
        return 0.0
    blended = (float(match.get("input", 0.0)) + float(match.get("output", 0.0))) / 2
    return tokens * blended / 1_000_000


def _load_costs() -> dict[str, dict[str, float]]:
    defaults = {
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-5": {"input": 1.25, "output": 10.00},
        "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
        "claude-haiku": {"input": 0.80, "output": 4.00},
    }
    raw = os.getenv("MODEL_COSTS_JSON", "").strip()
    if not raw:
        return defaults
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return {str(key).lower(): {"input": float(value.get("input", 0.0)), "output": float(value.get("output", 0.0))} for key, value in loaded.items() if isinstance(value, dict)}
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return defaults


def extract_text_from_response(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


__all__ = [
    "Classification",
    "classify",
    "classify_and_route",
    "estimate_cost_usd",
    "estimate_savings_usd",
    "estimate_tokens",
    "extract_text_from_response",
    "message_chars",
    "messages_text",
    "route_from_header",
    "use_free_pool",
]
