(function () {
  'use strict';

  // ── Compression engine ────────────────────────────────────────────────────
  const PS_RULES = [
    [/you are an? expert (in |on |at )?/gi, '§EXP '],
    [/you are an? /gi, '§ROLE '],
    [/please /gi, '§ACT '],
    [/return only the code[^.]*\./gi, '→code'],
    [/return as (a )?bullet list/gi, '→list'],
    [/return as (a )?table/gi, '→table'],
    [/return as json/gi, '→json'],
    [/return as markdown/gi, '→md'],
    [/step[- ]by[- ]step/gi, '→step'],
    [/pros and cons/gi, '→pros/cons'],
    [/be concise[^.]*\./gi, '→short'],
    [/\bsummarize\b/gi, '∑'],
    [/\bsummary\b/gi, '∑'],
    [/\bexplain\b/gi, '?'],
    [/\bhow does\b/gi, '?'],
    [/\bwhat is\b/gi, '?'],
    [/\boptimize\b/gi, 'OPT'],
    [/\bperformance\b/gi, 'OPT'],
    [/\bdebug\b/gi, 'BUG'],
    [/\bfix (the |any |a )?bug(s)?\b/gi, 'BUG'],
    [/\bfunction\b/gi, 'FN'],
    [/\bmethod\b/gi, 'FN'],
    [/\bclass\b/gi, 'CLS'],
    [/\bdatabase\b/gi, 'DB'],
    [/\bunit test(s)?\b/gi, 'TEST'],
    [/\btest(s)?\b/gi, 'TEST'],
    [/\binclude\b/gi, '⊕'],
    [/\bwith\b/gi, '⊕'],
    [/\bexclude\b/gi, '⊖'],
    [/\bwithout\b/gi, '⊖'],
    [/\bno explanation\b/gi, '⊖explain'],
    [/\bdo not\b/gi, '§NOT'],
    [/\bdon't\b/gi, '§NOT'],
    [/\bavoid\b/gi, '§NOT'],
    [/\bnever\b/gi, '§NOT'],
    [/\balso\b/gi, '§ALSO'],
    [/\badditionally\b/gi, '§ALSO'],
    [/\balternative(ly)?\b/gi, '§ALT'],
    [/\binstead\b/gi, '§ALT'],
    [/\bbest practice(s)?\b/gi, '§BEST'],
    [/\brecommended\b/gi, '§BEST'],
    [/\bcompare\b/gi, '§DIFF'],
    [/\bdifference between\b/gi, '§DIFF'],
    [/\bcontext\b/gi, '§CTX'],
    [/\bbackground\b/gi, '§CTX'],
    [/\bfor example\b/gi, '§EX'],
    [/\bfor instance\b/gi, '§EX'],
    [/\bexample\b/gi, '§EX'],
    [/\boutput\b/gi, '→'],
    [/\breturn\b/gi, '→'],
    [/\bproduce\b/gi, '→'],
    [/\bgenerate\b/gi, '→'],
    [/\bgiven\b/gi, '←'],
    [/\binput\b/gi, '←'],
    [/\bimportant\b/gi, '!!'],
    [/\bcritical\b/gi, '!!'],
    [/\bmust\b/gi, '!!'],
    [/\bedit\b/gi, '∆'],
    [/\bimprove\b/gi, '∆'],
    [/\bupdate\b/gi, '∆'],
    [/\bchange\b/gi, '∆'],
    [/\breview\b/gi, '«'],
    [/\brevise\b/gi, '«'],
    [/\bcontinue\b/gi, '»'],
    [/\bpython\b/gi, 'py'],
    [/\bjavascript\b/gi, 'js'],
    [/\btypescript\b/gi, 'ts'],
    [/\bsimply\b/gi, '→ELI5'],
    [/\bsimple language\b/gi, '→ELI5'],
    [/\bbriefly\b/gi, '→short'],
    [/\bconcise(ly)?\b/gi, '→short'],
    [/\bdetailed\b/gi, '→long'],
    [/\bcomprehensive\b/gi, '→long'],
    [/\ball\b/gi, '∀'],
    [/\beach\b/gi, '∀'],
    [/\bevery\b/gi, '∀'],
    [/ +/g, ' '],
  ];

  function psEncode(text) {
    let out = text.trim();
    for (const [pat, rep] of PS_RULES) out = out.replace(pat, rep);
    return out.replace(/\. /g, '.\n').replace(/  +/g, ' ').trim();
  }

  function countTokens(text) {
    return Math.ceil(text.trim().split(/[\s,.:;!?()\[\]{}"']+/).filter(Boolean).length * 1.3);
  }

  // ── Site selectors ────────────────────────────────────────────────────────
  const SITES = {
    'claude.ai': {
      input: '[contenteditable="true"]',
      getText: (el) => el.innerText,
      setText: (el, t) => { el.focus(); document.execCommand('selectAll', false, null); document.execCommand('insertText', false, t); },
    },
    'chatgpt.com': {
      input: '#prompt-textarea',
      getText: (el) => el.value || el.innerText,
      setText: (el, t) => { el.focus(); const s = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set; s.call(el, t); el.dispatchEvent(new Event('input', { bubbles: true })); },
    },
    'chat.openai.com': {
      input: '#prompt-textarea',
      getText: (el) => el.value || el.innerText,
      setText: (el, t) => { el.focus(); const s = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set; s.call(el, t); el.dispatchEvent(new Event('input', { bubbles: true })); },
    },
    'gemini.google.com': {
      input: '.ql-editor',
      getText: (el) => el.innerText,
      setText: (el, t) => { el.focus(); document.execCommand('selectAll', false, null); document.execCommand('insertText', false, t); },
    },
    'copilot.microsoft.com': {
      input: 'textarea[placeholder]',
      getText: (el) => el.value,
      setText: (el, t) => { el.focus(); const s = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set; s.call(el, t); el.dispatchEvent(new Event('input', { bubbles: true })); },
    },
  };

  function getSite() {
    const host = location.hostname;
    for (const [k, v] of Object.entries(SITES)) {
      if (host.includes(k)) return v;
    }
    return null;
  }

  // ── State ─────────────────────────────────────────────────────────────────
  let isCompressed = false;
  let originalText = '';
  let sessionSaved = 0;

  // ── Toolbar ───────────────────────────────────────────────────────────────
  function injectToolbar() {
    if (document.getElementById('promptly-bar')) return;
    const site = getSite();
    if (!site) return;

    const bar = document.createElement('div');
    bar.id = 'promptly-bar';
    bar.innerHTML = `
      <span class="p-logo">P→</span>
      <span class="p-stats" id="p-stats">Promptly ready</span>
      <div class="p-actions">
        <button class="p-btn p-compress" id="p-compress">⚡ Compress</button>
        <button class="p-btn p-undo" id="p-undo" style="display:none">↩ Restore</button>
        <button class="p-btn p-toggle" id="p-toggle">ON</button>
      </div>
    `;
    document.body.appendChild(bar);

    // Load enabled state
    chrome.storage.local.get(['promptly_enabled'], (res) => {
      updateToggle(res.promptly_enabled !== false);
    });

    document.getElementById('p-compress').addEventListener('click', () => {
      const site = getSite();
      const input = document.querySelector(site.input);
      if (!input) return;
      const text = site.getText(input);
      if (!text.trim()) return;
      if (!isCompressed) {
        originalText = text;
        const compressed = psEncode(text);
        site.setText(input, compressed);
        const et = countTokens(text), pt = countTokens(compressed);
        const saved = Math.max(0, et - pt);
        const pct = et > 0 ? Math.round(saved / et * 100) : 0;
        sessionSaved += saved;
        document.getElementById('p-stats').innerHTML =
          `<span style="color:#4ade80;font-weight:600">${pct}% saved</span> · ${et}→${pt} tokens · session: ${sessionSaved}`;
        isCompressed = true;
        const btn = document.getElementById('p-compress');
        btn.textContent = '✓ Compressed';
        btn.style.opacity = '0.6';
        document.getElementById('p-undo').style.display = 'inline-flex';
        chrome.runtime.sendMessage({ type: 'STATS', saved: pct });
      }
    });

    document.getElementById('p-undo').addEventListener('click', () => {
      const site = getSite();
      const input = document.querySelector(site.input);
      if (!input || !originalText) return;
      site.setText(input, originalText);
      isCompressed = false;
      originalText = '';
      document.getElementById('p-compress').textContent = '⚡ Compress';
      document.getElementById('p-compress').style.opacity = '1';
      document.getElementById('p-undo').style.display = 'none';
      document.getElementById('p-stats').textContent = 'Promptly ready';
    });

    document.getElementById('p-toggle').addEventListener('click', () => {
      chrome.storage.local.get(['promptly_enabled'], (res) => {
        const next = !(res.promptly_enabled !== false);
        chrome.storage.local.set({ promptly_enabled: next });
        updateToggle(next);
      });
    });

    document.addEventListener('input', (e) => {
      const site = getSite();
      if (!site) return;
      const input = document.querySelector(site.input);
      if (e.target === input && isCompressed) {
        isCompressed = false;
        document.getElementById('p-compress').textContent = '⚡ Compress';
        document.getElementById('p-compress').style.opacity = '1';
        document.getElementById('p-undo').style.display = 'none';
      }
    });
  }

  function updateToggle(enabled) {
    const btn = document.getElementById('p-toggle');
    if (!btn) return;
    btn.textContent = enabled ? 'ON' : 'OFF';
    btn.style.background = enabled ? '#22c55e22' : 'transparent';
    btn.style.color = enabled ? '#16a34a' : 'inherit';
    const cb = document.getElementById('p-compress');
    if (cb) cb.style.display = enabled ? 'inline-flex' : 'none';
  }

  const obs = new MutationObserver(() => {
    if (!document.getElementById('promptly-bar')) setTimeout(injectToolbar, 600);
  });
  obs.observe(document.body, { childList: true, subtree: true });
  setTimeout(injectToolbar, 1000);
})();
