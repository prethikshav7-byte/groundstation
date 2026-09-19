#include <RadioLib.h>

// =============================
// LoRa MODULE
// =============================
SX1276 radio = new Module(10, 2, 9, 3);

// =============================
// SETUP
// =============================
void setup() {

  Serial.begin(115200);
  delay(2000);

  Serial.println();
  Serial.println("=================================");
  Serial.println(" PHOENIX dB.V1 LoRa TRANSMITTER");
  Serial.println(" SERVO BENCH TEST CONTROLLER");
  Serial.println("=================================");

  int state = radio.begin(
    867.0,    // Frequency MHz
    125.0,    // Bandwidth kHz
    10,       // Spreading Factor
    5,        // Coding Rate 4/5
    0x12,     // Sync Word
    17,       // TX Power dBm
    8,        // Preamble
    0         // Gain
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
  Serial.println("AVAILABLE COMMANDS:");
  Serial.println("---------------------------------");

  Serial.println("DOOR:30");
  Serial.println("DOOR:60");
  Serial.println("DOOR:90");

  Serial.println("CANSAT:30");
  Serial.println("CANSAT:60");
  Serial.println("CANSAT:90");

  Serial.println("SEPARATION:30");
  Serial.println("SEPARATION:60");
  Serial.println("SEPARATION:90");

  Serial.println("LOCK:DOOR");
  Serial.println("LOCK:CANSAT");
  Serial.println("LOCK:SEPARATION");
  Serial.println("LOCK:ALL");

  Serial.println("TEST1");
  Serial.println("TEST2");
  Serial.println("TEST3");
  Serial.println("TEST4");
  Serial.println("TEST5");
  Serial.println("NEXT");
  Serial.println("TEST6");
  Serial.println("RESET");

  Serial.println("---------------------------------");
  Serial.println("Enter command:");
}


// =============================
// SEND COMMAND
// =============================
void sendCommand(String command) {

  command.trim();

  if (command.length() == 0) {
    return;
  }

  Serial.print("TX >> ");
  Serial.println(command);

  int state = radio.transmit(command);

  if (state == RADIOLIB_ERR_NONE) {

    Serial.println("TX SUCCESS");

  } else {

    Serial.print("TX FAILED. ERROR: ");
    Serial.println(state);
  }

  Serial.println();
}


// =============================
// MAIN LOOP
// =============================
void loop() {

  if (Serial.available()) {

    String command = Serial.readStringUntil('\n');

    command.trim();

    sendCommand(command);
  }
}
