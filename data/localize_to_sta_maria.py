"""
AgriSense - STEP 2 of 2: calibrate the crop preset to the Sta. Maria (Bulacan) soil survey.

    python data/localize_to_sta_maria.py        (needs: pandas, numpy, openpyxl)

Reads   data/Crop_recommendation_agronomic_base.csv   (step 1: textbook agronomic ranges, 22 crops x 100 rows)
        data/sta_maria/Sta_Maria_STK.xlsx             328 soil-test-kit samples:  pH, N, P, K as Low / Medium / High
        data/sta_maria/Sta_Maria_FM.xlsx              122 laboratory samples:     pH, OM %, P (ppm), K (ppm)
Writes  ../Crop_recommendation.csv                    the crop preset the website and the TinyML networks use
        data/Sta_Maria_local_soil_clean.csv           the two surveys cleaned and merged (for the paper / checking)

WHAT IS CHANGED AND WHY
  The surveys have NO crop label, NO temperature and NO humidity, so they cannot teach the model which crop
  grows where. What they DO tell us is what soil in Sta. Maria really looks like: nitrogen is almost always Low,
  potassium is almost always High, phosphorus is mostly Low, pH is moderately acidic. The textbook N-P-K numbers
  in the base preset (cabbage N 110-140, ...) are crop *demand* guesses on a scale no local soil ever reaches,
  so a probe reading taken in Sta. Maria would never sit near them.

  So N, P and K are re-expressed on the LOCAL soil scale with quantile transfer: every row keeps its rank among
  the 2200 rows (a heavy-nitrogen crop stays heavy-nitrogen compared with a legume), but the value it gets is the
  value at that rank in the Sta. Maria soils.
      * share of soils at each level (Low / Medium / High)  <- STK survey (it has N, P, K and pH)
      * how values spread INSIDE a level (P, K)             <- FM lab survey (continuous ppm, log scale), rank-matched
        into the level's band. FM has no N, so N is spread evenly inside its level.
      * a level is turned into mg/kg-style numbers with the SAME limits the website already uses
        (below 40 = low, 40-79 = medium, 80 and up = high), so a "Low" in the survey is a "low" on the website.
  pH, temperature and humidity are NOT changed: pH is an absolute quantity the probe reads directly (the crop pH
  ranges already sit inside the local span 4.2-8.2) and the surveys carry no climate data.

ASSUMPTION to state in the paper: the STK and FM surveys sample the same population (both Sta. Maria, Bulacan,
overlapping barangays), so FM's spread can be placed inside STK's levels.
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, "Crop_recommendation_agronomic_base.csv")
STK  = os.path.join(HERE, "sta_maria", "Sta_Maria_STK.xlsx")
FM   = os.path.join(HERE, "sta_maria", "Sta_Maria_FM.xlsx")
OUT  = os.path.join(HERE, "..", "Crop_recommendation.csv")
CLEAN = os.path.join(HERE, "Sta_Maria_local_soil_clean.csv")
SEED = 2026

# Level -> number band on the website's scale (same limits as READING_RANGES in app.py: 40 = low/optimal edge)
BANDS = {"N": {"L": (0, 39),  "M": (40, 79), "H": (80, 140)},
         "P": {"L": (5, 39),  "M": (40, 79), "H": (80, 145)},
         "K": {"L": (5, 39),  "M": (40, 79), "H": (80, 145)}}
LEVELS = ("L", "M", "H")


def load_surveys():
    stk = pd.read_excel(STK)
    fm  = pd.read_excel(FM)
    stk.columns = [c.strip() for c in stk.columns]
    fm.columns  = [c.strip() for c in fm.columns]
    for c in ("P", "K", "N"):
        stk[c] = stk[c].astype(str).str.strip().str.upper()
        bad = ~stk[c].isin(LEVELS)
        if bad.any():
            raise SystemExit(f"STK column {c}: unexpected values {stk.loc[bad, c].unique()}")
    stk["BRGY."] = stk["BRGY."].fillna("(not recorded)")
    return stk, fm


def level_shares(col):
    """Share of Low / Medium / High soils in the STK survey."""
    vc = col.value_counts()
    return np.array([vc.get(l, 0) for l in LEVELS], float) / len(col)


def make_quantile_fn(nutrient, shares, fm_values=None):
    """Return Q(u): u in [0,1] (rank among soils) -> mg/kg-style value on the local scale."""
    cum = np.concatenate([[0.0], np.cumsum(shares)])
    cum[-1] = 1.0
    # lab ppm values are right-skewed (K reaches 900 ppm), so their SHAPE is taken on a log scale
    fm_sorted = None if fm_values is None else np.sort(np.log1p(np.asarray(fm_values, float)))

    def shape(u_lo, u_hi, u):
        """0..1 position of rank u inside a level; evenly spread, or shaped by the FM lab values."""
        t = (u - u_lo) / max(u_hi - u_lo, 1e-9)
        if fm_sorted is None:
            return t
        lo, hi = np.quantile(fm_sorted, u_lo), np.quantile(fm_sorted, u_hi)
        if hi - lo < 1e-9:
            return t
        return np.clip((np.quantile(fm_sorted, u) - lo) / (hi - lo), 0, 1)

    def Q(u):
        u = np.clip(np.asarray(u, float), 0, 1 - 1e-9)
        out = np.empty_like(u)
        for i, lvl in enumerate(LEVELS):
            if shares[i] <= 0:
                continue
            m = (u >= cum[i]) & (u < cum[i + 1]) if i < 2 else (u >= cum[i])
            if not m.any():
                continue
            lo, hi = BANDS[nutrient][lvl]
            t = np.array([shape(cum[i], cum[i + 1], x) for x in u[m]])
            out[m] = lo + t * (hi - lo)
        return out
    return Q


def main():
    stk, fm = load_surveys()
    base = pd.read_csv(BASE)
    rng = np.random.default_rng(SEED)

    shares = {n: level_shares(stk[n]) for n in "NPK"}
    fm_vals = {"N": None, "P": fm["P"], "K": fm["K"]}

    df = base.copy()
    for n in "NPK":
        # rank of each row among all rows (random tie-break so equal textbook values do not collapse to one number)
        order = np.lexsort((rng.random(len(df)), df[n].values))
        rank = np.empty(len(df)); rank[order] = np.arange(len(df))
        u = (rank + 0.5) / len(df)
        df[n] = np.rint(make_quantile_fn(n, shares[n], fm_vals[n])(u)).astype(int)

    df.to_csv(OUT, index=False)

    # ---- cleaned local soil table (evidence for the paper) -----------------------------------------------------
    a = stk.rename(columns={"SAMPLE NO.": "sample", "BRGY.": "barangay", "pH": "pH",
                            "N": "N_level", "P": "P_level", "K": "K_level"})
    a["survey"] = "STK"; a["OM_pct"] = np.nan; a["P_ppm"] = np.nan; a["K_ppm"] = np.nan
    b = fm.rename(columns={"SAMPLE NO.": "sample", "BRGY.": "barangay", "OM": "OM_pct", "P": "P_ppm", "K": "K_ppm"})
    b["survey"] = "FM"; b["N_level"] = b["P_level"] = b["K_level"] = np.nan
    cols = ["survey", "sample", "barangay", "pH", "N_level", "P_level", "K_level", "OM_pct", "P_ppm", "K_ppm"]
    pd.concat([a[cols], b[cols]], ignore_index=True).to_csv(CLEAN, index=False)

    # ---- report -------------------------------------------------------------------------------------------------
    print(f"wrote {os.path.normpath(OUT)}: {len(df)} rows, {df['label'].nunique()} crops")
    print(f"wrote {os.path.normpath(CLEAN)}: {len(a)} STK + {len(b)} FM samples")
    print("\nlevel shares in Sta. Maria (STK):")
    for n in "NPK":
        print(f"  {n}: " + "  ".join(f"{l}={s*100:.0f}%" for l, s in zip(LEVELS, shares[n])))
    print("\nbefore -> after (mean, min-max)")
    for n in "NPK":
        print(f"  {n}: {base[n].mean():5.1f} ({base[n].min()}-{base[n].max()})  ->  {df[n].mean():5.1f} ({df[n].min()}-{df[n].max()})")
    return df


if __name__ == "__main__":
    main()
