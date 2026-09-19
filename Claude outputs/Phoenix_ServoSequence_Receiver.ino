#include <RadioLib.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

// =====================================================
// PHOENIX dB.V1
// THREE-SERVO SEQUENCE TEST
//
// RA-01H -> Teensy 4.1 -> PCA9685 -> 3 Servos
//
// CH0 = DOOR
// CH1 = CANSAT DEPLOYMENT
// CH2 = SEPARATION
//
// BENCH TEST VERSION
// =====================================================

// =====================================================
// RA-01H PINS
// =====================================================

#define LORA_CS      10
#define LORA_DIO0     2
#define LORA_RST      9
#define LORA_DIO1     3

SX1276 radio = new Module(
  LORA_CS,
  LORA_DIO0,
  LORA_RST,
  LORA_DIO1
);

// =====================================================
// PCA9685
// =====================================================

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// =====================================================
// SERVO CHANNELS
// =====================================================

#define DOOR_SERVO       0
#define CANSAT_SERVO     1
#define SEPARATION_SERVO 2

// =====================================================
// SERVO CALIBRATION
// =====================================================

#define SERVO_MIN 150
#define SERVO_MAX 600
#define SERVO_FREQ 50

// =====================================================
// TEST ANGLES
// =====================================================

// CLOSED / SAFE POSITION
#define DOOR_CLOSED       0
#define DOOR_OPEN         60

#define CANSAT_STOWED     0
#define CANSAT_DEPLOY     60

#define SEPARATION_SAFE   0
#define SEPARATION_TEST   60

// =====================================================
// STATE MACHINE
// =====================================================

enum TestState
{
  IDLE,
  DOOR_STAGE,
  CANSAT_STAGE,
  SEPARATION_STAGE,
  COMPLETE
};

TestState testState = IDLE;

// =====================================================
// SERVO FUNCTION
// =====================================================

void moveServo(uint8_t channel, int angle)
{
  angle = constrain(angle, 0, 180);

  int pulse = map(
    angle,
    0,
    180,
    SERVO_MIN,
    SERVO_MAX
  );

  pwm.setPWM(
    channel,
    0,
    pulse
  );

  Serial.print("Channel ");
  Serial.print(channel);
  Serial.print(" -> ");
  Serial.print(angle);
  Serial.println(" degrees");
}

// =====================================================
// SAFE INITIAL POSITION
// =====================================================

void setAllServosSafe()
{
  moveServo(DOOR_SERVO, DOOR_CLOSED);
  moveServo(CANSAT_SERVO, CANSAT_STOWED);
  moveServo(SEPARATION_SERVO, SEPARATION_SAFE);
}

// =====================================================
// SETUP
// =====================================================

void setup()
{
  Serial.begin(115200);
  delay(2000);

  Serial.println();
  Serial.println("======================================");
  Serial.println("       PHOENIX dB.V1");
  Serial.println("    THREE SERVO TEST SYSTEM");
  Serial.println("======================================");

  // =================================================
  // PCA9685
  // =================================================

  Wire.begin();
  pwm.begin();
  pwm.setOscillatorFrequency(27000000);
  pwm.setPWMFreq(SERVO_FREQ);

  delay(500);

  Serial.println("PCA9685 initialized.");
  Serial.println();
  Serial.println("Setting all servos to SAFE position...");

  setAllServosSafe();

  Serial.println("All servos SAFE.");

  // =================================================
  // RA-01H
  // =================================================

  Serial.println();
  Serial.println("Initializing RA-01H...");

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

  if (state != RADIOLIB_ERR_NONE)
  {
    Serial.print("RA-01H INIT FAILED: ");
    Serial.println(state);

    while (true)
    {
      delay(1000);
    }
  }

  radio.setCRC(true);

  Serial.println("RA-01H initialized successfully.");
  Serial.println("--------------------------------------");
  Serial.println("Frequency : 867 MHz");
  Serial.println("BW        : 125 kHz");
  Serial.println("SF        : 10");
  Serial.println("CR        : 4/5");
  Serial.println("CRC       : ON");
  Serial.println("--------------------------------------");

  Serial.println();
  Serial.println("SYSTEM READY");
  Serial.println();
  Serial.println("Send:");
  Serial.println("START");
}

// =====================================================
// LOOP
// =====================================================

void loop()
{
  String command;
  int state = radio.receive(command);

  if (state != RADIOLIB_ERR_NONE)
  {
    return;
  }

  command.trim();

  Serial.println();
  Serial.println("======================================");
  Serial.println("COMMAND RECEIVED:");
  Serial.println(command);
  Serial.println("======================================");

  // =================================================
  // START
  // =================================================

  if (command == "START")
  {
    if (testState == IDLE)
    {
      Serial.println("STARTING SERVO TEST...");
      testState = DOOR_STAGE;

      Serial.println();
      Serial.println("STAGE 1: DOOR SERVO");
      Serial.println("Opening door...");

      moveServo(
        DOOR_SERVO,
        DOOR_OPEN
      );

      Serial.println();
      Serial.println("Door stage complete.");
      Serial.println("Send CONFIRM to continue.");
    }
    else
    {
      Serial.println("TEST ALREADY STARTED.");
    }
  }

  // =================================================
  // CONFIRM
  // =================================================

  else if (command == "CONFIRM")
  {
    // -----------------------------------------------
    // DOOR -> CANSAT
    // -----------------------------------------------

    if (testState == DOOR_STAGE)
    {
      Serial.println();
      Serial.println("STAGE 2: CANSAT DEPLOYMENT");
      Serial.println("Moving CanSat deployment servo...");

      moveServo(
        CANSAT_SERVO,
        CANSAT_DEPLOY
      );

      Serial.println();
      Serial.println("CanSat deployment stage complete.");
      testState = CANSAT_STAGE;
      Serial.println("Send CONFIRM to continue.");
    }

    // -----------------------------------------------
    // CANSAT -> SEPARATION
    // -----------------------------------------------

    else if (testState == CANSAT_STAGE)
    {
      Serial.println();
      Serial.println("STAGE 3: SEPARATION SERVO");
      Serial.println("Moving separation servo...");

      moveServo(
        SEPARATION_SERVO,
        SEPARATION_TEST
      );

      Serial.println();
      Serial.println("Separation servo test complete.");
      testState = SEPARATION_STAGE;
      Serial.println("Send CONFIRM to finish test.");
    }

    // -----------------------------------------------
    // COMPLETE
    // -----------------------------------------------

    else if (testState == SEPARATION_STAGE)
    {
      Serial.println();
      Serial.println("======================================");
      Serial.println("      SERVO TEST COMPLETE");
      Serial.println("======================================");
      testState = COMPLETE;
    }
    else
    {
      Serial.println("CONFIRM NOT EXPECTED.");
    }
  }

  // =================================================
  // RESET
  // =================================================

  else if (command == "RESET")
  {
    Serial.println("RESETTING SERVO TEST...");
    setAllServosSafe();
    testState = IDLE;

    Serial.println("All servos returned to SAFE position.");
    Serial.println();
    Serial.println("SYSTEM READY");
    Serial.println("Send START.");
  }

  // =================================================
  // UNKNOWN COMMAND
  // =================================================

  else
  {
    Serial.println("UNKNOWN COMMAND.");
    Serial.println("Valid commands:");
    Serial.println("START");
    Serial.println("CONFIRM");
    Serial.println("RESET");
  }

  Serial.println();
}
