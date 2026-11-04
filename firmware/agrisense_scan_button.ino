// ============================================================
// AGRISENSE SCANNER  -  ESP32-S3 + 20x4 I2C LCD (HD44780 + PCF8574 backpack) + RS485 soil probe
//
// No touchscreen, no buttons. Everything is done from the website:
//   1. The scanner joins Wi-Fi and checks in with the website every few seconds, so the website
//      FINDS it:  Dashboard > Find device > Connect.
//   2. Press SCAN NOW on the website. The scanner reads the probe and sends the reading.
//   3. The website saves it and answers with the SAME numbers the website popup shows (levels for
//      N, P, K, pH, temperature, moisture, the soil health score and grade).
//      The LCD shows exactly that answer, so the LCD and the website always agree.
//   The result stays on the LCD for 1 minute (RESULT_SHOW_MS), then the LCD goes back to "Scan now".
//
// OFFLINE MODE (no Wi-Fi needed):
//   If Wi-Fi is not connected for OFFLINE_WIFI_WAIT_MS the scanner switches to offline mode by itself.
//   You then have OFFLINE_WINDOW_MS (1 minute) to push the probe into the soil; if you don't, offline mode
//   times out ("OFFLINE TIMEOUT"). Press the board's BOOT button to start the 1-minute window again.
//   It keeps reading the probe. While N, P, K and moisture all read 0 the probe is NOT in soil.
//   The moment any of them becomes non-zero the probe is in the soil: the sketch waits OFFLINE_SETTLE_MS
//   for the numbers to settle, reads again, and works everything out ON the ESP32 (levels and the soil health
//   score) and shows the result on the LCD (see below).
//   Pull the probe out of the soil to scan the next spot.
//
// RESULT SCREEN (online and offline): full names, 2 readings per page, pages change every RESULT_PAGE_MS.
//   The whole result stays on the LCD for 1 minute, then the LCD goes back to "Scan now".
//   Page 1: Nitrogen (N)      Page 2: Potassium (K)     Page 3: pH            Page 4: Soil Health Score
//           Phosphorus (P)            Temperature               Moisture
//   Offline mode switches itself off as soon as Wi-Fi connects. Offline results are NOT saved to the website.
//
// Serial Monitor is output only: it prints what happens.
//
// PINS (ESP32-S3)
//   LCD  SDA -> GPIO 8     LCD  SCL -> GPIO 9    LCD VCC -> 5V, GND -> GND
//   RS485 module RO -> GPIO 16 (ESP RX)   DI -> GPIO 17 (ESP TX)   DE+RE (tied together) -> GPIO 4
//   RS485 module VCC/GND -> 3V3/GND   A/B -> probe A/B   probe power wires -> 12-24 V supply (common GND)
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
#define LCD_SDA   3
#define LCD_SCL   9

// ---- OPTIONAL TUNING -------------------------------------------------------

const unsigned long HEARTBEAT_MS           = 8000;   // how often we tell the website "I'm here"
const unsigned long HEARTBEAT_CONNECTED_MS = 3000;   // faster while someone is connected (website button reacts quicker)

#define RESULT_SHOW_MS      60000    // the result stays on the LCD this long (1 minute), then the screen goes back to "Scan now"
#define RESULT_PAGES        4        // Nitrogen+Phosphorus, Potassium+Temperature, pH+Moisture, Soil Health Score
#define RESULT_PAGE_MS      3500     // how long each result page stays on the LCD

// ---- OFFLINE MODE ----
#define OFFLINE_ENABLED     1
const unsigned long OFFLINE_WIFI_WAIT_MS = 15000;   // Wi-Fi missing this long -> switch to offline mode
const unsigned long OFFLINE_WINDOW_MS    = 60000;   // 1 minute to push the probe into the soil, then offline mode times out
const unsigned long OFFLINE_SETTLE_MS    = 3000;    // soil first seen -> wait this long, then take the real reading
const unsigned long OFFLINE_POLL_MS      = 700;     // how often the probe is checked while waiting

#define USE_BOOT_BUTTON     1        // 1 = the board's BOOT button (GPIO0) restarts the 1-minute offline window
#define BOOT_BUTTON_PIN     0
#define SHOW_DEVICE_ID      1

#define FW_VERSION "3.6"

const wifi_power_t WIFI_TX_POWER = WIFI_POWER_11dBm;   // gentler Wi-Fi power bursts (raise to WIFI_POWER_15dBm only if Wi-Fi will not connect)

// ---- PINS --------------------------------------------------------------------
// RS485 probe: RX 16, TX 17, DE/RE 4   (if your probe is wired to GPIO 15, change RX2_PIN to 15)
#define RX2_PIN    15

#define TX2_PIN    17
#define RE_DE_PIN  4

#define LCD_COLS   20
#define LCD_ROWS   4

// ============================================================
// SENSOR DATA
// ============================================================

float soilMoisture = 0;          // soil moisture %
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
bool wifiWasUp = false;

bool webScan = false;         // website "Scan now" arrived with a check-in

// offline mode state
enum OffState : uint8_t { OFF_NONE, OFF_WAIT, OFF_SETTLE, OFF_REMOVE };
OffState      offState = OFF_NONE;       // NONE = online behaviour
bool          offDeclined = false;       // the 1-minute window ran out
unsigned long offDeadline = 0, offSettleStart = 0, offNextPoll = 0, wifiDownSince = 0;
unsigned long lastTapMs = 0;
int           offPolls = 0;
bool          sensorQuiet = false;       // true = readSensor() prints nothing (used while polling)

volatile bool inNetwork = false;   // (kept so the HTTP helper below is unchanged)

void redrawUi();


// ============================================================
// 20x4 I2C LCD  (HD44780 controller behind a PCF8574 "backpack")  -  tiny built-in driver
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

// Show 4 lines of up to 20 characters. Only lines that changed are rewritten (no flicker).
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
  float health;               // soil health 0-100
  char  grade[4];
  char  level[6][10];         // optimal / watch / low / high   for N, P, K, pH, temp, moisture
  float v[6];                 // N, P, K, pH, temp, moisture as measured
};
ScanResult res = { false, 0, "", { "", "", "", "", "", "" }, { 0, 0, 0, 0, 0, 0 } };

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
  snprintf(out, n, "AgriSense   %s", deviceTag);
#else
  snprintf(out, n, "AgriSense");
#endif
}

#if OFFLINE_ENABLED
void drawOffline() {
  const char* probeLine = (offPolls > 0 && !sensorOK) ? "Check RS485 wire" : "Push probe in soil";

  if (offState == OFF_WAIT) {
    long left = ((long)(offDeadline - millis()) + 999) / 1000;
    if (left < 0) left = 0;
    char l3[24];
    snprintf(l3, sizeof(l3), "Time left: %lds", left);
    lcdShow("OFFLINE MODE", "No Wi-Fi", probeLine, l3);
  }
  else if (offState == OFF_SETTLE) lcdShow("SOIL DETECTED", "Hold the probe", "still...", "");
  else                             lcdShow("OFFLINE MODE", "Scan done.", "Remove probe", "to scan again");
}
#endif

void drawIdle() {
  char head[24];
  headerLine(head, sizeof(head));

#if OFFLINE_ENABLED
  if (offState != OFF_NONE) { drawOffline(); return; }
  if (offDeclined && WiFi.status() != WL_CONNECTED) { lcdShow(head, "Wi-Fi not found", "Press BOOT for", "offline mode"); return; }
#endif

  if (WiFi.status() != WL_CONNECTED)  lcdShow(head, "Connecting to", "Wi-Fi...", WIFI_SSID);
  else if (!serverOK)                 lcdShow(head, "Can't reach the", "website.", "Retrying...");
  else if (connState == 1)            lcdShow(head, "CONNECTED", "Press SCAN NOW", "on the website");
  else if (connState == 0)            lcdShow(head, "NOT CONNECTED", "Website: Find", "Device > Connect");
  else                                lcdShow(head, "Starting...", "", "");
}

// Result screen (20x4). Full names, 2 readings per page (name on one line, value on the next):
//   Nitrogen (N)        Potassium (K)       pH                  Soil Health Score
//     120 mg/kg           90 mg/kg            6.5                 83 / 100
//   Phosphorus (P)      Temperature         Moisture              Grade B
//     60 mg/kg            25.5 C              60.0 %
void putReading(char l[4][24], int row, const char* name, const char* value) {
  snprintf(l[row],     24, "%s", name);
  snprintf(l[row + 1], 24, "  %s", value);
}

void drawResultPage() {
  char l[4][24] = { "", "", "", "" };
  char v[16];
  int page = (int)(((millis() - resultsStart) / RESULT_PAGE_MS) % RESULT_PAGES);

  if (page == 0) {
    snprintf(v, sizeof(v), "%.0f mg/kg", res.v[0]); putReading(l, 0, "Nitrogen (N)", v);
    snprintf(v, sizeof(v), "%.0f mg/kg", res.v[1]); putReading(l, 2, "Phosphorus (P)", v);
  }
  else if (page == 1) {
    snprintf(v, sizeof(v), "%.0f mg/kg", res.v[2]); putReading(l, 0, "Potassium (K)", v);
    snprintf(v, sizeof(v), "%.1f C", res.v[4]);     putReading(l, 2, "Temperature", v);
  }
  else if (page == 2) {
    snprintf(v, sizeof(v), "%.1f", res.v[3]);       putReading(l, 0, "pH", v);
    if (res.v[5] >= 99.95f) snprintf(v, sizeof(v), "%.0f %%", res.v[5]);
    else                    snprintf(v, sizeof(v), "%.1f %%", res.v[5]);
    putReading(l, 2, "Moisture", v);
  }
  else {
    snprintf(l[0], 24, "Soil Health Score");
    if (res.health >= 0) {
      snprintf(l[1], 24, "  %d / 100", (int)(res.health + 0.5f));
      if (res.grade[0]) snprintf(l[2], 24, "  Grade %s", res.grade);
    }
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
    drawResultPage();
  }
  else if (msgUntil != 0) lcdShow(msgLine[0], msgLine[1], msgLine[2], msgLine[3]);
  else                    drawIdle();
}

void uiTick() {
  static unsigned long lastRepaint = 0;

  if (resultsActive) {
    if (millis() - resultsStart >= RESULT_SHOW_MS) {               // 1 minute is over: back to the "Scan now" screen
      resultsActive = false;
      lastRepaint = millis();
      return;
    }

    drawResultPage();
    return;
  }

  if (msgUntil != 0 && msgUntil != 0xFFFFFFFFUL && millis() > msgUntil) msgUntil = 0;

  if (msgUntil == 0) drawIdle();                         // only writes lines that changed

  if (millis() - lastRepaint > 20000) {                  // heal any glitch from electrical noise
    lastRepaint = millis();
    lcdRepaintAll();
  }
}

// ============================================================
// RS485 / NPK SENSOR
// ============================================================

// Modbus RTU CRC-16 (polynomial 0xA001; transmitted low byte first).
uint16_t modbusCRC16(const uint8_t* data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      if (crc & 1) crc = (crc >> 1) ^ 0xA001;
      else         crc >>= 1;
    }
  }
  return crc;
}

void readSensor() {
  while (Serial2.available()) {
    Serial2.read();
  }

  if (!sensorQuiet) Serial.println("[SENSOR] Sending request...");

  digitalWrite(RE_DE_PIN, HIGH);
  delay(5);

  Serial2.write(query, sizeof(query));
  Serial2.flush();

  digitalWrite(RE_DE_PIN, LOW);
  delay(5);

  int bytesRead = 0;
  unsigned long start = millis();

  unsigned long lastByte = 0;

  while (millis() - start < 1500) {
    if (Serial2.available()) {
      lastByte = millis();
      if (bytesRead < (int)sizeof(response)) {
        response[bytesRead++] = Serial2.read();
      } else {
        Serial2.read();
      }
    }
    else if (bytesRead >= 19 && millis() - lastByte > 30) break;   // full frame arrived, no need to wait the full 1.5 s
  }

  if (!sensorQuiet) Serial.print("[SENSOR] Bytes read: ");
  if (!sensorQuiet) Serial.println(bytesRead);

  int startIndex = -1;
  bool sawException = false;
  bool sawBadCRC = false;

  // Find a response for slave 1. Normal response: 01 03 0E + 14 data bytes + CRC.
  // Exception response: 01 83 <exception-code> + CRC.
  for (int i = 0; i + 4 < bytesRead; i++) {
    if (response[i] != 0x01) continue;

    if (response[i + 1] == 0x83) {
      if (i + 5 <= bytesRead) {
        uint16_t expected = modbusCRC16(&response[i], 3);
        uint16_t received = (uint16_t)response[i + 3] |
                            ((uint16_t)response[i + 4] << 8);
        if (expected == received) sawException = true;
      }
      continue;
    }

    if (response[i + 1] != 0x03 || response[i + 2] != 0x0E) continue;
    if (i + 19 > bytesRead) continue;

    uint16_t expected = modbusCRC16(&response[i], 17); // address through last data byte
    uint16_t received = (uint16_t)response[i + 17] |
                        ((uint16_t)response[i + 18] << 8);

    if (expected != received) {
      sawBadCRC = true;
      continue;
    }

    startIndex = i;
    break;
  }

  if (startIndex >= 0) {
    uint16_t moistureRaw =
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

    soilMoisture = moistureRaw / 10.0f;
    soilTemperature = (int16_t)temperatureRaw / 10.0f;
    soilPH = phRaw / 10.0f;

    sensorOK = true;
    sensorState = "ok";

    if (!sensorQuiet) Serial.printf(
      "[SENSOR] OK N=%u P=%u K=%u pH=%.1f T=%.1f M=%.1f EC=%u (CRC OK)\n",
      nitrogen, phosphorus, potassium,
      soilPH, soilTemperature, soilMoisture, soilConductivity
    );
  } else {
    sensorOK = false;
    sensorState = "fail";

    if (!sensorQuiet) Serial.print("[SENSOR] Invalid/no response. Bytes=");
    if (!sensorQuiet) Serial.println(bytesRead);

    if (bytesRead > 0) {
      if (!sensorQuiet) Serial.print("[SENSOR] RX HEX:");
      for (int i = 0; i < bytesRead; i++) {
        if (!sensorQuiet) Serial.printf(" %02X", response[i]);
      }
      if (!sensorQuiet) Serial.println();
    }

    if (sawException) {
      if (!sensorQuiet) Serial.println("[SENSOR] Probe returned Modbus exception (check function/register address).");
    } else if (sawBadCRC) {
      if (!sensorQuiet) Serial.println("[SENSOR] Response found but CRC failed (noise, baud, wiring, or power).");
    } else if (bytesRead == 0) {
      if (!sensorQuiet) Serial.println("[SENSOR] No bytes received (check power, A/B polarity, baud, slave ID, and DE/RE).");
    } else {
      if (!sensorQuiet) Serial.println("[SENSOR] Bytes received but no valid 01 03 0E frame; check protocol/register map.");
    }
  }
}

// ============================================================
// OFFLINE MODE  (no Wi-Fi, no website: everything is worked out here on the ESP32-S3)
// ============================================================

// true when the probe is really in soil: N, P, K and moisture are NOT all 0
bool soilPresent() {
  return sensorOK && (nitrogen > 0 || phosphorus > 0 || potassium > 0 || soilMoisture > 0.05f);
}

// same ranges as the website (app.py READING_RANGES): optimal / watch / low / high
const char* offLevel(float v, float okLo, float okHi, float wLo, float wHi) {
  if (v >= okLo && v <= okHi) return "optimal";
  if ((v >= wLo && v < okLo) || (v > okHi && v <= wHi)) return "watch";
  if (v < wLo) return "low";
  return "high";
}

void computeOfflineResult() {
  res.v[0] = nitrogen; res.v[1] = phosphorus; res.v[2] = potassium;
  res.v[3] = soilPH;   res.v[4] = soilTemperature; res.v[5] = soilMoisture;

  snprintf(res.level[0], sizeof(res.level[0]), "%s", offLevel(res.v[0], 40, 120, 25, 130));     // N
  snprintf(res.level[1], sizeof(res.level[1]), "%s", offLevel(res.v[1], 40, 120, 25, 130));     // P
  snprintf(res.level[2], sizeof(res.level[2]), "%s", offLevel(res.v[2], 40, 120, 25, 150));     // K
  snprintf(res.level[3], sizeof(res.level[3]), "%s", offLevel(res.v[3], 6.0, 7.0, 5.5, 7.5));   // pH
  snprintf(res.level[4], sizeof(res.level[4]), "%s", offLevel(res.v[4], 20, 35, 15, 40));       // temp
  snprintf(res.level[5], sizeof(res.level[5]), "%s", offLevel(res.v[5], 50, 90, 40, 95));       // moisture

  // soil health score: same rule as the website (calculate_fertility)
  int score = 0;
  if (nitrogen >= 40 && nitrogen <= 120)               score++;
  if (phosphorus >= 40 && phosphorus <= 120)           score++;
  if (potassium >= 40 && potassium <= 120)             score++;
  if (soilPH >= 5.5f && soilPH <= 7.5f)                score++;
  if (soilTemperature >= 20 && soilTemperature <= 35)  score++;
  if (soilMoisture >= 50 && soilMoisture <= 90)        score++;
  res.health = (score / 6.0f) * 100.0f;
  snprintf(res.grade, sizeof(res.grade), "%s", res.health >= 85 ? "A" : res.health >= 65 ? "B" : res.health >= 45 ? "C" : "D");

  res.have = true;
  Serial.printf("[OFFLINE] Result: health %.0f %s\n", res.health, res.grade);
}

void offlineStart() {                                    // start (or restart) the 1-minute window
  offState = OFF_WAIT;
  offDeadline = millis() + OFFLINE_WINDOW_MS;
  offNextPoll = 0;
  offPolls = 0;
  offDeclined = false;
  resultsActive = false;
  msgUntil = 0;
  Serial.println("[OFFLINE] No Wi-Fi - push the probe into the soil within 1 minute");
}

void offlineButton() {                                   // BOOT button while Wi-Fi is down
  if (offState == OFF_NONE)      offlineStart();
  else if (offState == OFF_WAIT) offDeadline = millis() + OFFLINE_WINDOW_MS;   // give another full minute
}

void offlineTakeReading() {
  sensorQuiet = false;
  readSensor();
  if (soilPresent()) {
    computeOfflineResult();
    offState = OFF_REMOVE;                 // next scan only after the probe is pulled out
    startResults();
  } else {
    Serial.println("[OFFLINE] Soil lost while settling - waiting again");
    offState = OFF_WAIT;
    offDeadline = millis() + OFFLINE_WINDOW_MS;
  }
}

void offlineTick() {
#if OFFLINE_ENABLED
  unsigned long now = millis();

  if (WiFi.status() == WL_CONNECTED) {                   // offline mode only exists while there is no Wi-Fi
    wifiDownSince = 0;
    offDeclined = false;
    if (offState != OFF_NONE) {
      offState = OFF_NONE;
      Serial.println("[OFFLINE] Wi-Fi connected - back to online mode");
      showMsg("Wi-Fi connected", "Back online", "", "", 3000);
    }
    return;
  }

  if (wifiDownSince == 0) wifiDownSince = now;

  if (offState == OFF_NONE) {                            // Wi-Fi has been missing long enough: go offline by itself
    if (!offDeclined && !resultsActive && msgUntil == 0 && now - wifiDownSince >= OFFLINE_WIFI_WAIT_MS) offlineStart();
    return;
  }

  if (offState == OFF_WAIT && (long)(now - offDeadline) >= 0) {      // 1 minute is over
    offState = OFF_NONE;
    offDeclined = true;
    Serial.println("[OFFLINE] Timed out - press BOOT to try again");
    showMsg("OFFLINE TIMEOUT", "No soil found", "Press BOOT to", "try again", 4000);
    return;
  }

  if (resultsActive || msgUntil != 0) return;            // a result or message is on the LCD

  if (offState == OFF_SETTLE) {
    if (now - offSettleStart >= OFFLINE_SETTLE_MS) offlineTakeReading();
    return;
  }

  if (now < offNextPoll) return;
  sensorQuiet = true;
  readSensor();
  sensorQuiet = false;
  offNextPoll = millis() + OFFLINE_POLL_MS;
  offPolls++;
  bool soil = soilPresent();

  if (offState == OFF_REMOVE) {                          // wait until the probe is out of the soil
    if (!soil) {
      offState = OFF_WAIT;
      offDeadline = millis() + OFFLINE_WINDOW_MS;        // a fresh minute for the next spot
      Serial.println("[OFFLINE] Probe removed - ready for the next scan");
    }
    return;
  }

  if (soil) {                                            // WAIT: soil detected -> settle, then scan
    Serial.printf("[OFFLINE] Soil detected (N=%u P=%u K=%u M=%.1f)\n", nitrogen, phosphorus, potassium, soilMoisture);
    offState = OFF_SETTLE;
    offSettleStart = millis();
  }
#endif
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
  prefs.begin("agrisense", false);

  deviceToken = prefs.getString("token", "");

  if (deviceToken.length() != 32) {
    // Scanner was flashed with the old SoilSense sketch: keep its identity instead of registering as a new device.
    Preferences old;
    old.begin("soilsense", true);
    String oldToken = old.getString("token", "");
    old.end();
    if (oldToken.length() == 32) {
      deviceToken = oldToken;
      prefs.putString("token", deviceToken);
      Serial.println("[ID] Kept the device secret from the old SoilSense sketch");
    }
  }

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

  // NOTE: "humidity" is only the field name the website expects; the value is the soil MOISTURE %.
  char body[320];

  snprintf(
    body, sizeof(body),
    "{\"N\":%u,\"P\":%u,\"K\":%u,\"ph\":%.1f,\"temp\":%.1f,\"humidity\":%.1f,\"scan_uid\":\"%s\"}",
    (unsigned)nitrogen,
    (unsigned)phosphorus,
    (unsigned)potassium,
    soilPH,
    soilTemperature,
    soilMoisture,
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

  String hs = jsonGet(reply, "fertility_score");
  if (hs.length() == 0) return;                  // e.g. a re-sent scan: the website answered without details
  res.health = hs.toFloat();

  snprintf(res.grade, sizeof(res.grade), "%s", jsonGet(reply, "grade").c_str());

  // (the website still names the moisture field "humidity" internally, so these key names stay)
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
    res.v[3] = soilPH;   res.v[4] = soilTemperature; res.v[5] = soilMoisture;

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
  showMsg("AgriSense", "Starting...", deviceTag, FW_VERSION);

#if USE_BOOT_BUTTON
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
#endif

  // RS485 soil probe
  Serial2.begin(4800, SERIAL_8N1, RX2_PIN, TX2_PIN);
  pinMode(RE_DE_PIN, OUTPUT);
  digitalWrite(RE_DE_PIN, LOW);

  loadOrCreateToken();
  delay(800);
  wifiBegin();

  Serial.println();
  Serial.println("================================");
  Serial.println("AGRISENSE SCANNER - 20x4 LCD");
  Serial.printf("Device ID: AgriSense Scanner %s\n", deviceTag);
  Serial.println("Probe: 4800 baud, ID 1, function 03, 7 registers");
  Serial.println("Website: Find device > Connect, then press Scan now");
  Serial.println("No Wi-Fi? After 15 s offline mode starts by itself: push the probe into the soil within 1 minute");
  Serial.println("================================");

  msgUntil = 0;
}

void loop() {
  wifiWatch();

  unsigned long beatEvery = (connState == 1) ? HEARTBEAT_CONNECTED_MS : HEARTBEAT_MS;

  if (WiFi.status() == WL_CONNECTED && (lastHeartbeat == 0 || millis() - lastHeartbeat >= beatEvery)) {
    sendHeartbeat();
  }

  if (webScan) {                       // Scan now was pressed on the website
    webScan = false;
    Serial.println("[SCAN] Website request received");
    doScan();
    webScan = false;                   // ignore a second request that arrived during this scan
  }

#if USE_BOOT_BUTTON && OFFLINE_ENABLED
  if (digitalRead(BOOT_BUTTON_PIN) == LOW && millis() - lastTapMs > 1500) {
    lastTapMs = millis();
    if (WiFi.status() != WL_CONNECTED) offlineButton();
  }
#endif

  offlineTick();
  uiTick();

  delay(20);
}
