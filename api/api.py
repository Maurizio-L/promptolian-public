"""
Promptly API — api.py
Runs at http://localhost:3001

Endpoints:
  GET  /health
  POST /compress   body: {"text": "..."}
  GET  /stats
  POST /feedback   body: {"original": "...", "compressed": "...", "rating": 1-5}

Run: python3 api.py
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import re, math, json, sqlite3, os
from datetime import datetime

app = Flask(__name__)
CORS(app)

DB_PATH = os.path.join(os.path.dirname(__file__), '../../private/database/promptly.db')

# ── Compression engine ────────────────────────────────────────────────────────
RULES = [
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

def compress(text):
    out = text.strip()
    for pat, rep in RULES:
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    return out.replace('. ', '.\n').replace('  ', ' ').strip()

def count_tokens(text):
    words = re.split(r'[\s,.:;!?()\[\]{}"\']+', text.strip())
    return max(1, math.ceil(len([w for w in words if w]) * 1.3))

# ── DB setup ──────────────────────────────────────────────────────────────────
def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        # Minimal fallback if schema file is missing
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS compression_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                api_key TEXT,
                original_tokens INTEGER NOT NULL,
                compressed_tokens INTEGER NOT NULL,
                pct_saved INTEGER NOT NULL,
                mode TEXT DEFAULT 'standard',
                platform TEXT DEFAULT 'api',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                original TEXT,
                compressed TEXT,
                rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                comment TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
    conn.commit()
    conn.close()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'service': 'Promptly API',
        'version': '1.0.0',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/compress', methods=['POST'])
def compress_route():
    data = request.get_json()
    if not data or 'text' not in data:
        return jsonify({'error': 'text field required'}), 400

    text = data['text']
    if len(text) > 50000:
        return jsonify({'error': 'text too long (max 50000 chars)'}), 400

    compressed = compress(text)
    orig_t = count_tokens(text)
    comp_t = count_tokens(compressed)
    saved  = max(0, orig_t - comp_t)
    pct    = round(saved / orig_t * 100) if orig_t > 0 else 0

    # Log to DB
    try:
        conn = get_db()
        conn.execute(
            'INSERT INTO compression_events (original_tokens, compressed_tokens, pct_saved, platform) VALUES (?,?,?,?)',
            (orig_t, comp_t, pct, data.get('platform', 'api'))
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return jsonify({
        'compressed':       compressed,
        'original_tokens':  orig_t,
        'compressed_tokens':comp_t,
        'tokens_saved':     saved,
        'tokens_saved_pct': pct,
    })

@app.route('/stats')
def stats():
    try:
        conn = get_db()
        row = conn.execute('''
            SELECT
                COUNT(*) as total_compressions,
                SUM(tokens_saved) as total_tokens_saved,
                ROUND(AVG(pct_saved),1) as avg_pct_saved
            FROM (
                SELECT *, (original_tokens - compressed_tokens) as tokens_saved
                FROM compression_events
            )
        ''').fetchone()
        conn.close()
        return jsonify({
            'total_compressions':  row['total_compressions'] or 0,
            'total_tokens_saved':  row['total_tokens_saved'] or 0,
            'avg_compression_pct': row['avg_pct_saved'] or 0,
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
            'INSERT INTO feedback (original, compressed, rating) VALUES (?,?,?)',
            (data.get('original',''), data.get('compressed',''), data.get('rating',5))
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/optimize-context', methods=['POST'])
def optimize_context():
    """Context optimisation pipeline.

    POST body:
        {
          "messages": [{"role": "user"|"assistant", "content": "..."},...],
          "query":    "current user message",
          "summary":  "optional prior rolling summary",
          "mode":     "lossless" | "aggressive"  (default: lossless)
        }

    Response:
        {
          "optimized_prompt":      str,  -- structured context string for LLM
          "system":                str,  -- system-slot text (summary)
          "messages":              list, -- pruned, optionally compressed messages
          "user":                  str,  -- current query (optionally compressed)
          "new_summary":           str,  -- updated rolling summary
          "tokens_saved_estimate": int,
          "original_tokens":       int,
          "optimized_tokens":      int,
          "messages_pruned":       int
        }
    """
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

    # Validate message objects
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
            return jsonify({
                'error': f'messages[{i}] must have "role" and "content" fields'
            }), 400
        if m['role'] not in ('user', 'assistant', 'system'):
            return jsonify({
                'error': f'messages[{i}].role must be user | assistant | system'
            }), 400

    try:
        from context_engine import ContextEngine
        ce     = ContextEngine()
        result = ce.optimize(messages, query, summary=summary, mode=mode)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    init_db()
    print('\n  Promptly API')
    print('  ────────────────────────────────')
    print('  http://localhost:3001/health')
    print('  POST http://localhost:3001/compress')
    print('  GET  http://localhost:3001/stats')
    print()
    app.run(host='0.0.0.0', port=3001, debug=True)
