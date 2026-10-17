"""
AgriSense — TinyML Soil Intelligence Platform
================================================
Identical to the original app.py EXCEPT the database layer has been
switched from SQLite to PostgreSQL (Neon) so it can run on Vercel.

WHAT CHANGED  (everything else is byte-for-byte the same):
  - import sqlite3            →  import psycopg2 / psycopg2.extras
  - sqlite3.connect(...)      →  psycopg2.connect(DATABASE_URL)
  - conn.row_factory          →  RealDictCursor  (rows behave like dicts)
  - placeholder  ?            →  %s
  - conn.execute(sql)         →  cur = conn.cursor(); cur.execute(sql)
  - INTEGER PRIMARY KEY AUTOINCREMENT  →  SERIAL PRIMARY KEY
  - TEXT DEFAULT 'manual'     →  TEXT DEFAULT 'manual'  (same, Postgres ok)
  - INSERT OR IGNORE          →  INSERT ... ON CONFLICT DO NOTHING
  - datetime('now')           →  NOW()
  - PRAGMA table_info / ALTER →  information_schema check (safe migration)
  - _add_column_if_missing    →  postgres-safe version
  - record_to_dict            →  rows are already plain dicts via RealDictCursor

DEVICE PAIRING (added): /api/device/pair, /api/device/unlink, /pair, /pair/unlink,
  a `devices` table, and /api/esp32 now identifies the user from the linked device.

WHAT DID NOT CHANGE:
  All domain logic, ML model, crop knowledge base, AI assistant,
  soil scoring, all routes, all template calls — untouched.
"""

from flask import Flask, render_template, request, redirect, session, jsonify, url_for
import psycopg2
import psycopg2.extras
import psycopg2.errors
import os
import re
import hashlib
import json
import secrets
import traceback
from datetime import datetime
from collections import Counter, OrderedDict

from sklearn.neighbors import KNeighborsClassifier
import pandas as pd

try:
    import tinyml_web            # the TinyML networks, run with numpy (see tinyml_web.py)
except Exception as _e:          # never take the whole site down because of the extra model
    tinyml_web = None
    print(f"[AgriSense] TinyML on the website is disabled: {_e}")

app = Flask(__name__)
app.secret_key = os.environ.get("AGRISENSE_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals["zip"] = zip

BASE_DIR     = os.path.dirname(__file__)
CSV_PATH     = os.path.join(BASE_DIR, "Crop_recommendation.csv")
# Vercel's deployment folder (/var/task) is read-only. Only /tmp is writable,
# and it is temporary (wiped between invocations), so uploads here are not permanent.
UPLOAD_FOLDER = os.path.join("/tmp", "uploads")
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except OSError as _e:
    print(f"[AgriSense] Could not create upload folder: {_e}")

# Placeholder plot name / size were removed from the UI (they were invented data).
DEVICE_NAME     = ""
DEVICE_SUBTITLE = ""
DATABASE_URL    = os.environ.get("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# SHOW REAL ERRORS instead of a blank 500
# ---------------------------------------------------------------------------
@app.errorhandler(500)
def internal_error(e):
    tb = traceback.format_exc()
    db_hint = (
        f"DATABASE_URL starts with: {DATABASE_URL[:40]}..."
        if DATABASE_URL else
        "DATABASE_URL is NOT SET — add it in Vercel → Settings → Environment Variables"
    )
    return (
        f"<html><body style='font-family:monospace;padding:30px;"
        f"background:#111;color:#eee;'>"
        f"<h2 style='color:#f87171;'>500 — Internal Server Error</h2>"
        f"<p style='color:#fcd34d;'>{db_hint}</p>"
        f"<pre style='background:#1e1e1e;padding:20px;border-radius:8px;"
        f"color:#86efac;overflow:auto;white-space:pre-wrap;'>{tb}</pre>"
        f"</body></html>"
    ), 500


# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Add it in Vercel → Settings → Environment Variables."
        )
    # Neon sometimes hands out postgres:// but psycopg2 needs postgresql://
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def _column_exists(conn, table, column):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
        """,
        (table, column),
    )
    exists = cur.fetchone() is not None
    cur.close()
    return exists


def _add_column_if_missing(conn, table, column, coltype):
    if not _column_exists(conn, table, column):
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        cur.close()


# Admin account provisioning
#   * The admin lives in the database (role = 'admin'). It is never offered on
#     the registration form.
#   * OPTIONAL: set ADMIN_USERNAME and ADMIN_PASSWORD in Vercel -> Settings ->
#     Environment Variables and that account is created automatically (only if
#     it does not exist yet) - handy for a brand-new database or recovery.
#     No default password is stored in the code.
#   * More admins: log in as an admin -> Admin Panel -> Add New User -> Admin.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Names the public cannot register (stops look-alike "admin" accounts and
# stops someone grabbing the seeded admin's name first). Compared lower-case.
RESERVED_USERNAMES = {"admin", "administrator"}
if ADMIN_USERNAME:
    RESERVED_USERNAMES.add(ADMIN_USERNAME.lower())


def init_db():
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username     TEXT PRIMARY KEY,
            password     TEXT,
            role         TEXT,
            display_name TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id         SERIAL PRIMARY KEY,
            username   TEXT,
            "N"        REAL, "P" REAL, "K" REAL,
            ph         REAL, temp REAL, humidity REAL,
            crop       TEXT, image TEXT,
            source     TEXT DEFAULT 'manual',
            fertility  REAL,
            created_at TEXT
        )
    """)

    # Devices (ESP32 scanners). A device is identified by a secret token that it
    # generates itself; only the SHA-256 hash is stored. `owner` is the website
    # account the device is currently linked to (NULL = waiting to be linked).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            token_hash   TEXT PRIMARY KEY,
            owner        TEXT,
            pair_code    TEXT,
            pair_expires TIMESTAMPTZ,
            paired_at    TEXT,
            last_seen    TEXT,
            created_at   TEXT
        )
    """)

    # Device discovery (added): the scanner "checks in" every few seconds, the website
    # lists the ones that are online, and a user claims a free one. These columns hold
    # the live state; the old pair_code columns are no longer used.
    for _col, _type in (("last_seen_at", "TIMESTAMPTZ"), ("connected_at", "TIMESTAMPTZ"),
                        ("rssi", "INTEGER"), ("sensor_state", "TEXT"), ("fw", "TEXT"),
                        ("released_user", "TEXT"), ("released_reason", "TEXT"), ("dev_name", "TEXT"),
                        ("released_at", "TIMESTAMPTZ"),
                        ("scan_requested_at", "TIMESTAMPTZ")):
        _add_column_if_missing(conn, "devices", _col, _type)

    # Idempotent migrations for columns added after initial release
    _add_column_if_missing(conn, "records", "source",     "TEXT DEFAULT 'manual'")
    _add_column_if_missing(conn, "records", "fertility",  "REAL")
    _add_column_if_missing(conn, "records", "created_at", "TEXT")
    _add_column_if_missing(conn, "users",   "display_name", "TEXT")
    # TinyML + offline sync (added): who picked the crop, how sure it was, was the
    # scan stored on the ESP32 while offline, and a unique id so a re-sent scan
    # is never saved twice.
    _add_column_if_missing(conn, "records", "crop_source", "TEXT")
    _add_column_if_missing(conn, "records", "tinyml_conf", "REAL")
    _add_column_if_missing(conn, "records", "offline",     "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "records", "scan_uid",    "TEXT")
    _add_column_if_missing(conn, "records", "tiny_json",   "TEXT")   # top-3 crops, probe check, advisor

    # Backfill created_at for any legacy rows
    cur.execute("UPDATE records SET created_at = NOW()::TEXT WHERE created_at IS NULL")

    # Optional preset admin from environment variables (see ADMIN_* above).
    # ON CONFLICT DO NOTHING: safe on every cold start, never overwrites an
    # existing account or password.
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        cur.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, 'admin') "
            "ON CONFLICT (username) DO NOTHING",
            (ADMIN_USERNAME, ADMIN_PASSWORD)
        )

    conn.commit()
    cur.close()
    conn.close()


try:
    init_db()
except Exception as _e:
    print(f"[AgriSense] DB init warning (will retry on next request): {_e}")


# ---------------------------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return jsonify({
            "status":   "ok",
            "database": "connected",
            "url_prefix": DATABASE_URL[:30] + "..." if DATABASE_URL else "NOT SET",
        })
    except Exception as ex:
        return jsonify({"status": "error", "error": str(ex)}), 500


# ---------------------------------------------------------------------------
# ML MODEL + CROP KNOWLEDGE BASE   (crops = Philippine crops, see data/make_ph_dataset.py)
# ---------------------------------------------------------------------------
FEATURES = ["N", "P", "K", "temperature", "humidity", "ph"]     # no rainfall: the prototype does not use it

df     = pd.read_csv(CSV_PATH)
X      = df[FEATURES]
y      = df["label"]
model  = KNeighborsClassifier(n_neighbors=5)
model.fit(X, y)

_crop_bands = {}
for label, group in df.groupby("label"):
    _crop_bands[label] = {
        feat: (
            float(group[feat].quantile(0.10)),
            float(group[feat].quantile(0.90)),
            float(group[feat].mean()),
        )
        for feat in FEATURES
    }

CROP_INFO = {
    "rice":        dict(category="Grains", emoji="\U0001F33E", harvest="105-130 days",
                        desc="Rice (palay) is the Philippines' staple: it thrives in warm, wet lowland paddies or irrigated fields with plenty of water and slightly acidic, clay-rich soil."),
    "corn":        dict(category="Grains", emoji="\U0001F33D", harvest="90-115 days",
                        desc="Corn (mais) is the country's second staple grain: it wants fertile, well-drained soil, steady warmth and steady moisture, and responds strongly to nitrogen."),
    "cassava":     dict(category="Root Crops", emoji="\U0001F360", harvest="8-12 months",
                        desc="Cassava (kamoteng kahoy) tolerates poor, sandy or sloping soil and dry spells, needs little nitrogen but plenty of potassium, and is a food, feed and starch crop."),
    "sweetpotato": dict(category="Root Crops", emoji="\U0001F360", harvest="90-120 days",
                        desc="Sweet potato (kamote) grows fast in loose, well-drained soil with moderate moisture; too much nitrogen gives lots of leaves but small roots."),
    "mungbean":    dict(category="Legumes", emoji="\U0001F331", harvest="60-70 days",
                        desc="Mung bean (mongo) is a fast, drought-tolerant legume that fits after rice; it fixes its own nitrogen, so it needs very little added."),
    "peanut":      dict(category="Legumes", emoji="\U0001F95C", harvest="100-120 days",
                        desc="Peanut (mani) wants light, loose, well-drained sandy loam with moderate moisture and enough potassium and calcium for the pods to fill."),
    "sitaw":       dict(category="Legumes", emoji="\U0001FADB", harvest="55-70 days",
                        desc="Sitaw (yardlong or string bean) climbs on trellises in warm, humid weather and well-drained loam, and is a quick-cycle vegetable legume."),
    "tomato":      dict(category="Vegetables", emoji="\U0001F345", harvest="65-90 days",
                        desc="Tomato likes well-drained, fertile loam, plenty of phosphorus and potassium, and relatively cool, drier weather; waterlogging brings disease."),
    "eggplant":    dict(category="Vegetables", emoji="\U0001F346", harvest="75-110 days",
                        desc="Eggplant (talong) is a hardy, warm-season vegetable that fruits for months in fertile, well-drained soil with steady moisture."),
    "ampalaya":    dict(category="Vegetables", emoji="\U0001F952", harvest="60-90 days",
                        desc="Ampalaya (bitter gourd) is a warm, humid-weather vine that needs a trellis, moderate fertility and soil that never waterlogs."),
    "cabbage":     dict(category="Vegetables", emoji="\U0001F96C", harvest="70-90 days",
                        desc="Cabbage is a highland crop (Benguet, Bukidnon): it needs cool weather, lots of nitrogen and fertile, moisture-holding loam."),
    "onion":       dict(category="Vegetables", emoji="\U0001F9C5", harvest="100-120 days",
                        desc="Onion (sibuyas) is a dry-season crop (Nueva Ecija, Ilocos) that needs fertile, well-drained loam, high potassium and a dry spell at bulbing."),
    "banana":      dict(category="Fruits", emoji="\U0001F34C", harvest="9-12 months",
                        desc="Banana (saging) needs rich, moisture-retentive soil, high humidity and steady warmth all year round."),
    "mango":       dict(category="Fruits", emoji="\U0001F96D", harvest="Perennial (100-150 days post-flower)",
                        desc="Mango (mangga) trees want a distinct dry season to flower, warm temperatures and deep, well-drained soil."),
    "pineapple":   dict(category="Fruits", emoji="\U0001F34D", harvest="14-18 months",
                        desc="Pineapple (pinya) is one of the few crops that likes acidic soil (pH 4.5-5.5); it needs good drainage and plenty of potassium."),
    "papaya":      dict(category="Fruits", emoji="\U0001F348", harvest="9-11 months",
                        desc="Papaya grows fast in hot, humid weather with rich, well-drained soil and protection from strong wind and standing water."),
    "calamansi":   dict(category="Fruits", emoji="\U0001F34B", harvest="Perennial (fruits 2-3 years after planting)",
                        desc="Calamansi is the Philippines' own citrus: it likes warm, humid weather, slightly acidic, well-drained loam and light but regular feeding."),
    "coconut":     dict(category="Cash Crops", emoji="\U0001F965", harvest="Perennial (6-10 years to first nuts)",
                        desc="Coconut (niyog) palms love hot, humid coastal climates with sandy or loamy soil and abundant, well-spread water."),
    "sugarcane":   dict(category="Cash Crops", emoji="\U0001F38D", harvest="10-14 months",
                        desc="Sugarcane (tubo) is a heavy feeder: it needs deep fertile soil, lots of nitrogen and potassium, warm weather and good drainage."),
    "coffee":      dict(category="Cash Crops", emoji="\U00002615", harvest="Perennial (~8-10 months flower-to-cherry)",
                        desc="Coffee (kape) grows best in the cool uplands with partial shade, high humidity and slightly acidic, well-drained, fertile soil."),
    "cacao":       dict(category="Cash Crops", emoji="\U0001F36B", harvest="Perennial (~5-6 months pod-to-harvest)",
                        desc="Cacao grows under shade in warm, humid, sheltered sites (often under coconut), in deep, moist, well-drained, slightly acidic soil."),
    "abaca":       dict(category="Cash Crops", emoji="\U0001F33F", harvest="Perennial (stalks cut every 3-4 months)",
                        desc="Abaca (Manila hemp) is a banana relative grown for fibre: it needs rich, moist, well-drained volcanic or alluvial soil, high humidity and lots of potassium."),
}
CROP_CATEGORIES = ["Grains", "Root Crops", "Legumes", "Vegetables", "Fruits", "Cash Crops"]


def recommend_crop(N, P, K, temp, humidity, ph):
    sample     = pd.DataFrame([[N, P, K, temp, humidity, ph]], columns=FEATURES)
    prediction = model.predict(sample)[0]
    info       = CROP_INFO.get(prediction.lower(), {})
    base_desc  = info.get("desc", "This crop is well suited to the analyzed soil and climate conditions.")
    description = f"{base_desc} (Based on your input: pH {ph}, {temp}\u00b0C, {humidity}% humidity)"
    return {
        "name":     prediction,
        "label":    prediction.capitalize(),
        "desc":     description,
        "emoji":    info.get("emoji",    "\U0001F331"),
        "category": info.get("category", "Other"),
        "harvest":  info.get("harvest",  "Varies"),
    }


def _band_score(value, low, high):
    span   = max(high - low, 1e-6)
    margin = span * 0.25
    if low <= value <= high: return 1.0
    if value < low:          return max(0.0, 1 - (low  - value) / margin)
    return                          max(0.0, 1 - (value - high)  / margin)


def crop_matches(N, P, K, temp, humidity, ph, category=None, suitable_only=False):
    reading = {"N": N, "P": P, "K": K, "temperature": temp,
               "humidity": humidity, "ph": ph}
    results = []
    for label, bands in _crop_bands.items():
        scores = {feat: _band_score(reading[feat], bands[feat][0], bands[feat][1]) for feat in FEATURES}
        pct    = round(sum(scores.values()) / len(FEATURES) * 100)
        tier   = "Excellent" if pct >= 85 else "Good" if pct >= 60 else "Fair"
        info   = CROP_INFO.get(label, {})
        checks = {
            "Soil pH":     scores["ph"]          >= 0.999,
            "Moisture":    scores["humidity"]     >= 0.999,
            "Temperature": scores["temperature"]  >= 0.999,
            "Nitrogen":    scores["N"]            >= 0.999,
        }
        results.append({
            "name": label, "label": label.capitalize(),
            "emoji":    info.get("emoji",    "\U0001F331"),
            "category": info.get("category", "Other"),
            "desc":     info.get("desc",     ""),
            "harvest":  info.get("harvest",  "Varies"),
            "match": pct, "tier": tier, "checks": checks,
        })
    if category and category != "All Crops":
        results = [r for r in results if r["category"] == category]
    if suitable_only:
        results = [r for r in results if r["match"] >= 60]
    return sorted(results, key=lambda r: (-r["match"], r["label"]))


# ---------------------------------------------------------------------------
# SOIL HEALTH SCORING   (unchanged)
# ---------------------------------------------------------------------------
READING_RANGES = {
    "ph":       dict(label="Soil pH",     unit="pH",    icon="\U0001F9EA",        scale=(0,10),  optimal=(6.0,7.0),  watch=(5.5,7.5)),
    "humidity": dict(label="Moisture",    unit="%",     icon="\U0001F4A7",        scale=(0,100), optimal=(50,90),    watch=(40,95)),
    "temp":     dict(label="Temperature", unit="\u00b0C",icon="\U0001F321\ufe0f", scale=(0,50),  optimal=(20,35),    watch=(15,40)),
    "N":        dict(label="Nitrogen",    unit="mg/kg", icon="\U0001F343",        scale=(0,150), optimal=(40,120),   watch=(25,130)),
    "P":        dict(label="Phosphorus",  unit="mg/kg", icon="\U0001F33F",        scale=(0,150), optimal=(40,120),   watch=(25,130)),
    "K":        dict(label="Potassium",   unit="mg/kg", icon="\U0001F9C2",        scale=(0,220), optimal=(40,120),   watch=(25,150)),
}
READING_COPY = {
    "ph":       {"low":"Acidic soil — most crops will struggle to take up nutrients below pH 5.5.","watch":"Slightly outside the sweet spot — worth watching but not urgent.","optimal":"Slightly acidic to neutral — great for most crops!","high":"Alkaline soil — some micronutrients become harder for roots to absorb."},
    "humidity": {"low":"Dry — irrigation is likely needed soon.","watch":"A little below field capacity — keep an eye on it.","optimal":"At field capacity — no irrigation needed.","high":"Saturated soil — risk of root rot in poorly drained plots."},
    "temp":     {"low":"Cool — growth slows for most warm-season crops below this range.","watch":"A bit cool for peak growth, but workable.","optimal":"Warm and ideal for active root and shoot growth.","high":"Hot — heat stress risk without shade or extra irrigation."},
    "N":        {"low":"Below target — leaves may turn pale or yellow soon.","watch":"On the low side — a light nitrogen feed would help.","optimal":"Healthy nitrogen level for steady leafy growth.","high":"High nitrogen — can delay flowering and attract pests."},
    "P":        {"low":"Low phosphorus can stunt root development and delay flowering.","watch":"Slightly low — monitor root and flower development.","optimal":"Good phosphorus level for strong root and flower growth.","high":"Excess phosphorus can lock out micronutrients like zinc and iron."},
    "K":        {"low":"Low potassium weakens stems and reduces disease resistance.","watch":"Slightly low — keep watching fruit and stem quality.","optimal":"Solid potassium level for stem strength and fruit quality.","high":"Very high potassium can interfere with magnesium uptake."},
}


READING_GROUPS = [
    ("nutrients", "Nutrients (NPK)",  "What the plant eats",           ["N", "P", "K"]),
    ("condition", "Soil condition",   "How the soil itself is doing",  ["ph", "humidity", "temp", "organic_matter"]),
]
READING_ACTION = {
    "ph":       {"low":"Too acidic. Spread agricultural lime (or wood ash) and re-test in 2-4 weeks.",
                 "watch":"Close to the sweet spot. Re-test next week; no treatment yet.",
                 "optimal":"Nothing to do. Keep the current liming and fertilising routine.",
                 "high":"Too alkaline. Mix in compost or elemental sulfur and avoid more lime."},
    "humidity": {"low":"Water the plot now, preferably early morning, then re-scan in a few hours.",
                 "watch":"Plan irrigation today. Mulch to slow evaporation.",
                 "optimal":"Nothing to do. Do not water extra.",
                 "high":"Hold irrigation and check drainage. Open furrows if water is standing."},
    "temp":     {"low":"Cool soil slows roots. Use mulch or plastic cover and delay sowing warm-season crops.",
                 "watch":"Slightly cool. Fine for most crops; watch germination.",
                 "optimal":"Nothing to do. Good for root and shoot growth.",
                 "high":"Heat stress risk. Add shade or mulch and water in the early morning or evening."},
    "N":        {"low":"Add a nitrogen source (compost, manure or a nitrogen fertiliser) at the rate your adviser recommends.",
                 "watch":"A light top-dressing of compost or nitrogen fertiliser will help.",
                 "optimal":"Nothing to do. Do not add more nitrogen.",
                 "high":"Skip nitrogen fertiliser this cycle. Too much delays flowering and attracts pests."},
    "P":        {"low":"Add a phosphorus source (bone meal, compost or phosphate fertiliser) near the roots.",
                 "watch":"Slightly low. A small phosphorus feed at planting is enough.",
                 "optimal":"Nothing to do.",
                 "high":"Skip phosphorus inputs. Excess can lock out zinc and iron."},
    "K":        {"low":"Add potassium (wood ash, compost or potash) to strengthen stems and fruit.",
                 "watch":"Slightly low. A small potash or compost top-up is enough.",
                 "optimal":"Nothing to do.",
                 "high":"Skip potassium inputs. Excess can block magnesium uptake."},
    "organic_matter": {"low":"Add compost, manure or a cover crop to build organic matter.",
                       "watch":"Slightly outside target. Regular compost will keep it moving the right way.",
                       "optimal":"Nothing to do. Keep returning crop residues.",
                       "high":"Check drainage and avoid more heavy organic inputs for now."},
}
READING_WHY = {
    "ph":"pH controls how easily roots can take up nutrients. Most crops do best at 6.0-7.0.",
    "humidity":"Soil moisture is the water available to roots. Too little stresses plants, too much starves roots of air.",
    "temp":"Soil temperature drives germination, root growth and microbe activity.",
    "N":"Nitrogen builds leaves and green growth.",
    "P":"Phosphorus builds roots and flowers.",
    "K":"Potassium builds strong stems and improves fruit quality and disease resistance.",
    "organic_matter":"Organic matter holds water, feeds soil life and releases nutrients slowly.",
}
READING_SEVERITY = {"low": 3, "high": 3, "watch": 2, "optimal": 0}


def reading_status(key, value):
    spec = READING_RANGES[key]
    lo_ok, hi_ok = spec["optimal"]
    lo_w,  hi_w  = spec["watch"]
    if   lo_ok <= value <= hi_ok:           status = "optimal"
    elif lo_w  <= value < lo_ok or hi_ok < value <= hi_w: status = "watch"
    elif value < lo_w:                      status = "low"
    else:                                   status = "high"
    return status, READING_COPY[key][status]


def estimate_organic_matter(N, K):
    raw = ((N + K) / 2) / 24.0
    return round(max(0.0, min(10.0, raw)), 1)


def build_readings(rec):
    values = OrderedDict([
        ("ph",       rec["ph"]),
        ("humidity", rec["humidity"]),
        ("temp",     rec["temp"]),
        ("N",        rec["N"]),
        ("P",        rec["P"]),
        ("K",        rec["K"]),
    ])
    om  = estimate_organic_matter(rec["N"], rec["K"])
    out = []
    from_probe = (rec.get("source") == "esp32") if hasattr(rec, "get") else False
    for key, value in values.items():
        spec         = READING_RANGES[key]
        status, copy = reading_status(key, value)
        lo, hi       = spec["scale"]
        pct          = max(0.0, min(1.0, (value - lo) / (hi - lo))) * 100
        how = "Measured" if from_probe else "Entered"
        out.append({
            "key": key, "label": spec["label"], "unit": spec["unit"], "icon": spec["icon"],
            "value": round(value, 1), "status": status, "copy": copy,
            "scale_min": lo, "scale_max": hi,
            "optimal_min": spec["optimal"][0], "optimal_max": spec["optimal"][1],
            "watch_min": spec["watch"][0], "watch_max": spec["watch"][1],
            "percent": round(pct, 1),
            "how": how, "action": READING_ACTION[key][status], "why": READING_WHY[key],
            "gap": _gap_text(value, spec["optimal"][0], spec["optimal"][1], spec["unit"]),
        })
    om_status = "optimal" if 3 <= om <= 6 else ("watch" if 2 <= om < 3 or 6 < om <= 7 else ("low" if om < 2 else "high"))
    out.append({
        "key": "organic_matter", "label": "Organic Matter", "unit": "%", "icon": "\U0001F7EB",
        "value": om, "status": om_status,
        "copy": {"optimal":"Healthy organic-matter estimate — good structure and nutrient buffering.","watch":"Organic matter is a little outside the target band.","low":"Low organic matter — consider compost or cover cropping.","high":"Very high organic matter estimate — double-check drainage."}[om_status],
        "scale_min": 0, "scale_max": 10, "optimal_min": 3, "optimal_max": 6,
        "watch_min": 2, "watch_max": 7,
        "percent": round(max(0, min(10, om)) / 10 * 100, 1),
        "how": "Estimated", "action": READING_ACTION["organic_matter"][om_status],
        "why": READING_WHY["organic_matter"], "gap": _gap_text(om, 3, 6, "%"),
    })
    return out


def _gap_text(value, lo, hi, unit):
    """'1 pH below the optimal range' / 'Inside the optimal range'."""
    u = "%" if unit == "%" else f" {unit}"
    if value < lo: return f"{round(lo - value, 1):g}{u} below the optimal range"
    if value > hi: return f"{round(value - hi, 1):g}{u} above the optimal range"
    return "Inside the optimal range"


def group_readings(readings):
    """Readings arranged for the Sensors page: [(id, title, subtitle, [reading, ...]), ...]"""
    by_key = {r["key"]: r for r in readings}
    groups = []
    for gid, title, sub, keys in READING_GROUPS:
        items = [by_key[k] for k in keys if k in by_key]
        if items:
            groups.append({"id": gid, "title": title, "subtitle": sub, "rows": items})
    return groups


# On-device advisor flags, in the same bit order as TINYML_ADVICE[] in the Arduino header
TINY_ADVICE = ["Low nitrogen", "Low phosphorus", "Low potassium", "Acidic soil",
               "Alkaline soil", "Soil too dry", "Soil waterlogged", "Soil too hot"]


def tiny_extra_from_payload(data, main_crop, main_conf):
    """Validate the optional TinyML fields sent by the ESP32 and pack them as JSON text."""
    import json
    top = []
    for ck, pk in (("crop", "crop_conf"), ("crop2", "crop2_conf"), ("crop3", "crop3_conf")):
        name = str(data.get(ck) or "").strip().lower()
        if name in CROP_INFO:
            try:    conf = max(0.0, min(100.0, float(data.get(pk))))
            except (TypeError, ValueError): conf = None
            top.append([name, conf])
    has_probe = "odd" in data
    try:    mask = int(data.get("advice")) & 0xFF
    except (TypeError, ValueError): mask = None
    if not top and not has_probe and mask is None:
        return None
    try:    err = float(data.get("odd_err"))
    except (TypeError, ValueError): err = None
    return json.dumps({
        "top3": top,
        "odd": bool(int(data.get("odd") or 0)) if has_probe else None,
        "odd_err": err,
        "advice": [TINY_ADVICE[i] for i in range(8) if mask is not None and mask & (1 << i)] if mask is not None else None,
    })


def parse_tiny(rec):
    import json
    raw = rec.get("tiny_json")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    d["top3"] = [{"name": n, "label": n.capitalize(), "conf": (round(c) if c is not None else None),
                  "emoji": CROP_INFO.get(n, {}).get("emoji", "")} for n, c in d.get("top3", [])]
    return d


def web_tiny_json(N, P, K, temp, humidity, ph):
    """Run the TinyML networks on the website and pack the result like the ESP32 used to (JSON text)."""
    if tinyml_web is None:
        return None
    try:
        return json.dumps(tinyml_web.analyze(N, P, K, temp, humidity, ph))
    except Exception as e:
        print(f"[AgriSense] TinyML (web) failed: {e}")
        return None


def tiny_for_record(rec):
    """TinyML analysis for a record -> (analysis dict or None, where it was computed).
    where: 'device' (ESP32 sent it), 'web' (the website computed it when the scan arrived) or
    'web-live' (older / manual record: computed now, not stored)."""
    t = parse_tiny(rec)
    if t:
        return t, ("web" if rec.get("crop_source") == "tinyml-web" else "device")
    js = web_tiny_json(rec["N"], rec["P"], rec["K"], rec["temp"], rec["humidity"], rec["ph"])
    return (parse_tiny({"tiny_json": js}), "web-live") if js else (None, "")


def sensors_summary(rec, readings):
    """Headline numbers for the top of the Sensors page."""
    counts = {"optimal": 0, "watch": 0, "attention": 0}
    for r in readings:
        if r["status"] == "optimal":   counts["optimal"] += 1
        elif r["status"] == "watch":   counts["watch"] += 1
        else:                          counts["attention"] += 1
    todo = sorted((r for r in readings if r["status"] != "optimal"),
                  key=lambda r: (-READING_SEVERITY[r["status"]], r["label"]))
    src  = rec.get("source") or "manual"
    conf = rec.get("tinyml_conf")
    tiny, tiny_where = tiny_for_record(rec)
    return {
        "counts": counts, "total": len(readings), "todo": todo[:3],
        "when": (rec.get("created_at") or "")[:16],
        "source_label": "ESP32-S3 scanner" if src == "esp32" else "Manual entry",
        "was_offline": bool(rec.get("offline")),
        "crop": (rec.get("crop") or "").capitalize(),
        "crop_by": ("TinyML on the ESP32-S3" if rec.get("crop_source") == "tinyml"
                    else "TinyML on the website" if rec.get("crop_source") == "tinyml-web"
                    else "Website model (KNN)"),
        "crop_conf": round(conf) if conf is not None else None,
        "tiny": tiny,
        "tiny_where": tiny_where,
    }


def calculate_fertility(N, P, K, ph, temp, humidity):
    score = 0
    if 40 <= N <= 120:        score += 1
    if 40 <= P <= 120:        score += 1
    if 40 <= K <= 120:        score += 1
    if 5.5 <= ph <= 7.5:      score += 1
    if 20 <= temp <= 35:      score += 1
    if 50 <= humidity <= 90:  score += 1
    return round((score / 6) * 100, 2)


def grade_for_score(score):
    if score >= 85: return "A", "Excellent — soil is in great shape"
    if score >= 65: return "B", "Good — minor fixes needed"
    if score >= 45: return "C", "Fair — needs attention"
    return "D", "Poor — action needed soon"


def greeting_for_now():
    hour = datetime.now().hour
    if hour < 12: return "Good morning"
    if hour < 18: return "Good afternoon"
    return "Good evening"


def record_to_dict(rec):
    """Rows from RealDictCursor are already plain dicts."""
    d = dict(rec)
    if not d.get("fertility"):
        d["fertility"] = calculate_fertility(d["N"], d["P"], d["K"], d["ph"], d["temp"], d["humidity"])
    return d


def get_latest_record(username):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        'SELECT * FROM records WHERE username=%s ORDER BY id DESC LIMIT 1',
        (username,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return record_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# RULE-BASED AI ASSISTANT   (unchanged)
# ---------------------------------------------------------------------------
AI_TOPICS = [
    {"id":"ph",      "chip":"What is soil pH?",             "keywords":["ph","acid","alkalin"],
     "answer": lambda r: ("Soil pH measures how acidic or alkaline your soil is, on a 0-14 scale where 7 is neutral. Most crops prefer 6.0-7.0, where nutrients are easiest for roots to absorb.{context}").format(context=(f" Your latest reading is pH {r['ph']}, which is {reading_status('ph',r['ph'])[1].lower()}" if r else ""))},
    {"id":"npk",     "chip":"Explain NPK nutrients",        "keywords":["npk","nitrogen","phosphorus","potassium","nutrient"],
     "answer": lambda r: ("N-P-K stands for Nitrogen, Phosphorus, and Potassium — the three nutrients plants need most. Nitrogen drives leafy green growth, phosphorus builds roots and flowers, and potassium strengthens stems and improves disease resistance and fruit quality.{context}").format(context=(f" Right now you're at N {r['N']} mg/kg, P {r['P']} mg/kg, K {r['K']} mg/kg." if r else ""))},
    {"id":"moisture","chip":"How is moisture measured?",    "keywords":["moisture","water","humid","irrigat"],
     "answer": lambda r: ("Your sensor estimates soil moisture as a percentage of water content, roughly where 0% is bone dry and 100% is fully saturated. Most crops do best between 50-90%, sometimes called 'field capacity'.{context}").format(context=(f" Your latest reading is {r['humidity']}%, which is {reading_status('humidity',r['humidity'])[1].lower()}" if r else ""))},
    {"id":"organic", "chip":"What is organic matter?",      "keywords":["organic","humus","compost"],
     "answer": lambda r: ("Organic matter is the decomposed plant and animal material in soil — it holds water, feeds soil microbes, and slowly releases nutrients. A healthy field usually sits around 3-6%.{context}").format(context=(f" Your estimate right now is about {estimate_organic_matter(r['N'],r['K'])}%." if r else ""))},
    {"id":"score",   "chip":"How is my health score calculated?", "keywords":["health score","grade","fertility","score"],
     "answer": lambda r: ("Your soil health score checks all six readings — N, P, K, pH, temperature and moisture — against their optimal ranges. Each reading in range adds roughly 17 points, for a score out of 100. 85+ is Grade A, 65-84 is B, 45-64 is C, and below 45 is D.{context}").format(context=(f" You're currently at {r['fertility']}/100 (Grade {grade_for_score(r['fertility'])[0]})." if r else ""))},
    {"id":"crops",   "chip":"How does crop matching work?", "keywords":["crop","recommend","match","grow","plant"],
     "answer": lambda r: "The Crop Picks page compares your readings against the typical range for each of 22 crops in the training dataset, feature by feature. A crop scores higher the more of your readings fall inside its comfort zone — 85%+ is 'Excellent match', 60-84% is 'Good match', below that is 'Fair'."},
]
AI_FALLBACK = "I can walk you through pH, NPK nutrients, moisture, temperature, organic matter, your health score, how crop matching works, what TinyML is, why your scanner picked a crop, and how offline scanning works. Try one of those, or tap a suggestion above."


# ---- TinyML-aware topics (added) ------------------------------------------------
TINYML_LABELS = {"N": "Nitrogen", "P": "Phosphorus", "K": "Potassium",
                 "temperature": "Temperature", "humidity": "Moisture", "ph": "pH"}


def explain_crop(rec):
    """Which of the six probe readings fall inside the chosen crop's typical range."""
    crop  = (rec.get("crop") or "").lower() if rec else ""
    bands = _crop_bands.get(crop)
    if not bands:
        return None
    vals = {"N": rec["N"], "P": rec["P"], "K": rec["K"], "temperature": rec["temp"],
            "humidity": rec["humidity"], "ph": rec["ph"]}
    fits, off = [], []
    for feat, v in vals.items():
        lo, hi, _ = bands[feat]
        dec  = 1 if feat == "ph" else 0
        item = f"{TINYML_LABELS[feat]} {round(v, 1):g} (typical {lo:.{dec}f}-{hi:.{dec}f})"
        (fits if lo <= v <= hi else off).append(item)
    return crop.capitalize(), fits, off


def _tiny_of(r):
    return parse_tiny(r) if r else None


def _tinyml_context(r):
    if not r or not r.get("crop"):
        return ""
    by   = ("classified on the ESP32-S3" if r.get("crop_source") == "tinyml"
            else "classified by TinyML on the website" if r.get("crop_source") == "tinyml-web"
            else "suggested by the website model")
    conf = f" with {round(r['tinyml_conf'])}% confidence" if r.get("tinyml_conf") is not None else ""
    return f" Your latest scan was {by}: {r['crop'].capitalize()}{conf}."


def _why_crop(r):
    if not r or not r.get("crop"):
        return "Take a scan first (press Scan now on the device) and I can explain why the crop was picked."
    ex = explain_crop(r)
    if not ex:
        return "I can't find the usual range for that crop, so I can't break it down."
    name, fits, off = ex
    msg = f"The model picked {name}."
    if fits: msg += " These readings sit inside its typical range: " + "; ".join(fits) + "."
    if off:  msg += " These are outside it: " + "; ".join(off) + "."
    if not off: msg += " All six probe readings fit, so this is a strong match."
    msg += (" A neural network weighs all six readings together, so one reading outside the range does not rule a crop out, "
            "but several outside it lowers the confidence.")
    if r.get("tinyml_conf") is not None:
        msg += f" Confidence for this scan: {round(r['tinyml_conf'])}%."
    return msg


AI_TOPICS_TINYML = [
    {"id":"tinyknn", "chip":"TinyML vs the website model?", "keywords":["knn","website model","difference","compare","vs","versus"],
     "answer": lambda r: ("Two models work together. The TinyML network on the device picks a crop from the six probe readings, offline. "
        "The website's KNN model uses the same six inputs and also handles readings you type in manually. "
        "The Crop Picks page then ranks all 22 crops by how many of your readings sit in each crop's comfort zone.")},
    {"id":"whycrop", "chip":"Why did my scan pick this crop?", "keywords":["why this crop","why did","why was","why is","explain my crop","explain the crop","my crop"],
     "answer": lambda r: _why_crop(r)},
    {"id":"reliable","chip":"How reliable is the crop result?", "keywords":["reliable","confidence","accurate","accuracy","trust"],
     "answer": lambda r: ("The confidence number is how strongly the model favours its top crop. 85%+ is Excellent, 60-84% Good, below that Fair. "
        "One honest limit: the training data is a reference table of Philippine crops built from agronomic ranges, not measurements from your own plot, "
        "so treat the result as a guide to discuss with your adviser, not a guarantee." + _tinyml_context(r))},
    {"id":"offline", "chip":"What if there is no Wi-Fi?", "keywords":["offline","no wifi","no wi-fi","no internet","without wifi","without wi-fi","saved scan","sync","upload"],
     "answer": lambda r: ("If the scanner cannot find Wi-Fi it asks 'Continue offline?'. Say yes and Scan now still works: the reading and the TinyML crop are stored in the ESP32-S3's memory "
        "(up to 80 scans, kept even if you switch it off). As soon as Wi-Fi is back they upload by themselves, oldest first, and show up on this website. "
        "A scan is only deleted from the device after the website confirms it.")},
    {"id":"probecheck","chip":"What is the probe check?", "keywords":["probe check","odd value","unusual","anomaly","sensor fault","autoencoder","looks wrong","check probe"],
     "answer": lambda r: ("The probe check is a tiny autoencoder: it was trained only on real soil readings and learns what a normal combination of N, P, K, pH, temperature and moisture looks like. "
        "If a new scan cannot be rebuilt well, the scanner shows 'Odd value' because the probe may be in air, loosely inserted, or faulty. "
        "In testing it caught 100% of probe-in-air readings and about 98% of random garbage, with about 1% false alarms on normal rows. "
        "It is weaker at subtle faults (only about a quarter of readings stuck at pH 7 and 100% moisture), so re-scan if something looks off."
        + ((" Your latest scan was flagged as an odd value: re-insert the probe fully and scan again." if (_tiny_of(r) or {}).get("odd") else
            " Your latest scan passed the check.") if _tiny_of(r) and _tiny_of(r).get("odd") is not None else ""))},
    {"id":"advisor", "chip":"What does the TinyML advisor do?", "keywords":["advisor","what to fix","fertilizer","fertiliser","recommendation to fix"],
     "answer": lambda r: ("The advisor is a tiny network that reads the six probe values and names what to fix: low nitrogen, phosphorus or potassium, acidic or alkaline soil, soil too dry, waterlogged, or too hot. "
        "It was trained on the AgriSense threshold rules, so it agrees with them about 99% of the time. Its benefit is that it works offline on the scanner, not that it is smarter than the rules."
        + ((" For your latest scan it says: " + (", ".join(_tiny_of(r)["advice"]) if _tiny_of(r).get("advice") else "nothing to fix") + ".")
           if _tiny_of(r) and _tiny_of(r).get("advice") is not None else ""))},
    {"id":"tinyml",  "chip":"What is TinyML?", "keywords":["tinyml","tiny ml","on-device","microcontroller","edge ai","neural"],
     "answer": lambda r: ("TinyML means running small machine-learning models directly on a tiny chip instead of in the cloud. "
        "The ESP32-S3 in AgriSense holds three tiny neural networks (about 4 KB of weights in total): a crop recommender that ranks the top 3 crops, "
        "a probe check that flags readings that do not look like real soil, and a nutrient advisor that names what to fix. "
        "All three read the same six probe values (N, P, K, pH, soil temperature, soil moisture) and finish in milliseconds with no Wi-Fi and no server. "
        "On held-out data the crop model was right about 96% of the time for its first pick and 100% within its top 3. "
        "The same three networks also run inside the website itself (open the TinyML page in the menu), so every scan gets analysed even when the scanner's screen is not available."
        + _tinyml_context(r))},
    {"id":"temp",    "chip":None, "keywords":["temperature","temp","warm","hot","cold"],
     "answer": lambda r: ("Soil temperature drives germination, root growth and microbe activity. Most crops are happiest at 20-35 C." +
        (f" Your latest reading is {r['temp']} C, which is {reading_status('temp', r['temp'])[1].lower()}" if r else ""))},
]
AI_TOPICS = AI_TOPICS_TINYML + AI_TOPICS


def _kw_hit(text, kw):
    """Short keywords (ph, vs, n...) must match a whole word, so 'phosphorus' or 'graph' is not read as 'pH'."""
    if len(kw) <= 3:
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None
    return kw in text


def ai_answer(message, reading):
    text = (message or "").lower()
    for topic in AI_TOPICS:
        if any(_kw_hit(text, kw) for kw in topic["keywords"]):
            return topic["answer"](reading)
    return AI_FALLBACK


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------
def device_status_for(username):
    """Is a scanner connected to this account? Used by the top bar, the sidebar and the
    Overview. `connected` = we own a scanner AND it is checking in; `lost` = we own one
    but it went quiet (the 'connection error' state). Never raises: a database hiccup
    must not break a page."""
    status = {"connected": False, "lost": False, "count": 0, "devices": [], "last_scan": None}
    try:
        conn = get_db(); cur = conn.cursor()
        _release_stale(cur)
        cur.execute(
            "SELECT LEFT(token_hash,8) AS short_id, paired_at, "
            "EXTRACT(EPOCH FROM (NOW() - last_seen_at))::INT AS idle_s "
            "FROM devices WHERE owner=%s ORDER BY paired_at DESC", (username,))
        rows = cur.fetchall()
        cur.execute(
            "SELECT created_at FROM records WHERE username=%s AND source='esp32' "
            "ORDER BY id DESC LIMIT 1", (username,))
        last = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        devs = []
        for r in rows:
            online = r["idle_s"] is not None and r["idle_s"] <= DEVICE_ONLINE_SECS
            devs.append({"short_id": (r["short_id"] or "").upper(),
                         "paired_at": (r["paired_at"] or "")[:16], "online": online})
        status["devices"]   = devs
        status["count"]     = len(devs)
        status["connected"] = any(d["online"] for d in devs)
        status["lost"]      = bool(devs) and not status["connected"]
        status["last_scan"] = (last["created_at"] or "")[:16] if last else None
    except Exception as e:
        print(f"[AgriSense] device status unavailable: {e}")
    return status


@app.context_processor
def inject_globals():
    ctx = {
        "today":           datetime.now().strftime("%b %d, %Y"),
        "device_name":     DEVICE_NAME,
        "device_subtitle": DEVICE_SUBTITLE,
    }
    if "user" in session:
        ctx["device_status"] = device_status_for(session["user"])
    ctx["admin_msg"] = session.pop("admin_msg", None) if request.path.startswith("/admin") else None
    return ctx


def login_required(view):
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def _db_role(username):
    """Current role straight from the database (never trust a stale cookie)."""
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE username=%s", (username,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return (row["role"] or "").strip().lower() if row else None
    except Exception as e:
        print(f"[AgriSense] role lookup failed: {e}")
        return None


def admin_required(view):
    def wrapped(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        role = _db_role(session["user"])
        if role != "admin":
            # demoted / deleted accounts lose admin access immediately
            session["role"] = role or "user"
            return redirect(url_for("overview" if role else "login"))
        session["role"] = "admin"
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


@app.route("/", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        user = request.form.get("username","").strip()
        pw   = request.form.get("password","")
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username=%s AND password=%s", (user, pw))
        result = cur.fetchone()
        cur.close()
        conn.close()
        if result:
            session["user"] = user
            session["role"] = result["role"]
            nxt = session.pop("next_url", None)          # e.g. came from a device QR code
            if nxt and nxt.startswith("/pair"):
                return redirect(nxt)
            if result["role"].strip().lower() == "admin":
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("overview"))
        error = "Incorrect username or password. Please try again."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET","POST"])
def register():
    error = None
    if request.method == "POST":
        user = request.form.get("username","").strip()
        pw   = request.form.get("password","")
        # Role is ALWAYS 'user' on public registration. The form has no role
        # field, and even if someone posts one by hand (curl, dev tools) it
        # is ignored. Admins are created only in the database / env seed or
        # by an existing admin in the Admin Panel.
        role = "user"
        if not user or not pw:
            error = "Username and password are required."
        elif user.lower() in RESERVED_USERNAMES:
            error = "That username is not available."
        else:
            conn = get_db()
            cur  = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO users (username, password, role) VALUES (%s,%s,%s)",
                    (user, pw, role)
                )
                conn.commit()
                cur.close()
                conn.close()
                return redirect(url_for("login"))
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                error = "That username is already taken."
                cur.close()
                conn.close()
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# CORE PAGES   (routes unchanged — only DB calls updated)
# ---------------------------------------------------------------------------
@app.route("/home")
@login_required
def home():
    return redirect(url_for("overview"))


@app.route("/overview")
@login_required
def overview():
    rec           = get_latest_record(session["user"])
    readings      = build_readings(rec) if rec else []
    optimal_count = sum(1 for r in readings if r["status"] == "optimal")
    attention_count = len(readings) - optimal_count
    grade, grade_copy = grade_for_score(rec["fertility"]) if rec else ("-","Add a reading to get started")
    headline   = [r for r in readings if r["key"] in ("ph","humidity","temp")]
    npk_readings = [r for r in readings if r["key"] in ("N","P","K")]
    worst_npk  = None
    if npk_readings:
        order    = {"low":0,"high":0,"watch":1,"optimal":2}
        worst_npk = sorted(npk_readings, key=lambda r: order[r["status"]])[0]
    glance = headline + ([worst_npk] if worst_npk else [])
    issues  = sorted((r for r in readings if r["status"] != "optimal"),
                     key=lambda r: (-READING_SEVERITY[r["status"]], r["label"]))
    optimal = [r for r in readings if r["status"] == "optimal"]
    return render_template("overview.html",
        user=session["user"], greeting=greeting_for_now(),
        record=rec, readings=readings, npk_readings=npk_readings,
        glance=glance, optimal_count=optimal_count, attention_count=attention_count,
        issues=issues, optimal=optimal,
        grade=grade, grade_copy=grade_copy,
        last_scan=(rec.get("created_at") or "")[:16] if rec else "",
        today=datetime.now().strftime("%b %d, %Y"))


@app.route("/sensors")
@login_required
def sensors():
    rec      = get_latest_record(session["user"])
    readings = build_readings(rec) if rec else []
    grade, grade_copy = grade_for_score(rec["fertility"]) if rec else ("-","")
    return render_template("sensors.html",
        user=session["user"], record=rec, readings=readings, grade=grade, grade_copy=grade_copy,
        groups=group_readings(readings) if rec else [],
        summary=sensors_summary(rec, readings) if rec else None,
        device_name=DEVICE_NAME, device_subtitle=DEVICE_SUBTITLE)


@app.route("/crops")
@login_required
def crops():
    rec          = get_latest_record(session["user"])
    category     = request.args.get("category","All Crops")
    sort_by      = request.args.get("sort","match")
    suitable_only = request.args.get("suitable","1") != "0"
    matches      = crop_matches(rec["N"],rec["P"],rec["K"],rec["temp"],rec["humidity"],rec["ph"],
                                category=category,suitable_only=suitable_only) if rec else []
    if sort_by == "name":
        matches = sorted(matches, key=lambda r: r["label"])
    elif sort_by == "harvest":
        def harvest_key(r):
            digits = re.findall(r"\d+", r["harvest"])
            return int(digits[0]) if digits else 9999
        matches = sorted(matches, key=harvest_key)
    excellent  = sum(1 for m in matches if m["tier"] == "Excellent")
    good       = sum(1 for m in matches if m["tier"] == "Good")
    top_emojis = [m["emoji"] for m in matches[:3]]
    grade, _   = grade_for_score(rec["fertility"]) if rec else ("-","")
    return render_template("crops.html",
        user=session["user"], record=rec, matches=matches,
        categories=["All Crops"]+CROP_CATEGORIES, active_category=category,
        sort_by=sort_by, suitable_only=suitable_only,
        excellent=excellent, good=good, top_emojis=top_emojis,
        grade=grade, device_name=DEVICE_NAME, device_subtitle=DEVICE_SUBTITLE)


@app.route("/tinyml")
@login_required
def tinyml_page():
    rec    = get_latest_record(session["user"])
    grade, _ = grade_for_score(rec["fertility"]) if rec else ("-", "")
    sample = ({"N": rec["N"], "P": rec["P"], "K": rec["K"], "temperature": rec["temp"],
               "humidity": rec["humidity"], "ph": rec["ph"]} if rec else None)
    emojis = {k: v["emoji"] for k, v in CROP_INFO.items()}
    return render_template("tinyml.html",
        user=session["user"], record=rec, sample=sample, emojis=emojis, grade=grade,
        enabled=tinyml_web is not None,
        metrics=(tinyml_web.METRICS if tinyml_web else {}),
        n_params=(tinyml_web.N_PARAMS if tinyml_web else 0),
        kb=(tinyml_web.KB if tinyml_web else 0),
        device_name=DEVICE_NAME, device_subtitle=DEVICE_SUBTITLE)


@app.route("/ai-assistant")
@login_required
def ai_assistant():
    rec    = get_latest_record(session["user"])
    grade, _ = grade_for_score(rec["fertility"]) if rec else ("-","")
    return render_template("ai_assistant.html",
        user=session["user"], record=rec, topics=AI_TOPICS,
        grade=grade, device_name=DEVICE_NAME, device_subtitle=DEVICE_SUBTITLE)


@app.route("/api/ai/ask", methods=["POST"])
@login_required
def api_ai_ask():
    payload = request.get_json(silent=True) or {}
    message = payload.get("message","")
    rec     = get_latest_record(session["user"])
    return jsonify({"reply": ai_answer(message, rec)})


@app.route("/learning")
@login_required
def learning():
    rec    = get_latest_record(session["user"])
    grade, _ = grade_for_score(rec["fertility"]) if rec else ("-","")
    return render_template("learning.html",
        user=session["user"], grade=grade,
        device_name=DEVICE_NAME, device_subtitle=DEVICE_SUBTITLE)


# ---------------------------------------------------------------------------
# READING SUBMISSION + HISTORY
# ---------------------------------------------------------------------------
ALLOWED_IMAGE_EXT = {".png",".jpg",".jpeg",".gif",".webp"}


def _safe_image_save(image_file):
    ext = os.path.splitext(image_file.filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        return None
    safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{ext}"
    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        image_file.save(os.path.join(UPLOAD_FOLDER, safe_name))
    except OSError as e:
        print(f"[AgriSense] Image not saved: {e}")
    return safe_name


@app.route("/dashboard", methods=["GET","POST"])
@login_required
def dashboard():
    result    = None
    fertility = None
    if request.method == "POST":
        try:
            N        = float(request.form["N"])
            P        = float(request.form["P"])
            K        = float(request.form["K"])
            ph       = float(request.form["ph"])
            temp     = float(request.form["temp"])
            humidity = float(request.form["humidity"])
        except (KeyError, ValueError):
            records = _records_for(session["user"], session["role"])
            return render_template("dashboard.html", result=None, fertility=None,
                error="Please enter valid numbers for every field.",
                records=records, user=session["user"], **_chart_context(records))

        fertility = calculate_fertility(N, P, K, ph, temp, humidity)
        result    = recommend_crop(N, P, K, temp, humidity, ph)

        filename   = None
        image_file = request.files.get("soil_image")
        if image_file and image_file.filename:
            filename = _safe_image_save(image_file)

        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO records
              (username,"N","P","K",ph,temp,humidity,crop,image,source,fertility,created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'manual',%s,NOW()::TEXT)
        """, (session["user"], N, P, K, ph, temp, humidity, result["name"], filename, fertility))
        conn.commit()
        cur.close()
        conn.close()

    records = _records_for(session["user"], session["role"])
    return render_template("dashboard.html",
        result=result, fertility=fertility, error=None,
        records=records, user=session["user"], **_chart_context(records))


def _records_for(username, role):
    conn = get_db()
    cur  = conn.cursor()
    if role.strip().lower() == "admin":
        cur.execute("SELECT * FROM records ORDER BY id DESC")
    else:
        cur.execute("SELECT * FROM records WHERE username=%s ORDER BY id DESC", (username,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.route("/api/records/delete", methods=["POST"])
@login_required
def api_records_delete():
    """Bulk-delete readings from the history tables (Analysis History and Admin > All Records).

    JSON body:
      {"mode": "ids", "ids": [12, 15, ...]}   delete exactly these rows (selected / filtered rows)
      {"mode": "all"}                         clear every reading
    A student can only ever delete their OWN readings (enforced here, not just in the UI).
    An admin's "all" clears everyone's readings; their "ids" can be anyone's.
    JSON-only on purpose: a plain cross-site <form> cannot send it.
    """
    if not request.is_json:
        return jsonify({"error": "expected JSON"}), 415
    p     = request.get_json(silent=True) or {}
    mode  = p.get("mode")
    user  = session["user"]
    admin = (_db_role(user) == "admin")

    conn = get_db(); cur = conn.cursor()
    try:
        if mode == "all":
            if admin:
                cur.execute("DELETE FROM records")
            else:
                cur.execute("DELETE FROM records WHERE username=%s", (user,))
        elif mode == "ids":
            ids = []
            for v in (p.get("ids") or [])[:20000]:
                try:    ids.append(int(v))
                except (TypeError, ValueError): pass
            ids = sorted(set(ids))
            if not ids:
                return jsonify({"deleted": 0})
            if admin:
                cur.execute("DELETE FROM records WHERE id = ANY(%s)", (ids,))
            else:
                cur.execute("DELETE FROM records WHERE id = ANY(%s) AND username=%s", (ids, user))
        else:
            return jsonify({"error": "mode must be 'ids' or 'all'"}), 400
        deleted = cur.rowcount
        conn.commit()
    finally:
        cur.close(); conn.close()
    return jsonify({"deleted": deleted})


def _chart_context(records):
    crop_counts = Counter([r["crop"] for r in records if r["crop"]])
    return {"labels": list(crop_counts.keys()), "values": list(crop_counts.values())}


# ---------------------------------------------------------------------------
# ESP32-S3 HARDWARE API
# ---------------------------------------------------------------------------
ESP32_API_KEY = os.environ.get("ESP32_API_KEY","")


# --- device helpers --------------------------------------------------------
# HOW DISCOVERY WORKS
# A browser cannot scan a Wi-Fi network, and the site runs on Vercel, not on the
# school LAN. So the scanner does the "announcing": every few seconds it posts a
# heartbeat to /api/device/heartbeat. "Find device" on the website simply lists the
# scanners whose last heartbeat is recent (= online right now). A user claims a FREE
# one with a single atomic UPDATE, so two people can never both get it.
DEVICE_ONLINE_SECS   = 25      # no heartbeat for this long -> the scanner counts as offline
DEVICE_RELEASE_SECS  = 600     # an owned scanner offline this long is released automatically
DEVICE_SCAN_TTL_SECS = 30      # a "Scan now" pressed on the website is dropped if the scanner has not taken it by then
DEVICE_NOTICE_SECS   = 1800    # how long a disconnected user keeps seeing the "you were disconnected" notice


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _device_token():
    """Raw secret from the X-Device-Token header, or None if missing/odd."""
    tok = request.headers.get("X-Device-Token", "").strip()
    return tok if 16 <= len(tok) <= 128 else None


def _api_key_ok():
    return not ESP32_API_KEY or request.headers.get("X-API-Key") == ESP32_API_KEY


def _release_stale(cur):
    """A scanner that has been silent for a long time cannot be 'in use': free it so a
    ghost connection never blocks the next student. The previous owner is told why."""
    cur.execute(
        "UPDATE devices SET released_user=owner, released_reason='offline', released_at=NOW(), "
        "owner=NULL, paired_at=NULL, scan_requested_at=NULL "
        "WHERE owner IS NOT NULL AND "
        f"(last_seen_at IS NULL OR last_seen_at < NOW() - INTERVAL '{int(DEVICE_RELEASE_SECS)} seconds')")


def _device_label(short_id, name=None):
    """Same text the scanner prints in the corner of its screen ("ID A1B2")."""
    return f"AgriSense Scanner {(name or short_id[:4]).upper()}"


def _device_view(row, viewer, is_admin):
    """One scanner as the website needs to show it, from `viewer`'s point of view.
    state: available | mine | busy | lost | offline"""
    idle   = row["idle_s"]
    online = idle is not None and idle <= DEVICE_ONLINE_SECS
    owner  = row["owner"]
    if owner and owner == viewer:
        state = "mine" if online else "lost"
    elif owner:
        state = "busy" if online else "offline"
    else:
        state = "available" if online else "offline"
    sid = (row["id"] or "").upper()
    out = {
        "id": row["id"], "short_id": sid, "label": _device_label(sid, row.get("dev_name")),
        "state": state, "online": online,
        "idle_s": idle, "rssi": row.get("rssi"), "sensor": row.get("sensor_state") or "unknown",
        "in_use": bool(owner), "mine": bool(owner and owner == viewer),
    }
    if owner and owner == viewer:      # a Scan now request still waiting for the scanner to pick it up
        out["scan_pending"] = bool(row.get("scan_pending"))
    if is_admin:                       # only admins ever see who is using a scanner
        out["owner"] = owner
    return out


def _list_devices_for(cur, viewer, is_admin):
    _release_stale(cur)
    cur.execute(
        "SELECT LEFT(token_hash,8) AS id, owner, rssi, sensor_state, dev_name, "
        f"(scan_requested_at > NOW() - INTERVAL '{int(DEVICE_SCAN_TTL_SECS)} seconds') AS scan_pending, "
        "EXTRACT(EPOCH FROM (NOW() - last_seen_at))::INT AS idle_s "
        "FROM devices WHERE last_seen_at IS NOT NULL ORDER BY last_seen_at DESC LIMIT 50")
    views = [_device_view(r, viewer, is_admin) for r in cur.fetchall()]
    if is_admin:
        return views
    # students: scanners that are online right now, plus their own (even if it went quiet)
    return [v for v in views if v["online"] or v["mine"]]


def _my_notice(cur, username):
    cur.execute(
        "SELECT released_reason, "
        f"(released_at > NOW() - INTERVAL '{int(DEVICE_NOTICE_SECS)} seconds') AS fresh "
        "FROM devices WHERE released_user=%s AND (owner IS NULL OR owner<>%s) "
        "ORDER BY released_at DESC LIMIT 1", (username, username))
    row = cur.fetchone()
    if not row or not row["fresh"]:
        return None
    if row["released_reason"] == "admin":
        return {"kind": "admin", "text": "An admin disconnected you from the scanner. You can connect again if it is free."}
    if row["released_reason"] == "offline":
        return {"kind": "offline", "text": "The scanner went offline for a while, so it was released. Connect again once it is back."}
    return None


@app.route("/api/device/heartbeat", methods=["POST"])
def api_device_heartbeat():
    """The scanner calls this every few seconds. It makes the scanner discoverable and
    tells the LCD whether somebody is connected to it. The reply also carries "scan": true
    when the connected user pressed Scan now on the website. Body (all optional):
    {"rssi": -60, "sensor": "ok|fail|unknown", "fw": "2.0"}"""
    if not _api_key_ok():
        return jsonify({"error": "unauthorized"}), 401
    token = _device_token()
    if not token:
        return jsonify({"error": "missing X-Device-Token"}), 400
    data = request.get_json(silent=True) or {}
    try:
        rssi = int(data.get("rssi")) if data.get("rssi") is not None else None
    except (TypeError, ValueError):
        rssi = None
    sensor = str(data.get("sensor") or "unknown").lower()
    if sensor not in ("ok", "fail", "unknown"):
        sensor = "unknown"
    fw = str(data.get("fw") or "")[:16] or None
    name = re.sub(r"[^A-Za-z0-9]", "", str(data.get("name") or ""))[:8] or None
    th = _hash_token(token)

    conn = get_db(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO devices (token_hash, created_at) VALUES (%s, NOW()::TEXT) ON CONFLICT DO NOTHING", (th,))
    cur.execute(
        "UPDATE devices SET last_seen=NOW()::TEXT, last_seen_at=NOW(), rssi=%s, "
        "sensor_state=%s, fw=COALESCE(%s, fw), dev_name=COALESCE(%s, dev_name) "
        "WHERE token_hash=%s RETURNING owner, (scan_requested_at IS NOT NULL) AS has_req, "
        f"(scan_requested_at > NOW() - INTERVAL '{int(DEVICE_SCAN_TTL_SECS)} seconds') AS scan_due", (rssi, sensor, fw, name, th))
    row = cur.fetchone()
    # "Scan now" pressed on the website: hand it to the scanner exactly once, then clear it
    scan = bool(row and row["owner"] and row["scan_due"])
    if row and row["has_req"]:
        cur.execute("UPDATE devices SET scan_requested_at=NULL WHERE token_hash=%s", (th,))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"status": "ok", "connected": bool(row and row["owner"]), "scan": scan}), 200


@app.route("/api/devices")
@login_required
def api_devices():
    """Live list for the 'Find device' panel (polled every few seconds by device.js)."""
    viewer   = session["user"]
    is_admin = _db_role(viewer) == "admin"
    conn = get_db(); cur = conn.cursor()
    try:
        devices = _list_devices_for(cur, viewer, is_admin)
        notice  = _my_notice(cur, viewer)
        conn.commit()
    finally:
        cur.close(); conn.close()
    resp = jsonify({"devices": devices, "notice": notice, "is_admin": is_admin,
                    "online_secs": DEVICE_ONLINE_SECS})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/devices/<dev_id>/connect", methods=["POST"])
@login_required
def api_device_connect(dev_id):
    """Claim a FREE, ONLINE scanner. One atomic UPDATE: if two people press Connect at
    the same moment, exactly one row update succeeds and the other gets 'busy'."""
    dev_id = re.sub(r"[^0-9a-fA-F]", "", dev_id).lower()[:8]
    user   = session["user"]
    conn = get_db(); cur = conn.cursor()
    try:
        _release_stale(cur)
        cur.execute(
            "UPDATE devices SET owner=%s, paired_at=NOW()::TEXT, connected_at=NOW(), scan_requested_at=NULL, "
            "released_user=NULL, released_reason=NULL, released_at=NULL "
            "WHERE LEFT(token_hash,8)=%s AND owner IS NULL "
            f"AND last_seen_at > NOW() - INTERVAL '{int(DEVICE_ONLINE_SECS)} seconds' "
            "RETURNING token_hash", (user, dev_id))
        won = cur.fetchone()
        if won:
            conn.commit()
            return jsonify({"ok": True})
        # Why not? Give the user an honest, specific answer.
        cur.execute(
            "SELECT owner, EXTRACT(EPOCH FROM (NOW() - last_seen_at))::INT AS idle_s "
            "FROM devices WHERE LEFT(token_hash,8)=%s", (dev_id,))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not row:
        return jsonify({"ok": False, "error": "not_found",
                        "message": "That scanner is no longer listed. Press Find device again."}), 404
    if row["owner"] == user:
        return jsonify({"ok": True, "already": True})
    if row["owner"]:
        return jsonify({"ok": False, "error": "busy",
                        "message": "Someone else is connected to this scanner. Ask them to disconnect, or ask an admin."}), 409
    return jsonify({"ok": False, "error": "offline",
                    "message": "The scanner stopped responding. Check it is powered on and on Wi-Fi, then try again."}), 409


@app.route("/api/devices/<dev_id>/disconnect", methods=["POST"])
@login_required
def api_device_disconnect(dev_id):
    """Owners release their own scanner. Admins may release ANY scanner (the 'someone is
    hogging it' case); the user who was disconnected is told why."""
    dev_id   = re.sub(r"[^0-9a-fA-F]", "", dev_id).lower()[:8]
    user     = session["user"]
    is_admin = _db_role(user) == "admin"
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT owner FROM devices WHERE LEFT(token_hash,8)=%s", (dev_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found", "message": "Scanner not found."}), 404
        owner = row["owner"]
        if not owner:
            return jsonify({"ok": True, "already": True})
        if owner == user:
            cur.execute(
                "UPDATE devices SET owner=NULL, paired_at=NULL, released_user=NULL, released_reason=NULL, scan_requested_at=NULL "
                "WHERE LEFT(token_hash,8)=%s AND owner=%s", (dev_id, user))
            conn.commit()
            return jsonify({"ok": True})
        if not is_admin:
            return jsonify({"ok": False, "error": "forbidden",
                            "message": "Only the person using this scanner, or an admin, can disconnect it."}), 403
        cur.execute(
            "UPDATE devices SET released_user=owner, released_reason='admin', released_at=NOW(), "
            "owner=NULL, paired_at=NULL, scan_requested_at=NULL WHERE LEFT(token_hash,8)=%s AND owner IS NOT NULL RETURNING released_user",
            (dev_id,))
        gone = cur.fetchone()
        conn.commit()
        return jsonify({"ok": True, "disconnected_user": gone["released_user"] if gone else owner})
    finally:
        cur.close(); conn.close()


@app.route("/api/devices/<dev_id>/scan", methods=["POST"])
@login_required
def api_device_scan(dev_id):
    """'Scan now' on the website. Only the person connected to the scanner can press it.
    It just leaves a note for the scanner; the scanner finds it on its next check-in
    (every ~3 s while connected), reads the probe and sends the reading to /api/esp32 like
    a tap on its screen. `baseline` = id of this user's newest scanner reading right now,
    so the page can tell when the NEW one has arrived (GET /api/latest-scan?since=baseline)."""
    dev_id = re.sub(r"[^0-9a-fA-F]", "", dev_id).lower()[:8]
    user   = session["user"]
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("SELECT COALESCE(MAX(id),0) AS m FROM records WHERE username=%s AND source='esp32'", (user,))
        baseline = cur.fetchone()["m"]
        cur.execute(
            "UPDATE devices SET scan_requested_at=NOW() "
            "WHERE LEFT(token_hash,8)=%s AND owner=%s "
            f"AND last_seen_at > NOW() - INTERVAL '{int(DEVICE_ONLINE_SECS)} seconds' "
            f"AND (scan_requested_at IS NULL OR scan_requested_at < NOW() - INTERVAL '{int(DEVICE_SCAN_TTL_SECS)} seconds') "
            "RETURNING token_hash", (dev_id, user))
        if cur.fetchone():
            conn.commit()
            return jsonify({"ok": True, "baseline": baseline})
        cur.execute(
            "SELECT owner, EXTRACT(EPOCH FROM (NOW() - last_seen_at))::INT AS idle_s, "
            f"(scan_requested_at > NOW() - INTERVAL '{int(DEVICE_SCAN_TTL_SECS)} seconds') AS pending "
            "FROM devices WHERE LEFT(token_hash,8)=%s", (dev_id,))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close(); conn.close()
    if not row:
        return jsonify({"ok": False, "error": "not_found", "message": "Scanner not found. Press Find device again."}), 404
    if row["owner"] != user:
        return jsonify({"ok": False, "error": "not_connected",
                        "message": "Connect to this scanner first. Only the person connected to it can scan."}), 403
    if row["idle_s"] is None or row["idle_s"] > DEVICE_ONLINE_SECS:
        return jsonify({"ok": False, "error": "offline",
                        "message": "The scanner is not responding. Check it is powered on and on Wi-Fi."}), 409
    if row["pending"]:        # double click: the first request is still on its way
        return jsonify({"ok": True, "already": True, "baseline": baseline})
    return jsonify({"ok": False, "error": "busy", "message": "Could not start the scan. Please try again."}), 409


@app.route("/api/devices/dismiss_notice", methods=["POST"])
@login_required
def api_device_dismiss_notice():
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE devices SET released_user=NULL, released_reason=NULL WHERE released_user=%s",
                (session["user"],))
    conn.commit(); cur.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/esp32", methods=["POST"])
def api_esp32():
    if not _api_key_ok():
        return jsonify({"error":"unauthorized"}), 401
    token = _device_token()
    if not token:
        return jsonify({"error":"missing X-Device-Token"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error":"expected JSON body"}), 400
    missing = [f for f in ["N","P","K","ph","temp","humidity"] if f not in data]
    if missing:
        return jsonify({"error":f"missing fields: {', '.join(missing)}"}), 400
    try:
        N        = float(data["N"]);      P        = float(data["P"])
        K        = float(data["K"]);      ph       = float(data["ph"])
        temp     = float(data["temp"]);   humidity = float(data["humidity"])
    except (TypeError, ValueError):
        return jsonify({"error":"all sensor fields must be numeric"}), 400

    # --- optional fields (TinyML result + offline-sync info) -------------------
    # Older firmware does not send them: everything below then behaves as before.
    tiny_crop = str(data.get("crop") or "").strip().lower()
    if tiny_crop not in CROP_INFO:
        tiny_crop = ""                       # unknown / empty -> use the website model instead
    try:
        tiny_conf = max(0.0, min(100.0, float(data.get("crop_conf"))))
    except (TypeError, ValueError):
        tiny_conf = None
    tiny_json = tiny_extra_from_payload(data, tiny_crop, tiny_conf)
    crop_source = "tinyml" if tiny_crop else "knn"
    if not tiny_crop and not tiny_json:
        # The scanner did not run TinyML (its screen / on-device models are not in use):
        # the website runs the very same networks on the six probe readings instead.
        wj = web_tiny_json(N, P, K, temp, humidity, ph)
        if wj:
            top = json.loads(wj)["top3"][0]
            tiny_crop, tiny_conf, tiny_json, crop_source = top[0], top[1], wj, "tinyml-web"
    offline  = bool(data.get("offline"))
    try:
        age_s = max(0.0, min(30 * 86400.0, float(data.get("age_s"))))
    except (TypeError, ValueError):
        age_s = 0.0
    scan_uid = str(data.get("scan_uid") or "").strip()[:64] or None

    th   = _hash_token(token)
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT owner FROM devices WHERE token_hash=%s", (th,))
    dev = cur.fetchone()
    if not dev or not dev["owner"]:
        cur.close(); conn.close()
        # LCD shows "connect on the website first"
        return jsonify({"error":"device_not_linked"}), 403
    username = dev["owner"]

    # A scan the ESP32 re-sends (e.g. the reply was lost) is acknowledged, never saved twice
    if scan_uid:
        cur.execute("SELECT id FROM records WHERE username=%s AND scan_uid=%s", (username, scan_uid))
        if cur.fetchone():
            cur.execute("UPDATE devices SET last_seen=NOW()::TEXT, last_seen_at=NOW() WHERE token_hash=%s", (th,))
            conn.commit(); cur.close(); conn.close()
            return jsonify({"status":"ok","duplicate":True}), 200

    fertility = calculate_fertility(N, P, K, ph, temp, humidity)
    knn       = recommend_crop(N, P, K, temp, humidity, ph)
    crop_name   = tiny_crop or knn["name"]

    # Scans taken offline keep the time they were scanned (not the time they were uploaded)
    cur.execute("""
        INSERT INTO records
          (username,"N","P","K",ph,temp,humidity,crop,image,source,fertility,created_at,
           crop_source,tinyml_conf,offline,scan_uid,tiny_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,'esp32',%s,
                (NOW() - make_interval(secs => %s))::TEXT,
                %s,%s,%s,%s,%s)
    """, (username, N, P, K, ph, temp, humidity, crop_name, fertility, age_s,
          crop_source, tiny_conf, 1 if offline else 0, scan_uid, tiny_json))
    cur.execute("UPDATE devices SET last_seen=NOW()::TEXT, last_seen_at=NOW() WHERE token_hash=%s", (th,))
    conn.commit()
    cur.close()
    conn.close()

    out = {
        "status":"ok","user":username,
        "crop":crop_name,"crop_source":crop_source,"crop_conf":tiny_conf,
        "description":CROP_INFO.get(crop_name, {}).get("desc", knn["desc"]),
        "fertility_score":fertility,"grade":grade_for_score(fertility)[0],"grade_text":grade_for_score(fertility)[1],
        "inputs":{"N":N,"P":P,"K":K,"ph":ph,"temp":temp,"humidity":humidity},
    }
    # Flat "lvl_*" keys (optimal / watch / low / high) = the colours the website popup shows.
    # The scanner's LCD reads them: it has no way to judge the numbers itself.
    for key, val in (("N", N), ("P", P), ("K", K), ("ph", ph), ("temp", temp), ("humidity", humidity)):
        out["lvl_" + key] = reading_status(key, val)[0]
    return jsonify(out), 200


# --- website side of pairing (user is logged in) -----------------------------
# ---------------------------------------------------------------------------
# LIVE "NEW SCAN" POPUP  (polled by static/js/scan-popup.js)
#
# Vercel cannot hold a connection open, so the browser asks every few seconds
# "is there a scan newer than id <since>?".  The common answer ("no") is a
# tiny query + tiny reply; the full scan is only built when something is new.
#   no ?since      -> {"latest_id": N}                     (page just opened)
#   since >= N     -> {"latest_id": N, "new": false}
#   since <  N     -> {"latest_id": N, "new": true, "new_count": k, "scan": {...}}
# Only the logged-in user's OWN scans from the ESP32 are ever returned.
# ---------------------------------------------------------------------------
SCAN_POPUP_ORDER = ["N", "P", "K", "ph", "temp", "humidity"]     # what the probe really measures


@app.route("/api/latest-scan")
def api_latest_scan():
    if "user" not in session:
        return jsonify({"error": "login required"}), 401
    username = session["user"]
    since    = request.args.get("since", type=int)

    conn = get_db()
    cur  = conn.cursor()
    try:
        cur.execute("SELECT * FROM records WHERE username=%s AND source='esp32' "
                    "ORDER BY id DESC LIMIT 1", (username,))
        row = cur.fetchone()
        if not row:
            resp = jsonify({"latest_id": 0, "new": False})
        elif since is None:
            resp = jsonify({"latest_id": row["id"]})
        elif row["id"] <= since:
            resp = jsonify({"latest_id": row["id"], "new": False})
        else:
            cur.execute("SELECT COUNT(*) AS n FROM records WHERE username=%s AND source='esp32' "
                        "AND id > %s", (username, since))
            new_count = cur.fetchone()["n"]
            rec       = record_to_dict(row)
            grade, grade_text = grade_for_score(rec["fertility"])
            readings  = [r for r in build_readings(rec) if r["key"] in SCAN_POPUP_ORDER]
            readings.sort(key=lambda r: SCAN_POPUP_ORDER.index(r["key"]))
            resp = jsonify({
                "latest_id": row["id"], "new": True, "new_count": new_count,
                "scan": {
                    "id":         row["id"],
                    "created_at": str(rec.get("created_at") or ""),
                    "crop":       rec.get("crop") or "",
                    "crop_source": rec.get("crop_source") or "",
                    "crop_conf":  rec.get("tinyml_conf"),
                    "offline":    bool(rec.get("offline")),
                    "fertility":  round(float(rec["fertility"]), 1),
                    "grade":      grade,
                    "grade_text": grade_text,
                    "readings":   [{"key": r["key"], "label": r["label"], "unit": r["unit"],
                                    "value": r["value"], "status": r["status"]} for r in readings],
                },
            })
    finally:
        cur.close()
        conn.close()
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/pair")
def pair():
    """The 'Device' page: Find device -> Connect / Disconnect. The list itself is live
    (static/js/device.js polls /api/devices); this route only renders the shell."""
    if "user" not in session:
        session["next_url"] = request.full_path.rstrip("?")   # come back after login
        return redirect(url_for("login"))
    return render_template("pair.html", user=session["user"])


# ---------------------------------------------------------------------------
# ADMIN
# ---------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT username, role FROM users ORDER BY username")
    users = cur.fetchall()
    cur.execute("SELECT * FROM records ORDER BY id DESC")
    records = cur.fetchall()
    devices = _list_devices_for(cur, session["user"], True)
    conn.commit()
    cur.close()
    conn.close()

    crop_counts  = Counter([r["crop"] for r in records if r["crop"]])
    user_counts  = Counter([r["username"] for r in records])
    scores       = [record_to_dict(r)["fertility"] for r in records]
    avg_health   = round(sum(scores)/len(scores),1) if scores else 0
    admin_count   = sum(1 for u in users if (u["role"] or "").strip().lower() == "admin")

    return render_template("admin_dashboard.html",
        users=users, records=records, devices=devices,
        devices_in_use=sum(1 for d in devices if d["in_use"]),
        chart_labels=list(crop_counts.keys()), chart_values=list(crop_counts.values()),
        user_counts=user_counts,
        total_users=len(users), total_records=len(records),
        admin_count=admin_count, student_count=len(users) - admin_count,
        avg_health=avg_health, admin_user=session["user"])


def _back(tab=""):
    return redirect(url_for("admin_dashboard") + (f"#{tab}" if tab else ""))


def _admin_msg(text, kind="ok"):
    session["admin_msg"] = {"text": text, "kind": kind}


def _target_role(cur, username):
    cur.execute("SELECT role FROM users WHERE username=%s", (username,))
    row = cur.fetchone()
    return (row["role"] or "").strip().lower() if row else None


@app.route("/admin/delete_user/<username>", methods=["POST"])
@admin_required
def delete_user(username):
    """Admins can delete students only. Removing an admin account is a
    deliberate backend/database operation, never a button in the UI."""
    if username == session.get("user"):
        _admin_msg("You can't delete the account you are signed in with.", "err")
        return _back("users")
    conn = get_db(); cur = conn.cursor()
    role = _target_role(cur, username)
    if role is None:
        cur.close(); conn.close()
        _admin_msg("That user no longer exists.", "err")
        return _back("users")
    if role == "admin":
        cur.close(); conn.close()
        _admin_msg("Admin accounts are protected and can only be removed from the database (backend).", "err")
        return _back("users")
    cur.execute("DELETE FROM records WHERE username=%s", (username,))
    cur.execute("UPDATE devices SET owner=NULL, paired_at=NULL, released_user=NULL, released_reason=NULL WHERE owner=%s", (username,))
    cur.execute("DELETE FROM users   WHERE username=%s", (username,))
    conn.commit(); cur.close(); conn.close()
    _admin_msg(f"Deleted student \u201c{username}\u201d and their records.")
    return _back("users")


@app.route("/admin/delete_record/<int:record_id>", methods=["POST"])
@admin_required
def delete_record(record_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM records WHERE id=%s", (record_id,))
    conn.commit(); cur.close(); conn.close()
    _admin_msg(f"Record #{record_id} deleted.")
    return _back("records")


@app.route("/admin/add_user", methods=["POST"])
@admin_required
def add_user():
    username = request.form.get("username","").strip()
    password = request.form.get("password","").strip()
    role     = request.form.get("role","user")
    if role not in ("user", "admin"):
        role = "user"
    if not username or not password:
        _admin_msg("Username and password are required.", "err")
        return _back("users")
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username,password,role) VALUES (%s,%s,%s)",
            (username, password, role)
        )
        conn.commit()
        _admin_msg(f"{'Admin' if role == 'admin' else 'Student'} \u201c{username}\u201d created.")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        _admin_msg(f"The username \u201c{username}\u201d is already taken.", "err")
    cur.close(); conn.close()
    return _back("users")


@app.route("/admin/reset_password/<username>", methods=["POST"])
@admin_required
def reset_password(username):
    """Set a new password for a STUDENT. Other admins' passwords are off limits."""
    new_pw = request.form.get("password","").strip()
    if len(new_pw) < 4:
        _admin_msg("New password must be at least 4 characters.", "err")
        return _back("users")
    conn = get_db(); cur = conn.cursor()
    role = _target_role(cur, username)
    if role is None:
        _admin_msg("That user no longer exists.", "err")
    elif role == "admin":
        _admin_msg("You can't reset another admin's password. Admins change their own.", "err")
    else:
        cur.execute("UPDATE users SET password=%s WHERE username=%s", (new_pw, username))
        conn.commit()
        _admin_msg(f"Password for \u201c{username}\u201d was reset.")
    cur.close(); conn.close()
    return _back("users")


@app.route("/admin/promote/<username>", methods=["POST"])
@admin_required
def promote_user(username):
    conn = get_db(); cur = conn.cursor()
    role = _target_role(cur, username)
    if role == "user" or (role and role != "admin"):
        cur.execute("UPDATE users SET role='admin' WHERE username=%s", (username,))
        conn.commit()
        _admin_msg(f"\u201c{username}\u201d is now an admin.")
    else:
        _admin_msg("Nothing to change.", "err")
    cur.close(); conn.close()
    return _back("users")


@app.route("/admin/change_password", methods=["POST"])
@admin_required
def admin_change_password():
    """An admin changes THEIR OWN password (needs the current one)."""
    current = request.form.get("current","")
    new_pw  = request.form.get("password","").strip()
    if len(new_pw) < 4:
        _admin_msg("New password must be at least 4 characters.", "err")
        return _back("settings")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username=%s AND password=%s", (session["user"], current))
    if cur.fetchone():
        cur.execute("UPDATE users SET password=%s WHERE username=%s", (new_pw, session["user"]))
        conn.commit()
        _admin_msg("Your password was changed.")
    else:
        _admin_msg("Current password is incorrect.", "err")
    cur.close(); conn.close()
    return _back("settings")


@app.route("/admin/export_records.csv")
@admin_required
def export_records():
    import csv, io
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM records ORDER BY id DESC")
    rows = cur.fetchall(); cur.close(); conn.close()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["id","username","created_at","N","P","K","ph","temp","humidity","crop","health","source"])
    for r in rows:
        d = record_to_dict(r)
        w.writerow([d["id"], d["username"], d.get("created_at"), d["N"], d["P"], d["K"], d["ph"],
                    d["temp"], d["humidity"], d.get("crop"), d.get("fertility"), d.get("source")])
    from flask import Response
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=agrisense_records.csv"})


# ---------------------------------------------------------------------------
# STATIC PAGES
# ---------------------------------------------------------------------------
@app.route("/about")
@login_required
def about():
    return render_template("about.html", user=session["user"])


@app.route("/contact")
@login_required
def contact():
    return render_template("contact.html", user=session["user"])


# ---------------------------------------------------------------------------
# ONE-TIME UPDATE OF SAVED RECORDS  (new crop dataset, no rainfall)
#
# Records saved before the Philippine crop dataset still carry old crop names (apple, grapes,
# chickpea, ...) and a health score that counted rainfall. On the first start with this
# version every saved record is re-analysed with the CURRENT models: new crop, TinyML result
# and 6-reading health score. The N, P, K, pH, temperature, humidity, date, owner and photo of
# a record are never touched. A marker row in `app_meta` makes it run only once; to run it again
# after changing the dataset, bump RECORDS_MIGRATION_VERSION.
# ---------------------------------------------------------------------------
RECORDS_MIGRATION_VERSION = "ph-crops-v1-no-rainfall"


def relabel_record(row):
    """New analysis for one saved record (pure function, no database).
    -> (crop, crop_source, tinyml_conf, tiny_json, fertility)   or None if a reading is missing."""
    try:
        N, P, K, ph = float(row["N"]), float(row["P"]), float(row["K"]), float(row["ph"])
        temp, hum   = float(row["temp"]), float(row["humidity"])
    except (TypeError, ValueError, KeyError):
        return None
    fertility = calculate_fertility(N, P, K, ph, temp, hum)
    if (row.get("source") or "manual") == "esp32":
        wj = web_tiny_json(N, P, K, temp, hum, ph)          # same as a fresh scan arriving from the scanner
        if wj:
            name, conf = json.loads(wj)["top3"][0]
            return name, "tinyml-web", conf, wj, fertility
        return recommend_crop(N, P, K, temp, hum, ph)["name"], "knn", None, None, fertility
    # manual entries: the website model, as when they were typed in
    return recommend_crop(N, P, K, temp, hum, ph)["name"], None, None, None, fertility


def migrate_records_to_current_dataset():
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("SELECT pg_advisory_xact_lock(7140)")            # two cold starts never run it twice at once
        cur.execute("SELECT value FROM app_meta WHERE key='records_dataset'")
        done = cur.fetchone()
        if done and done["value"] == RECORDS_MIGRATION_VERSION:
            conn.commit()
            return 0
        cur.execute('SELECT id, "N", "P", "K", ph, temp, humidity, source FROM records ORDER BY id')
        batch = []
        for r in cur.fetchall():
            new = relabel_record(r)
            if new:
                batch.append((r["id"],) + new)
        for i in range(0, len(batch), 500):                          # one statement per 500 rows (fast on a remote database)
            psycopg2.extras.execute_values(cur,
                "UPDATE records AS r SET crop=v.crop, crop_source=v.src, tinyml_conf=v.conf, tiny_json=v.tj, fertility=v.f "
                "FROM (VALUES %s) AS v(id, crop, src, conf, tj, f) WHERE r.id = v.id",
                batch[i:i + 500], template="(%s::int, %s, %s, %s::real, %s, %s::real)")
        cur.execute("INSERT INTO app_meta (key, value) VALUES ('records_dataset', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (RECORDS_MIGRATION_VERSION,))
        conn.commit()
        print(f"[AgriSense] Re-analysed {len(batch)} saved records with the Philippine crop dataset.")
        return len(batch)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


try:
    migrate_records_to_current_dataset()
except Exception as _e:
    print(f"[AgriSense] Record update skipped (will retry on next start): {_e}")


if __name__ == "__main__":
    app.run(debug=True)
