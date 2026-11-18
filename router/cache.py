from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator

import redis.asyncio as redis

SEMANTIC_PREFIX = "sem:"
VECTOR_DIM = 2048


def _text_vector(text: str) -> dict[int, float]:
    vec: dict[int, float] = {}
    for token in re.findall(r"[a-z0-9_]{3,}", text.lower()):
        index = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:8], 16) % VECTOR_DIM
        vec[index] = vec.get(index, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm:
        return {k: v / norm for k, v in vec.items()}
    return vec


def _cosine(a: dict[int, float], b: dict[int, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(weight * b.get(index, 0.0) for index, weight in a.items())
    return dot


class ResponseCache:
    def __init__(
        self,
        backend: str | None = None,
        redis_url: str | None = None,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
        file_path: str | Path | None = None,
    ) -> None:
        self.backend = (backend or os.getenv("CACHE_BACKEND", "auto")).lower()
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.ttl_seconds = ttl_seconds if ttl_seconds is not None else int(os.getenv("CACHE_TTL_SECONDS", "86400"))
        self.max_entries = max_entries if max_entries is not None else int(os.getenv("CACHE_MAX_ENTRIES", "10000"))
        self.file_path = Path(file_path or os.getenv("CACHE_FILE", ".cache/responses.json"))
        self.semantic_enabled = os.getenv("SEMANTIC_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        try:
            self.semantic_threshold = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.92"))
        except (TypeError, ValueError):
            self.semantic_threshold = 0.92
        try:
            self.semantic_min_chars = int(os.getenv("SEMANTIC_CACHE_MIN_CHARS", "30"))
        except (TypeError, ValueError):
            self.semantic_min_chars = 30
        self.semantic_max_entries = int(os.getenv("SEMANTIC_CACHE_MAX_ENTRIES", "500"))
        self._redis: redis.Redis | None = None
        self._redis_disabled = False
        self._local: dict[str, dict[str, Any]] = {}
        self._local_loaded = False
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self.sets = 0
        self.local_evictions = 0

    async def initialize(self) -> None:
        if self.backend in {"redis", "auto"} and self.redis_url:
            try:
                client = redis.Redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                await asyncio.wait_for(client.ping(), timeout=2)
                self._redis = client
            except Exception:
                self._redis_disabled = True
                self.errors += 1
                self._redis = None
        self._load_local()

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None

    async def get(self, key: str) -> Any | None:
        if self._redis is not None and not self._redis_disabled:
            try:
                raw = await self._redis.get(key)
                if raw is not None:
                    self.hits += 1
                    return json.loads(raw)
            except Exception:
                self.errors += 1
                self._redis_disabled = True
                self._redis = None

        value = self._local_get(key)
        if value is not None:
            self.hits += 1
        else:
            self.misses += 1
        return value

    async def set(self, key: str, value: Any) -> bool:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        stored = False

        if self._redis is not None and not self._redis_disabled:
            try:
                await self._redis.setex(key, self.ttl_seconds, serialized)
                stored = True
            except Exception:
                self.errors += 1
                self._redis_disabled = True
                self._redis = None

        if not stored:
            self._local_set(key, value)
            self._save_local()

        self.sets += 1
        return True

    async def _semantic_items(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if self._redis is not None and not self._redis_disabled:
            try:
                keys = await self._redis.keys(f"{SEMANTIC_PREFIX}*")
                if keys:
                    raws = await self._redis.mget(keys)
                    for key, raw in zip(keys, raws):
                        if raw:
                            try:
                                yield str(key), json.loads(raw)
                            except Exception:
                                continue
                return
            except Exception:
                self.errors += 1
                self._redis_disabled = True
                self._redis = None
        self._load_local()
        now = time.time()
        for key, item in self._local.items():
            if key.startswith(SEMANTIC_PREFIX) and float(item.get("expires_at", 0)) > now:
                value = item.get("value")
                if isinstance(value, dict):
                    yield key, value

    async def get_semantic(self, text: str) -> Any | None:
        if not self.semantic_enabled or len(text.strip()) < self.semantic_min_chars:
            return None
        vector = _text_vector(text)
        if not vector:
            return None
        best_score = 0.0
        best_response = None
        async for _key, value in self._semantic_items():
            stored = value.get("vector")
            if not isinstance(stored, dict):
                continue
            stored_vec = {int(k): float(v) for k, v in stored.items()}
            score = _cosine(vector, stored_vec)
            if score > best_score:
                best_score = score
                best_response = value.get("response")
        if best_response is not None and best_score >= self.semantic_threshold:
            self.hits += 1
            return best_response
        self.misses += 1
        return None

    async def set_semantic(self, text: str, response: Any) -> None:
        if not self.semantic_enabled or len(text.strip()) < self.semantic_min_chars:
            return
        vector = _text_vector(text)
        if not vector:
            return
        key = SEMANTIC_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()
        payload = {"vector": {str(k): v for k, v in vector.items()}, "response": response}
        stored = False
        if self._redis is not None and not self._redis_disabled:
            try:
                await self._redis.setex(key, self.ttl_seconds, json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
                stored = True
            except Exception:
                self.errors += 1
                self._redis_disabled = True
                self._redis = None
        if not stored:
            self._local_set(key, payload)
            self._evict_semantic_local()
            self._save_local()
        self.sets += 1

    def _evict_semantic_local(self) -> None:
        sem_keys = [k for k in self._local if k.startswith(SEMANTIC_PREFIX)]
        while len(sem_keys) > self.semantic_max_entries:
            oldest = min(sem_keys, key=lambda k: float(self._local[k].get("expires_at", 0)))
            self._local.pop(oldest, None)
            sem_keys.remove(oldest)
            self.local_evictions += 1

    async def clear(self) -> None:
        if self._redis is not None and not self._redis_disabled:
            try:
                await self._redis.flushdb()
            except Exception:
                self.errors += 1
                self._redis_disabled = True
                self._redis = None
        self._local.clear()
        self._save_local()

    def stats(self) -> dict[str, Any]:
        return {
            "backend": self.active_backend,
            "ttl_seconds": self.ttl_seconds,
            "max_entries": self.max_entries,
            "local_entries": len(self._local),
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "errors": self.errors,
            "local_evictions": self.local_evictions,
        }

    @property
    def active_backend(self) -> str:
        if self._redis is not None and not self._redis_disabled:
            return "redis"
        if self.backend == "redis":
            return "unavailable"
        return "memory-json"

    def _load_local(self) -> None:
        if self._local_loaded:
            return
        self._local_loaded = True
        if not self.file_path.exists():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            now = time.time()
            for key, item in raw.items():
                if isinstance(item, dict) and float(item.get("expires_at", 0)) > now:
                    self._local[str(key)] = item
        except Exception:
            self.errors += 1
            self._local = {}
        self._evict_local_if_needed(save=False)

    def _local_get(self, key: str) -> Any | None:
        self._load_local()
        item = self._local.get(key)
        if item is None:
            return None
        if float(item.get("expires_at", 0)) <= time.time():
            self._local.pop(key, None)
            self._save_local()
            return None
        return item.get("value")

    def _local_set(self, key: str, value: Any) -> None:
        self._load_local()
        self._local[key] = {
            "expires_at": time.time() + self.ttl_seconds,
            "value": value,
        }
        self._evict_local_if_needed()

    def _evict_local_if_needed(self, save: bool = True) -> None:
        while len(self._local) > self.max_entries:
            oldest = min(self._local, key=lambda item: float(self._local[item].get("expires_at", 0)))
            self._local.pop(oldest, None)
            self.local_evictions += 1
        if save:
            self._save_local()

    def _save_local(self) -> None:
        if not self._local:
            try:
                self.file_path.unlink(missing_ok=True)
            except Exception:
                self.errors += 1
            return
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            temp_path.write_text(json.dumps(self._local, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
            temp_path.replace(self.file_path)
        except Exception:
            self.errors += 1


def cache_key(payload: dict[str, Any]) -> str:
    import hashlib

    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "router:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = ["ResponseCache", "cache_key"]
