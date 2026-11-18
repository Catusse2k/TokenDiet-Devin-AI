# TokenDiet — Devin Token Optimizer

> An OpenAI-compatible API proxy and context toolkit that **cuts token and ACU consumption** on Devin Desktop and Devin CLI, and on any other OpenAI-compatible client (Cursor, Continue, Python scripts, curl, ...).

Instead of sending every request to an expensive model, TokenDiet sits between your client and the LLM providers and decides, request by request, **the cheapest option that can still do the job**. In typical coding-agent sessions the estimated overall savings are **85-90%**.

---

## Table of Contents

1. [What is TokenDiet?](#1-what-is-tokendiet)
2. [How it works](#2-how-it-works)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
   - [4.1 Get the code](#41-get-the-code)
   - [4.2 Quick install (setup scripts)](#42-quick-install-setup-scripts)
   - [4.3 Manual install: Linux / macOS](#43-manual-install-linux--macos)
   - [4.4 Manual install: Windows](#44-manual-install-windows)
   - [4.5 Verify the installation](#45-verify-the-installation)
   - [4.6 Optional components](#46-optional-components)
5. [Configuration (.env)](#5-configuration-env)
6. [Starting the router](#6-starting-the-router)
7. [Tutorial: your first requests](#7-tutorial-your-first-requests)
8. [Routing in depth](#8-routing-in-depth)
9. [Zero-token local skills](#9-zero-token-local-skills)
10. [Free-Tier Cloud Pool](#10-free-tier-cloud-pool)
11. [AI Handoff System](#11-ai-handoff-system)
12. [Caching](#12-caching)
13. [Context packer](#13-context-packer)
14. [Connecting your tools](#14-connecting-your-tools)
15. [API reference](#15-api-reference)
16. [Docker Compose](#16-docker-compose)
17. [Tests and verification](#17-tests-and-verification)
18. [Maintenance: update, stop, uninstall](#18-maintenance-update-stop-uninstall)
19. [Troubleshooting](#19-troubleshooting)
20. [Security](#20-security)
21. [Known limitations](#21-known-limitations)
22. [Project structure](#22-project-structure)

---

## 1. What is TokenDiet?

TokenDiet is a small **FastAPI** server that speaks the OpenAI Chat Completions protocol. You point your tool at it (`http://localhost:8000/v1`) instead of at OpenAI, and it handles the rest.

Each request is dispatched to one of these tiers:

| Tier | Destination | Cost | Typical use |
|---|---|---|---|
| `local` | Local Ollama (`qwen2.5-coder:7b`) | ~$0 | Simple tasks, small contexts |
| `free-pool` | Free LLM APIs (ovhcloud, kilo, groq, gemini, openrouter) | $0 | Low/medium complexity tasks, automatic failover |
| `economy` | `gpt-4o-mini` / Claude Haiku | Low | Medium tasks or fallback |
| `flagship` | `gpt-4o` / Claude Sonnet | High | Architecture, security, large contexts |
| `explicit` | The model chosen by the client | Varies | Deterministic control |

**The four efficiency pillars**

- **Zero-token local skills**: deterministic scripts (`repo_map`, `log_trimmer`, `git_diff_test`) run *before* any LLM call and answer with `prompt_tokens: 0`.
- **Exact + semantic caching**: identical (or very similar) prompts are answered from cache, not from a provider.
- **Smart routing + failover**: automatic tier selection, free-pool rotation on 429/timeout errors, and Ollama -> economy fallback.
- **AI Handoff**: a `HANDOVER.md` file carries context between models/agents without burning tokens.

## 2. How it works

Every request travels through this pipeline:

```text
 Your client (Devin, Cursor, curl, Python...)
          |
          v   POST /v1/chat/completions
 +---------------------------+
 | 1. Skill interceptor      |--- match? --> run local script --> reply (0 tokens)
 +---------------------------+
          | no match
          v
 +---------------------------+
 | 2. /handoff command?      |--- yes ----> write HANDOVER.md --> reply (0 tokens)
 +---------------------------+
          | no
          v
 +---------------------------+
 | 3. Cache lookup           |--- HIT / SEMANTIC --> cached reply
 |    (exact, then semantic) |
 +---------------------------+
          | MISS
          v
 +---------------------------+
 | 4. Classifier             |   size + keywords + complexity score
 |    (or explicit model)    |   -> local | economy | flagship
 +---------------------------+
          |
          v
 +---------------------------+
 | 5. Provider call          |   Ollama / Free pool (failover) / OpenAI / Anthropic
 |    + HANDOVER injection   |   on model change
 +---------------------------+
          |
          v
   Response + diagnostic headers (X-Devin-Route, X-Cache, ...)
```

## 3. Requirements

| Component | Required? | Notes |
|---|---|---|
| **Python 3.10+** | Yes | Check with `python3 --version` (Linux/macOS) or `python --version` (Windows) |
| **pip** | Yes | Ships with Python; used to install `requirements.txt` |
| **Git** | Optional | Needed to clone the repo; also enables "Recently Modified Files" in `HANDOVER.md` |
| **Ollama** | Optional | Only for the `local` tier. Without it, the router uses the free pool / economy |
| **API keys** (OpenAI / Anthropic) | Optional | Only for the paid `economy` / `flagship` tiers. Not needed if you only use the free pool |
| **Docker Compose** | Optional | Only if you want Redis as cache backend |

> **Minimum to try it out:** Python 3.10+ and nothing else. `ovhcloud` and `kilo` in the free pool work with no keys at all.

## 4. Installation

### 4.1 Get the code

```bash
git clone <repo-url> devin-token-optimizer
cd devin-token-optimizer
```

### 4.2 Quick install (setup scripts)

The setup scripts do everything for you: create `.venv`, install `requirements.txt`, copy `.env.example` to `.env` if missing, start Redis when Docker is available, and launch Uvicorn on `http://localhost:8000`.

**Linux / macOS / Devin CLI**

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

**Windows / Devin Desktop (PowerShell)**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

From an already-trusted PowerShell terminal you can simply run `.\scripts\setup.ps1`.

If you prefer to control each step, use the manual installation below.

### 4.3 Manual install: Linux / macOS

Run these commands from the project folder (`devin-token-optimizer/`).

**Step 1: check Python (3.10 or newer)**

```bash
python3 --version
```

If it is missing or too old:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 python3-venv python3-pip

# Fedora
sudo dnf install -y python3 python3-pip

# Arch
sudo pacman -S --needed python python-pip

# macOS (Homebrew)
brew install python
```

**Step 2: create and activate a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

**Step 3: install the dependencies from `requirements.txt`**

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Without activating the venv you can call it directly:
> `.venv/bin/python -m pip install -r requirements.txt`

**Step 4: create your configuration file**

```bash
cp .env.example .env
```

Then edit `.env` (see [Configuration](#5-configuration-env)).

### 4.4 Manual install: Windows

#### PowerShell (recommended)

**Step 1: check Python (3.10 or newer)**

```powershell
python --version
```

If it is missing, install it from <https://www.python.org/downloads/> (tick **"Add python.exe to PATH"**) or with winget:

```powershell
winget install -e --id Python.Python.3.12
```

**Step 2: create and activate a virtual environment**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> If PowerShell blocks the activation script ("running scripts is disabled"), allow it for the current session only:
>
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> .\.venv\Scripts\Activate.ps1
> ```

**Step 3: install the dependencies from `requirements.txt`**

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Without activating the venv:
> `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`

**Step 4: create your configuration file**

```powershell
copy .env.example .env
```

#### Command Prompt (cmd.exe)

```bat
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env
```

### 4.5 Verify the installation

With the virtual environment active:

```bash
# Linux / macOS
python -c "import fastapi, uvicorn, httpx, pydantic, redis, dotenv, yaml, pyperclip; print('All dependencies OK')"
pip list
```

```powershell
# Windows (PowerShell)
python -c "import fastapi, uvicorn, httpx, pydantic, redis, dotenv, yaml, pyperclip; print('All dependencies OK')"
pip list
```

You should see `All dependencies OK`.

**What each dependency does**

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | 0.115.12 | Web framework exposing the OpenAI-compatible API |
| `uvicorn[standard]` | 0.34.2 | ASGI server that runs the router |
| `httpx` | 0.28.1 | HTTP client used to call providers |
| `pydantic` | 2.11.7 | Request/response validation |
| `redis` | 6.2.0 | Client for the optional Redis cache backend (the Redis *server* is separate, see below) |
| `python-dotenv` | 1.1.1 | Loads settings from `.env` |
| `pyperclip` | 1.9.0 | Clipboard support for `context_packer.py --copy` |
| `PyYAML` | 6.0.3 | Parses `router/free_pool.yaml` |

### 4.6 Optional components

#### Ollama (for the free `local` tier)

1. Install Ollama from <https://ollama.com/download> (Linux: `curl -fsSL https://ollama.com/install.sh | sh`, macOS: `brew install ollama`, Windows: download the installer or `winget install Ollama.Ollama`).
2. Download the default model and start the server:

```bash
ollama pull qwen2.5-coder:7b
ollama serve
```

(On Windows and macOS the desktop app usually starts the server automatically.)

3. Check it responds: `curl http://localhost:11434/api/tags`

Without Ollama everything still works: `local` requests fall back to the free pool or economy tier.

#### Redis (for a shared/persistent cache)

```bash
docker run -d --name tokendiet-redis -p 6379:6379 redis:7
```

Without Redis, the cache silently falls back to `.cache/responses.json`.

#### OpenAI client for Python (only for the Python example in this README)

```bash
pip install openai
```

## 5. Configuration (.env)

Copy `.env.example` to `.env` and edit it. **Never commit `.env`.**

### Routing tiers

```dotenv
# Local tier (Ollama)
ROUTER_LOCAL_MODEL=qwen2.5-coder:7b
ROUTER_LOCAL_FALLBACK=true
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_API_KEY=

# Economy tier
ROUTER_ECONOMY_PROVIDER=openai
ROUTER_ECONOMY_MODEL=gpt-4o-mini
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1

# Flagship tier
ROUTER_FLAGSHIP_PROVIDER=openai
ROUTER_FLAGSHIP_MODEL=gpt-4o
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_BASE_URL=https://api.anthropic.com
```

To use **Claude Haiku** as the economy tier:

```dotenv
ROUTER_ECONOMY_PROVIDER=anthropic
ROUTER_ECONOMY_MODEL=claude-3-5-haiku-latest
```

### Classifier thresholds

```dotenv
ROUTER_LOCAL_MAX_CHARS=4000
ROUTER_ECONOMY_MAX_CHARS=15000
ROUTER_LOCAL_MAX_SCORE=25
ROUTER_ECONOMY_MAX_SCORE=65
ROUTER_ALLOW_FLAGSHIP=true
```

### Free pool, cache, handoff and misc

```dotenv
FREE_POOL_ENABLED=true
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=sk-or-...
AXFORGE_API_KEY=...

CACHE_BACKEND=auto                # auto | redis | memory
SEMANTIC_CACHE_ENABLED=true
SEMANTIC_CACHE_THRESHOLD=0.92
SEMANTIC_CACHE_MIN_CHARS=30
SEMANTIC_CACHE_MAX_ENTRIES=500

HANDOVER_ENABLED=true
SKILLS_ENABLED=true
PROVIDER_TIMEOUT_SECONDS=60       # raise it for slow providers
ROUTER_RELOAD=false               # true = auto-reload for development
```

### Which settings do I actually need?

| Your goal | Minimum configuration |
|---|---|
| Try it for free, no accounts | Nothing: leave defaults, `FREE_POOL_ENABLED=true` |
| Free + more reliable | Add `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY` |
| Offline / private local model | Install Ollama, `ollama pull qwen2.5-coder:7b` |
| Full cost-optimized setup | All of the above + `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY` |

## 6. Starting the router

With the virtual environment active (or using the explicit venv path):

```bash
# Linux / macOS
.venv/bin/uvicorn router.main:app --host 0.0.0.0 --port 8000
```

```powershell
# Windows (PowerShell)
.\.venv\Scripts\uvicorn router.main:app --host 0.0.0.0 --port 8000
```

For local-only access (safer), use `--host 127.0.0.1`. For development with auto-reload, set `ROUTER_RELOAD=true` in `.env`.

To stop the router press `Ctrl+C` in its terminal.

## 7. Tutorial: your first requests

Open a **second terminal** while the router is running.

### Step 1: health check

```bash
curl http://localhost:8000/health
```

Expected: `{"status":"ok",...}`.

> **Windows PowerShell:** `curl` is an alias of `Invoke-WebRequest`. Use `curl.exe` instead of `curl`, or use the PowerShell equivalents shown below.

### Step 2: your first chat completion

**Linux / macOS**

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Explain FastAPI in two sentences."}]}'
```

**Windows PowerShell**

```powershell
$body = @{
  model = "auto"
  messages = @(@{ role = "user"; content = "Explain FastAPI in two sentences." })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Uri http://localhost:8000/v1/chat/completions `
  -Method Post -ContentType "application/json" -Body $body
```

The answer is in `choices[0].message.content`.

### Step 3: see *why* the router chose a tier

Show the headers:

```bash
curl -si http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Solve: 2+2=?"}]}' | head -n 20
```

Look for `X-Devin-Route`, `X-Devin-Provider`, `X-Devin-Model`, `X-Devin-Score`, `X-Devin-Reasons` and `X-Cache`.

### Step 4: watch the cache work

Send the exact same request twice. The first reply has `X-Cache: MISS`, the second `X-Cache: HIT` (and costs nothing).

### Step 5: try a zero-token skill

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"/skill:repo_map"}]}'
```

The usage in the response shows `prompt_tokens: 0`.

### Step 6: check your savings

```bash
curl -s http://localhost:8000/stats
```

### Python example

```python
from openai import OpenAI   # pip install openai

client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Write a Python function that reverses a string."}],
)
print(resp.choices[0].message.content)
```

> The `api_key` value is ignored by the router itself; provider keys live in `.env`.

## 8. Routing in depth

### Automatic routing

Use `"model": "auto"`. `router/classifier.py` evaluates:

- **Context size**: `< 4000` chars -> local, `< 15000` -> economy, beyond -> flagship.
- **Critical keywords**: architecture, security, vulnerability, race condition, complex refactors -> flagship.
- **Complexity score** (0-100) compared against `ROUTER_LOCAL_MAX_SCORE` and `ROUTER_ECONOMY_MAX_SCORE`.

`local` and `economy` requests go to the **free pool first** (when enabled); `flagship` stays reserved for high complexity.

If Ollama is unreachable, `local` requests fall back to economy (`ROUTER_LOCAL_FALLBACK=true`).

### Explicit models

Bypass the classifier by naming a provider and model:

```json
{"model": "ollama/llama3.1:8b"}
{"model": "openai/gpt-4o-mini"}
{"model": "anthropic/claude-3-5-haiku-latest"}
```

### Forcing a tier

Send the `X-Devin-Route` (or `X-Route-Tier`) header with `local`, `economy` or `flagship`:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Devin-Route: flagship" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Review this design for race conditions."}]}'
```

### Diagnostic headers

| Header | Meaning |
|---|---|
| `X-Devin-Route` | Selected tier: `local`, `economy`, `flagship`, `free-pool`, `explicit`, `skill`, `handoff` |
| `X-Devin-Provider` | Final provider |
| `X-Devin-Model` | Final model |
| `X-Devin-Score` | Complexity score (0-100) |
| `X-Devin-Reasons` | Why that tier was chosen |
| `X-Cache` | `HIT`, `SEMANTIC`, `MISS`, `BYPASS`, `SKILL`, `HANDOFF` |

### Streaming

`"stream": true` returns Server-Sent Events in `chat.completion.chunk` format. Streaming responses are **not cached**.

```bash
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Count to 5."}]}'
```

## 9. Zero-token local skills

The router intercepts messages **before** the cache and the LLM. If the prompt matches a trigger in `skills/registry.json`, a deterministic local script runs and the reply costs `prompt_tokens: 0`.

| Skill | Example triggers | What it does |
|---|---|---|
| `repo_map` | `/skill:repo_map`, "repo map", "codebase map" | Compact codebase map built from the AST |
| `log_trimmer` | `/skill:log_trimmer`, "filter logs", "extract errors" | Extracts errors, warnings and stack traces from logs |
| `git_diff_test` | `/skill:git_diff_test`, "targeted tests", "tests for changed files" | Maps git-changed files to candidate tests |

**Add your own skill:** register it in `skills/registry.json` with a trigger, a script and a timeout, and put the script in `skills/`.

**Disable skills:** `SKILLS_ENABLED=false`.

## 10. Free-Tier Cloud Pool

`router/free_pool.yaml` defines an ordered list of free providers with OpenAI-style endpoints (`POST {api_base}/chat/completions`). `local` and `economy` requests go to the pool **instead of** paid providers.

| Name | Auth | Model |
|---|---|---|
| `ovhcloud` | none | `Qwen3.8-27B` |
| `kilo` | none | `cohere/north-mini-code:free` |
| `groq` | `GROQ_API_KEY` | `qwen/qwen3.8-27b` |
| `gemini` | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` |
| `openrouter` | `OPENROUTER_API_KEY` | `nvidia/nemotron-3-ultra-550b-a55b:free` |

**Rotation and failover**

1. If `requires_key: true` and the key variable is empty/missing, that provider is **silently skipped**.
2. On **429 (rate limit)**, timeout, or auth/service errors, the router moves to the next provider without interrupting the client.
3. If **all** providers fail, the request falls back to Ollama / economy.

**Add a custom provider** in `router/free_pool.yaml`:

```yaml
providers:
  - name: my-provider
    model: "openai/my-model"
    api_base: "https://api.example.com/v1"
    env_key: "MY_PROVIDER_API_KEY"
    requires_key: true
```

**Disable the pool:** `FREE_POOL_ENABLED=false`.

`GET /stats` reports `free_pool_requests`, `failover_events` and `free_pool_savings_usd` (estimated savings vs the flagship model).

## 11. AI Handoff System

`HANDOVER.md` (project root, gitignored, generated at runtime) lets different models and agents continue each other's work without re-sending the whole conversation.

```markdown
# HANDOVER — Handoff between AI agents

## Last Model Used & Timestamp
- Model: `gpt-4o` / Timestamp UTC: ...

## Current Task Status / Goal
<summary of the last user message>

## Recently Modified Files
<output of `git status --short`, fallback if not a git repo>

## Architectural Decisions & TODO for the Next Agent
- <notes preserved incrementally across updates>
```

**Automatic behavior**

- The router remembers the last model used.
- **On every model/route change** (Ollama -> free pool, 429 failover, flagship escalation) it updates `HANDOVER.md` and **injects it as a system message** into the new model's request.
- The TODO section is **incremental**: notes are appended, never overwritten.

**Manual trigger:** send a message containing `/handoff` (e.g. `/handoff` or `/handoff summarize the work`). The reply is a standard chat completion with `total_tokens: 0` and no LLM call.

**Disable:** `HANDOVER_ENABLED=false`.

## 12. Caching

| `CACHE_BACKEND` | Behavior |
|---|---|
| `auto` (default) | Redis if available, otherwise local JSON file + memory |
| `redis` | Requires a running Redis |
| `memory` | In-memory / local JSON file only |

- Fallback is **silent**: with no Redis, responses are stored in `.cache/responses.json` (24h TTL), no errors.
- Keys include provider, model and normalized payload.
- Streaming requests are never cached.

### Semantic cache

Besides exact matching, each prompt is compared with stored ones using **cosine similarity** over sparse token vectors (MD5 hashing + L2 normalization, standard library only). Above the threshold, the cached answer is returned with `X-Cache: SEMANTIC`.

```dotenv
SEMANTIC_CACHE_ENABLED=true
SEMANTIC_CACHE_THRESHOLD=0.92      # lower = more hits, higher risk of wrong matches
SEMANTIC_CACHE_MIN_CHARS=30
SEMANTIC_CACHE_MAX_ENTRIES=500
```

**Clear the cache:** stop the router and delete `.cache/responses.json` (`rm .cache/responses.json` on Linux/macOS, `del .cache\responses.json` on Windows). For Redis: `docker exec tokendiet-redis redis-cli FLUSHALL` (this wipes *everything* in that Redis instance).

## 13. Context packer

`devin_toolkit/context_packer.py` compresses and bundles source files into one Markdown/JSON/text document, so you can hand an agent lean context.

```bash
# Pack a directory to Markdown
python devin_toolkit/context_packer.py src/ -o context.md --compress --stats

# Pack specific files with a token budget
python devin_toolkit/context_packer.py router/main.py router/cache.py \
  --format markdown --token-budget 30000 -o router-context.md

# JSON output, excluding tests
python devin_toolkit/context_packer.py . --format json --exclude 'tests/**' -o context.json

# Dry run: see what would be included/excluded
python devin_toolkit/context_packer.py . --dry-run --compress

# Strip comments (docstrings kept) and copy to clipboard
python devin_toolkit/context_packer.py router/ --strip-comments --copy
```

On Windows use `python devin_toolkit\context_packer.py ...`.

**Features**

- Automatic exclusion of `.git`, `node_modules`, `.venv`, `dist`, `build`, caches, etc.
- Secret-like files (`.env`, keys, certificates) are excluded.
- Binary file detection; per-file, total and token limits.
- `--compress`: removes blank lines and trailing whitespace.
- `--strip-comments`: removes comments (docstrings preserved), about -25-40% estimated tokens.
- `--copy`: copies the bundle to the clipboard (uses `pyperclip`).
- `--line-numbers`, `--dry-run`, `--stats`.

## 14. Connecting your tools

The router is an **OpenAI-compatible** endpoint. In any client, set:

| Setting | Value |
|---|---|
| Base URL | `http://localhost:8000/v1` |
| API key | Any non-empty string (the router ignores it) |
| Model | `auto` (or an explicit `provider/model`) |

### Devin (Desktop / CLI)

1. Start the router.
2. Set `http://localhost:8000/v1` as the OpenAI-compatible base URL.
3. Use `"model": "auto"` for automatic routing.
4. To force a tier use `X-Devin-Route` / `X-Route-Tier`, or the `ollama/`, `openai/`, `anthropic/` prefixes.
5. Copy `devin_toolkit/.devinrules` as your agent rules: it adds the HANDOVER directive (read `HANDOVER.md` at startup, update it at task end).
6. Use `context_packer.py` to prepare lean context before large prompts.

### Cursor / Continue / other clients

Look for an "OpenAI-compatible" or "custom base URL" option and use the values in the table above.

### Plain environment variables (many SDKs and CLIs)

```bash
# Linux / macOS
export OPENAI_BASE_URL=http://localhost:8000/v1
export OPENAI_API_KEY=not-needed
```

```powershell
# Windows PowerShell
$env:OPENAI_BASE_URL = "http://localhost:8000/v1"
$env:OPENAI_API_KEY  = "not-needed"
```

## 15. API reference

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | POST | OpenAI-compatible chat completions (streaming supported) |
| `/health` | GET | Liveness check |
| `/stats` | GET | Per-route/provider/model breakdown, errors, average latency, cache and free-pool savings |

## 16. Docker Compose

```bash
docker compose up -d
```

Starts `redis` (port 6379) and `router` (port 8000). The router service uses `python:3.12-slim` and installs `requirements.txt` at startup; for production, build a dedicated image.

Useful commands:

```bash
docker compose logs -f router    # follow router logs
docker compose down              # stop everything
```

## 17. Tests and verification

```bash
# Compile check
python3 -m py_compile router/*.py devin_toolkit/context_packer.py

# Integration tests (pool failover, handoff, /handoff)
python3 tests/test_pool_and_handoff.py

# Live endpoints
curl -s http://localhost:8000/health
curl -s http://localhost:8000/stats
```

On Windows, replace `python3` with `python` and use `curl.exe` (or `Invoke-RestMethod`). Note that `py_compile router/*.py` relies on shell wildcards: in PowerShell use `python -m compileall router devin_toolkit`.

`tests/test_pool_and_handoff.py` verifies:

- **Failover**: a 429 on the first pool provider falls through to the second.
- **Handoff**: on a model change, `HANDOVER.md` is created and injected into the payload.
- **`/handoff`**: returns a chat completion with `total_tokens: 0`.

## 18. Maintenance: update, stop, uninstall

**Update code and dependencies**

```bash
# Linux / macOS (venv active)
git pull
pip install -r requirements.txt --upgrade
```

```powershell
# Windows PowerShell (venv active)
git pull
pip install -r requirements.txt --upgrade
```

Restart the router afterwards.

**Uninstall**

```bash
# Linux / macOS
deactivate
rm -rf .venv .cache HANDOVER.md
docker rm -f tokendiet-redis 2>/dev/null
```

```powershell
# Windows PowerShell
deactivate
Remove-Item -Recurse -Force .venv, .cache, HANDOVER.md -ErrorAction SilentlyContinue
docker rm -f tokendiet-redis
```

Then delete the project folder if you no longer need it.

## 19. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `python3: command not found` / `python is not recognized` | Python not installed or not in PATH | Reinstall Python and tick "Add to PATH" (Windows), or install via your package manager |
| `No module named venv` (Debian/Ubuntu) | `python3-venv` missing | `sudo apt install python3-venv` |
| `externally-managed-environment` error | Installing into system Python | Always use the virtual environment (`.venv`) |
| `running scripts is disabled on this system` (PowerShell) | Execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |
| `ModuleNotFoundError: No module named 'fastapi'` | Venv not active or deps not installed | Activate `.venv`, then `pip install -r requirements.txt` |
| `Address already in use` (port 8000) | Another process uses the port | Use another port: `uvicorn router.main:app --port 8001` |
| `Provider timeout` / 504 | Slow or unreachable provider | Increase `PROVIDER_TIMEOUT_SECONDS`, check your network |
| `OPENAI_API_KEY is not configured` | Missing key in `.env` | Add it to `.env` and restart the router |
| Slow `local` requests | Ollama not installed/running | Run `ollama serve`, or keep `FREE_POOL_ENABLED=true` |
| Always `X-Cache: MISS` | Different provider/model/payload, or streaming | Compare requests; check `/stats` -> `cache` |
| `/handoff` gets no response | Router not running | Check `GET /health` |
| `HANDOVER.md` not created | `HANDOVER_ENABLED=false`, or cached request | Enable it and retry with a different prompt |
| `curl` behaves strangely on Windows | PowerShell aliases `curl` | Use `curl.exe` or `Invoke-RestMethod` |

**Useful diagnostics:** the `X-Devin-Reasons` header explains the tier decision; `/stats` shows per-route/provider/model breakdown, errors and average latency.

## 20. Security

- Never commit `.env` or API keys (`.gitignore` already excludes them).
- `HANDOVER.md` and `.cache/` are gitignored.
- The context packer excludes secret-like files by default.
- Do not expose the router on untrusted networks without a reverse proxy with authentication (prefer `--host 127.0.0.1` when you only use it locally).
- Request contents are sent to the configured providers, **including the free pool**: check their data policies before sending proprietary code.

## 21. Known limitations

- Token estimation is heuristic (~4 chars per token), not a provider-specific tokenizer.
- Streaming requests do not benefit from caching.
- Anthropic tool calling is converted to OpenAI format; very advanced features may need specific adaptations.
- The classifier is configurable but does not guarantee perfect routing; use explicit models when you need deterministic control.
- The free pool depends on third-party free endpoints: rate limits and downtime are handled via failover but not eliminated.
- `HANDOVER.md` summarizes the last user message as task state: it is a heuristic summary, not full memory.

## 22. Project structure

```text
devin-token-optimizer/
├── .env.example              # configuration template
├── .gitignore
├── docker-compose.yml        # redis + router
├── requirements.txt          # Python dependencies
├── README.md
├── HANDOVER.md               # handoff report (generated at runtime, gitignored)
├── scripts/
│   ├── setup.sh              # Linux/macOS setup
│   └── setup.ps1             # Windows/PowerShell setup
├── skills/
│   ├── registry.json         # intents/triggers -> local scripts
│   ├── repo_map.py           # AST/regex codebase map
│   ├── log_trimmer.py        # extracts only errors/stack traces from logs
│   └── git_diff_test.py      # targeted tests from git status/diff
├── tests/
│   └── test_pool_and_handoff.py  # pool + handoff integration tests
├── router/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, routing, pool, handoff, endpoints
│   ├── classifier.py         # local/economy/flagship tier classification
│   ├── cache.py              # exact + semantic cache (Redis/file)
│   ├── handoff.py            # HandoffManager for HANDOVER.md
│   ├── free_pool.yaml        # ordered free providers for failover
│   └── skills_manager.py     # zero-token local skill execution
└── devin_toolkit/
    ├── .devinrules           # agent operating rules (incl. HANDOVER)
    └── context_packer.py     # context packer for compact prompts
```
