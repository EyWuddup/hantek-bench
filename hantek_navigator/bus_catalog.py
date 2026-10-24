"""Industrial / SCADA / fieldbus catalog for the bench UI."""

from __future__ import annotations

from typing import Any

# scope = analog/threshold with 1–2 probes; la = needs H/P released; both = either
BUSES: list[dict[str, Any]] = [
    {
        "id": "rs232",
        "name": "RS-232 / UART",
        "modes": ["scope", "la"],
        "wiring": ["single"],
        "hint": "Single-ended TX or RX to CH1 (GND shared). Typical ±3..15 V — use ×10 probe on plant.",
    },
    {
        "id": "rs485",
        "name": "RS-485 (2-wire)",
        "modes": ["scope", "la"],
        "wiring": ["differential"],
        "hint": "CH1=A, CH2=B (or vice versa). Decode A−B. Half-duplex shared pair.",
    },
    {
        "id": "rs422",
        "name": "RS-422 (4-wire)",
        "modes": ["scope", "la"],
        "wiring": ["differential"],
        "hint": "Only one differential pair at a time (TX or RX) with 2 scope channels.",
    },
    {
        "id": "modbus",
        "name": "Modbus RTU",
        "modes": ["scope", "la"],
        "wiring": ["single", "differential"],
        "hint": "UART framing over RS-232/485. Scope: bytes; LA+sigrok: modbus decoder.",
    },
    {
        "id": "can",
        "name": "CAN / CANopen",
        "modes": ["scope", "la"],
        "wiring": ["differential"],
        "hint": "Scope: CANH/CANL eye. LA: full can decoder. Watch common-mode / termination.",
    },
    {
        "id": "lin",
        "name": "LIN",
        "modes": ["scope", "la"],
        "wiring": ["single"],
        "hint": "Single-wire ~12 V automotive. Prefer ×10. LA has lin decoder.",
    },
    {
        "id": "dali",
        "name": "DALI",
        "modes": ["scope", "la"],
        "wiring": ["differential", "single"],
        "hint": "≈1200 baud Manchester-ish / UART-family. Isolated bus — mind polarity & supply.",
    },
    {
        "id": "dmx",
        "name": "DMX512",
        "modes": ["scope", "la"],
        "wiring": ["differential"],
        "hint": "RS-485 physical, 250 kbaud. Needs fast sample rate (≥1–2 MS/s).",
    },
    {
        "id": "i2c",
        "name": "I²C",
        "modes": ["la"],
        "wiring": ["digital"],
        "hint": "Needs LA mode (H/P out): SDA+SCL on digital channels.",
    },
    {
        "id": "spi",
        "name": "SPI",
        "modes": ["la"],
        "wiring": ["digital"],
        "hint": "Needs LA mode: CLK/MOSI/MISO/CS.",
    },
    {
        "id": "profibus",
        "name": "PROFIBUS DP",
        "modes": ["la", "scope"],
        "wiring": ["differential"],
        "hint": "RS-485 phy. Scope: signal integrity / A−B. Full protocol: LA or specialized tool.",
    },
    {
        "id": "profinet",
        "name": "PROFINET / Ethernet",
        "modes": [],
        "wiring": [],
        "hint": "Not a 2-ch USB scope job — use managed switch mirror / Wireshark / industrial Ethernet TAP.",
    },
    {
        "id": "modbus-tcp",
        "name": "Modbus TCP",
        "modes": [],
        "wiring": [],
        "hint": "Ethernet — capture on PC/NIC, not Hantek analog.",
    },
    {
        "id": "estop",
        "name": "E-stop / safety chain",
        "modes": ["scope"],
        "wiring": ["single"],
        "hint": "Measure 24 V chain continuity / bounce with ×10. Do NOT bypass safety. LOTO for intrusive work.",
    },
    {
        "id": "24v-digital",
        "name": "24 V digital I/O",
        "modes": ["scope", "la"],
        "wiring": ["single"],
        "hint": "PLC inputs/outputs. ×10 probe. Check 0 V reference.",
    },
    {
        "id": "analog-4-20",
        "name": "4–20 mA / 0–10 V",
        "modes": ["scope"],
        "wiring": ["single"],
        "hint": "Across shunt (mA) or signal–0 V. Slow timebase; measure DC + ripple/noise.",
    },
]


def catalog() -> list[dict[str, Any]]:
    return BUSES
