// ============================================================
// SOILSENSE SCANNER  -  ESP32-S3 + 16x4 I2C LCD (HD44780 + PCF8574 backpack) + RS485 soil probe
//
// No touchscreen. Press SCAN NOW on the website (Dashboard > Find device > Connect first):
// the scanner reads the probe, sends the reading, and the LCD shows the SAME result the website
// popup shows: N, P, K, pH, temperature, moisture (each with Optimal / Watch / Low / High),
// the recommended crop and the soil health score. The result pages repeat RESULT_REPEATS times
// so you have time to read them.
//
// Also: the board's BOOT button, or typing  tap  in the Serial Monitor, takes a reading.
//
// LCD wiring (4 wires):  GND -> GND,  VCC -> 5V (most backpacks) or 3V3,  SDA -> LCD_SDA,  SCL -> LCD_SCL.
// No LCD library needed: the sketch drives the backpack itself, and finds its I2C address (0x27, 0x3F...).
// If the LCD lights up but shows only blocks or nothing, turn the small blue contrast screw on the backpack.
// ============================================================

#include <Wire.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <esp_system.h>
#include <Preferences.h>

// ---- EDIT THESE ------------------------------------------------------------

const char* WIFI_SSID     = "akoaypogi";        // 2.4 GHz network only
const char* WIFI_PASSWORD = "12345678";

const char* SERVER_BASE   = "https://soilsense-amber.vercel.app";
const char* API_KEY       = "";                 // leave "" unless ESP32_API_KEY is set on Vercel

// LCD I2C pins. If nothing is found here the sketch also tries 8/9, 3/6, 1/2 and 41/42.
#define LCD_SDA   8
#define LCD_SCL   9

// ---- OPTIONAL TUNING -------------------------------------------------------

const unsigned long HEARTBEAT_MS           = 8000;   // how often we tell the website "I'm here"
const unsigned long HEARTBEAT_CONNECTED_MS = 3000;   // faster while someone is connected (website button reacts quicker)

#define RESULT_PAGES        3        // NPK page, soil page, crop + health page
#define RESULT_PAGE_MS      3500     // how long each page stays on the LCD
#define RESULT_REPEATS      4        // how many times the pages are shown (3 pages x 3.5 s x 4 = ~42 s)

#define USE_BOOT_BUTTON     1        // 1 = the board's BOOT button (GPIO0) also takes a reading
#define BOOT_BUTTON_PIN     0
#define SHOW_DEVICE_ID      1

#define FW_VERSION "3.0"

const wifi_power_t WIFI_TX_POWER = WIFI_POWER_11dBm;   // gentler Wi-Fi power bursts (raise to WIFI_POWER_15dBm only if Wi-Fi will not connect)

// ---- PINS (unchanged) --------------------------------------------------------
// RS485 probe: RX 16, TX 17, DE/RE 4
#define RX2_PIN    16
#define TX2_PIN    17
#define RE_DE_PIN  4

#define LCD_COLS   16
#define LCD_ROWS   4

// ============================================================
// SENSOR DATA
// ============================================================

float soilHumidity = 0;
float soilTemperature = 0;
float soilPH = 0;

uint16_t soilConductivity = 0;
uint16_t nitrogen = 0;
uint16_t phosphorus = 0;
uint16_t potassium = 0;

bool sensorOK = false;
byte response[32];

const byte query[] = {
  0x01, 0x03, 0x00, 0x00,
  0x00, 0x07, 0x04, 0x08
};

// ============================================================
// STATE
// ============================================================

int  connState = -1;          // -1 unknown, 0 nobody connected, 1 somebody connected
bool serverOK = true;

String sensorState = "unknown";
String deviceToken = "";
char deviceTag[8] = "----";

unsigned long lastHeartbeat = 0;
unsigned long lastWifiRetry = 0;
unsigned long lastTapMs = 0;
bool wifiWasUp = false;

bool webScan = false;         // website "Scan now" arrived with a check-in
bool serialTap = false;

volatile bool inNetwork = false;   // (kept so the HTTP helper below is unchanged)

String serialBuf = "";
unsigned long serialLastChar = 0;

void redrawUi();


// ============================================================
// 16x4 I2C LCD  (HD44780 controller behind a PCF8574 "backpack")  -  tiny built-in driver
// Backpack bits: P0=RS  P1=RW  P2=EN  P3=backlight  P4..P7 = D4..D7
// ============================================================

int     lcdAddr = 0;
bool    lcdOK = false;
uint8_t lcdBL = 0x08;                          // backlight on
char    lcdShadow[LCD_ROWS][LCD_COLS + 1];     // what is on the glass now: only changed lines are rewritten

void lcdExp(uint8_t v) {
  Wire.beginTransmission(lcdAddr);
  Wire.write(v | lcdBL);
  Wire.endTransmission();
}

void lcdSend4(uint8_t hiNibble, uint8_t rs) {
  uint8_t v = (hiNibble & 0xF0) | rs;
  lcdExp(v | 0x04);                            // EN high
  delayMicroseconds(2);
  lcdExp(v & ~0x04);                           // EN low -> latch
  delayMicroseconds(60);
}

void lcdSend(uint8_t value, uint8_t rs) {
  lcdSend4(value & 0xF0, rs);
  lcdSend4((uint8_t)(value << 4), rs);
}

void lcdCmd(uint8_t c) {
  lcdSend(c, 0);
  if (c == 0x01 || c == 0x02) delay(3);        // clear / home are slow
}

void lcdSetCursor(uint8_t col, uint8_t row) {
  // 16x4 modules: lines start at 0x00, 0x40, 0x10, 0x50 (20x4: 0x00, 0x40, 0x14, 0x54) = 0x00/0x40 + COLS
  static const uint8_t rowOff[4] = { 0x00, 0x40, (uint8_t)(0x00 + LCD_COLS), (uint8_t)(0x40 + LCD_COLS) };
  lcdCmd(0x80 | (rowOff[row & 3] + col));
}

int lcdFindAddress() {
  for (int a = 0x20; a <= 0x3F; a++) {
    if (a > 0x27 && a < 0x38) continue;          // PCF8574 = 0x20-0x27, PCF8574A = 0x38-0x3F
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) return a;
  }
  return 0;
}

void lcdInitChip() {
  delay(60);
  lcdSend4(0x30, 0); delay(5);
  lcdSend4(0x30, 0); delay(5);
  lcdSend4(0x30, 0); delay(1);
  lcdSend4(0x20, 0); delay(1);                   // switch to 4-bit mode
  lcdCmd(0x28);                                  // 4-bit, 2-line mode (also drives 4-line panels), 5x8 font
  lcdCmd(0x0C);                                  // display on, no cursor
  lcdCmd(0x06);                                  // cursor moves right
  lcdCmd(0x01);                                  // clear
  memset(lcdShadow, 0, sizeof(lcdShadow));
}

bool lcdBegin() {
  const int8_t tries[][2] = { { LCD_SDA, LCD_SCL }, { 8, 9 }, { 3, 6 }, { 1, 2 }, { 41, 42 } };

  for (unsigned i = 0; i < sizeof(tries) / sizeof(tries[0]); i++) {
    int sda = tries[i][0], scl = tries[i][1];

    bool dup = false;
    for (unsigned j = 0; j < i; j++) if (tries[j][0] == sda && tries[j][1] == scl) dup = true;
    if (dup) continue;

    Wire.end();
    Wire.begin(sda, scl);
    Wire.setClock(100000);

    int a = lcdFindAddress();
    if (a) {
      lcdAddr = a;
      lcdInitChip();
      lcdOK = true;
      Serial.printf("[LCD] found at I2C address 0x%02X on SDA=%d SCL=%d\n", a, sda, scl);
      return true;
    }
  }

  lcdOK = false;
  Serial.println("[LCD] NOT FOUND. Check the 4 wires (GND, VCC, SDA, SCL) and LCD_SDA / LCD_SCL at the top of the sketch.");
  return false;
}

// Show 4 lines of up to 16 characters. Only lines that changed are rewritten (no flicker).
void lcdShow(const char* l0, const char* l1, const char* l2, const char* l3) {
  const char* in[LCD_ROWS] = { l0, l1, l2, l3 };
  bool changed = false;

  for (int r = 0; r < LCD_ROWS; r++) {
    char line[LCD_COLS + 1];
    int n = in[r] ? (int)strlen(in[r]) : 0;
    for (int c = 0; c < LCD_COLS; c++) line[c] = (c < n) ? in[r][c] : ' ';
    line[LCD_COLS] = 0;

    if (memcmp(line, lcdShadow[r], LCD_COLS + 1) != 0) {
      changed = true;
      memcpy(lcdShadow[r], line, LCD_COLS + 1);
      if (lcdOK) {
        lcdSetCursor(0, r);
        for (int c = 0; c < LCD_COLS; c++) lcdSend((uint8_t)line[c], 1);
      }
    }
  }

  if (changed) {      // mirror to the Serial Monitor
    Serial.printf("[LCD] |%s|%s|%s|%s|\n", lcdShadow[0], lcdShadow[1], lcdShadow[2], lcdShadow[3]);
  }
}

void lcdRepaintAll() {            // after noise on the wires: rewrite every line without clearing
  memset(lcdShadow, 0, sizeof(lcdShadow));
  redrawUi();
}

// ============================================================
// WHAT THE LCD SHOWS
// ============================================================

struct ScanResult {
  bool  have;
  char  crop[24];
  int   conf;                 // -1 = not available
  float health;               // soil health 0-100
  char  grade[4];
  char  level[6][10];         // optimal / watch / low / high   for N, P, K, pH, temp, moisture
  float v[6];                 // N, P, K, pH, temp, moisture as measured
};
ScanResult res = { false, "", -1, 0, "", { "", "", "", "", "", "" }, { 0, 0, 0, 0, 0, 0 } };

bool          resultsActive = false;
unsigned long resultsStart = 0;

unsigned long msgUntil = 0;                // a temporary message is on screen until this time
char          msgLine[4][24];

const char* statusWord(const char* lvl) {  // same meaning as the website popup (Optimal / Watch / Low / High), shortened to fit
  if (!strcmp(lvl, "optimal")) return "OK";
  if (!strcmp(lvl, "watch"))   return "MON";      // "Monitor" (the website popup says Watch)
  if (!strcmp(lvl, "low"))     return "LOW";
  if (!strcmp(lvl, "high"))    return "HIGH";
  return "";
}

void showMsg(const char* a, const char* b = "", const char* c = "", const char* d = "", unsigned long ms = 0) {
  resultsActive = false;
  snprintf(msgLine[0], sizeof(msgLine[0]), "%s", a);
  snprintf(msgLine[1], sizeof(msgLine[1]), "%s", b);
  snprintf(msgLine[2], sizeof(msgLine[2]), "%s", c);
  snprintf(msgLine[3], sizeof(msgLine[3]), "%s", d);
  msgUntil = ms ? millis() + ms : 0xFFFFFFFFUL;      // 0 = stay until something else is shown
  lcdShow(msgLine[0], msgLine[1], msgLine[2], msgLine[3]);
}

void headerLine(char* out, size_t n) {
#if SHOW_DEVICE_ID
  snprintf(out, n, "SoilSense   %s", deviceTag);
#else
  snprintf(out, n, "SoilSense");
#endif
}

void drawIdle() {
  char head[24];
  headerLine(head, sizeof(head));

  if (WiFi.status() != WL_CONNECTED)  lcdShow(head, "Connecting to", "Wi-Fi...", WIFI_SSID);
  else if (!serverOK)                 lcdShow(head, "Can't reach the", "website.", "Retrying...");
  else if (connState == 1)            lcdShow(head, "CONNECTED", "Press SCAN NOW", "on the website");
  else if (connState == 0)            lcdShow(head, "NOT CONNECTED", "Website: Find", "Device > Connect");
  else                                lcdShow(head, "Starting...", "", "");
}

void rowFmt(char* out, size_t n, const char* name, const char* val, const char* lvl) {
  snprintf(out, n, "%-5s%6s %-4s", name, val, statusWord(lvl));
}

void drawResultPage(int page) {
  char l[4][24] = { "", "", "", "" };
  char val[12];

  if (page == 0) {                                   // nutrients
    snprintf(l[0], 24, "-- NPK mg/kg --");
    snprintf(val, sizeof(val), "%.0f", res.v[0]); rowFmt(l[1], 24, "Nitro", val, res.level[0]);
    snprintf(val, sizeof(val), "%.0f", res.v[1]); rowFmt(l[2], 24, "Phos.", val, res.level[1]);
    snprintf(val, sizeof(val), "%.0f", res.v[2]); rowFmt(l[3], 24, "Potas", val, res.level[2]);
  }
  else if (page == 1) {                              // soil condition
    snprintf(l[0], 24, "---- SOIL ----");
    snprintf(val, sizeof(val), "%.1f", res.v[3]);  rowFmt(l[1], 24, "pH", val, res.level[3]);
    snprintf(val, sizeof(val), "%.1fC", res.v[4]); rowFmt(l[2], 24, "Temp", val, res.level[4]);
    if (res.v[5] >= 99.95f) snprintf(val, sizeof(val), "%.0f%%", res.v[5]);
    else                    snprintf(val, sizeof(val), "%.1f%%", res.v[5]);
    rowFmt(l[3], 24, "Moist", val, res.level[5]);
  }
  else {                                             // crop + health
    snprintf(l[0], 24, "-- BEST CROP --");

    char name[24];
    snprintf(name, sizeof(name), "%.16s", res.crop[0] ? res.crop : "-");
    for (char* p = name; *p; p++) if (*p >= 'a' && *p <= 'z') *p -= 32;
    int pad = (LCD_COLS - (int)strlen(name)) / 2;
    if (pad < 0) pad = 0;
    memset(l[1], ' ', pad);
    snprintf(l[1] + pad, 24 - pad, "%s", name);

    if (res.conf >= 0) snprintf(l[2], 24, "Match: %d%%", res.conf);
    if (res.health >= 0) snprintf(l[3], 24, "Health %d/100 %s", (int)(res.health + 0.5f), res.grade);
  }

  lcdShow(l[0], l[1], l[2], l[3]);
}

void startResults() {
  resultsActive = true;
  msgUntil = 0;
  resultsStart = millis();
}

void redrawUi() {
  if (resultsActive) {
    unsigned long n = (millis() - resultsStart) / RESULT_PAGE_MS;
    drawResultPage((int)(n % RESULT_PAGES));
  }
  else if (msgUntil != 0) lcdShow(msgLine[0], msgLine[1], msgLine[2], msgLine[3]);
  else                    drawIdle();
}

void uiTick() {
  static unsigned long lastRepaint = 0;

  if (resultsActive) {
    unsigned long n = (millis() - resultsStart) / RESULT_PAGE_MS;

    if (n >= (unsigned long)(RESULT_PAGES * RESULT_REPEATS)) {     // shown enough times: back to the idle screen
      resultsActive = false;
      lastRepaint = millis();
      return;
    }

    drawResultPage((int)(n % RESULT_PAGES));
    return;
  }

  if (msgUntil != 0 && msgUntil != 0xFFFFFFFFUL && millis() > msgUntil) msgUntil = 0;

  if (msgUntil == 0) drawIdle();                         // only writes lines that changed

  if (millis() - lastRepaint > 20000) {                  // heal any glitch from electrical noise
    lastRepaint = millis();
    lcdRepaintAll();
  }
}

void readSensor() {
  while (Serial2.available()) {
    Serial2.read();
  }

  Serial.println("[SENSOR] Sending request...");

  digitalWrite(RE_DE_PIN, HIGH);
  delay(5);

  Serial2.write(query, sizeof(query));
  Serial2.flush();

  digitalWrite(RE_DE_PIN, LOW);
  delay(5);

  int bytesRead = 0;
  unsigned long start = millis();

  while (millis() - start < 1500) {
    if (Serial2.available()) {
      if (bytesRead < (int)sizeof(response)) {
        response[bytesRead++] = Serial2.read();
      } else {
        Serial2.read();
      }
    }
  }

  Serial.print("[SENSOR] Bytes read: ");
  Serial.println(bytesRead);

  int startIndex = -1;

  for (int i = 0; i < bytesRead - 2; i++) {
    if (
      response[i] == 0x01 &&
      response[i + 1] == 0x03 &&
      response[i + 2] == 0x0E
    ) {
      startIndex = i;
      break;
    }
  }

  if (startIndex >= 0 && bytesRead - startIndex >= 19) {
    uint16_t humidityRaw =
      ((uint16_t)response[startIndex + 3] << 8) |
      response[startIndex + 4];

    uint16_t temperatureRaw =
      ((uint16_t)response[startIndex + 5] << 8) |
      response[startIndex + 6];

    soilConductivity =
      ((uint16_t)response[startIndex + 7] << 8) |
      response[startIndex + 8];

    uint16_t phRaw =
      ((uint16_t)response[startIndex + 9] << 8) |
      response[startIndex + 10];

    nitrogen =
      ((uint16_t)response[startIndex + 11] << 8) |
      response[startIndex + 12];

    phosphorus =
      ((uint16_t)response[startIndex + 13] << 8) |
      response[startIndex + 14];

    potassium =
      ((uint16_t)response[startIndex + 15] << 8) |
      response[startIndex + 16];

    soilHumidity = humidityRaw / 10.0;
    soilTemperature = temperatureRaw / 10.0;
    soilPH = phRaw / 10.0;

    sensorOK = true;
    sensorState = "ok";

    Serial.printf(
      "[SENSOR] OK N=%u P=%u K=%u pH=%.1f T=%.1f H=%.1f EC=%u\n",
      nitrogen, phosphorus, potassium,
      soilPH, soilTemperature, soilHumidity,
      soilConductivity
    );
  }
  else {
    sensorOK = false;
    sensorState = "fail";

    Serial.println(
      "[SENSOR] NO VALID FRAME - check probe, power and RS485 wiring"
    );
  }
}

// ============================================================
// WI-FI
// ============================================================

void wifiBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setTxPower(WIFI_TX_POWER);

  Serial.print("[WIFI] Connecting to ");
  Serial.println(WIFI_SSID);
}

void wifiWatch() {
  bool up = WiFi.status() == WL_CONNECTED;

  if (up && !wifiWasUp) {
    Serial.print("[WIFI] Connected. IP ");
    Serial.print(WiFi.localIP());
    Serial.print(" RSSI ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");

    lastHeartbeat = 0;
    redrawUi();
  }

  if (!up && wifiWasUp) {
    Serial.println("[WIFI] Connection lost, retrying...");
    redrawUi();
  }

  wifiWasUp = up;

  if (!up && millis() - lastWifiRetry > 10000) {
    lastWifiRetry = millis();

    Serial.println(
      "[WIFI] Retrying (check Wi-Fi name/password and 2.4 GHz)"
    );

    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    WiFi.setTxPower(WIFI_TX_POWER);
  }
}

// ============================================================
// DEVICE TOKEN
// ============================================================

void loadOrCreateToken() {
  Preferences prefs;
  prefs.begin("soilsense", false);

  deviceToken = prefs.getString("token", "");

  if (deviceToken.length() != 32) {
    char buf[33];

    for (int i = 0; i < 4; i++) {
      snprintf(
        buf + i * 8, 9,
        "%08lx", (unsigned long)esp_random()
      );
    }

    deviceToken = String(buf);
    prefs.putString("token", deviceToken);

    Serial.println("[ID] Created a new device secret");
  }

  prefs.end();
}

// ============================================================
// HTTP POST
// ============================================================

int httpPost(
  const String &path,
  const char* body,
  String &reply,
  uint32_t timeoutMs
) {
  String url = String(SERVER_BASE) + path;

  inNetwork = true;

  WiFiClientSecure secureClient;
  WiFiClient plainClient;
  HTTPClient http;

  bool began;

  if (url.startsWith("https://")) {
    secureClient.setInsecure();
    began = http.begin(secureClient, url);
  }
  else {
    began = http.begin(plainClient, url);
  }

  if (!began) {
    reply = "http.begin() failed";
    inNetwork = false;
    return -1;
  }

  http.setTimeout(timeoutMs);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", deviceToken);

  if (strlen(API_KEY) > 0) {
    http.addHeader("X-API-Key", API_KEY);
  }

  int code = http.POST(
    (uint8_t*)body,
    strlen(body)
  );

  reply = code > 0
    ? http.getString()
    : http.errorToString(code);

  http.end();

  inNetwork = false;
  return code;
}

// ============================================================
// SIMPLE JSON READER
// ============================================================

String jsonGet(const String &json, const char* key) {
  String k = String("\"") + key + "\"";

  int i = json.indexOf(k);
  if (i < 0) return "";

  i = json.indexOf(':', i + k.length());
  if (i < 0) return "";

  i++;

  while (
    i < (int)json.length() &&
    (json[i] == ' ' || json[i] == '\n')
  ) {
    i++;
  }

  if (i >= (int)json.length()) return "";

  if (json[i] == '"') {
    int e = json.indexOf('"', i + 1);

    return e < 0
      ? String("")
      : json.substring(i + 1, e);
  }

  int e = i;

  while (
    e < (int)json.length() &&
    json[e] != ',' &&
    json[e] != '}' &&
    json[e] != ' ' &&
    json[e] != '\n'
  ) {
    e++;
  }

  return json.substring(i, e);
}

// ============================================================
// WEBSITE HEARTBEAT
// ============================================================

bool sendHeartbeat() {
  lastHeartbeat = millis();

  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  char body[160];

  snprintf(
    body, sizeof(body),
    "{\"rssi\":%d,\"sensor\":\"%s\",\"fw\":\"%s\",\"name\":\"%s\"}",
    (int)WiFi.RSSI(),
    sensorState.c_str(),
    FW_VERSION,
    deviceTag
  );

  String reply;

  int code = httpPost(
    "/api/device/heartbeat",
    body,
    reply,
    6000
  );

  if (code == 200) {
    int before = connState;

    connState =
      jsonGet(reply, "connected") == "true" ? 1 : 0;

    serverOK = true;

    if (
      connState == 1 &&
      jsonGet(reply, "scan") == "true"
    ) {
      webScan = true;
      Serial.println("[LINK] Website Scan now pressed");
    }

    if (before != connState) {
      Serial.println(
        connState == 1
          ? "[LINK] A user connected"
          : "[LINK] Nobody is connected"
      );
    }

    return true;
  }

  serverOK = false;

  Serial.print("[LINK] Heartbeat failed, HTTP ");
  Serial.println(code);

  return false;
}

// ============================================================
// UPLOAD SENSOR READING
// ============================================================

int sendReading(String &reply) {
  char uid[17];

  snprintf(
    uid, sizeof(uid),
    "%08lx%08lx",
    (unsigned long)esp_random(),
    (unsigned long)esp_random()
  );

  char body[320];

  snprintf(
    body, sizeof(body),
    "{\"N\":%u,\"P\":%u,\"K\":%u,\"ph\":%.1f,\"temp\":%.1f,\"humidity\":%.1f,\"scan_uid\":\"%s\"}",
    (unsigned)nitrogen,
    (unsigned)phosphorus,
    (unsigned)potassium,
    soilPH,
    soilTemperature,
    soilHumidity,
    uid
  );

  int code = httpPost(
    "/api/esp32",
    body,
    reply,
    20000
  );

  if (code <= 0) {
    Serial.println("[UPLOAD] Network problem - retrying once");
    delay(800);

    code = httpPost(
      "/api/esp32",
      body,
      reply,
      20000
    );
  }

  Serial.print("[UPLOAD] HTTP ");
  Serial.println(code);

  Serial.println(reply.length() > 200 ? reply.substring(0, 200) + "..." : reply);

  return code;
}


// ============================================================
// SCAN
// ============================================================

void parseResult(const String &reply) {
  res.have = false;

  String crop = jsonGet(reply, "crop");
  if (crop.length() == 0) return;                // e.g. a re-sent scan: the website answered without details

  snprintf(res.crop, sizeof(res.crop), "%s", crop.c_str());

  String conf = jsonGet(reply, "crop_conf");
  res.conf = (conf.length() && conf != "null") ? (int)(conf.toFloat() + 0.5f) : -1;

  String hs = jsonGet(reply, "fertility_score");
  res.health = hs.length() ? hs.toFloat() : -1;

  snprintf(res.grade, sizeof(res.grade), "%s", jsonGet(reply, "grade").c_str());

  static const char* keys[6] = { "lvl_N", "lvl_P", "lvl_K", "lvl_ph", "lvl_temp", "lvl_humidity" };
  for (int i = 0; i < 6; i++) snprintf(res.level[i], sizeof(res.level[i]), "%s", jsonGet(reply, keys[i]).c_str());

  res.have = true;
}

void doScan() {
  Serial.println("[SCAN] Scan requested");

  if (WiFi.status() != WL_CONNECTED) {
    showMsg("NO WI-FI", "Check the network", "", "", 4000);
    return;
  }

  if (connState != 1) {
    showMsg("Checking the", "website...");

    sendHeartbeat();

    if (!serverOK) {
      showMsg("CAN'T REACH THE", "WEBSITE", "Try again", "", 4000);
      return;
    }

    if (connState != 1) {
      showMsg("NOT CONNECTED", "Website: Find", "Device > Connect", "then Scan now", 5000);
      return;
    }
  }

  showMsg("SCANNING...", "Keep the probe", "in the soil");

  readSensor();

  if (!sensorOK) {
    showMsg("NO SENSOR", "Check the probe", "cable", "", 5000);
    return;
  }

  showMsg("SENDING...", "Please wait");

  String reply;
  int code = sendReading(reply);

  if (code == 200) {
    connState = 1;
    serverOK = true;

    // what the probe measured (same numbers the website saved, shown with 1 decimal)
    res.v[0] = nitrogen; res.v[1] = phosphorus; res.v[2] = potassium;
    res.v[3] = soilPH;   res.v[4] = soilTemperature; res.v[5] = soilHumidity;

    parseResult(reply);

    if (res.have) startResults();
    else          showMsg("SENT", "Look at the", "website", "", 5000);
  }
  else if (code == 403) {
    connState = 0;
    serverOK = true;
    Serial.println("[LINK] Nobody connected - reading NOT saved");
    showMsg("NOT CONNECTED", "Website: Find", "Device > Connect", "then Scan now", 5000);
  }
  else if (code == 401) {
    showMsg("BAD API KEY", "Check API_KEY", "in the sketch", "", 5000);
  }
  else {
    serverOK = code > 0;
    showMsg("SEND FAILED", "Press Scan now", "again", "", 5000);
  }
}


// ============================================================
// SERIAL MONITOR COMMANDS  (115200 baud)
//   tap | t | scan | s | 1   take a reading, same as Scan now on the website
//   again | r                show the last result on the LCD again
//   status | ?               Wi-Fi / website / sensor / LCD state
//   lcd                      look for the LCD again (after fixing wires)
// ============================================================

void printHelp() {
  Serial.println();
  Serial.println("---- SoilSense commands ----");
  Serial.println("tap | t | scan | s | 1 : take a reading");
  Serial.println("again | r              : show the last result again");
  Serial.println("status | ?             : status");
  Serial.println("lcd                    : search for the LCD again");
  Serial.println("help | h               : this list");
  Serial.println("----------------------------");
}

void printStatus() {
  Serial.println();
  Serial.printf("[STATUS] Device ID: %s  firmware %s\n", deviceTag, FW_VERSION);
  Serial.printf("[STATUS] Wi-Fi: %s", WiFi.status() == WL_CONNECTED ? "connected" : "NOT connected");
  if (WiFi.status() == WL_CONNECTED) Serial.printf(" (%s, %d dBm)", WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
  Serial.println();
  Serial.printf("[STATUS] Website: %s\n", !serverOK ? "cannot reach it"
                                       : connState == 1 ? "a user is connected"
                                       : connState == 0 ? "nobody connected"
                                       : "not checked yet");
  Serial.printf("[STATUS] Sensor: %s\n", sensorState.c_str());
  Serial.printf("[STATUS] LCD: %s", lcdOK ? "found" : "NOT found");
  if (lcdOK) Serial.printf(" at 0x%02X", lcdAddr);
  Serial.println();
  Serial.printf("[STATUS] Free heap: %u bytes\n", (unsigned)ESP.getFreeHeap());
}

void handleSerialCommand(String cmd) {
  cmd.trim();
  cmd.toLowerCase();

  if (cmd.length() == 0) return;

  if (cmd == "tap" || cmd == "t" || cmd == "scan" || cmd == "s" || cmd == "1") {
    Serial.println("[SERIAL] Scan command received");
    serialTap = true;
  }
  else if (cmd == "again" || cmd == "r") {
    if (res.have) startResults(); else Serial.println("[SERIAL] No result yet. Take a reading first.");
  }
  else if (cmd == "status" || cmd == "?") printStatus();
  else if (cmd == "lcd") { lcdBegin(); redrawUi(); }
  else if (cmd == "help" || cmd == "h") printHelp();
  else {
    Serial.print("[SERIAL] Unknown command: ");
    Serial.println(cmd);
  }
}

void serialTick() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    serialLastChar = millis();

    if (c == '\n' || c == '\r') {
      handleSerialCommand(serialBuf);
      serialBuf = "";
    }
    else if (serialBuf.length() < 24) {
      serialBuf += c;
    }
  }

  if (serialBuf.length() > 0 && millis() - serialLastChar > 250) {
    handleSerialCommand(serialBuf);
    serialBuf = "";
  }
}

// ============================================================
// SETUP / LOOP
// ============================================================

void setup() {
  Serial.begin(115200);
  delay(500);

  snprintf(deviceTag, sizeof(deviceTag), "%04X", (unsigned)((uint16_t)(ESP.getEfuseMac() >> 32)));

  Serial.println();
  Serial.print("[BOOT] Restart reason: ");
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  Serial.println("power on"); break;
    case ESP_RST_EXT:      Serial.println("reset button"); break;
    case ESP_RST_SW:       Serial.println("software restart"); break;
    case ESP_RST_PANIC:    Serial.println("CRASH"); break;
    case ESP_RST_BROWNOUT: Serial.println("BROWNOUT - check the power supply"); break;
    default:               Serial.println("other"); break;
  }

  // LCD first, so there is something to read from the first second
  lcdBegin();
  showMsg("SoilSense", "Starting...", deviceTag, FW_VERSION);

  // RS485 soil probe
  Serial2.begin(4800, SERIAL_8N1, RX2_PIN, TX2_PIN);
  pinMode(RE_DE_PIN, OUTPUT);
  digitalWrite(RE_DE_PIN, LOW);

#if USE_BOOT_BUTTON
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
#endif

  loadOrCreateToken();
  delay(800);
  wifiBegin();

  Serial.println();
  Serial.println("================================");
  Serial.println("SOILSENSE SCANNER - 16x4 LCD");
  Serial.printf("Device ID: SoilSense Scanner %s\n", deviceTag);
  Serial.println("Press Scan now on the website, or type  tap  here");
  Serial.println("================================");

  msgUntil = 0;
}

void loop() {
  wifiWatch();
  serialTick();

  unsigned long beatEvery = (connState == 1) ? HEARTBEAT_CONNECTED_MS : HEARTBEAT_MS;

  if (WiFi.status() == WL_CONNECTED && (lastHeartbeat == 0 || millis() - lastHeartbeat >= beatEvery)) {
    sendHeartbeat();
  }

  bool tap = false;

#if USE_BOOT_BUTTON
  if (digitalRead(BOOT_BUTTON_PIN) == LOW) tap = true;
#endif

  if (serialTap) { serialTap = false; tap = true; }

  if (webScan) {                       // Scan now was pressed on the website
    webScan = false;
    Serial.println("[SCAN] Website request received");
    doScan();
    webScan = false;                   // ignore a second request that arrived during this scan
    lastTapMs = millis();
  }
  else if (tap && millis() - lastTapMs > 1500) {
    lastTapMs = millis();
    doScan();
  }

  uiTick();

  delay(20);
}
