# Promptolian

Compress prompts, conversation history, and tool schemas before sending to any LLM. Fully deterministic — no LLM calls, no data leaves your machine.

**[promptolian.com](https://promptolian.com)** · [API docs](https://promptolian.com/docs) · [Benchmarks](https://promptolian.com/benchmarks)

---

## Why it's different

Most compression tools shorten a single prompt. Promptolian stacks three independent layers:

| Layer | Saves | How |
|---|---|---|
| **Prompt** | 15–33% | Symbol rules, grammar cleanup, domain packs |
| **Conversation history** | up to 52.9% | KV-cache-aware sandwich layout across turns |
| **Tool schemas** | 69% turn 1 · **97% turn 2+** | JSON → DSL compiler with session caching |

The tool schema layer is the headline: on the second call, the entire schema block collapses to `TOOLS:[name1,name2]` — three tokens. No other tool does this.

**100% fact preservation** across 41 runs — numbers, file paths, named entities survive unchanged.

---

## Get started

```bash
# CLI
pip install promptolian
promptolian compress "You are an expert Python developer..." --tier developer

# API
curl -X POST https://api.promptolian.com/compress-prompt \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "tier": "pro"}'

# Claude Code MCP
promptolian-server   # exposes compress_prompt, compress_tools, compression_stats
```

Browser extension for Claude, ChatGPT, Gemini, Copilot — install at [promptolian.com](https://promptolian.com).

---

## Self-hosting

```bash
docker compose up
# or: pip install -r requirements-selfhost.txt && python api/api.py
```

| Env var | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL URL (defaults to SQLite) |
| `STRIPE_SECRET_KEY` | Billing (optional) |
| `GROQ_API_KEY` | Neural summarisation in context engine (optional) |

API runs on port `3001`.
