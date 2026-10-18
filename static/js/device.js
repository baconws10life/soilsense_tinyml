/* AgriSense - "Find device" panel.
 *
 * The scanner announces itself to the server every few seconds; this script asks the
 * server which scanners are online and lets the signed-in user Connect to a free one
 * or Disconnect from their own. Admins may disconnect anyone.
 *
 * States shown (all come from /api/devices, never guessed in the browser):
 *   searching | none found | available | connected (you) | in use by someone else |
 *   connection lost | server unreachable | "you were disconnected" notice
 *
 * Also: a "Scan now" button under the scanner you are connected to. It asks the server to
 * tell the scanner to take a reading (POST /api/devices/<id>/scan), then waits for the new
 * reading to appear (GET /api/latest-scan). The scan-popup then shows the values.
 */
(function () {
  'use strict';
  var panel = document.querySelector('[data-device-panel]');
  if (!panel) return;

  var $ = function (id) { return document.getElementById(id); };
  var body = $('dpBody'), pill = $('dpPill'), sub = $('dpSub'), hint = $('dpHint'),
      findBtn = $('dpFind'), notice = $('dpNotice');
  var chip = document.getElementById('devChip');
  var POLL_MS = 4000, MIN_SEARCH_MS = 1300;
  var busy = false, timer = null, lastErr = '', searching = true;
  // "Scan now" button state. Kept here (not in the DOM) because the list is redrawn every few seconds.
  //   phase: idle | sending | waiting | done | error
  var SCAN_TIMEOUT_MS = 45000, SCAN_POLL_MS = 1500;
  var scan = { phase: 'idle', msg: '', devId: null, baseline: 0, t0: 0, timer: null, reset: null };
  var lastMine = null, scanEl = null;

  function h(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function icon(path, size) {   // tiny inline SVG, 24px grid
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size || 16); s.setAttribute('height', size || 16);
    s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor');
    s.setAttribute('stroke-width', '2'); s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
    s.innerHTML = path;   // constant strings below, never user data
    return s;
  }
  var ICON = {
    wifi:  '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
    probe: '<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
    scan:  '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3"/>',
    lock:  '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>'
  };

  function setPill(kind, text) {
    pill.className = 'dp-pill dp-pill-' + kind;
    pill.querySelector('span').textContent = text;
    if (chip) {
      var live = kind === 'ok', bad = kind === 'err';
      chip.className = 'chip ' + (live ? 'chip-live' : bad ? 'chip-err' : 'chip-off');
      var t = chip.querySelector('.chip-txt');
      if (t) t.textContent = live ? 'Device connected' : bad ? 'Connection lost' : 'No device';
    }
  }

  function signal(rssi) {
    if (rssi == null) return null;
    return rssi > -60 ? 'Strong signal' : rssi > -75 ? 'Good signal' : 'Weak signal';
  }

  function post(url) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { j._status = r.status; return j; }); });
  }

  /* ---------- rendering ---------- */

  function row(d, isAdmin) {
    var li = h('div', 'dp-item dp-' + d.state);
    var ico = h('span', 'dp-item-ico'); ico.appendChild(icon(ICON.probe, 20));
    li.appendChild(ico);

    var main = h('div', 'dp-item-main');
    main.appendChild(h('div', 'dp-item-name', d.label));

    var meta = h('div', 'dp-item-meta');
    var line = [];
    if (d.state === 'mine')        line.push('Connected to you');
    else if (d.state === 'available') line.push('Free to use');
    else if (d.state === 'busy')   line.push(isAdmin && d.owner ? 'In use by ' + d.owner : 'In use by someone else');
    else if (d.state === 'lost')   line.push('Not responding');
    else                           line.push('Offline');
    var sig = d.online ? signal(d.rssi) : null;
    if (sig) line.push(sig);
    meta.textContent = line.join(' \u00b7 ');
    main.appendChild(meta);

    if (d.state === 'lost') {
      var w = h('div', 'dp-warn'); w.appendChild(icon(ICON.alert, 14));
      w.appendChild(h('span', null, 'Connection lost. Check the scanner is powered on and on Wi-Fi - it reconnects by itself.'));
      main.appendChild(w);
    } else if (d.online && d.sensor === 'fail' && (d.state === 'mine' || isAdmin)) {
      var w2 = h('div', 'dp-warn'); w2.appendChild(icon(ICON.alert, 14));
      w2.appendChild(h('span', null, 'Scanner is online but its soil probe is not answering. Check the probe cable.'));
      main.appendChild(w2);
    }
    var err = h('div', 'dp-err'); err.hidden = true; main.appendChild(err);
    li.appendChild(main);

    var act = h('div', 'dp-item-act');
    if (d.state === 'available') {
      act.appendChild(btn('Connect', 'btn btn-primary btn-sm', function (b) { connect(d, b, err); }));
    } else if (d.state === 'mine' || d.state === 'lost') {
      act.appendChild(btn('Disconnect', 'btn btn-ghost btn-sm', function (b) { disconnect(d, b, err, false); }));
    } else if (d.state === 'busy') {
      if (isAdmin) act.appendChild(btn('Disconnect user', 'btn btn-danger btn-sm', function (b) { disconnect(d, b, err, true); }));
      else { var lk = h('span', 'dp-locked'); lk.appendChild(icon(ICON.lock, 14)); lk.appendChild(h('span', null, 'In use')); act.appendChild(lk); }
    } else if (isAdmin && d.in_use) {
      act.appendChild(btn('Release', 'btn btn-ghost btn-sm', function (b) { disconnect(d, b, err, true); }));
    }
    li.appendChild(act);
    return li;
  }


  /* ---------- Scan now (website button) ---------- */

  function scanBlock(d) {
    var box = h('div', 'dp-scan dp-scan-' + scan.phase);
    var b = h('button', 'btn btn-primary dp-scan-btn'); b.type = 'button';
    b.appendChild(icon(ICON.scan, 20));
    var working = scan.phase === 'sending' || scan.phase === 'waiting';
    var label = scan.phase === 'sending' ? 'Sending…'
              : scan.phase === 'waiting' ? 'Scanning…' : 'Scan now';
    b.appendChild(h('span', null, label));
    b.disabled = working;
    if (working) b.classList.add('is-working');
    b.addEventListener('click', function () { startScan(d); });
    box.appendChild(b);

    var line = h('div', 'dp-scan-msg'); line.setAttribute('role', 'status'); line.setAttribute('aria-live', 'polite');
    if (scan.phase === 'sending')      line.textContent = 'Telling the scanner…';
    else if (scan.phase === 'waiting') line.textContent = (lastMine && lastMine.scan_pending)
                                          ? 'Waiting for the scanner to answer…'
                                          : 'The scanner is reading the soil – keep the probe in the ground.';
    else if (scan.phase === 'done' || scan.phase === 'error') line.textContent = scan.msg;
    else line.textContent = 'Takes one reading with the probe, same as pressing the scanner’s screen.';
    box.appendChild(line);
    return box;
  }

  function paintScan() {          // redraw only the button block (phase changed between list refreshes)
    if (!scanEl || !scanEl.parentNode || !lastMine) return;
    var fresh = scanBlock(lastMine);
    scanEl.parentNode.replaceChild(fresh, scanEl);
    scanEl = fresh;
  }

  function scanSet(phase, msg) {
    scan.phase = phase; scan.msg = msg || '';
    clearTimeout(scan.reset);
    if (phase === 'done' || phase === 'error') {
      scan.reset = setTimeout(function () { scan.phase = 'idle'; scan.msg = ''; paintScan(); }, phase === 'done' ? 8000 : 12000);
    }
    paintScan();
  }

  function getJson(url) {
    return fetch(url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (r.status === 401) { location.href = '/'; throw new Error('signed out'); } return r.json(); });
  }

  function startScan(d) {
    if (scan.phase === 'sending' || scan.phase === 'waiting') return;
    scan.devId = d.id;
    scanSet('sending');
    post('/api/devices/' + encodeURIComponent(d.id) + '/scan').then(function (j) {
      if (!j.ok) { scanSet('error', j.message || 'Could not start the scan.'); return; }
      scan.baseline = j.baseline || 0; scan.t0 = Date.now();
      scanSet('waiting');
      watchScan();
    }).catch(function () { scanSet('error', 'Could not reach the website. Try again.'); });
  }

  // Wait for the reading to land: the scanner posts it, so a newer scan id shows up.
  function watchScan() {
    clearTimeout(scan.timer);
    scan.timer = setTimeout(function () {
      if (scan.phase !== 'waiting') return;
      getJson('/api/latest-scan?since=' + scan.baseline).then(function (j) {
        if (j && j.new) { scanSet('done', 'Done – your new reading has been saved.'); refresh(false); return; }
        if (Date.now() - scan.t0 > SCAN_TIMEOUT_MS) { scanFailed(); return; }
        watchScan();
      }).catch(function () {
        if (Date.now() - scan.t0 > SCAN_TIMEOUT_MS) scanFailed(); else watchScan();
      });
    }, SCAN_POLL_MS);
  }

  // Nothing arrived in time: ask the server why, so the message is specific.
  function scanFailed() {
    getJson('/api/devices').then(function (data) {
      var d = (data.devices || []).filter(function (x) { return x.id === scan.devId; })[0];
      var msg;
      if (!d || d.state === 'lost' || d.state === 'offline') msg = 'The scanner stopped responding. Check it is powered on and on Wi-Fi.';
      else if (d.state !== 'mine')                           msg = 'You are no longer connected to this scanner.';
      else if (d.scan_pending)                               msg = 'The scanner did not pick up the request. Try again in a moment.';
      else if (d.sensor === 'fail')                          msg = 'The scanner could not read the soil probe. Check the probe cable.';
      else                                                   msg = 'No reading came back. Try again.';
      scanSet('error', msg);
    }).catch(function () { scanSet('error', 'No reading came back. Try again.'); });
  }

  function btn(label, cls, fn) {
    var b = h('button', cls, label); b.type = 'button';
    b.addEventListener('click', function () { fn(b); });
    return b;
  }

  function render(data) {
    var devices = data.devices || [], isAdmin = !!data.is_admin;

    // "you were disconnected" notice
    if (data.notice) {
      notice.hidden = false; notice.querySelector('.dp-notice-txt').textContent = data.notice.text;
    } else { notice.hidden = true; }

    var mine = devices.filter(function (d) { return d.state === 'mine'; });
    var lost = devices.filter(function (d) { return d.state === 'lost'; });
    var free = devices.filter(function (d) { return d.state === 'available'; });
    var used = devices.filter(function (d) { return d.state === 'busy'; });

    if (mine.length)       { setPill('ok',   'Connected');        sub.textContent = 'Press Scan now below (or the big button on the scanner) - your reading shows up here.'; hint.textContent = 'Done? Disconnect so the next person can use it.'; }
    else if (lost.length)  { setPill('err',  'Connection lost');  sub.textContent = 'Your scanner stopped responding.'; hint.textContent = 'It reconnects by itself when Wi-Fi is back.'; }
    else if (free.length)  { setPill('found','Scanner found');    sub.textContent = free.length > 1 ? free.length + ' scanners are free - pick one to connect.' : 'A scanner is ready. Connect to start scanning.'; hint.textContent = 'Only one person can use a scanner at a time.'; }
    else if (used.length)  { setPill('busy', 'In use');           sub.textContent = 'The scanner is being used by someone else right now.'; hint.textContent = 'You can connect as soon as they disconnect.'; }
    else                   { setPill('none', 'Not found');        sub.textContent = 'No scanner is online.'; hint.textContent = 'Power on the scanner and make sure it is on Wi-Fi, then press Find device.'; }

    // "Find device" is the main action only when nothing has been found yet
    findBtn.classList.toggle('btn-primary', !devices.length);
    findBtn.classList.toggle('btn-ghost', !!devices.length);

    lastMine = mine.length ? mine[0] : null;
    if (!lastMine && (scan.phase === 'sending' || scan.phase === 'waiting')) { clearTimeout(scan.timer); scan.phase = 'idle'; }
    scanEl = null;

    body.textContent = '';
    if (!devices.length) {
      var empty = h('div', 'dp-empty');
      empty.appendChild(h('div', 'dp-empty-t', 'No scanner found'));
      empty.appendChild(h('div', 'dp-empty-s', 'Check that it is plugged in, the screen shows SCAN NOW, and it is on the school Wi-Fi.'));
      body.appendChild(empty);
    } else {
      var order = { mine: 0, lost: 1, available: 2, busy: 3, offline: 4 };
      devices.slice().sort(function (a, b) { return order[a.state] - order[b.state]; })
             .forEach(function (d) {
               body.appendChild(row(d, isAdmin));
               if (d.state === 'mine' && d.online) { scanEl = scanBlock(d); body.appendChild(scanEl); }
             });
    }
  }

  function renderError(msg) {
    setPill('err', 'Can\u2019t reach server');
    sub.textContent = 'The website could not check for scanners.';
    hint.textContent = 'Check your internet connection - retrying automatically.';
    body.textContent = '';
    var e = h('div', 'dp-empty dp-empty-err');
    e.appendChild(h('div', 'dp-empty-t', 'Connection problem'));
    e.appendChild(h('div', 'dp-empty-s', msg || 'Please try again.'));
    body.appendChild(e);
  }

  /* ---------- talking to the server ---------- */

  function refresh(manual) {
    if (busy) return Promise.resolve();
    busy = true;
    var t0 = Date.now();
    if (manual) {
      searching = true;
      setPill('search', 'Searching'); sub.textContent = 'Looking for scanners on the network\u2026';
      body.textContent = ''; var r = h('div', 'dp-radar'); r.innerHTML = '<i></i><i></i><i></i>'; body.appendChild(r);
      findBtn.disabled = true; findBtn.querySelector('span').textContent = 'Searching\u2026';
    }
    return fetch('/api/devices', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if (r.status === 401) { location.href = '/'; throw new Error('signed out'); }
        if (!r.ok) throw new Error('Server error ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var wait = manual ? Math.max(0, MIN_SEARCH_MS - (Date.now() - t0)) : 0;
        return new Promise(function (res) { setTimeout(function () { render(data); res(); }, wait); });
      })
      .catch(function (e) { if (e.message !== 'signed out') renderError(e.message); })
      .then(function () {
        busy = false; searching = false;
        findBtn.disabled = false; findBtn.querySelector('span').textContent = 'Find device';
      });
  }

  function connect(d, b, errEl) {
    b.disabled = true; b.textContent = 'Connecting\u2026'; errEl.hidden = true;
    post('/api/devices/' + encodeURIComponent(d.id) + '/connect').then(function (j) {
      if (j.ok) return refresh(false);
      errEl.textContent = j.message || 'Could not connect.'; errEl.hidden = false;
      b.disabled = false; b.textContent = 'Connect';
      return refresh(false).then(function () {});   // show what the server says now (e.g. someone else got it first)
    }).catch(function () {
      errEl.textContent = 'Could not reach the website. Try again.'; errEl.hidden = false;
      b.disabled = false; b.textContent = 'Connect';
    });
  }

  function disconnect(d, b, errEl, asAdmin) {
    var msg = asAdmin
      ? 'Disconnect ' + (d.owner || 'this user') + ' from ' + d.label + '? They will be told an admin did it.'
      : 'Disconnect from this scanner so someone else can use it?';
    if (!window.confirm(msg)) return;
    b.disabled = true; b.textContent = 'Disconnecting\u2026'; errEl.hidden = true;
    post('/api/devices/' + encodeURIComponent(d.id) + '/disconnect').then(function (j) {
      if (!j.ok) { errEl.textContent = j.message || 'Could not disconnect.'; errEl.hidden = false; }
      return refresh(false);
    }).catch(function () {
      errEl.textContent = 'Could not reach the website. Try again.'; errEl.hidden = false;
      b.disabled = false; b.textContent = 'Disconnect';
    });
  }

  notice.querySelector('.dp-notice-x').addEventListener('click', function () {
    notice.hidden = true; post('/api/devices/dismiss_notice');
  });
  findBtn.addEventListener('click', function () { refresh(true); });

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (!document.hidden) refresh(false).then(schedule); else schedule();
    }, POLL_MS);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(false); });

  // first look happens automatically: the user never has to hunt for the device
  refresh(true).then(schedule);
})();
