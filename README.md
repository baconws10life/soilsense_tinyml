# SoilSense — TinyML Soil Intelligence Platform

A Flask app that turns soil sensor readings (N, P, K, pH, temperature, humidity, rainfall) into
a soil health score, a ranked crop-recommendation list, and plain-language explanations — redesigned
to match the SoilSense dashboard (dark, growth-green theme with Overview / Sensor Readings / Crop
Picks / AI Assistant / Learning Guide).

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5000, register an account, and log in. The first user you register as
`admin` gets access to the `/admin` panel.

## What changed from the original `app.py`

**Bug fix — crop data didn't match the model.** The original `crop_info` dictionary described
`wheat`, `sugarcane`, `tea`, and `millet` — none of which exist in `Crop_recommendation.csv` — and
was missing 9 of the 22 crops the model can actually predict (chickpea, mothbeans, blackgram,
pomegranate, banana, mango, watermelon, muskmelon, apple, orange, papaya, coconut, coffee). Most
predictions were silently falling back to a generic sentence. `CROP_INFO` now covers all 22 real
labels with a description, category, emoji, and harvest-window estimate for each.

**New pages, one per sidebar item:**
- **Overview** (`/overview`, alias `/home`) — greeting, health-score ring with letter grade,
  optimal/attention counts, at-a-glance sensor cards, and an NPK bar chart.
- **Sensor Readings** (`/sensors`) — every reading (pH, moisture, temperature, N, P, K, rainfall,
  plus an estimated organic-matter figure) with its optimal band, a mini gauge, and plain-language
  status copy.
- **Crop Picks** (`/crops`) — every crop in the training set ranked by an explainable match score
  (the % of readings that fall inside that crop's learned 10th-90th-percentile comfort band, not a
  black-box classifier score), filterable by category and sorted by match/name/harvest speed.
- **AI Assistant** (`/ai-assistant` + `POST /api/ai/ask`) — a small rule-based Q&A engine, grounded
  in the user's latest reading, answering questions about pH, NPK, moisture, organic matter,
  rainfall, the health score, and crop matching. No external API key required.
- **Learning Guide** (`/learning`) — a static, accordion-style explainer for each concept above.

**Feature/robustness improvements to the original routes:**
- `/dashboard` (kept, repurposed as "New Reading") now validates numeric input instead of raising
  a 500 on bad data, saves uploaded images with a collision-safe filename and an extension
  allow-list, and stores a `source` (`manual`/`esp32`) and `fertility` score per record.
- The database schema gained `source`, `fertility`, and `created_at` columns via idempotent
  migrations, so the reading history and admin panel can show a real timeline and per-record score.
- Every soil-health threshold now has an explicit "watch" band, not just optimal/out-of-range —
  most of the UI distinguishes optimal / monitor / low / high rather than a binary pass/fail.
- `login_required`/`admin_required` decorators replace repeated inline session checks.
- The ESP32 endpoint (`POST /api/esp32`) is unchanged in shape but now also returns a `grade`.

## Project layout

```
app.py                     Flask app: routes, ML model, scoring, rule-based assistant
Crop_recommendation.csv    training data (22 crops, 2200 rows)
templates/                 Jinja templates (base.html holds the sidebar shell)
static/css/style.css       design tokens + all page styles
static/js/main.js          accordion, chat, filters, ring-gauge rendering
static/uploads/            soil photos uploaded from the New Reading form (created at runtime)
database.db                SQLite database (created at first run)
```

## Notes

- The "AI Assistant" is intentionally rule-based (keyword-routed, grounded in the user's own
  latest reading) rather than calling an external LLM API, so the app has no external dependency
  or API key requirement at request time.
- Organic matter is clearly labeled everywhere as an *estimate* derived from N and K — real
  organic-matter % requires a lab test (loss-on-ignition or Walkley-Black), which a soil-moisture/
  NPK probe cannot measure directly.


## Device status on the dashboard

The **Overview** page has a "Your Device" card that reads *Device connected* (scanner ID + last scan
time, **Manage device** button) or *Device not connected* (**Link device** button). The same status
shows in the top bar and as a dot next to **Link Device** in the sidebar. The Link Device page
(`/pair`) is manual: type the 6-character code from the scanner's LCD (no camera / QR scanner).


## Admin accounts

Registration only ever creates normal users (`role = 'user'`): the form has no role field, and the
server ignores a `role` value even if one is posted by hand. Names like `admin` / `administrator`
(and the seeded admin's name) cannot be registered.

Admins live in the database. To get one:

- **Already in your database** - keep using it.
- **Fresh database / recovery** - in Vercel -> Settings -> Environment Variables set `ADMIN_USERNAME`
  and `ADMIN_PASSWORD`, then redeploy. The account is created once if it does not exist; it never
  overwrites an existing account. No default password is stored in the code.
- **More admins** - log in as an admin -> Admin Panel -> Add New User -> Role: Admin.

An admin cannot delete their own account from the panel (prevents locking yourself out).


## Live "new scan" popup

When the linked ESP32 sends a scan (**Scan now** on the device), a popup appears on whatever page is
open (user pages and the Admin Panel) showing what the sensor just measured: Nitrogen, Phosphorus,
Potassium, pH, temperature and moisture (each with its status), plus the recommended crop and soil
health. Buttons: **Dismiss** and **View dashboard**; Esc also closes it.

How it works: Vercel can't keep a live connection open, so `static/js/scan-popup.js` asks
`GET /api/latest-scan?since=<id>` every 4 seconds while the tab is visible (hidden tabs make no
requests). Opening the site never pops old scans; only scans newer than the last one you saw do.
Only your own device scans are returned. To change the interval add `data-interval="8000"` (ms) to the
script tag in `templates/base.html`.

---

## Update: TinyML + offline scanning + new Sensors page

**TinyML (on the ESP32-S3).** `tinyml/train_tinyml.py` trains a small neural network (6 -> 24 -> 22) on
`Crop_recommendation.csv` and writes `soilsense_tinyml.h`. Put that header in the same folder as the `.ino`.
The device now picks the crop itself on every scan, with no Wi-Fi, and sends `crop` + `crop_conf` with the reading.
The website stores it (`records.crop_source = 'tinyml'`) and falls back to its own KNN model when an older
firmware does not send a crop.

**Offline scanning.** No Wi-Fi at start-up -> the LCD asks "Continue offline?". Scans are stored in the ESP32-S3
flash (up to 100) and uploaded oldest-first as soon as Wi-Fi returns. `/api/esp32` now accepts the optional fields
`offline`, `age_s` (so the record keeps the time it was scanned) and `scan_uid` (a re-sent scan is never saved twice).
New `records` columns are added automatically on the next start: `crop_source`, `tinyml_conf`, `offline`, `scan_uid`.

**Sensors page.** Summary on top (grade, counts, "Do this first", suggested crop), filter chips, readings grouped
as Nutrients / Soil condition / Climate, a "What to do" line per reading and a folded Details section.

**AI assistant.** New topics: what TinyML is, why a crop was picked (uses the crop's typical ranges), how reliable
the result is, TinyML vs the website model, and offline scanning. Short keywords such as "ph" now match whole words
only, so "phosphorus" is no longer answered as pH.

**More TinyML (three on-device networks).** `tinyml/train_tinyml.py` now trains and exports three tiny networks that
share the six probe inputs: (1) crop recommender with **top-3** crops, (2) **probe check** autoencoder that flags
readings that do not look like real soil ("Odd value" on the LCD header), (3) **nutrient advisor** that names what to
fix (distilled from the SoilSense threshold rules). The results travel with each scan and appear on the Sensors page
in the "On-device TinyML analysis" card. Offline storage holds up to 80 scans. The script prints the accuracy numbers to
quote in the thesis.
