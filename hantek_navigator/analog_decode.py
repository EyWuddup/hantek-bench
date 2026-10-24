"""Threshold / differential decode from scope analog captures (2 probe ports)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np


@dataclass
class UartFrame:
    t_start: float
    bits: str
    value: int | None
    ascii: str | None
    ok: bool
    note: str = ""


@dataclass
class DecodeResult:
    bus: str
    mode: str
    samplerate_hz: float
    threshold_v: float
    idle_high: bool
    baud_est: float | None
    frames: list[UartFrame] = field(default_factory=list)
    hex_bytes: list[str] = field(default_factory=list)
    text: str = ""
    notes: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def differential(ch_a: np.ndarray, ch_b: np.ndarray) -> np.ndarray:
    """A−B for RS485 / RS422-style pairs (2-wire or one pair of 4-wire)."""
    n = min(ch_a.size, ch_b.size)
    return ch_a[:n].astype(np.float64) - ch_b[:n].astype(np.float64)


def auto_threshold(y: np.ndarray) -> float:
    return float(0.5 * (np.min(y) + np.max(y)))


def to_bits(y: np.ndarray, threshold: float, idle_high: bool = True) -> np.ndarray:
    """Return digital levels as 0/1 with 1 = idle/mark if idle_high."""
    raw = y >= threshold
    return raw.astype(np.uint8) if idle_high else (~raw).astype(np.uint8)


def estimate_baud(bits: np.ndarray, samplerate_hz: float) -> float | None:
    if bits.size < 16 or samplerate_hz <= 0:
        return None
    edges = np.where(bits[1:] != bits[:-1])[0]
    if edges.size < 4:
        return None
    widths = np.diff(edges)
    widths = widths[widths > 1]
    if widths.size == 0:
        return None
    # shortest pulse ≈ 1 bit time
    bit_samples = float(np.median(np.sort(widths)[: max(3, widths.size // 4)]))
    if bit_samples <= 0:
        return None
    baud = samplerate_hz / bit_samples
    # snap to common rates
    commons = [
        300,
        600,
        1200,
        2400,
        4800,
        9600,
        19200,
        38400,
        57600,
        115200,
        230400,
        250000,
        500000,
        1000000,
    ]
    return float(min(commons, key=lambda b: abs(b - baud)))


def decode_uart(
    y: np.ndarray,
    samplerate_hz: float,
    *,
    baud: float | None = None,
    threshold: float | None = None,
    idle_high: bool = True,
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: float = 1.0,
    bus_label: str = "uart",
) -> DecodeResult:
    thr = auto_threshold(y) if threshold is None else float(threshold)
    bits = to_bits(y, thr, idle_high=idle_high)
    baud_est = baud or estimate_baud(bits, samplerate_hz)
    notes: list[str] = []
    if baud_est is None:
        return DecodeResult(
            bus=bus_label,
            mode="analog_threshold",
            samplerate_hz=samplerate_hz,
            threshold_v=thr,
            idle_high=idle_high,
            baud_est=None,
            notes=["Could not estimate baud — set baud manually or check wiring/idle level"],
            stats={"n": int(y.size), "vmin": float(np.min(y)), "vmax": float(np.max(y))},
        )

    spb = samplerate_hz / baud_est  # samples per bit
    frames: list[UartFrame] = []
    hex_bytes: list[str] = []
    chars: list[str] = []
    i = 0
    n = bits.size
    idle = 1
    while i < n - int(spb * (1 + data_bits + 2)):
        # find start bit (idle→0)
        if bits[i] != idle:
            i += 1
            continue
        # look for falling edge
        if i + 1 < n and bits[i] == idle and bits[i + 1] == 0:
            start = i + 1
            # sample mid-bit
            mid0 = start + int(0.5 * spb)
            if mid0 >= n or bits[mid0] != 0:
                i += 1
                continue
            bitvals: list[int] = []
            ok = True
            note = ""
            for b in range(data_bits):
                idx = start + int((1.5 + b) * spb)
                if idx >= n:
                    ok = False
                    note = "truncated"
                    break
                bitvals.append(int(bits[idx]))
            if not ok:
                i = start + 1
                continue
            value = 0
            for k, bv in enumerate(bitvals):
                value |= bv << k
            # parity / stop ignored lightly
            stop_idx = start + int((1.5 + data_bits) * spb)
            if stop_idx < n and bits[stop_idx] != idle:
                ok = False
                note = "bad_stop"
            ch = None
            if 32 <= value < 127:
                ch = chr(value)
                chars.append(ch)
            elif ok:
                chars.append(".")
            hx = f"{value:02X}"
            hex_bytes.append(hx)
            frames.append(
                UartFrame(
                    t_start=start / samplerate_hz,
                    bits="".join(str(b) for b in bitvals),
                    value=value,
                    ascii=ch,
                    ok=ok,
                    note=note,
                )
            )
            i = start + int((1 + data_bits + stop_bits) * spb)
            continue
        i += 1

    if parity != "none":
        notes.append(f"parity={parity} not strictly validated in v1")
    notes.append(
        "Scope-mode UART decode is threshold-based (not a hardware UART). "
        "For RS485 use differential A−B; for 4-wire only one pair at a time (2 channels)."
    )
    return DecodeResult(
        bus=bus_label,
        mode="analog_threshold",
        samplerate_hz=samplerate_hz,
        threshold_v=thr,
        idle_high=idle_high,
        baud_est=baud_est,
        frames=frames[:500],
        hex_bytes=hex_bytes[:500],
        text="".join(chars[:500]),
        notes=notes,
        stats={
            "n": int(y.size),
            "frames": len(frames),
            "vmin": float(np.min(y)),
            "vmax": float(np.max(y)),
            "vpp": float(np.max(y) - np.min(y)),
            "samples_per_bit": spb,
        },
    )


def decode_bus(
    ch1: np.ndarray,
    ch2: np.ndarray | None,
    samplerate_hz: float,
    *,
    bus: str,
    wiring: str = "single",  # single | differential
    channel: str = "CH1",
    baud: float | None = None,
    threshold: float | None = None,
    idle_high: bool = True,
) -> DecodeResult:
    bus_l = bus.lower().strip()
    if wiring == "differential":
        if ch2 is None:
            return DecodeResult(
                bus=bus_l,
                mode="analog_threshold",
                samplerate_hz=samplerate_hz,
                threshold_v=0.0,
                idle_high=idle_high,
                baud_est=None,
                notes=["Differential decode needs CH1 and CH2 (A and B)."],
            )
        y = differential(ch1, ch2)
        label = f"{bus_l}:diff(CH1-CH2)"
    else:
        y = ch1 if channel.upper() != "CH2" else (ch2 if ch2 is not None else ch1)
        label = f"{bus_l}:{channel}"

    # Map industrial aliases onto UART-family decode for scope mode
    uart_family = {
        "uart",
        "rs232",
        "rs-232",
        "rs485",
        "rs-485",
        "rs422",
        "rs-422",
        "modbus",
        "modbus-rtu",
        "dali",
        "dmx",
        "dmx512",
        "lin",
        "ascii",
    }
    if bus_l in uart_family or bus_l.startswith("modbus"):
        # DALI / DMX are special baud
        if baud is None:
            if bus_l in {"dali"}:
                baud = 1200.0
            elif bus_l in {"dmx", "dmx512"}:
                baud = 250000.0
        return decode_uart(
            y,
            samplerate_hz,
            baud=baud,
            threshold=threshold,
            idle_high=idle_high,
            bus_label=label,
        )

    if bus_l in {"can", "can-hs", "canbus"}:
        # Analog eye / edge stats only in scope mode — full CAN in LA
        thr = auto_threshold(y) if threshold is None else float(threshold)
        bits = to_bits(y, thr, idle_high=False)
        edges = np.where(bits[1:] != bits[:-1])[0]
        return DecodeResult(
            bus=label,
            mode="analog_can_preview",
            samplerate_hz=samplerate_hz,
            threshold_v=thr,
            idle_high=False,
            baud_est=None,
            notes=[
                "CAN in scope mode: differential preview only (hook CANH/CANL on CH1/CH2).",
                "Full CAN frame decode requires LA mode (release H/P) or a dedicated CAN tool.",
            ],
            stats={
                "edges": int(edges.size),
                "vmin": float(np.min(y)),
                "vmax": float(np.max(y)),
                "vpp": float(np.max(y) - np.min(y)),
            },
        )

    return DecodeResult(
        bus=label,
        mode="unsupported_scope",
        samplerate_hz=samplerate_hz,
        threshold_v=threshold or 0.0,
        idle_high=idle_high,
        baud_est=None,
        notes=[
            f"Bus '{bus}' needs LA mode digital decode (toggle H/P) or is not yet mapped for analog.",
            "Supported scope-mode: UART/RS232/RS485/RS422/Modbus RTU/DALI/DMX (+ CAN eye preview).",
        ],
    )
