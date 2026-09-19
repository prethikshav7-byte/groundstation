#include <RadioLib.h>

// =====================================================
// PHOENIX dB.V1
// SERVO SEQUENCE COMMAND TRANSMITTER
//
// Ground Station USB Serial -> Teensy 4.1 -> RA-01H LoRa TX
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

void setup()
{
  Serial.begin(115200);
  delay(2000);

  Serial.println();
  Serial.println("======================================");
  Serial.println("       PHOENIX dB.V1");
  Serial.println(" SERVO SEQUENCE COMMAND TRANSMITTER");
  Serial.println("======================================");

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

  Serial.println("RA-01H initialized.");
  Serial.println();
  Serial.println("Available commands:");
  Serial.println("START");
  Serial.println("CONFIRM");
  Serial.println("RESET");
}

void loop()
{
  if (Serial.available())
  {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (
      command == "START" ||
      command == "CONFIRM" ||
      command == "RESET"
    )
    {
      Serial.print("Sending: ");
      Serial.println(command);

      int state = radio.transmit(command);

      if (state == RADIOLIB_ERR_NONE)
      {
        Serial.println("TX SUCCESS");
      }
      else
      {
        Serial.print("TX FAILED: ");
        Serial.println(state);
      }
    }
    else if (command.length() > 0)
    {
      Serial.println("Invalid command.");
      Serial.println("Use:");
      Serial.println("START");
      Serial.println("CONFIRM");
      Serial.println("RESET");
    }
  }
}
