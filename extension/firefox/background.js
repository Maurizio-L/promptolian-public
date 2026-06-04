// Promptolian — background service worker

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    promptolian_enabled: true,
    promptolian_tokens_saved: 0,
    promptolian_prompts: 0,
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'STATS') {
    chrome.storage.local.get(['promptolian_tokens_saved', 'promptolian_pct_sum', 'promptolian_prompts'], (res) => {
      chrome.storage.local.set({
        promptolian_tokens_saved: (res.promptolian_tokens_saved || 0) + (msg.saved || 0),
        promptolian_pct_sum:      (res.promptolian_pct_sum      || 0) + (msg.pct   || 0),
        promptolian_prompts:      (res.promptolian_prompts      || 0) + 1,
      });
    });
  }
});
