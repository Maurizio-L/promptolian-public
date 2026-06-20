"""
Promptolian API — api.py
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
  POST /compress-context  body: {"messages":[...],"model":"...","summary":""} — proxy session reset (requires X-API-Key)
  POST /optimize-context  body: {"messages":[...],"query":"...","summary":"","mode":"lossless","use_kv_geometry":true}
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

DB_PATH = os.getenv('DB_PATH', os.path.join(os.path.dirname(__file__), '../../private/database/promptolian.db'))
_DATABASE_URL = os.getenv('DATABASE_URL')  # set by Railway; if absent, use SQLite

_LOCAL_API   = str(Path(__file__).parent)
_ENGINE_PATH = str(Path(__file__).parent.parent.parent / 'private' / 'research' / 'code')
for _p in (_LOCAL_API, _ENGINE_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ══════════════════════════════════════════════════════════════════════════════
# INFRASTRUCTURE — CompressionRepository  (SRP: owns all DB interactions)
# ══════════════════════════════════════════════════════════════════════════════

class CompressionRepository:
    """Single responsibility: persist and query compression events and feedback."""

    _INLINE_SCHEMA = """
        CREATE TABLE IF NOT EXISTS chat_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            user_message TEXT NOT NULL,
            bot_response TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
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
        CREATE TABLE IF NOT EXISTS website_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            user_id TEXT,
            page TEXT NOT NULL,
            event_type TEXT NOT NULL,
            element TEXT,
            duration_sec INTEGER,
            scroll_pct INTEGER,
            country TEXT,
            region TEXT,
            referrer TEXT,
            device_type TEXT,
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
            CREATE TABLE IF NOT EXISTS chat_events (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                user_message TEXT NOT NULL,
                bot_response TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
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
                api_key TEXT UNIQUE,
                expires_at TIMESTAMPTZ,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS api_key TEXT UNIQUE")
        cur.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS website_events (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT,
                page TEXT NOT NULL,
                event_type TEXT NOT NULL,
                element TEXT,
                duration_sec INTEGER,
                scroll_pct INTEGER,
                country TEXT,
                region TEXT,
                referrer TEXT,
                device_type TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

    def log_website_event(
        self,
        session_id: str,
        page: str,
        event_type: str,
        user_id: Optional[str] = None,
        element: Optional[str] = None,
        duration_sec: Optional[int] = None,
        scroll_pct: Optional[int] = None,
        country: Optional[str] = None,
        region: Optional[str] = None,
        referrer: Optional[str] = None,
        device_type: Optional[str] = None,
    ) -> None:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f'INSERT INTO website_events '
                f'(session_id, user_id, page, event_type, element, duration_sec, scroll_pct, '
                f'country, region, referrer, device_type) '
                f'VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})'
            )
            vals = (session_id, user_id, page, event_type, element, duration_sec, scroll_pct,
                    country, region, referrer, device_type)
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, vals); conn.commit(); cur.close()
            else:
                conn.execute(sql, vals); conn.commit()
            conn.close()
        except Exception:
            pass

    def get_website_stats(self, days: int = 30) -> dict:
        conn = self._connect()
        try:
            if self._is_pg():
                cur = conn.cursor()
                interval = 'INTERVAL \'1 day\' * %s'
                cur.execute(f'SELECT COUNT(*) FROM website_events WHERE event_type=%s AND created_at > NOW() - {interval}', ('pageview', days))
                total_pageviews = cur.fetchone()[0] or 0
                cur.execute(f'SELECT COUNT(DISTINCT session_id) FROM website_events WHERE created_at > NOW() - {interval}', (days,))
                unique_sessions = cur.fetchone()[0] or 0
                cur.execute(f'SELECT page, COUNT(*) AS n FROM website_events WHERE event_type=%s AND created_at > NOW() - {interval} GROUP BY page ORDER BY n DESC LIMIT 10', ('pageview', days))
                top_pages = [{'page': r[0], 'views': r[1]} for r in cur.fetchall()]
                cur.execute(f'SELECT country, COUNT(*) AS n FROM website_events WHERE event_type=%s AND country IS NOT NULL AND created_at > NOW() - {interval} GROUP BY country ORDER BY n DESC LIMIT 20', ('pageview', days))
                top_countries = [{'country': r[0], 'views': r[1]} for r in cur.fetchall()]
                cur.execute(f'SELECT device_type, COUNT(*) AS n FROM website_events WHERE event_type=%s AND device_type IS NOT NULL AND created_at > NOW() - {interval} GROUP BY device_type', ('pageview', days))
                devices = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(f'SELECT page, ROUND(AVG(duration_sec)::numeric,1) FROM website_events WHERE event_type=%s AND created_at > NOW() - {interval} GROUP BY page ORDER BY 2 DESC LIMIT 10', ('time_on_page', days))
                avg_time = [{'page': r[0], 'avg_sec': float(r[1] or 0)} for r in cur.fetchall()]
                cur.execute(f'SELECT scroll_pct, COUNT(*) FROM website_events WHERE event_type=%s AND scroll_pct IS NOT NULL AND created_at > NOW() - {interval} GROUP BY scroll_pct ORDER BY scroll_pct', ('scroll_depth', days))
                scroll = {str(r[0]): r[1] for r in cur.fetchall()}
                cur.close()
            else:
                d = f'-{days} days'
                total_pageviews = conn.execute("SELECT COUNT(*) FROM website_events WHERE event_type='pageview' AND created_at > datetime('now',?)", (d,)).fetchone()[0] or 0
                unique_sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM website_events WHERE created_at > datetime('now',?)", (d,)).fetchone()[0] or 0
                top_pages = [{'page': r[0], 'views': r[1]} for r in conn.execute("SELECT page, COUNT(*) AS n FROM website_events WHERE event_type='pageview' AND created_at > datetime('now',?) GROUP BY page ORDER BY n DESC LIMIT 10", (d,)).fetchall()]
                top_countries = [{'country': r[0], 'views': r[1]} for r in conn.execute("SELECT country, COUNT(*) AS n FROM website_events WHERE event_type='pageview' AND country IS NOT NULL AND created_at > datetime('now',?) GROUP BY country ORDER BY n DESC LIMIT 20", (d,)).fetchall()]
                devices = {r[0]: r[1] for r in conn.execute("SELECT device_type, COUNT(*) AS n FROM website_events WHERE event_type='pageview' AND device_type IS NOT NULL AND created_at > datetime('now',?) GROUP BY device_type", (d,)).fetchall()}
                avg_time = [{'page': r[0], 'avg_sec': float(r[1] or 0)} for r in conn.execute("SELECT page, ROUND(AVG(duration_sec),1) FROM website_events WHERE event_type='time_on_page' AND created_at > datetime('now',?) GROUP BY page ORDER BY 2 DESC LIMIT 10", (d,)).fetchall()]
                scroll = {str(r[0]): r[1] for r in conn.execute("SELECT scroll_pct, COUNT(*) FROM website_events WHERE event_type='scroll_depth' AND scroll_pct IS NOT NULL AND created_at > datetime('now',?) GROUP BY scroll_pct ORDER BY scroll_pct", (d,)).fetchall()}
            return {
                'period_days':      days,
                'total_pageviews':  total_pageviews,
                'unique_sessions':  unique_sessions,
                'top_pages':        top_pages,
                'top_countries':    top_countries,
                'devices':          devices,
                'avg_time_on_page': avg_time,
                'scroll_depth':     scroll,
            }
        finally:
            conn.close()

    def get_stats(self) -> dict:
        conn = self._connect()
        try:
            if self._is_pg():
                cur = conn.cursor()
                cur.execute('''
                    SELECT COUNT(*) AS total,
                           SUM(original_tokens - compressed_tokens) AS total_saved,
                           ROUND(AVG(pct_saved)::numeric, 1) AS avg_pct
                    FROM compression_events
                ''')
                row = cur.fetchone()
                cur.execute('''
                    SELECT mode, COUNT(*) AS n, ROUND(AVG(pct_saved)::numeric, 1) AS avg_pct
                    FROM compression_events GROUP BY mode
                ''')
                by_tier = cur.fetchall()
                cur.close()
                return {
                    'total_compressions':  row[0] or 0,
                    'total_tokens_saved':  row[1] or 0,
                    'avg_compression_pct': float(row[2] or 0),
                    'by_tier': {r[0]: {'count': r[1], 'avg_pct': float(r[2] or 0)}
                                for r in by_tier},
                }
            else:
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
                return {
                    'total_compressions':  row['total'] or 0,
                    'total_tokens_saved':  row['total_saved'] or 0,
                    'avg_compression_pct': row['avg_pct'] or 0,
                    'by_tier': {r['mode']: {'count': r['n'], 'avg_pct': r['avg_pct']}
                                for r in by_tier},
                }
        finally:
            conn.close()

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
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f'INSERT INTO context_events '
                f'(api_key, mode, original_tokens, optimized_tokens, tokens_saved, '
                f'messages_total, messages_pruned, summary_tokens, platform) '
                f'VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})'
            )
            vals = (api_key, mode, original_tokens, optimized_tokens, tokens_saved,
                    messages_total, messages_pruned, summary_tokens, platform)
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, vals); conn.commit(); cur.close()
            else:
                conn.execute(sql, vals); conn.commit()
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
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f'INSERT INTO mcp_events '
                f'(api_key, tool_name, tier, tool_session_id, original_tokens, '
                f'compressed_tokens, pct_saved, cache_hit, claude_session_id) '
                f'VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})'
            )
            vals = (api_key, tool_name, tier, tool_session_id, original_tokens,
                    compressed_tokens, pct_saved, 1 if cache_hit else 0, claude_session_id)
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, vals); conn.commit(); cur.close()
            else:
                conn.execute(sql, vals); conn.commit()
            conn.close()
        except Exception:
            pass

    def get_mcp_tool_session_names(self, session_id: str) -> Optional[set]:
        """Return the set of tool names seen in a previous session, or None if not found."""
        try:
            conn = self._connect()
            if self._is_pg():
                cur = conn.cursor()
                cur.execute('SELECT tool_names FROM mcp_tool_sessions WHERE session_id = %s', (session_id,))
                row = cur.fetchone(); cur.close()
            else:
                row = conn.execute(
                    'SELECT tool_names FROM mcp_tool_sessions WHERE session_id = ?', (session_id,)
                ).fetchone()
            conn.close()
            if row is None:
                return None
            return set(json.loads(row[0] if self._is_pg() else row['tool_names']))
        except Exception:
            return None

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
            p = self._placeholder()
            conn = self._connect()
            if self._is_pg():
                cur = conn.cursor()
                if is_first_turn:
                    cur.execute(
                        f'INSERT INTO mcp_tool_sessions '
                        f'(session_id, api_key, tool_names, tool_count, raw_tokens, dsl_tokens, '
                        f'cr_turn1, turn_count, tokens_saved_total, cr_session_avg) '
                        f'VALUES ({p},{p},{p},{p},{p},{p},{p},1,{p},{p}) ON CONFLICT (session_id) DO NOTHING',
                        (session_id, api_key, json.dumps(tool_names), len(tool_names),
                         raw_tokens, dsl_tokens, cr, tokens_saved, cr),
                    )
                else:
                    cur.execute(
                        f'UPDATE mcp_tool_sessions SET '
                        f'turn_count = turn_count + 1, '
                        f'tokens_saved_total = tokens_saved_total + {p}, '
                        f'cr_session_avg = ROUND((tokens_saved_total + {p}) * 1.0 / '
                        f'    (raw_tokens * (turn_count + 1)), 4), '
                        f'last_used_at = NOW() '
                        f'WHERE session_id = {p}',
                        (tokens_saved, tokens_saved, session_id),
                    )
                conn.commit(); cur.close()
            else:
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

    def activate_subscription(self, email: str, plan: str, stripe_sub_id: str,
                              api_key: Optional[str] = None,
                              expires_at: Optional[str] = None) -> None:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = (
                f'INSERT INTO subscriptions (email, plan, stripe_sub_id, api_key, expires_at, status, created_at) '
                f'VALUES ({p},{p},{p},{p},{p},{p},CURRENT_TIMESTAMP) '
                f'ON CONFLICT (email) DO UPDATE SET plan={p}, stripe_sub_id={p}, '
                f'api_key=COALESCE({p}, subscriptions.api_key), expires_at={p}, status={p}'
            )
            vals = (email, plan, stripe_sub_id, api_key, expires_at, 'active',
                    plan, stripe_sub_id, api_key, expires_at, 'active')
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, vals); conn.commit(); cur.close()
            else:
                conn.execute(sql.replace('ON CONFLICT (email) DO UPDATE SET',
                    'ON CONFLICT(email) DO UPDATE SET'), vals)
                conn.commit()
            conn.close()
        except Exception:
            pass

    def get_subscription_by_key(self, api_key: str) -> Optional[dict]:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = f"SELECT email, plan, status, expires_at FROM subscriptions WHERE api_key={p}"
            if self._is_pg():
                cur = conn.cursor()
                cur.execute(sql, (api_key,))
                row = cur.fetchone()
                cur.close(); conn.close()
                if not row:
                    return None
                return {'email': row[0], 'plan': row[1], 'status': row[2], 'expires_at': row[3]}
            else:
                row = conn.execute(sql, (api_key,)).fetchone()
                conn.close()
                return dict(row) if row else None
        except Exception:
            return None

    def count_key_usage_month(self, api_key: str) -> int:
        try:
            p = self._placeholder()
            conn = self._connect()
            month_start = datetime.now().strftime('%Y-%m-01')
            sql = f"SELECT COUNT(*) FROM context_events WHERE api_key={p} AND created_at >= {p}"
            if self._is_pg():
                cur = conn.cursor()
                cur.execute(sql, (api_key, month_start))
                count = cur.fetchone()[0] or 0
                cur.close(); conn.close()
                return int(count)
            else:
                count = conn.execute(sql, (api_key, month_start)).fetchone()[0] or 0
                conn.close()
                return int(count)
        except Exception:
            return 0

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
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = f'INSERT INTO feedback (original, compressed, rating, comment) VALUES ({p},{p},{p},{p})'
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, (original, compressed, rating, comment)); conn.commit(); cur.close()
            else:
                conn.execute(sql, (original, compressed, rating, comment)); conn.commit()
            conn.close()
        except Exception:
            pass

    def log_chat(self, session_id: str, user_message: str, bot_response: str) -> None:
        try:
            p = self._placeholder()
            conn = self._connect()
            sql = f'INSERT INTO chat_events (session_id, user_message, bot_response) VALUES ({p},{p},{p})'
            if self._is_pg():
                cur = conn.cursor(); cur.execute(sql, (session_id, user_message, bot_response)); conn.commit(); cur.close()
            else:
                conn.execute(sql, (session_id, user_message, bot_response)); conn.commit()
            conn.close()
        except Exception:
            pass

    def count_chat_in_window(self, session_id: str, window_minutes: int = 60) -> int:
        try:
            p = self._placeholder()
            conn = self._connect()
            if self._is_pg():
                sql = f"SELECT COUNT(*) FROM chat_events WHERE session_id={p} AND created_at > NOW() - INTERVAL '{window_minutes} minutes'"
                cur = conn.cursor(); cur.execute(sql, (session_id,)); row = cur.fetchone(); cur.close()
            else:
                sql = f"SELECT COUNT(*) FROM chat_events WHERE session_id={p} AND created_at > datetime('now', {p})"
                row = conn.execute(sql, (session_id, f'-{window_minutes} minutes')).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0


# ══════════════════════════════════════════════════════════════════════════════
# INFRASTRUCTURE — RateLimiter  (SRP: owns rate-limit policy; OCP: extend by
#                                subclassing, not editing _check)
# ══════════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Monthly free-tier cap for unauthenticated Pro/Developer callers.
    Standard tier is rule-based (zero cost) and always exempt.
    Authenticated callers are checked against per-plan monthly limits.
    """

    FREE_MONTHLY_LIMIT = 5_000

    PLAN_LIMITS: dict = {
        'free':  0,
        'solo':  1_000,
        'team':  10_000,
        'gift':  1_000,
    }

    def __init__(self, repository: CompressionRepository) -> None:
        self._repo = repository

    def check(
        self, api_key: Optional[str], ip: str, tier: str
    ) -> tuple[bool, int, int]:
        """Returns (allowed, used, limit)."""
        if api_key:
            sub = self._repo.get_subscription_by_key(api_key)
            if sub and sub['status'] == 'active':
                return True, 0, 0
            return False, 0, 0         # key not found or inactive
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

try:
    import context_engine as _ce_module  # type: ignore
    _ce_module.ContextEngine()  # verify it initialises
    _CONTEXT_ENGINE_AVAILABLE = True
except Exception:
    _CONTEXT_ENGINE_AVAILABLE = False

_svc = CompressionService(engine=_engine_instance)

# In-process session cache for /compress-tools  (keyed by session_id)
_TOOL_SESSION_CACHE: dict[str, set] = {}


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — thin HTTP layer; no business logic lives here
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/health')
def health():
    return jsonify({
        'status':             'ok',
        'service':            'Promptolian API',
        'version':            '2.3.5',
        'context_engine':     _CONTEXT_ENGINE_AVAILABLE,
        'engine_v4':          _ENGINE_AVAILABLE,
        'tiers_available':    ['standard', 'pro', 'developer'] if _ENGINE_AVAILABLE else ['standard'],
        'kv_sandwich':        _CONTEXT_ENGINE_AVAILABLE,
        'endpoints':          ['/compress', '/compress-context', '/compress-tools', '/optimize-context', '/stats', '/feedback', '/website-event', '/website-stats', '/visit-count', '/admin/subscription'],
        'smtp_configured':    bool(_SMTP_HOST and _SMTP_USER and _SMTP_PASS),
        'timestamp':          datetime.now().isoformat(),
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


_MASTER_KEY  = os.getenv('PROMPTOLIAN_MASTER_KEY', '')
_SMTP_HOST   = os.getenv('SMTP_HOST', '')
_SMTP_PORT   = int(os.getenv('SMTP_PORT', '587'))
_SMTP_USER   = os.getenv('SMTP_USER', '')
_SMTP_PASS   = os.getenv('SMTP_PASS', '')
_SMTP_FROM   = os.getenv('SMTP_FROM', _SMTP_USER)


def _send_key_email(to_email: str, api_key: str, plan: str, expires_at: Optional[str] = None) -> bool:
    if not (_SMTP_HOST and _SMTP_USER and _SMTP_PASS):
        return False
    import smtplib
    from email.mime.text import MIMEText
    expiry_note = ''
    if expires_at:
        try:
            exp_date = expires_at[:10]
            expiry_note = f'\nThis key is valid until {exp_date}.\n'
        except Exception:
            pass
    body = f"""Hi,

You've been given a Promptolian API key ({plan} plan) — a gift from a friend.
{expiry_note}

Your API key:

  {api_key}

To use it, add it to your requests:

  curl https://api.promptolian.com/compress \\
    -H "X-API-Key: {api_key}" \\
    -H "Content-Type: application/json" \\
    -d '{{"text": "your text here", "tier": "standard"}}'

Or set it once in your environment:

  export PROMPTOLIAN_API_KEY={api_key}

Docs: https://promptolian.com/docs.html

Enjoy — and let us know how it goes.
The Promptolian team
"""
    msg = MIMEText(body)
    msg['Subject'] = 'Your Promptolian API key'
    msg['From']    = _SMTP_FROM
    msg['To']      = to_email
    try:
        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as s:
            s.starttls()
            s.login(_SMTP_USER, _SMTP_PASS)
            s.sendmail(_SMTP_FROM, [to_email], msg.as_string())
        return True
    except Exception:
        return False


@app.route('/website-event', methods=['POST'])
def website_event():
    if request.headers.get('DNT') == '1':
        return '', 204

    # force=True: accept text/plain sent by tracker to avoid CORS preflight
    data = request.get_json(force=True, silent=True) or {}
    session_id = str(data.get('session_id', '')).strip()[:64]
    page       = str(data.get('page', '')).strip()[:200]
    event_type = str(data.get('event_type', '')).strip()[:32]
    if not session_id or not page or not event_type:
        return jsonify({'error': 'session_id, page, event_type required'}), 400
    if event_type not in ('pageview', 'click', 'scroll_depth', 'time_on_page'):
        return jsonify({'error': 'invalid event_type'}), 400

    user_id      = str(data.get('user_id', ''))[:64]  or None
    element      = str(data.get('element', ''))[:64]  or None
    duration_sec = int(data['duration_sec']) if isinstance(data.get('duration_sec'), (int, float)) else None
    scroll_pct   = int(data['scroll_pct'])   if isinstance(data.get('scroll_pct'),   (int, float)) else None
    referrer     = str(data.get('referrer', 'direct'))[:200]

    ua = request.headers.get('User-Agent', '').lower()
    if 'tablet' in ua or 'ipad' in ua:
        device_type = 'tablet'
    elif 'mobile' in ua or 'android' in ua or 'iphone' in ua:
        device_type = 'mobile'
    else:
        device_type = 'desktop'

    country, region = None, None
    raw_ip = (request.headers.get('X-Forwarded-For', '') or request.remote_addr or '').split(',')[0].strip()
    if raw_ip and raw_ip not in ('127.0.0.1', '::1', ''):
        try:
            import httpx
            geo = httpx.get(
                f'http://ip-api.com/json/{raw_ip}?fields=status,countryCode,regionName',
                timeout=2.0,
            ).json()
            if geo.get('status') == 'success':
                country = geo.get('countryCode')
                region  = geo.get('regionName')
        except Exception:
            pass

    _repo.log_website_event(
        session_id=session_id, page=page, event_type=event_type,
        user_id=user_id, element=element, duration_sec=duration_sec,
        scroll_pct=scroll_pct, country=country, region=region,
        referrer=referrer, device_type=device_type,
    )
    return '', 204


@app.route('/website-stats')
def website_stats():
    if _MASTER_KEY and request.headers.get('X-Master-Key') != _MASTER_KEY:
        return jsonify({'error': 'unauthorized'}), 401
    try:
        days = max(1, min(365, int(request.args.get('days', 30))))
        return jsonify(_repo.get_website_stats(days))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/subscription', methods=['POST'])
def admin_subscription():
    if not _MASTER_KEY or request.headers.get('X-Master-Key') != _MASTER_KEY:
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(force=True, silent=True) or {}
    email    = (data.get('email')    or '').strip().lower()
    plan     = (data.get('plan')     or 'gift').strip()
    duration = (data.get('duration') or '').strip()   # 1m, 3m, 1y — only for gift
    if not email:
        return jsonify({'error': 'email required'}), 400
    if plan not in ('free', 'solo', 'team', 'gift'):
        return jsonify({'error': 'plan must be free, solo, team, or gift'}), 400

    expires_at = None
    if plan == 'gift' or duration:
        from datetime import timezone, timedelta
        _dur_map = {'1m': 31, '3m': 92, '1y': 365}
        days = _dur_map.get(duration, 31)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    import secrets
    sub_id  = data.get('stripe_sub_id') or f'manual_{secrets.token_hex(8)}'
    api_key = f'ptl_{secrets.token_urlsafe(24)}'
    _repo.activate_subscription(email, plan, sub_id, api_key, expires_at)
    emailed = _send_key_email(email, api_key, plan, expires_at)
    return jsonify({'ok': True, 'email': email, 'plan': plan, 'api_key': api_key,
                    'expires_at': expires_at, 'emailed': emailed})


@app.route('/visit-count')
def visit_count():
    try:
        conn = _repo._connect()
        if _repo._is_pg():
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM website_events WHERE event_type='pageview'")
            count = cur.fetchone()[0] or 0
            cur.close()
        else:
            count = conn.execute("SELECT COUNT(*) FROM website_events WHERE event_type='pageview'").fetchone()[0] or 0
        conn.close()
        resp = jsonify({'count': int(count)})
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception:
        return jsonify({'count': 0})


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

    messages        = data.get('messages')
    query           = data.get('query', '').strip()
    summary         = data.get('summary', '')
    mode            = data.get('mode', 'lossless')
    use_kv_geometry = bool(data.get('use_kv_geometry', True))
    kv_prefix       = int(data.get('kv_prefix', 2))
    kv_tail         = int(data.get('kv_tail', 4))

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
        result = ce.optimize(
            messages, query,
            summary=summary, mode=mode,
            use_kv_geometry=use_kv_geometry,
            kv_prefix=kv_prefix,
            kv_tail=kv_tail,
        )

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


@app.route('/compress-context', methods=['POST'])
def compress_context():
    """KV-sandwich compression for proxy session reset.

    Called by the Promptolian proxy (pip install users) when
    PROMPTOLIAN_API_KEY is set and context_engine is not available locally.

    Request:
      X-API-Key: <key>          (required — 401 if missing)
      {
        "messages": [...],      (required)
        "model":    "claude-...",
        "summary":  ""          (optional — previous session summary)
      }

    Response 200:
      { "optimized_prompt": "...", "new_summary": "..." }

    Errors:
      401  missing or invalid API key
      400  bad request body
      500  compression failed
    """
    api_key = request.headers.get('X-API-Key', '').strip()
    if not api_key:
        return jsonify({'error': 'API key required. Get one at promptolian.com/pricing'}), 401

    sub = _repo.get_subscription_by_key(api_key)
    if not sub or sub['status'] != 'active':
        return jsonify({'error': 'Invalid or inactive API key'}), 401

    # Check expiry (gift subs)
    if sub.get('expires_at'):
        from datetime import timezone
        exp = sub['expires_at']
        if hasattr(exp, 'tzinfo'):
            now = datetime.now(timezone.utc)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now > exp:
                return jsonify({'error': 'Subscription expired. Renew at promptolian.com/pricing'}), 402
        else:
            from datetime import timezone
            if datetime.now() > datetime.fromisoformat(str(exp)):
                return jsonify({'error': 'Subscription expired. Renew at promptolian.com/pricing'}), 402

    # Enforce per-plan monthly limit
    plan = sub.get('plan', 'free')
    monthly_limit = RateLimiter.PLAN_LIMITS.get(plan, 0)
    if monthly_limit == 0:
        return jsonify({'error': 'Plan does not include cloud compression. Upgrade at promptolian.com/pricing'}), 402
    used = _repo.count_key_usage_month(api_key)
    if used >= monthly_limit:
        return jsonify({
            'error':       'Monthly limit reached',
            'plan':        plan,
            'used':        used,
            'limit':       monthly_limit,
            'upgrade_url': 'https://promptolian.com/pricing',
        }), 429

    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'request body required'}), 400

    messages = data.get('messages')
    model    = data.get('model', '')
    summary  = data.get('summary', '')

    if not isinstance(messages, list) or not messages:
        return jsonify({'error': '"messages" must be a non-empty array'}), 400

    # Extract last user turn as query for the context engine
    query = ''
    for m in reversed(messages):
        content = m.get('content', '')
        if m.get('role') == 'user':
            if isinstance(content, str):
                query = content
            elif isinstance(content, list):
                query = ' '.join(
                    b.get('text', '') for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                )
            break

    if not query:
        return jsonify({'error': 'no user message found in messages array'}), 400

    try:
        from context_engine import ContextEngine  # type: ignore
        ce     = ContextEngine()
        result = ce.optimize(messages, query=query, summary=summary)

        _repo.log_context_event(
            api_key          = api_key,
            mode             = 'kv-sandwich',
            original_tokens  = result.get('original_tokens', 0),
            optimized_tokens = result.get('optimized_tokens', 0),
            tokens_saved     = result.get('tokens_saved_estimate', 0),
            messages_total   = len(messages),
            messages_pruned  = result.get('messages_pruned', 0),
            summary_tokens   = _count_tokens(result.get('new_summary', '')),
            platform         = 'proxy',
        )

        return jsonify({
            'optimized_prompt': result.get('optimized_prompt', ''),
            'new_summary':      result.get('new_summary', ''),
        })
    except ImportError:
        return jsonify({'error': 'context_engine not available on this server'}), 500
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

        seen: Optional[set] = None
        if session_id:
            if session_id not in _TOOL_SESSION_CACHE:
                # Restore from DB so turn-2+ savings survive server restarts
                persisted = _repo.get_mcp_tool_session_names(session_id)
                _TOOL_SESSION_CACHE[session_id] = persisted if persisted is not None else set()
            seen = _TOOL_SESSION_CACHE[session_id]
        is_first_turn = seen is not None and len(seen) == 0

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
    'solo_monthly':  os.getenv('STRIPE_SOLO_MONTHLY', ''),
    'solo_annual':   os.getenv('STRIPE_SOLO_ANNUAL',  ''),
    'team_monthly':  os.getenv('STRIPE_TEAM_MONTHLY', ''),
    'team_annual':   os.getenv('STRIPE_TEAM_ANNUAL',  ''),
}
_BASE_URL   = os.getenv('BASE_URL', 'https://promptolian.com')
_GROQ_KEY   = os.getenv('GROQ_API_KEY', '')

_CHAT_SYSTEM = """You are the Promptolian assistant — a concise support bot for Promptolian (promptolian.com).

Key facts:
- Promptolian compresses AI prompts 15–33%, saving API costs. Runs privately on device.
- Plans: Free (Standard, browser JS), Pro $7/mo (grammar engine, domain packs), Builder $19/mo (REST API, context engine, MCP)
- Browser extension: works on Claude, ChatGPT, Gemini, Copilot, Perplexity. All compression is local — prompts never leave the device.
- REST API at api.promptolian.com. CLI: pip install promptolian then: promptolian compress "..."
- Context Engine (Builder): compresses conversation history — 33% combined savings, 101K tokens/month
- Tool schema compression: 69% first turn, 92% across session
- Languages: English, Spanish, Italian, French, German (auto-detected)
- Self-hosted option: docker compose up (from github.com/Maurizio-L/promptolian-public)
- Privacy: extension = 100% local. REST API = processed but never stored.
- 98.4% fact accuracy across 200 test prompts. Works deterministically — no external AI calls.

STRICT RULES:
1. Only answer questions about Promptolian — features, pricing, privacy, API, CLI, self-hosting, browser extension.
2. For anything outside this scope reply EXACTLY: "I can only help with Promptolian questions. See the docs at promptolian.com/docs.html or email support@promptolian.com"
3. Keep answers to 2–3 sentences. Plain text only, no markdown."""

_CHAT_FAQ = [
    (['price','cost','plan','free','pro','builder','paid','cheap','subscription'],
     "Free forever for Standard compression. Pro is $7/mo — grammar engine and domain packs. Builder is $19/mo — full REST API, context engine, and Claude Code MCP."),
    (['private','privacy','data','store','track','gdpr','safe','secure'],
     "The browser extension runs 100% locally — your prompts never leave your device. The REST API processes prompts server-side but never writes them to disk."),
    (['install','start','begin','setup','extension','chrome','firefox'],
     "Install the free browser extension from the Chrome or Firefox store — no account needed. It adds a Compress button directly on ChatGPT, Claude, Gemini and Copilot."),
    (['api','developer','rest','endpoint','curl','key'],
     "The REST API lives at api.promptolian.com. See full docs at promptolian.com/docs.html. No API key needed for Standard tier."),
    (['cli','command','terminal','pip','install'],
     "Install with: pip install promptolian — then run: promptolian compress \"your prompt here\". Works offline, fully local."),
    (['docker','self.host','self-host','own server','on.premise','on premise'],
     "Clone github.com/Maurizio-L/promptolian-public and run: docker compose up — the full API runs on your machine with zero data leaving your network."),
    (['context','memory','history','conversation','session'],
     "The Context Engine (Builder plan) compresses old conversation turns automatically — saving 33% combined across a session without losing any facts."),
    (['language','spanish','italian','french','german','multilingual'],
     "Five languages are supported: English, Spanish, Italian, French, German. Language is detected automatically — no setup needed."),
    (['how','work','compression','token','shrink'],
     "Promptolian replaces common phrases with symbols, removes filler words, and (on Pro/Developer) does grammar analysis — cutting 15–33% of tokens before the prompt reaches the AI."),
]

def _faq_fallback(message: str) -> str:
    msg = message.lower()
    for keywords, answer in _CHAT_FAQ:
        if any(k.replace('.', ' ') in msg for k in keywords):
            return answer
    return "Good question! Check the full docs at promptolian.com/docs.html or email support@promptolian.com and we'll help."

def _groq_response(message: str) -> str:
    if not _GROQ_KEY:
        return _faq_fallback(message)
    try:
        import httpx
        r = httpx.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Authorization': f'Bearer {_GROQ_KEY}', 'Content-Type': 'application/json'},
            json={
                'model': 'llama-3.1-8b-instant',
                'max_tokens': 200,
                'temperature': 0.3,
                'messages': [
                    {'role': 'system', 'content': _CHAT_SYSTEM},
                    {'role': 'user',   'content': message[:500]},
                ],
            },
            timeout=12,
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception:
        return _faq_fallback(message)

_CHAT_RATE_LIMIT = 30  # messages per session per hour

@app.route('/chat', methods=['POST'])
def chat():
    data       = request.json or {}
    message    = (data.get('message') or '').strip()[:500]
    session_id = (data.get('session_id') or 'anon')[:64]

    if not message:
        return jsonify({'error': 'message required'}), 400

    if _repo.count_chat_in_window(session_id) >= _CHAT_RATE_LIMIT:
        return jsonify({'reply': "You've sent a lot of messages — please wait a bit before trying again."}), 429

    reply = _groq_response(message)
    _repo.log_chat(session_id, message, reply)
    return jsonify({'reply': reply})


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
        import secrets as _sec
        customer_email = obj.get('customer_email', '')
        plan    = _resolve_plan_from_session(obj)
        api_key = f'ptl_{_sec.token_urlsafe(24)}'
        _repo.activate_subscription(customer_email, plan, obj.get('subscription', ''), api_key)
        _send_key_email(customer_email, api_key, plan)

    elif etype in ('customer.subscription.deleted', 'customer.subscription.updated'):
        sub    = obj
        status = sub.get('status', '')
        if status in ('canceled', 'unpaid', 'past_due'):
            _repo.deactivate_subscription(obj.get('id', ''))

    return jsonify({'received': True})


def _resolve_plan_from_session(session_obj: dict) -> str:
    """Map Stripe price ID back to plan name (solo/team)."""
    # Try line_items first, fall back to display_items (older API versions)
    items = session_obj.get('line_items', {}).get('data', []) or session_obj.get('display_items') or []
    price_id = ''
    if items:
        item = items[0]
        price_id = (item.get('price') or {}).get('id', '') if isinstance(item, dict) else ''
    for key, pid in _STRIPE_PRICES.items():
        if pid and pid == price_id:
            return key.split('_')[0]  # 'solo_monthly' → 'solo'
    return 'solo'  # safe default


if __name__ == '__main__':
    _repo.init_schema(
        Path(__file__).parent.parent.parent / 'tools' / 'reports' / 'schema_local.sql'
    )
    print('\n  Promptolian API v2.2  (SOLID)')
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