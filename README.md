# AgriSense — TinyML Soil Intelligence Platform

A Flask app that turns soil sensor readings (N, P, K, pH, temperature, humidity, rainfall) into
a soil health score, a ranked crop-recommendation list, and plain-language explanations — redesigned
to match the AgriSense dashboard (dark, growth-green theme with Home / Sensor Readings / Crop
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
- **Home** (`/home`, also `/overview`) — greeting, health-score ring with letter grade,
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
Crop_recommendation.csv    training data (22 Philippine crops, 2200 rows) - built by data/make_ph_dataset.py
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

The **Home** page has a "Your Device" card that reads *Device connected* (scanner ID + last scan
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
fix (distilled from the AgriSense threshold rules). The results travel with each scan and appear on the Sensors page
in the "On-device TinyML analysis" card. Offline storage holds up to 80 scans. The script prints the accuracy numbers to
quote in the thesis.


---

## Device discovery (replaces the 6-character code)

The LCD is now **one big "SCAN NOW" button**. Nothing is typed on the device.

**How the website "scans" for the scanner.** A browser cannot search a Wi-Fi network, and the site runs on Vercel, not on
your LAN. So the scanner announces itself: every 8 s it posts a heartbeat to `/api/device/heartbeat`. **Find device**
(Home + the *Find Device* page) lists the scanners whose last heartbeat is under 25 s old, i.e. the ones online right now.

| Who | What they can do |
|---|---|
| Student | See free scanners, **Connect** to a free one, **Disconnect** from their own. A scanner in use shows *In use* (no name) and cannot be connected. |
| Admin | Everything above, plus **Disconnect user** on any scanner (Admin > Scanners). The kicked user sees an "admin disconnected you" notice. Admins also see who is using each scanner. |

States shown on the website: *Searching, Scanner found, Connected, In use, Not found, Connection lost* (you own a scanner that
stopped responding), *Can't reach server*, and a warning when the scanner is online but its soil probe isn't answering.

Rules enforced on the server (not just hidden buttons):
* Connecting is one atomic `UPDATE ... WHERE owner IS NULL AND online`, so two people pressing Connect at once cannot both win.
* Readings are only accepted from a scanner that has an owner; otherwise `/api/esp32` answers 403 and the LCD says NOT CONNECTED.
* A scanner offline for 10 minutes is released automatically (no ghost connections).
* Only the owner or an admin can disconnect.

New endpoints: `POST /api/device/heartbeat` (scanner), `GET /api/devices`, `POST /api/devices/<id>/connect`,
`POST /api/devices/<id>/disconnect`, `POST /api/devices/dismiss_notice` (website). Removed: `/api/device/pair`, `/api/device/unlink`.
New columns on `devices` are added automatically on the first request after deploying.

**Firmware:** `firmware/soilsense_scan_button.ino`. Edit `WIFI_SSID`, `WIFI_PASSWORD`, `SERVER_BASE` (and `API_KEY` if you set
`ESP32_API_KEY`). The sketch prints `Device ID on the website: AgriSense Scanner XXXX` on the Serial Monitor, and shows `ID XXXX`
in a corner of the LCD, so you can tell scanners apart in a classroom.


---

## Update: clear records, Serial Monitor tap, TinyML on the website

**Clear / filter-delete records.** *Analyze Soil > Analysis History* and *Admin > All Records* now have a toolbar:
filter by source (scanner / manual), crop and date range, tick rows, then **Delete selected**, **Delete all shown**
(appears when a filter is on) or **Clear all records**. Everything goes through `POST /api/records/delete`
(JSON only). A student can only ever delete their own readings (checked on the server). An admin's *Clear all* removes
everyone's readings and asks them to type `DELETE`. Files: `templates/_records_tools.html`, `static/js/records-tools.js`.

**Serial Monitor tap (firmware 2.1).** Open the Serial Monitor at 115200 and send `tap` (or `t`, `scan`, `s`, `1`):
it does exactly what touching the screen does. `status` and `help` also work. Everything the LCD would show is printed as
`[SCREEN] ...` lines, so a broken LCD no longer blocks testing. Any line-ending setting works.

**TinyML on the website.** The same three networks the ESP32 used now also run on the website:
* `static/tinyml/soilsense_tinyml.json` - the trained weights (made by `tinyml/train_tinyml.py`, which now writes both the
  Arduino header and this file).
* `tinyml_web.py` - runs them with numpy on the server. A scan that arrives without on-device TinyML results is analysed here:
  the record gets `crop_source = 'tinyml-web'`, its top-3 crops, probe check and advisor flags. Older and manual records are
  analysed live when the Sensors page is opened. A scanner that still sends its own TinyML result is respected.
* `static/js/tinyml.js` + `templates/tinyml.html` - the **TinyML** page (sidebar): what TinyML is, a live demo with sliders,
  a look inside the network, one neuron worked out by hand, accuracy numbers and honest limits. Runs entirely in the browser.


## Update: "Scan now" button on the website (firmware 2.2)

Once you are **connected** to a scanner (Home or the *Find Device* page), a big **Scan now** button appears under it.
Pressing it does the same as touching the scanner's screen: one reading, saved to your account, and the usual new-scan popup.

How it works (Vercel cannot push to the scanner, so the scanner asks):
1. `POST /api/devices/<id>/scan` - only the person connected to that scanner. It stores `devices.scan_requested_at` and returns `baseline`
   (the id of your newest scanner reading).
2. The scanner's next check-in (`/api/device/heartbeat`, every **3 s while someone is connected**, 8 s otherwise) gets `"scan": true`
   in the reply - once, then the request is cleared. A request nobody picks up is dropped after 30 s.
3. The scanner reads the probe and posts to `/api/esp32` as usual. The page watches `GET /api/latest-scan?since=<baseline>` and shows
   *Done*, or a specific message if nothing came back within 45 s (scanner offline / probe not answering / not picked up).

* New column `devices.scan_requested_at` is added automatically on the next start. Nothing else in the database changes.
* `GET /api/devices` now includes `scan_pending` for the scanner you are connected to.
* Disconnecting, an admin disconnect, or the scanner being released cancels any waiting request.
* Firmware: flash `firmware/soilsense_scan_button.ino` (v2.2). Tunable: `HEARTBEAT_CONNECTED_MS` (3000 ms; smaller = faster button,
  more traffic). Older firmware still works - it just ignores the website button.


## Update: Philippine crop dataset

`Crop_recommendation.csv` now holds **22 Philippine crops** instead of the original ones (apple, grapes, chickpea, lentil, cotton, ...).
Same 2,200 rows (100 per crop), same units and **same N-P-K scale**; the `rainfall` column is gone (see the next section). Nothing about how the site reads NPK changed
(soil health scoring, the scanner and the API are untouched); only the crops did.

| Category | Crops |
|---|---|
| Grains | rice, corn |
| Root Crops | cassava, sweetpotato (kamote) |
| Legumes | mungbean (mongo), peanut, sitaw |
| Vegetables | tomato, eggplant (talong), ampalaya, cabbage, onion |
| Fruits | banana, mango, pineapple, papaya, calamansi |
| Cash Crops | coconut, sugarcane, coffee, cacao, abaca |

* Crops that were already in the original data and are grown here (rice, corn = old "maize", mungbean, banana, mango, papaya, coconut,
  coffee, calamansi ~ old "orange") **keep their original N-P-K ranges**. New crops get N-P-K on the same scale from their general
  nutrient demand (cabbage / sugarcane heavy N; cassava / abaca / pineapple potassium-hungry; legumes low N). Temperature, humidity, pH and
  are set for Philippine conditions (lowland, highland, wet and dry-season crops).
* **It is a synthetic reference table**, like the original: every row is drawn uniformly inside a range written in
  `data/make_ph_dataset.py` (one line per crop). For the paper, check those ranges against BSWM / DA-BPI / PhilRice / PCAARRD references or
  your own soil samples, edit them, and rebuild.
* **To change a crop or range:** edit `PROFILES` in `data/make_ph_dataset.py`, run `python data/make_ph_dataset.py`, then
  `python tinyml/train_tinyml.py` (rewrites `static/tinyml/soilsense_tinyml.json` and `tinyml/soilsense_tinyml.h`). If you add or remove a crop,
  also add/remove it in `CROP_INFO` in `app.py` (description, category, emoji, harvest) and in the Supported Crops list in `admin_dashboard.html`.
* The original dataset is kept as `data/Crop_recommendation_original.csv`.
* TinyML was retrained on the new data (held-out top-1 97.5 %, top-3 100 %; website KNN on the same 6 inputs 93.9 %). New categories
  *Root Crops* and *Vegetables* appear as filters on the Crop Picks page.
* Existing saved records keep the crop name they were saved with (e.g. an old "apple"); they are not rewritten.


## Update: no rainfall + saved records re-labelled (firmware 2.3)

**Rainfall is gone from the whole prototype.** The probe never measured it, so every place that used it was removed:
dataset (`Crop_recommendation.csv` has 6 inputs: N, P, K, temperature, humidity, pH), the KNN crop model, Crop Picks matching, the
health score (now **6 readings**, each worth ~17 points instead of 7 x 14), the New Reading form, the history tables, the Sensors page,
the CSV export, the AI assistant (the "Why does rainfall matter?" topic was removed) and the scanner firmware (v2.3 no longer sends it).
`POST /api/esp32` simply ignores a `rainfall` field if an older scanner still sends one.
The `rainfall` column in the `records` table is **kept** (old values stay in the database, nothing is deleted) but nothing reads or writes it.

**Saved records were updated to the new crop data.** On the first start of this version the website re-analyses every row in `records`
once (`migrate_records_to_current_dataset()` at the end of `app.py`):

* `crop`, `crop_source`, `tinyml_conf` and `tiny_json` are recomputed with the current models (scanner rows: the TinyML networks, like a new scan;
  manual rows: the website KNN, like a typed-in reading), and `fertility` is recomputed with the 6-reading score.
* Never changed: owner, date, source, N, P, K, pH, temperature, humidity, photo, scan id.
* It runs once. A marker row (`app_meta.records_dataset`) records that it is done; it is safe if two serverless instances start together (advisory lock).
  To run it again after changing the dataset or models, change `RECORDS_MIGRATION_VERSION` in `app.py` (or delete that row).
* Check the Vercel log for `Re-analysed N saved records ...`.
* An old record's N-P-K can land on a surprising new crop (e.g. an old "apple" with K 200 becomes the nearest Philippine crop). That is expected:
  the crop is a recommendation recomputed from the saved readings, not a stored fact.

TinyML was retrained on the 6-input data: held-out top-1 96.1 % (website KNN on the same inputs 94.8 %), top-3 100 %.


## Update: 16x4 I2C LCD scanner (firmware 3.0)

The ILI9341 colour touchscreen was replaced by a **16x4 character LCD on I2C** (4 wires: GND, VCC, SDA, SCL), so there is no touch.
`firmware/soilsense_lcd1604.ino` (the old TFT sketches stay in `firmware/` for reference).

* **Scan from the website.** Connect to the scanner on the website, press **Scan now**: the scanner reads the probe, sends the reading, and the LCD shows
  the **same result as the website popup**, in 3 pages that repeat **4 times** (about 40 s; `RESULT_PAGE_MS`, `RESULT_REPEATS` at the top of the sketch):
  1. NPK: Nitrogen, Phosphorus, Potassium (mg/kg) with a status each,
  2. Soil: pH, temperature, moisture with a status each,
  3. Best crop, match % and soil health score / grade.
  Status words: `OK` = Optimal, `MON` = Watch/Monitor, `LOW`, `HIGH` - the same rule the website uses (the server sends it, see below).
* Also: the board's **BOOT** button or `tap` in the Serial Monitor (115200) takes a reading; `again` replays the last result; `status`, `lcd`, `help`.
* **No LCD library needed.** The sketch drives the PCF8574 backpack itself and finds its I2C address (0x20-0x27 / 0x38-0x3F). Set `LCD_SDA` / `LCD_SCL`
  at the top; if nothing answers there it also tries 8/9, 3/6, 1/2 and 41/42 and prints where it found the LCD. Blank or blocks only? Turn the blue contrast screw on the backpack.
* **Server:** `POST /api/esp32` now also returns `crop_conf`, `grade_text` and `lvl_N`, `lvl_P`, `lvl_K`, `lvl_ph`, `lvl_temp`, `lvl_humidity`
  (optimal / watch / low / high). Older scanners ignore the extra keys.
