"""
MCP server for Hantek 6022BL navigator (Cursor).

JSON-first probe / capture / measure / cal-test / LA tools.
Bench instrument only — no PLC writes.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import measure as meas
from . import sigrok_bridge as bridge
from .live_la import LA
from .policy import POLICY_TEXT
from .usb_probe import probe as usb_probe

mcp = FastMCP("hantek-navigator")


def _j(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=True, indent=2)


@mcp.tool()
def htk_policy() -> str:
    """Return bench safety / H/P / probe policy for the Hantek 6022BL."""
    return POLICY_TEXT


@mcp.tool()
def htk_probe() -> str:
    """Detect USB mode (scope vs LA), WinUSB service, and sigrok-cli path/version."""
    return _j(usb_probe().to_dict())


@mcp.tool()
def htk_doctor() -> str:
    """Run USB + sigrok checklist and a short scope/LA smoke capture for the current mode."""
    return _j(bridge.doctor())


@mcp.tool()
def htk_scope_capture(
    samples: str = "20k",
    samplerate: str = "1m",
    channels: str = "CH1",
    vdiv: str = "1V",
    probe_factor: float = 1.0,
    measure: bool = False,
    out: str | None = None,
) -> str:
    """
    Capture analog channels via sigrok hantek-6xxx to CSV.
    probe_factor must match the physical probe switch (1 or 10).
    Requires H/P pressed (scope mode).
    """
    r = bridge.scope_capture(
        samples=samples,
        samplerate=samplerate,
        channels=channels,
        vdiv=vdiv,
        probe_factor=probe_factor,
        measure=measure,
        out=Path(out) if out else None,
    )
    return _j(r.to_dict())


@mcp.tool()
def htk_scope_measure(
    samples: str = "20k",
    samplerate: str = "1m",
    channels: str = "CH1",
    vdiv: str = "1V",
    probe_factor: float = 1.0,
    out: str | None = None,
) -> str:
    """
    Capture + AI-friendly measurements (Vpp, freq, duty, RMS, rise/fall).
    Default tool for reading the cal terminal or a signal under test.
    """
    r = bridge.scope_measure(
        samples=samples,
        samplerate=samplerate,
        channels=channels,
        vdiv=vdiv,
        probe_factor=probe_factor,
        out=Path(out) if out else None,
    )
    return _j(r.to_dict())


@mcp.tool()
def htk_cal_test(
    repeat: int = 10,
    probe_factor: float = 1.0,
    samplerate: str = "1m",
    samples: str = "20k",
    vdiv: str = "1V",
    channel: str = "CH1",
) -> str:
    """
    Stability test against the 1 kHz probe-calibration terminal.
    Expects ~1 kHz, ~50% duty; documents as-found Vpp (does not invent amplitude).
    Keep H/P pressed; probe physical switch x1 with probe_factor=1.
    """
    return _j(
        bridge.scope_cal_test(
            repeat=repeat,
            probe_factor=probe_factor,
            samplerate=samplerate,
            samples=samples,
            vdiv=vdiv,
            channel=channel,
        )
    )


@mcp.tool()
def htk_la_show() -> str:
    """fx2lafw --show + USB probe (LA mode)."""
    return _j(bridge.la_show())


@mcp.tool()
def htk_la_capture(
    samples: str = "100k",
    samplerate: str = "6m",
    channels: str = "D0,D1,D2,D3,D4,D5,D6,D7",
    time_ms: int | None = None,
    trigger: str | None = None,
    wait_trigger: bool = False,
    measure: bool = True,
    out: str | None = None,
) -> str:
    """
    Logic analyzer capture via fx2lafw (8 channels) + digital measurements.
    Requires H/P RELEASED (mode=la). trigger e.g. 'D0=r'. Not an MSO.
    """
    r = bridge.la_capture(
        samples=samples,
        samplerate=samplerate,
        channels=channels,
        time_ms=time_ms,
        trigger=trigger,
        wait_trigger=wait_trigger,
        measure=measure,
        out=Path(out) if out else None,
    )
    out_d = r.to_dict()
    if r.ok and r.path and measure:
        out_d["summary"] = meas.measure_la_csv(Path(r.path))
    return _j(out_d)


@mcp.tool()
def htk_la_measure(
    samples: str = "100k",
    samplerate: str = "6m",
    channels: str = "D0,D1,D2,D3,D4,D5,D6,D7",
    time_ms: int | None = None,
    trigger: str | None = None,
    wait_trigger: bool = False,
    out: str | None = None,
) -> str:
    """
    LA capture + structured digital metrics (freq, duty, pulse width, edges per Dn).
    Preferred AI tool for reading digital activity in LA mode.
    """
    return _j(
        bridge.la_measure(
            samples=samples,
            samplerate=samplerate,
            channels=channels,
            time_ms=time_ms,
            trigger=trigger,
            wait_trigger=wait_trigger,
            out=Path(out) if out else None,
        )
    )


@mcp.tool()
def htk_la_decode(
    protocol: str,
    samples: str = "100k",
    samplerate: str = "6m",
    channels: str = "D0,D1,D2,D3,D4,D5,D6,D7",
    channel_map: str | None = None,
    time_ms: int | None = None,
    trigger: str | None = None,
    wait_trigger: bool = False,
) -> str:
    """
    LA capture with sigrok protocol decoder.
    protocol: bare id (uart/i2c/spi/can/…) or full -P string (i2c:scl=D0:sda=D1).
    channel_map: optional 'rx=D0,tx=D1' / 'scl=D0:sda=D1'.
    """
    return _j(
        bridge.la_decode(
            protocol=protocol,
            samples=samples,
            samplerate=samplerate,
            channels=channels,
            channel_map=channel_map,
            time_ms=time_ms,
            trigger=trigger,
            wait_trigger=wait_trigger,
        )
    )


@mcp.tool()
def htk_la_autoset() -> str:
    """
    Discover digital activity and set rate/window/trigger for detail view.
    Starts rolling LA live. Requires LA mode.
    """
    return _j(LA.autoset())


@mcp.tool()
def htk_la_live_status() -> str:
    """Status of rolling LA live streamer."""
    return _j(LA.snapshot_status())


@mcp.tool()
def htk_measure_file(csv_path: str, probe_factor: float = 1.0) -> str:
    """Measure an existing sigrok CSV capture on disk (analog or LA)."""
    p = Path(csv_path)
    # Heuristic: LA files often have 'logic' columns / la_ prefix
    try:
        names, _, _, _ = meas.load_sigrok_csv(p)
        if names and all(n.upper().startswith("D") or n.upper() == "LOGIC" for n in names):
            return _j(meas.measure_la_csv(p))
    except Exception:
        pass
    return _j(meas.measure_csv(p, probe_factor=probe_factor).to_dict())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
