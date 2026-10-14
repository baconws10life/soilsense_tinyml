/* TinyML page: live demo + "look inside" view. Uses static/js/tinyml.js (the in-browser model). */
(function () {
  "use strict";
  var cfgEl = document.getElementById('tm-cfg');
  if (!cfgEl) return;
  var cfg = JSON.parse(cfgEl.textContent);

  var FIELDS = [
    { key: 'N',           label: 'Nitrogen',    unit: 'mg/kg', min: 0,  max: 200, step: 1 },
    { key: 'P',           label: 'Phosphorus',  unit: 'mg/kg', min: 0,  max: 200, step: 1 },
    { key: 'K',           label: 'Potassium',   unit: 'mg/kg', min: 0,  max: 250, step: 1 },
    { key: 'temperature', label: 'Soil temp',   unit: '°C', min: 0, max: 50,  step: 0.1 },
    { key: 'humidity',    label: 'Moisture',    unit: '%',     min: 0,  max: 100, step: 1 },
    { key: 'ph',          label: 'pH',          unit: '',      min: 3,  max: 10,  step: 0.1 }
  ];
  var PRESETS = {
    rice:     { N: 80, P: 48, K: 40, temperature: 27.5, humidity: 82, ph: 6.0 },
    cabbage:  { N: 125, P: 70, K: 85, temperature: 19.5, humidity: 82, ph: 6.5 },
    air:      { N: 0,  P: 0,  K: 0,  temperature: 25,   humidity: 0,  ph: 7.0 },
    mine:     cfg.sample
  };

  var $ = function (id) { return document.getElementById(id); };
  var model = null, values = {}, neuron = 0;
  var start = cfg.sample || PRESETS.rice;
  FIELDS.forEach(function (f) { values[f.key] = Number(start[f.key]); });

  var esc = function (s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var fmt = function (v, d) { return (Math.round(v * Math.pow(10, d)) / Math.pow(10, d)).toFixed(d); };

  // ---- inputs ---------------------------------------------------------------
  function buildInputs() {
    var host = $('tm-inputs');
    host.innerHTML = '';
    FIELDS.forEach(function (f) {
      var row = document.createElement('div');
      row.className = 'tm-in';
      row.innerHTML =
        '<label for="tm-n-' + f.key + '">' + f.label + '<small>' + esc(f.unit) + '</small></label>' +
        '<input type="range" id="tm-r-' + f.key + '" min="' + f.min + '" max="' + f.max + '" step="' + f.step + '" aria-label="' + f.label + ' slider">' +
        '<input type="number" id="tm-n-' + f.key + '" min="' + f.min + '" max="' + f.max + '" step="' + f.step + '" inputmode="decimal">';
      host.appendChild(row);
      var r = row.querySelector('input[type=range]'), n = row.querySelector('input[type=number]');
      r.addEventListener('input', function () { values[f.key] = parseFloat(r.value); n.value = r.value; update(); });
      n.addEventListener('input', function () {
        var v = parseFloat(n.value);
        if (isNaN(v)) return;
        values[f.key] = v; r.value = v; update();
      });
    });
    syncInputs();
  }
  function syncInputs() {
    FIELDS.forEach(function (f) {
      var v = values[f.key];
      $('tm-r-' + f.key).value = v;
      $('tm-n-' + f.key).value = (f.step < 1) ? fmt(v, 1) : Math.round(v);
    });
  }

  // ---- results --------------------------------------------------------------
  function update() {
    if (!model) return;
    var raw = FIELDS.map(function (f) { return values[f.key]; });
    var r = model.run(raw);

    // 1. top crops
    $('tm-top').innerHTML = r.crop.top3.map(function (c, i) {
      var p = Math.round(c.p);
      return '<div class="sx-top"><span class="sx-top-name">' + (i + 1) + '. ' + (cfg.emojis[c.key] || '') + ' ' + esc(c.label) +
             '</span><span class="sx-top-bar"><i style="width:' + Math.max(0, Math.min(100, c.p)) + '%"></i></span>' +
             '<span class="sx-top-pct">' + p + '%</span></div>';
    }).join('');

    // 2. probe check (bar shows error relative to 2x the threshold)
    var pr = r.probe, scale = pr.threshold * 2.5;
    var el = $('tm-probe');
    el.className = 'tm-state ' + (pr.odd ? 'tm-bad' : 'tm-ok');
    el.innerHTML = pr.odd
      ? '<b>Odd value.</b> This does not look like real soil. Push the probe in fully, wait a few seconds and scan again.'
      : '<b>Looks like real soil.</b> The six values fit together the way real soil readings do.';
    var bar = $('tm-probe-bar');
    bar.style.width = Math.min(100, pr.err / scale * 100) + '%';
    bar.className = pr.odd ? 'bad' : '';
    $('tm-probe-thr').style.left = (pr.threshold / scale * 100) + '%';
    $('tm-probe-note').textContent = 'rebuild error ' + fmt(pr.err, 3) + '  ·  flag line ' + fmt(pr.threshold, 3);

    // 3. advisor
    $('tm-adv').innerHTML = r.advisor.found.length
      ? '<div class="sx-chips">' + r.advisor.found.map(function (a) { return '<span class="badge badge-watch">' + esc(a) + '</span>'; }).join('') + '</div>'
      : '<p class="tm-state tm-ok"><b>Nothing to fix.</b> No problem flagged.</p>';

    drawInside(r);
  }

  // ---- inside view ----------------------------------------------------------
  function drawInside(r) {
    var t = r.trace;

    $('tm-z').innerHTML = FIELDS.map(function (f, i) {
      var z = t.x[i], w = Math.min(50, Math.abs(z) / 3 * 50);
      var style = z >= 0 ? 'left:50%;width:' + w + '%;background:var(--accent)' : 'right:50%;width:' + w + '%;background:var(--amber)';
      return '<div class="tm-zrow"><span>' + f.label + '</span><span class="tm-zbar"><i style="' + style + '"></i></span><span class="tm-zval">' + (z >= 0 ? '+' : '') + fmt(z, 1) + '</span></div>';
    }).join('');

    var mx = Math.max.apply(null, t.hidden.concat([1e-9]));
    var cells = $('tm-cells');
    if (!cells.children.length) {
      t.hidden.forEach(function (_, i) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'tm-cell'; b.setAttribute('aria-label', 'Neuron ' + (i + 1));
        b.addEventListener('click', function () { neuron = i; $('tm-neuron').value = i; drawWork(); markSel(); });
        cells.appendChild(b);
      });
    }
    t.hidden.forEach(function (h, i) {
      var a = Math.min(1, h / mx), c = cells.children[i];
      c.style.background = 'rgba(76,175,111,' + (0.08 + a * 0.82).toFixed(2) + ')';
      c.textContent = fmt(h, 1);
      c.style.color = a > 0.55 ? '#06170c' : 'var(--text)';
    });
    markSel();

    $('tm-out').innerHTML = r.crop.all.slice(0, 6).map(function (c) {
      return '<div class="tm-oc"><span class="sx-top-name">' + (cfg.emojis[c.key] || '') + ' ' + esc(c.label) +
             '</span><span class="sx-top-bar"><i style="width:' + Math.max(0, Math.min(100, c.p)) + '%"></i></span><span class="sx-top-pct">' + fmt(c.p, 0) + '%</span></div>';
    }).join('');

    drawWork();
  }

  function markSel() {
    var cells = $('tm-cells').children;
    for (var i = 0; i < cells.length; i++) cells[i].classList.toggle('sel', i === neuron);
  }

  function drawWork() {
    if (!model) return;
    var L = model.m.crop[0], x = model.run(FIELDS.map(function (f) { return values[f.key]; })).trace.x;
    var rows = '', sum = L.b[neuron];
    FIELDS.forEach(function (f, i) {
      var w = L.W[i][neuron], p = x[i] * w; sum += p;
      rows += '<tr><td>' + f.label + '</td><td>' + (x[i] >= 0 ? '+' : '') + fmt(x[i], 2) + '</td><td>' + (w >= 0 ? '+' : '') + fmt(w, 2) +
              '</td><td>' + (p >= 0 ? '+' : '') + fmt(p, 2) + '</td></tr>';
    });
    document.querySelector('#tm-work tbody').innerHTML = rows;
    document.querySelector('#tm-work tfoot').innerHTML =
      '<tr><td colspan="3">+ bias</td><td>' + (L.b[neuron] >= 0 ? '+' : '') + fmt(L.b[neuron], 2) + '</td></tr>' +
      '<tr><td colspan="3">Total</td><td>' + fmt(sum, 2) + '</td></tr>' +
      '<tr><td colspan="3">After ReLU (negatives become 0)</td><td>' + fmt(Math.max(0, sum), 2) + '</td></tr>';
    $('tm-nn').textContent = String(neuron + 1);
    $('tm-work-note').textContent = sum > 0
      ? 'This neuron is switched ON for this soil (output ' + fmt(sum, 2) + '). The next layer will listen to it.'
      : 'The total is negative, so ReLU switches this neuron OFF (output 0). It stays silent for this soil.';
  }

  // ---- wiring ---------------------------------------------------------------
  var sel = $('tm-neuron');
  for (var i = 0; i < 24; i++) { var o = document.createElement('option'); o.value = i; o.textContent = String(i + 1); sel.appendChild(o); }
  sel.addEventListener('change', function () { neuron = parseInt(sel.value, 10) || 0; drawWork(); markSel(); });

  Array.prototype.forEach.call(document.querySelectorAll('[data-preset]'), function (b) {
    b.addEventListener('click', function () {
      var p = PRESETS[b.dataset.preset]; if (!p) return;
      FIELDS.forEach(function (f) { values[f.key] = Number(p[f.key]); });
      syncInputs(); update();
    });
  });

  buildInputs();
  SoilTiny.load(cfg.model).then(function (m) { model = m; update(); }).catch(function (e) {
    $('tm-top').innerHTML = '<p class="tm-state tm-bad">Could not load the TinyML model (' + esc(e.message) + ').</p>';
  });
})();
