from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CACHE_BACKEND", "memory")
os.environ["SKILLS_ENABLED"] = "false"
for _key in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "AXFORGE_API_KEY"):
    os.environ.pop(_key, None)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import router.main as main  # noqa: E402
from router.handoff import HandoffManager  # noqa: E402


def _fake_completion(text: str = "ok") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def test_free_pool_failover() -> None:
    original = main.request_json
    calls: list[str] = []

    async def fake_request_json(client, method, url, **kwargs):
        calls.append(url)
        if "ovh.net" in url:
            raise main.ProviderError("rate limit exceeded", 429)
        return _fake_completion("pool-ok")

    main.request_json = fake_request_json
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CACHE_FILE"] = str(Path(tmp) / "cache.json")
            response, entry, failovers = asyncio.run(
                main.free_pool_chat(httpx.AsyncClient(), {"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
            )
    finally:
        main.request_json = original

    assert response["choices"][0]["message"]["content"] == "pool-ok"
    assert entry["name"] == "kilo", f"expected failover to kilo, got {entry['name']}"
    assert failovers == 1, f"expected 1 failover, got {failovers}"
    assert any("ovh.net" in url for url in calls) and any("kilo.ai" in url for url in calls)
    print("PASS test_free_pool_failover")


def test_handoff_injection() -> None:
    original_provider_chat = main.provider_chat
    captured: list[dict] = []

    async def fake_provider_chat(client, provider, model, payload):
        captured.append({"provider": provider, "model": model, "messages": payload.get("messages")})
        return _fake_completion("done")

    main.provider_chat = fake_provider_chat
    os.environ["FREE_POOL_ENABLED"] = "false"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            os.environ["CACHE_FILE"] = str(tmp_path / "cache.json")
            with TestClient(main.app) as client:
                main.app.state.handoff = HandoffManager(tmp_path / "HANDOVER.md")
                main.app.state.last_model_key = None
                main.app.state.last_model_label = ""
                main.app.state.last_model_provider = ""

                r1 = client.post("/v1/chat/completions", json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Say hello in one word."}],
                })
                assert r1.status_code == 200, r1.text
                assert (tmp_path / "HANDOVER.md").exists(), "HANDOVER.md not created after first request"

                r2 = client.post(
                    "/v1/chat/completions",
                    json={"model": "auto", "messages": [{"role": "user", "content": "Now do something else entirely different."}]},
                    headers={"X-Devin-Route": "flagship"},
                )
                assert r2.status_code == 200, r2.text

                assert len(captured) == 2, f"expected 2 provider calls, got {len(captured)}"
                second_messages = captured[1]["messages"]
                assert second_messages and second_messages[0]["role"] == "system"
                assert "HANDOVER" in str(second_messages[0]["content"])

                handover_text = (tmp_path / "HANDOVER.md").read_text(encoding="utf-8")
                assert "## Last Model Used" in handover_text
                assert "gpt-4o" in handover_text
                assert "## Recently Modified Files" in handover_text

                stats = client.get("/stats").json()
                for key in ("free_pool_requests", "failover_events", "free_pool_savings_usd"):
                    assert key in stats, f"missing stats key {key}"
    finally:
        main.provider_chat = original_provider_chat
        os.environ.pop("FREE_POOL_ENABLED", None)
    print("PASS test_handoff_injection")


def test_handoff_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        os.environ["CACHE_FILE"] = str(tmp_path / "cache.json")
        os.environ["FREE_POOL_ENABLED"] = "false"
        with TestClient(main.app) as client:
            main.app.state.handoff = HandoffManager(tmp_path / "HANDOVER.md")
            response = client.post("/v1/chat/completions", json={
                "model": "auto",
                "messages": [{"role": "user", "content": "/handoff"}],
            })
    assert response.status_code == 200, response.text
    body = response.json()
    content = body["choices"][0]["message"]["content"]
    assert "## Last Model Used" in content
    assert "## Current Task Status" in content
    assert "## Recently Modified Files" in content
    assert "## Architectural Decisions" in content
    assert body["usage"]["total_tokens"] == 0
    assert body["usage"]["prompt_tokens"] == 0
    assert body["usage"]["completion_tokens"] == 0
    print("PASS test_handoff_command")


def main_runner() -> int:
    failures = 0
    for test in (test_free_pool_failover, test_handoff_injection, test_handoff_command):
        try:
            test()
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main_runner())
