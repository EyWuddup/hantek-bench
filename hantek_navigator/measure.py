"""Waveform measurements from sigrok CSV exports."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class ChannelMeas:
    name: str
    n_samples: int
    vmin: float
    vmax: float
    vpp: float
    vavg: float
    vrms: float
    frequency_hz: float | None
    period_s: float | None
    duty_high_pct: float | None
    rise_time_s: float | None
    fall_time_s: float | None
    probe_factor: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureMeas:
    path: str
    samplerate_hz: float | None
    channels: list[ChannelMeas] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "samplerate_hz": self.samplerate_hz,
            "comments": self.comments,
            "channels": [c.to_dict() for c in self.channels],
        }


_SR_RE = re.compile(r"Samplerate:\s*([0-9.]+)\s*([kKmMgG]?)\s*Hz", re.I)
_UNIT = {"": 1.0, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "g": 1e9, "G": 1e9}


def _parse_samplerate(comments: list[str]) -> float | None:
    for c in comments:
        m = _SR_RE.search(c)
        if m:
            return float(m.group(1)) * _UNIT.get(m.group(2), 1.0)
    return None


def load_sigrok_csv(path: Path) -> tuple[list[str], np.ndarray, list[str], float | None]:
    """Return (channel_names, data[n,ch], comments, samplerate_hz)."""
    comments: list[str] = []
    header: list[str] | None = None
    rows: list[list[float]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for line in fh:
            if line.startswith(";"):
                comments.append(line[1:].strip())
                continue
            # sigrok csv may start with header names or bare 'V'
            reader_line = next(csv.reader([line]))
            if header is None:
                # If first non-comment looks like numbers, invent CH names later
                try:
                    floats = [float(x) for x in reader_line if x != ""]
                    if floats and all(_looks_numeric(x) for x in reader_line if x != ""):
                        header = [f"CH{i+1}" for i in range(len(floats))]
                        rows.append(floats)
                        continue
                except ValueError:
                    pass
                header = [h.strip() or f"CH{i+1}" for i, h in enumerate(reader_line)]
                continue
            try:
                vals = [float(x) for x in reader_line[: len(header)]]
            except ValueError:
                continue
            if len(vals) != len(header):
                continue
            rows.append(vals)
    if not header or not rows:
        raise ValueError(f"No samples in {path}")
    data = np.asarray(rows, dtype=np.float64)
    # sigrok often labels every analog column "V" — rename for dual-channel
    if len(header) >= 2 and all(h.strip().upper() in {"V", "VOLTAGE", ""} for h in header):
        header = [f"CH{i+1}" for i in range(len(header))]
    elif len(header) == 1 and header[0].strip().upper() in {"V", "VOLTAGE"}:
        header = ["CH1"]
    # LA csv labels every column "logic" — rename to D0..Dn
    elif len(header) >= 1 and all(h.strip().upper() in {"LOGIC", "L", ""} for h in header):
        header = [f"D{i}" for i in range(len(header))]
    return header, data, comments, _parse_samplerate(comments)


def measure_la_channel(
    name: str,
    bits: np.ndarray,
    samplerate_hz: float | None,
) -> dict[str, Any]:
    """Per-channel digital metrics (freq, duty, pulse width, edges)."""
    col = (np.asarray(bits, dtype=np.float64) >= 0.5).astype(np.uint8)
    n = int(col.size)
    edges = int(np.count_nonzero(col[1:] != col[:-1])) if n > 1 else 0
    high = int(np.count_nonzero(col))
    high_pct = 100.0 * high / n if n else 0.0
    freq = period = duty = pw = None
    if samplerate_hz and n > 2:
        t = np.arange(n, dtype=np.float64) / float(samplerate_hz)
        rise, fall = _edge_times(col.astype(np.float64), t, 0.5)
        if rise.size >= 2:
            periods = np.diff(rise)
            period = float(np.median(periods))
            if period > 0:
                freq = 1.0 / period
        duty = _duty(col.astype(np.float64), 0.5)
        # positive pulse width: rise→next fall
        if rise.size and fall.size:
            widths = []
            for r in rise:
                later = fall[fall > r]
                if later.size:
                    widths.append(float(later[0] - r))
            if widths:
                pw = float(np.median(widths))
    return {
        "name": name,
        "n_samples": n,
        "high_pct": round(high_pct, 2),
        "duty_high_pct": round(duty, 2) if duty is not None else None,
        "edges": edges,
        "rising_edges": int(np.count_nonzero((col[:-1] == 0) & (col[1:] == 1))) if n > 1 else 0,
        "falling_edges": int(np.count_nonzero((col[:-1] == 1) & (col[1:] == 0))) if n > 1 else 0,
        "stuck": edges == 0,
        "last": int(col[-1]) if n else 0,
        "frequency_hz": freq,
        "period_s": period,
        "pulse_width_s": pw,
    }


def measure_la_csv(path: Path, *, max_plot_points: int = 4000) -> dict[str, Any]:
    """Full LA measure + downsampled series for UI / MCP / live frames."""
    names, data, comments, sr = load_sigrok_csv(path)
    n = int(data.shape[0])
    channels = [
        measure_la_channel(names[i], data[:, i], sr) for i in range(len(names))
    ]
    if n <= max_plot_points:
        idx = np.arange(n)
        plotted = data
    else:
        idx = np.linspace(0, n - 1, max_plot_points).astype(np.int64)
        plotted = data[idx]
    t = (idx.astype(np.float64) / float(sr)) if sr else idx.astype(np.float64)
    series = {
        names[i]: [int(v >= 0.5) for v in plotted[:, i]] for i in range(len(names))
    }
    active = [c["name"] for c in channels if not c.get("stuck")]
    return {
        "path": str(path),
        "samplerate_hz": sr,
        "n_samples": n,
        "duration_s": (n / sr) if sr else None,
        "comments": comments,
        "channels": channels,
        "active_channels": active,
        "t": t.tolist(),
        "series": series,
    }


def summarize_la_csv(path: Path, *, max_plot_points: int = 4000) -> dict[str, Any]:
    """Alias for measure_la_csv (back-compat)."""
    return measure_la_csv(path, max_plot_points=max_plot_points)


def _looks_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _edge_times(y: np.ndarray, t: np.ndarray, mid: float) -> tuple[np.ndarray, np.ndarray]:
    above = y >= mid
    rise = np.where(~above[:-1] & above[1:])[0]
    fall = np.where(above[:-1] & ~above[1:])[0]
    return t[rise], t[fall]


def _duty(y: np.ndarray, mid: float) -> float | None:
    if y.size < 2:
        return None
    return float(100.0 * np.mean(y >= mid))


def _slew_time(y: np.ndarray, t: np.ndarray, low: float, high: float, rising: bool) -> float | None:
    if rising:
        i0 = np.where(y >= low)[0]
        i1 = np.where(y >= high)[0]
    else:
        i0 = np.where(y <= high)[0]
        i1 = np.where(y <= low)[0]
    if i0.size == 0 or i1.size == 0:
        return None
    # First crossing of low/high in the same transition region
    start = int(i0[0])
    later = i1[i1 >= start]
    if later.size == 0:
        return None
    return float(t[int(later[0])] - t[start])


def measure_channel(
    name: str,
    y: np.ndarray,
    samplerate_hz: float | None,
    probe_factor: float = 1.0,
) -> ChannelMeas:
    y = y.astype(np.float64) * float(probe_factor)
    vmin = float(np.min(y))
    vmax = float(np.max(y))
    vpp = vmax - vmin
    vavg = float(np.mean(y))
    vrms = float(np.sqrt(np.mean(y * y)))
    freq = period = duty = rise = fall = None
    if samplerate_hz and samplerate_hz > 0 and y.size > 4:
        t = np.arange(y.size, dtype=np.float64) / samplerate_hz
        mid = 0.5 * (vmin + vmax)
        lo = vmin + 0.1 * vpp
        hi = vmin + 0.9 * vpp
        if vpp > 1e-9:
            tr, tf = _edge_times(y, t, mid)
            if tr.size >= 2:
                periods = np.diff(tr)
                period = float(np.median(periods))
                if period > 0:
                    freq = 1.0 / period
            duty = _duty(y, mid)
            rise = _slew_time(y, t, lo, hi, rising=True)
            fall = _slew_time(y, t, lo, hi, rising=False)
    return ChannelMeas(
        name=name,
        n_samples=int(y.size),
        vmin=vmin,
        vmax=vmax,
        vpp=vpp,
        vavg=vavg,
        vrms=vrms,
        frequency_hz=freq,
        period_s=period,
        duty_high_pct=duty,
        rise_time_s=rise,
        fall_time_s=fall,
        probe_factor=float(probe_factor),
    )


def measure_csv(path: Path, probe_factor: float = 1.0) -> CaptureMeas:
    names, data, comments, sr = load_sigrok_csv(path)
    chans = [
        measure_channel(names[i], data[:, i], sr, probe_factor=probe_factor)
        for i in range(data.shape[1])
    ]
    return CaptureMeas(path=str(path), samplerate_hz=sr, channels=chans, comments=comments)


def plot_csv(path: Path, out_png: Path, probe_factor: float = 1.0) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names, data, comments, sr = load_sigrok_csv(path)
    n = data.shape[0]
    if sr and sr > 0:
        x = np.arange(n) / sr
        xlabel = "Time (s)"
    else:
        x = np.arange(n)
        xlabel = "Sample"
    fig, ax = plt.subplots(figsize=(10, 4))
    for i, name in enumerate(names):
        ax.plot(x, data[:, i] * probe_factor, label=name, linewidth=0.8)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Voltage (V)" if probe_factor == 1 else f"Voltage (V) ×{probe_factor:g}")
    ax.set_title(path.name)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return out_png


def cal_pass_fail(meas: ChannelMeas) -> dict[str, Any]:
    """Pass bands for 1 kHz probe-cal square (amplitude documented, not invented)."""
    checks: dict[str, Any] = {}
    freq_ok = (
        meas.frequency_hz is not None and 900.0 <= meas.frequency_hz <= 1100.0
    )
    duty_ok = (
        meas.duty_high_pct is not None and 40.0 <= meas.duty_high_pct <= 60.0
    )
    vpp_ok = meas.vpp >= 0.3  # cal tip is typically ~1–2 Vpp at x1; floor only
    checks["frequency_hz"] = {
        "value": meas.frequency_hz,
        "expect": "900..1100",
        "pass": freq_ok,
    }
    checks["duty_high_pct"] = {
        "value": meas.duty_high_pct,
        "expect": "40..60",
        "pass": duty_ok,
    }
    checks["vpp"] = {
        "value": meas.vpp,
        "expect": ">=0.3 (document as-found)",
        "pass": vpp_ok,
    }
    checks["pass"] = bool(freq_ok and duty_ok and vpp_ok)
    return checks
