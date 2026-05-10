"""context_engine.py — Context optimization layer for the Promptolian API.

Reduces token usage across multi-turn conversations by combining:
  1. prune()                — relevance-weighted + recency-based message trimming
  2. delta_prune()          — drop messages whose facts are already in history
  3. build_entity_register()— centralize entities, remove inline repetition
  4. apply_session_huffman()— session-adaptive symbol table for repeated phrases
  5. summarize()            — rolling extractive summary of older history
  6. build_context()        — merges summary + register + messages + query
  7. optimize()             — full pipeline, returns API-ready result dict
  8. compress_tools()       — compile JSON tool schemas → compact function-sig DSL

Pipeline (used by POST /optimize-context):
    raw messages
      → delta_prune (remove zero-delta turns)
      → entity_register (centralize facts)
      → relevance prune (keep query-relevant head)
      → summarize (if needed)
      → session_huffman (compress repeated phrases)
      → build_context
      → [compress]

Tool schema pipeline (used by POST /compress-tools):
    raw tool schemas (OpenAI / Anthropic / plain format)
      → normalise (extract name, description, parameters)
      → type inference (elide obvious types from param names)
      → shared-type registry (alias params used in 3+ tools)
      → DSL serialisation  →  "fn(p1, p2: type = default)  # desc"
      → session cache      →  turn 2+ sends only "TOOLS:[fn1,fn2]"

Standalone usage:
    from context_engine import ContextEngine, compress_tools
    ce = ContextEngine()
    result = ce.optimize(messages, query)
    # → {optimized_prompt, new_summary, tokens_saved_estimate,
    #    original_tokens, optimized_tokens, messages_pruned}

    dsl, meta = compress_tools(tools)
    # → ("search_web(query, n=10)  # …\\n…", {original_tokens, compressed_tokens, cr})
"""

from __future__ import annotations

import re
import math
import collections
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Token counting — tiktoken when available, words*1.3 heuristic as fallback
# ─────────────────────────────────────────────────────────────────────────────

try:
    import tiktoken as _tiktoken
    _enc = _tiktoken.get_encoding('cl100k_base')
    def _tokens(text: str) -> int:
        return max(1, len(_enc.encode(text)))
except Exception:
    def _tokens(text: str) -> int:
        words = re.split(r'[\s,.:;!?()\[\]{}"\']+', text.strip())
        return max(1, math.ceil(len([w for w in words if w]) * 1.3))


def _msg_tokens(msg: dict) -> int:
    return _tokens(msg.get('content', ''))


# ─────────────────────────────────────────────────────────────────────────────
# Sentence scoring helpers for extractive summarisation
# ─────────────────────────────────────────────────────────────────────────────

_GOAL_RE = re.compile(
    r'\b(i want|i need|i\'m trying|goal is|task is|objective is|'
    r'please|i\'d like|we need|we want|help me|create|build|write|'
    r'implement|generate|analyze|explain|summarize|compare)\b',
    re.I,
)
_CONSTRAINT_RE = re.compile(
    r'\b(must|should|don\'t|do not|avoid|never|always|required|'
    r'important|ensure|make sure|without|except|only|no\b)\b',
    re.I,
)
_CONTEXT_RE = re.compile(
    r'\b(i am|i\'m|we are|we\'re|the project|the app|the system|'
    r'background|context|using|built with|based on|version|codebase)\b',
    re.I,
)
_FORMAT_RE = re.compile(
    r'\b(return|format|output|as json|as yaml|as table|step.by.step|'
    r'bullet|numbered|concise|detailed|code only)\b',
    re.I,
)


def _score_sentence(sent: str) -> float:
    words = len(sent.split())
    if words < 4:
        return 0.0
    score = 0.0
    score += len(_GOAL_RE.findall(sent))       * 3.0
    score += len(_CONSTRAINT_RE.findall(sent)) * 2.5
    score += len(_CONTEXT_RE.findall(sent))    * 2.0
    score += len(_FORMAT_RE.findall(sent))     * 1.5
    # prefer medium-length sentences (8–30 words)
    if 8 <= words <= 30:
        score += 1.0
    elif words > 50:
        score -= 1.0
    return score


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in parts if s.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Quant helpers
# ─────────────────────────────────────────────────────────────────────────────

# Entity extraction — numbers, URLs, acronyms, proper names
_ENTITY_RE = re.compile(
    r'https?://\S+'                            # URLs
    r'|\b\d+(?:[.,/]\d+)*\s*(?:%|USD|EUR|GBP|k|M|B|ms|px|rem|em)?\b'  # numbers + units
    r'|\b[A-Z]{2,}\b'                          # acronyms
    r'|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+'        # proper names (2+ words)
)

# Short symbol prefix for session Huffman
_HUFF_PREFIX = '§H'

def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity between two strings on word sets."""
    sa = set(re.sub(r'\W+', ' ', a.lower()).split()) - {'the', 'a', 'an', 'is', 'are', 'and', 'or', 'to', 'of', 'in', 'i'}
    sb = set(re.sub(r'\W+', ' ', b.lower()).split()) - {'the', 'a', 'an', 'is', 'are', 'and', 'or', 'to', 'of', 'in', 'i'}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _extract_entities(text: str) -> frozenset[str]:
    """Return frozenset of entity strings found in text."""
    return frozenset(m.strip() for m in _ENTITY_RE.findall(text) if len(m.strip()) > 1)


def _ngrams(tokens: list[str], n: int) -> list[tuple]:
    return [tuple(tokens[i:i+n]) for i in range(len(tokens) - n + 1)]


def _apply_entity_map(text: str, entity_map: dict[str, str]) -> str:
    """Replace entity strings with their symbols (longest first)."""
    out = text
    for ent in sorted(entity_map, key=len, reverse=True):
        out = out.replace(ent, entity_map[ent])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tool schema compression — DSL compilation
# ─────────────────────────────────────────────────────────────────────────────

import json as _json

_JSON_TYPE_MAP = {
    'string': 'str', 'integer': 'int', 'number': 'float',
    'boolean': 'bool', 'array': 'list', 'object': 'dict', 'null': 'None',
}

# Param names whose type is obvious — skip annotation when schema matches
_ELIDE_STR = re.compile(
    r'(?:_id|_key|_code|_slug|_name|_title|_text|_content|_message|_prompt|'
    r'_query|_url|_path|_email|_token|_hash|_type|_format|_lang|_language|'
    r'_status|_mode|_tag|_label|_prefix|_suffix|_pattern|_description|'
    r'_summary|_note|_reason|_input|_output|_cursor|_sort|_order|_filter|'
    r'^query$|^text$|^content$|^message$|^prompt$|^url$|^path$|^email$|'
    r'^language$|^format$|^sort$|^order$|^filter$|^cursor$|^token$)$', re.I,
)
_ELIDE_INT = re.compile(
    r'(?:_count|_num|_number|_size|_limit|_offset|_index|_page|_rank|'
    r'_score|_age|_year|_month|_day|_hour|_minute|_second|_timeout|'
    r'_length|_width|_height|_depth|_priority|_weight|_version|_max|_min|'
    r'^n$|^k$|^count$|^limit$|^offset$|^page$|^size$|^top_k$|^max$|^min$)$', re.I,
)
_ELIDE_BOOL = re.compile(
    r'(?:^is_|^has_|^enable|^disable|^use_|^with_|^include_|^exclude_|'
    r'^allow_|^show_|^hide_|^force_|^strict_|^verbose$|^debug$|^async$|'
    r'_enabled$|_disabled$|_active$|_required$|_visible$|_recursive$)$', re.I,
)
_ELIDE_LIST = re.compile(
    r'(?:_ids$|_list$|_items$|_tags$|_labels$|_names$|_urls$|_paths$|'
    r'_keys$|_values$|_fields$|_columns$|_rows$|_args$|_params$|'
    r'^ids$|^items$|^tags$|^fields$|^args$|^keys$|^values$)$', re.I,
)


def _map_type(json_type: Optional[str]) -> str:
    return _JSON_TYPE_MAP.get(json_type or '', json_type or 'any')


def _should_elide_type(name: str, mapped_type: str) -> bool:
    """Return True when the type is obvious from the parameter name."""
    if mapped_type == 'str'  and _ELIDE_STR.search(name):  return True
    if mapped_type == 'int'  and _ELIDE_INT.search(name):  return True
    if mapped_type == 'bool' and _ELIDE_BOOL.search(name): return True
    if mapped_type == 'list' and _ELIDE_LIST.search(name): return True
    return False


def _normalise_tool(tool: dict) -> Optional[dict]:
    """Extract {name, description, params} from OpenAI / Anthropic / plain schema."""
    # OpenAI function-calling wrapper: {"type":"function","function":{...}}
    if tool.get('type') == 'function' and isinstance(tool.get('function'), dict):
        tool = tool['function']

    name = tool.get('name', '').strip()
    if not name:
        return None

    desc = tool.get('description', '').strip()

    # Parameter schema: OpenAI uses "parameters", Anthropic uses "input_schema"
    param_schema = tool.get('parameters') or tool.get('input_schema') or {}
    properties   = param_schema.get('properties', {})
    required_set = set(param_schema.get('required', []))

    params: list[dict] = []
    for pname, pschema in properties.items():
        json_type   = pschema.get('type') if isinstance(pschema, dict) else None
        mapped      = _map_type(json_type)
        elide       = _should_elide_type(pname, mapped)
        default_val = pschema.get('default') if isinstance(pschema, dict) else None
        is_required = pname in required_set
        enum_vals   = pschema.get('enum') if isinstance(pschema, dict) else None
        params.append({
            'name':       pname,
            'type':       mapped,
            'elide_type': elide,
            'required':   is_required,
            'default':    default_val,
            'enum':       enum_vals[:3] if enum_vals else None,  # cap enum display
        })

    # Sort: required first, then optional
    params.sort(key=lambda p: (0 if p['required'] else 1, p['name']))
    return {'name': name, 'description': desc, 'params': params}


def _format_param(p: dict) -> str:
    """Render one parameter as 'name', 'name: type', or 'name=default'."""
    name    = p['name']
    typ     = p['type']
    default = p['default']
    elide   = p['elide_type']

    if p['enum']:
        # Show compact enum hint instead of type
        opts = '|'.join(str(v) for v in p['enum'])
        suffix = f': {opts}' if not default else f': {opts}={default!r}'
        return f'{name}{suffix}'

    if default is not None:
        # Optional with default: show default, drop type if elided or it's str
        if elide or typ == 'str':
            return f'{name}={default!r}'
        return f'{name}: {typ}={default!r}'

    if elide:
        return name  # type obvious from name

    if typ in ('any', ''):
        return name

    return f'{name}: {typ}'


def _build_shared_registry(normalised: list[dict], min_tools: int = 3) -> dict[str, str]:
    """Alias parameter (name, type) pairs that appear in min_tools+ functions."""
    counter: collections.Counter = collections.Counter()
    for tool in normalised:
        seen = set()
        for p in tool['params']:
            key = (p['name'], p['type'])
            if key not in seen:
                counter[key] += 1
                seen.add(key)

    aliases: dict[str, str] = {}
    idx = 1
    for (pname, ptype), cnt in counter.most_common():
        if cnt >= min_tools and not _should_elide_type(pname, ptype):
            aliases[f'§T{idx}'] = f'{pname}: {ptype}'
            idx += 1
    return aliases  # {symbol: "param: type"}


def _serialise_dsl(
    normalised: list[dict],
    registry: dict[str, str],
    session_seen: Optional[set],
) -> tuple[str, list[str]]:
    """
    Render tools as DSL lines.

    Returns (dsl_block, list_of_names_for_cache_line).
    Already-seen tools (session cache) are skipped from DSL; caller emits
    a compact TOOLS:[...] reference for them.
    """
    # Build reverse map: "param: type" → symbol
    rev_registry = {v: k for k, v in registry.items()}

    lines: list[str] = []
    new_names: list[str] = []
    cached_names: list[str] = []

    for tool in normalised:
        name = tool['name']
        if session_seen is not None and name in session_seen:
            cached_names.append(name)
            continue

        new_names.append(name)
        parts: list[str] = []
        for p in tool['params']:
            raw = f'{p["name"]}: {p["type"]}'
            if raw in rev_registry:
                parts.append(rev_registry[raw])
            else:
                parts.append(_format_param(p))

        sig  = f'{name}({", ".join(parts)})'
        desc = f'  # {tool["description"]}' if tool['description'] else ''
        # Truncate very long descriptions
        if len(desc) > 80:
            desc = desc[:77] + '…'
        lines.append(f'{sig}{desc}')

    dsl_block = '\n'.join(lines)
    return dsl_block, cached_names


def compress_tools(
    tools: list[dict],
    session_seen: Optional[set] = None,
    min_registry_tools: int = 3,
) -> tuple[str, dict]:
    """Compile JSON tool schemas to a compact function-signature DSL.

    Strategies applied (stacked):
      1. Format conversion  — JSON → Python-style function signatures
      2. Type elision       — drop annotations obvious from param names
      3. Shared-type registry — alias (name:type) pairs in 3+ tools as §T1 etc.
      4. Session cache      — tools sent in a previous turn become TOOLS:[…]

    Parameters
    ----------
    tools              : list of tool schema dicts (OpenAI, Anthropic, or plain)
    session_seen       : set of tool names already sent this session; mutated
                         in-place to add newly sent names (pass None to disable)
    min_registry_tools : minimum tools sharing a param before it gets an alias

    Returns
    -------
    dsl_str : str   — compact string to prepend to system prompt
    meta    : dict  — {original_tokens, compressed_tokens, cr, cached_count,
                       registry, new_tools, cached_tools}
    """
    if not tools:
        return '', {'original_tokens': 0, 'compressed_tokens': 0, 'cr': 0.0,
                    'cached_count': 0, 'registry': {}, 'new_tools': [], 'cached_tools': []}

    # Measure original (pretty JSON, as most SDKs send it)
    original_str    = _json.dumps(tools, separators=(',', ':'))
    original_tokens = _tokens(original_str)

    # Normalise all tools
    normalised = [n for t in tools if (n := _normalise_tool(t)) is not None]

    # Build shared-type registry
    registry = _build_shared_registry(normalised, min_tools=min_registry_tools)

    # Serialise to DSL (respecting session cache)
    dsl_block, cached_names = _serialise_dsl(normalised, registry, session_seen)
    new_names = [t['name'] for t in normalised if t['name'] not in (cached_names or [])]

    # Update session cache
    if session_seen is not None:
        session_seen.update(new_names)

    # Assemble output block
    parts: list[str] = []

    if registry:
        reg_line = 'TYPES:{' + ', '.join(f'{s}={v}' for s, v in registry.items()) + '}'
        parts.append(reg_line)

    if cached_names:
        parts.append(f'TOOLS:[{",".join(cached_names)}]')  # reference only

    if dsl_block:
        parts.append(dsl_block)

    out = '\n'.join(parts)
    compressed_tokens = _tokens(out) if out else 0
    cr = round(1 - compressed_tokens / original_tokens, 4) if original_tokens else 0.0

    return out, {
        'original_tokens':    original_tokens,
        'compressed_tokens':  compressed_tokens,
        'cr':                 cr,
        'cached_count':       len(cached_names),
        'registry':           registry,
        'new_tools':          new_names,
        'cached_tools':       cached_names,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compression bridge (optional — uses research engine if on path)
# ─────────────────────────────────────────────────────────────────────────────

def _try_compress(text: str) -> str:
    """Apply Promptolian symbol compression if engine is importable."""
    try:
        import sys, os
        _code_dir = os.path.join(os.path.dirname(__file__),
                                 '../../private/research/code')
        if _code_dir not in sys.path:
            sys.path.insert(0, _code_dir)
        from engine_v4 import PromptolianEngine  # type: ignore
        engine = PromptolianEngine()
        result = engine.compress(text, tier='pro')
        return result.compressed
    except Exception:
        # fallback: basic inline compression (mirrors api.py RULES)
        import re as _re
        _BASIC = [
            (r'you are an? expert (in |on |at )?', '§EXP '),
            (r'you are an? ', '§ROLE '),
            (r'please ', ''),
            (r'I need you to ', ''),
            (r'I would like you to ', ''),
            (r'return only (the )?code[^.]*\.?', '→code'),
            (r'step[- ]by[- ]step', '→step'),
            (r'be (very )?concise\.?', '→short'),
            (r'\bsummarize\b', '∑'),
            (r'\bexplain\b', '?'),
            (r'\boptimize\b', 'OPT'),
            (r'\bdebug\b', 'BUG'),
            (r' +', ' '),
        ]
        out = text.strip()
        for pat, rep in _BASIC:
            out = _re.sub(pat, rep, out, flags=_re.IGNORECASE)
        return out.strip()


# ─────────────────────────────────────────────────────────────────────────────
# ContextEngine
# ─────────────────────────────────────────────────────────────────────────────

class ContextEngine:
    """Context pruning, summarisation, and prompt assembly for multi-turn chats.

    Parameters
    ----------
    budget_tokens      : max total tokens for conversation history (default 3 000)
    keep_last          : number of most-recent messages always kept (default 6)
    summarize_threshold: message count that triggers auto-summarisation (default 12)
    summary_budget     : target token count for generated summaries (default 400)
    groq_api_key       : optional — enables LLM-assisted summarisation via Groq
    groq_model         : Groq model to use for summarisation
    """

    def __init__(
        self,
        budget_tokens: int = 3_000,
        keep_last: int = 6,
        summarize_threshold: int = 12,
        summary_budget: int = 400,
        groq_api_key: Optional[str] = None,
        groq_model: str = 'llama-3.3-70b-versatile',
    ):
        self.budget_tokens       = budget_tokens
        self.keep_last           = keep_last
        self.summarize_threshold = summarize_threshold
        self.summary_budget      = summary_budget
        self._groq_key           = groq_api_key or self._load_groq_key()
        self._groq_model         = groq_model

    @staticmethod
    def _load_groq_key() -> Optional[str]:
        try:
            import sys, os
            _code_dir = os.path.join(os.path.dirname(__file__),
                                     '../../private/research/code')
            if _code_dir not in sys.path:
                sys.path.insert(0, _code_dir)
            from params_loader import params  # type: ignore
            return params.groq_api_key
        except Exception:
            import os
            return os.getenv('GROQ_API_KEY')

    # ── 1. Prune (relevance-weighted) ────────────────────────────────────────

    def prune(
        self,
        messages: list[dict],
        budget_tokens: Optional[int] = None,
        keep_last: Optional[int] = None,
        query: Optional[str] = None,
    ) -> list[dict]:
        """Trim *messages* to fit within *budget_tokens*.

        Strategy:
          - Always keep the last *keep_last* messages (recency anchor)
          - If *query* supplied: fill remaining budget by relevance score (Jaccard)
          - Otherwise: fill newest-first (legacy behaviour)
          - Returns ordered list (oldest → newest)
        """
        budget = budget_tokens or self.budget_tokens
        keep_n = keep_last     or self.keep_last
        msgs   = [m for m in messages if isinstance(m, dict) and 'content' in m]

        if not msgs:
            return []

        tail = msgs[-keep_n:] if len(msgs) > keep_n else msgs[:]
        head = msgs[:-keep_n] if len(msgs) > keep_n else []

        tail_tokens = sum(_msg_tokens(m) for m in tail)
        remaining   = max(0, budget - tail_tokens)

        if query and head:
            # Sort head by relevance to current query, highest first
            scored = sorted(head, key=lambda m: _jaccard(m.get('content', ''), query), reverse=True)
        else:
            scored = list(reversed(head))  # newest-first (original behaviour)

        kept_head: list[dict] = []
        for msg in scored:
            t = _msg_tokens(msg)
            if t <= remaining:
                kept_head.append(msg)
                remaining -= t

        # Restore chronological order
        head_set = {id(m) for m in kept_head}
        kept_head = [m for m in head if id(m) in head_set]

        return kept_head + tail

    # ── 2. Delta prune ───────────────────────────────────────────────────────

    def delta_prune(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Drop messages whose entity set is a subset of earlier messages.

        A message has Δ=0 (zero new information) when all its facts have
        already appeared in the conversation. Pure acknowledgements
        ("ok", "sounds good") and reformulations are dropped.

        Returns
        -------
        (kept_messages, n_dropped)
        """
        msgs    = [m for m in messages if isinstance(m, dict) and 'content' in m]
        known   : set[str] = set()
        kept    : list[dict] = []
        dropped : int = 0

        for msg in msgs:
            content  = msg.get('content', '')
            entities = _extract_entities(content)
            words    = set(re.sub(r'\W+', ' ', content.lower()).split())

            # Always keep: system messages, messages with new entities, messages
            # with meaningful new vocabulary (> 5 new non-stopwords)
            _STOP = {'i','you','the','a','an','is','are','was','were','be',
                     'to','of','in','it','that','this','and','or','but','so',
                     'ok','okay','yes','no','sure','great','thanks','thank',
                     'sounds','good','got','noted','understood'}
            new_words = words - known - _STOP

            if (msg.get('role') == 'system'
                    or not entities.issubset(known)
                    or len(new_words) > 5):
                kept.append(msg)
                known |= entities
                known |= words
            else:
                dropped += 1

        return kept, dropped

    # ── 3. Entity register ───────────────────────────────────────────────────

    def build_entity_register(
        self, messages: list[dict]
    ) -> tuple[str, dict[str, str]]:
        """Centralise repeated entities into a compact register string.

        Entities that appear in 2+ messages get a short symbol (§E1, §E2 …).
        The register is injected into the system prompt; repeated inline
        mentions can be removed from individual messages.

        Returns
        -------
        register_str : str  — compact "ENTITIES: {§E1=X, §E2=Y}" block
        entity_map   : dict — {entity_text: symbol}  (empty if too few)
        """
        freq: dict[str, int] = collections.Counter()
        for m in messages:
            for ent in _extract_entities(m.get('content', '')):
                freq[ent] += 1

        # Only entities appearing in 2+ turns warrant a register entry
        repeated = {e: c for e, c in freq.items() if c >= 2}
        if not repeated:
            return '', {}

        # Sort by frequency desc, assign symbols
        sorted_ents = sorted(repeated, key=lambda e: (-repeated[e], e))
        entity_map  = {ent: f'§E{i+1}' for i, ent in enumerate(sorted_ents)}

        pairs = ', '.join(f'{sym}={ent}' for ent, sym in entity_map.items())
        register_str = f'ENTITIES: {{{pairs}}}'
        return register_str, entity_map

    # ── 4. Session Huffman ───────────────────────────────────────────────────

    def apply_session_huffman(
        self,
        messages: list[dict],
        top_n: int = 8,
        min_freq: int = 3,
    ) -> tuple[list[dict], dict[str, str], int]:
        """Build a session-adaptive symbol table from repeated bigrams/trigrams.

        Phrases that repeat *min_freq* or more times across the session get
        assigned short §HN symbols. Compressed messages and a decode table
        are returned. The decode table must be injected into the system prompt.

        Returns
        -------
        compressed_messages : list[dict]  — messages with replacements applied
        decode_table        : dict        — {symbol: original_phrase}
        tokens_saved        : int
        """
        # Collect all word tokens across user messages
        all_tokens: list[str] = []
        for m in messages:
            toks = re.sub(r'\W+', ' ', m.get('content', '').lower()).split()
            all_tokens.extend(toks)

        # Count bigrams and trigrams
        freq: collections.Counter = collections.Counter()
        for n in (3, 2):
            for gram in _ngrams(all_tokens, n):
                phrase = ' '.join(gram)
                if len(phrase) > 5:  # skip trivial phrases
                    freq[phrase] += 1

        # Pick top-N by token-savings = (phrase_tokens - 1) * frequency
        def _savings(phrase: str, count: int) -> int:
            return (len(phrase.split()) - 1) * count

        candidates = [
            (phrase, cnt) for phrase, cnt in freq.items() if cnt >= min_freq
        ]
        candidates.sort(key=lambda x: _savings(x[0], x[1]), reverse=True)
        selected = candidates[:top_n]

        if not selected:
            return messages, {}, 0

        # Build symbol table
        decode_table: dict[str, str] = {}
        sym_map: dict[str, str] = {}  # phrase → symbol
        for i, (phrase, _) in enumerate(selected):
            sym = f'{_HUFF_PREFIX}{i+1}'
            decode_table[sym] = phrase
            sym_map[phrase] = sym

        # Apply substitutions (longest first to avoid partial matches)
        sorted_phrases = sorted(sym_map, key=len, reverse=True)
        total_saved = 0
        compressed: list[dict] = []
        for m in messages:
            content = m.get('content', '')
            new_content = content
            for phrase in sorted_phrases:
                sym      = sym_map[phrase]
                pattern  = re.compile(r'\b' + re.escape(phrase) + r'\b', re.IGNORECASE)
                matches  = len(pattern.findall(new_content))
                if matches:
                    new_content  = pattern.sub(sym, new_content)
                    total_saved += matches * (len(phrase.split()) - 1)
            compressed.append({**m, 'content': new_content})

        return compressed, decode_table, total_saved

    # ── 5. Summarise ─────────────────────────────────────────────────────────

    def summarize(
        self,
        messages: list[dict],
        existing_summary: str = '',
    ) -> str:
        """Build a compact rolling summary of *messages*.

        Prefers LLM summarisation (via Groq) when available; falls back to
        extractive scoring otherwise.

        Parameters
        ----------
        messages         : list of {role, content} dicts to summarise
        existing_summary : prior summary to update (rolling)
        """
        if not messages:
            return existing_summary

        if self._groq_key:
            try:
                return self._summarize_llm(messages, existing_summary)
            except Exception:
                pass  # fall through to extractive

        return self._summarize_extractive(messages, existing_summary)

    def _summarize_extractive(
        self,
        messages: list[dict],
        existing_summary: str,
    ) -> str:
        """Score-based extractive summary — no external deps."""
        # Collect sentences from user messages (intent source)
        user_sents: list[tuple[float, str]] = []
        for msg in messages:
            if msg.get('role') != 'user':
                continue
            for sent in _split_sentences(msg.get('content', '')):
                score = _score_sentence(sent)
                if score > 0:
                    user_sents.append((score, sent))

        # Sort by score desc, deduplicate near-identical sentences
        user_sents.sort(key=lambda x: x[0], reverse=True)
        seen: set[str] = set()
        selected: list[str] = []
        budget = self.summary_budget
        for _, sent in user_sents:
            key = re.sub(r'\W+', '', sent.lower())[:40]
            if key in seen:
                continue
            seen.add(key)
            t = _tokens(sent)
            if t <= budget:
                selected.append(sent)
                budget -= t
            if budget <= 0:
                break

        if not selected:
            # Nothing scored — just take the first user message's first sentence
            for msg in messages:
                if msg.get('role') == 'user':
                    s = _split_sentences(msg.get('content', ''))
                    if s:
                        selected = [s[0]]
                    break

        new_part = ' '.join(selected)

        if existing_summary:
            # Rolling update: keep existing summary, append new non-redundant info
            combined = f"{existing_summary.rstrip()} {new_part}".strip()
            # Trim to summary_budget tokens
            words = combined.split()
            max_words = int(self.summary_budget / 1.3)
            if len(words) > max_words:
                combined = ' '.join(words[-max_words:])
            return combined
        return new_part.strip()

    def _summarize_llm(
        self,
        messages: list[dict],
        existing_summary: str,
    ) -> str:
        """LLM-assisted summarisation via Groq."""
        try:
            from groq import Groq  # type: ignore
        except ImportError:
            raise RuntimeError("groq package not installed")

        client = Groq(api_key=self._groq_key)

        history_text = '\n'.join(
            f"{m.get('role','?').upper()}: {m.get('content','')}"
            for m in messages[-20:]  # limit context sent to LLM
        )

        prior_block = (
            f"Existing summary (update, don't repeat):\n{existing_summary}\n\n"
            if existing_summary else ''
        )

        system_prompt = (
            "You are a conversation summarizer. Produce a concise summary "
            "(under 400 tokens) capturing: user goals, constraints, key context, "
            "and output format requirements. Be factual and terse. "
            "Do not include filler phrases."
        )
        user_prompt = (
            f"{prior_block}"
            f"Conversation to summarize:\n{history_text}\n\n"
            "Summary:"
        )

        resp = client.chat.completions.create(
            model=self._groq_model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user',   'content': user_prompt},
            ],
            max_tokens=450,
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()

    # ── 6. Build context ─────────────────────────────────────────────────────

    def build_context(
        self,
        messages: list[dict],
        query: str,
        summary: str = '',
        mode: str = 'lossless',
    ) -> dict:
        """Assemble final context structure from summary + messages + query.

        Parameters
        ----------
        messages : pruned message list (output of prune())
        query    : current user query (not yet in messages)
        summary  : long-term memory string (output of summarize())
        mode     : 'lossless' — keep content as-is
                   'aggressive' — compress message content with Promptolian

        Returns
        -------
        dict with keys:
            system           : system prompt (summary)
            messages         : list of {role, content} ready for chat API
            user             : current query (optionally compressed)
            optimized_prompt : flat string combining all parts
        """
        def _maybe_compress(text: str) -> str:
            return _try_compress(text) if mode == 'aggressive' else text

        # Build system block
        system = summary.strip() if summary else ''

        # Compress message contents if aggressive
        clean_msgs = [
            {'role': m.get('role', 'user'),
             'content': _maybe_compress(m.get('content', ''))}
            for m in messages
        ]

        # Compress current query if aggressive
        compressed_query = _maybe_compress(query)

        # Build flat optimized_prompt
        parts: list[str] = []
        if system:
            parts.append(f"[CONTEXT]\n{system}")
        if clean_msgs:
            convo = '\n'.join(
                f"{m['role'].upper()}: {m['content']}" for m in clean_msgs
            )
            parts.append(f"[CONVERSATION]\n{convo}")
        parts.append(f"[QUERY]\n{compressed_query}")
        optimized_prompt = '\n\n'.join(parts)

        return {
            'system':           system,
            'messages':         clean_msgs,
            'user':             compressed_query,
            'optimized_prompt': optimized_prompt,
        }

    # ── 7. Full pipeline ─────────────────────────────────────────────────────

    def optimize(
        self,
        messages: list[dict],
        query: str,
        summary: str = '',
        mode: str = 'lossless',
        budget_tokens: Optional[int] = None,
    ) -> dict:
        """Full pipeline: delta_prune → entity_register → relevance_prune
                         → summarize → session_huffman → build_context.

        Parameters
        ----------
        messages      : raw conversation history [{role, content}, ...]
        query         : current user query
        summary       : existing rolling summary ('' on first call)
        mode          : 'lossless' | 'aggressive'
        budget_tokens : override token budget

        Returns
        -------
        dict with:
            optimized_prompt     : str — formatted context string for the LLM
            new_summary          : str — updated rolling summary
            tokens_saved_estimate: int — tokens removed from original history
            original_tokens      : int — token count of raw input
            optimized_tokens     : int — token count of result
            messages_pruned      : int — messages dropped by all pruning steps
            delta_dropped        : int — messages dropped by delta_prune
            huffman_tokens_saved : int — tokens saved by session Huffman
            entity_register      : str — entity register block (injected in system)
        """
        budget = budget_tokens or self.budget_tokens

        # Baseline
        original_text   = ' '.join(m.get('content', '') for m in messages) + ' ' + query
        original_tokens = _tokens(original_text)

        # Step 1: delta prune — drop zero-new-info turns
        after_delta, delta_dropped = self.delta_prune(messages)

        # Step 2: entity register — apply substitutions BEFORE pruning so
        # shorter messages allow more history to fit in budget
        register_str, entity_map = self.build_entity_register(after_delta)
        if entity_map:
            after_delta = [
                {**m, 'content': _apply_entity_map(m.get('content',''), entity_map)}
                for m in after_delta
            ]

        # Step 3: relevance prune — keep query-relevant head within budget
        pruned    = self.prune(after_delta, budget_tokens=budget, query=query)
        n_dropped = len(messages) - len(pruned)

        # Step 4: summarise if history warranted it
        new_summary = summary
        should_summarize = (
            len(messages) >= self.summarize_threshold
            or original_tokens > budget
        )
        if should_summarize:
            dropped_msgs = [m for m in after_delta if m not in pruned]
            if dropped_msgs or not summary:
                new_summary = self.summarize(
                    dropped_msgs or after_delta,
                    existing_summary=summary,
                )

        # Append entity register only when its overhead is covered by message savings
        effective_summary = new_summary
        if register_str:
            reg_overhead = _tokens(register_str)
            # Count tokens saved in pruned messages from entity substitution
            reg_savings  = sum(
                _tokens(m.get('content','')) for m in pruned
                if any(sym in m.get('content','') for sym in entity_map.values())
            ) if entity_map else 0
            if reg_savings > reg_overhead or entity_map:
                effective_summary = (
                    f"{new_summary}\n{register_str}" if new_summary else register_str
                )

        # Step 5: session Huffman — compress repeated phrases
        compressed_msgs, decode_table, huff_saved = self.apply_session_huffman(pruned)

        # Inject Huffman decode table only when net-positive
        if decode_table and huff_saved > _tokens(' '.join(f'{s}={p}' for s,p in decode_table.items())):
            pairs = ', '.join(f'{sym}={phrase}' for sym, phrase in decode_table.items())
            huff_block = f'SYMBOLS: {{{pairs}}}'
            effective_summary = (
                f"{effective_summary}\n{huff_block}" if effective_summary else huff_block
            )
        else:
            compressed_msgs = pruned  # revert if not net-positive
            huff_saved = 0

        # Step 6: build context
        ctx = self.build_context(
            compressed_msgs, query, summary=effective_summary, mode=mode
        )

        optimized_tokens = _tokens(ctx['optimized_prompt'])
        tokens_saved     = max(0, original_tokens - optimized_tokens)

        return {
            'optimized_prompt':      ctx['optimized_prompt'],
            'system':                ctx['system'],
            'messages':              ctx['messages'],
            'user':                  ctx['user'],
            'new_summary':           new_summary,
            'tokens_saved_estimate': tokens_saved,
            'original_tokens':       original_tokens,
            'optimized_tokens':      optimized_tokens,
            'messages_pruned':       n_dropped,
            'delta_dropped':         delta_dropped,
            'huffman_tokens_saved':  huff_saved,
            'entity_register':       register_str,
        }
