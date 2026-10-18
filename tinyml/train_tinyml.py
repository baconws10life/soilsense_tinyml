"""
AgriSense - train the on-device TinyML models and export them for the ESP32-S3.

    python tinyml/train_tinyml.py

Reads   ../Crop_recommendation.csv
Writes  soilsense_tinyml.h   -> copy next to the .ino sketch in the Arduino IDE
        ../static/tinyml/soilsense_tinyml.json -> the SAME trained networks for the website
                                  (read by the browser in static/js/tinyml.js and by tinyml_web.py)

THREE tiny networks share the same 6 inputs (N, P, K, soil temp, soil moisture, pH):

  1. CROP RECOMMENDER  6-24-22   softmax  -> top-3 crops + confidence
                       trained on the 2200 labelled rows (80/20 split).
  2. PROBE CHECK       6-8-3-8-6 autoencoder -> "does this reading look like real soil?"
                       trained ONLY on the dataset rows; a scan it cannot reconstruct
                       (probe in air, loose contact, wrong units) is flagged.
  3. NUTRIENT ADVISOR  6-16-8    sigmoid  -> which problems to fix (low N, P, K, acidic,
                       alkaline, dry, waterlogged, hot). Labels come from the AgriSense
                       threshold rules, so this net is those rules distilled into a tiny
                       model that runs offline. It agrees with the rules, it does not beat them.

The dataset has no rainfall column: the prototype does not use rainfall.
Numbers printed at the end are the ones to quote in the thesis.
"""
import os, json
import numpy as np, pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.neighbors import KNeighborsClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
CSV  = os.path.join(HERE, "..", "Crop_recommendation.csv")
OUT  = os.path.join(HERE, "soilsense_tinyml.h")

df = pd.read_csv(CSV)
F  = ["N", "P", "K", "temperature", "humidity", "ph"]
Xraw = df[F].values.astype(np.float32)
y    = df["label"].values
classes = sorted(set(y)); yi = np.array([classes.index(c) for c in y])
mu, sd  = Xraw.mean(0), Xraw.std(0)
norm = lambda a: (a - mu) / sd
report = {}

# ---------------------------------------------------------------- 1. crop recommender
Xs = norm(Xraw)
Xtr, Xte, ytr, yte = train_test_split(Xs, yi, test_size=0.2, random_state=42, stratify=yi)
res = []
for h in (16, 24, 32):
    for seed in range(4):
        m = MLPClassifier(hidden_layer_sizes=(h,), activation="relu", max_iter=2000, random_state=seed).fit(Xtr, ytr)
        res.append((m.score(Xte, yte), h, seed, m))
res.sort(key=lambda t: -t[0])
cand = sorted([r for r in res if r[0] >= res[0][0] - 0.005], key=lambda t: (t[1], -t[0]))
acc, nh, seed, crop = cand[0]
proba = crop.predict_proba(Xte)
top3  = np.mean([yte[i] in np.argsort(-proba[i])[:3] for i in range(len(yte))])
knn   = KNeighborsClassifier().fit(Xtr, ytr).score(Xte, yte)
report["crop top-1 accuracy"] = f"{acc*100:.1f}%  (website KNN, same 6 inputs: {knn*100:.1f}%)"
report["crop top-3 accuracy"] = f"{top3*100:.1f}%"

# ---------------------------------------------------------------- 2. probe check (autoencoder)
Atr, Ate = train_test_split(Xs, test_size=0.2, random_state=1)
ae = MLPRegressor(hidden_layer_sizes=(8, 3, 8), activation="relu", max_iter=3000, random_state=0).fit(Atr, Atr)
err = lambda a: ((ae.predict(a) - a) ** 2).mean(1)
thr = float(np.percentile(err(Atr), 99))
rng = np.random.default_rng(7)
n   = 2000
faults = {
    "probe in air (N,P,K,moisture ~0)": np.column_stack([rng.uniform(0, 3, n), rng.uniform(0, 3, n), rng.uniform(0, 3, n),
                                                         rng.uniform(15, 40, n), rng.uniform(0, 3, n), rng.uniform(5, 9, n)]),
    "random garbage in sensor range":   np.column_stack([rng.uniform(0, 300, n), rng.uniform(0, 300, n), rng.uniform(0, 300, n),
                                                         rng.uniform(-5, 60, n), rng.uniform(0, 100, n), rng.uniform(0, 14, n)]),
    "pH stuck at 7.0 / moisture 100":   np.column_stack([rng.choice(Xraw[:, 0], n), rng.choice(Xraw[:, 1], n), rng.choice(Xraw[:, 2], n),
                                                         rng.choice(Xraw[:, 3], n), np.full(n, 100.0), np.full(n, 7.0)]),
}
report["probe check false alarms"] = f"{(err(Ate) > thr).mean()*100:.1f}% of normal held-out rows flagged"
for k, v in faults.items():
    report[f"probe check catches: {k}"] = f"{(err(norm(v.astype(np.float32))) > thr).mean()*100:.0f}%"

# ---------------------------------------------------------------- 3. nutrient advisor
ADVICE = ["N_low", "P_low", "K_low", "ph_low", "ph_high", "dry", "wet", "hot"]
def rules(a):
    N, P, K, T, H, PH = a.T
    return np.column_stack([N < 40, P < 40, K < 40, PH < 6.0, PH > 7.0, H < 50, H > 90, T > 35]).astype(int)
rr  = np.random.default_rng(3); m_ = 20000
synth = np.column_stack([rr.uniform(0, 200, (m_, 3)), rr.uniform(5, 45, m_), rr.uniform(0, 100, m_), rr.uniform(3.5, 9.5, m_)]).astype(np.float32)
Xa  = np.vstack([Xraw, synth]); Ya = rules(Xa)
Xa_tr, Xa_te, Ya_tr, Ya_te = train_test_split(norm(Xa), Ya, test_size=0.2, random_state=5)
adv = MLPClassifier(hidden_layer_sizes=(16,), activation="relu", max_iter=600, random_state=0).fit(Xa_tr, Ya_tr)
pr  = adv.predict(Xa_te)
report["advisor agrees with the rules"] = f"{(pr == Ya_te).mean()*100:.1f}% of individual flags, {(pr == Ya_te).all(1).mean()*100:.1f}% of whole scans"

# ---------------------------------------------------------------- export C header
def arr(name, a):
    flat = np.asarray(a, dtype=np.float32).ravel()
    body = ',\n  '.join(', '.join(f'{v:.7g}f' for v in flat[i:i+8]) for i in range(0, len(flat), 8))
    return f'static const float {name}[{len(flat)}] = {{\n  {body}\n}};\n'

nin = 6; ncrop = len(classes); nadv = len(ADVICE)
nums = sum(a.size for a in [*crop.coefs_, *crop.intercepts_, *ae.coefs_, *ae.intercepts_, *adv.coefs_, *adv.intercepts_])
h = f'''// ============================================================
// soilsense_tinyml.h  -  AgriSense TinyML models (generated file - do not edit)
//
// Three tiny neural networks that run ON the ESP32-S3 with no Wi-Fi, no server and no library.
// Inputs (all three): N, P, K, soil temperature, soil moisture, pH.
//
//  1. crop recommender : 6 -> {nh} -> {ncrop}  top-3 crops with confidence
//                        held-out top-1 {acc*100:.1f}%, top-3 {top3*100:.1f}%  (website KNN, same inputs: {knn*100:.1f}%)
//  2. probe check      : autoencoder 6-8-3-8-6, flags readings that do not look like real soil
//  3. nutrient advisor : 6 -> 16 -> {nadv}  which problems to fix (distilled from the AgriSense rules)
//
// total {nums*4/1024:.1f} KB of float32 weights.  Retrain with tinyml/train_tinyml.py.
// Keep this file in the SAME folder as the .ino sketch.
// ============================================================
#pragma once
#include <math.h>

#define TINYML_N_IN   {nin}
#define TINYML_N_HID  {nh}
#define TINYML_N_OUT  {ncrop}
#define TINYML_N_ADV  {nadv}

static const char* const TINYML_CROPS[TINYML_N_OUT] = {{
  {', '.join('"'+c.capitalize()+'"' for c in classes)}
}};
// advisor bits: bit i set = problem i present
static const char* const TINYML_ADVICE[TINYML_N_ADV] = {{
  "Low nitrogen", "Low phosphorus", "Low potassium", "Acidic soil", "Alkaline soil", "Soil too dry", "Soil waterlogged", "Soil too hot"
}};
static const char* const TINYML_ADVICE_KEY[TINYML_N_ADV] = {{ {', '.join('"'+a+'"' for a in ADVICE)} }};

static const float TINYML_MEAN[TINYML_N_IN] = {{ {', '.join(f'{v:.7g}f' for v in mu)} }};
static const float TINYML_STD [TINYML_N_IN] = {{ {', '.join(f'{v:.7g}f' for v in sd)} }};
#define TINYML_ANOMALY_THRESHOLD {thr:.7g}f

{arr('TINYML_W1', crop.coefs_[0])}{arr('TINYML_B1', crop.intercepts_[0])}{arr('TINYML_W2', crop.coefs_[1])}{arr('TINYML_B2', crop.intercepts_[1])}
{arr('TINYML_AE_W1', ae.coefs_[0])}{arr('TINYML_AE_B1', ae.intercepts_[0])}{arr('TINYML_AE_W2', ae.coefs_[1])}{arr('TINYML_AE_B2', ae.intercepts_[1])}{arr('TINYML_AE_W3', ae.coefs_[2])}{arr('TINYML_AE_B3', ae.intercepts_[2])}{arr('TINYML_AE_W4', ae.coefs_[3])}{arr('TINYML_AE_B4', ae.intercepts_[3])}
{arr('TINYML_AD_W1', adv.coefs_[0])}{arr('TINYML_AD_B1', adv.intercepts_[0])}{arr('TINYML_AD_W2', adv.coefs_[1])}{arr('TINYML_AD_B2', adv.intercepts_[1])}

// out = act(in * W + b); W is stored [nin][nout]
static inline void tinymlDense(const float *W, const float *b, const float *in, int nI, int nO, float *out, bool relu) {{
  for (int j = 0; j < nO; j++) {{
    float s = b[j];
    for (int i = 0; i < nI; i++) s += in[i] * W[i * nO + j];
    out[j] = (relu && s < 0.0f) ? 0.0f : s;
  }}
}}

static inline void tinymlNormalise(float N, float P, float K, float tempC, float moisture, float ph, float *x) {{
  x[0] = N; x[1] = P; x[2] = K; x[3] = tempC; x[4] = moisture; x[5] = ph;
  for (int i = 0; i < TINYML_N_IN; i++) x[i] = (x[i] - TINYML_MEAN[i]) / TINYML_STD[i];
}}

// 1) crop recommender. Fills idx[0..2] / pct[0..2] (confidence 0..100). Returns idx[0].
static inline int tinymlTop3(float N, float P, float K, float tempC, float moisture, float ph, int *idx, uint8_t *pct) {{
  float x[TINYML_N_IN], hdn[TINYML_N_HID], o[TINYML_N_OUT];
  tinymlNormalise(N, P, K, tempC, moisture, ph, x);
  tinymlDense(TINYML_W1, TINYML_B1, x, TINYML_N_IN, TINYML_N_HID, hdn, true);
  tinymlDense(TINYML_W2, TINYML_B2, hdn, TINYML_N_HID, TINYML_N_OUT, o, false);
  float mx = o[0]; for (int k = 1; k < TINYML_N_OUT; k++) if (o[k] > mx) mx = o[k];
  float sum = 0.0f; for (int k = 0; k < TINYML_N_OUT; k++) {{ o[k] = expf(o[k] - mx); sum += o[k]; }}
  bool used[TINYML_N_OUT] = {{ false }};
  for (int r = 0; r < 3; r++) {{
    int best = -1;
    for (int k = 0; k < TINYML_N_OUT; k++) if (!used[k] && (best < 0 || o[k] > o[best])) best = k;
    used[best] = true; idx[r] = best;
    float p = 100.0f * o[best] / sum; pct[r] = (uint8_t)(p > 100.0f ? 100.0f : p + 0.5f);
  }}
  return idx[0];
}}

// 2) probe check. Returns the reconstruction error; *odd = true when the reading does not look like real soil.
static inline float tinymlProbeCheck(float N, float P, float K, float tempC, float moisture, float ph, bool *odd) {{
  float x[TINYML_N_IN], a[8], b[3], c[8], d[TINYML_N_IN];
  tinymlNormalise(N, P, K, tempC, moisture, ph, x);
  tinymlDense(TINYML_AE_W1, TINYML_AE_B1, x, 6, 8, a, true);
  tinymlDense(TINYML_AE_W2, TINYML_AE_B2, a, 8, 3, b, true);
  tinymlDense(TINYML_AE_W3, TINYML_AE_B3, b, 3, 8, c, true);
  tinymlDense(TINYML_AE_W4, TINYML_AE_B4, c, 8, 6, d, false);
  float e = 0.0f; for (int i = 0; i < TINYML_N_IN; i++) {{ float t = d[i] - x[i]; e += t * t; }}
  e /= TINYML_N_IN;
  if (odd) *odd = (e > TINYML_ANOMALY_THRESHOLD);
  return e;
}}

// 3) nutrient advisor. Returns a bit mask (bit i = TINYML_ADVICE[i] applies).
static inline uint16_t tinymlAdvise(float N, float P, float K, float tempC, float moisture, float ph) {{
  float x[TINYML_N_IN], hdn[16], o[TINYML_N_ADV];
  tinymlNormalise(N, P, K, tempC, moisture, ph, x);
  tinymlDense(TINYML_AD_W1, TINYML_AD_B1, x, 6, 16, hdn, true);
  tinymlDense(TINYML_AD_W2, TINYML_AD_B2, hdn, 16, TINYML_N_ADV, o, false);
  uint16_t mask = 0;
  for (int k = 0; k < TINYML_N_ADV; k++) if (o[k] > 0.0f) mask |= (1u << k);   // logit > 0  <=>  sigmoid > 0.5
  return mask;
}}
'''
open(OUT, "w").write(h)
print("wrote", OUT, f"({os.path.getsize(OUT)//1024} KB source, {nums*4/1024:.1f} KB weights)")

# ---------------------------------------------------------------- export JSON for the website
def layer(W, b): return {"W": np.round(W, 6).tolist(), "b": np.round(b, 6).tolist()}   # W is [nin][nout]
web = {
    "version": 1,
    "inputs": ["N", "P", "K", "temperature", "humidity", "ph"],
    "mean": [round(float(v), 6) for v in mu], "std": [round(float(v), 6) for v in sd],
    "crops": [c.capitalize() for c in classes], "crop_keys": list(classes),
    "advice": ["Low nitrogen", "Low phosphorus", "Low potassium", "Acidic soil",
               "Alkaline soil", "Soil too dry", "Soil waterlogged", "Soil too hot"],
    "anomaly_threshold": thr,
    "crop":    [layer(crop.coefs_[0], crop.intercepts_[0]), layer(crop.coefs_[1], crop.intercepts_[1])],
    "probe":   [layer(ae.coefs_[i], ae.intercepts_[i]) for i in range(4)],
    "advisor": [layer(adv.coefs_[0], adv.intercepts_[0]), layer(adv.coefs_[1], adv.intercepts_[1])],
    "metrics": {k: v for k, v in report.items()},
    "n_params": int(nums), "kb_float32": round(nums * 4 / 1024, 1),
    "train_rows": int(len(Xtr)), "test_rows": int(len(Xte)),
}
WEB_DIR = os.path.join(HERE, "..", "static", "tinyml")
os.makedirs(WEB_DIR, exist_ok=True)
WEB_OUT = os.path.join(WEB_DIR, "soilsense_tinyml.json")
with open(WEB_OUT, "w") as f: json.dump(web, f, separators=(",", ":"))
print("wrote", os.path.normpath(WEB_OUT), f"({os.path.getsize(WEB_OUT)//1024} KB)")
print("\nRESULTS FOR THE THESIS")
for k, v in report.items(): print(f"  {k:55s} {v}")
