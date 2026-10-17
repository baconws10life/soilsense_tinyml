/* AgriSense TinyML in the browser.
   Loads static/tinyml/agrisense_tinyml.json (the weights trained by tinyml/train_tinyml.py) and runs the
   three tiny networks with plain JavaScript maths - no library. It does the same arithmetic as
   tinyml_web.py on the server and as agrisense_tinyml.h did on the ESP32.

     SoilTiny.load(url).then(function (model) {
       var r = model.run([N, P, K, tempC, moisture, pH]);
       r.crop.top3      -> [{key, label, p}]   p = 0..100
       r.probe.odd      -> true when the reading does not look like real soil
       r.advisor.found  -> ["Low nitrogen", ...]
       r.trace          -> every layer's numbers (for the "look inside" view)
     });
*/
(function (g) {
  "use strict";

  function dense(x, L, relu) {
    var nO = L.b.length, out = new Array(nO);
    for (var j = 0; j < nO; j++) {
      var s = L.b[j];
      for (var i = 0; i < x.length; i++) s += x[i] * L.W[i][j];
      out[j] = (relu && s < 0) ? 0 : s;
    }
    return out;
  }

  function Model(m) { this.m = m; }

  Model.prototype.run = function (raw) {
    var m = this.m, x = raw.map(function (v, i) { return (v - m.mean[i]) / m.std[i]; });

    // 1) crop recommender: 6 -> 24 (ReLU) -> 22 (softmax)
    var h1 = dense(x, m.crop[0], true);
    var o1 = dense(h1, m.crop[1], false);
    var mx = Math.max.apply(null, o1), sum = 0;
    var e = o1.map(function (v) { var t = Math.exp(v - mx); sum += t; return t; });
    var p = e.map(function (v) { return v / sum; });
    var order = p.map(function (v, i) { return i; }).sort(function (a, b) { return p[b] - p[a]; });
    var top = order.map(function (i) { return { key: m.crop_keys[i], label: m.crops[i], p: p[i] * 100 }; });

    // 2) probe check: autoencoder 6-8-3-8-6, error = how badly it rebuilds the reading
    var a = x, acts = [];
    for (var i = 0; i < m.probe.length; i++) { a = dense(a, m.probe[i], i < m.probe.length - 1); acts.push(a); }
    var err = 0; for (var k = 0; k < x.length; k++) err += (a[k] - x[k]) * (a[k] - x[k]);
    err /= x.length;

    // 3) nutrient advisor: 6 -> 16 -> 8 (logit > 0  <=>  sigmoid > 0.5)
    var ah = dense(x, m.advisor[0], true);
    var ao = dense(ah, m.advisor[1], false);
    var found = [];
    for (var j = 0; j < ao.length; j++) if (ao[j] > 0) found.push(m.advice[j]);

    return {
      crop:    { top3: top.slice(0, 3), all: top },
      probe:   { err: err, threshold: m.anomaly_threshold, odd: err > m.anomaly_threshold, rebuilt: a },
      advisor: { found: found, logits: ao },
      trace:   { x: x, hidden: h1, out: o1, probs: p }
    };
  };

  g.SoilTiny = {
    load: function (url) {
      return fetch(url, { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('model file ' + r.status);
        return r.json();
      }).then(function (m) { return new Model(m); });
    }
  };
})(window);
