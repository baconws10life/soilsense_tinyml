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
  All domain logic, AI assistant,
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

app = Flask(__name__)
app.secret_key = os.environ.get("AGRISENSE_SECRET_KEY") or os.environ.get("SOILSENSE_SECRET_KEY", "dev-secret-change-me")
app.jinja_env.globals["zip"] = zip

BASE_DIR     = os.path.dirname(__file__)
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

    # Bug reports sent from the Contact page; admins read them in the Admin Panel.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bug_reports (
            id         SERIAL PRIMARY KEY,
            username   TEXT,
            title      TEXT,
            page       TEXT,
            details    TEXT,
            status     TEXT DEFAULT 'open',
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
    # offline sync: was the scan stored on the ESP32 while offline, and a unique id so a
    # re-sent scan is never saved twice. (crop / crop_source / tinyml_conf / tiny_json are
    # old columns from the removed crop recommendation: kept so old rows stay readable.)
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
    return {
        "counts": counts, "total": len(readings), "todo": todo[:3],
        "when": (rec.get("created_at") or "")[:16],
        "source_label": "ESP32-S3 scanner" if src == "esp32" else "Manual entry",
        "was_offline": bool(rec.get("offline")),
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
# RULE-BASED AI ASSISTANT
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
]
AI_FALLBACK = "I can walk you through pH, NPK nutrients, moisture, temperature, organic matter, your health score and what to do when the scanner has no Wi-Fi. Try one of the suggestions, or ask in your own words."


AI_TOPICS_EXTRA = [
    {"id":"offline", "chip":"What if there is no Wi-Fi?", "keywords":["offline","no wifi","no wi-fi","no internet","without wifi","without wi-fi","saved scan","sync","upload"],
     "answer": lambda r: ("If the scanner cannot find Wi-Fi it asks 'Continue offline?'. Say yes and Scan now still works: the reading is stored in the ESP32-S3's memory "
        "(up to 80 scans, kept even if you switch it off). As soon as Wi-Fi is back they upload by themselves, oldest first, and show up on this website. "
        "A scan is only deleted from the device after the website confirms it.")},
    {"id":"temp",    "chip":None, "keywords":["temperature","temp","warm","hot","cold"],
     "answer": lambda r: ("Soil temperature drives germination, root growth and microbe activity. Most crops are happiest at 20-35 C." +
        (f" Your latest reading is {r['temp']} C, which is {reading_status('temp', r['temp'])[1].lower()}" if r else ""))},
]
AI_TOPICS = AI_TOPICS + AI_TOPICS_EXTRA


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



@app.after_request
def _no_cache_html(resp):
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


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
    if request.method == "GET" and "user" in session:
        return redirect(url_for("admin_dashboard" if str(session.get("role","")).strip().lower() == "admin" else "overview"))
    wants_json = request.headers.get("X-Requested-With") == "fetch"
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
                target = nxt
            elif result["role"].strip().lower() == "admin":
                target = url_for("admin_dashboard")
            else:
                target = url_for("overview")
            if wants_json:
                return jsonify(ok=True, redirect=target)
            return redirect(target)
        error = "Incorrect username or password. Please try again."
        if wants_json:
            return jsonify(ok=False, error=error), 401
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
@app.route("/tinyml")
@login_required
def crops_removed():
    """Crop recommendation and the TinyML crop page were removed: old links and bookmarks land on Sensors."""
    return redirect(url_for("sensors"))


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
                records=records, user=session["user"])

        fertility = calculate_fertility(N, P, K, ph, temp, humidity)

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
        """, (session["user"], N, P, K, ph, temp, humidity, "", filename, fertility))
        conn.commit()
        cur.close()
        conn.close()

    records = _records_for(session["user"], session["role"])
    return render_template("dashboard.html",
        result=result, fertility=fertility, error=None,
        records=records, user=session["user"])


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

    # --- optional offline-sync info (older firmware does not send it) ------------
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

    # Scans taken offline keep the time they were scanned (not the time they were uploaded)
    cur.execute("""
        INSERT INTO records
          (username,"N","P","K",ph,temp,humidity,crop,image,source,fertility,created_at,
           offline,scan_uid)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'',NULL,'esp32',%s,
                (NOW() - make_interval(secs => %s))::TEXT,
                %s,%s)
    """, (username, N, P, K, ph, temp, humidity, fertility, age_s,
          1 if offline else 0, scan_uid))
    cur.execute("UPDATE devices SET last_seen=NOW()::TEXT, last_seen_at=NOW() WHERE token_hash=%s", (th,))
    conn.commit()
    cur.close()
    conn.close()

    out = {
        "status":"ok","user":username,
        "crop":"-",     # crop recommendation was removed; kept so scanners still on the OLD sketch keep showing their result
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
    try:
        cur.execute("SELECT * FROM bug_reports ORDER BY (status='open') DESC, id DESC")
        bug_reports = cur.fetchall()
    except Exception:
        conn.rollback()
        bug_reports = []
    conn.commit()
    cur.close()
    conn.close()

    user_counts  = Counter([r["username"] for r in records])
    scores       = [record_to_dict(r)["fertility"] for r in records]
    avg_health   = round(sum(scores)/len(scores),1) if scores else 0
    admin_count   = sum(1 for u in users if (u["role"] or "").strip().lower() == "admin")

    return render_template("admin_dashboard.html",
        users=users, records=records, devices=devices, bug_reports=bug_reports,
        open_bugs=sum(1 for b in bug_reports if b['status'] == 'open'),
        devices_in_use=sum(1 for d in devices if d["in_use"]),
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
    w.writerow(["id","username","created_at","N","P","K","ph","temp","moisture","health","source"])
    for r in rows:
        d = record_to_dict(r)
        w.writerow([d["id"], d["username"], d.get("created_at"), d["N"], d["P"], d["K"], d["ph"],
                    d["temp"], d["humidity"], d.get("fertility"), d.get("source")])
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


CONTACT_EMAILS = [
    "johnchrispagtalunan@gmail.com",
    "carlmatthewreyes6@gmail.com",
    "cortezeldrich@gmail.com",
    "verkiel7@gmail.com",
]


@app.route("/contact")
@login_required
def contact():
    return render_template("contact.html", user=session["user"], emails=CONTACT_EMAILS,
                           bug_msg=session.pop("bug_msg", None))


@app.route("/report-bug", methods=["POST"])
@login_required
def report_bug():
    title   = (request.form.get("title") or "").strip()[:120]
    page    = (request.form.get("page") or "").strip()[:80]
    details = (request.form.get("details") or "").strip()[:3000]
    if not title or not details:
        session["bug_msg"] = {"kind": "err", "text": "Please add a short title and describe what went wrong."}
        return redirect(url_for("contact") + "#report-bug")
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO bug_reports (username, title, page, details, status, created_at) "
                    "VALUES (%s,%s,%s,%s,'open',%s)",
                    (session["user"], title, page, details, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit(); cur.close(); conn.close()
        session["bug_msg"] = {"kind": "ok", "text": "Thanks! Your bug report was sent to the admins."}
    except Exception as e:
        print(f"[AgriSense] bug report failed: {e}")
        session["bug_msg"] = {"kind": "err", "text": "Sorry, the report could not be saved. Please email the team instead."}
    return redirect(url_for("contact") + "#report-bug")


@app.route("/admin/bug/<int:bug_id>/<action>", methods=["POST"])
@admin_required
def admin_bug(bug_id, action):
    if action not in ("resolve", "reopen", "delete"):
        return _back("bugs")
    conn = get_db(); cur = conn.cursor()
    if action == "delete":
        cur.execute("DELETE FROM bug_reports WHERE id=%s", (bug_id,))
    else:
        cur.execute("UPDATE bug_reports SET status=%s WHERE id=%s",
                    ("resolved" if action == "resolve" else "open", bug_id))
    conn.commit(); cur.close(); conn.close()
    return _back("bugs")


if __name__ == "__main__":
    app.run(debug=True)
