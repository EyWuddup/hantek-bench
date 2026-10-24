/*
  Hantek 6022BL LA loopback bench sketch (Arduino Uno).

  Expected wiring (LA pin → Uno):
    GND → GND
    D0  → 5V     (constant HIGH)
    D1  → 3.3V   (constant HIGH)
    D2  → D2     (UART TX 9600 8N1, idle HIGH)
    D3  → D3     (~1 kHz square)
    D4  → D4     (~250 Hz square, phase marker)

  Payload every 50 ms: "BMI-LA\r\n" on D2 (known ASCII for sigrok uart decode).
*/

#include <Arduino.h>

static const uint8_t PIN_UART_TX = 2;
static const uint8_t PIN_SQ_1K = 3;
static const uint8_t PIN_SQ_250 = 4;
static const uint32_t UART_BAUD = 9600;
static const char PAYLOAD[] = "BMI-LA\r\n";

// Bit-bang UART TX (idle high). Avoids SoftSerial timer conflicts with millis toggles.
static void uartWriteByte(uint8_t b) {
  const uint16_t bit_us = (uint16_t)(1000000UL / UART_BAUD);
  noInterrupts();
  digitalWrite(PIN_UART_TX, LOW);  // start
  delayMicroseconds(bit_us);
  for (uint8_t i = 0; i < 8; i++) {
    digitalWrite(PIN_UART_TX, (b >> i) & 0x01);
    delayMicroseconds(bit_us);
  }
  digitalWrite(PIN_UART_TX, HIGH);  // stop
  delayMicroseconds(bit_us);
  interrupts();
}

static void uartWrite(const char *s) {
  while (*s) {
    uartWriteByte((uint8_t)*s++);
  }
}

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  pinMode(PIN_UART_TX, OUTPUT);
  pinMode(PIN_SQ_1K, OUTPUT);
  pinMode(PIN_SQ_250, OUTPUT);
  digitalWrite(PIN_UART_TX, HIGH);  // UART idle
  digitalWrite(PIN_SQ_1K, LOW);
  digitalWrite(PIN_SQ_250, LOW);

  Serial.begin(115200);
  Serial.println(F("la_loopback: D2=UART9600 'BMI-LA', D3=1kHz, D4=250Hz"));
}

void loop() {
  static uint32_t t_uart = 0;
  static uint32_t t_1k = 0;
  static uint32_t t_250 = 0;
  static uint32_t t_led = 0;
  const uint32_t now = micros();

  // ~1 kHz on D3 (500 us half-period)
  if ((uint32_t)(now - t_1k) >= 500) {
    t_1k = now;
    digitalWrite(PIN_SQ_1K, !digitalRead(PIN_SQ_1K));
  }

  // ~250 Hz on D4 (2000 us half-period)
  if ((uint32_t)(now - t_250) >= 2000) {
    t_250 = now;
    digitalWrite(PIN_SQ_250, !digitalRead(PIN_SQ_250));
  }

  // UART burst every 50 ms
  if ((uint32_t)(now - t_uart) >= 50000UL) {
    t_uart = now;
    uartWrite(PAYLOAD);
  }

  // Heartbeat LED ~2 Hz
  if ((uint32_t)(now - t_led) >= 250000UL) {
    t_led = now;
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
  }
}
