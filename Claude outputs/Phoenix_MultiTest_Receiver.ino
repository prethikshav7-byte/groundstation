#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include <RadioLib.h>

// =====================================================
// LORA
// =====================================================

SX1276 radio = new Module(10, 2, 9, 3);

// =====================================================
// PCA9685
// =====================================================

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// =====================================================
// SERVO CHANNELS
// =====================================================

#define DOOR_SERVO        0
#define CANSAT_SERVO      1
#define SEPARATION_SERVO  2

// =====================================================
// SERVO SETTINGS
// =====================================================

#define SERVO_MIN 150
#define SERVO_MAX 600

#define LOCK_ANGLE 0

// =====================================================
// FUNCTION: MOVE SERVO
// =====================================================

void moveServo(uint8_t channel, int angle) {

  angle = constrain(angle, 0, 180);

  int pulse = map(
    angle,
    0,
    180,
    SERVO_MIN,
    SERVO_MAX
  );

  pwm.setPWM(channel, 0, pulse);

  Serial.print("CH");
  Serial.print(channel);

  Serial.print(" -> ");
  Serial.print(angle);

  Serial.println(" degrees");
}


// =====================================================
// LOCK ALL
// =====================================================

void lockAll() {

  Serial.println("LOCKING ALL SERVOS");

  moveServo(DOOR_SERVO, LOCK_ANGLE);
  moveServo(CANSAT_SERVO, LOCK_ANGLE);
  moveServo(SEPARATION_SERVO, LOCK_ANGLE);
}


// =====================================================
// LOCK DOOR
// =====================================================

void lockDoor() {

  Serial.println("LOCK DOOR");

  moveServo(DOOR_SERVO, LOCK_ANGLE);
}


// =====================================================
// LOCK CANSAT
// =====================================================

void lockCanSat() {

  Serial.println("LOCK CANSAT");

  moveServo(CANSAT_SERVO, LOCK_ANGLE);
}


// =====================================================
// LOCK SEPARATION
// =====================================================

void lockSeparation() {

  Serial.println("LOCK SEPARATION");

  moveServo(SEPARATION_SERVO, LOCK_ANGLE);
}


// =====================================================
// DOOR ACTUATION
// =====================================================

void actuateDoor(int angle) {

  Serial.print("DOOR ACTUATE: ");
  Serial.print(angle);
  Serial.println(" degrees");

  moveServo(DOOR_SERVO, angle);
}


// =====================================================
// CANSAT ACTUATION
// =====================================================

void actuateCanSat(int angle) {

  Serial.print("CANSAT ACTUATE: ");
  Serial.print(angle);
  Serial.println(" degrees");

  moveServo(CANSAT_SERVO, angle);
}


// =====================================================
// SEPARATION ACTUATION
// =====================================================

void actuateSeparation(int angle) {

  Serial.print("SEPARATION ACTUATE: ");
  Serial.print(angle);
  Serial.println(" degrees");

  moveServo(SEPARATION_SERVO, angle);
}


// =====================================================
// PARSE ANGLE COMMAND
// =====================================================

bool getAngle(String command, String device, int &angle) {

  String prefix = device + ":";

  if (!command.startsWith(prefix)) {
    return false;
  }

  String value = command.substring(prefix.length());

  angle = value.toInt();

  if (
    angle == 30 ||
    angle == 60 ||
    angle == 90
  ) {
    return true;
  }

  return false;
}


// =====================================================
// PROCESS COMMAND
// =====================================================

void processCommand(String command) {

  command.trim();
  command.toUpperCase();

  Serial.println();
  Serial.print("RX << ");
  Serial.println(command);

  int angle;


  // ===================================================
  // DOOR
  // ===================================================

  if (getAngle(command, "DOOR", angle)) {

    actuateDoor(angle);
    return;
  }


  // ===================================================
  // CANSAT
  // ===================================================

  if (getAngle(command, "CANSAT", angle)) {

    actuateCanSat(angle);
    return;
  }


  // ===================================================
  // SEPARATION
  // ===================================================

  if (getAngle(command, "SEPARATION", angle)) {

    actuateSeparation(angle);
    return;
  }


  // ===================================================
  // LOCK DOOR
  // ===================================================

  if (command == "LOCK:DOOR") {

    lockDoor();
    return;
  }


  // ===================================================
  // LOCK CANSAT
  // ===================================================

  if (command == "LOCK:CANSAT") {

    lockCanSat();
    return;
  }


  // ===================================================
  // LOCK SEPARATION
  // ===================================================

  if (command == "LOCK:SEPARATION") {

    lockSeparation();
    return;
  }


  // ===================================================
  // LOCK ALL
  // ===================================================

  if (
    command == "LOCK:ALL" ||
    command == "RESET" ||
    command == "TEST4"
  ) {

    lockAll();
    return;
  }


  // ===================================================
  // TEST1 - DOOR
  // ===================================================

  if (command == "TEST1") {

    Serial.println("TEST1: DOOR");

    lockDoor();

    delay(1000);

    actuateDoor(60);

    return;
  }


  // ===================================================
  // TEST2 - CANSAT
  // ===================================================

  if (command == "TEST2") {

    Serial.println("TEST2: CANSAT");

    lockCanSat();

    delay(1000);

    actuateCanSat(60);

    return;
  }


  // ===================================================
  // TEST3 - SEPARATION
  // ===================================================

  if (command == "TEST3") {

    Serial.println("TEST3: SEPARATION");

    lockSeparation();

    delay(1000);

    actuateSeparation(60);

    return;
  }


  // ===================================================
  // TEST5
  // ===================================================

  if (command == "TEST5") {

    Serial.println("TEST5: MANUAL SEQUENCE");

    lockAll();

    delay(1000);

    Serial.println("Door actuating...");

    actuateDoor(60);

    Serial.println("Send NEXT for CanSat.");

    return;
  }


  // ===================================================
  // NEXT
  // ===================================================

  if (command == "NEXT") {

    Serial.println("NEXT COMMAND RECEIVED.");

    Serial.println(
      "For individual actuator testing, "
      "use DOOR:30/60/90, "
      "CANSAT:30/60/90 or "
      "SEPARATION:30/60/90."
    );

    return;
  }


  // ===================================================
  // TEST6
  // ===================================================

  if (command == "TEST6") {

    Serial.println("TEST6: AUTOMATIC BENCH SEQUENCE");

    lockAll();

    delay(1000);

    Serial.println("Door -> 60 degrees");

    actuateDoor(60);

    delay(3000);

    Serial.println("CanSat -> 60 degrees");

    actuateCanSat(60);

    delay(3000);

    Serial.println("Separation -> 60 degrees");

    actuateSeparation(60);

    Serial.println("TEST6 COMPLETE.");

    return;
  }


  // ===================================================
  // INVALID COMMAND
  // ===================================================

  Serial.println("INVALID COMMAND.");

  Serial.println(
    "Valid angles: 30, 60, 90"
  );
}


// =====================================================
// SETUP
// =====================================================

void setup() {

  Serial.begin(115200);

  delay(2000);

  Serial.println();
  Serial.println("========================================");
  Serial.println(" PHOENIX dB.V1 LoRa SERVO RECEIVER");
  Serial.println(" PCA9685 - 3 SERVO BENCH TEST");
  Serial.println("========================================");


  // ===================================================
  // PCA9685 INITIALIZATION
  // ===================================================

  Wire.begin();

  pwm.begin();

  pwm.setOscillatorFrequency(27000000);

  pwm.setPWMFreq(50);

  delay(500);

  Serial.println("PCA9685 initialized.");


  // ===================================================
  // LOCK ALL SERVOS AT STARTUP
  // ===================================================

  lockAll();

  delay(500);


  // ===================================================
  // LORA INITIALIZATION
  // ===================================================

  int state = radio.begin(
    867.0,
    125.0,
    10,
    5,
    0x12,
    17,
    8,
    0
  );


  if (state != RADIOLIB_ERR_NONE) {

    Serial.print("LoRa INIT FAILED. ERROR: ");
    Serial.println(state);

    while (true) {
      delay(1000);
    }
  }


  radio.setCRC(true);

  Serial.println("LoRa initialized successfully.");

  Serial.println();
  Serial.println("SYSTEM READY.");
  Serial.println("ALL SERVOS LOCKED.");
  Serial.println();
}


// =====================================================
// LOOP
// =====================================================

void loop() {

  String received;

  int state = radio.receive(received);

  if (state == RADIOLIB_ERR_NONE) {

    Serial.println();
    Serial.println("--------------------------------");

    Serial.print("PACKET RECEIVED: ");
    Serial.println(received);

    Serial.print("RSSI: ");
    Serial.print(radio.getRSSI());
    Serial.println(" dBm");

    Serial.print("SNR: ");
    Serial.print(radio.getSNR());
    Serial.println(" dB");

    Serial.println("--------------------------------");

    processCommand(received);

    Serial.println();
  }

  else if (state == RADIOLIB_ERR_RX_TIMEOUT) {

    // Normal timeout - continue listening
  }

  else {

    Serial.print("RX ERROR: ");
    Serial.println(state);
  }
}
