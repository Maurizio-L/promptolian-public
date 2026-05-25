# Promptolian

Transparent proxy and compression SDK for AI agents. Caches tool schemas automatically so you stop paying for the same tokens on every call.

**[promptolian.com](https://promptolian.com)** · [Pricing](https://promptolian.com/pricing.html) · [Dashboard](https://promptolian.com/dashboard.html) · [Docs](https://promptolian.com/docs.html)

---

## What it does

| Layer | Savings | How |
|---|---|---|
| **Tool schemas** | ~90% session avg | Proxy injects `cache_control`; Anthropic charges 10% on cached hits |
| **Conversation history** | 52.9% | KV-cache sandwich layout — old turns summarised, first/last kept verbatim |
| **Prompt text** | ~20% | Symbol rules, filler removal, grammar cleanup |

**100% fact preservation** — numbers, file paths, named entities survive unchanged.

---

## Quickstart

### Option A — Transparent proxy (any language)

```bash
pip install "promptolian[proxy]"
promptolian proxy              # starts at localhost:3002
```

Point your client at the proxy instead of Anthropic:

```python
import anthropic
client = anthropic.Anthropic(base_url="http://localhost:3002")
# All calls compressed automatically — no other changes needed
```

### Option B — Python SDK wrapper

```bash
pip install promptolian
```

```python
from promptolian import patch_anthropic
patch_anthropic()              # one call at startup

import anthropic
client = anthropic.Anthropic() # works normally, compression is transparent
```

### Option C — Claude Code MCP

```bash
pip install "promptolian[mcp]"
promptolian mcp install        # restart Claude Code after this
```

---

## Cloud proxy

Skip self-hosting. Point your agent at `proxy.promptolian.com`:

```python
client = anthropic.Anthropic(
    base_url="https://proxy.promptolian.com",
    default_headers={"X-Promptolian-Key": "pk_..."},
)
```

| Plan | Price | Keys | Sessions |
|---|---|---|---|
| Free | $0 | — | SQLite, self-hosted |
| Solo | $9/mo | 1 | PostgreSQL, always-on |
| Team | $29/mo | Up to 10 | PostgreSQL + per-project breakdown |

Sign up at [promptolian.com/pricing.html](https://promptolian.com/pricing.html).

---

## Self-hosting the API

```bash
pip install -r requirements-selfhost.txt
python api/api.py
```

| Env var | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL URL (defaults to SQLite) |
| `PROMPTOLIAN_MASTER_KEY` | Activates API key auth (cloud mode) |
| `STRIPE_SECRET_KEY` | Billing (optional) |

API runs on port `3001`.

---

## Response headers (proxy)

Every proxied response includes:

```
X-Promptolian-Cache-Hit: true|false
X-Promptolian-Tokens-Saved: 840
X-Promptolian-Session: sess_abc123
```