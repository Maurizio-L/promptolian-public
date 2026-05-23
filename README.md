# Promptolian

Token compression for Claude, ChatGPT, Gemini, Copilot and any LLM API. Three independent compression layers stack together — compress a single prompt, an entire conversation history, and your tool schemas at the same time.

**promptolian.com** — free browser extension, REST API, CLI, and native Claude Code MCP integration.

---

## What it does

| Layer | What it compresses | Savings |
|---|---|---|
| **Prompt** | Single prompt or message | 15–33% |
| **Context** | Multi-turn conversation history | up to 52.9% (20-turn, KV geometry) |
| **Tool schema** | JSON function definitions sent on every agent call | 69% turn 1 · 97% turn 2+ |

All three layers are fully deterministic. No LLM calls. No prompt data leaves your machine on standard/pro/developer tiers. Sub-millisecond latency.

**100% fact preservation rate** across 41 benchmark runs — every number, entity, file path, and named term survives compression unchanged.

---

## Quick start

### Browser extension

Install from [promptolian.com](https://promptolian.com). Click the toolbar button before sending any prompt on Claude, ChatGPT, Gemini, Copilot, or Perplexity. One click to restore.

### CLI

```bash
pip install promptolian

promptolian compress "You are an expert Python developer. Debug the function on line 47 and return only the corrected code." --tier developer
# → §EXP py. BUG FN line 47 →code

promptolian compress --file my_prompt.txt --tier pro --json
promptolian session          # cumulative stats for this session
promptolian session reset
promptolian stats            # lifetime stats from local DB or API
```

### REST API

```bash
# Compress a prompt
curl -X POST https://api.promptolian.com/compress-prompt \
  -H "Content-Type: application/json" \
  -d '{"text": "You are an expert Python developer...", "tier": "pro"}'

# Compress conversation history
curl -X POST https://api.promptolian.com/optimize-context \
  -H "Content-Type: application/json" \
  -d '{"messages": [...], "query": "current user question", "mode": "lossless"}'

# Compress tool schemas
curl -X POST https://api.promptolian.com/compress-tools \
  -H "Content-Type: application/json" \
  -d '{"tools": [...], "session_id": "my-session-123"}'
```

### Claude Code MCP

```bash
promptolian-server   # starts the MCP server

# In Claude Code, the following tools become available:
# compress_prompt, compress_tools, compression_stats
```

Add to your Claude Code MCP config:
```json
{
  "mcpServers": {
    "promptolian": {
      "command": "promptolian-server"
    }
  }
}
```

---

## API reference

### `POST /compress-prompt`

```json
{
  "text": "string — the prompt to compress",
  "tier": "standard | pro | developer",
  "lang": "auto | en | es | fr | de | it"
}
```

Response:
```json
{
  "compressed": "compressed text",
  "original_tokens": 42,
  "compressed_tokens": 28,
  "tokens_saved": 14,
  "tokens_saved_pct": 33,
  "elapsed_ms": 2.1
}
```

### `POST /optimize-context`

```json
{
  "messages": [{"role": "user", "content": "..."}, ...],
  "query": "current user question",
  "summary": "",
  "mode": "lossless | aggressive",
  "use_kv_geometry": false,
  "kv_prefix": 2,
  "kv_tail": 4
}
```

`use_kv_geometry: true` activates the KV-cache-aware sandwich layout — verbatim prefix and tail turns bracket a compressed middle, maximising cache reuse across turns. Reaches 52.9% CR on 20-turn sessions.

### `POST /compress-tools`

```json
{
  "tools": [...],
  "session_id": "optional — enables turn-2+ caching"
}
```

First call: returns compressed DSL (~69% smaller). Subsequent calls with the same `session_id`: returns `TOOLS:[name1,name2]` (~97% smaller). Session cache is stored in the database and survives API restarts.

### `POST /feedback`

```json
{
  "original": "...",
  "compressed": "...",
  "rating": 1
}
```

### `GET /stats`

Returns aggregate compression statistics for the current API key.

---

## Compression tiers

| Tier | Engine | What it does | Savings |
|---|---|---|---|
| **standard** | Rule-based, browser-safe | Symbol substitution, filler removal | 15–20% |
| **pro** | Standard + grammar | Verbose phrase removal, math operators, unit abbreviation | 20–25% |
| **developer** | Pro + NLP | Domain packs, clause scoring, abbreviation | up to 33% |

All tiers support: `en`, `es`, `fr`, `de`, `it`. Chinese/Cantonese support in progress.

---

## Self-hosting

```bash
git clone https://github.com/promptolian/promptolian
cd promptolian

# Docker
docker compose up

# Or plain Python
pip install -r requirements-selfhost.txt
python api/api.py
```

Environment variables:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | No | PostgreSQL URL. Falls back to SQLite if not set. |
| `STRIPE_SECRET_KEY` | For billing | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | For billing | Stripe webhook signing secret |
| `GROQ_API_KEY` | No | Enables neural summarisation in context engine |

The API runs on port `3001` by default.

---

## Repository layout

```
public/
├── api/
│   ├── api.py              # Flask REST API — all endpoints
│   ├── context_engine.py   # Layer 2: conversation history compression
│   └── engine_v4.py        # Layer 1: prompt compression (SOLID architecture)
├── extension/
│   ├── chrome/             # Chrome extension
│   └── firefox/            # Firefox extension
├── website/
│   ├── index.html          # Marketing site (self-contained, no build step)
│   ├── docs.html           # API documentation
│   ├── benchmarks.html     # Benchmark results
│   └── privacy.html
├── Dockerfile.selfhost
├── docker-compose.yml
├── requirements.txt        # Production dependencies
└── requirements-selfhost.txt
```

---

## Benchmarks

| Scenario | Tier | CR | FPR |
|---|---|---|---|
| Short prompt (8-turn) | context lossless | 14% | 100% |
| Short prompt (8-turn) | context aggressive | 17% | 100% |
| Long session (20-turn) | context lossless | 43% | 100% |
| Long session (20-turn) | KV geometry lossless | **52.9%** | 100% |
| Long session (20-turn) | KV geometry aggressive | **53.5%** | 100% |
| Tool schema (turn 1) | developer | 69% | 100% |
| Tool schema (turn 2+) | developer cached | 97% | 100% |
| Combined dev + context | 5-turn session | 33% | 100% |

FPR = Fact Preservation Rate. Measured by checking that every number, named entity, file path, and URL in the input appears unchanged in the output.

---

## Links

- [promptolian.com](https://promptolian.com)
- [API docs](https://promptolian.com/docs)
- [Benchmarks](https://promptolian.com/benchmarks)
- [Privacy policy](https://promptolian.com/privacy)