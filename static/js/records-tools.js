/* Filter + bulk delete for a records table (see templates/_records_tools.html).
   Filtering is done in the browser on the rows already on the page; the delete buttons send the
   exact row ids (or "all") to POST /api/records/delete, which re-checks ownership on the server. */
(function () {
  function init(root) {
    var tbody = document.getElementById(root.dataset.tbody);
    if (!tbody) return;
    var endpoint = root.dataset.endpoint;
    var isAdmin  = root.dataset.admin === '1';
    var q = function (k) { return root.querySelector('[data-rt="' + k + '"]'); };
    var el = {
      source: q('source'), crop: q('crop'), from: q('from'), to: q('to'),
      reset: q('reset'), info: q('info'), msg: q('msg'),
      delSel: q('del-selected'), delShown: q('del-shown'), clearAll: q('clear-all')
    };
    var table  = tbody.closest('table');
    var master = table ? table.querySelector('[data-rt="all"]') : null;
    var busy = false;

    function rows() { return Array.prototype.slice.call(tbody.querySelectorAll('tr[data-id]')); }
    function shown(r) { return !r.classList.contains('rt-hidden') && r.style.display !== 'none'; }
    function filterActive() { return !!(el.source.value || el.crop.value || el.from.value || el.to.value); }

    function applyFilter() {
      var s = el.source.value, c = el.crop.value, f = el.from.value, t = el.to.value;
      rows().forEach(function (r) {
        var ok = (!s || r.dataset.source === s) &&
                 (!c || r.dataset.crop === c) &&
                 (!f || (r.dataset.date && r.dataset.date >= f)) &&
                 (!t || (r.dataset.date && r.dataset.date <= t));
        r.classList.toggle('rt-hidden', !ok);
        if (!ok) { var cb = r.querySelector('.rt-check'); if (cb) cb.checked = false; }
      });
      refresh();
    }

    function picked()  { return rows().filter(function (r) { var cb = r.querySelector('.rt-check'); return cb && cb.checked && shown(r); }); }
    function visible() { return rows().filter(shown); }

    function refresh() {
      var all = rows(), vis = visible(), sel = picked();
      rows().forEach(function (r) {
        var cb = r.querySelector('.rt-check');
        r.classList.toggle('rt-picked', !!(cb && cb.checked));
      });
      el.info.textContent = vis.length + ' of ' + all.length + ' shown' + (sel.length ? ' · ' + sel.length + ' selected' : '');
      el.delSel.disabled   = busy || sel.length === 0;
      el.delSel.textContent = sel.length ? 'Delete selected (' + sel.length + ')' : 'Delete selected';
      var showBtn = filterActive();
      el.delShown.style.display = showBtn ? '' : 'none';
      el.delShown.disabled = busy || vis.length === 0;
      el.delShown.textContent = 'Delete all shown (' + vis.length + ')';
      el.clearAll.disabled = busy || all.length === 0;
      if (master) {
        master.checked = vis.length > 0 && sel.length === vis.length;
        master.indeterminate = sel.length > 0 && sel.length < vis.length;
      }
    }

    function say(text, kind) { el.msg.textContent = text; el.msg.className = 'rt-msg ' + kind; }

    function send(body, okText) {
      busy = true; refresh();
      return fetch(endpoint, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j }; });
      }).then(function (res) {
        if (!res.ok) throw new Error(res.j.error || 'The server refused the request.');
        say(okText(res.j.deleted), 'ok');
        // charts and counters elsewhere on the page are server-rendered, so reload to keep them true
        setTimeout(function () { window.location.reload(); }, 900);
      }).catch(function (e) {
        busy = false; refresh();
        say('Could not delete: ' + (e && e.message ? e.message : 'network problem') + ' Nothing was changed on screen.', 'err');
      });
    }

    function ids(list) { return list.map(function (r) { return parseInt(r.dataset.id, 10); }); }
    var plural = function (n) { return n + ' record' + (n === 1 ? '' : 's'); };

    el.delSel.addEventListener('click', function () {
      var list = picked(); if (!list.length) return;
      if (!confirm('Delete ' + plural(list.length) + '? This cannot be undone.')) return;
      send({ mode: 'ids', ids: ids(list) }, function (n) { return 'Deleted ' + plural(n) + '.'; });
    });

    el.delShown.addEventListener('click', function () {
      var list = visible(); if (!list.length) return;
      if (!confirm('Delete all ' + plural(list.length) + ' that match the filter? This cannot be undone.')) return;
      send({ mode: 'ids', ids: ids(list) }, function (n) { return 'Deleted ' + plural(n) + '.'; });
    });

    el.clearAll.addEventListener('click', function () {
      var n = rows().length; if (!n) return;
      if (isAdmin) {
        var t = prompt('This deletes EVERY reading of EVERY user (' + n + ' on this page). It cannot be undone.\n\nType DELETE to confirm.');
        if (t === null || t.trim() !== 'DELETE') return;
      } else if (!confirm('Clear ALL ' + plural(n) + '? This cannot be undone.')) return;
      send({ mode: 'all' }, function (d) { return 'Cleared ' + plural(d) + '.'; });
    });

    ['source', 'crop', 'from', 'to'].forEach(function (k) {
      el[k].addEventListener('input', applyFilter);
      el[k].addEventListener('change', applyFilter);
    });
    el.reset.addEventListener('click', function () {
      el.source.value = el.crop.value = el.from.value = el.to.value = '';
      applyFilter();
    });
    tbody.addEventListener('change', function (e) { if (e.target.classList.contains('rt-check')) refresh(); });
    if (master) master.addEventListener('change', function () {
      var on = master.checked;
      visible().forEach(function (r) { var cb = r.querySelector('.rt-check'); if (cb) cb.checked = on; });
      refresh();
    });
    // the admin page also has a free-text search that hides rows with style.display
    var search = document.getElementById('recSearch');
    if (search) search.addEventListener('input', function () { setTimeout(refresh, 0); });

    refresh();
  }

  function boot() { Array.prototype.forEach.call(document.querySelectorAll('[data-rt-root]'), init); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
