"""
SoilSense TinyML on the website (server side).

The three tiny networks trained by tinyml/train_tinyml.py are stored as plain numbers in
static/tinyml/soilsense_tinyml.json. This file runs them with numpy only - no scikit-learn, no
TensorFlow - so it is fast and works on Vercel. The browser version (static/js/tinyml.js) does the
very same arithmetic, and so did the ESP32 firmware header.

    analyze(N, P, K, temp_c, moisture, ph) -> dict   (see below)
"""
import json
import os

import numpy as np

_PATH = os.path.join(os.path.dirname(__file__), "static", "tinyml", "soilsense_tinyml.json")

with open(_PATH) as _f:
    _M = json.load(_f)

_MEAN = np.array(_M["mean"], dtype=np.float64)
_STD  = np.array(_M["std"],  dtype=np.float64)
_CROP = [(np.array(l["W"]), np.array(l["b"])) for l in _M["crop"]]
_PROBE = [(np.array(l["W"]), np.array(l["b"])) for l in _M["probe"]]
_ADV  = [(np.array(l["W"]), np.array(l["b"])) for l in _M["advisor"]]
CROP_KEYS = _M["crop_keys"]
ADVICE    = _M["advice"]
THRESHOLD = float(_M["anomaly_threshold"])
METRICS   = _M["metrics"]
N_PARAMS  = _M["n_params"]
KB        = _M["kb_float32"]


def _dense(x, layer, relu):
    W, b = layer
    y = x @ W + b
    return np.maximum(y, 0.0) if relu else y


def analyze(N, P, K, temp_c, moisture, ph):
    """Run all three networks on one reading.

    Returns {"top3": [[crop_key, confidence_percent], x3],
             "odd": bool, "odd_err": float,
             "advice": [labels of the problems found]}
    which is the same shape the ESP32 firmware used to send, so the Sensors page shows it unchanged.
    """
    x = (np.array([N, P, K, temp_c, moisture, ph], dtype=np.float64) - _MEAN) / _STD

    # 1) crop recommender 6 -> 24 -> 22 (softmax)
    o = _dense(_dense(x, _CROP[0], True), _CROP[1], False)
    e = np.exp(o - o.max())
    p = e / e.sum()
    order = np.argsort(-p)[:3]
    top3 = [[CROP_KEYS[i], round(float(p[i]) * 100.0, 1)] for i in order]

    # 2) probe check: autoencoder 6-8-3-8-6. Big reconstruction error = does not look like real soil
    h = x
    for i, layer in enumerate(_PROBE):
        h = _dense(h, layer, i < len(_PROBE) - 1)
    err = float(np.mean((h - x) ** 2))

    # 3) nutrient advisor 6 -> 16 -> 8 (logit > 0  <=>  sigmoid > 0.5)
    a = _dense(_dense(x, _ADV[0], True), _ADV[1], False)
    advice = [ADVICE[i] for i in range(len(ADVICE)) if a[i] > 0]

    return {"top3": top3, "odd": bool(err > THRESHOLD), "odd_err": round(err, 4), "advice": advice}
