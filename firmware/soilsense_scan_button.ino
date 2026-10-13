#include <SPI.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <esp_system.h>
#include <Preferences.h>

// ============================================================
// SOILSENSE SCANNER  -  ESP32-S3 + ILI9341 + FT6336 + RS485 7-IN-1 SOIL SENSOR
//
// THE WHOLE SCREEN IS ONE BUTTON:  "SCAN NOW".
//
//  * The scanner joins Wi-Fi and "checks in" with the website every few seconds.
//    That is how the website FINDS it: Dashboard > Find device > Connect.
//    No codes, nothing to type on the device.
//  * Only ONE person can be connected at a time. The website refuses a second
//    person while the scanner is in use. The person (or an admin) disconnects
//    from the website to free it.
//  * Touch anywhere = take one reading and send it to whoever is connected.
//    If nobody is connected the screen says so instead of sending.
//  * SERIAL MONITOR TAP: with the LCD/touch broken (or just for testing) open the Serial
//    Monitor at 115200 baud and send  tap  (or  t  /  scan  /  s)  - it does exactly what
//    touching the screen does. Type  help  for the list. Everything the screen would show
//    is also printed as a  [SCREEN] ...  line.
//
// Screen colours:
//   green        connected, ready          grey   not connected yet
//   blue         scanning / sending        amber  needs your attention
//   red          error (no sensor, no Wi-Fi, server)
// ============================================================


// ---- EDIT THESE ------------------------------------------------------------

const char* WIFI_SSID     = "akoaypogi";        // 2.4 GHz network only
const char* WIFI_PASSWORD = "12345678";

// Your website's address (must start with https://)
const char* SERVER_BASE   = "https://soilsense-amber.vercel.app";

// Leave "" unless you set ESP32_API_KEY on Vercel (then use the same value)
const char* API_KEY       = "";

// The probe does not measure rainfall; the website needs a number.
const float ASSUMED_RAINFALL_MM = 100.0;

// ---- OPTIONAL TUNING -------------------------------------------------------

const unsigned long HEARTBEAT_MS = 8000;   // how often we tell the website "I'm here"
                                           // (website treats us as offline after 25 s)
#define SHOW_DEVICE_ID 1                   // tiny "ID A1B2" in the corner so you can tell
                                           // scanners apart on the website. 0 = hide
const unsigned long LCD_BLIND_REFRESH_MS = 60000;  // see "LCD" notes below. 0 = off
const uint32_t      TFT_SPI_HZ  = 10000000;        // slower SPI = fewer glitches on jumper wires
const wifi_power_t  WIFI_TX_POWER = WIFI_POWER_11dBm;  // smaller current bursts. Raise to
                                                       // WIFI_POWER_15dBm only if Wi-Fi won't connect

#define FW_VERSION "2.1"

// ============================================================
// PINS
// ============================================================
// TFT:   CS 10, RST 9, DC 8, MOSI 11, SCK 12, MISO 13, LED 7
// TOUCH: SDA 3, SCL 6, RST 5, INT 2
// RS485: RX 16, TX 17, DE/RE 4

#define RX2_PIN    16
#define TX2_PIN    17
#define RE_DE_PIN  4

#define TFT_CS     10
#define TFT_RST    9
#define TFT_DC     8
#define TFT_MOSI   11
#define TFT_SCK    12
#define TFT_MISO   13
#define TFT_LED    7

#define CTP_SDA    3
#define CTP_SCL    6
#define CTP_RST    5
#define CTP_INT    2
#define FT6336_ADDR 0x38

#define SCREEN_W 320
#define SCREEN_H 240

Adafruit_ILI9341 tft(TFT_CS, TFT_DC, TFT_RST);


// ============================================================
// COLOURS  (RGB565)
// ============================================================

#define C565(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))

const uint16_t COL_WHITE     = C565(255, 255, 255);
const uint16_t BG_READY      = C565( 24, 140,  72);   // connected, ready
const uint16_t BG_NOCONN     = C565( 62,  72,  66);   // not connected yet
const uint16_t BG_BUSY       = C565( 30,  90, 170);   // scanning / sending
const uint16_t BG_OK         = C565( 18, 168,  88);   // sent
const uint16_t BG_WARN       = C565(196, 118,  18);   // needs attention
const uint16_t BG_ERROR      = C565(176,  44,  44);   // error


// ============================================================
// STATE
// ============================================================

float    soilHumidity = 0, soilTemperature = 0, soilPH = 0;
uint16_t soilConductivity = 0, nitrogen = 0, phosphorus = 0, potassium = 0;
bool     sensorOK = false;
byte     response[32];

const byte query[] = { 0x01, 0x03, 0x00, 0x00, 0x00, 0x07, 0x04, 0x08 };

// What the website last told us about this scanner
//   -1 = unknown yet, 0 = nobody connected, 1 = somebody is connected
int  connState   = -1;
bool serverOK    = true;           // did the last website call work?
String sensorState = "unknown";    // "ok" | "fail" | "unknown"  (reported to the website)

String deviceToken = "";
char   deviceTag[8] = "----";      // short ID shown in the corner and on the website

// Screen
enum UiMode { UI_IDLE, UI_SCANNING, UI_SENDING, UI_SENT, UI_NOT_CONNECTED, UI_NO_SENSOR, UI_NO_WIFI, UI_FAILED, UI_BAD_KEY };
UiMode        uiMode      = UI_IDLE;
unsigned long uiResetAt   = 0;     // when a temporary message goes back to the idle button
String        uiKey       = "";    // what is drawn now, so we only redraw on change

// Timers / touch
unsigned long lastHeartbeat = 0;
unsigned long lastWifiRetry = 0;
unsigned long lastBlindRefresh = 0;
bool          wifiWasUp = false;
bool          previousTouch = false;
unsigned long lastTapMs = 0;
int           touchX = 0, touchY = 0;

// A tap that happens while we are busy talking to the website would be missed
// (the screen is not polled during a network call). The touch chip's INT pin
// tells us it happened.
#define USE_TOUCH_INT 1
volatile bool inNetwork = false;
volatile bool tapDuringNetwork = false;
void IRAM_ATTR onTouchInt() { if (inNetwork) tapDuringNetwork = true; }

bool serialTap = false;       // set by the Serial Monitor "tap" command, handled like a touch

void redrawUi();


// ============================================================
// DRAWING  -  one big button
// ============================================================

void printCentered(int16_t cx, int16_t y, const String &text, uint8_t size, uint16_t color) {

  tft.setTextSize(size);

  int16_t x1, y1;
  uint16_t w, h;

  tft.getTextBounds(text.c_str(), 0, 0, &x1, &y1, &w, &h);

  tft.setTextColor(color);
  tft.setCursor(cx - (int)w / 2, y);
  tft.print(text);
}


// Split `s` into at most two lines of at most `maxChars` characters
void wrap2(const String &s, int maxChars, String &a, String &b) {

  if ((int)s.length() <= maxChars) { a = s; b = ""; return; }

  int cut = s.lastIndexOf(' ', maxChars);

  if (cut <= 0) cut = maxChars;

  a = s.substring(0, cut);
  b = s.substring(cut + 1);

  if ((int)b.length() > maxChars) b = b.substring(0, maxChars);
}


void drawButton(uint16_t bg, const String &big, const String &small) {

  String key = String(bg) + "|" + big + "|" + small;

  if (key == uiKey) return;            // nothing changed -> no SPI traffic

  uiKey = key;

  // mirror the screen to the Serial Monitor (useful when the LCD is broken)
  static String lastShown = "";
  String shown = big + " - " + small;

  if (shown != lastShown) {            // a repaint of the same text stays quiet
    lastShown = shown;
    Serial.print("[SCREEN] ");
    Serial.print(big);
    if (small.length() > 0) { Serial.print(" - "); Serial.print(small); }
    Serial.println();
  }

  tft.fillScreen(bg);

  // a lighter frame makes it read as one big button
  tft.drawRoundRect(6, 6, SCREEN_W - 12, SCREEN_H - 12, 18, COL_WHITE);
  tft.drawRoundRect(7, 7, SCREEN_W - 14, SCREEN_H - 14, 17, COL_WHITE);

  int bigSize = (big.length() <= 8) ? 5 : (big.length() <= 11 ? 4 : 3);

  int bigH   = 8 * bigSize;
  int bigY   = (small.length() > 0) ? 78 : (SCREEN_H - bigH) / 2;

  printCentered(SCREEN_W / 2, bigY, big, bigSize, COL_WHITE);

  if (small.length() > 0) {

    String l1, l2;

    wrap2(small, 22, l1, l2);

    printCentered(SCREEN_W / 2, 150, l1, 2, COL_WHITE);

    if (l2.length() > 0) printCentered(SCREEN_W / 2, 172, l2, 2, COL_WHITE);
  }

#if SHOW_DEVICE_ID
  tft.setTextSize(1);
  tft.setTextColor(COL_WHITE);
  tft.setCursor(SCREEN_W - 60, SCREEN_H - 22);
  tft.print("ID ");
  tft.print(deviceTag);
#endif
}


// The idle button: its colour + small line tell you the connection state
void drawIdle() {

  bool wifiUp = (WiFi.status() == WL_CONNECTED);

  if (!wifiUp) {
    drawButton(BG_NOCONN, "SCAN NOW", "Connecting to Wi-Fi...");
  }
  else if (!serverOK) {
    drawButton(BG_WARN, "SCAN NOW", "Can't reach the website");
  }
  else if (connState == 1) {
    drawButton(BG_READY, "SCAN NOW", "Connected - tap to scan");
  }
  else if (connState == 0) {
    drawButton(BG_NOCONN, "SCAN NOW", "Not connected. Use the website.");
  }
  else {
    drawButton(BG_NOCONN, "SCAN NOW", "Tap to scan");
  }
}


void redrawUi() {

  uiKey = "";      // force a real redraw

  switch (uiMode) {

    case UI_SCANNING:       drawButton(BG_BUSY,  "SCANNING", "Keep the probe in the soil"); break;
    case UI_SENDING:        drawButton(BG_BUSY,  "SENDING",  "Please wait..."); break;
    case UI_SENT:           drawButton(BG_OK,    "SENT",     "Look at the website"); break;
    case UI_NOT_CONNECTED:  drawButton(BG_WARN,  "NOT CONNECTED", "Website > Find Device > Connect"); break;
    case UI_NO_SENSOR:      drawButton(BG_ERROR, "NO SENSOR", "Check the probe cable"); break;
    case UI_NO_WIFI:        drawButton(BG_ERROR, "NO WI-FI", "Check the network"); break;
    case UI_FAILED:         drawButton(BG_ERROR, "SEND FAILED", "Tap to try again"); break;
    case UI_BAD_KEY:        drawButton(BG_ERROR, "BAD API KEY", "Check API_KEY in the sketch"); break;
    default:                drawIdle(); break;
  }
}


// Show a temporary message, then fall back to the idle button
void showFor(UiMode m, unsigned long ms) {

  uiMode    = m;
  uiResetAt = (ms > 0) ? millis() + ms : 0;

  redrawUi();
}


void uiTick() {

  if (uiResetAt != 0 && millis() > uiResetAt) {

    uiResetAt = 0;
    uiMode    = UI_IDLE;
  }

  if (uiMode == UI_IDLE) redrawUi();     // cheap: draws only if the text/colour changed
}


// ============================================================
// RS485 / NPK SENSOR
// ============================================================

void readSensor() {

  while (Serial2.available()) Serial2.read();

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

      if (bytesRead < (int)sizeof(response)) response[bytesRead++] = Serial2.read();
      else                                   Serial2.read();
    }
  }

  Serial.print("[SENSOR] Bytes read: ");
  Serial.println(bytesRead);

  int startIndex = -1;

  for (int i = 0; i < bytesRead - 2; i++) {

    if (response[i] == 0x01 && response[i + 1] == 0x03 && response[i + 2] == 0x0E) {
      startIndex = i;
      break;
    }
  }

  if (startIndex >= 0 && bytesRead - startIndex >= 19) {

    uint16_t humidityRaw    = ((uint16_t)response[startIndex + 3]  << 8) | response[startIndex + 4];
    uint16_t temperatureRaw = ((uint16_t)response[startIndex + 5]  << 8) | response[startIndex + 6];
    soilConductivity        = ((uint16_t)response[startIndex + 7]  << 8) | response[startIndex + 8];
    uint16_t phRaw          = ((uint16_t)response[startIndex + 9]  << 8) | response[startIndex + 10];
    nitrogen                = ((uint16_t)response[startIndex + 11] << 8) | response[startIndex + 12];
    phosphorus              = ((uint16_t)response[startIndex + 13] << 8) | response[startIndex + 14];
    potassium               = ((uint16_t)response[startIndex + 15] << 8) | response[startIndex + 16];

    soilHumidity    = humidityRaw / 10.0;
    soilTemperature = temperatureRaw / 10.0;
    soilPH          = phRaw / 10.0;

    sensorOK    = true;
    sensorState = "ok";

    Serial.printf("[SENSOR] OK  N=%u P=%u K=%u pH=%.1f T=%.1f H=%.1f EC=%u\n",
                  nitrogen, phosphorus, potassium, soilPH, soilTemperature, soilHumidity, soilConductivity);
  }
  else {

    sensorOK    = false;
    sensorState = "fail";

    Serial.println("[SENSOR] NO VALID FRAME - check the probe, power and RS485 wiring");
  }
}


// ============================================================
// TOUCH  (the whole screen is the button)
// ============================================================

void initTouch() {

  pinMode(CTP_RST, OUTPUT);
  digitalWrite(CTP_RST, LOW);
  delay(20);
  digitalWrite(CTP_RST, HIGH);
  delay(200);

  Wire.begin(CTP_SDA, CTP_SCL);

  Wire.beginTransmission(FT6336_ADDR);

  Serial.println(Wire.endTransmission() == 0 ? "[TOUCH] FT6336 detected"
                                              : "[TOUCH] WARNING: FT6336 not detected!");

#if USE_TOUCH_INT
  pinMode(CTP_INT, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(CTP_INT), onTouchInt, FALLING);
#endif
}


bool readTouch(int &x, int &y) {

  Wire.beginTransmission(FT6336_ADDR);
  Wire.write(0x02);

  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(FT6336_ADDR, 5) != 5) return false;

  byte touchCount = Wire.read();
  byte xHigh = Wire.read();
  byte xLow  = Wire.read();
  byte yHigh = Wire.read();
  byte yLow  = Wire.read();

  if (touchCount == 0) return false;

  int rawX = ((xHigh & 0x0F) << 8) | xLow;
  int rawY = ((yHigh & 0x0F) << 8) | yLow;

  x = constrain(rawY, 0, 319);
  y = constrain(239 - rawX, 0, 239);

  return true;
}


// ============================================================
// WI-FI
// ============================================================

void wifiBegin() {

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);        // does not block
  WiFi.setTxPower(WIFI_TX_POWER);

  Serial.print("[WIFI] Connecting to ");
  Serial.println(WIFI_SSID);
}


void wifiWatch() {

  bool up = (WiFi.status() == WL_CONNECTED);

  if (up && !wifiWasUp) {

    Serial.print("[WIFI] Connected. IP ");
    Serial.print(WiFi.localIP());
    Serial.print("  RSSI ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");

    lastHeartbeat = 0;                          // announce ourselves right away
    redrawUi();                                 // radio start-up can glitch the panel: repaint
  }

  if (!up && wifiWasUp) {
    Serial.println("[WIFI] Connection lost, retrying...");
    redrawUi();
  }

  wifiWasUp = up;

  if (!up && millis() - lastWifiRetry > 10000) {

    lastWifiRetry = millis();

    Serial.println("[WIFI] Not connected - retrying (check name/password, 2.4 GHz)");

    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    WiFi.setTxPower(WIFI_TX_POWER);
  }
}


// ============================================================
// WEBSITE
// ============================================================

// Device secret: 32 random hex characters made on first boot, kept in flash.
void loadOrCreateToken() {

  Preferences prefs;

  prefs.begin("soilsense", false);

  deviceToken = prefs.getString("token", "");

  if (deviceToken.length() != 32) {

    char buf[33];

    for (int i = 0; i < 4; i++) snprintf(buf + i * 8, 9, "%08lx", (unsigned long)esp_random());

    deviceToken = String(buf);

    prefs.putString("token", deviceToken);

    Serial.println("[ID] Created a new device secret");
  }

  prefs.end();
}


// One tiny HTTP helper. Returns the HTTP status (negative = could not connect).
int httpPost(const String &path, const char* body, String &reply, uint32_t timeoutMs) {

  String url = String(SERVER_BASE) + path;

  inNetwork = true;

  WiFiClientSecure secureClient;
  WiFiClient       plainClient;
  HTTPClient       http;

  bool began;

  if (url.startsWith("https://")) {
    secureClient.setInsecure();                 // no certificate check (fine for a school project)
    began = http.begin(secureClient, url);
  } else {
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

  if (strlen(API_KEY) > 0) http.addHeader("X-API-Key", API_KEY);

  int code = http.POST((uint8_t*)body, strlen(body));

  reply = (code > 0) ? http.getString() : http.errorToString(code);

  http.end();

  inNetwork = false;

  return code;
}


// Minimal JSON reader:  "key": value  ->  value as text
String jsonGet(const String &json, const char* key) {

  String k = String("\"") + key + "\"";

  int i = json.indexOf(k);
  if (i < 0) return "";

  i = json.indexOf(':', i + k.length());
  if (i < 0) return "";

  i++;
  while (i < (int)json.length() && (json[i] == ' ' || json[i] == '\n')) i++;
  if (i >= (int)json.length()) return "";

  if (json[i] == '"') {
    int e = json.indexOf('"', i + 1);
    return (e < 0) ? String("") : json.substring(i + 1, e);
  }

  int e = i;
  while (e < (int)json.length() && json[e] != ',' && json[e] != '}' && json[e] != ' ' && json[e] != '\n') e++;

  return json.substring(i, e);
}


// "I'm here" - makes the scanner appear under Find device, and tells us whether
// somebody is connected. Returns true if the website answered.
bool sendHeartbeat() {

  lastHeartbeat = millis();

  if (WiFi.status() != WL_CONNECTED) return false;

  char body[160];

  snprintf(body, sizeof(body),
           "{\"rssi\":%d,\"sensor\":\"%s\",\"fw\":\"%s\",\"name\":\"%s\"}",
           (int)WiFi.RSSI(), sensorState.c_str(), FW_VERSION, deviceTag);

  String reply;

  int code = httpPost("/api/device/heartbeat", body, reply, 6000);

  if (code == 200) {

    int before = connState;

    connState = (jsonGet(reply, "connected") == "true") ? 1 : 0;
    serverOK  = true;

    if (before != connState) {
      Serial.println(connState == 1 ? "[LINK] A user connected to this scanner"
                                    : "[LINK] Nobody is connected to this scanner");
    }

    return true;
  }

  serverOK = false;

  Serial.print("[LINK] Heartbeat failed, HTTP ");
  Serial.println(code);

  return false;
}


// Send the current reading to the website. Returns the HTTP status.
int sendReading() {

  char uid[17];

  snprintf(uid, sizeof(uid), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());

  char body[320];

  snprintf(body, sizeof(body),
           "{\"N\":%u,\"P\":%u,\"K\":%u,\"ph\":%.1f,\"temp\":%.1f,\"humidity\":%.1f,\"rainfall\":%.1f,\"scan_uid\":\"%s\"}",
           (unsigned)nitrogen, (unsigned)phosphorus, (unsigned)potassium,
           soilPH, soilTemperature, soilHumidity, ASSUMED_RAINFALL_MM, uid);

  String reply;

  int code = httpPost("/api/esp32", body, reply, 20000);

  // One automatic retry on a network failure. The same scan_uid is sent again,
  // so the website can never save the reading twice.
  if (code <= 0) {

    Serial.println("[UPLOAD] Network problem - retrying once");

    delay(800);

    code = httpPost("/api/esp32", body, reply, 20000);
  }

  Serial.print("[UPLOAD] HTTP ");
  Serial.println(code);

  if (reply.length() > 200) reply = reply.substring(0, 200) + "...";
  Serial.println(reply);

  return code;
}


// ============================================================
// SCAN NOW
// ============================================================

void doScan() {

  Serial.println("[SCAN] Tap received");

  if (WiFi.status() != WL_CONNECTED) {
    showFor(UI_NO_WIFI, 3000);
    return;
  }

  // Not connected as far as we know? Ask the website right now: the user may have
  // pressed Connect a second ago and our last check-in could be a few seconds old.
  if (connState != 1) {

    drawButton(BG_BUSY, "CHECKING", "Asking the website...");

    sendHeartbeat();

    if (!serverOK) { showFor(UI_FAILED, 3000); return; }

    if (connState != 1) { showFor(UI_NOT_CONNECTED, 4000); return; }
  }

  showFor(UI_SCANNING, 0);

  readSensor();

  if (!sensorOK) {
    showFor(UI_NO_SENSOR, 3500);
    return;
  }

  showFor(UI_SENDING, 0);

  int code = sendReading();

  if (code == 200) {
    connState = 1;
    serverOK  = true;
    showFor(UI_SENT, 2500);
  }
  else if (code == 403) {
    // Disconnected (by the user, or by an admin) since our last check-in
    connState = 0;
    serverOK  = true;
    Serial.println("[LINK] Website says nobody is connected - reading NOT saved");
    showFor(UI_NOT_CONNECTED, 4000);
  }
  else if (code == 401) {
    showFor(UI_BAD_KEY, 4000);
  }
  else {
    serverOK = (code > 0);
    showFor(UI_FAILED, 3500);
  }
}


// ============================================================
// LCD WATCHDOG
// ------------------------------------------------------------
// A blank / white LCD that still reacts to touch means the panel controller
// reset itself (usually a supply dip when the Wi-Fi radio transmits) and forgot
// its settings. Two layers of protection:
//  1. If the panel can be read back (MISO wire connected) we check it every
//     1.5 s and re-initialise it when it lost its settings.
//  2. If it cannot be read back, we simply re-initialise + repaint it every
//     LCD_BLIND_REFRESH_MS while idle, so a blank screen heals itself within a
//     minute instead of staying blank. (Costs a brief flicker. 0 = off.)
// The real fix for the root cause is power: a 470 uF capacitor across 3.3 V
// next to the LCD, short wires, and a good USB supply / cable.
// ============================================================

#define LCD_REG_RDMODE   0x0A
#define LCD_REG_RDMADCTL 0x0B

bool          lcdWatchdogOn = false;
uint8_t       lcdExpectPM = 0, lcdExpectMAD = 0, lcdBadChecks = 0;
unsigned long lcdLastCheck = 0, lcdLastFix = 0;
unsigned int  lcdFixCount = 0;
bool          bootDiagShown = false;


void lcdReinit() {

  tft.begin(TFT_SPI_HZ);
  tft.setRotation(1);
  tft.fillScreen(BG_NOCONN);

  redrawUi();
}


void lcdArmWatchdog() {

  lcdWatchdogOn = false;

  uint8_t pm  = tft.readcommand8(LCD_REG_RDMODE);
  uint8_t mad = tft.readcommand8(LCD_REG_RDMADCTL);

  bool stable = (pm == 0x9C);

  for (int i = 0; i < 3; i++) {

    delay(5);

    if (tft.readcommand8(LCD_REG_RDMODE) != pm || tft.readcommand8(LCD_REG_RDMADCTL) != mad) stable = false;
  }

  Serial.printf("[LCD] power mode 0x%02X  madctl 0x%02X\n", pm, mad);

  if (stable) {
    lcdExpectPM   = pm;
    lcdExpectMAD  = mad;
    lcdWatchdogOn = true;
    Serial.println("[LCD] watchdog ARMED");
  }
  else {
    Serial.println("[LCD] watchdog OFF (cannot read the panel back - check the SDO/MISO wire). Using timed refresh instead.");
  }
}


void lcdWatchdogTick() {

  if (lcdWatchdogOn) {

    if (millis() - lcdLastCheck < 1500) return;

    lcdLastCheck = millis();

    uint8_t pm  = tft.readcommand8(LCD_REG_RDMODE);
    uint8_t mad = tft.readcommand8(LCD_REG_RDMADCTL);

    if (pm == lcdExpectPM && mad == lcdExpectMAD) { lcdBadChecks = 0; return; }

    if (++lcdBadChecks < 2) return;

    lcdBadChecks = 0;

    if (millis() - lcdLastFix < 4000) return;

    lcdLastFix = millis();
    lcdFixCount++;

    Serial.printf("[LCD] Panel lost its settings (0x%02X / 0x%02X) - re-initialising. Count %u\n", pm, mad, lcdFixCount);

    lcdReinit();
    return;
  }

  // blind mode: periodic repair, only while idle and not mid-message
  if (LCD_BLIND_REFRESH_MS > 0 && uiMode == UI_IDLE && millis() - lastBlindRefresh > LCD_BLIND_REFRESH_MS) {

    lastBlindRefresh = millis();

    lcdFixCount++;

    lcdReinit();
  }
}


void bootDiagTick() {

  if (bootDiagShown || millis() < 5000) return;

  bootDiagShown = true;

  Serial.print("[BOOT] Last restart reason: ");

  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  Serial.println("power switched on (normal)"); break;
    case ESP_RST_EXT:      Serial.println("reset button / EN pin"); break;
    case ESP_RST_SW:       Serial.println("software restart"); break;
    case ESP_RST_PANIC:    Serial.println("CRASH (exception)"); break;
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:      Serial.println("watchdog (program hung)"); break;
    case ESP_RST_BROWNOUT: Serial.println("BROWNOUT - the 3.3 V supply dipped. Fix the power."); break;
    default:               Serial.println("other"); break;
  }

  Serial.printf("[BOOT] Free memory: %u bytes\n", (unsigned)ESP.getFreeHeap());
  Serial.printf("[LCD] watchdog %s, repairs so far: %u\n", lcdWatchdogOn ? "armed" : "off (timed refresh)", lcdFixCount);
}


// ============================================================
// SERIAL MONITOR COMMANDS
// ------------------------------------------------------------
// Open Tools > Serial Monitor, 115200 baud. Any line ending works ("No line ending"
// too: a command is taken after a short pause).
//   tap | t | scan | s | 1   same as touching the screen (one reading, sent to the website)
//   status | ?               Wi-Fi, website link, sensor, free memory
//   help | h                 this list
// ============================================================

String        serialBuf = "";
unsigned long serialLastChar = 0;


void printHelp() {

  Serial.println();
  Serial.println("---- SoilSense Serial Monitor commands ----");
  Serial.println("  tap | t | scan | s | 1   take one reading and send it (same as touching the screen)");
  Serial.println("  status | ?               show Wi-Fi / website / sensor state");
  Serial.println("  help | h                 show this list");
  Serial.println("--------------------------------------------");
}


void printStatus() {

  Serial.println();
  Serial.printf("[STATUS] Device ID : %s   firmware %s\n", deviceTag, FW_VERSION);
  Serial.printf("[STATUS] Wi-Fi     : %s", WiFi.status() == WL_CONNECTED ? "connected" : "NOT connected");
  if (WiFi.status() == WL_CONNECTED) Serial.printf("  (%s, %d dBm)", WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
  Serial.println();
  Serial.printf("[STATUS] Website   : %s\n", !serverOK ? "cannot reach it"
                                            : connState == 1 ? "a user is CONNECTED - scans will be saved"
                                            : connState == 0 ? "nobody connected (use Find Device > Connect on the website)"
                                            : "not checked yet");
  Serial.printf("[STATUS] Sensor    : %s\n", sensorState.c_str());
  Serial.printf("[STATUS] Free heap : %u bytes\n", (unsigned)ESP.getFreeHeap());
}


void handleSerialCommand(String cmd) {

  cmd.trim();
  cmd.toLowerCase();

  if (cmd.length() == 0) return;

  if (cmd == "tap" || cmd == "t" || cmd == "scan" || cmd == "s" || cmd == "1") {
    Serial.println("[SERIAL] tap received");
    serialTap = true;
  }
  else if (cmd == "status" || cmd == "?") printStatus();
  else if (cmd == "help" || cmd == "h")   printHelp();
  else {
    Serial.print("[SERIAL] Unknown command \"");
    Serial.print(cmd);
    Serial.println("\". Type  tap  to scan, or  help.");
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

  // "No line ending" selected in the Serial Monitor: take what we have after a short pause
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

  // short ID from the chip, shown in the corner and on the website
  snprintf(deviceTag, sizeof(deviceTag), "%04X", (unsigned)((uint16_t)(ESP.getEfuseMac() >> 32)));

  pinMode(TFT_LED, OUTPUT);
  digitalWrite(TFT_LED, HIGH);

  SPI.begin(TFT_SCK, TFT_MISO, TFT_MOSI, TFT_CS);

  tft.begin(TFT_SPI_HZ);
  tft.setRotation(1);

  drawButton(BG_NOCONN, "SCAN NOW", "Starting...");     // the button is on screen from the first second

  Serial2.begin(4800, SERIAL_8N1, RX2_PIN, TX2_PIN);

  pinMode(RE_DE_PIN, OUTPUT);
  digitalWrite(RE_DE_PIN, LOW);

  initTouch();

  loadOrCreateToken();

  delay(800);

  lcdArmWatchdog();         // learn what a healthy panel reports BEFORE Wi-Fi starts

  wifiBegin();

  Serial.println();
  Serial.println("================================");
  Serial.println("SOILSENSE SCANNER - SCAN NOW");
  Serial.printf("Device ID on the website: SoilSense Scanner %s\n", deviceTag);
  Serial.println("Serial Monitor: type  tap  to scan, or  help");
  Serial.println("================================");

  uiMode = UI_IDLE;
  redrawUi();
}


void loop() {

  wifiWatch();

  serialTick();

  lcdWatchdogTick();

  bootDiagTick();

  // --- announce ourselves / learn whether somebody is connected -------------
  if (WiFi.status() == WL_CONNECTED && (lastHeartbeat == 0 || millis() - lastHeartbeat >= HEARTBEAT_MS)) {

    sendHeartbeat();
  }

  // --- touch: anywhere on the screen = scan ---------------------------------
  bool tap = false;

  bool touched = readTouch(touchX, touchY);

  if (touched && !previousTouch) tap = true;

  if (tapDuringNetwork) {          // tapped while we were busy with the website
    tapDuringNetwork = false;
    tap = true;
    touched = readTouch(touchX, touchY);   // a finger still held must not count again
  }

  previousTouch = touched;

  if (serialTap) {                 // "tap" typed in the Serial Monitor counts exactly like a touch
    serialTap = false;
    tap = true;
  }

  if (tap && millis() - lastTapMs > 600) {

    lastTapMs = millis();

    doScan();

    previousTouch = readTouch(touchX, touchY);
    tapDuringNetwork = false;
  }

  uiTick();

  delay(20);
}
