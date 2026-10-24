"""Hard policy for Hantek bench work."""

POLICY_TEXT = """\
Hantek 6022BL navigator — bench instrument policy
=================================================

This tool talks to a USB oscilloscope / logic analyzer on the engineering PC.
It is NOT a PLC, VLT, or machine-control interface.

Allowed
-------
- Probe USB mode (scope vs LA), driver health, sigrok-cli presence
- Capture analog CH1/CH2 and export CSV / measure JSON
- Capture digital LA (fx2lafw) when H/P is released
- Plot and document cal / bench waveforms under %LOCALAPPDATA%/hantek-bench/captures/ (or HTK_ARTIFACT_DIR)

Not allowed
-----------
- Bypass E-stops, light curtains, door switches, or safety relays
- Force PLC bits, download ladder, or write VLT parameters via this tool
- Use the scope ground clip as a substitute for proper differential /
  isolated measurement practice on mains or floating circuits

H/P button (physical)
---------------------
- Pressed (P): oscilloscope mode (USB 04b4/04b5:602a) — keep for cal tests
- Released: logic analyzer mode (USB 0925:3881) — unplug, switch, replug
- Not an MSO: scope OR LA, never both at once

Probe attenuation
-----------------
Physical probe switch (x1/x10) MUST match the --probe-factor argument.
Cal terminal: use x1. High voltage: use x10 and respect probe ratings.

Plant measurements
------------------
LOTO before opening cabinets or probing live power circuits. Confirm
ground reference. Prefer floating / differential methods for non-grounded
nodes. Mark unverified amplitudes as TODO — never invent nameplates.
"""
