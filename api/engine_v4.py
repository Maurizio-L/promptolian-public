#!/usr/bin/env python3
"""
engine_v4.py — Promptolian Compression Engine v4  (SOLID refactor)

Architecture
------------
  Interfaces (typing.Protocol):
    TierCompressor   — compress(text, lang) -> str
    LanguageDetector — detect(text) -> str
    TokenCounter     — count(text, lang) -> int
    TextPruner       — prune(text) -> str
    DomainDetector   — detect(text) -> str | None

  Implementations:
    RegexLanguageDetector   → LanguageDetector
    TiktokenTokenCounter    → TokenCounter
    FallbackTokenCounter    → TokenCounter  (no tiktoken)
    KeywordDomainDetector   → DomainDetector
    SpaCyPruner             → TextPruner    (relcl only)
    SpaCyDeepPruner         → TextPruner    (relcl + advcl — Developer tier)
    NullPruner              → TextPruner    (no-op when spaCy unavailable)

  Tier compressors (Open/Closed — new tier = new class, zero edits to Engine):
    StandardCompressor      → TierCompressor
    ProCompressor           → TierCompressor (wraps StandardCompressor)
    DeveloperCompressor     → TierCompressor (wraps ProCompressor)

  Orchestrator (Dependency-Inversion — depends only on interfaces):
    PromptolianEngine       — injects lang_detector, token_counter, tiers dict

  Factory:
    make_engine()           — wires defaults; callers never touch internals

Backward compatibility
----------------------
  All v3/v4 exports unchanged:
    from engine_v4 import PromptolianEngine, CompressResult,
                          compress, count_tokens, detect_language, ...
"""

from __future__ import annotations

import math
import re
import uuid
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

# ── Import rule sets + v3 utilities (re-exported for callers) ─────────────────
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))

from engine_v3 import (
    SYMBOL_RULES, ES_SYMBOL_RULES, ZH_SYMBOL_RULES,
    GRAMMAR, SIMPLIFY, DECODE, TECH_TERMS,
    _lean_pass, _abbrev_pass,
    extract_protected, protect, restore,
    count_tokens, extract_facts, fact_preservation_rate,
    decode_symbols, semantic_sim,
    compress as _v3_compress,
)

# ── Try loading optional heavy dependencies at module level ───────────────────
try:
    import spacy as _spacy
    _NLP_SM = _spacy.load('en_core_web_sm')
    _SPACY_AVAILABLE = True
except Exception:
    _NLP_SM = None
    _SPACY_AVAILABLE = False

try:
    import tiktoken as _tiktoken
    _enc = _tiktoken.get_encoding('cl100k_base')
    _TIKTOKEN_AVAILABLE = True
except Exception:
    _enc = None
    _TIKTOKEN_AVAILABLE = False

# Groq kept for backward compat only
try:
    from groq import Groq as _GroqClient  # noqa: F401
except ImportError:
    _GroqClient = None


# ══════════════════════════════════════════════════════════════════════════════
# RULE SETS  (unchanged from original)
# ══════════════════════════════════════════════════════════════════════════════

PRO_MATH = [
    (r'(?:grew|increased|went\s+up|rose)\s+from\s+([\d.,]+(?:\s*(?:million|billion|thousand|[KMB]))?)\s+to\s+([\d.,]+(?:\s*(?:million|billion|thousand|[KMB]))?)',
     r'\1→\2', re.I),
    (r'(?:fell|dropped|declined|decreased|went\s+down)\s+from\s+([\d.,]+(?:\s*(?:million|billion|thousand|[KMB]))?)\s+to\s+([\d.,]+(?:\s*(?:million|billion|thousand|[KMB]))?)',
     r'\1→\2↓', re.I),
    (r'\bgreater than or equal to\b',   '≥',   re.I),
    (r'\bless than or equal to\b',      '≤',   re.I),
    (r'\bnot equal to\b',               '≠',   re.I),
    (r'\bapproximately equal to\b',     '≈',   re.I),
    (r'\bapproximately\b',              '≈',   re.I),
    (r'\bplus or minus\b',              '±',   re.I),
    (r'\btimes(?=\s+\d)',               '×',   re.I),
    (r'\bmonth[- ]over[- ]month\b',     'MoM', re.I),
    (r'\bweek[- ]over[- ]week\b',       'WoW', re.I),
    (r'\bday[- ]over[- ]day\b',         'DoD', re.I),
    (r'\bversus\b',                     'vs',  re.I),
    (r'\bcompared (?:to|with)\b',       'vs',  re.I),
    (r'\bin comparison (?:to|with)\b',  'vs',  re.I),
    (r'\brelative to\b',                'vs',  re.I),
]

PRO_NUMERIC = [
    (r'\b(\d+(?:\.\d+)?)\s*million\b',      r'\g<1>M',   re.I),
    (r'\b(\d+(?:\.\d+)?)\s*billion\b',      r'\g<1>B',   re.I),
    (r'\b(\d+(?:\.\d+)?)\s*thousand\b',     r'\g<1>K',   re.I),
    (r'\b(\d+(?:\.\d+)?)\s*percent\b',      r'\g<1>%',   re.I),
    (r'\b(\d+)\s*milliseconds?\b',          r'\g<1>ms',  re.I),
    (r'\b(\d+)\s*seconds?\b(?!\s+(?:per|each|every))', r'\g<1>s', re.I),
    (r'\b(\d+)\s*minutes?\b(?!\s+(?:per|each|every))', r'\g<1>min', re.I),
    (r'\b(\d+)\s*hours?\b(?!\s+(?:per|each|every))',   r'\g<1>h',  re.I),
    (r'\b(\d+)\s*days?\b(?!\s+(?:per|each|every))',    r'\g<1>d',  re.I),
    (r'\b(\d+)\s*weeks?\b',                r'\g<1>wk',  re.I),
    (r'\b(\d+)\s*months?\b(?!\s+(?:per|each|every))', r'\g<1>mo', re.I),
    (r'\b(\d+)\s*years?\b(?!\s+(?:per|each|every))',  r'\g<1>yr', re.I),
]

PRO_OPENERS = [
    (r'\bIt is (?:important|critical|essential|crucial|key) to note that\s*', 'Note: ', re.I),
    (r'\bIt should be noted that\s*',   'Note: ', re.I),
    (r'\bPlease note that\s*',          'Note: ', re.I),
    (r'\bNote that\s*',                 'Note: ', re.I),
    (r'\bPlease be aware that\s*',      'Note: ', re.I),
    (r'\bIt is worth (?:noting|mentioning) that\s*', 'Note: ', re.I),
    (r'\bKeep in mind that\s*',         'Note: ', re.I),
    (r'\bBear in mind that\s*',         'Note: ', re.I),
    (r'\bFor your (?:information|reference),?\s*', 'FYI: ', re.I),
    (r'\bAs you (?:may|might|should|can) know,?\s*', '', re.I),
    (r'\bAs (?:I|we) (?:mentioned|noted|discussed|said) (?:before|earlier|previously|above),?\s*', '', re.I),
    (r'\bWith that (?:being )?said,?\s*', '', re.I),
    (r'\bHaving said that,?\s*',        '', re.I),
    (r'\bThat being said,?\s*',         '', re.I),
    (r'\bIn (?:light|view) of (?:this|the above),?\s*', '', re.I),
    (r'\bFor (?:context|reference|background)[,:\s]+', '§CTX ', re.I),
    (r'\bTo (?:summarize|recap|put it simply)[,:\s]+', '∑: ', re.I),
    (r'\bIn (?:summary|conclusion)[,:\s]+', '∑: ', re.I),
    (r'\bTo be (?:more )?(?:specific|precise|clear)[,:\s]+', '', re.I),
    (r'\bAs a (?:quick )?reminder,?\s*', '', re.I),
    (r'\bJust (?:a )?(?:quick )?note[,:\s]+', 'Note: ', re.I),
]

PRO_VERBOSE = [
    (r'\bhas the ability to\b',         'can',        re.I),
    (r'\bis (?:able|capable) to\b',     'can',        re.I),
    (r'\bwas (?:able|capable) to\b',    'could',      re.I),
    (r'\bmake (?:use of|usage of)\b',   'use',        re.I),
    (r'\btake into (?:account|consideration)\b', 'consider', re.I),
    (r'\bcarry out\b',                  'do',         re.I),
    (r'\bcome up with\b',               'find',       re.I),
    (r'\bpoint out\b',                  'note',       re.I),
    (r'\blook into\b',                  'check',      re.I),
    (r'\bput forward\b',                'propose',    re.I),
    (r'\bkeep in mind\b',               'remember',   re.I),
    (r'\bat the end of the day\b',      'ultimately', re.I),
    (r'\bfrom the (?:perspective|standpoint|point of view) of\b', 're:', re.I),
    (r'\bin (?:terms|respect) of\b',    're:',        re.I),
    (r'\bpertaining to\b',              're:',        re.I),
    (r'\bin (?:light|view) of\b',       'given',      re.I),
    (r'\bon (?:a|the) regular basis\b', 'regularly',  re.I),
    (r'\bon (?:a|the) monthly basis\b', 'monthly',    re.I),
    (r'\bon (?:a|the) weekly basis\b',  'weekly',     re.I),
    (r'\bat (?:the )?(?:this )?(?:point )?in time\b', 'now', re.I),
    (r'\bso as to\b',                   'to',         re.I),
    (r'\bwith the (?:goal|aim|objective|intention) of\b', 'to', re.I),
    (r'\bunder (?:the )?(?:circumstances|conditions) (?:where|that|when)\b', 'if', re.I),
    (r'\bby (?:means|way) of\b',        'via',        re.I),
    (r'\bthat is to say\b',             'i.e.',       re.I),
    (r'\bnamely\b',                     'i.e.',       re.I),
    (r'\balong with\b',                 '+',          re.I),
    (r'\btogether with\b',              '+',          re.I),
    (r'\bincluding but not limited to\b', 'incl.',    re.I),
    (r'\bnot limited to\b',             'incl.',      re.I),
    (r'\ba (?:total|sum) of\b',         '',           re.I),
]

PRO_RELCL = [
    (r',\s*which\s+(?:is|are|was|were)\s+(?:a |an |the )?[^,\.;]{3,45}(?=[,\.])', '', re.I),
    (r',\s*which\s+(?:has|have|had)\s+[^,\.;]{3,45}(?=[,\.])',                    '', re.I),
    (r',\s*(?:also\s+)?(?:known as|referred to as|called)\s+[^,\.;]{2,30}',       '', re.I),
    (r',\s*(?:providing|enabling|allowing|requiring|using|making)\s+the\s+\w+',   '', re.I),
]

TELE_RULES = [
    (r'\bCould you (?:please )?(?=[a-z])',                '',   re.I),
    (r'\bWould you (?:please )?(?=[a-z])',                '',   re.I),
    (r'\bCan you (?:please )?(?=[a-z])',                  '',   re.I),
    (r'\bWill you (?:please )?(?=[a-z])',                 '',   re.I),
    (r'(?m)^Please (?=[A-Z])',                            '',   0),
    (r'\bPlease (?=[a-z])',                               '',   re.I),
    (r'\bKindly (?=[a-z])',                               '',   re.I),
    (r'\bFeel free to\s+',                               '',   re.I),
    (r'\bMake sure (?:to\s+|that\s+)?',                  '',   re.I),
    (r"\bI(?:'d| would) like (?:you )?to\s+",           '',   re.I),
    (r'\bI (?:want|need) (?:you )?to\s+',               '',   re.I),
    (r'\bYour (?:task|job|goal|objective|mission) is to\s+', '', re.I),
    (r'\bYou (?:should|must|need to|have to)\s+',        '',   re.I),
    (r'\bIn this (?:task|exercise|scenario)[,:]?\s+',    '',   re.I),
    (r'\bFor this (?:task|exercise|request)[,:]?\s+',    '',   re.I),
]

DOMAIN_PACKS: dict[str, list] = {
    'finance': [
        (r'\bmonthly recurring revenue\b',        'MRR',   re.I),
        (r'\bannual recurring revenue\b',         'ARR',   re.I),
        (r'\bcustomer acquisition cost\b',        'CAC',   re.I),
        (r'\bcustomer lifetime value\b',          'LTV',   re.I),
        (r'\bnet revenue retention\b',            'NRR',   re.I),
        (r'\bnet promoter score\b',               'NPS',   re.I),
        (r'\bpercentage point(?:s)?\b',           'pp',    re.I),
        (r'\bquarter[- ]over[- ]quarter\b',       'QoQ',   re.I),
        (r'\byear[- ]over[- ]year\b',             'YoY',   re.I),
        (r'\bkey performance indicator(?:s)?\b',  'KPI',   re.I),
        (r'\breturn on investment\b',             'ROI',   re.I),
        (r'\bprofit and loss\b',                  'P&L',   re.I),
        (r'\bearnings per share\b',               'EPS',   re.I),
        (r'\bprice[- ]to[- ]earnings\b',          'P/E',   re.I),
        (r'\bfree cash flow\b',                   'FCF',   re.I),
        (r'\bgross merchandise value\b',          'GMV',   re.I),
        (r'\btotal addressable market\b',         'TAM',   re.I),
        (r'\bserviceable addressable market\b',   'SAM',   re.I),
        (r'\bearnings before interest.*?taxes.*?depreciation.*?amortization\b', 'EBITDA', re.I),
        (r'\bearnings before interest and taxes\b', 'EBIT', re.I),
    ],
    'legal': [
        (r'\bnon-?disclosure agreement\b',            'NDA',  re.I),
        (r'\bintellectual property\b',                'IP',   re.I),
        (r'\blimited liability company\b',            'LLC',  re.I),
        (r'\bterms and conditions\b',                 'T&C',  re.I),
        (r'\bservice level agreement\b',              'SLA',  re.I),
        (r'\bpersonally identifiable information\b',  'PII',  re.I),
        (r'\bgeneral data protection regulation\b',   'GDPR', re.I),
        (r'\bstatement of work\b',                    'SOW',  re.I),
        (r'\bmaster service agreement\b',             'MSA',  re.I),
        (r'\bpursuant to\b',                         'per',  re.I),
        (r'\bnotwithstanding\b',                     'despite', re.I),
        (r'\bhereinafter referred to as\b',          '=',    re.I),
    ],
    'medical': [
        (r'\bblood pressure\b',                      'BP',    re.I),
        (r'\bheart rate\b',                          'HR',    re.I),
        (r'\bbody mass index\b',                     'BMI',   re.I),
        (r'\bcomplete blood count\b',                'CBC',   re.I),
        (r'\belectrocardiogram\b',                   'ECG',   re.I),
        (r'\bmagnetic resonance imaging\b',          'MRI',   re.I),
        (r'\bcomputed tomography\b',                 'CT',    re.I),
        (r'\bwhite blood cell(?:s)?\b',              'WBC',   re.I),
        (r'\bred blood cell(?:s)?\b',                'RBC',   re.I),
        (r'\btype 2 diabetes\b',                     'T2D',   re.I),
        (r'\bsystolic blood pressure\b',             'SBP',   re.I),
        (r'\bdiastolic blood pressure\b',            'DBP',   re.I),
        (r'\bangiotensin.converting enzyme\b',       'ACE',   re.I),
        (r'\bangiotensin receptor blocker\b',        'ARB',   re.I),
        (r'\bnonsteroidal anti.inflammatory\b',      'NSAID', re.I),
    ],
    'programming': [
        (r'\bapplication programming interface\b',   'API',   re.I),
        (r'\brepresentational state transfer\b',     'REST',  re.I),
        (r'\bcontinuous integration(?:/continuous deployment)?\b', 'CI/CD', re.I),
        (r'\bcommand[- ]line interface\b',           'CLI',   re.I),
        (r'\bpull request\b',                        'PR',    re.I),
        (r'\bdatabase\b',                            'DB',    re.I),
        (r'\bobject.oriented programming\b',         'OOP',   re.I),
        (r'\btest.driven development\b',             'TDD',   re.I),
        (r'\bbehavior.driven development\b',         'BDD',   re.I),
        (r'\bmicroservice(?:s)?\b',                  'µsvc',  re.I),
        (r'\bkubernetes\b',                          'K8s',   re.I),
        (r'\bopen.source\b',                         'OSS',   re.I),
    ],
    'marketing': [
        (r'\bcall to action\b',                      'CTA',  re.I),
        (r'\bsearch engine optimization\b',          'SEO',  re.I),
        (r'\bsearch engine marketing\b',             'SEM',  re.I),
        (r'\bclick[- ]through rate\b',               'CTR',  re.I),
        (r'\bconversion rate optimization\b',        'CRO',  re.I),
        (r'\bcost per click\b',                      'CPC',  re.I),
        (r'\bcost per acquisition\b',                'CPA',  re.I),
        (r'\breturn on ad spend\b',                  'ROAS', re.I),
        (r'\bmonthly active user(?:s)?\b',           'MAU',  re.I),
        (r'\bdaily active user(?:s)?\b',             'DAU',  re.I),
        (r'\buser[- ]generated content\b',           'UGC',  re.I),
        (r'\blanding page\b',                        'LP',   re.I),
    ],
}

_DOMAIN_SIGNALS: dict[str, list[str]] = {
    'finance':     ['revenue','mrr','arr','churn','cac','ltv','profit','loss','ebitda',
                    'roi','quarterly','fiscal','funding','valuation','investor','cashflow'],
    'legal':       ['agreement','contract','clause','liability','jurisdiction','plaintiff',
                    'defendant','arbitration','indemnif','warrant','breach','nda','gdpr'],
    'medical':     ['patient','diagnosis','treatment','symptom','clinical','dosage',
                    'medication','therapy','blood pressure','surgery','physician','prognosis'],
    'programming': ['function','class','api','database','algorithm','bug','deploy','server',
                    'framework','endpoint','authentication','repository','docker','kubernetes'],
    'marketing':   ['campaign','audience','conversion','engagement','brand','content',
                    'social media','traffic','seo','ctr','funnel','segment','influencer'],
}

_ADVCL_TRIGGERS = frozenset(['as', 'since', 'given', 'considering', 'noting', 'assuming'])


# ══════════════════════════════════════════════════════════════════════════════
# RESULT DATA CLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class CompressResult:
    """Immutable result from a single PromptolianEngine.compress() call."""
    original:        str
    compressed:      str
    tier:            str
    lang:            str
    cr:              float
    orig_tokens:     int
    comp_tokens:     int
    token_savings:   int
    groq_failed:     bool = field(default=False)   # deprecated, kept for compat
    fallback_reason: str  = field(default='')      # deprecated, kept for compat

    def __repr__(self) -> str:
        return (f"CompressResult(tier={self.tier!r}, lang={self.lang!r}, "
                f"cr={self.cr:.1f}%, tokens={self.orig_tokens}→{self.comp_tokens})")


# ══════════════════════════════════════════════════════════════════════════════
# INTERFACES  (ISP — each protocol is minimal; DIP — Engine depends on these)
# ══════════════════════════════════════════════════════════════════════════════

@runtime_checkable
class TierCompressor(Protocol):
    """S: single job — compress text for one tier."""
    def compress(self, text: str, lang: str) -> str: ...


@runtime_checkable
class LanguageDetector(Protocol):
    """S: single job — detect the language of a text."""
    def detect(self, text: str) -> str: ...


@runtime_checkable
class TokenCounter(Protocol):
    """S: single job — count tokens for a language."""
    def count(self, text: str, lang: str = 'en') -> int: ...


@runtime_checkable
class TextPruner(Protocol):
    """S: single job — prune syntactic fluff from text."""
    def prune(self, text: str) -> str: ...


@runtime_checkable
class DomainDetector(Protocol):
    """S: single job — detect the domain of a text."""
    def detect(self, text: str) -> Optional[str]: ...


# ══════════════════════════════════════════════════════════════════════════════
# CONCRETE IMPLEMENTATIONS  (SRP — each class has exactly one reason to change)
# ══════════════════════════════════════════════════════════════════════════════

class RegexLanguageDetector:
    """Lightweight language detection via vocabulary heuristics."""

    _PATTERNS = {
        'es': re.compile(r'\b(?:el|la|los|las|es|en|de|un|una|para|con|por|que|se|no)\b', re.I),
        'fr': re.compile(r'\b(?:le|la|les|un|une|des|du|est|et|je|tu|nous|vous|ils|que|pas)\b', re.I),
        'de': re.compile(r'\b(?:der|die|das|ein|eine|ist|und|ich|du|wir|sie|für|mit|von|zu|nicht)\b', re.I),
        'it': re.compile(r'\b(?:il|la|i|le|un|una|dei|del|della|che|non|per|con|sono|è|da)\b', re.I),
    }
    _THRESHOLD = 0.08

    def detect(self, text: str) -> str:
        words = max(len(text.split()), 1)
        scores = {lang: len(pat.findall(text)) for lang, pat in self._PATTERNS.items()}
        best = max(scores, key=scores.get)
        return best if scores[best] / words > self._THRESHOLD else 'en'


class TiktokenTokenCounter:
    """BPE token counting via tiktoken (accurate)."""

    def __init__(self) -> None:
        self._enc = _enc  # module-level encoding loaded once

    def count(self, text: str, lang: str = 'en') -> int:
        if lang == 'zh':
            return _count_tokens_zh(text)
        return max(1, len(self._enc.encode(text)))


class FallbackTokenCounter:
    """Word-split × 1.3 heuristic — used when tiktoken is unavailable."""

    def count(self, text: str, lang: str = 'en') -> int:
        if lang == 'zh':
            return _count_tokens_zh(text)
        words = re.split(r'[\s,.:;!?()\[\]{}"\']+', text.strip())
        return max(1, math.ceil(len([w for w in words if w]) * 1.3))


class KeywordDomainDetector:
    """Score-based domain detection from _DOMAIN_SIGNALS keyword lists."""

    _MIN_SCORE = 2

    def detect(self, text: str) -> Optional[str]:
        lower = text.lower()
        scores = {
            dom: sum(1 for kw in kws if kw in lower)
            for dom, kws in _DOMAIN_SIGNALS.items()
        }
        best = max(scores, key=scores.get)
        return best if scores[best] >= self._MIN_SCORE else None


class NullPruner:
    """No-op pruner — satisfies TextPruner when spaCy is unavailable."""

    def prune(self, text: str) -> str:
        return text


class SpaCyPruner:
    """Remove non-restrictive relative clauses using spaCy dep-parse (Standard/Pro)."""

    def __init__(self, nlp) -> None:
        self._nlp = nlp

    def prune(self, text: str) -> str:
        try:
            doc = self._nlp(text)
            spans: list[tuple[int, int]] = []
            for token in doc:
                if token.dep_ == 'relcl':
                    sub = list(token.subtree)
                    s = sub[0].idx
                    e = sub[-1].idx + len(sub[-1].text)
                    if s > 0 and text[s - 2:s].strip() == ',':
                        spans.append((s - 2, e))
            for s, e in sorted(spans, reverse=True):
                text = text[:s] + text[e:]
            return text.strip()
        except Exception:
            return text


class SpaCyDeepPruner:
    """Remove relcl + low-information advcl clauses (Developer tier)."""

    def __init__(self, nlp) -> None:
        self._nlp = nlp

    def prune(self, text: str) -> str:
        try:
            doc = self._nlp(text)
            spans: list[tuple[int, int]] = []
            for token in doc:
                if token.dep_ == 'relcl':
                    sub = list(token.subtree)
                    s = sub[0].idx
                    e = sub[-1].idx + len(sub[-1].text)
                    if s > 0 and text[s - 2:s].strip() == ',':
                        spans.append((s - 2, e))
                elif token.dep_ == 'advcl':
                    sub = list(token.subtree)
                    if sub[0].text.lower() in _ADVCL_TRIGGERS:
                        s = sub[0].idx
                        e = sub[-1].idx + len(sub[-1].text)
                        comma_start = s - 2 if s >= 2 and text[s - 2] == ',' else s
                        spans.append((comma_start, e))
            for s, e in sorted(spans, reverse=True):
                text = text[:s] + text[e:]
            return text.strip()
        except Exception:
            return text


# ══════════════════════════════════════════════════════════════════════════════
# TIER COMPRESSORS  (OCP — add a new tier by creating a new class, not editing)
# ══════════════════════════════════════════════════════════════════════════════

class StandardCompressor:
    """
    Standard tier: symbol substitution + grammar stripping + lean/abbrev passes.
    Delegates to the stable engine_v3 pipeline — no duplication.
    """

    def compress(self, text: str, lang: str) -> str:
        return _v3_compress(text, lang=lang)


class ProCompressor:
    """
    Pro tier: wraps StandardCompressor and applies 7 English-only passes.
    Injected standard compressor satisfies LSP — any TierCompressor works here.
    """

    def __init__(self, standard: TierCompressor, pruner: TextPruner) -> None:
        self._standard = standard
        self._pruner   = pruner

    def compress(self, text: str, lang: str) -> str:
        if lang != 'en':
            return self._standard.compress(text, lang)

        t = self._apply_rules(text, PRO_OPENERS)   # P1 opener removal
        t = self._apply_rules(t,    PRO_VERBOSE)    # P2 verbose phrases
        t = self._tele_pass(t)                      # P3 telegraphic stripping
        t = self._apply_rules(t,    PRO_MATH)       # P4 math operators
        t = self._apply_rules(t,    PRO_NUMERIC)    # P5 unit abbreviations
        t = self._standard.compress(t, lang)        # Standard pipeline
        t = self._pruner.prune(t)                   # P6 spaCy relcl
        t = self._apply_rules(t,    PRO_RELCL)      # P6b regex relcl fallback
        return self._collapse_whitespace(t)

    @staticmethod
    def _apply_rules(text: str, rules: list) -> str:
        for pat, repl, flags in rules:
            text = re.sub(pat, repl, text, flags=flags)
        return text

    @staticmethod
    def _tele_pass(text: str) -> str:
        for pat, repl, flags in TELE_RULES:
            text = re.sub(pat, repl, text, flags=flags)
        text = re.sub(r'(?<=[.!?] )([a-z])', lambda m: m.group(1).upper(), text)
        if text and text[0].islower():
            text = text[0].upper() + text[1:]
        return text

    @staticmethod
    def _collapse_whitespace(text: str) -> str:
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


class DeveloperCompressor:
    """
    Developer tier: wraps ProCompressor and applies domain + deep-NLP passes.
    DomainDetector and TextPruner are injected — swappable without touching this class.
    """

    def __init__(
        self,
        pro:             TierCompressor,
        domain_detector: DomainDetector,
        deep_pruner:     TextPruner,
    ) -> None:
        self._pro             = pro
        self._domain_detector = domain_detector
        self._deep_pruner     = deep_pruner

    def compress(self, text: str, lang: str) -> str:
        t = self._pro.compress(text, lang)
        if lang != 'en':
            return t

        domain = self._domain_detector.detect(text)
        if domain:
            t = self._apply_domain_pack(t, domain)  # D1 domain abbreviations

        t = self._deep_pruner.prune(t)               # D2 deep spaCy pruning
        t = self._parenthetical_pruner(t)            # D3 parenthetical removal

        t = re.sub(r'[ \t]{2,}', ' ', t)
        t = re.sub(r'\n{3,}', '\n\n', t)
        return t.strip()

    @staticmethod
    def _apply_domain_pack(text: str, domain: str) -> str:
        for pat, repl, flags in DOMAIN_PACKS.get(domain, []):
            text = re.sub(pat, repl, text, flags=flags)
        return text

    @staticmethod
    def _parenthetical_pruner(text: str) -> str:
        def _safe(content: str) -> bool:
            if re.search(r'\d',           content): return False
            if re.search(r'https?://',    content): return False
            if re.search(r'[/\\]|\bdef\b|\bclass\b', content): return False
            return len(content.split()) >= 4

        return re.sub(
            r'\s*\(([^)]{10,80})\)',
            lambda m: '' if _safe(m.group(1)) else m.group(0),
            text,
        )


# ══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR  (DIP — depends only on interfaces, not concrete classes)
# ══════════════════════════════════════════════════════════════════════════════

class PromptolianEngine:
    """
    Thin orchestrator: detects language, runs the requested tier compressor,
    measures tokens, and returns a CompressResult.

    All heavy lifting is delegated to injected collaborators — the engine
    itself is closed for modification (OCP) and can be extended by passing
    new tiers or detectors via the constructor.

    Parameters
    ----------
    lang_detector : LanguageDetector
    token_counter : TokenCounter
    tiers         : dict mapping tier name → TierCompressor
                    Default tiers: 'standard', 'pro', 'developer'
    """

    def __init__(
        self,
        lang_detector: LanguageDetector,
        token_counter: TokenCounter,
        tiers:         dict[str, TierCompressor],
    ) -> None:
        self._lang_detector = lang_detector
        self._token_counter = token_counter
        self._tiers         = tiers

    @property
    def available_tiers(self) -> tuple[str, ...]:
        return tuple(self._tiers)

    # ── Public API ────────────────────────────────────────────────────────────

    def compress(self, text: str, tier: str = 'standard', lang: str = 'auto') -> CompressResult:
        if tier not in self._tiers:
            raise ValueError(f"Unknown tier {tier!r}. Available: {self.available_tiers}")

        lang       = self._lang_detector.detect(text) if lang == 'auto' else lang
        orig_tok   = self._token_counter.count(text, lang)
        compressed = self._tiers[tier].compress(text, lang)
        comp_tok   = self._token_counter.count(compressed, lang)
        cr         = round(max(0.0, (1 - comp_tok / orig_tok) * 100), 1)

        return CompressResult(
            original      = text,
            compressed    = compressed,
            tier          = tier,
            lang          = lang,
            cr            = cr,
            orig_tokens   = orig_tok,
            comp_tokens   = comp_tok,
            token_savings = orig_tok - comp_tok,
        )

    def compress_all_tiers(self, text: str, lang: str = 'auto') -> dict[str, CompressResult]:
        lang = self._lang_detector.detect(text) if lang == 'auto' else lang
        return {t: self.compress(text, tier=t, lang=lang) for t in self._tiers}

    def fact_pres(self, original: str, compressed: str) -> float:
        _, _, rate, _ = fact_preservation_rate(original, decode_symbols(compressed))
        return rate

    def decode(self, text: str) -> str:
        return decode_symbols(text)

    def token_count(self, text: str, lang: str = 'en') -> int:
        return self._token_counter.count(text, lang)


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY  (wires default collaborators — callers import make_engine() or
#           PromptolianEngine() for custom wiring)
# ══════════════════════════════════════════════════════════════════════════════

def make_engine() -> PromptolianEngine:
    """
    Build a PromptolianEngine with production defaults.
    Automatically selects the best available TokenCounter and TextPruner.
    """
    lang_detector  = RegexLanguageDetector()
    token_counter: TokenCounter = (
        TiktokenTokenCounter() if _TIKTOKEN_AVAILABLE else FallbackTokenCounter()
    )
    domain_detector = KeywordDomainDetector()

    # Pruners: use spaCy when available, NullPruner otherwise
    if _SPACY_AVAILABLE and _NLP_SM is not None:
        pruner      = SpaCyPruner(_NLP_SM)
        deep_pruner = SpaCyDeepPruner(_NLP_SM)
    else:
        pruner      = NullPruner()
        deep_pruner = NullPruner()

    standard   = StandardCompressor()
    pro        = ProCompressor(standard=standard, pruner=pruner)
    developer  = DeveloperCompressor(pro=pro, domain_detector=domain_detector,
                                     deep_pruner=deep_pruner)

    return PromptolianEngine(
        lang_detector = lang_detector,
        token_counter = token_counter,
        tiers = {
            'standard':  standard,
            'pro':       pro,
            'developer': developer,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _count_tokens_zh(text: str) -> int:
    zh_chars   = sum(1 for c in text if '一' <= c <= '鿿')
    ascii_part = re.sub(r'[一-鿿]', ' ', text)
    ascii_words = [w for w in re.split(r'[\s,.:;!?()\[\]{}"\']+', ascii_part.strip()) if w]
    return max(1, zh_chars + len(ascii_words))


# Standalone detect_language — kept as module-level function for backward compat
def detect_language(text: str) -> str:
    return RegexLanguageDetector().detect(text)


# ══════════════════════════════════════════════════════════════════════════════
# BACKWARD-COMPAT MODULE-LEVEL EXPORTS  (drop-in for engine_v3 / old engine_v4)
# ══════════════════════════════════════════════════════════════════════════════

_default_engine = make_engine()


def compress(text: str, lang: str = 'en') -> str:
    return _default_engine.compress(text, tier='standard', lang=lang).compressed


def compress_pro(text: str, lang: str = 'en') -> str:
    return _default_engine.compress(text, tier='pro', lang=lang).compressed


def compress_developer(text: str, lang: str = 'en') -> str:
    return _default_engine.compress(text, tier='developer', lang=lang).compressed


def compress_adaptive(text: str, lang: str = 'en'):
    from engine_v3 import compress_adaptive as _v3_adap
    return _v3_adap(text, lang=lang)


__all__ = [
    # Classes
    'PromptolianEngine', 'CompressResult',
    # Interfaces (for custom wiring / testing)
    'TierCompressor', 'LanguageDetector', 'TokenCounter', 'TextPruner', 'DomainDetector',
    # Concrete implementations
    'StandardCompressor', 'ProCompressor', 'DeveloperCompressor',
    'RegexLanguageDetector', 'TiktokenTokenCounter', 'FallbackTokenCounter',
    'KeywordDomainDetector', 'SpaCyPruner', 'SpaCyDeepPruner', 'NullPruner',
    # Factory
    'make_engine',
    # Compression functions (backward compat)
    'compress', 'compress_pro', 'compress_developer', 'compress_adaptive',
    # Utilities
    'count_tokens', '_count_tokens_zh', 'detect_language',
    'protect', 'restore', 'extract_protected',
    'fact_preservation_rate', 'extract_facts',
    'decode_symbols', 'semantic_sim',
    # Rule sets (re-exported for external use)
    'SYMBOL_RULES', 'ZH_SYMBOL_RULES', 'ES_SYMBOL_RULES',
    'GRAMMAR', 'SIMPLIFY', 'DECODE', 'TECH_TERMS',
    'PRO_MATH', 'PRO_NUMERIC', 'PRO_OPENERS', 'PRO_VERBOSE', 'PRO_RELCL',
    'TELE_RULES', 'DOMAIN_PACKS', '_DOMAIN_SIGNALS',
]