"""
Promptly API — api.py
Runs at http://localhost:3001

Three fully-deterministic compression tiers (no external API calls):
  standard   — symbol rules + grammar + lean pass (~9% avg CR)
  pro        — standard + math + verbose phrases + telegraphic framing (~10% avg CR, up to 18% on long prompts)
  developer  — pro + domain packs + spaCy deep pruning (~11% avg CR, up to 20% on long prompts)

Endpoints:
  GET  /health
  POST /compress          body: {"text":"...","tier":"standard|pro|developer","lang":"auto"}
  GET  /stats
  POST /feedback          body: {"original":"...","compressed":"...","rating":1-5}
  POST /optimize-context  body: {"messages":[...],"query":"...","summary":"","mode":"lossless"}
  POST /compress-tools    body: {"tools":[...], "session_id":"optional-string"}
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import re, math, json, sqlite3, os, sys
from pathlib import Path
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), '../../private/database/promptly.db')

# ── Bootstrap engine_v4 (shared with research pipeline) ──────────────────────
_ENGINE_PATH = str(Path(__file__).parent.parent.parent / 'private' / 'research' / 'code')
if _ENGINE_PATH not in sys.path:
    sys.path.insert(0, _ENGINE_PATH)

try:
    from engine_v4 import PromptolianEngine, detect_language, count_tokens  # type: ignore
    _engine = PromptolianEngine()
    _ENGINE_AVAILABLE = True
except Exception as _e:
    _ENGINE_AVAILABLE = False
    _engine = None

# ── Fallback rule-based compression (Standard only, no engine_v4) ─────────────
_RULES = [
    (r'you are an? expert (in |on |at )?', '§EXP '),
    (r'you are an? ', '§ROLE '),
    (r'please ', '§ACT '),
    (r'return only (the )?code[^.]*\.?', '→code'),
    (r'return as (a )?bullet[- ]?list', '→list'),
    (r'return as (a )?table', '→table'),
    (r'return as json', '→json'),
    (r'step[- ]by[- ]step', '→step'),
    (r'pros and cons', '→pros/cons'),
    (r'be (very )?concise', '→short'),
    (r'\bsummarize\b', '∑'),
    (r'\bexplain\b', '?'),
    (r'\boptimize\b', 'OPT'),
    (r'\bdebug\b', 'BUG'),
    (r'\bfix (the |any |a )?bug(s)?\b', 'BUG'),
    (r'\bfunction\b', 'FN'),
    (r'\bunit test(s)?\b', 'TEST'),
    (r'\bdo not\b', '§NOT'),
    (r"\bdon't\b", '§NOT'),
    (r'\bavoid\b', '§NOT'),
    (r'\bcompare\b', '§DIFF'),
    (r'\bfor example\b', '§EX'),
    (r'\bpython\b', 'py'),
    (r'\bjavascript\b', 'js'),
    (r'\btypescript\b', 'ts'),
    (r'\bbriefly\b', '→short'),
    (r'\bdetailed\b', '→long'),
    (r'\bimportant\b', '!!'),
    (r'\breview\b', '«'),
    (r'\bimprove\b', '∆'),
    (r' +', ' '),
]

def _fallback_compress(text: str) -> str:
    out = text.strip()
    for pat, rep in _RULES:
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    return out.replace('. ', '.\n').replace('  ', ' ').strip()


def _count_tokens_approx(text: str) -> int:
    words = re.split(r'[\s,.:;!?()\[\]{}"\']+', text.strip())
    return max(1, math.ceil(len([w for w in words if w]) * 1.3))

try:
    import tiktoken as _tiktoken
    _enc = _tiktoken.get_encoding('cl100k_base')
    def count_tokens_exact(text: str) -> int:
        return max(1, len(_enc.encode(text)))
except Exception:
    count_tokens_exact = _count_tokens_approx


def _do_compress(text: str, tier: str = 'standard', lang: str = 'auto') -> dict:
    """Run compression at the requested tier. Returns dict with compressed text + metrics."""
    VALID_TIERS = ('standard', 'pro', 'developer')
    if tier not in VALID_TIERS:
        tier = 'standard'

    orig_tokens = count_tokens_exact(text)
    fallback_reason = None

    if _ENGINE_AVAILABLE:
        detected = detect_language(text) if lang == 'auto' else lang
        try:
            result = _engine.compress(text, tier=tier, lang=detected)
            compressed = result.compressed
        except Exception as exc:
            compressed = _fallback_compress(text)
            detected = 'en'
            fallback_reason = f'Engine error ({exc.__class__.__name__}) — used Standard fallback'
    else:
        compressed = _fallback_compress(text)
        detected = 'en'
        fallback_reason = 'engine_v4 not available — used Standard fallback'

    comp_tokens = count_tokens_exact(compressed)
    saved = max(0, orig_tokens - comp_tokens)
    pct = round(saved / orig_tokens * 100) if orig_tokens > 0 else 0

    out = {
        'compressed':        compressed,
        'tier':              tier,
        'original_tokens':   orig_tokens,
        'compressed_tokens': comp_tokens,
        'tokens_saved':      saved,
        'tokens_saved_pct':  pct,
    }
    if fallback_reason:
        out['warning'] = fallback_reason
    return out


# ── Free-tier rate limiting ───────────────────────────────────────────────────
FREE_MONTHLY_LIMIT = 5000   # Pro/Developer compressions/month for unauthenticated callers

def _check_rate_limit(api_key: str | None, ip: str, tier: str = 'standard') -> tuple[bool, int, int]:
    """Returns (allowed, used, limit). Standard tier is always free/unlimited."""
    if api_key:
        return True, 0, 0   # authenticated — plan limits enforced at billing layer
    if tier == 'standard':
        return True, 0, 0   # standard is rule-based, zero cost — no limit
    try:
        conn = get_db()
        month_start = datetime.now().strftime('%Y-%m-01')
        row = conn.execute(
            "SELECT COUNT(*) FROM compression_events WHERE api_key IS NULL "
            "AND platform=? AND mode!=? AND created_at >= ?",
            (f'ip:{ip}', 'standard', month_start)
        ).fetchone()
        conn.close()
        used = row[0] if row else 0
        return used < FREE_MONTHLY_LIMIT, used, FREE_MONTHLY_LIMIT
    except Exception:
        return True, 0, FREE_MONTHLY_LIMIT   # fail open


# ── DB setup ──────────────────────────────────────────────────────────────────
def get_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    import pathlib
    conn = get_db()
    schema_file = pathlib.Path(__file__).parent.parent.parent / 'tools' / 'reports' / 'schema_local.sql'
    if schema_file.exists():
        conn.executescript(schema_file.read_text())
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS compression_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, api_key TEXT,
                original_tokens INTEGER NOT NULL, compressed_tokens INTEGER NOT NULL,
                pct_saved INTEGER NOT NULL, mode TEXT DEFAULT 'standard',
                platform TEXT DEFAULT 'api',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, original TEXT, compressed TEXT,
                rating INTEGER CHECK (rating BETWEEN 1 AND 5), comment TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
    conn.commit()
    conn.close()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({
        'status':           'ok',
        'service':          'Promptly API',
        'version':          '2.1.0',
        'engine_v4':        _ENGINE_AVAILABLE,
        'tiers_available':  ['standard', 'pro', 'developer'] if _ENGINE_AVAILABLE else ['standard'],
        'endpoints':        ['/compress', '/compress-tools', '/optimize-context', '/stats', '/feedback'],
        'timestamp':        datetime.now().isoformat(),
    })


@app.route('/compress', methods=['POST'])
def compress_route():
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'error': 'text field required'}), 400

    text = data['text']
    if len(text) > 50_000:
        return jsonify({'error': 'text too long (max 50 000 chars)'}), 400

    tier    = data.get('tier', 'standard').lower()
    lang    = data.get('lang', 'auto')
    api_key = data.get('api_key') or request.headers.get('X-API-Key')
    ip      = request.remote_addr or 'unknown'

    # Rate limit free-tier callers (standard is always unlimited)
    allowed, used, limit = _check_rate_limit(api_key, ip, tier)
    if not allowed:
        return jsonify({
            'error':       'Monthly free-tier limit reached',
            'used':        used,
            'limit':       limit,
            'upgrade_url': 'https://promptolian.com/pricing',
        }), 429

    result = _do_compress(text, tier=tier, lang=lang)

    # Log to DB
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO compression_events '
            '(api_key, original_tokens, compressed_tokens, pct_saved, mode, platform) '
            'VALUES (?,?,?,?,?,?)',
            (
                api_key,
                result['original_tokens'],
                result['compressed_tokens'],
                result['tokens_saved_pct'],
                tier,
                data.get('platform', 'api') if api_key else f'ip:{ip}',
            )
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Rate-limit headers
    response = jsonify(result)
    if not api_key:
        response.headers['X-RateLimit-Limit']     = str(limit)
        response.headers['X-RateLimit-Remaining'] = str(max(0, limit - used - 1))
    return response


@app.route('/stats')
def stats():
    try:
        conn = get_db()
        row = conn.execute('''
            SELECT COUNT(*) as total,
                   SUM(original_tokens - compressed_tokens) as total_saved,
                   ROUND(AVG(pct_saved),1) as avg_pct
            FROM compression_events
        ''').fetchone()
        by_tier = conn.execute('''
            SELECT mode, COUNT(*) as n, ROUND(AVG(pct_saved),1) as avg_pct
            FROM compression_events GROUP BY mode
        ''').fetchall()
        conn.close()
        return jsonify({
            'total_compressions':  row['total'] or 0,
            'total_tokens_saved':  row['total_saved'] or 0,
            'avg_compression_pct': row['avg_pct'] or 0,
            'by_tier': {r['mode']: {'count': r['n'], 'avg_pct': r['avg_pct']}
                        for r in by_tier},
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/feedback', methods=['POST'])
def feedback():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'no data'}), 400
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO feedback (original, compressed, rating, comment) VALUES (?,?,?,?)',
            (data.get('original',''), data.get('compressed',''),
             data.get('rating', 5), data.get('comment',''))
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/optimize-context', methods=['POST'])
def optimize_context():
    """Context optimisation pipeline — see context_engine.py for full docs."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'request body required'}), 400

    messages = data.get('messages')
    query    = data.get('query', '').strip()
    summary  = data.get('summary', '')
    mode     = data.get('mode', 'lossless')

    if not isinstance(messages, list):
        return jsonify({'error': '"messages" must be a JSON array'}), 400
    if not query:
        return jsonify({'error': '"query" field is required'}), 400
    if mode not in ('lossless', 'aggressive'):
        return jsonify({'error': '"mode" must be "lossless" or "aggressive"'}), 400

    for i, m in enumerate(messages):
        if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
            return jsonify({'error': f'messages[{i}] must have "role" and "content"'}), 400
        if m['role'] not in ('user', 'assistant', 'system'):
            return jsonify({'error': f'messages[{i}].role must be user|assistant|system'}), 400

    try:
        from context_engine import ContextEngine  # type: ignore
        ce     = ContextEngine()
        result = ce.optimize(messages, query, summary=summary, mode=mode)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── In-process session cache for /compress-tools  (keyed by session_id) ──────
_TOOL_SESSION_CACHE: dict[str, set] = {}

@app.route('/compress-tools', methods=['POST'])
def compress_tools_route():
    """Compile JSON tool schemas to compact function-signature DSL.

    Request body:
      {
        "tools":      [...],          // required — list of tool schema dicts
                                      //   (OpenAI, Anthropic, or plain format)
        "session_id": "abc123"        // optional — enables session caching:
                                      //   turn 1 sends full DSL,
                                      //   subsequent turns send TOOLS:[names] only
      }

    Response:
      {
        "dsl":               "search_web(query, n=10)  # ...",
        "original_tokens":   1220,
        "compressed_tokens": 373,
        "cr":                0.694,
        "cached_count":      0,
        "new_tools":         ["search_web", ...],
        "cached_tools":      []
      }

    Compression rates (benchmarked on 10 realistic tools):
      - Turn 1  : ~69% CR (JSON → DSL, type elision, enum compaction)
      - Turn 2+ : ~97% CR (session cache — sends only TOOLS:[...] reference)
      - 5-turn  : ~92% average CR over a full session
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'request body required'}), 400

    tools = data.get('tools')
    if not isinstance(tools, list) or not tools:
        return jsonify({'error': '"tools" must be a non-empty JSON array'}), 400
    if len(tools) > 128:
        return jsonify({'error': 'maximum 128 tools per request'}), 400

    session_id = data.get('session_id', '').strip() or None

    try:
        from context_engine import compress_tools  # type: ignore

        # Resolve (or create) the session cache set
        seen: set | None = None
        if session_id:
            if session_id not in _TOOL_SESSION_CACHE:
                _TOOL_SESSION_CACHE[session_id] = set()
            seen = _TOOL_SESSION_CACHE[session_id]

        dsl, meta = compress_tools(tools, session_seen=seen)

        return jsonify({
            'dsl':               dsl,
            'original_tokens':   meta['original_tokens'],
            'compressed_tokens': meta['compressed_tokens'],
            'cr':                meta['cr'],
            'cached_count':      meta['cached_count'],
            'new_tools':         meta['new_tools'],
            'cached_tools':      meta['cached_tools'],
            'registry':          meta['registry'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    init_db()
    print('\n  Promptly API v2.1')
    print('  ─────────────────────────────────────────')
    print(f'  engine_v4 : {"✓ loaded" if _ENGINE_AVAILABLE else "✗ not found (Standard only)"}')
    print(f'  tiers     : {"standard / pro / developer" if _ENGINE_AVAILABLE else "standard only"}')
    print('  http://localhost:3001/health')
    print('  POST http://localhost:3001/compress        {"text":"...","tier":"pro"}')
    print('  POST http://localhost:3001/compress-tools  {"tools":[...],"session_id":"optional"}')
    print('  POST http://localhost:3001/optimize-context {"messages":[...],"query":"..."}')
    print()
    app.run(host='0.0.0.0', port=3001, debug=True)