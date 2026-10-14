"""
SoilSense - build the PHILIPPINE crop dataset  (../Crop_recommendation.csv)

    python data/make_ph_dataset.py

Same columns, same size and same units as the original dataset:
    N, P, K (mg/kg-style scale, 0-145 / 5-145 / 5-205), temperature (C), humidity (%), ph, label
(NO rainfall column: the prototype does not use rainfall)
22 crops x 100 rows = 2200 rows.  Only the CROPS changed. The N-P-K scale and its meaning are the same:
crops that were already in the original data and are grown in the Philippines (rice, corn, mungbean,
banana, mango, papaya, coconut, coffee, calamansi~orange) KEEP their original N-P-K ranges; the new crops
get N-P-K ranges on the same scale from their general nutrient demand (e.g. cabbage / sugarcane = heavy N,
cassava / abaca / pineapple = potassium-hungry, legumes = low N). Temperature, humidity and pH
are set for Philippine growing conditions (warm, humid, wet/dry seasons, highlands for cabbage / coffee).

IMPORTANT (for the paper): like the original, this is a SYNTHETIC reference table built from agronomic
ranges, not field measurements. Each row is drawn uniformly inside the crop's range below. Check/adjust the
ranges against BSWM / DA-BPI / PhilRice / PCAARRD references or your own soil samples, edit PROFILES, and run
the script again. Then retrain the TinyML networks:  python tinyml/train_tinyml.py
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(HERE, "..", "Crop_recommendation.csv")
ROWS_PER_CROP = 100
SEED = 2026

# crop: (N, P, K, temperature C, humidity %, pH)  each as (low, high)
PROFILES = {
    # ---- grains ----------------------------------------------------------------------------
    "rice":        ((60, 99), (35, 60),  (35, 45),   (24, 32), (75, 90), (5.0, 6.8)),  # NPK as original; paddy = very wet
    "corn":        ((60, 100), (35, 60), (15, 25),   (24, 34), (55, 75), (5.8, 7.0)),   # NPK as original "maize"
    # ---- root crops ------------------------------------------------------------------------
    "cassava":     ((20, 45), (15, 30),  (90, 130),  (25, 35), (55, 70), (5.0, 6.5)),  # low N, potassium-hungry
    "sweetpotato": ((20, 45), (30, 50),  (60, 90),   (22, 30), (65, 78), (5.5, 6.5)),   # kamote
    # ---- legumes ---------------------------------------------------------------------------
    "mungbean":    ((0, 40), (35, 60),   (15, 25),   (26, 32), (75, 90), (6.2, 7.2)),    # NPK as original; mongo
    "peanut":      ((10, 30), (25, 45),  (40, 60),   (25, 32), (55, 70), (5.8, 6.8)),   # mani
    "sitaw":       ((0, 40), (55, 80),   (15, 25),   (24, 32), (70, 85), (5.5, 6.8)),  # yardlong / string beans; legume NPK as original
    # ---- vegetables ------------------------------------------------------------------------
    "tomato":      ((90, 130), (60, 90), (100, 140), (22, 30), (65, 80), (5.8, 6.8)),
    "eggplant":    ((70, 110), (40, 65), (70, 100),  (24, 32), (70, 85), (5.8, 6.8)),  # talong
    "ampalaya":    ((50, 80), (25, 45),  (45, 70),   (24, 32), (70, 85), (6.0, 7.0)),  # bitter gourd
    "cabbage":     ((110, 140), (55, 85), (70, 100), (15, 24), (75, 90), (6.0, 7.0)),  # highland (Benguet)
    "onion":       ((60, 90), (55, 85),  (80, 110),  (20, 30), (55, 70), (6.0, 7.0)),    # dry-season crop (Nueva Ecija)
    # ---- fruits ----------------------------------------------------------------------------
    "banana":      ((80, 120), (70, 95), (45, 55),   (25, 32), (75, 88), (5.5, 6.5)),  # NPK as original
    "mango":       ((0, 40), (15, 40),   (25, 35),   (26, 36), (50, 65), (5.5, 7.0)),   # NPK as original; needs a dry season
    "pineapple":   ((55, 80), (5, 20),   (70, 95),   (24, 32), (70, 85), (4.5, 5.5)),  # acid-loving
    "papaya":      ((31, 70), (46, 70),  (45, 55),   (24, 34), (80, 92), (6.0, 6.8)),  # NPK as original
    "calamansi":   ((10, 40), (5, 30),   (5, 20),    (24, 32), (78, 90), (5.5, 6.5)),  # citrus NPK as original "orange"
    # ---- cash / industrial crops -----------------------------------------------------------
    "coconut":     ((0, 40), (5, 30),    (25, 35),   (25, 32), (88, 98), (5.5, 6.5)),  # NPK as original; niyog
    "sugarcane":   ((100, 140), (30, 50), (80, 120), (26, 34), (65, 80), (6.0, 7.5)),
    "coffee":      ((80, 120), (15, 40), (25, 35),   (18, 28), (60, 80), (5.0, 6.5)),  # NPK as original; highland
    "cacao":       ((45, 70), (20, 35),  (55, 80),   (24, 30), (80, 92), (5.5, 6.8)),
    "abaca":       ((60, 100), (20, 40), (90, 120),  (24, 30), (80, 92), (5.5, 6.8)),  # Manila hemp; analog of original "jute"
}

rng  = np.random.default_rng(SEED)
rows = []
for crop, (n, p, k, t, h, ph) in PROFILES.items():
    d = pd.DataFrame({
        "N": rng.integers(n[0], n[1] + 1, ROWS_PER_CROP),
        "P": rng.integers(p[0], p[1] + 1, ROWS_PER_CROP),
        "K": rng.integers(k[0], k[1] + 1, ROWS_PER_CROP),
        "temperature": rng.uniform(*t, ROWS_PER_CROP).round(6),
        "humidity":    rng.uniform(*h, ROWS_PER_CROP).round(6),
        "ph":          rng.uniform(*ph, ROWS_PER_CROP).round(6),
        "label": crop,
    })
    rows.append(d)

df = pd.concat(rows, ignore_index=True)
df.to_csv(OUT, index=False)
print(f"wrote {os.path.normpath(OUT)}: {len(df)} rows, {df['label'].nunique()} crops")
