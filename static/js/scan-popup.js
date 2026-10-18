/* AgriSense - "new scan" popup
 *
 * Vercel cannot keep a live connection open, so the page asks the server
 * "anything newer than the last scan I showed?" every few seconds (only
 * while the tab is visible). When a new scan from the linked ESP32 arrives,
 * a popup shows the values the sensor just measured.
 *
 * Works on every page (user pages and the admin panel): it injects its own
 * CSS and takes its colours from whatever theme the page already uses.
 */
(function () {
  'use strict';

  var script = (typeof document !== 'undefined' && document.currentScript) || null;
  var ds     = (script && script.dataset) || {};
  var cfg = {
    api:       ds.api       || '/api/latest-scan',
    dashboard: ds.dashboard || '/overview',
    user:      ds.user      || '',
    interval:  parseInt(ds.interval, 10) || 4000
  };
  var KEY = 'ss_last_scan_id:' + cfg.user;
  var REFRESH_PATHS = ['/overview', '/sensors', '/crops'];   // pages that show the latest scan

  /* ---------- what to do with a server answer (pure, unit-tested) ---------- */
  function decide(last, data) {
    var id = (data && typeof data.latest_id === 'number') ? data.latest_id : 0;
    if (last === null)               return { action: 'baseline', last: id };  // first visit: don't pop old scans
    if (id < last)                   return { action: 'baseline', last: id };  // database was reset
    if (data.new && data.scan && id > last) return { action: 'show', last: id };
    return { action: 'none', last: last };
  }

  /* ---------- tiny storage helpers (never throw) ---------- */
  function getLast() {
    try {
      var v = window.localStorage.getItem(KEY);
      if (v === null || v === '') return null;
      var n = parseInt(v, 10);
      return isNaN(n) ? null : n;
    } catch (e) { return null; }
  }
  function setLast(n) {
    try { window.localStorage.setItem(KEY, String(n)); } catch (e) {}
  }

  /* ---------- time label ---------- */
  function timeLabel(s) {
    // "2026-10-08 10:12:33.45+00"  (database timestamp as text)
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:\s*(Z|[+-]\d{2})(?::?(\d{2}))?)?/.exec(s || '');
    if (!m) return '';
    var ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    if (m[7] && m[7] !== 'Z') {
      var sign = m[7].charAt(0) === '-' ? -1 : 1;
      var off  = Math.abs(parseInt(m[7], 10)) * 60 + (m[8] ? parseInt(m[8], 10) : 0);
      ms -= sign * off * 60000;
    }
    try {
      return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
  }

  /* ---------- styling ---------- */
  var CSS = [
    '.ssp-overlay{--ssp-bg:var(--bg-raised,var(--surface,#0f1712));--ssp-border:var(--border,#253028);--ssp-text:var(--text,#d4e8da);',
    '--ssp-muted:var(--text-muted,var(--muted,#6b8a72));--ssp-accent:var(--accent,var(--green,#4caf6f));--ssp-amber:var(--amber,#e8b84b);',
    '--ssp-red:var(--red,#e05c5c);--ssp-tile:var(--panel-strong,var(--surface2,rgba(127,127,127,.10)));',
    'position:fixed;top:0;right:0;bottom:0;left:0;z-index:99999;display:flex;align-items:center;justify-content:center;padding:16px;',
    'background:rgba(0,0,0,.55);-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px);opacity:0;pointer-events:none;transition:opacity .2s}',
    '.ssp-overlay.ssp-open{opacity:1;pointer-events:auto}',
    '.ssp-card{width:100%;max-width:500px;max-height:calc(100vh - 32px);overflow:auto;box-sizing:border-box;background:var(--ssp-bg);color:var(--ssp-text);',
    'border:1px solid var(--ssp-border);border-radius:18px;padding:20px;box-shadow:0 24px 60px rgba(0,0,0,.45);',
    'transform:translateY(14px) scale(.98);transition:transform .22s;font-family:inherit}',
    '.ssp-open .ssp-card{transform:none}',
    '.ssp-head{display:flex;align-items:center;gap:10px}',
    '.ssp-dot{width:10px;height:10px;border-radius:50%;background:var(--ssp-accent);flex-shrink:0;animation:ssp-pulse 1.6s ease-out infinite}',
    '@keyframes ssp-pulse{0%{box-shadow:0 0 0 0 rgba(124,250,107,.55)}100%{box-shadow:0 0 0 12px rgba(124,250,107,0)}}',
    '.ssp-title{font-size:17px;font-weight:700;margin:0;line-height:1.2}',
    '.ssp-time{margin-left:auto;font-size:12.5px;color:var(--ssp-muted);white-space:nowrap}',
    '.ssp-sub{margin:6px 0 14px;font-size:13px;color:var(--ssp-muted)}',
    '.ssp-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}',
    '.ssp-tile{background:var(--ssp-tile);border:1px solid var(--ssp-border);border-left-width:3px;border-radius:12px;padding:10px 11px;min-width:0}',
    '.ssp-tile.ssp-optimal{border-left-color:var(--ssp-accent)}',
    '.ssp-tile.ssp-watch{border-left-color:var(--ssp-amber)}',
    '.ssp-tile.ssp-low,.ssp-tile.ssp-high{border-left-color:var(--ssp-red)}',
    '.ssp-label{font-size:11.5px;color:var(--ssp-muted);text-transform:uppercase;letter-spacing:.04em}',
    '.ssp-value{font-size:22px;font-weight:700;line-height:1.15;margin-top:3px;word-break:break-word}',
    '.ssp-unit{font-size:11.5px;font-weight:500;color:var(--ssp-muted);margin-left:3px}',
    '.ssp-status{font-size:11.5px;margin-top:3px;font-weight:600}',
    '.ssp-optimal .ssp-status{color:var(--ssp-accent)}',
    '.ssp-watch .ssp-status{color:var(--ssp-amber)}',
    '.ssp-low .ssp-status,.ssp-high .ssp-status{color:var(--ssp-red)}',
    '.ssp-summary{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}',
    '.ssp-sum{flex:1 1 180px;background:var(--ssp-tile);border:1px solid var(--ssp-border);border-radius:12px;padding:10px 12px;min-width:0}',
    '.ssp-sum b{display:block;font-size:16px;margin-top:2px}',
    '.ssp-more{margin-top:10px;font-size:12.5px;color:var(--ssp-muted)}',
    '.ssp-actions{display:flex;gap:10px;margin-top:16px;justify-content:flex-end;flex-wrap:wrap}',
    '.ssp-btn{font:inherit;font-size:14px;font-weight:600;border-radius:10px;padding:10px 16px;cursor:pointer;border:1px solid var(--ssp-border);',
    'background:transparent;color:var(--ssp-text)}',
    '.ssp-btn.ssp-primary{background:var(--ssp-accent);border-color:var(--ssp-accent);color:#04130a}',
    '.ssp-btn:focus-visible{outline:2px solid var(--ssp-accent);outline-offset:2px}',
    '@media (max-width:520px){.ssp-overlay{align-items:flex-end;padding:0}',
    '.ssp-card{max-width:none;border-radius:18px 18px 0 0;padding-bottom:calc(20px + env(safe-area-inset-bottom,0px))}',
    '.ssp-grid{grid-template-columns:repeat(2,1fr)}.ssp-actions .ssp-btn{flex:1}}',
    '@media (prefers-reduced-motion:reduce){.ssp-overlay,.ssp-card{transition:none}.ssp-dot{animation:none}}'
  ].join('');

  /* ---------- DOM ---------- */
  var ui = null;                       // built lazily, once

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls)  e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function build() {
    if (ui) return ui;
    var style = el('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    var overlay = el('div', 'ssp-overlay');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'ssp-title');
    overlay.setAttribute('aria-hidden', 'true');

    var card    = el('div', 'ssp-card');
    var head    = el('div', 'ssp-head');
    var title   = el('h2', 'ssp-title', 'New scan received');
    title.id    = 'ssp-title';
    var time    = el('span', 'ssp-time');
    head.appendChild(el('span', 'ssp-dot'));
    head.appendChild(title);
    head.appendChild(time);

    var sub     = el('div', 'ssp-sub', 'Just measured by your linked scanner');
    var grid    = el('div', 'ssp-grid');
    var summary = el('div', 'ssp-summary');
    var more    = el('div', 'ssp-more');
    var actions = el('div', 'ssp-actions');
    var close   = el('button', 'ssp-btn', 'Dismiss');
    var view    = el('button', 'ssp-btn ssp-primary', 'View dashboard');
    close.type = 'button';
    view.type  = 'button';
    actions.appendChild(close);
    actions.appendChild(view);

    card.appendChild(head);
    card.appendChild(sub);
    card.appendChild(grid);
    card.appendChild(summary);
    card.appendChild(more);
    card.appendChild(actions);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    close.addEventListener('click', function () { dismiss(true); });
    view.addEventListener('click', function () { goDashboard(); });
    overlay.addEventListener('click', function (ev) { if (ev.target === overlay) dismiss(true); });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape' && ui && ui.open) dismiss(true);
    });

    ui = { overlay: overlay, time: time, grid: grid, summary: summary, more: more, view: view, close: close, open: false };
    return ui;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function statusWord(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

  function show(scan, newCount) {
    var u = build();

    u.time.textContent = timeLabel(scan.created_at);

    clear(u.grid);
    (scan.readings || []).forEach(function (r) {
      var tile = el('div', 'ssp-tile ssp-' + (r.status || 'optimal'));
      tile.appendChild(el('div', 'ssp-label', r.label));
      var val = el('div', 'ssp-value', String(r.value));
      val.appendChild(el('span', 'ssp-unit', r.unit));
      tile.appendChild(val);
      tile.appendChild(el('div', 'ssp-status', statusWord(r.status)));
      u.grid.appendChild(tile);
    });

    clear(u.summary);
    var crop = el('div', 'ssp-sum');
    crop.appendChild(el('span', 'ssp-label', 'Recommended crop'));
    crop.appendChild(el('b', '', scan.crop || '-'));
    var health = el('div', 'ssp-sum');
    health.appendChild(el('span', 'ssp-label', 'Soil health'));
    health.appendChild(el('b', '', Math.round(scan.fertility) + ' / 100  \u00b7  Grade ' + (scan.grade || '-')));
    u.summary.appendChild(crop);
    u.summary.appendChild(health);

    u.more.textContent = (newCount && newCount > 1)
      ? ('+ ' + (newCount - 1) + ' earlier new scan' + (newCount - 1 > 1 ? 's' : '') + ' since you last looked')
      : '';

    u.open = true;
    u.overlay.classList.add('ssp-open');
    u.overlay.setAttribute('aria-hidden', 'false');
    try { u.view.focus(); } catch (e) {}
  }

  function refreshIfDataPage() {
    if (REFRESH_PATHS.indexOf(window.location.pathname) !== -1) window.location.reload();
  }

  function dismiss(refresh) {
    if (!ui || !ui.open) return;
    ui.open = false;
    ui.overlay.classList.remove('ssp-open');
    ui.overlay.setAttribute('aria-hidden', 'true');
    if (refresh) refreshIfDataPage();
  }

  function goDashboard() {
    dismiss(false);
    if (window.location.pathname === cfg.dashboard) window.location.reload();
    else window.location.href = cfg.dashboard;
  }

  /* ---------- polling ---------- */
  var busy = false;

  function poll() {
    if (busy || (typeof document !== 'undefined' && document.hidden)) return Promise.resolve();
    busy = true;
    var last = getLast();
    var url  = cfg.api + (last !== null ? ('?since=' + last) : '');
    return fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (r.status === 401) { stop(); return null; }          // logged out: stop asking
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        var d = decide(getLast(), data);                         // re-read: another tab may have shown it
        if (d.action === 'show') {
          setLast(d.last);
          show(data.scan, data.new_count);
        } else if (d.action === 'baseline' || d.last !== getLast()) {
          setLast(d.last);
        }
      })
      .catch(function () { /* offline / server asleep: try again next tick */ })
      .then(function () { busy = false; });
  }

  var timer = null;
  function start() {
    if (timer) return;
    poll();
    timer = setInterval(poll, cfg.interval);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  var api = { decide: decide, show: show, poll: poll, dismiss: dismiss, timeLabel: timeLabel, _cfg: cfg, _key: KEY };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }
  window.SoilSenseScanPopup = api;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
