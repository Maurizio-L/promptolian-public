"""
Promptly API — api.py
Runs at http://localhost:3001

SOLID architecture:
  CompressionRepository — DB persistence (SRP)
  RateLimiter           — rate-limit policy (SRP, OCP)
  CompressionService    — engine wiring + fallback (SRP, DIP)
  Routes                — thin HTTP layer only (SRP)

Three fully-deterministic compression tiers (no external API calls):
  standard   — symbol rules + grammar + lean pass (~9% avg CR)
  pro        — standard + math + verbose phrases + telegraphic (~10% avg CR)
  developer  — pro + domain packs + spaCy deep pruning (~11% avg CR, up to 20%)

Endpoints:
  GET  /health
  POST /compress          body: {"text":"...","tier":"standard|pro|developer","lang":"auto"}
  GET  /stats
  POST /feedback          body: {"original":"...","compressed":"...","rating":1-5}
  POST /optimize-context  body: {"messages":[...],"query":"...","summary":"","mode":"lossless"}
  POST /compress-tools    body: {"tools":[...], "session_id":"optional-string"}
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import re, math, json, sqlite3, os, sys
from pathlib import Path
from datetime import datetime
from typing import Optional

app = Flask(__name__)
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), '../../private/database/promptly.db')
_DATABASE_URL = os.getenv('DATABASE_URL')  # set by Railway; if absent, use SQLite

_ENGINE_PATH = str(Path(__file__).parent.parent.parent / 'private' / 'research' / 'code')
if _ENGINE_PATH not in sys.path:
    sys.path.insert(0, _ENGINE_PATH)


# ══════════════════════════════════════════════════════════════════════════════
# INFRASTRUCTURE — CompressionRepository  (SRP: owns all DB interactions)
# ══════════════════════════════════════════════════════════════════════════════

class CompressionRepository:
    """Single responsibility: persist and query compression events and feedback."""

    _INLINE_SCHEMA = """
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
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._pg_url  = _DATABASE_URL  # None locally, set on Railway

    def _connect(self):
        if self._pg_url:
            import psycopg2, psycopg2.extras
            conn = psycopg2.connect(self._pg_url)
            return conn
        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _is_pg(self) -> bool:
        return bool(self._pg_url)

    def _placeholder(self) -> str:
        return '%s' if self._is_pg() else '?'

    def init_schema(self, schema_file: Optional[Path] = None) -> None:
        if self._is_pg():
            self._init_pg_schema()
            return
        conn = self._connect()
        if schema_file and schema_file.exists():
            conn.executescript(schema_file.read_text())
        else:
            conn.executescript(self._INLINE_SCHEMA)
        conn.commit()
        conn.close()

    def _init_pg_schema(self) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS compression_events (
                id SERIAL PRIMARY KEY,
                user_id INTEGER, api_key TEXT,
                original_tokens INTEGER NOT NULL, compressed_tokens INTEGER NOT NULL,
                pct_saved INTEGER NOT NULL, mode TEXT DEFAULT 'standard',
                platform TEXT DEFAULT 'api',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_id INTEGER, original TEXT, compressed TEXT,
                rating INTEGER CHECK (rating BETWEEN 1 AND 5), comment TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                stripe_sub_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()

    def log_event(
        self,
        api_key: Optional[str],
        original_tokens: int,
        compressed_tokens: int,
        pct_saved: int,
        mode: str,
        platform: str,
    ) -> None:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f'INSERT INTO compression_events '
                f'(api_key, original_tokens, compressed_tokens, pct_saved, mode, platform) '
                f'VALUES ({p},{p},{p},{p},{p},{p})'
            )
            if self._is_pg():
                cur = conn.cursor()
                cur.execute(sql, (api_key, original_tokens, compressed_tokens, pct_saved, mode, platform))
                conn.commit(); cur.close()
            else:
                conn.execute(sql, (api_key, original_tokens, compressed_tokens, pct_saved, mode, platform))
                conn.commit()
            conn.close()
        except Exception:
            pass  # logging is non-critical; never crash the response

    def count_free_tier_usage(self, ip: str, mode: str, month_start: str) -> int:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f"SELECT COUNT(*) FROM compression_events WHERE api_key IS NULL "
                f"AND platform={p} AND mode!={p} AND created_at >= {p}"
            )
            if self._is_pg():
                cur = conn.cursor()
                cur.execute(sql, (f'ip:{ip}', 'standard', month_start))
                row = cur.fetchone(); cur.close()
            else:
                row = conn.execute(sql, (f'ip:{ip}', 'standard', month_start)).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0

    def get_stats(self) -> dict:
        conn = self._connect()
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
        return {
            'total_compressions':  row['total'] or 0,
            'total_tokens_saved':  row['total_saved'] or 0,
            'avg_compression_pct': row['avg_pct'] or 0,
            'by_tier': {r['mode']: {'count': r['n'], 'avg_pct': r['avg_pct']}
                        for r in by_tier},
        }

    def log_context_event(
        self,
        api_key: Optional[str],
        mode: str,
        original_tokens: int,
        optimized_tokens: int,
        tokens_saved: int,
        messages_total: int,
        messages_pruned: int,
        summary_tokens: int,
        platform: str,
    ) -> None:
        try:
            conn = self._connect()
            conn.execute(
                'INSERT INTO context_events '
                '(api_key, mode, original_tokens, optimized_tokens, tokens_saved, '
                'messages_total, messages_pruned, summary_tokens, platform) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (api_key, mode, original_tokens, optimized_tokens, tokens_saved,
                 messages_total, messages_pruned, summary_tokens, platform),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def log_mcp_event(
        self,
        api_key: Optional[str],
        tool_name: str,
        tier: Optional[str],
        tool_session_id: Optional[str],
        original_tokens: Optional[int],
        compressed_tokens: Optional[int],
        pct_saved: Optional[int],
        cache_hit: bool,
        claude_session_id: Optional[str],
    ) -> None:
        try:
            conn = self._connect()
            conn.execute(
                'INSERT INTO mcp_events '
                '(api_key, tool_name, tier, tool_session_id, original_tokens, '
                'compressed_tokens, pct_saved, cache_hit, claude_session_id) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (api_key, tool_name, tier, tool_session_id, original_tokens,
                 compressed_tokens, pct_saved, 1 if cache_hit else 0, claude_session_id),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def upsert_mcp_tool_session(
        self,
        session_id: str,
        api_key: Optional[str],
        tool_names: list,
        raw_tokens: int,
        dsl_tokens: int,
        tokens_saved: int,
        is_first_turn: bool,
    ) -> None:
        try:
            cr = round(1 - dsl_tokens / raw_tokens, 4) if raw_tokens else 0.0
            conn = self._connect()
            if is_first_turn:
                conn.execute(
                    'INSERT OR IGNORE INTO mcp_tool_sessions '
                    '(session_id, api_key, tool_names, tool_count, raw_tokens, dsl_tokens, '
                    'cr_turn1, turn_count, tokens_saved_total, cr_session_avg) '
                    'VALUES (?,?,?,?,?,?,?,1,?,?)',
                    (session_id, api_key, json.dumps(tool_names), len(tool_names),
                     raw_tokens, dsl_tokens, cr, tokens_saved, cr),
                )
            else:
                conn.execute(
                    'UPDATE mcp_tool_sessions SET '
                    'turn_count = turn_count + 1, '
                    'tokens_saved_total = tokens_saved_total + ?, '
                    'cr_session_avg = ROUND((tokens_saved_total + ?) * 1.0 / '
                    '    (raw_tokens * (turn_count + 1)), 4), '
                    'last_used_at = datetime("now") '
                    'WHERE session_id = ?',
                    (tokens_saved, tokens_saved, session_id),
                )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def activate_subscription(self, email: str, plan: str, stripe_sub_id: str) -> None:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f'INSERT INTO subscriptions (email, plan, stripe_sub_id, status, created_at) '
                f'VALUES ({p},{p},{p},{p},CURRENT_TIMESTAMP) '
                f'ON CONFLICT (email) DO UPDATE SET plan={p}, stripe_sub_id={p}, status={p}'
            )
            vals = (email, plan, stripe_sub_id, 'active', plan, stripe_sub_id, 'active')
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, vals); conn.commit(); cur.close()
            else:
                conn.execute(sql.replace('ON CONFLICT (email) DO UPDATE SET',
                    'ON CONFLICT(email) DO UPDATE SET'), vals)
                conn.commit()
            conn.close()
        except Exception:
            pass

    def deactivate_subscription(self, stripe_sub_id: str) -> None:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = f"UPDATE subscriptions SET status='canceled' WHERE stripe_sub_id={p}"
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, (stripe_sub_id,)); conn.commit(); cur.close()
            else:
                conn.execute(sql, (stripe_sub_id,)); conn.commit()
            conn.close()
        except Exception:
            pass

    def log_feedback(
        self,
        original: str,
        compressed: str,
        rating: int,
        comment: str,
    ) -> None:
        conn = self._connect()
        conn.execute(
            'INSERT INTO feedback (original, compressed, rating, comment) VALUES (?,?,?,?)',
            (original, compressed, rating, comment),
        )
        conn.commit()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# INFRASTRUCTURE — RateLimiter  (SRP: owns rate-limit policy; OCP: extend by
#                                subclassing, not editing _check)
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Monthly free-tier cap for unauthenticated Pro/Developer callers.
    Standard tier is rule-based (zero cost) and always exempt.
    Authenticated callers bypass this limiter entirely.
    """

    FREE_MONTHLY_LIMIT = 5_000

    def __init__(self, repository: CompressionRepository) -> None:
        self._repo = repository

    def check(
        self, api_key: Optional[str], ip: str, tier: str
    ) -> tuple[bool, int, int]:
        """Returns (allowed, used, limit)."""
        if api_key:
            return True, 0, 0          # authenticated — billing layer enforces plan limits
        if tier == 'standard':
            return True, 0, 0          # standard is free / unlimited

        month_start = datetime.now().strftime('%Y-%m-01')
        used = self._repo.count_free_tier_usage(ip, tier, month_start)
        allowed = used < self.FREE_MONTHLY_LIMIT
        return allowed, used, self.FREE_MONTHLY_LIMIT


# ══════════════════════════════════════════════════════════════════════════════
# SERVICE — CompressionService  (DIP: depends on engine interface, not import)
# ══════════════════════════════════════════════════════════════════════════════

_FALLBACK_RULES = [
    (r'you are an? expert (in |on |at )?', '§EXP '),
    (r'you are an? ',                       '§ROLE '),
    (r'please ',                            '§ACT '),
    (r'return only (the )?code[^.]*\.?',   '→code'),
    (r'return as (a )?bullet[- ]?list',    '→list'),
    (r'return as (a )?table',              '→table'),
    (r'return as json',                    '→json'),
    (r'step[- ]by[- ]step',               '→step'),
    (r'be (very )?concise',               '→short'),
    (r'\bsummarize\b',                     '∑'),
    (r'\bexplain\b',                       '?'),
    (r'\boptimize\b',                      'OPT'),
    (r'\bdebug\b',                         'BUG'),
    (r'\bfix (the |any |a )?bug(s)?\b',   'BUG'),
    (r'\bfunction\b',                      'FN'),
    (r'\bunit test(s)?\b',                 'TEST'),
    (r'\bdo not\b',                        '§NOT'),
    (r"\bdon't\b",                         '§NOT'),
    (r'\bavoid\b',                         '§NOT'),
    (r'\bcompare\b',                       '§DIFF'),
    (r'\bpython\b',                        'py'),
    (r'\bjavascript\b',                    'js'),
    (r'\btypescript\b',                    'ts'),
    (r'\bimportant\b',                     '!!'),
    (r'\breview\b',                        '«'),
    (r'\bimprove\b',                       '∆'),
    (r' +',                                ' '),
]


def _count_tokens_approx(text: str) -> int:
    words = re.split(r'[\s,.:;!?()\[\]{}"\']+', text.strip())
    return max(1, math.ceil(len([w for w in words if w]) * 1.3))


try:
    import tiktoken as _tiktoken
    _enc = _tiktoken.get_encoding('cl100k_base')
    def _count_tokens(text: str) -> int:
        return max(1, len(_enc.encode(text)))
except Exception:
    _count_tokens = _count_tokens_approx


class CompressionService:
    """
    Single responsibility: run compression and return a metrics dict.
    Depends on the engine abstraction (duck-typed); falls back to regex rules
    if the engine is unavailable — no crash, just a warning in the response.
    """

    VALID_TIERS = ('standard', 'pro', 'developer')

    def __init__(self, engine=None) -> None:
        self._engine = engine  # PromptolianEngine or None

    def compress(self, text: str, tier: str = 'standard', lang: str = 'auto') -> dict:
        tier = tier if tier in self.VALID_TIERS else 'standard'
        orig_tokens    = _count_tokens(text)
        fallback_reason: Optional[str] = None

        if self._engine is not None:
            try:
                from engine_v4 import detect_language  # type: ignore
                detected  = detect_language(text) if lang == 'auto' else lang
                result    = self._engine.compress(text, tier=tier, lang=detected)
                compressed = result.compressed
            except Exception as exc:
                compressed      = self._regex_fallback(text)
                detected        = 'en'
                fallback_reason = f'Engine error ({exc.__class__.__name__}) — used Standard fallback'
        else:
            compressed      = self._regex_fallback(text)
            detected        = 'en'
            fallback_reason = 'engine_v4 not available — used Standard fallback'

        comp_tokens = _count_tokens(compressed)
        saved       = max(0, orig_tokens - comp_tokens)
        pct         = round(saved / orig_tokens * 100) if orig_tokens > 0 else 0

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

    @staticmethod
    def _regex_fallback(text: str) -> str:
        out = text.strip()
        for pat, rep in _FALLBACK_RULES:
            out = re.sub(pat, rep, out, flags=re.IGNORECASE)
        return out.replace('. ', '.\n').replace('  ', ' ').strip()


# ══════════════════════════════════════════════════════════════════════════════
# WIRING — instantiate collaborators once at startup
# ══════════════════════════════════════════════════════════════════════════════

_repo        = CompressionRepository(DB_PATH)
_rate_limiter = RateLimiter(_repo)

try:
    from engine_v4 import make_engine  # type: ignore
    _engine_instance  = make_engine()
    _ENGINE_AVAILABLE = True
except Exception as _e:
    _engine_instance  = None
    _ENGINE_AVAILABLE = False

_svc = CompressionService(engine=_engine_instance)

# In-process session cache for /compress-tools  (keyed by session_id)
_TOOL_SESSION_CACHE: dict[str, set] = {}


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — thin HTTP layer; no business logic lives here
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/health')
def health():
    return jsonify({
        'status':           'ok',
        'service':          'Promptly API',
        'version':          '2.2.0',
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

    allowed, used, limit = _rate_limiter.check(api_key, ip, tier)
    if not allowed:
        return jsonify({
            'error':       'Monthly free-tier limit reached',
            'used':        used,
            'limit':       limit,
            'upgrade_url': 'https://promptolian.com/pricing',
        }), 429

    result = _svc.compress(text, tier=tier, lang=lang)

    _repo.log_event(
        api_key           = api_key,
        original_tokens   = result['original_tokens'],
        compressed_tokens = result['compressed_tokens'],
        pct_saved         = result['tokens_saved_pct'],
        mode              = tier,
        platform          = data.get('platform', 'api') if api_key else f'ip:{ip}',
    )

    response = jsonify(result)
    if not api_key:
        response.headers['X-RateLimit-Limit']     = str(limit)
        response.headers['X-RateLimit-Remaining'] = str(max(0, limit - used - 1))
    return response


@app.route('/stats')
def stats():
    try:
        return jsonify(_repo.get_stats())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/feedback', methods=['POST'])
def feedback():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'no data'}), 400
    try:
        _repo.log_feedback(
            original   = data.get('original', ''),
            compressed = data.get('compressed', ''),
            rating     = data.get('rating', 5),
            comment    = data.get('comment', ''),
        )
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/optimize-context', methods=['POST'])
def optimize_context():
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

    api_key = data.get('api_key') or request.headers.get('X-API-Key')
    platform = data.get('platform', 'api')

    try:
        from context_engine import ContextEngine  # type: ignore
        ce     = ContextEngine()
        result = ce.optimize(messages, query, summary=summary, mode=mode)

        summary_tokens = _count_tokens(result.get('new_summary', ''))
        _repo.log_context_event(
            api_key          = api_key,
            mode             = mode,
            original_tokens  = result.get('original_tokens', 0),
            optimized_tokens = result.get('optimized_tokens', 0),
            tokens_saved     = result.get('tokens_saved_estimate', 0),
            messages_total   = len(messages),
            messages_pruned  = result.get('messages_pruned', 0),
            summary_tokens   = summary_tokens,
            platform         = platform,
        )

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/compress-tools', methods=['POST'])
def compress_tools_route():
    """Compile JSON tool schemas to compact function-signature DSL.

    Request body:
      { "tools": [...], "session_id": "abc123" }

    Response:
      { "dsl": "...", "original_tokens": 1220, "compressed_tokens": 373,
        "cr": 0.694, "cached_count": 0, "new_tools": [...], "cached_tools": [] }
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
    api_key = data.get('api_key') or request.headers.get('X-API-Key')
    claude_session_id = request.headers.get('X-Claude-Session')

    try:
        from context_engine import compress_tools  # type: ignore

        is_first_turn = True
        seen: Optional[set] = None
        if session_id:
            is_first_turn = session_id not in _TOOL_SESSION_CACHE
            if is_first_turn:
                _TOOL_SESSION_CACHE[session_id] = set()
            seen = _TOOL_SESSION_CACHE[session_id]

        dsl, meta = compress_tools(tools, session_seen=seen)

        orig   = meta['original_tokens']
        comp   = meta['compressed_tokens']
        saved  = max(0, orig - comp)
        pct    = round(saved / orig * 100) if orig > 0 else 0
        cache_hit = meta['cached_count'] > 0

        _repo.log_mcp_event(
            api_key           = api_key,
            tool_name         = 'compress_tools',
            tier              = None,
            tool_session_id   = session_id,
            original_tokens   = orig,
            compressed_tokens = comp,
            pct_saved         = pct,
            cache_hit         = cache_hit,
            claude_session_id = claude_session_id,
        )

        if session_id:
            _repo.upsert_mcp_tool_session(
                session_id   = session_id,
                api_key      = api_key,
                tool_names   = [t.get('name', '') for t in tools if isinstance(t, dict)],
                raw_tokens   = orig,
                dsl_tokens   = comp,
                tokens_saved = saved,
                is_first_turn = is_first_turn,
            )

        return jsonify({
            'dsl':               dsl,
            'original_tokens':   orig,
            'compressed_tokens': comp,
            'cr':                meta['cr'],
            'cached_count':      meta['cached_count'],
            'new_tools':         meta['new_tools'],
            'cached_tools':      meta['cached_tools'],
            'registry':          meta['registry'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# STRIPE — checkout + webhook
# ══════════════════════════════════════════════════════════════════════════════

_STRIPE_KEY      = os.getenv('STRIPE_SECRET_KEY', '')
_STRIPE_WEBHOOK  = os.getenv('STRIPE_WEBHOOK_SECRET', '')
_STRIPE_PRICES   = {
    'pro_monthly':       os.getenv('STRIPE_PRO_MONTHLY', ''),
    'pro_annual':        os.getenv('STRIPE_PRO_ANNUAL', ''),
    'builder_monthly':   os.getenv('STRIPE_BUILDER_MONTHLY', ''),
    'builder_annual':    os.getenv('STRIPE_BUILDER_ANNUAL', ''),
}
_BASE_URL = os.getenv('BASE_URL', 'https://promptolian.com')


@app.route('/billing/checkout', methods=['POST'])
def billing_checkout():
    """Create a Stripe Checkout Session.

    Body: {"plan": "pro|builder", "billing": "monthly|annual", "email": "..."}
    Returns: {"url": "https://checkout.stripe.com/..."}
    """
    if not _STRIPE_KEY:
        return jsonify({'error': 'Payments not configured'}), 503
    try:
        import stripe
        stripe.api_key = _STRIPE_KEY
    except ImportError:
        return jsonify({'error': 'stripe package not installed'}), 503

    data   = request.get_json(silent=True) or {}
    plan   = data.get('plan', '')
    billing = data.get('billing', 'monthly')
    email  = data.get('email', '')

    price_key = f'{plan}_{billing}'
    price_id  = _STRIPE_PRICES.get(price_key, '')
    if not price_id:
        return jsonify({'error': f'Unknown plan/billing: {price_key}'}), 400

    try:
        params = {
            'mode': 'subscription',
            'line_items': [{'price': price_id, 'quantity': 1}],
            'success_url': f'{_BASE_URL}/?checkout=success',
            'cancel_url':  f'{_BASE_URL}/?checkout=cancel',
        }
        if email:
            params['customer_email'] = email
        session = stripe.checkout.Session.create(**params)
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/billing/webhook', methods=['POST'])
def billing_webhook():
    """Stripe webhook — activates/deactivates subscriptions."""
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

    if etype == 'checkout.session.completed':
        customer_email = obj.get('customer_email', '')
        plan = _resolve_plan_from_session(obj)
        _repo.activate_subscription(customer_email, plan, obj.get('subscription', ''))

    elif etype in ('customer.subscription.deleted', 'customer.subscription.updated'):
        sub    = obj
        status = sub.get('status', '')
        if status in ('canceled', 'unpaid', 'past_due'):
            _repo.deactivate_subscription(obj.get('id', ''))

    return jsonify({'received': True})


def _resolve_plan_from_session(session_obj: dict) -> str:
    """Map Stripe price ID back to plan name."""
    price_id = ''
    items = session_obj.get('display_items') or []
    if not items:
        return 'pro'
    price_id = items[0].get('price', {}).get('id', '') if isinstance(items[0], dict) else ''
    for key, pid in _STRIPE_PRICES.items():
        if pid == price_id:
            return key.split('_')[0]
    return 'pro'


if __name__ == '__main__':
    _repo.init_schema(
        Path(__file__).parent.parent.parent / 'tools' / 'reports' / 'schema_local.sql'
    )
    print('\n  Promptly API v2.2  (SOLID)')
    print('  ─────────────────────────────────────────')
    print(f'  engine_v4 : {"✓ loaded" if _ENGINE_AVAILABLE else "✗ not found (Standard only)"}')
    print(f'  tiers     : {"standard / pro / developer" if _ENGINE_AVAILABLE else "standard only"}')
    print('  http://localhost:3001/health')
    print('  POST http://localhost:3001/compress        {"text":"...","tier":"pro"}')
    print('  POST http://localhost:3001/compress-tools  {"tools":[...],"session_id":"optional"}')
    print('  POST http://localhost:3001/optimize-context {"messages":[...],"query":"..."}')
    print()
    port = int(os.getenv('PORT', 3001))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_DEBUG', '0') == '1')