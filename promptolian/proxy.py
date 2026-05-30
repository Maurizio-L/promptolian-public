#!/usr/bin/env python3
"""
Promptolian Proxy — transparent Anthropic + OpenAI proxy with tool schema caching.

LOCAL (default):
    promptolian proxy               # localhost:3002, SQLite sessions
    promptolian proxy --port 8080

CLOUD (Railway / production):
    Set DATABASE_URL env var → sessions stored in PostgreSQL.
    Set PROMPTOLIAN_MASTER_KEY env var → API key auth required on every request.
    Set STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET + STRIPE_CLOUD_MONTHLY → billing.

Usage (local, no auth):
    client = anthropic.Anthropic(base_url="http://localhost:3002")

Usage (cloud, with key):
    client = anthropic.Anthropic(
        base_url="https://proxy.promptolian.com",
        default_headers={"X-Promptolian-Key": "your-api-key"},
    )

Session caching:
    Call 1 — send tools normally.  Proxy stores them + sets cache_control.
    Call 2+ — omit tools entirely. Proxy re-injects with cache_control.
             Anthropic charges 10% of normal price → ~90% tool token savings.

Response headers:
    X-Promptolian-Cache-Hit: true|false
    X-Promptolian-Tokens-Saved: <int>
    X-Promptolian-Session: <session_id>
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional

try:
    import httpx
    from flask import Flask, Response, request, jsonify
    from flask_cors import CORS
except ImportError:
    raise ImportError(
        "Proxy dependencies not installed.\n"
        "Run: pip install 'promptolian[proxy]'"
    )

ANTHROPIC_API = 'https://api.anthropic.com'
OPENAI_API    = 'https://api.openai.com'

_CACHE_TTL        = 5 * 60         # Anthropic prompt cache TTL (seconds)
_COMPRESS_HISTORY = False           # set to True via --compress CLI flag

try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent / 'api'))
    from context_engine import ContextEngine as _ContextEngine
    _CONTEXT_ENGINE_AVAILABLE = True
except ImportError:
    _CONTEXT_ENGINE_AVAILABLE = False
_DATABASE_URL    = os.getenv('DATABASE_URL')          # set by Railway
_MASTER_KEY      = os.getenv('PROMPTOLIAN_MASTER_KEY')  # required in cloud mode
_STRIPE_KEY          = os.getenv('STRIPE_SECRET_KEY', '')
_STRIPE_WEBHOOK      = os.getenv('STRIPE_WEBHOOK_SECRET', '')
_STRIPE_SOLO_MONTHLY = os.getenv('STRIPE_SOLO_MONTHLY', '')
_STRIPE_TEAM_MONTHLY = os.getenv('STRIPE_TEAM_MONTHLY', '')
_BASE_URL            = os.getenv('BASE_URL', 'https://promptolian.com')

_PLAN_KEY_LIMITS = {'solo': 1, 'team': 10}  # max API keys per plan

_DB_PATH = Path.home() / '.promptolian' / 'sessions.db'  # local fallback

# ── Sensitive data detection patterns ─────────────────────────────────────────

_HIGH_RISK: dict[str, re.Pattern] = {
    'CONNECTION_STRING': re.compile(
        r'(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?|mssql|sqlserver)://\S{10,}',
        re.IGNORECASE,
    ),
    'API_KEY': re.compile(
        r'(?:'
        r'sk-[A-Za-z0-9]{20,}'            # OpenAI / Anthropic style
        r'|AKIA[0-9A-Z]{16}'              # AWS access key
        r'|ghp_[A-Za-z0-9]{36}'           # GitHub PAT
        r'|gho_[A-Za-z0-9]{36}'           # GitHub OAuth
        r'|xoxb-\d{9,}-\S{20,}'           # Slack bot token
        r'|AIza[0-9A-Za-z_\-]{35}'        # Google API key
        r')',
        re.IGNORECASE,
    ),
    'PRIVATE_KEY': re.compile(
        r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----',
    ),
    'JWT': re.compile(
        r'ey[A-Za-z0-9_\-]{10,}\.ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}',
    ),
    'ENV_FILE': re.compile(
        r'(?:(?:^|\n)[A-Z][A-Z0-9_]{2,}=[^\n]{3,}\n){3,}',
        re.MULTILINE,
    ),
}

_MEDIUM_RISK: dict[str, re.Pattern] = {
    'SQL_DUMP': re.compile(
        r'(?:INSERT\s+INTO\s+\w+[^;]{5,};\s*){3,}',
        re.IGNORECASE | re.DOTALL,
    ),
    'STACK_TRACE': re.compile(
        r'Traceback \(most recent call last\)',
        re.IGNORECASE,
    ),
    'CSV_DATA': re.compile(
        r'(?:[^\n,]{1,60},){4}[^\n,]{1,60}\n'
        r'(?:[^\n,]{1,60},){4}[^\n,]{1,60}\n'
        r'(?:[^\n,]{1,60},){4}',
        re.MULTILINE,
    ),
    'LARGE_JSON': re.compile(
        r'\[\s*\{[^{}]{20,}\}(?:\s*,\s*\{[^{}]{20,}\}){9,}\s*\]',
        re.DOTALL,
    ),
}

app = Flask(__name__)
CORS(app)


# ── Database (SQLite local · PostgreSQL cloud) ────────────────────────────────

def _is_pg() -> bool:
    return bool(_DATABASE_URL)


def _get_conn():
    if _is_pg():
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(_DATABASE_URL)
        psycopg2.extras.register_uuid(conn)
        return conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _p() -> str:
    return '%s' if _is_pg() else '?'


def _ensure_schema() -> None:
    conn = _get_conn()
    p = _p()
    try:
        if _is_pg():
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS proxy_sessions (
                    session_id  TEXT PRIMARY KEY,
                    tools_json  TEXT NOT NULL,
                    last_call   DOUBLE PRECISION NOT NULL
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS proxy_users (
                    api_key      TEXT PRIMARY KEY,
                    email        TEXT NOT NULL,
                    plan         TEXT NOT NULL DEFAULT 'solo',
                    stripe_sub   TEXT,
                    status       TEXT NOT NULL DEFAULT 'active',
                    project_name TEXT NOT NULL DEFAULT 'default',
                    created_at   TIMESTAMP DEFAULT NOW(),
                    tokens_saved BIGINT DEFAULT 0
                )
            ''')
            cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_project ON proxy_users (email, project_name)')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS pii_events (
                    id          SERIAL PRIMARY KEY,
                    session_id  TEXT NOT NULL,
                    api_key     TEXT,
                    timestamp   DOUBLE PRECISION NOT NULL,
                    categories  TEXT NOT NULL,
                    risk_level  TEXT NOT NULL,
                    preview     TEXT
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_pii_api_key ON pii_events (api_key)')
            conn.commit()
            cur.close()
        else:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS proxy_sessions (
                    session_id  TEXT PRIMARY KEY,
                    tools_json  TEXT NOT NULL,
                    last_call   REAL NOT NULL
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS proxy_users (
                    api_key      TEXT PRIMARY KEY,
                    email        TEXT NOT NULL,
                    plan         TEXT NOT NULL DEFAULT 'solo',
                    stripe_sub   TEXT,
                    status       TEXT NOT NULL DEFAULT 'active',
                    project_name TEXT NOT NULL DEFAULT 'default',
                    created_at   TEXT DEFAULT (datetime(\'now\')),
                    tokens_saved INTEGER DEFAULT 0
                )
            ''')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_project ON proxy_users (email, project_name)')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS pii_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    api_key     TEXT,
                    timestamp   REAL NOT NULL,
                    categories  TEXT NOT NULL,
                    risk_level  TEXT NOT NULL,
                    preview     TEXT
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_pii_api_key ON pii_events (api_key)')
            conn.commit()
    finally:
        conn.close()


# ── Session persistence ───────────────────────────────────────────────────────

def _load_session(session_id: str) -> Optional[tuple[list, float]]:
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f'SELECT tools_json, last_call FROM proxy_sessions WHERE session_id = {p}', (session_id,))
                row = cur.fetchone()
                cur.close()
                if row is None:
                    return None
                return json.loads(row[0]), float(row[1])
            else:
                row = conn.execute(f'SELECT tools_json, last_call FROM proxy_sessions WHERE session_id = {p}', (session_id,)).fetchone()
                if row is None:
                    return None
                return json.loads(row['tools_json']), row['last_call']
        finally:
            conn.close()
    except Exception:
        return None


def _save_session(session_id: str, tools: list) -> None:
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f'''
                    INSERT INTO proxy_sessions (session_id, tools_json, last_call)
                    VALUES ({p}, {p}, {p})
                    ON CONFLICT (session_id) DO UPDATE SET
                        tools_json = EXCLUDED.tools_json,
                        last_call  = EXCLUDED.last_call
                ''', (session_id, json.dumps(tools), time.time()))
                conn.commit()
                cur.close()
            else:
                conn.execute(f'''
                    INSERT INTO proxy_sessions (session_id, tools_json, last_call)
                    VALUES ({p}, {p}, {p})
                    ON CONFLICT(session_id) DO UPDATE SET
                        tools_json = excluded.tools_json,
                        last_call  = excluded.last_call
                ''', (session_id, json.dumps(tools), time.time()))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _touch_session(session_id: str) -> None:
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f'UPDATE proxy_sessions SET last_call = {p} WHERE session_id = {p}', (time.time(), session_id))
                conn.commit()
                cur.close()
            else:
                conn.execute(f'UPDATE proxy_sessions SET last_call = {p} WHERE session_id = {p}', (time.time(), session_id))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _delete_session(session_id: str) -> None:
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f'DELETE FROM proxy_sessions WHERE session_id = {p}', (session_id,))
                conn.commit()
                cur.close()
            else:
                conn.execute(f'DELETE FROM proxy_sessions WHERE session_id = {p}', (session_id,))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _record_savings(api_key: str, tokens_saved: int) -> None:
    if not api_key or not tokens_saved:
        return
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f'UPDATE proxy_users SET tokens_saved = tokens_saved + {p} WHERE api_key = {p}', (tokens_saved, api_key))
                conn.commit()
                cur.close()
            else:
                conn.execute(f'UPDATE proxy_users SET tokens_saved = tokens_saved + {p} WHERE api_key = {p}', (tokens_saved, api_key))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ── Sensitive data detection ─────────────────────────────────────────────────

def _extract_message_text(body: dict) -> str:
    parts = []
    for msg in body.get('messages', []):
        content = msg.get('content', '')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    parts.append(block.get('text', ''))
    return '\n'.join(parts)


def _detect_sensitive_data(text: str) -> list[dict]:
    if not text or len(text) < 20:
        return []
    hits = []
    for category, pattern in _HIGH_RISK.items():
        if pattern.search(text):
            hits.append({'category': category, 'risk_level': 'HIGH'})
    for category, pattern in _MEDIUM_RISK.items():
        if pattern.search(text):
            hits.append({'category': category, 'risk_level': 'MEDIUM'})
    return hits


def _record_pii_event(session_id: str, api_key: Optional[str], hits: list[dict]) -> None:
    if not hits:
        return
    risk_level = 'HIGH' if any(h['risk_level'] == 'HIGH' for h in hits) else 'MEDIUM'
    categories = json.dumps([h['category'] for h in hits])
    # preview intentionally not stored — category name is sufficient to know what to rotate
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(
                    f'INSERT INTO pii_events (session_id, api_key, timestamp, categories, risk_level) VALUES ({p},{p},{p},{p},{p})',
                    (session_id, api_key, time.time(), categories, risk_level),
                )
                conn.commit()
                cur.close()
            else:
                conn.execute(
                    f'INSERT INTO pii_events (session_id, api_key, timestamp, categories, risk_level) VALUES ({p},{p},{p},{p},{p})',
                    (session_id, api_key, time.time(), categories, risk_level),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ── API key auth ──────────────────────────────────────────────────────────────

def _validate_api_key(key: str) -> Optional[dict]:
    """Return user row dict if key is valid and active, else None."""
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(
                    f"SELECT api_key, email, plan, status, project_name, tokens_saved FROM proxy_users WHERE api_key = {p}",
                    (key,),
                )
                row = cur.fetchone()
                cur.close()
                if row is None:
                    return None
                return {'api_key': row[0], 'email': row[1], 'plan': row[2],
                        'status': row[3], 'project_name': row[4], 'tokens_saved': row[5]}
            else:
                row = conn.execute(
                    f"SELECT api_key, email, plan, status, project_name, tokens_saved FROM proxy_users WHERE api_key = {p}",
                    (key,),
                ).fetchone()
                if row is None:
                    return None
                return dict(row)
        finally:
            conn.close()
    except Exception:
        return None


def _list_user_keys(email: str) -> list[dict]:
    """Return all API keys for a given email (for Team dashboard)."""
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(
                    f"SELECT api_key, project_name, status, tokens_saved, created_at FROM proxy_users WHERE email = {p} ORDER BY created_at",
                    (email,),
                )
                rows = cur.fetchall()
                cur.close()
                return [{'api_key': r[0], 'project_name': r[1], 'status': r[2],
                         'tokens_saved': r[3], 'created_at': str(r[4])} for r in rows]
            else:
                rows = conn.execute(
                    f"SELECT api_key, project_name, status, tokens_saved, created_at FROM proxy_users WHERE email = {p} ORDER BY created_at",
                    (email,),
                ).fetchall()
                return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _check_auth() -> tuple[Optional[str], Optional[Response]]:
    """
    Returns (promptolian_api_key, error_response).
    In cloud mode (MASTER_KEY set): X-Promptolian-Key required and validated.
    In local mode: no auth needed, returns (None, None).
    """
    if not _MASTER_KEY:
        return None, None   # local mode — no auth

    key = request.headers.get('X-Promptolian-Key', '').strip()
    if not key:
        return None, (jsonify({
            'error': 'Missing X-Promptolian-Key header.',
            'signup': f'{_BASE_URL}/pricing.html',
        }), 401)

    user = _validate_api_key(key)
    if not user:
        return None, (jsonify({
            'error': 'Invalid or expired API key.',
            'signup': f'{_BASE_URL}/pricing.html',
        }), 401)

    if user.get('status') != 'active':
        return None, (jsonify({
            'error': 'Subscription inactive. Renew at promptolian.com/pricing.html',
        }), 403)

    return key, None


# ── Core session logic ────────────────────────────────────────────────────────

def _add_cache_control(tools: list) -> list:
    if not tools:
        return tools
    result = [dict(t) for t in tools]
    result[-1] = {**result[-1], 'cache_control': {'type': 'ephemeral'}}
    return result


def _token_estimate(tools: list) -> int:
    return len(tools) * 120


def _resolve_session(session_id: str, tools_in_request: Optional[list]) -> tuple[Optional[list], bool, int]:
    if tools_in_request:
        _save_session(session_id, tools_in_request)
        return _add_cache_control(tools_in_request), False, 0

    stored = _load_session(session_id)
    if stored is None:
        return None, False, 0

    cached_tools, last_call = stored
    if (time.time() - last_call) > _CACHE_TTL:
        _save_session(session_id, cached_tools)
        return _add_cache_control(cached_tools), False, 0

    _touch_session(session_id)
    orig         = _token_estimate(cached_tools)
    tokens_saved = orig - int(orig * 0.10)
    return _add_cache_control(cached_tools), True, tokens_saved


# ── Anthropic /v1/messages ────────────────────────────────────────────────────

def _compress_messages(messages: list[dict]) -> tuple[list[dict], int]:
    """Run context engine on messages. Returns (compressed, tokens_saved)."""
    if not _COMPRESS_HISTORY or not _CONTEXT_ENGINE_AVAILABLE or not messages:
        return messages, 0
    try:
        query  = next((m['content'] for m in reversed(messages) if m.get('role') == 'user'), '')
        orig   = sum(max(1, len(m.get('content', '').split()) * 4 // 3) for m in messages)
        engine = _ContextEngine(keep_last=4, budget_tokens=max(300, orig // 2),
                                summarize_threshold=6, summary_budget=120)
        result = engine.optimize(messages, query, mode='lossless', use_kv_geometry=True)
        compressed = result.get('optimized_prompt')
        if not compressed:
            return messages, 0
        # optimized_prompt is a string — wrap back into messages format
        comp_tokens = max(1, len(compressed.split()) * 4 // 3)
        saved = max(0, orig - comp_tokens)
        # Return as a single user-role context message prepended to the tail
        tail  = [m for m in messages if m.get('role') != 'system'][-4:]
        sys   = [m for m in messages if m.get('role') == 'system']
        ctx_msg = {'role': 'user', 'content': compressed}
        return sys + [ctx_msg] + tail, saved
    except Exception:
        return messages, 0


@app.route('/v1/messages', methods=['POST'])
def proxy_messages():
    promptolian_key, err = _check_auth()
    if err:
        return err

    api_key = (
        request.headers.get('X-Api-Key') or
        request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    )
    if not api_key:
        return jsonify({'error': 'Missing Anthropic API key (X-Api-Key header)'}), 401

    session_id = (
        request.headers.get('X-Session') or
        request.headers.get('X-Promptolian-Session') or
        str(uuid.uuid4())
    )

    body: dict = request.get_json(force=True) or {}
    tools_to_send, cache_hit, tokens_saved = _resolve_session(session_id, body.get('tools'))
    if tools_to_send is not None:
        body['tools'] = tools_to_send

    if cache_hit and promptolian_key:
        _record_savings(promptolian_key, tokens_saved)

    body['messages'], ctx_saved = _compress_messages(body.get('messages', []))

    pii_hits = _detect_sensitive_data(_extract_message_text(body))
    if pii_hits:
        _record_pii_event(session_id, promptolian_key, pii_hits)
    pii_risk = 'HIGH' if any(h['risk_level'] == 'HIGH' for h in pii_hits) else ('MEDIUM' if pii_hits else '')

    forward_headers = {
        'x-api-key':         api_key,
        'anthropic-version': request.headers.get('anthropic-version', '2023-06-01'),
        'content-type':      'application/json',
    }
    if request.headers.get('anthropic-beta'):
        forward_headers['anthropic-beta'] = request.headers['anthropic-beta']

    resp = _forward(ANTHROPIC_API, '/v1/messages', forward_headers, body)
    return _attach_headers(resp, session_id, cache_hit, tokens_saved, pii_risk, ctx_saved)


# ── OpenAI /v1/responses ──────────────────────────────────────────────────────

@app.route('/v1/responses', methods=['POST'])
def proxy_responses():
    promptolian_key, err = _check_auth()
    if err:
        return err

    api_key = (
        request.headers.get('Authorization', '').removeprefix('Bearer ').strip() or
        request.headers.get('X-Api-Key')
    )
    if not api_key:
        return jsonify({'error': 'Missing OpenAI API key (Authorization: Bearer <key>)'}), 401

    body: dict = request.get_json(force=True) or {}
    session_id = (
        request.headers.get('X-Session') or
        body.get('previous_response_id') or
        str(uuid.uuid4())
    )

    tools_to_send, cache_hit, tokens_saved = _resolve_session(session_id, body.get('tools'))
    if tools_to_send is not None:
        body['tools'] = tools_to_send

    if cache_hit and promptolian_key:
        _record_savings(promptolian_key, tokens_saved)

    body['messages'], ctx_saved = _compress_messages(body.get('messages', []))

    pii_hits = _detect_sensitive_data(_extract_message_text(body))
    if pii_hits:
        _record_pii_event(session_id, promptolian_key, pii_hits)
    pii_risk = 'HIGH' if any(h['risk_level'] == 'HIGH' for h in pii_hits) else ('MEDIUM' if pii_hits else '')

    forward_headers = {
        'Authorization': f'Bearer {api_key}',
        'content-type':  'application/json',
    }
    if request.headers.get('OpenAI-Organization'):
        forward_headers['OpenAI-Organization'] = request.headers['OpenAI-Organization']

    resp = _forward(OPENAI_API, '/v1/responses', forward_headers, body)
    return _attach_headers(resp, session_id, cache_hit, tokens_saved, pii_risk, ctx_saved)


# ── Generic forwarder ─────────────────────────────────────────────────────────

def _forward(base_url: str, path: str, headers: dict, body: dict) -> Response:
    is_stream = body.get('stream', False)
    with httpx.Client(timeout=120) as client:
        if is_stream:
            with client.stream('POST', f'{base_url}{path}', headers=headers, json=body) as r:
                def generate():
                    for chunk in r.iter_bytes():
                        yield chunk
                return Response(generate(), status=r.status_code,
                                content_type=r.headers.get('content-type', 'text/event-stream'))
        r = client.post(f'{base_url}{path}', headers=headers, json=body)
        return Response(r.content, status=r.status_code,
                        content_type=r.headers.get('content-type', 'application/json'))


def _attach_headers(resp: Response, session_id: str, cache_hit: bool, tokens_saved: int,
                    pii_risk: str = '', ctx_saved: int = 0) -> Response:
    resp.headers['X-Promptolian-Session']      = session_id
    resp.headers['X-Promptolian-Cache-Hit']    = 'true' if cache_hit else 'false'
    resp.headers['X-Promptolian-Tokens-Saved'] = str(tokens_saved)
    if cache_hit:
        resp.headers['X-Promptolian-Note'] = (
            f'Tools re-injected from session cache. '
            f'~{tokens_saved} tokens billed at 10% (prompt cache).'
        )
    if ctx_saved > 0:
        resp.headers['X-Promptolian-Context-Saved'] = str(ctx_saved)
    if pii_risk:
        resp.headers['X-Promptolian-Sensitive'] = pii_risk
    return resp


# ── Passthrough ───────────────────────────────────────────────────────────────

@app.route('/v1/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy_passthrough(path):
    api_key = (
        request.headers.get('X-Api-Key') or
        request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    )
    forward_headers = {
        'x-api-key':         api_key,
        'anthropic-version': request.headers.get('anthropic-version', '2023-06-01'),
        'content-type':      'application/json',
    }
    with httpx.Client(timeout=30) as client:
        r = client.request(request.method, f'{ANTHROPIC_API}/v1/{path}',
                           headers=forward_headers, content=request.get_data())
    return Response(r.content, status=r.status_code,
                    content_type=r.headers.get('content-type', 'application/json'))


# ── Session management ────────────────────────────────────────────────────────

@app.route('/proxy/sessions', methods=['GET'])
def list_sessions():
    try:
        conn = _get_conn()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute('SELECT session_id, tools_json, last_call FROM proxy_sessions')
                rows = cur.fetchall()
                cur.close()
                rows = [{'session_id': r[0], 'tools_json': r[1], 'last_call': float(r[2])} for r in rows]
            else:
                rows = [dict(r) for r in conn.execute('SELECT session_id, tools_json, last_call FROM proxy_sessions').fetchall()]
        finally:
            conn.close()
    except Exception:
        rows = []

    now = time.time()
    return jsonify({
        r['session_id']: {
            'tool_count':      len(json.loads(r['tools_json'])),
            'tool_names':      [t.get('name', '?') for t in json.loads(r['tools_json'])],
            'tokens_cached':   _token_estimate(json.loads(r['tools_json'])),
            'cache_warm':      (now - r['last_call']) < _CACHE_TTL,
            'last_call_ago_s': int(now - r['last_call']),
        }
        for r in rows
    })


@app.route('/proxy/sessions/<session_id>', methods=['DELETE'])
def clear_session(session_id):
    _delete_session(session_id)
    return jsonify({'deleted': session_id})


@app.route('/proxy/health', methods=['GET'])
def health():
    try:
        conn = _get_conn()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute('SELECT COUNT(*) FROM proxy_sessions')
                count = cur.fetchone()[0]
                cur.close()
            else:
                count = conn.execute('SELECT COUNT(*) FROM proxy_sessions').fetchone()[0]
        finally:
            conn.close()
    except Exception:
        count = 0

    return jsonify({
        'status':          'ok',
        'mode':            'cloud' if _MASTER_KEY else 'local',
        'storage':         'postgresql' if _is_pg() else 'sqlite',
        'sessions_cached': count,
        'db_path':         None if _is_pg() else str(_DB_PATH),
    })


# ── Billing (Stripe) ──────────────────────────────────────────────────────────

@app.route('/proxy/signup', methods=['POST'])
def proxy_signup():
    """Create a Stripe Checkout session.

    Body: {"email": "user@example.com", "plan": "solo"|"team"}
    Returns: {"url": "https://checkout.stripe.com/..."}
    """
    if not _STRIPE_KEY:
        return jsonify({'error': 'Payments not configured on this instance'}), 503
    try:
        import stripe
        stripe.api_key = _STRIPE_KEY
    except ImportError:
        return jsonify({'error': 'stripe not installed'}), 503

    data  = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()
    plan  = data.get('plan', 'solo').strip().lower()

    if not email:
        return jsonify({'error': 'email required'}), 400
    if plan not in ('solo', 'team'):
        return jsonify({'error': 'plan must be solo or team'}), 400

    price_id = _STRIPE_SOLO_MONTHLY if plan == 'solo' else _STRIPE_TEAM_MONTHLY
    if not price_id:
        return jsonify({'error': f'{plan} plan not configured'}), 503

    try:
        session = stripe.checkout.Session.create(
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            customer_email=email,
            success_url=f'{_BASE_URL}/dashboard.html?checkout=success',
            cancel_url=f'{_BASE_URL}/pricing.html?checkout=cancel',
            metadata={'product': 'proxy_cloud', 'plan': plan},
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/proxy/webhook', methods=['POST'])
def proxy_webhook():
    """Stripe webhook — provisions/deprovisions API keys."""
    if not _STRIPE_KEY or not _STRIPE_WEBHOOK:
        return jsonify({'error': 'not configured'}), 503
    try:
        import stripe
        stripe.api_key = _STRIPE_KEY
    except ImportError:
        return jsonify({'error': 'stripe not installed'}), 503

    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig, _STRIPE_WEBHOOK)
    except Exception:
        return jsonify({'error': 'invalid signature'}), 400

    etype = event['type']
    obj   = event['data']['object']

    def _get(o, key, default=''):
        try:
            v = o[key]
            return v if v is not None else default
        except (KeyError, TypeError):
            return default

    if etype == 'checkout.session.completed':
        email    = _get(obj, 'customer_email')
        sub_id   = _get(obj, 'subscription')
        metadata = _get(obj, 'metadata') or {}
        if email and _get(metadata, 'product') == 'proxy_cloud':
            plan = _get(metadata, 'plan') or 'solo'
            _provision_user(email, sub_id, plan)

    elif etype in ('customer.subscription.deleted', 'customer.subscription.updated'):
        status = _get(obj, 'status')
        if status in ('canceled', 'unpaid', 'past_due'):
            _deprovision_user(_get(obj, 'id'))

    return jsonify({'received': True})


def _provision_user(email: str, stripe_sub: str, plan: str = 'solo') -> None:
    """Create the default API key for a new subscriber and email it."""
    api_key = f'pk_{secrets.token_urlsafe(32)}'
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f'''
                    INSERT INTO proxy_users (api_key, email, plan, stripe_sub, status, project_name)
                    VALUES ({p},{p},{p},{p},'active','default')
                    ON CONFLICT (email, project_name) DO UPDATE SET
                        api_key    = EXCLUDED.api_key,
                        stripe_sub = EXCLUDED.stripe_sub,
                        plan       = EXCLUDED.plan,
                        status     = 'active'
                ''', (api_key, email, plan, stripe_sub))
                conn.commit()
                cur.close()
            else:
                conn.execute(f'''
                    INSERT INTO proxy_users (api_key, email, plan, stripe_sub, status, project_name)
                    VALUES ({p},{p},{p},{p},'active','default')
                    ON CONFLICT(email, project_name) DO UPDATE SET
                        api_key    = excluded.api_key,
                        stripe_sub = excluded.stripe_sub,
                        plan       = excluded.plan,
                        status     = 'active'
                ''', (api_key, email, plan, stripe_sub))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
    _send_key_email(email, api_key, plan)


def _deprovision_user(stripe_sub: str) -> None:
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(f"UPDATE proxy_users SET status='canceled' WHERE stripe_sub = {p}", (stripe_sub,))
                conn.commit()
                cur.close()
            else:
                conn.execute(f"UPDATE proxy_users SET status='canceled' WHERE stripe_sub = {p}", (stripe_sub,))
                conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _send_key_email(email: str, api_key: str, plan: str = 'solo') -> None:
    """Best-effort SMTP email with the new API key."""
    smtp_host = os.getenv('SMTP_HOST', '')
    smtp_user = os.getenv('SMTP_USER', '')
    smtp_pass = os.getenv('SMTP_PASS', '')
    if not smtp_host:
        print(f'[promptolian] New key for {email}: {api_key}')  # fallback log
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        body = f"""Welcome to Promptolian Cloud!

Your API key: {api_key}

Add it to your client:

    client = anthropic.Anthropic(
        base_url="https://proxy.promptolian.com",
        default_headers={{"X-Promptolian-Key": "{api_key}"}},
    )

View your savings dashboard at: {_BASE_URL}/dashboard.html

Questions? Reply to this email.

— Maurizio @ Promptolian
"""
        from_addr = 'support@promptolian.com'
        msg = MIMEText(body)
        msg['Subject'] = 'Your Promptolian API key'
        msg['From']    = f'Promptolian <{from_addr}>'
        msg['To']      = email
        with smtplib.SMTP_SSL(smtp_host, 465) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, [email], msg.as_string())
    except Exception as e:
        print(f'[promptolian] Email failed for {email}: {e}')


# ── Dashboard endpoint ────────────────────────────────────────────────────────

@app.route('/proxy/dashboard', methods=['GET'])
def proxy_dashboard():
    """Return savings stats for the authenticated user.

    Solo: single key stats.
    Team: aggregate + per-project breakdown.
    """
    key = request.headers.get('X-Promptolian-Key', '').strip()
    if _MASTER_KEY and not key:
        return jsonify({'error': 'X-Promptolian-Key required'}), 401

    user = _validate_api_key(key) if key else None
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401 if key else jsonify({'plan': 'local'})

    email = user['email']
    plan  = user['plan']

    # All keys for this account
    all_keys = _list_user_keys(email)
    total_tokens = sum(k['tokens_saved'] for k in all_keys)
    dollar_saved = round(total_tokens / 1_000_000 * 3, 2)

    # Active session count
    try:
        conn = _get_conn()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute('SELECT COUNT(*) FROM proxy_sessions')
                session_count = cur.fetchone()[0]
                cur.close()
            else:
                session_count = conn.execute('SELECT COUNT(*) FROM proxy_sessions').fetchone()[0]
        finally:
            conn.close()
    except Exception:
        session_count = 0

    projects = [
        {
            'project_name':  k['project_name'],
            'api_key_hint':  k['api_key'][:8] + '...',
            'status':        k['status'],
            'tokens_saved':  k['tokens_saved'],
            'dollar_saved':  round(k['tokens_saved'] / 1_000_000 * 3, 2),
        }
        for k in all_keys
    ]

    return jsonify({
        'email':           email,
        'plan':            plan,
        'status':          user['status'],
        'sessions_active': session_count,
        'tokens_saved':    total_tokens,
        'dollar_saved':    dollar_saved,
        'projects':        projects if plan == 'team' else [],
        'key_limit':       _PLAN_KEY_LIMITS.get(plan, 1),
        'keys_used':       len(all_keys),
    })


@app.route('/proxy/keys', methods=['GET'])
def list_keys():
    """List all API keys for the authenticated account (Team plan)."""
    key = request.headers.get('X-Promptolian-Key', '').strip()
    user = _validate_api_key(key) if key else None
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401

    keys = _list_user_keys(user['email'])
    return jsonify({'keys': [
        {'project_name': k['project_name'], 'api_key_hint': k['api_key'][:8] + '...',
         'status': k['status'], 'tokens_saved': k['tokens_saved']}
        for k in keys
    ]})


@app.route('/proxy/keys/new', methods=['POST'])
def create_project_key():
    """Create a new project API key (Team plan only).

    Body: {"project_name": "my-agent"}
    """
    key = request.headers.get('X-Promptolian-Key', '').strip()
    user = _validate_api_key(key) if key else None
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401
    if user['plan'] != 'team':
        return jsonify({'error': 'Team plan required to create multiple keys'}), 403
    if user['status'] != 'active':
        return jsonify({'error': 'Subscription inactive'}), 403

    data         = request.get_json(silent=True) or {}
    project_name = data.get('project_name', '').strip()
    if not project_name:
        return jsonify({'error': 'project_name required'}), 400

    email    = user['email']
    existing = _list_user_keys(email)
    limit    = _PLAN_KEY_LIMITS.get(user['plan'], 1)
    if len(existing) >= limit:
        return jsonify({'error': f'Key limit reached ({limit} for {user["plan"]} plan)'}), 403

    new_key = f'pk_{secrets.token_urlsafe(32)}'
    try:
        conn = _get_conn()
        p = _p()
        stripe_sub = user.get('stripe_sub') or (existing[0].get('stripe_sub') if existing else '')
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(
                    f"INSERT INTO proxy_users (api_key, email, plan, stripe_sub, status, project_name) VALUES ({p},{p},{p},{p},'active',{p})",
                    (new_key, email, user['plan'], stripe_sub, project_name),
                )
                conn.commit()
                cur.close()
            else:
                conn.execute(
                    f"INSERT INTO proxy_users (api_key, email, plan, stripe_sub, status, project_name) VALUES ({p},{p},{p},{p},'active',{p})",
                    (new_key, email, user['plan'], stripe_sub, project_name),
                )
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': f'Could not create key: {e}'}), 500

    return jsonify({'api_key': new_key, 'project_name': project_name})


@app.route('/proxy/keys/rotate', methods=['POST'])
def rotate_key():
    """Rotate the current API key — generates a new one, old one immediately invalid.

    Returns: {"api_key": "pk_...", "project_name": "..."}
    """
    key = request.headers.get('X-Promptolian-Key', '').strip()
    user = _validate_api_key(key) if key else None
    if not user:
        return jsonify({'error': 'Invalid API key'}), 401

    new_key = f'pk_{secrets.token_urlsafe(32)}'
    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE proxy_users SET api_key = {p} WHERE api_key = {p}",
                    (new_key, key),
                )
                conn.commit()
                cur.close()
            else:
                conn.execute(f"UPDATE proxy_users SET api_key = {p} WHERE api_key = {p}", (new_key, key))
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'api_key': new_key, 'project_name': user['project_name']})


# ── PII events ───────────────────────────────────────────────────────────────

@app.route('/proxy/pii-events', methods=['GET'])
def get_pii_events():
    """Return sensitive-data detection events for the authenticated account.

    Events are scoped to the caller's API key(s) only — never aggregated across accounts.
    Query params:
        limit  — max events to return (default 100, max 500)
    """
    key = request.headers.get('X-Promptolian-Key', '').strip()
    if _MASTER_KEY and not key:
        return jsonify({'error': 'X-Promptolian-Key required'}), 401

    user = _validate_api_key(key) if key else None
    if _MASTER_KEY and not user:
        return jsonify({'error': 'Invalid API key'}), 401

    limit = min(int(request.args.get('limit', 100)), 500)

    try:
        conn = _get_conn()
        p = _p()
        try:
            if _is_pg():
                cur = conn.cursor()
                if user:
                    all_keys = _list_user_keys(user['email'])
                    key_list  = [k['api_key'] for k in all_keys]
                    if not key_list:
                        return jsonify({'events': [], 'count': 0})
                    ph = ','.join([p] * len(key_list))
                    cur.execute(
                        f'SELECT session_id, api_key, timestamp, categories, risk_level FROM pii_events WHERE api_key IN ({ph}) ORDER BY timestamp DESC LIMIT {p}',
                        key_list + [limit],
                    )
                else:
                    cur.execute(
                        f'SELECT session_id, api_key, timestamp, categories, risk_level FROM pii_events ORDER BY timestamp DESC LIMIT {p}',
                        (limit,),
                    )
                rows   = cur.fetchall()
                cur.close()
                result = [
                    {'session_id': r[0],
                     'api_key_hint': (r[1][:8] + '...') if r[1] else None,
                     'timestamp': r[2], 'categories': json.loads(r[3]),
                     'risk_level': r[4]}
                    for r in rows
                ]
            else:
                if user:
                    all_keys = _list_user_keys(user['email'])
                    key_list  = [k['api_key'] for k in all_keys]
                    if not key_list:
                        return jsonify({'events': [], 'count': 0})
                    ph   = ','.join(['?'] * len(key_list))
                    rows = conn.execute(
                        f'SELECT session_id, api_key, timestamp, categories, risk_level FROM pii_events WHERE api_key IN ({ph}) ORDER BY timestamp DESC LIMIT ?',
                        key_list + [limit],
                    ).fetchall()
                else:
                    rows = conn.execute(
                        'SELECT session_id, api_key, timestamp, categories, risk_level FROM pii_events ORDER BY timestamp DESC LIMIT ?',
                        (limit,),
                    ).fetchall()
                result = [
                    {'session_id': r['session_id'],
                     'api_key_hint': (r['api_key'][:8] + '...') if r['api_key'] else None,
                     'timestamp': r['timestamp'], 'categories': json.loads(r['categories']),
                     'risk_level': r['risk_level']}
                    for r in rows
                ]
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({'events': result, 'count': len(result)})


# ── Main ──────────────────────────────────────────────────────────────────────

def main(port: int = 3002, host: str = '127.0.0.1', debug: bool = False,
         compress: bool = False) -> None:
    global _COMPRESS_HISTORY
    _COMPRESS_HISTORY = compress

    _ensure_schema()
    mode    = 'cloud' if _MASTER_KEY else 'local'
    storage = 'postgresql' if _is_pg() else f'sqlite ({_DB_PATH})'

    print()
    print('  Promptolian Proxy')
    print('  ─────────────────────────────────────────────────')
    print(f'  Mode      : {mode}')
    print(f'  Storage   : {storage}')
    print(f'  Listening : http://{host}:{port}')
    print(f'  Anthropic : {ANTHROPIC_API}')
    print(f'  OpenAI    : {OPENAI_API}')
    if compress:
        status = 'enabled' if _CONTEXT_ENGINE_AVAILABLE else 'UNAVAILABLE (context_engine not found)'
        print(f'  Context   : {status}')
    if mode == 'local':
        print()
        print('  In your code, change one line:')
        print(f'    client = anthropic.Anthropic(base_url="http://{host}:{port}")')
    else:
        print()
        print('  Cloud mode — X-Promptolian-Key required on all requests')
        print(f'  Signup    : {_BASE_URL}/pricing.html')
    print()
    print(f'  Health    : http://localhost:{port}/proxy/health')
    print(f'  Sessions  : http://localhost:{port}/proxy/sessions')
    print(f'  Dashboard : http://localhost:{port}/proxy/dashboard')
    print(f'  PII events: http://localhost:{port}/proxy/pii-events')
    print()
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=3002)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--debug', action='store_true')
    p.add_argument('--compress', action='store_true',
                   help='Enable context history compression (KV-sandwich)')
    args = p.parse_args()
    main(args.port, args.host, args.debug, args.compress)