/*
  RocketTelemetry_Teensy.ino
  ──────────────────────────
  Teensy + MS5611 + LoRa (SX1276/RFM95) firmware. Reads real pressure/
  temperature off the MS5611, packs a compact 43-byte binary telemetry
  frame + 2-byte CRC16 (45 bytes total), and transmits it over LoRa
  radio instead of USB.

  WHY THIS EXISTS: the old version of this file sent 21-field ASCII CSV
  directly over USB. That was fine for USB-direct bring-up, but once you
  move to LoRa, CSV text is very airtime-expensive -- at SF10 a ~143-byte
  CSV line takes about 1.3 seconds to transmit, which is where your
  "1.3s per packet" number was coming from. Airtime scales with byte
  count, not implicit "how much data" -- so the fix is to shrink the
  bytes, not the sample rate.

  THE FIX: this sketch transmits 17 data fields packed into 43 raw
  bytes (scaled/fixed-point integers instead of ASCII text -- see the
  struct below), CRC16-protected. At SF10 / 125 kHz / CR 4:5 that is
  about 550-600 ms of airtime -- under your 1-second target with room
  to spare. On the ground, GroundReceiver_Teensy.ino (a separate sketch,
  see that file) receives this, decodes it, and re-builds the EXACT
  same 21-field CSV line your existing Python ground_station app
  already parses. The Python side (codec.py / parser.py / the whole
  dashboard) needs ZERO changes -- the binary format only exists on
  the radio hop between this sketch and the ground receiver.

  team_id and vehicle_id are NOT sent over the air on purpose: team_id
  never changes in flight (the ground receiver hardcodes it), and
  vehicle_id is implicit in which of the ground receiver's two radios
  (867.00 MHz vs 867.75 MHz) picked the packet up. Dropping those two
  fields is "free" bytes saved for zero information loss.

  ══════════════════════════════════════════════════════════════════
  BEFORE YOU FLASH THIS -- SET ONE OF THESE:
  ══════════════════════════════════════════════════════════════════
      #define VEHICLE_ROCKET      -> transmits on 867.00 MHz
      #define VEHICLE_CANSAT      -> transmits on 867.75 MHz
  (exactly one, right below, matches your CanSat/Rocket frequency plan)

  WIRING -- MS5611 (same as MS5611_Test.ino):
    MS5611 VCC -> Teensy 3.3V     MS5611 GND -> Teensy GND
    MS5611 SCL -> Teensy pin 19   MS5611 SDA -> Teensy pin 18
    MS5611 CSB -> 3.3V (addr 0x77) or GND (addr 0x76)

  WIRING -- LoRa (SX1276 / RFM95 breakout), shares the hardware SPI bus:
    LoRa VCC  -> Teensy 3.3V (NOT 5V)     LoRa GND -> Teensy GND
    LoRa SCK  -> Teensy pin 13            LoRa MISO -> Teensy pin 12
    LoRa MOSI -> Teensy pin 11            LoRa NSS/CS -> Teensy pin 10
    LoRa RST  -> Teensy pin 9             LoRa DIO0  -> Teensy pin 2
  (change LORA_CS / LORA_RST / LORA_DIO0 below if you wired it
  differently -- these three are the only pins that matter, SPI is
  fixed by the Teensy's hardware SPI0.)

  LIBRARY: Sandeep Mistry's "LoRa" library. Arduino IDE -> Tools ->
  Manage Libraries -> search "LoRa" by Sandeep Mistry -> Install.

  WHAT'S REAL VS. PLACEHOLDER RIGHT NOW (unchanged from before):
    - pressure, temperature, altitude -> REAL, from the MS5611.
    - battery_voltage, GNSS, accel_x/y/z, gyro_x/y/z -> PLACEHOLDER,
      sent as the exact sentinel values (0) the ground station already
      treats as "not attached" (see pages/vehicle.py's pressure_valid /
      temp_valid / imu_valid checks) -- unchanged, this firmware just
      changes HOW the same values get to the ground station.
*/

#include <Wire.h>
#include <math.h>
#include <SPI.h>
#include <LoRa.h>

// ─────────────────────────────────────────────────────────────────────────
//  1. PICK YOUR VEHICLE -- exactly one of these two lines uncommented
// ─────────────────────────────────────────────────────────────────────────

#define VEHICLE_ROCKET
// #define VEHICLE_CANSAT

#if defined(VEHICLE_ROCKET) && defined(VEHICLE_CANSAT)
  #error "Define VEHICLE_ROCKET or VEHICLE_CANSAT, not both."
#elif !defined(VEHICLE_ROCKET) && !defined(VEHICLE_CANSAT)
  #error "Define VEHICLE_ROCKET or VEHICLE_CANSAT above before flashing."
#endif

#if defined(VEHICLE_ROCKET)
  static const long    LORA_FREQUENCY_HZ = 867000000UL;  // 867.00 MHz
  static const uint8_t LORA_SYNC_WORD    = 0x52;          // 'R'ocket
#else
  static const long    LORA_FREQUENCY_HZ = 867750000UL;  // 867.75 MHz
  static const uint8_t LORA_SYNC_WORD    = 0x43;          // 'C'anSat
#endif

// ─────────────────────────────────────────────────────────────────────────
//  2. Config
// ─────────────────────────────────────────────────────────────────────────

static const uint8_t  LORA_CS       = 10;
static const uint8_t  LORA_RST      = 9;
static const uint8_t  LORA_DIO0     = 2;

static const uint8_t  LORA_SF       = 10;      // spreading factor
static const long     LORA_BW_HZ    = 125E3;   // bandwidth
static const uint8_t  LORA_CR_DENOM = 5;        // coding rate 4/5

static const uint16_t SEND_RATE_HZ  = 1;        // 1 packet/sec (see note below)
// At SF10 / 125 kHz / CR 4:5, a 45-byte frame (43-byte struct + 2-byte
// CRC16, plus LoRa's own preamble/header overhead) takes roughly
// 550-600 ms of airtime -- comfortably under 1 second, which is the
// whole point of this rewrite. 1 Hz leaves headroom; if your sensors
// can genuinely support faster updates AND your link budget allows a
// lower spreading factor (SF7 is ~4x faster on airtime than SF10, at
// the cost of range), you can raise SEND_RATE_HZ and/or lower LORA_SF.
// NOTE: many regions regulate the 863-870 MHz ISM band with a duty-
// cycle limit (commonly 1%) unless your hardware implements LBT/AFA --
// check your local rules/competition rules before pushing the rate up.

// ─────────────────────────────────────────────────────────────────────────
//  3. Wire frame format -- MUST be byte-for-byte identical to the
//     matching block in GroundReceiver_Teensy.ino. 17 data fields
//     packed into 43 bytes using fixed-point scaling (see comments per
//     field), so nothing here needs floating point on the wire.
// ─────────────────────────────────────────────────────────────────────────

typedef struct __attribute__((packed)) {
  uint32_t mission_time_ms;   // ms since boot                    -> /1000.0 = s
  uint16_t packet_count;      // wraps at 65535, fine for one flight
  uint8_t  status;            // bit7-5 state(0-7) bit4 gps_fix bit3-0 sat_count(0-15)
  int16_t  altitude_dm;       // decimetres AGL                   -> /10.0  = m   (+-3276.7 m)
  uint32_t pressure_pa;       // pascals, raw (no scaling needed) -> Pa directly
  int16_t  temperature_cdeg;  // centi-degC                       -> /100.0 = degC
  uint16_t battery_mv;        // millivolts                       -> /1000.0 = V
  uint32_t gnss_time_cs;      // centiseconds since UTC midnight  -> HH:MM:SS.ss
  int32_t  gnss_lat_e7;       // degrees * 1e7                    -> /1e7
  int32_t  gnss_lon_e7;       // degrees * 1e7                    -> /1e7
  int16_t  gnss_alt_dm;       // decimetres                       -> /10.0  = m
  int16_t  accel_x_mg;        // milli-g                          -> /1000.0 = g
  int16_t  accel_y_mg;
  int16_t  accel_z_mg;
  int16_t  gyro_x_cdps;       // centi-deg/s                      -> /100.0 = deg/s
  int16_t  gyro_y_cdps;
  int16_t  gyro_z_cdps;
} TelemetryFrame;   // sizeof == 43

static_assert(sizeof(TelemetryFrame) == 43, "TelemetryFrame must stay 43 bytes -- keep in sync with GroundReceiver_Teensy.ino");

// FlightState codes packed into status bits 7-5. Order MUST match
// ground_station/models.py's FlightState enum ordinal order exactly.
enum FlightStateCode : uint8_t {
  ST_BOOT = 0, ST_PRE_LAUNCH = 1, ST_BOOST = 2, ST_COAST = 3,
  ST_APOGEE = 4, ST_DESCENT = 5, ST_LANDING = 6, ST_RECOVERY = 7
};

static uint8_t packStatus(uint8_t stateCode, bool gpsFix, uint8_t satCount) {
  if (satCount > 15) satCount = 15;
  return (uint8_t)(((stateCode & 0x07) << 5) | ((gpsFix ? 1 : 0) << 4) | (satCount & 0x0F));
}

// CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xorout.
// Verified against the standard test vector: crc16("123456789") == 0x29B1.
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

// ─────────────────────────────────────────────────────────────────────────
//  4. Minimal MS5611 driver (identical to MS5611_Test.ino)
// ─────────────────────────────────────────────────────────────────────────

class MS5611 {
public:
  bool begin(TwoWire &wire = Wire) {
    _wire = &wire;
    for (uint8_t addr : {0x77, 0x76}) {
      _addr = addr;
      if (reset() && readCalibration() && crcOk()) {
        _present = true;
        return true;
      }
    }
    _present = false;
    return false;
  }

  bool present() const { return _present; }

  bool read(float &pressurePa, float &temperatureC) {
    if (!_present) return false;

    uint32_t d2 = readADC(0x58);
    uint32_t d1 = readADC(0x48);
    if (d1 == 0 || d2 == 0) return false;

    int64_t dT   = (int64_t)d2 - ((int64_t)_c[4] << 8);
    int64_t temp = 2000 + ((dT * _c[5]) >> 23);

    int64_t off  = ((int64_t)_c[1] << 16) + ((_c[3] * dT) >> 7);
    int64_t sens = ((int64_t)_c[0] << 15) + ((_c[2] * dT) >> 8);

    if (temp < 2000) {
      int64_t t2    = (dT * dT) >> 31;
      int64_t off2  = 5 * ((temp - 2000) * (temp - 2000)) / 2;
      int64_t sens2 = off2 / 2;
      if (temp < -1500) {
        int64_t extra = (temp + 1500) * (temp + 1500);
        off2  += 7 * extra;
        sens2 += (11 * extra) / 2;
      }
      temp -= t2;
      off  -= off2;
      sens -= sens2;
    }

    int64_t p = ((((int64_t)d1 * sens) >> 21) - off) >> 15;

    pressurePa   = (float)p;   // 0.01 mbar units == Pa, numerically
    temperatureC = temp / 100.0f;
    return true;
  }

private:
  TwoWire *_wire = &Wire;
  uint8_t  _addr = 0x77;
  bool     _present = false;
  uint16_t _prom[8] = {0};
  uint16_t *_c = _prom + 1;

  bool writeCmd(uint8_t cmd) {
    _wire->beginTransmission(_addr);
    _wire->write(cmd);
    return _wire->endTransmission() == 0;
  }

  bool reset() {
    if (!writeCmd(0x1E)) return false;
    delay(5);
    return true;
  }

  bool readCalibration() {
    for (uint8_t i = 0; i < 8; i++) {
      if (!writeCmd(0xA0 + i * 2)) return false;
      if (_wire->requestFrom(_addr, (uint8_t)2) != 2) return false;
      uint16_t hi = _wire->read();
      uint16_t lo = _wire->read();
      _prom[i] = (hi << 8) | lo;
    }
    return true;
  }

  bool crcOk() {
    uint16_t promCopy[8];
    memcpy(promCopy, _prom, sizeof(promCopy));
    uint8_t crcRead = promCopy[7] & 0x0F;
    promCopy[7] &= 0xFF00;

    uint16_t rem = 0;
    for (uint8_t i = 0; i < 16; i++) {
      if (i % 2 == 1) rem ^= (promCopy[i >> 1] & 0x00FF);
      else            rem ^= (promCopy[i >> 1] >> 8);
      for (uint8_t bit = 8; bit > 0; bit--) {
        if (rem & 0x8000) rem = (rem << 1) ^ 0x3000;
        else              rem = (rem << 1);
      }
    }
    uint8_t crcCalc = (rem >> 12) & 0x0F;
    return crcCalc == crcRead;
  }

  uint32_t readADC(uint8_t convertCmd) {
    if (!writeCmd(convertCmd)) return 0;
    delay(10);
    if (!writeCmd(0x00)) return 0;
    if (_wire->requestFrom(_addr, (uint8_t)3) != 3) return 0;
    uint32_t b2 = _wire->read();
    uint32_t b1 = _wire->read();
    uint32_t b0 = _wire->read();
    return (b2 << 16) | (b1 << 8) | b0;
  }
};

MS5611 baro;
uint32_t packetCount = 0;
float groundPressurePa = 101325.0f;   // set from the first good reading

// Standard barometric formula, altitude AGL relative to groundPressurePa.
float altitudeFromPressure(float pressurePa) {
  return 44330.0f * (1.0f - powf(pressurePa / groundPressurePa, 1.0f / 5.255f));
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) { /* wait for USB Serial, optional (debug only) */ }

  Wire.begin();
  Wire.setClock(400000);

  if (baro.begin()) {
    float p, t, sum = 0;
    uint8_t n = 0;
    for (uint8_t i = 0; i < 8; i++) {
      if (baro.read(p, t)) { sum += p; n++; }
      delay(20);
    }
    if (n > 0) groundPressurePa = sum / n;
    Serial.println("MS5611 found, ground pressure calibrated.");
  } else {
    Serial.println("MS5611 NOT FOUND -- pressure/temperature will be sent as sentinels.");
  }

  LoRa.setPins(LORA_CS, LORA_RST, LORA_DIO0);
  if (!LoRa.begin(LORA_FREQUENCY_HZ)) {
    Serial.println("LoRa init FAILED -- check wiring/frequency. Halting.");
    while (true) { delay(1000); }
  }
  LoRa.setSpreadingFactor(LORA_SF);
  LoRa.setSignalBandwidth(LORA_BW_HZ);
  LoRa.setCodingRate4(LORA_CR_DENOM);
  LoRa.setSyncWord(LORA_SYNC_WORD);
  // Not calling LoRa.enableCrc(): our own CRC16 (appended to every
  // frame below) already covers end-to-end integrity, so the radio's
  // built-in CRC would just be redundant airtime.

  Serial.print("LoRa TX ready on ");
  Serial.print(LORA_FREQUENCY_HZ / 1000000.0, 2);
  Serial.println(" MHz.");
}

void loop() {
  static uint32_t lastSendMs = 0;
  uint32_t nowMs = millis();
  uint32_t intervalMs = 1000UL / SEND_RATE_HZ;
  if (nowMs - lastSendMs < intervalMs) return;
  lastSendMs = nowMs;

  float pressurePa, temperatureC, altitudeM;
  bool sensorOk = baro.present() && baro.read(pressurePa, temperatureC);
  if (!sensorOk) {
    // Sentinels the ground station already recognises as "not attached"
    // (pressure_valid / temp_valid in pages/vehicle.py) -- unchanged
    // from the old firmware, just now carried over the radio instead
    // of USB.
    pressurePa   = 101325.0f;
    temperatureC = 0.0f;
    altitudeM    = 0.0f;
  } else {
    altitudeM = altitudeFromPressure(pressurePa);
  }

  // TODO: replace with real readings once an IMU/GPS/voltage divider
  // are wired up. Left at exactly 0 on purpose -- imu_valid on the
  // ground station treats an all-zero accel vector as "IMU not
  // attached" and shows NO SIGNAL instead of a fake flat trace.
  float accel_x = 0.0f, accel_y = 0.0f, accel_z = 0.0f;
  float gyro_x  = 0.0f, gyro_y  = 0.0f, gyro_z  = 0.0f;
  float battery_voltage = 0.0f;
  float gnss_lat = 0.0f, gnss_lon = 0.0f, gnss_alt = 0.0f;
  uint8_t gnss_sats = 0;
  bool gnss_fix = false;
  uint32_t gnss_time_cs = 0;   // 00:00:00.00 placeholder until real GPS

  packetCount++;

  // TODO: drive this from a real flight-state machine (altitude/accel
  // thresholds). Hardcoded for now, same as the previous firmware.
  uint8_t stateCode = ST_PRE_LAUNCH;

  TelemetryFrame f;
  f.mission_time_ms  = nowMs;
  f.packet_count      = (uint16_t)(packetCount & 0xFFFF);
  f.status             = packStatus(stateCode, gnss_fix, gnss_sats);
  f.altitude_dm        = (int16_t)constrain(lroundf(altitudeM * 10.0f), -32768, 32767);
  f.pressure_pa        = (uint32_t)lroundf(pressurePa);
  f.temperature_cdeg   = (int16_t)constrain(lroundf(temperatureC * 100.0f), -32768, 32767);
  f.battery_mv         = (uint16_t)constrain(lroundf(battery_voltage * 1000.0f), 0, 65535);
  f.gnss_time_cs        = gnss_time_cs;
  f.gnss_lat_e7         = (int32_t)lroundf(gnss_lat * 1e7f);
  f.gnss_lon_e7         = (int32_t)lroundf(gnss_lon * 1e7f);
  f.gnss_alt_dm         = (int16_t)constrain(lroundf(gnss_alt * 10.0f), -32768, 32767);
  f.accel_x_mg          = (int16_t)constrain(lroundf(accel_x * 1000.0f), -32768, 32767);
  f.accel_y_mg          = (int16_t)constrain(lroundf(accel_y * 1000.0f), -32768, 32767);
  f.accel_z_mg          = (int16_t)constrain(lroundf(accel_z * 1000.0f), -32768, 32767);
  f.gyro_x_cdps         = (int16_t)constrain(lroundf(gyro_x * 100.0f), -32768, 32767);
  f.gyro_y_cdps         = (int16_t)constrain(lroundf(gyro_y * 100.0f), -32768, 32767);
  f.gyro_z_cdps         = (int16_t)constrain(lroundf(gyro_z * 100.0f), -32768, 32767);

  uint8_t buf[sizeof(TelemetryFrame) + 2];
  memcpy(buf, &f, sizeof(TelemetryFrame));
  uint16_t crc = crc16_ccitt(buf, sizeof(TelemetryFrame));
  buf[sizeof(TelemetryFrame)]     = (uint8_t)(crc >> 8);
  buf[sizeof(TelemetryFrame) + 1] = (uint8_t)(crc & 0xFF);

  LoRa.beginPacket();
  LoRa.write(buf, sizeof(buf));
  LoRa.endPacket();   // blocking send; returns once airtime completes

  // Optional debug echo over USB -- comment out once confirmed working,
  // it costs nothing over the air (this only goes out USB, not LoRa).
  Serial.print("TX #"); Serial.print(packetCount);
  Serial.print("  "); Serial.print(sizeof(buf)); Serial.println(" bytes");
}
