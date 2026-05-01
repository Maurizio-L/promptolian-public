// Promptly — background service worker

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    promptly_enabled: true,
    promptly_tokens_saved: 0,
    promptly_prompts: 0,
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'STATS') {
    chrome.storage.local.get(['promptly_tokens_saved', 'promptly_prompts'], (res) => {
      chrome.storage.local.set({
        promptly_tokens_saved: (res.promptly_tokens_saved || 0) + (msg.saved || 0),
        promptly_prompts: (res.promptly_prompts || 0) + 1,
      });
    });
  }
});
