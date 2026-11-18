from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from router import skills_manager
from router.cache import ResponseCache, cache_key
from router.classifier import classify_and_route, estimate_cost_usd, estimate_savings_usd, messages_text, route_from_header, use_free_pool
from router.handoff import HandoffManager, handover_messages

load_dotenv()


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(default="auto")
    messages: list[Message] = Field(min_length=1)
    stream: bool = False


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def endpoint_url(base_url: str, *parts: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return "/".join([base, *parts])
    return "/".join([base, "v1", *parts])


def infer_provider(model: str) -> str:
    lowered = model.lower()
    if lowered.startswith(("claude", "anthropic/")):
        return "anthropic"
    if lowered.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return ""


def normalize_provider(provider: str, model: str = "") -> str:
    value = provider.strip().lower()
    aliases = {"local": "ollama", "ollama": "ollama", "openai": "openai", "anthropic": "anthropic", "claude": "anthropic", "auto": ""}
    resolved = aliases.get(value, value)
    if resolved == "ollama" or model.lower().startswith(("ollama/", "local/")):
        return "ollama"
    inferred = infer_provider(model)
    if resolved and inferred and resolved != inferred:
        return inferred
    if resolved in {"ollama", "openai", "anthropic"}:
        return resolved
    return inferred or "openai"


def clean_model_name(model: str) -> str:
    for prefix in ("ollama/", "local/", "openai/", "anthropic/", "claude/"):
        if model.lower().startswith(prefix):
            return model[len(prefix):]
    return model


def route_config(route: str) -> tuple[str, str]:
    if route == "local":
        return "ollama", clean_model_name(setting("ROUTER_LOCAL_MODEL", "qwen2.5-coder:7b"))
    if route == "economy":
        model = clean_model_name(setting("ROUTER_ECONOMY_MODEL", "gpt-4o-mini"))
        provider = normalize_provider(setting("ROUTER_ECONOMY_PROVIDER", "auto"), model)
        return provider, model
    model = clean_model_name(setting("ROUTER_FLAGSHIP_MODEL", "gpt-4o"))
    provider = normalize_provider(setting("ROUTER_FLAGSHIP_PROVIDER", "auto"), model)
    return provider, model


FREE_POOL_PATH = Path(__file__).resolve().parent / "free_pool.yaml"
POOL_PROVIDER_PREFIXES = {"openai", "anthropic", "groq", "gemini", "openrouter", "ollama", "local", "azure", "bedrock"}
POOL_DEFAULT_BASES = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openrouter": "https://openrouter.ai/api/v1",
}


def load_free_pool() -> list[dict[str, Any]]:
    if yaml is None or not FREE_POOL_PATH.exists():
        return []
    try:
        data = yaml.safe_load(FREE_POOL_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = data.get("providers") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    pool: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = str(entry.get("model") or "").strip()
        if not model:
            continue
        name = str(entry.get("name") or model).strip() or model
        wire_model = model
        head, sep, tail = model.partition("/")
        if sep and tail and (head in POOL_PROVIDER_PREFIXES or head == name):
            wire_model = tail
        env_key = str(entry.get("env_key") or "").strip()
        api_key = setting(env_key) if env_key else ""
        if bool(entry.get("requires_key")) and not api_key:
            continue
        api_base = str(entry.get("api_base") or "").strip() or POOL_DEFAULT_BASES.get(name, "")
        if not api_base:
            continue
        pool.append({"name": name, "model": model, "wire_model": wire_model, "api_base": api_base, "api_key": api_key})
    return pool


async def free_pool_chat(client: httpx.AsyncClient, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], int]:
    entries = load_free_pool()
    if not entries:
        raise ProviderError("Free pool is empty or PyYAML is not installed", 503)
    last_error: ProviderError | None = None
    failovers = 0
    for entry in entries:
        body = dict(payload)
        body.pop("stream_options", None)
        body["model"] = entry["wire_model"]
        body["stream"] = False
        headers = {"Content-Type": "application/json"}
        if entry["api_key"]:
            headers["Authorization"] = f"Bearer {entry['api_key']}"
        url = entry["api_base"].rstrip("/") + "/chat/completions"
        try:
            data = await request_json(client, "POST", url, json=body, headers=headers)
            return data, entry, failovers
        except ProviderError as exc:
            last_error = exc
            failovers += 1
        except Exception as exc:
            last_error = ProviderError(f"Free pool provider {entry['name']} failed: {exc}", 502)
            failovers += 1
    error = ProviderError(f"Free pool exhausted: {last_error.message if last_error else 'no providers'}", last_error.status_code if last_error else 503)
    error.failovers = failovers
    raise error


def explicit_route(model: str) -> tuple[str, str]:
    lowered = model.lower()
    if lowered.startswith(("ollama/", "local/")):
        return "ollama", clean_model_name(model)
    if lowered.startswith("openai/"):
        return "openai", clean_model_name(model)
    if lowered.startswith(("anthropic/", "claude/")):
        return "anthropic", clean_model_name(model)
    provider = infer_provider(model)
    if provider:
        return provider, clean_model_name(model)
    if ":" in model:
        return "ollama", clean_model_name(model)
    return "openai", clean_model_name(model)


def resolve_route(request: ChatCompletionRequest, forced_route: str | None = None) -> tuple[str, str, str, int, tuple[str, ...]]:
    payload = provider_payload(request)
    messages = [message.model_dump(exclude_none=True) for message in request.messages]
    classification = classify_and_route(messages, payload)
    route = forced_route or classification.route
    if route not in {"local", "economy", "flagship"}:
        route = classification.route

    explicit = explicit_route(request.model)
    if request.model.strip().lower() not in {"", "auto", "router", "devin-auto"} and explicit:
        provider, model = explicit
        route = "explicit"
    else:
        provider, model = route_config(route)

    return route, provider, model, classification.score, classification.reasons


def provider_payload(request: ChatCompletionRequest) -> dict[str, Any]:
    data = request.model_dump(exclude={"model", "stream"}, exclude_none=True)
    extra = getattr(request, "model_extra", None) or {}
    for key, value in extra.items():
        if key not in {"model", "stream"} and value is not None:
            data[key] = value
    data["stream"] = False
    return data


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cache = ResponseCache()
    await app.state.cache.initialize()
    app.state.http = httpx.AsyncClient(timeout=env_float("PROVIDER_TIMEOUT_SECONDS", 120.0))
    app.state.started_at = time.time()
    app.state.handoff = HandoffManager()
    app.state.last_model_key: tuple[str, ...] | None = None
    app.state.last_model_label = ""
    app.state.last_model_provider = ""
    app.state.stats = {
        "requests": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "provider_calls": 0,
        "errors": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "estimated_cost_usd": 0.0,
        "latency_ms_total": 0.0,
        "routes": {},
        "providers": {},
        "models": {},
        "cache_hits_exact": 0,
        "cache_hits_semantic": 0,
        "skills_executed": 0,
        "free_pool_requests": 0,
        "failover_events": 0,
        "free_pool_savings_usd": 0.0,
    }
    app.state.stats_lock = asyncio.Lock()
    yield
    await app.state.http.aclose()
    await app.state.cache.close()


app = FastAPI(title="Devin Token Optimizer Router", version="0.1.0", lifespan=lifespan)


def bump(mapping: dict[str, int], key: str, amount: int = 1) -> None:
    mapping[key] = mapping.get(key, 0) + amount


async def record_request(state: Any, *, route: str, provider: str, model: str, usage: dict[str, Any] | None, latency_ms: float, cache_hit: bool, cache_kind: str | None = None, error: bool = False) -> None:
    async with state.stats_lock:
        stats = state.stats
        stats["requests"] += 1
        stats["cache_hits" if cache_hit else "cache_misses"] += 1
        if cache_hit:
            stats[f"cache_hits_{cache_kind or 'exact'}"] = stats.get(f"cache_hits_{cache_kind or 'exact'}", 0) + 1
        if route == "skill":
            stats["skills_executed"] = stats.get("skills_executed", 0) + 1
        stats["errors"] += int(error)
        stats["latency_ms_total"] += latency_ms
        bump(stats["routes"], route)
        bump(stats["providers"], provider)
        bump(stats["models"], model)
        if usage:
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total = prompt + completion
            if cache_hit:
                stats["cached_tokens"] += total
            else:
                stats["prompt_tokens"] += prompt
                stats["completion_tokens"] += completion
                stats["total_tokens"] += total
                stats["estimated_cost_usd"] += estimate_cost_usd(model, usage)
        if not cache_hit:
            stats["provider_calls"] += 1


def usage_from_response(response: dict[str, Any]) -> dict[str, Any] | None:
    usage = response.get("usage")
    return usage if isinstance(usage, dict) else None


def error_response(message: str, status_code: int = 500) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "router_error", "code": status_code}}, status_code=status_code)


def route_headers(route: str, provider: str, model: str, score: int, reasons: tuple[str, ...], cache_status: str) -> dict[str, str]:
    return {
        "X-Devin-Route": route,
        "X-Devin-Provider": provider,
        "X-Devin-Model": model,
        "X-Devin-Score": str(score),
        "X-Devin-Reasons": ",".join(reasons[:5]),
        "X-Cache": cache_status,
    }


async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    headers = kwargs.pop("headers", {}) or {}
    retries = max(0, env_int("PROVIDER_MAX_RETRIES", 2))
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise ProviderError(f"Provider timeout: {exc}", 504) from exc
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            raise ProviderError(f"Provider request failed: {exc}", 502) from exc

        if response.status_code in {408, 409, 425, 429, 500, 502, 503, 504} and attempt < retries:
            await asyncio.sleep(0.5 * (2**attempt))
            continue
        if response.status_code >= 400:
            detail = response.text[:1000]
            try:
                parsed = response.json()
                detail = parsed.get("error", {}).get("message") or parsed.get("message") or detail
            except Exception:
                pass
            raise ProviderError(str(detail), response.status_code if 400 <= response.status_code < 600 else 502)
        try:
            return response.json()
        except Exception as exc:
            raise ProviderError("Provider returned invalid JSON", 502) from exc
    raise ProviderError(f"Provider request failed: {last_error}", 502)


async def openai_compatible_chat(client: httpx.AsyncClient, provider: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
    if provider == "ollama":
        base = setting("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        api_key = setting("OLLAMA_API_KEY")
    else:
        base = setting("OPENAI_BASE_URL", "https://api.openai.com/v1")
        api_key = setting("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured", 503)
    url = endpoint_url(base, "chat", "completions")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = dict(payload)
    body.pop("stream_options", None)
    body["model"] = model
    body["stream"] = False
    return await request_json(client, "POST", url, json=body, headers=headers)


def text_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        elif isinstance(item, dict):
            item_type = item.get("type")
            if item_type in {"text", "input_text", "output_text"}:
                blocks.append({"type": "text", "text": str(item.get("text", ""))})
            elif item_type in {"image_url", "input_image"}:
                image = item.get("image_url") or item
                url = image.get("url") if isinstance(image, dict) else image
                if isinstance(url, str) and url.startswith("data:"):
                    header, _, data = url.partition(",")
                    media_type = header[5:].split(";")[0] or "image/png"
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
                elif isinstance(url, str):
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return [block for block in blocks if block.get("text") != "" or block.get("type") == "image"]


def anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1].get("role") == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = str(message.get("role", "user")).lower()
        content = message.get("content")
        if role in {"system", "developer"}:
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or message.get("id") or ""),
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str),
            })
            continue

        target_role = "assistant" if role == "assistant" else "user"
        blocks = pending_tool_results if target_role == "user" else []
        pending_tool_results = [] if target_role == "user" else pending_tool_results
        blocks.extend(text_blocks(content))
        if target_role == "assistant":
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else {}
                if isinstance(function, dict):
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except Exception:
                        arguments = {"raw": function.get("arguments")}
                    blocks.append({"type": "tool_use", "id": str(call.get("id") or uuid.uuid4()), "name": str(function.get("name") or "tool"), "input": arguments})
        if blocks or content is None:
            append(target_role, blocks or [{"type": "text", "text": ""}])

    if pending_tool_results:
        append("user", pending_tool_results)
    if converted and converted[0].get("role") != "user":
        converted.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    if not converted:
        converted.append({"role": "user", "content": [{"type": "text", "text": ""}]})
    return "\n\n".join(system_parts), converted


async def anthropic_chat(client: httpx.AsyncClient, model: str, payload: dict[str, Any]) -> dict[str, Any]:
    api_key = setting("ANTHROPIC_API_KEY")
    if not api_key:
        raise ProviderError("ANTHROPIC_API_KEY is not configured", 503)
    base = setting("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    url = endpoint_url(base, "messages")
    system, messages = anthropic_messages(payload.get("messages") or [])
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": int(payload.get("max_tokens") or payload.get("max_completion_tokens") or env_int("ANTHROPIC_DEFAULT_MAX_TOKENS", 4096)),
        "stream": False,
    }
    if system:
        body["system"] = system
    for source, target in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k")):
        if payload.get(source) is not None:
            body[target] = payload[source]
    stop = payload.get("stop") or payload.get("stop_sequences")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if payload.get("user"):
        body["metadata"] = {"user_id": str(payload["user"])}

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": setting("ANTHROPIC_VERSION", "2023-06-01"),
    }
    data = await request_json(client, "POST", url, json=body, headers=headers)
    return anthropic_to_openai(data, model)


def anthropic_to_openai(data: dict[str, Any], model: str) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or uuid.uuid4()),
                "type": "function",
                "function": {"name": str(block.get("name") or "tool"), "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)},
            })
    finish = data.get("stop_reason")
    finish_reason = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(str(finish), "stop")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = data.get("usage") or {}
    return {
        "id": str(data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(data.get("model") or model),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        },
    }


async def provider_chat(client: httpx.AsyncClient, provider: str, model: str, payload: dict[str, Any]) -> dict[str, Any]:
    if provider == "anthropic":
        return await anthropic_chat(client, model, payload)
    return await openai_compatible_chat(client, provider, model, payload)


def stream_chunks(response: dict[str, Any], model: str, include_usage: bool) -> list[str]:
    response_id = str(response.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}")
    created = int(response.get("created") or time.time())
    chunks: list[str] = []
    choices = response.get("choices") or [{}]
    for index, choice in enumerate(choices):
        delta: dict[str, Any] = {"role": "assistant", "content": ""}
        chunks.append({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": index, "delta": delta, "finish_reason": None}]})
        message = choice.get("message") or {}
        content = message.get("content")
        if content:
            chunks.append({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": index, "delta": {"content": content}, "finish_reason": None}]})
        for call in message.get("tool_calls") or []:
            chunks.append({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": index, "delta": {"tool_calls": [call]}, "finish_reason": None}]})
        chunks.append({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": index, "delta": {}, "finish_reason": choice.get("finish_reason") or "stop"}]})
    if include_usage and response.get("usage"):
        chunks.append({"id": response_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [], "usage": response.get("usage")})
    return [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks] + ["data: [DONE]\n\n"]


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role", "")).lower() == "user":
            return messages_text([message]).strip()
    return ""


def handoff_manager(state: Any) -> HandoffManager:
    manager = getattr(state, "handoff", None)
    if not isinstance(manager, HandoffManager):
        manager = HandoffManager()
        state.handoff = manager
    return manager


def prepare_handoff_context(state: Any, *, provider: str, model: str, pooled: bool, entry_name: str, payload: dict[str, Any], user_text: str) -> None:
    if not env_bool("HANDOVER_ENABLED", True):
        return
    key = ("free-pool",) if pooled else (provider, model)
    if getattr(state, "last_model_key", None) == key:
        return
    manager = handoff_manager(state)
    label = f"free-pool/{entry_name}" if pooled and entry_name else ("free-pool" if pooled else model)
    provider_label = "free-pool" if pooled else provider
    previous = getattr(state, "last_model_label", "") or ""
    notes = f"Took over from: {previous}" if previous else ""
    content = manager.update(model=label, provider=provider_label, summary=user_text, notes=notes)
    state.last_model_key = key
    state.last_model_label = label
    state.last_model_provider = provider_label
    messages = payload.get("messages")
    if isinstance(messages, list) and content.strip():
        messages[:0] = handover_messages(content)


def sync_handoff_model(state: Any, *, provider: str, model: str, pooled: bool, entry_name: str, user_text: str) -> None:
    if not env_bool("HANDOVER_ENABLED", True):
        return
    key = ("free-pool",) if pooled else (provider, model)
    label = f"free-pool/{entry_name}" if pooled and entry_name else ("free-pool" if pooled else model)
    if getattr(state, "last_model_key", None) == key and getattr(state, "last_model_label", "") == label:
        return
    manager = handoff_manager(state)
    manager.update(model=label, provider="free-pool" if pooled else provider, summary=user_text)
    state.last_model_key = key
    state.last_model_label = label
    state.last_model_provider = "free-pool" if pooled else provider


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "devin-token-optimizer",
        "cache": app.state.cache.active_backend,
        "uptime_seconds": round(time.time() - app.state.started_at, 3),
    }


@app.get("/stats")
async def stats() -> dict[str, Any]:
    state_stats = dict(app.state.stats)
    state_stats["requests"] = state_stats.get("requests", 0)
    state_stats["average_latency_ms"] = round(state_stats.get("latency_ms_total", 0.0) / max(state_stats.get("requests", 1), 1), 3)
    state_stats["cache"] = app.state.cache.stats()
    state_stats["uptime_seconds"] = round(time.time() - app.state.started_at, 3)
    flagship_model = route_config("flagship")[1]
    state_stats["estimated_savings_usd"] = round(estimate_savings_usd(flagship_model, int(state_stats.get("cached_tokens", 0))), 6)
    state_stats["savings_reference_model"] = flagship_model
    return state_stats


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    x_devin_route: str | None = Header(default=None, alias="X-Devin-Route"),
    x_route_tier: str | None = Header(default=None, alias="X-Route-Tier"),
) -> Any:
    started = time.perf_counter()
    forced_route = route_from_header(x_devin_route) or route_from_header(x_route_tier)

    messages_dump = [message.model_dump(exclude_none=True) for message in request.messages]

    user_text = last_user_text(messages_dump)
    if "/handoff" in user_text:
        manager = handoff_manager(app.state)
        current_label = getattr(app.state, "last_model_label", "") or "n/d"
        current_provider = getattr(app.state, "last_model_provider", "") or "n/d"
        summary = user_text.replace("/handoff", "").strip() or "Explicit handoff request."
        report = manager.update(model=current_label, provider=current_provider, summary=summary)
        body = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "handoff",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": report}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        await record_request(app.state, route="handoff", provider="handoff", model="handoff", usage=None, latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False)
        return JSONResponse(body, headers={"X-Devin-Route": "handoff", "X-Cache": "HANDOFF"})

    handled = skills_manager.try_handle(messages_dump)
    if handled is not None:
        skill_name, skill_body = handled
        await record_request(app.state, route="skill", provider="local-skill", model=skill_name, usage=None, latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False)
        return JSONResponse(skill_body, headers={
            "X-Optimizer-Routed-Model": f"local-skill/{skill_name}",
            "X-Devin-Route": "skill",
            "X-Cache": "SKILL",
        })

    try:
        route, provider, model, score, reasons = resolve_route(request, forced_route)
    except Exception as exc:
        await record_request(app.state, route="unknown", provider="unknown", model=request.model, usage=None, latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False, error=True)
        return error_response(f"Routing failed: {exc}", 400)

    payload = provider_payload(request)
    key = cache_key({"provider": provider, "model": model, "payload": payload}) if not request.stream else None

    prompt_text = messages_text(messages_dump)
    if key:
        cached = await app.state.cache.get(key)
        if cached is not None:
            headers = route_headers(route, provider, model, score, reasons, "HIT")
            await record_request(app.state, route=route, provider=provider, model=model, usage=usage_from_response(cached), latency_ms=(time.perf_counter() - started) * 1000, cache_hit=True, cache_kind="exact")
            return JSONResponse(cached, headers=headers)

    if not request.stream:
        semantic_hit = await app.state.cache.get_semantic(prompt_text)
        if semantic_hit is not None:
            headers = route_headers(route, provider, model, score, reasons, "SEMANTIC")
            await record_request(app.state, route=route, provider=provider, model=model, usage=usage_from_response(semantic_hit), latency_ms=(time.perf_counter() - started) * 1000, cache_hit=True, cache_kind="semantic")
            return JSONResponse(semantic_hit, headers=headers)

    response = None
    pool_entry: dict[str, Any] | None = None
    pool_failovers = 0
    pool_attempted = False

    pool_entries = load_free_pool() if use_free_pool(route) else []
    if pool_entries:
        pool_attempted = True
        prepare_handoff_context(app.state, provider=provider, model=model, pooled=True, entry_name=str(pool_entries[0].get("name") or ""), payload=payload, user_text=user_text)
        try:
            response, pool_entry, pool_failovers = await free_pool_chat(app.state.http, payload)
            provider = str(pool_entry.get("name") or provider)
            model = str(pool_entry.get("model") or model)
            route = "free-pool"
        except ProviderError as pool_error:
            pool_failovers = int(getattr(pool_error, "failovers", 0) or 0)
            response = None

    if response is None:
        if not pool_attempted:
            prepare_handoff_context(app.state, provider=provider, model=model, pooled=False, entry_name="", payload=payload, user_text=user_text)
        try:
            response = await provider_chat(app.state.http, provider, model, payload)
        except ProviderError as first_error:
            if provider == "ollama" and env_bool("ROUTER_LOCAL_FALLBACK", True):
                fallback_provider, fallback_model = route_config("economy")
                fallback_key = None if request.stream else cache_key({"provider": fallback_provider, "model": fallback_model, "payload": payload})
                if fallback_key:
                    cached = await app.state.cache.get(fallback_key)
                    if cached is not None:
                        headers = route_headers("economy", fallback_provider, fallback_model, score, reasons, "HIT")
                        await record_request(app.state, route="economy", provider=fallback_provider, model=fallback_model, usage=usage_from_response(cached), latency_ms=(time.perf_counter() - started) * 1000, cache_hit=True)
                        return JSONResponse(cached, headers=headers)
                try:
                    response = await provider_chat(app.state.http, fallback_provider, fallback_model, payload)
                    provider, model = fallback_provider, fallback_model
                    route = "economy"
                    key = fallback_key
                except Exception:
                    response = None
            if response is None:
                await record_request(app.state, route=route, provider=provider, model=model, usage=None, latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False, error=True)
                return error_response(first_error.message, first_error.status_code)
        except Exception as exc:
            await record_request(app.state, route=route, provider=provider, model=model, usage=None, latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False, error=True)
            return error_response(f"Provider error: {exc}", 502)

    if pool_attempted:
        async with app.state.stats_lock:
            app.state.stats["failover_events"] += pool_failovers
            if pool_entry is not None:
                app.state.stats["free_pool_requests"] += 1
                app.state.stats["free_pool_savings_usd"] += estimate_cost_usd(route_config("flagship")[1], usage_from_response(response))

    sync_handoff_model(
        app.state,
        provider=provider,
        model=model,
        pooled=pool_entry is not None,
        entry_name=str(pool_entry.get("name") or "") if pool_entry else "",
        user_text=user_text,
    )

    headers = route_headers(route, provider, model, score, reasons, "BYPASS" if request.stream else "MISS")
    if request.stream:
        await record_request(app.state, route=route, provider=provider, model=model, usage=usage_from_response(response), latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False)
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        return StreamingResponse(stream_chunks(response, model, include_usage), media_type="text/event-stream", headers={**headers, "Cache-Control": "no-cache"})

    if key:
        await app.state.cache.set(key, response)
        await app.state.cache.set_semantic(prompt_text, response)
    await record_request(app.state, route=route, provider=provider, model=model, usage=usage_from_response(response), latency_ms=(time.perf_counter() - started) * 1000, cache_hit=False)
    return JSONResponse(response, headers=headers)


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "devin-token-optimizer", "endpoint": "/v1/chat/completions", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "router.main:app",
        host=setting("ROUTER_HOST", "0.0.0.0"),
        port=env_int("ROUTER_PORT", 8000),
        reload=env_bool("ROUTER_RELOAD", False),
    )
