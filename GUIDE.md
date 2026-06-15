# Developer Guide

How to integrate Promptolian into your agent, verify it's working, and use each feature.

---

## Contents

1. [Choose your integration path](#1-choose-your-integration-path)
2. [Start the proxy](#2-start-the-proxy)
3. [Point your client at the proxy](#3-point-your-client-at-the-proxy)
4. [Verify it's working](#4-verify-its-working)
5. [Tool result compression](#5-tool-result-compression)
6. [Tool schema caching](#6-tool-schema-caching)
7. [Session reset (context wall prevention)](#7-session-reset-context-wall-prevention)
8. [Claude Code MCP](#8-claude-code-mcp)
9. [Environment variables](#9-environment-variables)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Choose your integration path

| Path | When to use |
|---|---|
| **Proxy** | Any language, any agent framework — one line change |
| **SDK patch** | Python only, prefer not to change `base_url` |
| **MCP** | Claude Code users |

All three do the same thing under the hood. Most users want the proxy.

---

## 2. Start the proxy

```bash
pip install "promptolian[proxy]"
python -m promptolian.proxy
```

Expected output:
```
Promptolian Proxy
─────────────────────────────────────────────────
Mode      : local
Storage   : sqlite (~/.promptolian/sessions.db)
Listening : http://127.0.0.1:3002
```

**With session reset** (requires API key — see [section 7](#7-session-reset-context-wall-prevention)):
```bash
PROMPTOLIAN_API_KEY=your_key python -m promptolian.proxy --reset-at 0.70
```

Output will show which compression mode is active:
```
Session reset : at 70% · compression: cloud (https://api.promptolian.com)
```

---

## 3. Point your client at the proxy

**Anthropic Python SDK:**
```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:3002",   # only change
)
```

**OpenAI Python SDK:**
```python
import openai

client = openai.OpenAI(
    base_url="http://localhost:3002/v1",
    api_key="your-openai-key",
)
```

**curl:**
```bash
curl http://localhost:3002/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":64,"messages":[{"role":"user","content":"hello"}]}'
```

**Claude Code** — add to `~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:3002"
  }
}
```

**JavaScript / TypeScript:**
```typescript
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({
  baseURL: "http://localhost:3002",
});
```

---

## 4. Verify it's working

**Health check:**
```bash
curl http://localhost:3002/proxy/health
```
```json
{"status": "ok", "storage": "sqlite", "mode": "local"}
```

**Check response headers after an API call:**
```bash
curl -s -D - -o /dev/null http://localhost:3002/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":64,"messages":[{"role":"user","content":"hello"}]}' \
  | grep X-Promptolian
```

Expected headers:
```
X-Promptolian-Session: <session-id>
X-Promptolian-Tokens-Saved: <n>
X-Promptolian-Cache-Hit: false
```

**Check active sessions:**
```bash
curl http://localhost:3002/proxy/sessions
```

---

## 5. Tool result compression

No configuration needed — fires automatically on every request for free.

When an agent reads the same file, runs the same bash command, or gets the same API response more than once in a session, the proxy replaces subsequent identical outputs with a short reference before forwarding to the model:

```
Call 1: read_file("config.yaml") → full content (400 tokens)
Call 2: read_file("config.yaml") → [TOOL_CACHE_REF: same as call #0]  (5 tokens)
Call 3: read_file("config.yaml", modified) → [TOOL_CACHE_DIFF from call #0:
+max_connections: 200
-max_connections: 100]  (12 tokens)
```

**Benchmark it yourself:**
```bash
# Generate synthetic agentic sessions
python3 tools/scripts/gen_agentic_sessions.py

# Run compression benchmark
python3 tools/scripts/benchmark_tool_compression.py

# Verbose — per-session breakdown
python3 tools/scripts/benchmark_tool_compression.py --verbose
```

Expected output:
```
Total tool result tokens : 10,608
After compression        : 6,937
Tokens saved             : 3,671 (34.6%)
REF compressions         : 12
DIFF compressions        : 6
Avg fact retention       : 99.0%
```

**When compression fires:**
- REF: exact same content seen before in the session
- DIFF: content is ≥70% similar to a previous result and the diff is shorter than 60% of the original

**Response header:**
```
X-Promptolian-Tool-Tokens-Saved: 320
```

---

## 6. Tool schema caching

No configuration needed — fires automatically for free.

On the first call, the proxy stores your tool schemas and adds `cache_control` before forwarding. On subsequent calls, Anthropic's prompt cache serves the schema at 10% of the normal token price.

```
Call 1: tools sent → stored + cache_control added → Anthropic caches for 5 min
Call 2: tools omitted or re-sent → proxy re-injects from cache → Anthropic bills at 10%
```

**Response header:**
```
X-Promptolian-Cache-Hit: true
X-Promptolian-Tokens-Saved: 540
```

**Cost at 500 calls/day, 5 tools:**
```
Without proxy : 9,000,000 tokens/month → $27.00
With proxy    : 900,000 tokens/month   → $2.70
```

You can pass tools on every call or omit them after the first — the proxy handles both correctly.

---

## 7. Session reset (context wall prevention)

Requires a `PROMPTOLIAN_API_KEY`. Get one at [promptolian.com/pricing](https://promptolian.com/pricing.html).

```bash
PROMPTOLIAN_API_KEY=your_key python -m promptolian.proxy --reset-at 0.70
```

The proxy tracks cumulative token usage per session. When it reaches the threshold (e.g. 70% of the model's context window), it:

1. Compresses the full conversation history using KV-sandwich
2. Starts a fresh session
3. Injects the compressed history as a system prompt
4. Forwards the current user turn to the new session

The model never sees a context long enough to trigger provider-side compression. Your agent code does not change.

**Context window registry (built in):**

| Model family | Window |
|---|---|
| Claude Opus 4 / Sonnet 4 | 200,000 tokens |
| GPT-4o / GPT-4o-mini | 128,000 tokens |
| o1 / o3 | 128,000 tokens |

**Response header on reset:**
```
X-Promptolian-Reset: true
```

**Compression priority:**

1. Local `context_engine` (if running from source)
2. Cloud API (`PROMPTOLIAN_API_KEY` set)
3. Skip reset (no key, no local engine)

**KV-sandwich quality (25 sessions, Factory.ai scoring):**

| | Promptolian | Anthropic built-in | OpenAI built-in |
|---|---|---|---|
| Quality score | **4.26 / 5** | 3.44 / 5 | 3.35 / 5 |

---

## 8. Claude Code MCP

```bash
pip install "promptolian[mcp]"
```

Add to `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "promptolian": {
      "command": "promptolian-mcp"
    }
  }
}
```

Restart Claude Code. Run `/mcp` to confirm it loaded.

**Available tools:**

| Tool | What it does |
|---|---|
| `compress_prompt` | Compress any text prompt locally — no network call |
| `compress_tools_schema` | Compress a JSON tool schema array to compact DSL |
| `compression_stats` | Show tokens saved in the current session |

**Usage inside a Claude Code session:**
```
Use compress_prompt to compress this: [paste a long system prompt]
Show me compression_stats
```

---

## 9. Environment variables

| Variable | Default | Description |
|---|---|---|
| `PROMPTOLIAN_API_KEY` | — | Unlocks cloud KV-sandwich compression and session reset |
| `PROMPTOLIAN_API_URL` | `https://api.promptolian.com` | Override for self-hosted API server |
| `DATABASE_URL` | — | PostgreSQL URL — defaults to SQLite at `~/.promptolian/sessions.db` |
| `PROMPTOLIAN_MASTER_KEY` | — | Cloud mode only — activates API key auth |
| `STRIPE_SECRET_KEY` | — | Cloud mode only — billing |

---

## 10. Troubleshooting

**Proxy won't start — port in use:**
```bash
lsof -i :3002          # find what's using the port
python -m promptolian.proxy --port 3003   # use a different port
```

**`X-Promptolian-Tokens-Saved: 0` on every call:**
Tool schemas aren't being cached. Check that your call includes a `tools` array on the first request. The proxy needs to see the schemas once to store them.

**Session reset not firing:**
- Check startup message — should show `compression: cloud (...)` not `disabled`
- Verify `PROMPTOLIAN_API_KEY` is set in the same shell as the proxy process
- Token count must actually reach the `--reset-at` threshold — use long sessions to trigger it

**DIFF compressions not firing:**
Expected for short tool outputs or outputs with many differences. DIFF only fires when the diff is shorter than 60% of the original content. The REF path fires on exact repeats. Run the benchmark with `--verbose` to see which sessions hit each path.

**`/proxy/health` returns 404:**
The health endpoint is at `/proxy/health`, not `/health`.

**MCP not showing in `/mcp`:**
Check `~/.claude/settings.json` has the `mcpServers` block and `promptolian-mcp` is in your PATH:
```bash
which promptolian-mcp
```
If missing: `pip install "promptolian[mcp]"`, then restart Claude Code.
