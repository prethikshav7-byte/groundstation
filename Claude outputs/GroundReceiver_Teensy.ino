/*
  GroundReceiver_Teensy.ino
  ──────────────────────────
  Sits between your two LoRa radios and the Python ground_station app.
  Listens on BOTH vehicle frequencies at once (two physical LoRa
  modules on the same Teensy, one per frequency), decodes the compact
  43-byte binary frame each one sends (see RocketTelemetry_Teensy.ino
  for the TX side and the frame layout -- both files must be kept
  byte-for-byte in sync), and re-builds the EXACT 21-field "$T,..." CSV
  line the existing Python app already expects, printing it over USB
  Serial exactly like the old USB-direct firmware did.

  RESULT: your Python side (ground_station/codec.py, parser.py, every
  dashboard page) needs ZERO code changes. Plug this Teensy in over
  USB, note its COM port, and launch the app exactly as before:

      python main.py --port COM4 --baud 115200

  This Teensy is a translator, not a "vehicle" -- it never itself has a
  vehicle_id, mission_time origin, etc. It just relays whatever the two
  transmitting Teensys (rocket + cansat) send it.

  ══════════════════════════════════════════════════════════════════
  WIRING -- TWO LoRa MODULES ON ONE SHARED SPI BUS
  ══════════════════════════════════════════════════════════════════
  Both radios share SCK/MOSI/MISO (standard SPI bus-sharing); each one
  needs its OWN chip-select (NSS), reset, and DIO0 pin so the Teensy
  can talk to them independently.

    Shared:
      LoRa #1 SCK  -> Teensy pin 13     LoRa #2 SCK  -> Teensy pin 13
      LoRa #1 MISO -> Teensy pin 12     LoRa #2 MISO -> Teensy pin 12
      LoRa #1 MOSI -> Teensy pin 11     LoRa #2 MOSI -> Teensy pin 11
      Both LoRa VCC -> 3.3V (NOT 5V), both GND -> Teensy GND

    Radio A -- tuned to the ROCKET frequency (867.00 MHz):
      NSS/CS -> Teensy pin 10   RST -> Teensy pin 9    DIO0 -> Teensy pin 2

    Radio B -- tuned to the CANSAT frequency (867.75 MHz):
      NSS/CS -> Teensy pin 8    RST -> Teensy pin 7    DIO0 -> Teensy pin 3

  (Adjust the pin numbers below if you wired it differently -- as long
  as CS/RST/DIO0 are unique per radio, any free digital pins work.)

  LIBRARY: same as the TX sketches -- Sandeep Mistry's "LoRa" library
  (Arduino IDE -> Tools -> Manage Libraries -> search "LoRa").
*/

#include <SPI.h>
#include <LoRa.h>

// ─────────────────────────────────────────────────────────────────────────
//  1. Config -- must match the TX sketches exactly (frequency, SF, BW,
//     CR, sync word) or the radios simply won't demodulate each other.
// ─────────────────────────────────────────────────────────────────────────

static const uint32_t SERIAL_BAUD = 115200;
static const uint16_t TEAM_ID     = 1234;   // hardcoded -- never sent over the air

static const uint8_t  LORA_SF       = 10;
static const long     LORA_BW_HZ    = 125E3;
static const uint8_t  LORA_CR_DENOM = 5;

struct RadioConfig {
  const char *vehicleId;
  long        frequencyHz;
  uint8_t     syncWord;
  uint8_t     csPin, rstPin, dio0Pin;
};

static RadioConfig ROCKET_CFG = { "ROCKET", 867000000UL, 0x52, 10, 9, 2 };
static RadioConfig CANSAT_CFG = { "CANSAT", 867750000UL, 0x43, 8, 7, 3 };

LoRaClass radioRocket;
LoRaClass radioCansat;

// ─────────────────────────────────────────────────────────────────────────
//  2. Wire frame format -- MUST be byte-for-byte identical to the block
//     in RocketTelemetry_Teensy.ino. See that file for field-by-field
//     comments on the scaling used.
// ─────────────────────────────────────────────────────────────────────────

typedef struct __attribute__((packed)) {
  uint32_t mission_time_ms;
  uint16_t packet_count;
  uint8_t  status;
  int16_t  altitude_dm;
  uint32_t pressure_pa;
  int16_t  temperature_cdeg;
  uint16_t battery_mv;
  uint32_t gnss_time_cs;
  int32_t  gnss_lat_e7;
  int32_t  gnss_lon_e7;
  int16_t  gnss_alt_dm;
  int16_t  accel_x_mg;
  int16_t  accel_y_mg;
  int16_t  accel_z_mg;
  int16_t  gyro_x_cdps;
  int16_t  gyro_y_cdps;
  int16_t  gyro_z_cdps;
} TelemetryFrame;   // sizeof == 43

static_assert(sizeof(TelemetryFrame) == 43, "TelemetryFrame must stay 43 bytes -- keep in sync with RocketTelemetry_Teensy.ino");

static const char *STATE_NAMES[8] = {
  "BOOT", "PRE_LAUNCH", "BOOST", "COAST",
  "APOGEE", "DESCENT", "LANDING", "RECOVERY"
};

static void unpackStatus(uint8_t status, uint8_t &stateCode, bool &gpsFix, uint8_t &satCount) {
  stateCode = (status >> 5) & 0x07;
  gpsFix    = (status >> 4) & 0x01;
  satCount  = status & 0x0F;
}

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xorout.
// Must match the TX sketch's implementation exactly.
static uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

// XOR checksum -- byte-for-byte identical to codec.py's xor_checksum():
// XOR of every byte of the payload from "$T," up to (not including)
// the trailing comma+checksum, printed as 2 uppercase hex digits.
static uint8_t xorChecksum(const String &payload) {
  uint8_t x = 0;
  for (size_t i = 0; i < payload.length(); i++) x ^= (uint8_t)payload[i];
  return x;
}

// ─────────────────────────────────────────────────────────────────────────
//  3. Setup
// ─────────────────────────────────────────────────────────────────────────

static bool initRadio(LoRaClass &radio, const RadioConfig &cfg) {
  radio.setPins(cfg.csPin, cfg.rstPin, cfg.dio0Pin);
  if (!radio.begin(cfg.frequencyHz)) return false;
  radio.setSpreadingFactor(LORA_SF);
  radio.setSignalBandwidth(LORA_BW_HZ);
  radio.setCodingRate4(LORA_CR_DENOM);
  radio.setSyncWord(cfg.syncWord);
  return true;
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  while (!Serial && millis() < 3000) { /* wait for USB Serial */ }

  bool okRocket = initRadio(radioRocket, ROCKET_CFG);
  bool okCansat = initRadio(radioCansat, CANSAT_CFG);

  // These prints are harmless debug noise on USB -- the Python app's
  // parser only acts on lines starting with "$T,"/"$H,"/"$F,", so any
  // other line (like these) is simply ignored by it.
  if (!okRocket) Serial.println("# WARN: ROCKET radio (867.00 MHz) failed to init -- check wiring.");
  if (!okCansat) Serial.println("# WARN: CANSAT radio (867.75 MHz) failed to init -- check wiring.");
  Serial.println("# GroundReceiver ready.");
}

// ─────────────────────────────────────────────────────────────────────────
//  4. Decode one radio's pending packet (if any) and print the
//     reconstructed CSV line over USB.
// ─────────────────────────────────────────────────────────────────────────

static void pollRadio(LoRaClass &radio, const RadioConfig &cfg) {
  int packetSize = radio.parsePacket();
  if (packetSize == 0) return;

  static const size_t EXPECTED = sizeof(TelemetryFrame) + 2;   // struct + CRC16
  if (packetSize != (int)EXPECTED) {
    // Wrong size -- corrupt packet, foreign transmitter, etc. Drop it.
    while (radio.available()) radio.read();
    Serial.print("# WARN: "); Serial.print(cfg.vehicleId);
    Serial.print(" got "); Serial.print(packetSize);
    Serial.print(" bytes, expected "); Serial.println((int)EXPECTED);
    return;
  }

  uint8_t buf[EXPECTED];
  for (size_t i = 0; i < EXPECTED; i++) buf[i] = (uint8_t)radio.read();

  uint16_t rxCrc = ((uint16_t)buf[sizeof(TelemetryFrame)] << 8) | buf[sizeof(TelemetryFrame) + 1];
  uint16_t calcCrc = crc16_ccitt(buf, sizeof(TelemetryFrame));
  if (rxCrc != calcCrc) {
    Serial.print("# WARN: "); Serial.print(cfg.vehicleId);
    Serial.println(" CRC16 mismatch -- dropped corrupt packet.");
    return;
  }

  TelemetryFrame f;
  memcpy(&f, buf, sizeof(TelemetryFrame));

  uint8_t stateCode, satCount;
  bool gpsFix;
  unpackStatus(f.status, stateCode, gpsFix, satCount);
  const char *stateName = (stateCode < 8) ? STATE_NAMES[stateCode] : "PRE_LAUNCH";

  float missionTimeS  = f.mission_time_ms / 1000.0f;
  float altitudeM     = f.altitude_dm / 10.0f;
  float pressurePa    = (float)f.pressure_pa;
  float temperatureC  = f.temperature_cdeg / 100.0f;
  float batteryV      = f.battery_mv / 1000.0f;
  float gnssLat       = f.gnss_lat_e7 / 1e7f;
  float gnssLon       = f.gnss_lon_e7 / 1e7f;
  float gnssAlt       = f.gnss_alt_dm / 10.0f;
  float accelX = f.accel_x_mg / 1000.0f, accelY = f.accel_y_mg / 1000.0f, accelZ = f.accel_z_mg / 1000.0f;
  float gyroX  = f.gyro_x_cdps / 100.0f, gyroY  = f.gyro_y_cdps / 100.0f, gyroZ  = f.gyro_z_cdps / 100.0f;

  uint32_t cs = f.gnss_time_cs;
  uint32_t gh = cs / 360000UL;
  uint32_t gm = (cs % 360000UL) / 6000UL;
  float    gs = (cs % 6000UL) / 100.0f;
  char gnssTimeStr[16];
  snprintf(gnssTimeStr, sizeof(gnssTimeStr), "%02lu:%02lu:%05.2f", (unsigned long)gh, (unsigned long)gm, gs);

  // Field order MUST match ground_station/codec.py's FIELD_SPEC exactly
  // -- this is the same 20-field body the old USB-direct firmware sent.
  String payload = "$T,";
  payload += String(TEAM_ID) + ",";
  payload += String(cfg.vehicleId) + ",";
  payload += String(missionTimeS, 2) + ",";
  payload += String(f.packet_count) + ",";
  payload += String(stateName) + ",";
  payload += String(altitudeM, 2) + ",";
  payload += String(pressurePa, 1) + ",";
  payload += String(temperatureC, 2) + ",";
  payload += String(batteryV, 2) + ",";
  payload += String(gnssTimeStr) + ",";
  payload += String(gnssLat, 6) + ",";
  payload += String(gnssLon, 6) + ",";
  payload += String(gnssAlt, 2) + ",";
  payload += String(satCount) + ",";
  payload += String(accelX, 3) + ",";
  payload += String(accelY, 3) + ",";
  payload += String(accelZ, 3) + ",";
  payload += String(gyroX, 2) + ",";
  payload += String(gyroY, 2) + ",";
  payload += String(gyroZ, 2);

  char csum[3];
  snprintf(csum, sizeof(csum), "%02X", xorChecksum(payload));

  Serial.print(payload);
  Serial.print(",");
  Serial.println(csum);
}

void loop() {
  pollRadio(radioRocket, ROCKET_CFG);
  pollRadio(radioCansat, CANSAT_CFG);
}
