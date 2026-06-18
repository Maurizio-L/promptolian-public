(function () {
  'use strict';

  // Respect Do Not Track
  if (navigator.doNotTrack === '1' || window.doNotTrack === '1') return;

  var API  = 'https://api.promptolian.com';
  var PAGE = location.pathname;
  var REF  = document.referrer ? document.referrer.slice(0, 200) : 'direct';

  // Anonymous session ID — sessionStorage only (no cookies, no cross-session tracking)
  var sid = sessionStorage.getItem('_ptl_sid');
  if (!sid) {
    sid = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2) + Date.now().toString(36);
    sessionStorage.setItem('_ptl_sid', sid);
  }

  function getUserId() {
    return (typeof window._ptlUserId === 'string' && window._ptlUserId) ? window._ptlUserId : null;
  }

  // fetch-based send — visible in Network panel, logs errors to console
  function send(payload) {
    var body = JSON.stringify(Object.assign(
      { session_id: sid, page: PAGE, referrer: REF, user_id: getUserId() },
      payload
    ));
    fetch(API + '/website-event', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
      keepalive: true,
    }).catch(function (err) {
      console.warn('[promptolian tracker] failed to send event:', err);
    });
  }

  // sendBeacon — for unload events only (browser may kill fetch on tab close)
  function sendBeacon(payload) {
    var body = JSON.stringify(Object.assign(
      { session_id: sid, page: PAGE, referrer: REF, user_id: getUserId() },
      payload
    ));
    if (navigator.sendBeacon) {
      navigator.sendBeacon(API + '/website-event', new Blob([body], { type: 'application/json' }));
    } else {
      // keepalive fetch as fallback
      fetch(API + '/website-event', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: true,
      }).catch(function () {});
    }
  }

  // 1. Pageview
  send({ event_type: 'pageview' });

  // 2. Click tracking — elements with data-track attribute
  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest && e.target.closest('[data-track]');
    if (!el) return;
    send({ event_type: 'click', element: el.getAttribute('data-track') });
  }, { passive: true });

  // 3. Time on page — sendBeacon on unload so browser doesn't cancel it
  var t0 = Date.now();
  var timeSent = false;
  function sendTime() {
    if (timeSent) return;
    timeSent = true;
    var sec = Math.round((Date.now() - t0) / 1000);
    if (sec < 2) return;
    sendBeacon({ event_type: 'time_on_page', duration_sec: sec });
  }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') sendTime();
  });
  window.addEventListener('pagehide', sendTime);

  // 4. Scroll depth — once per milestone
  var milestones = {};
  window.addEventListener('scroll', function () {
    var el  = document.documentElement;
    var max = el.scrollHeight - el.clientHeight;
    if (max <= 0) return;
    var pct = Math.round(el.scrollTop / max * 100);
    [25, 50, 75, 100].forEach(function (m) {
      if (pct >= m && !milestones[m]) {
        milestones[m] = true;
        send({ event_type: 'scroll_depth', scroll_pct: m });
      }
    });
  }, { passive: true });
})();
