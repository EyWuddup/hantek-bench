"""
Auto-set for Hantek 6022BL live stream.

Goal: one full peak-to-peak period on screen, at the highest sample rate
(and tightest voltage range) that still fits — max detail per cycle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .live_stream import SAMPLE_RATES, VOLTAGE_RANGES

# Most → least sensitive (higher index = more gain / smaller FS)
RANGE_CANDIDATES: tuple[int, ...] = (10, 5, 2, 1)

# Dual-channel friendly rates (slow → fast)
RATE_CANDIDATES: tuple[int, ...] = (110, 120, 150, 1, 2, 4, 8)

# Discovery ladder when frequency is unknown (slow → fast)
DISCOVERY_RATES: tuple[int, ...] = (110, 150, 1)

# ~2 cycles on screen at the *fastest* rate that still fits.
# (Need ≥2 rising edges for a stable period/freq estimate; samples/period
#  depends only on rate/freq — same detail as a single-cycle zoom.)
TARGET_PERIODS = 2.15
MIN_PERIODS_ON_SCREEN = 1.95
MAX_PERIODS_ON_SCREEN = 2.8  # don't zoom out further when we can stay tight

MIN_DISPLAY_POINTS = 3000
MAX_DISPLAY_POINTS = 12000
DEFAULT_DISPLAY_POINTS = 4000

# Prefer ADC utilisation high (fill vertical) without clipping
UTIL_LO = 0.35
UTIL_HI = 0.85
HEADROOM = 1.22  # peak × headroom must fit in FS


def tip_half_fs_v(range_index: int, probe: float) -> float:
    """Approx. tip-referred half-scale (V) for this gain × probe factor."""
    return 5.12 * float(probe) / float(range_index)


def _ch_stats(meas: dict[str, Any] | None, y: list[float] | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "vmin": None,
        "vmax": None,
        "vpp": None,
        "peak": None,
        "frequency_hz": None,
        "duty_high_pct": None,
        "active": False,
    }
    if meas:
        vmin = meas.get("vmin")
        vmax = meas.get("vmax")
        vpp = meas.get("vpp")
        if vmin is not None and vmax is not None:
            out["vmin"] = float(vmin)
            out["vmax"] = float(vmax)
            out["peak"] = max(abs(float(vmin)), abs(float(vmax)))
        if vpp is not None:
            out["vpp"] = float(vpp)
        fh = meas.get("frequency_hz")
        out["frequency_hz"] = float(fh) if fh is not None else None
        dh = meas.get("duty_high_pct")
        out["duty_high_pct"] = float(dh) if dh is not None else None
    elif y:
        import numpy as np

        arr = np.asarray(y, dtype=np.float64)
        if arr.size:
            vmin = float(arr.min())
            vmax = float(arr.max())
            out["vmin"] = vmin
            out["vmax"] = vmax
            out["vpp"] = vmax - vmin
            out["peak"] = max(abs(vmin), abs(vmax))
    peak = out["peak"] or 0.0
    vpp = out["vpp"] or 0.0
    out["active"] = bool(vpp >= 0.03 or peak >= 0.02)
    return out


def choose_range(peak_v: float, vpp: float, probe: float) -> int:
    """Most sensitive range that still fits full PTP with headroom."""
    peak = max(float(peak_v or 0.0), 0.5 * float(vpp or 0.0))
    if peak < 0.015:
        return 5 if float(probe) <= 1.5 else 2

    need = peak * HEADROOM
    for ri in RANGE_CANDIDATES:
        if tip_half_fs_v(ri, probe) >= need:
            return ri
    return 1


def periods_on_screen(freq_hz: float, rate_index: int, display_points: int) -> float:
    rate = SAMPLE_RATES.get(rate_index, (None, 0.0))[1]
    if not rate or not freq_hz:
        return 0.0
    return (float(display_points) / float(rate)) * float(freq_hz)


def samples_per_period(freq_hz: float, rate_index: int) -> float:
    rate = SAMPLE_RATES.get(rate_index, (None, 0.0))[1]
    if not rate or not freq_hz:
        return 0.0
    return float(rate) / float(freq_hz)


def choose_rate_and_display(
    freq_hz: float | None, default_display: int = DEFAULT_DISPLAY_POINTS
) -> tuple[int, int]:
    """
    Max horizontal detail for one full period:

    - Prefer the *fastest* sample rate that can still fit ≥ MIN_PERIODS
      within MAX_DISPLAY_POINTS.
    - Size the record so ~TARGET_PERIODS fill the screen (not 3–50 cycles).
    """
    if not freq_hz or freq_hz < 0.5:
        return 110, max(default_display, 8000)

    best_ri = RATE_CANDIDATES[0]
    best_disp = MAX_DISPLAY_POINTS
    best_score = -1.0

    # Fast → slow: try to win with highest rate first
    for ri in reversed(RATE_CANDIDATES):
        rate = SAMPLE_RATES[ri][1]
        # Points needed for TARGET periods at this rate
        disp = int(round(TARGET_PERIODS * rate / float(freq_hz)))
        disp = max(MIN_DISPLAY_POINTS, min(MAX_DISPLAY_POINTS, disp))
        per = periods_on_screen(freq_hz, ri, disp)

        if per < MIN_PERIODS_ON_SCREEN:
            # Stretch record to fit one full cycle
            need = int(round(MIN_PERIODS_ON_SCREEN * rate / float(freq_hz) + 0.5))
            if need > MAX_DISPLAY_POINTS:
                continue  # too fast to show a full period
            disp = max(MIN_DISPLAY_POINTS, min(MAX_DISPLAY_POINTS, need))
            per = periods_on_screen(freq_hz, ri, disp)
            if per < MIN_PERIODS_ON_SCREEN:
                continue

        # If many periods (HF + min points floor), shrink display toward TARGET
        if per > MAX_PERIODS_ON_SCREEN:
            tight = int(round(TARGET_PERIODS * rate / float(freq_hz)))
            tight = max(MIN_DISPLAY_POINTS, min(MAX_DISPLAY_POINTS, tight))
            # Only shrink if we still keep a full cycle
            if periods_on_screen(freq_hz, ri, tight) >= MIN_PERIODS_ON_SCREEN:
                disp = tight
                per = periods_on_screen(freq_hz, ri, disp)

        spp = samples_per_period(freq_hz, ri)
        # Primary: samples/period (detail). Secondary: closeness to one cycle on screen.
        period_fit = 1.0 - min(abs(per - TARGET_PERIODS) / TARGET_PERIODS, 1.0)
        score = spp * 100.0 + period_fit * 10.0
        if score > best_score:
            best_score = score
            best_ri = ri
            best_disp = disp

    return best_ri, int(best_disp)


def parse_scope_channels(channels: str | None) -> set[str]:
    """Normalize 'CH1' / 'CH2' / 'both' / 'CH1,CH2' → {'CH1',...}."""
    raw = (channels or "both").strip().lower()
    if raw in ("both", "all", "ch1,ch2", "ch2,ch1", ""):
        return {"CH1", "CH2"}
    out: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        p = part.strip().upper().replace(" ", "")
        if p in ("CH1", "1"):
            out.add("CH1")
        elif p in ("CH2", "2"):
            out.add("CH2")
    return out or {"CH1", "CH2"}


def choose_trigger_source(ch1: dict[str, Any], ch2: dict[str, Any] | None) -> str:
    if not ch2 or not ch2.get("active"):
        return "CH1"
    if not ch1.get("active"):
        return "CH2"
    f1 = ch1.get("frequency_hz") or 0.0
    f2 = ch2.get("frequency_hz") or 0.0
    if f2 > f1 * 1.2 and f2 > 5:
        return "CH2"
    if f1 > f2 * 1.2 and f1 > 5:
        return "CH1"
    v1 = ch1.get("vpp") or 0.0
    v2 = ch2.get("vpp") or 0.0
    return "CH2" if v2 > v1 * 1.15 else "CH1"


@dataclass
class AutoSetSuggestion:
    rate_index: int
    ch1_range: int
    ch2_range: int
    trigger_mode: str = "auto"
    trigger_source: str = "CH1"
    trigger_level: float = 0.0
    display_points: int = DEFAULT_DISPLAY_POINTS
    reasons: list[str] = field(default_factory=list)
    analysis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_config(self) -> dict[str, Any]:
        return {
            "rate_index": self.rate_index,
            "ch1_range": self.ch1_range,
            "ch2_range": self.ch2_range,
            "trigger_mode": self.trigger_mode,
            "trigger_source": self.trigger_source,
            "trigger_level": self.trigger_level,
            "display_points": self.display_points,
        }


def _raw_channel_stats(
    name: str,
    y: list[float] | None,
    rate_hz: float,
    ema_meas: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """Measure from raw samples — never trust live EMA (lags after cal/rate changes)."""
    if not y:
        return _ch_stats(ema_meas, None)
    try:
        import numpy as np

        from .measure import measure_channel

        m = measure_channel(name, np.asarray(y, dtype=np.float64), rate_hz, probe_factor=1.0)
        d = m.to_dict()
        if d.get("vmin") is not None and d.get("vmax") is not None:
            d["mid"] = 0.5 * (d["vmin"] + d["vmax"])
            d["peak"] = max(abs(d["vmin"]), abs(d["vmax"]))
        d["active"] = bool((d.get("vpp") or 0) >= 0.03 or (d.get("peak") or 0) >= 0.02)
        return {
            "vmin": d.get("vmin"),
            "vmax": d.get("vmax"),
            "vpp": d.get("vpp"),
            "peak": d.get("peak"),
            "frequency_hz": d.get("frequency_hz"),
            "duty_high_pct": d.get("duty_high_pct"),
            "active": d.get("active"),
        }
    except Exception:
        return _ch_stats(ema_meas, y)


def suggest_from_frame(
    frame: dict[str, Any],
    *,
    probe_ch1: float,
    probe_ch2: float,
    display_points: int = DEFAULT_DISPLAY_POINTS,
    current: dict[str, Any] | None = None,
    channels: str | set[str] | None = None,
) -> AutoSetSuggestion:
    """Pure function: frame + probe factors → max-detail one-period config."""
    want = (
        set(channels)
        if isinstance(channels, set)
        else parse_scope_channels(channels if isinstance(channels, str) else None)
    )
    use1 = "CH1" in want
    use2 = "CH2" in want
    meas = frame.get("measure") or {}
    rate_hz = float(frame.get("rate_hz") or (meas.get("rate_hz") or 1e6))
    ch1 = (
        _raw_channel_stats("CH1", frame.get("ch1"), rate_hz, meas.get("CH1"))
        if use1
        else {
            "vmin": None,
            "vmax": None,
            "vpp": None,
            "peak": None,
            "frequency_hz": None,
            "duty_high_pct": None,
            "active": False,
        }
    )
    ch2 = (
        _raw_channel_stats("CH2", frame.get("ch2"), rate_hz, meas.get("CH2"))
        if use2 and frame.get("ch2") is not None
        else None
    )

    cur = current or {}
    r1 = (
        choose_range(float(ch1["peak"] or 0), float(ch1["vpp"] or 0), probe_ch1)
        if use1
        else int(cur.get("ch1_range", 1))
    )
    r2 = (
        choose_range(float(ch2["peak"] or 0), float(ch2["vpp"] or 0), probe_ch2)
        if ch2 is not None
        else int(cur.get("ch2_range", 1))
    )

    freqs: list[float | None] = []
    if use1:
        freqs.append(ch1.get("frequency_hz"))
    if ch2 is not None:
        freqs.append(ch2.get("frequency_hz"))
    freqs_f = [float(f) for f in freqs if f]
    best_f = max(freqs_f) if freqs_f else None
    rate, disp = choose_rate_and_display(best_f, display_points)
    per = periods_on_screen(best_f, rate, disp) if best_f else None
    spp = samples_per_period(best_f, rate) if best_f else None

    if use1 and use2:
        src = choose_trigger_source(ch1, ch2)
    elif use2:
        src = "CH2"
    else:
        src = "CH1"
    src_stats = ch2 if src == "CH2" and ch2 else ch1
    mid = None
    if src_stats.get("vmin") is not None and src_stats.get("vmax") is not None:
        mid = 0.5 * (float(src_stats["vmin"]) + float(src_stats["vmax"]))

    reasons: list[str] = []
    if use1:
        reasons.append(
            f"CH1 peak={ch1['peak'] and round(ch1['peak'], 3)} V → {VOLTAGE_RANGES.get(r1, r1)} (max sens)"
        )
    if ch2 is not None:
        reasons.append(
            f"CH2 peak={ch2['peak'] and round(ch2['peak'], 3)} V → {VOLTAGE_RANGES.get(r2, r2)}"
        )
    if best_f and per is not None and spp is not None:
        reasons.append(
            f"f≈{best_f:.3g} Hz → {SAMPLE_RATES[rate][0]} / {disp} pts "
            f"(~{per:.2f} cycles, {spp:.0f} samples/cycle — max detail)"
        )
    else:
        reasons.append(
            f"no stable frequency → {SAMPLE_RATES[rate][0]} / {disp} pts (discovery)"
        )
    reasons.append(f"trigger {src} auto-rise @ mid (lock one cycle)")

    for name, st, ri, pf in (
        ("CH1", ch1, r1, probe_ch1) if use1 else (None, None, None, None),
        ("CH2", ch2, r2, probe_ch2) if ch2 is not None else (None, None, None, None),
    ):
        if not name or not st:
            continue
        fs = tip_half_fs_v(int(ri), float(pf))
        peak = float(st["peak"] or 0)
        util = peak / fs if fs > 0 else 0
        if util > UTIL_HI:
            reasons.append(f"{name} util {util:.0%} (near clip — may widen)")
        elif util < UTIL_LO and st.get("active"):
            reasons.append(f"{name} util {util:.0%} (could be tighter)")

    return AutoSetSuggestion(
        rate_index=rate,
        ch1_range=r1,
        ch2_range=int(r2),
        trigger_mode="auto",
        trigger_source=src,
        trigger_level=float(mid or 0.0),
        display_points=disp,
        reasons=reasons,
        analysis={
            "CH1": ch1 if use1 else None,
            "CH2": ch2,
            "channels": sorted(want),
            "freq_hz": best_f,
            "periods_on_screen": per,
            "samples_per_period": spp,
            "ch1_half_fs_v": tip_half_fs_v(r1, probe_ch1) if use1 else None,
            "ch2_half_fs_v": tip_half_fs_v(int(r2), probe_ch2) if ch2 is not None else None,
        },
    )


def configs_differ(a: dict[str, Any], b: dict[str, Any], keys: tuple[str, ...] | None = None) -> bool:
    keys = keys or (
        "rate_index",
        "ch1_range",
        "ch2_range",
        "trigger_mode",
        "trigger_source",
        "display_points",
    )
    for k in keys:
        if a.get(k) != b.get(k):
            return True
    return False
