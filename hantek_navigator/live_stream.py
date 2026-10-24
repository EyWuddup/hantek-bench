"""
Continuous USB live stream for Hantek 6022BL (OpenHantek replacement core).

Talks the same vendor protocol as OpenHantek / Hantek6022API:
control 0xE0–0xE6, bulk IN 0x86. Works with sigrok fw (1D50:608E) and
OpenHantek fw (04B5:602A). Optionally flashes Ho-Ro firmware from loader.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from struct import pack
from typing import Any, Callable

import numpy as np

try:
    import usb1
except ImportError:  # pragma: no cover
    usb1 = None  # type: ignore

SAMPLE_RATES: dict[int, tuple[str, float]] = {
    110: ("100 kS/s", 100e3),
    120: ("200 kS/s", 200e3),
    150: ("500 kS/s", 500e3),
    1: ("1 MS/s", 1e6),
    2: ("2 MS/s", 2e6),
    4: ("4 MS/s", 4e6),
    8: ("8 MS/s", 8e6),
    16: ("16 MS/s", 16e6),
    24: ("24 MS/s", 24e6),
    30: ("30 MS/s", 30e6),
    48: ("48 MS/s", 48e6),
}

# range_index → full-scale half-range (V) for ±FS display helper
VOLTAGE_RANGES: dict[int, str] = {
    1: "±5 V",
    2: "±2.5 V",
    5: "±1 V",
    10: "±500 mV",
}

VID_PIDS = [
    (0x1D50, 0x608E),  # sigrok-named scope fw (current after sigrok use)
    (0x04B5, 0x602A),  # OpenHantek / Hantek6022API fw
    (0x04B4, 0x602A),  # loader — flash before stream
]


def _scale_adc(adc: np.ndarray, range_index: int, probe: float) -> np.ndarray:
    """Rough voltage scale (no EEPROM cal). Good enough for live UI."""
    # (adc-128) * 5.12 / (range<<7) * probe
    return (adc.astype(np.float64) - 128.0) * (5.12 * float(probe)) / float(range_index << 7)


@dataclass
class LiveConfig:
    rate_index: int = 1
    num_channels: int = 2
    ch1_range: int = 1
    ch2_range: int = 1
    probe_ch1: float = 1.0
    probe_ch2: float = 10.0
    block_samples: int = 8192  # per channel per USB read
    display_points: int = 4000
    # auto = lock on rising at mid (steady view); rising/falling = fixed level; off = free-run
    trigger_mode: str = "auto"
    trigger_level: float = 0.0  # used when trigger_mode is rising/falling (not auto mid)
    trigger_source: str = "CH1"
    # Small pre-trigger so one locked period fills most of the screen after T
    trigger_holdoff_frac: float = 0.08


@dataclass
class LiveStatus:
    running: bool = False
    error: str | None = None
    vid_pid: str | None = None
    rate_hz: float = 1e6
    frames: int = 0
    drops: int = 0
    last_fps: float = 0.0


class LiveScope:
    """Singleton-friendly continuous reader."""

    def __init__(self) -> None:
        self.cfg = LiveConfig()
        self.status = LiveStatus()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ctx = None
        self._handle = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._latest: dict[str, Any] | None = None
        self._fps_times: deque[float] = deque(maxlen=30)
        self._trig_lvl_ema: float | None = None
        self._meas_ema: dict[str, dict[str, float]] = {}
        # Cleared while SETCALFREQ / other EP0 commands need the bus quiet
        self._acq_gate = threading.Event()
        self._acq_gate.set()

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self._latest

    def configure(self, **kwargs: Any) -> LiveConfig:
        with self._lock:
            mode_before = self.cfg.trigger_mode
            for k, v in kwargs.items():
                if hasattr(self.cfg, k) and v is not None:
                    setattr(self.cfg, k, v)
            # Reset lock state when trigger mode/source changes
            if (
                kwargs.get("trigger_mode") is not None
                and kwargs.get("trigger_mode") != mode_before
            ) or kwargs.get("trigger_source") is not None:
                self._trig_lvl_ema = None
            return self.cfg

    def _find_and_open(self) -> None:
        if usb1 is None:
            raise RuntimeError("libusb1 / usb1 not installed")
        self._ctx = usb1.USBContext()
        handle = None
        matched = None
        for vid, pid in VID_PIDS:
            handle = self._ctx.openByVendorIDAndProductID(vid, pid, skip_on_error=True)
            if handle is not None:
                matched = (vid, pid)
                break
        if handle is None:
            raise RuntimeError("No Hantek 6022BL found (plug USB, H/P pressed for scope)")

        # Loader → flash Ho-Ro firmware for best continuous streaming
        if matched == (0x04B4, 0x602A):
            handle.close()
            self._flash_firmware()
            time.sleep(1.5)
            handle = self._ctx.openByVendorIDAndProductID(0x04B5, 0x602A, skip_on_error=True)
            if handle is None:
                handle = self._ctx.openByVendorIDAndProductID(0x1D50, 0x608E, skip_on_error=True)
            if handle is None:
                raise RuntimeError("Firmware flash attempted but device did not reappear")
            matched = (handle.getDevice().getVendorID(), handle.getDevice().getProductID())

        try:
            if handle.kernelDriverActive(0):
                handle.detachKernelDriver(0)
        except Exception:
            pass
        handle.claimInterface(0)
        self._handle = handle
        self.status.vid_pid = f"{matched[0]:04X}:{matched[1]:04X}"

    def _flash_firmware(self) -> None:
        from PyHT6022.LibUsbScope import Oscilloscope

        scope = Oscilloscope()
        if not scope.setup():
            raise RuntimeError("Cannot open loader for firmware flash")
        if not scope.open_handle():
            raise RuntimeError("Cannot claim loader interface")
        try:
            ok = scope.flash_firmware()
            if not ok:
                raise RuntimeError("flash_firmware returned False")
        finally:
            scope.close_handle(release_interface=True)

    def _ctrl(self, request: int, data: bytes) -> None:
        assert self._handle is not None
        n = self._handle.controlWrite(0x40, request, 0, 0, data, timeout=1000)
        if n != len(data):
            raise RuntimeError(f"control 0x{request:02X} wrote {n}/{len(data)}")

    def _apply_config(self) -> None:
        cfg = self.cfg
        if cfg.rate_index not in SAMPLE_RATES:
            raise ValueError(f"unsupported rate_index {cfg.rate_index}")
        nch = 2 if cfg.num_channels >= 2 else 1
        try:
            self._ctrl(0xE4, pack("B", nch))  # num channels (custom fw)
        except Exception:
            pass
        self._ctrl(0xE2, pack("B", cfg.rate_index))
        self._ctrl(0xE0, pack("B", cfg.ch1_range))
        if nch == 2:
            self._ctrl(0xE1, pack("B", cfg.ch2_range))
        self.status.rate_hz = SAMPLE_RATES[cfg.rate_index][1]

    def _start_hw(self) -> None:
        self._ctrl(0xE3, b"\x01")

    def _stop_hw(self) -> None:
        try:
            self._ctrl(0xE3, b"\x00")
        except Exception:
            pass

    def start(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            if self.status.running:
                self.configure(**kwargs)
                return self.snapshot_status()
            self.configure(**kwargs)
            self._stop.clear()
            self.status.error = None
            self.status.frames = 0
            self.status.drops = 0
            self._trig_lvl_ema = None
            self._meas_ema = {}
            self._acq_gate.set()
            self._thread = threading.Thread(target=self._run, name="htk-live", daemon=True)
            self._thread.start()
            # wait briefly for open
            for _ in range(50):
                if self.status.running or self.status.error:
                    break
                time.sleep(0.05)
            return self.snapshot_status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        with self._lock:
            self.status.running = False
        return self.snapshot_status()

    def pause_acquisition(self) -> None:
        """Stop the bulk-read loop so vendor EP0 commands (e.g. 0xE6) can run."""
        self._acq_gate.clear()
        time.sleep(0.08)

    def resume_acquisition(self) -> None:
        self._acq_gate.set()

    def set_cal_freq(self, freq_hz: float) -> dict[str, Any]:
        """Pause live capture, SETCALFREQ, resume. See calfreq.py for why."""
        from . import calfreq as calfreq_mod

        if not self.status.running or self._handle is None:
            return calfreq_mod.set_cal_freq(freq_hz, ensure_fw=True)
        self.pause_acquisition()
        try:
            # Gate already stopped bulk + HW. Firmware also stop_sampling() on
            # 0xE6; do not resume inside the helper — the gate path restarts HW.
            return calfreq_mod.set_cal_freq_on_handle(
                self._handle, freq_hz, pause_sampling=False, dialect="horo"
            )
        finally:
            self.resume_acquisition()

    def apply(self, **kwargs: Any) -> dict[str, Any]:
        """Configure; restart stream when hardware gain/rate changes."""
        hw_keys = {"rate_index", "num_channels", "ch1_range", "ch2_range", "block_samples"}
        before = self.snapshot_status()
        touched_hw = bool(hw_keys & set(kwargs))
        self.configure(**kwargs)
        if self.status.running and touched_hw:
            self.stop()
            return self.start(
                rate_index=int(kwargs.get("rate_index", before["rate_index"])),
                num_channels=int(kwargs.get("num_channels", before["num_channels"])),
                ch1_range=int(kwargs.get("ch1_range", before["ch1_range"])),
                ch2_range=int(kwargs.get("ch2_range", before["ch2_range"])),
                probe_ch1=float(kwargs.get("probe_ch1", before["probe_ch1"])),
                probe_ch2=float(kwargs.get("probe_ch2", before["probe_ch2"])),
                trigger_mode=str(kwargs.get("trigger_mode", before["trigger_mode"])),
                trigger_level=float(kwargs.get("trigger_level", before["trigger_level"])),
                trigger_source=str(kwargs.get("trigger_source", before["trigger_source"])),
                display_points=int(kwargs.get("display_points", self.cfg.display_points)),
                block_samples=int(kwargs.get("block_samples", self.cfg.block_samples)),
            )
        if not self.status.running:
            return self.start(**{**before, **kwargs})
        return self.snapshot_status()

    def _wait_frame(self, timeout_s: float = 1.5, min_frames: int = 3) -> dict[str, Any] | None:
        """Wait until a few fresh frames arrive (post-restart settle)."""
        start_frames = self.status.frames
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout_s:
            last = self.latest()
            if last and self.status.frames >= start_frames + min_frames:
                return last
            time.sleep(0.05)
        return last or self.latest()

    def _collect_raw_stats(self, n_frames: int = 10, timeout_s: float = 2.5) -> dict[str, Any]:
        """Median frequency / peaks from raw frames (ignores live EMA)."""
        from .autoset import _raw_channel_stats

        t0 = time.time()
        start_fr = self.status.frames
        samples: list[dict[str, Any]] = []
        while time.time() - t0 < timeout_s and len(samples) < n_frames:
            fr = self.latest()
            if fr and self.status.frames > start_fr:
                start_fr = self.status.frames
                rate = float(fr.get("rate_hz") or self.status.rate_hz or 1e6)
                ch1 = _raw_channel_stats("CH1", fr.get("ch1"), rate)
                ch2 = (
                    _raw_channel_stats("CH2", fr.get("ch2"), rate)
                    if fr.get("ch2") is not None
                    else None
                )
                samples.append({"CH1": ch1, "CH2": ch2, "frame": fr, "rate_hz": rate})
            time.sleep(0.04)
        if not samples:
            fr = self.latest()
            if not fr:
                return {}
            rate = float(fr.get("rate_hz") or self.status.rate_hz or 1e6)
            return {
                "CH1": _raw_channel_stats("CH1", fr.get("ch1"), rate),
                "CH2": (
                    _raw_channel_stats("CH2", fr.get("ch2"), rate)
                    if fr.get("ch2") is not None
                    else None
                ),
                "frame": fr,
                "rate_hz": rate,
                "n": 1,
            }

        def _median(vals: list[float]) -> float | None:
            if not vals:
                return None
            vals = sorted(vals)
            return float(vals[len(vals) // 2])

        def _merge(key: str) -> dict[str, Any] | None:
            rows = [s[key] for s in samples if s.get(key)]
            if not rows:
                return None
            out = dict(rows[-1])
            for fld in ("frequency_hz", "vpp", "vmin", "vmax", "peak", "duty_high_pct"):
                med = _median([float(r[fld]) for r in rows if r.get(fld) is not None])
                if med is not None:
                    out[fld] = med
            if out.get("vmin") is not None and out.get("vmax") is not None:
                out["peak"] = max(abs(out["vmin"]), abs(out["vmax"]))
                out["mid"] = 0.5 * (out["vmin"] + out["vmax"])
            out["active"] = bool((out.get("vpp") or 0) >= 0.03)
            return out

        return {
            "CH1": _merge("CH1"),
            "CH2": _merge("CH2"),
            "frame": samples[-1]["frame"],
            "rate_hz": samples[-1]["rate_hz"],
            "n": len(samples),
        }

    def autoset(
        self,
        *,
        probe_ch1: float | None = None,
        probe_ch2: float | None = None,
        channels: str | None = None,
        max_passes: int = 3,
        settle_s: float = 0.7,
    ) -> dict[str, Any]:
        """
        Max-detail autoset:
        1) Slow wide discovery → lock median frequency + peaks
        2) Pick fastest rate / tightest range for ~2 full PTP cycles
        3) If the zoomed view can't fit a full cycle, step the rate down once

        `channels`: CH1 / CH2 / both (CH1,CH2) — only those drive rate/range/trigger.
        """
        from .autoset import (
            MIN_PERIODS_ON_SCREEN,
            RATE_CANDIDATES,
            choose_range,
            choose_rate_and_display,
            parse_scope_channels,
            periods_on_screen,
            samples_per_period,
            suggest_from_frame,
        )

        want = parse_scope_channels(channels)
        use1 = "CH1" in want
        use2 = "CH2" in want
        # CH2 always needs dual interleaved capture; CH1-only can be single-ch
        nch = 2 if use2 else 1
        trig_src = "CH2" if (use2 and not use1) else "CH1"

        if probe_ch1 is not None:
            self.configure(probe_ch1=float(probe_ch1))
        if probe_ch2 is not None:
            self.configure(probe_ch2=float(probe_ch2))

        self._meas_ema.clear()
        self._trig_lvl_ema = None
        history: list[dict[str, Any]] = []

        # --- 1) Discovery at slow + fast rates; keep most consistent frequency ---
        # Slow catches 50–200 Hz; fast catches 10–100 kHz (slow alias/fails there).
        discover_rates = (110, 1, 4)
        locked_f: float | None = None
        best_quality = -1.0
        stats: dict[str, Any] = {}
        best_stats: dict[str, Any] = {}
        st = self.snapshot_status()
        for ri in discover_rates:
            st = self.apply(
                ch1_range=1,
                ch2_range=1,
                rate_index=ri,
                num_channels=nch,
                display_points=10000 if ri <= 150 else 8000,
                trigger_mode="auto",
                trigger_source=trig_src,
                probe_ch1=self.cfg.probe_ch1,
                probe_ch2=self.cfg.probe_ch2,
            )
            if st.get("error"):
                return {"ok": False, "error": st["error"], "live": st, "passes": history}
            self._meas_ema.clear()
            time.sleep(0.3)
            stats = self._collect_raw_stats(n_frames=14, timeout_s=settle_s + 1.4)
            # Per-frame freqs for stability score (selected channels only)
            from .autoset import _raw_channel_stats as _rcs

            frame_freqs: list[float] = []
            t_end = time.time() + 0.6
            seen = self.status.frames
            while time.time() < t_end and len(frame_freqs) < 10:
                fr = self.latest()
                if fr and self.status.frames > seen:
                    seen = self.status.frames
                    rate_f = float(fr.get("rate_hz") or 1e6)
                    if use1:
                        st1 = _rcs("CH1", fr.get("ch1"), rate_f)
                        if st1.get("frequency_hz"):
                            frame_freqs.append(float(st1["frequency_hz"]))
                    if use2 and fr.get("ch2") is not None:
                        st2 = _rcs("CH2", fr.get("ch2"), rate_f)
                        if st2.get("frequency_hz"):
                            frame_freqs.append(float(st2["frequency_hz"]))
                time.sleep(0.04)
            f1 = (stats.get("CH1") or {}).get("frequency_hz") if use1 else None
            f2 = (stats.get("CH2") or {}).get("frequency_hz") if use2 else None
            cand = [float(x) for x in (f1, f2) if x]
            med = None
            quality = 0.0
            if frame_freqs:
                frame_freqs.sort()
                med = float(frame_freqs[len(frame_freqs) // 2])
                # quality: many samples, low relative spread
                span = frame_freqs[-1] - frame_freqs[0]
                rel = span / med if med else 1.0
                quality = len(frame_freqs) * (1.0 / (1.0 + 8.0 * rel))
                # Prefer rates that can actually resolve the period (≥25 samples/cycle)
                rate_hz = SAMPLE_RATES.get(ri, (None, 0.0))[1]
                if med and rate_hz:
                    spp = rate_hz / med
                    if spp < 15:
                        quality *= 0.15  # barely sampled — distrust
                    elif spp < 25:
                        quality *= 0.5
            elif cand:
                med = max(cand)
                quality = 1.0
            history.append(
                {
                    "pass": f"discover-{ri}",
                    "freq_median": med,
                    "freq_spread": (
                        (max(frame_freqs) - min(frame_freqs)) if frame_freqs else None
                    ),
                    "quality": quality,
                    "rate_index": ri,
                    "channels": sorted(want),
                }
            )
            if med is not None and quality > best_quality:
                best_quality = quality
                locked_f = med
                best_stats = stats

        stats = best_stats or stats
        frame = stats.get("frame") or self.latest()
        if not frame:
            return {"ok": False, "error": "no live frames for autoset", "live": st, "passes": history}

        ch1 = (stats.get("CH1") or {}) if use1 else {}
        ch2 = (stats.get("CH2") or None) if use2 else None
        # --- 2) Build max-detail suggestion from locked discovery stats ---
        # Inject locked frequency into a synthetic frame measure so suggest can't drift
        synth = {
            "ch1": frame.get("ch1") if use1 else None,
            "ch2": frame.get("ch2") if use2 else None,
            "rate_hz": frame.get("rate_hz"),
            "measure": {
                "CH1": (
                    {**ch1, "frequency_hz": locked_f or ch1.get("frequency_hz")}
                    if use1
                    else None
                ),
                "CH2": (
                    {**ch2, "frequency_hz": locked_f or ch2.get("frequency_hz")}
                    if ch2
                    else None
                ),
                "rate_hz": frame.get("rate_hz"),
            },
        }
        # Prefer locked_f explicitly in choose
        rate, disp = choose_rate_and_display(locked_f, self.cfg.display_points)
        r1 = (
            choose_range(float(ch1.get("peak") or 0), float(ch1.get("vpp") or 0), self.cfg.probe_ch1)
            if use1
            else self.cfg.ch1_range
        )
        r2 = (
            choose_range(float(ch2.get("peak") or 0), float(ch2.get("vpp") or 0), self.cfg.probe_ch2)
            if ch2
            else self.cfg.ch2_range
        )
        suggestion = suggest_from_frame(
            synth,
            probe_ch1=self.cfg.probe_ch1,
            probe_ch2=self.cfg.probe_ch2,
            display_points=disp,
            current=self.snapshot_status(),
            channels=want,
        )
        # Override with locked discovery-based rate/display (ignore short-window remeasure)
        suggestion.rate_index = rate
        suggestion.display_points = disp
        suggestion.ch1_range = r1
        suggestion.ch2_range = int(r2)
        suggestion.trigger_mode = "auto"
        suggestion.trigger_source = trig_src if not (use1 and use2) else suggestion.trigger_source
        if locked_f:
            per = periods_on_screen(locked_f, rate, disp)
            spp = samples_per_period(locked_f, rate)
            reason_ranges: list[str] = []
            if use1:
                reason_ranges.append(
                    f"CH1 peak={ch1.get('peak') and round(float(ch1['peak']), 3)} V → {VOLTAGE_RANGES.get(r1, r1)}"
                )
            if ch2:
                reason_ranges.append(
                    f"CH2 peak={ch2.get('peak') and round(float(ch2['peak']), 3)} V → {VOLTAGE_RANGES.get(r2, r2)}"
                )
            suggestion.reasons = [
                *reason_ranges,
                f"locked f≈{locked_f:.4g} Hz → {SAMPLE_RATES[rate][0]} / {disp} pts "
                f"(~{per:.2f} cycles, {spp:.0f} samples/cycle)",
                f"trigger {suggestion.trigger_source} auto-rise (full PTP cycle lock)",
            ]
            suggestion.analysis["freq_hz"] = locked_f
            suggestion.analysis["periods_on_screen"] = per
            suggestion.analysis["samples_per_period"] = spp
            suggestion.analysis["channels"] = sorted(want)

        cfg = suggestion.as_config()
        cfg["probe_ch1"] = self.cfg.probe_ch1
        cfg["probe_ch2"] = self.cfg.probe_ch2
        cfg["num_channels"] = nch
        history.append({"pass": "detail", "suggested": suggestion.to_dict()})
        st = self.apply(**cfg)
        if st.get("error"):
            return {
                "ok": False,
                "error": st["error"],
                "live": st,
                "suggestion": suggestion.to_dict(),
                "passes": history,
            }
        self._meas_ema.clear()
        time.sleep(0.3)
        frame = self._wait_frame(timeout_s=settle_s + 0.5) or frame

        # --- 3) If zoomed view lost the full cycle, step rate down ---
        if locked_f:
            for _ in range(max(0, max_passes - 1)):
                per = periods_on_screen(
                    locked_f, self.cfg.rate_index, self.cfg.display_points
                )
                if per >= MIN_PERIODS_ON_SCREEN:
                    break
                # step to next-slower candidate
                try:
                    idx = RATE_CANDIDATES.index(self.cfg.rate_index)
                except ValueError:
                    idx = len(RATE_CANDIDATES) - 1
                if idx <= 0:
                    break
                slower = RATE_CANDIDATES[idx - 1]
                rate2, disp2 = choose_rate_and_display(locked_f, self.cfg.display_points)
                # force slower than current
                rate2 = slower
                disp2 = max(
                    disp2,
                    int(MIN_PERIODS_ON_SCREEN * SAMPLE_RATES[rate2][1] / locked_f + 0.5),
                )
                disp2 = min(12000, max(3000, disp2))
                history.append(
                    {
                        "pass": "backoff",
                        "periods": per,
                        "new_rate": rate2,
                        "display_points": disp2,
                    }
                )
                st = self.apply(
                    rate_index=rate2,
                    display_points=disp2,
                    ch1_range=suggestion.ch1_range,
                    ch2_range=suggestion.ch2_range,
                    trigger_mode="auto",
                )
                suggestion.rate_index = rate2
                suggestion.display_points = disp2
                self._meas_ema.clear()
                time.sleep(0.25)
                frame = self._wait_frame(timeout_s=settle_s + 0.4) or frame

        final_meas = {
            "CH1": ch1,
            "CH2": ch2,
            "rate_hz": (frame or {}).get("rate_hz"),
            "locked_freq_hz": locked_f,
        }
        return {
            "ok": True,
            "live": self.snapshot_status(),
            "suggestion": suggestion.to_dict(),
            "passes": history,
            "final_measure": final_meas,
        }

    def snapshot_status(self) -> dict[str, Any]:
        cfg = self.cfg
        label, rate = SAMPLE_RATES.get(cfg.rate_index, ("?", 0.0))
        return {
            "running": self.status.running,
            "error": self.status.error,
            "vid_pid": self.status.vid_pid,
            "rate_index": cfg.rate_index,
            "rate_label": label,
            "rate_hz": rate,
            "num_channels": cfg.num_channels,
            "ch1_range": cfg.ch1_range,
            "ch2_range": cfg.ch2_range,
            "ch1_range_label": VOLTAGE_RANGES.get(cfg.ch1_range, "?"),
            "ch2_range_label": VOLTAGE_RANGES.get(cfg.ch2_range, "?"),
            "probe_ch1": cfg.probe_ch1,
            "probe_ch2": cfg.probe_ch2,
            "frames": self.status.frames,
            "drops": self.status.drops,
            "fps": self.status.last_fps,
            "trigger_mode": cfg.trigger_mode,
            "trigger_level": cfg.trigger_level,
            "trigger_source": cfg.trigger_source,
            "sample_rates": {str(k): v[0] for k, v in SAMPLE_RATES.items()},
            "voltage_ranges": VOLTAGE_RANGES,
        }

    def _software_trigger(self, y: np.ndarray, disp: int) -> tuple[int, float, bool]:
        """
        Return (start_index, trigger_level_used, locked).
        Auto mode locks on a rising edge at mid so a repeating wave stays put.
        Picks the *newest* edge that still leaves a full pre/post window — the
        oldest-edge path made the live view walk as the ring buffer slid.
        """
        mode = (self.cfg.trigger_mode or "auto").lower()
        free = max(0, y.size - disp)
        if y.size < 32:
            return free, float(self.cfg.trigger_level), False
        vmin = float(np.min(y))
        vmax = float(np.max(y))
        vpp = vmax - vmin
        mid = 0.5 * (vmin + vmax)
        if mode == "off":
            return free, mid, False
        if mode == "auto":
            raw_lvl = mid
            edge = "rising"
        else:
            raw_lvl = float(self.cfg.trigger_level)
            if raw_lvl < vmin or raw_lvl > vmax:
                raw_lvl = mid
            edge = "rising" if mode == "rising" else "falling"
        # Smooth level so ADC noise doesn't hop the trip point every frame
        if self._trig_lvl_ema is None:
            self._trig_lvl_ema = raw_lvl
        else:
            self._trig_lvl_ema = 0.85 * self._trig_lvl_ema + 0.15 * raw_lvl
        lvl = float(self._trig_lvl_ema)
        hold = int(disp * float(self.cfg.trigger_holdoff_frac))
        post = max(16, disp - hold)
        # Schmitt-ish: ignore chatter within ±4% of Vpp around the trip level
        band = max(0.04 * vpp, 1e-3)
        if edge == "rising":
            edges = np.where((y[:-1] < (lvl - band * 0.25)) & (y[1:] >= lvl))[0]
        else:
            edges = np.where((y[:-1] > (lvl + band * 0.25)) & (y[1:] <= lvl))[0]
        if edges.size == 0:
            return free, lvl, False
        # Valid edges: enough pre-trigger AND post-trigger samples
        ok = edges[(edges >= hold) & (edges + post <= y.size)]
        if ok.size == 0:
            return free, lvl, False
        # Newest valid edge → stable live lock as buffer rolls forward
        chosen = int(ok[-1])
        return chosen - hold, lvl, True

    def _emit(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._latest = frame
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(frame)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            self._find_and_open()
            self._apply_config()
            self._start_hw()
            self.status.running = True
            nch = 2 if self.cfg.num_channels >= 2 else 1
            block = max(512, int(self.cfg.block_samples))
            nbytes = block * nch
            # Ring buffer so trigger can lock across block boundaries
            buf1: deque[np.ndarray] = deque()
            buf2: deque[np.ndarray] = deque()
            buf_len = 0
            # Keep ≥4 display windows so a newest-edge lock always has room
            need = max(int(self.cfg.display_points) * 4, block * 3)
            assert self._handle is not None
            while not self._stop.is_set():
                # Wait if SETCALFREQ (or similar) needs a quiet USB control path
                if not self._acq_gate.is_set():
                    try:
                        self._stop_hw()
                    except Exception:
                        pass
                    self._acq_gate.wait(timeout=0.5)
                    if self._stop.is_set():
                        break
                    if self._acq_gate.is_set():
                        try:
                            self._start_hw()
                        except Exception:
                            pass
                    continue
                try:
                    raw = self._handle.bulkRead(0x86, nbytes, timeout=2000)
                except Exception as exc:
                    self.status.drops += 1
                    if self.status.drops > 20:
                        raise RuntimeError(f"too many USB drops: {exc}") from exc
                    time.sleep(0.02)
                    continue
                data = np.frombuffer(raw, dtype=np.uint8)
                if nch == 2:
                    if data.size < 2:
                        continue
                    if data.size & 1:
                        data = data[:-1]
                    ch1_adc = data[0::2]
                    ch2_adc = data[1::2]
                else:
                    ch1_adc = data
                    ch2_adc = None

                y1 = _scale_adc(ch1_adc, self.cfg.ch1_range, self.cfg.probe_ch1)
                y2 = (
                    _scale_adc(ch2_adc, self.cfg.ch2_range, self.cfg.probe_ch2)
                    if ch2_adc is not None
                    else None
                )
                buf1.append(y1)
                buf_len += y1.size
                if y2 is not None:
                    buf2.append(y2)
                while buf_len > need and buf1:
                    dropped = buf1.popleft()
                    buf_len -= dropped.size
                    if buf2:
                        buf2.popleft()

                # Wait until ring is deep enough for a real lock window
                disp = int(self.cfg.display_points)
                if buf_len < disp * 2:
                    continue

                y1_all = np.concatenate(list(buf1)) if buf1 else y1
                y2_all = np.concatenate(list(buf2)) if buf2 else None

                src = y1_all if self.cfg.trigger_source.upper() != "CH2" or y2_all is None else y2_all
                start, trig_lvl, locked = self._software_trigger(src, disp)
                end = min(start + disp, y1_all.size)
                y1d = y1_all[start:end]
                y2d = y2_all[start:end] if y2_all is not None else None
                if y1d.size < 16:
                    continue
                # downsample for websocket if huge
                if y1d.size > disp:
                    idx = np.linspace(0, y1d.size - 1, disp).astype(np.int32)
                    y1d = y1d[idx]
                    if y2d is not None:
                        y2d = y2d[idx]
                rate = self.status.rate_hz or 1e6
                t = (np.arange(y1d.size, dtype=np.float64) / rate).tolist()
                meas = _full_meas(y1d, y2d, rate)
                meas = _smooth_meas(meas, self._meas_ema)
                # Cursors from trigger source channel (+ timing from that channel's measure)
                src_key = (
                    "CH2"
                    if self.cfg.trigger_source.upper() == "CH2" and y2d is not None
                    else "CH1"
                )
                src_d = y2d if src_key == "CH2" else y1d
                cursors = _cursors(src_d, trig_lvl, rate, meas.get(src_key) or {})
                # Same hold index used when slicing — keeps the T marker on the edge
                hold_i = min(len(t) - 1, int(disp * float(self.cfg.trigger_holdoff_frac)))
                frame = {
                    "t": t,
                    "ch1": y1d.tolist(),
                    "ch2": y2d.tolist() if y2d is not None else None,
                    "rate_hz": rate,
                    "frame": self.status.frames,
                    "measure": meas,
                    "cursors": cursors,
                    "trigger": {
                        "mode": self.cfg.trigger_mode,
                        "level": trig_lvl,
                        "source": self.cfg.trigger_source,
                        "index": hold_i,
                        "locked": locked,
                    },
                }
                self.status.frames += 1
                now = time.perf_counter()
                self._fps_times.append(now)
                if len(self._fps_times) >= 2:
                    dt = self._fps_times[-1] - self._fps_times[0]
                    if dt > 0:
                        self.status.last_fps = (len(self._fps_times) - 1) / dt
                self._emit(frame)
                # Pace UI ~30 fps; USB still fills the ring every block
                time.sleep(0.02)
        except Exception as exc:
            self.status.error = str(exc)
            self.status.running = False
        finally:
            try:
                self._stop_hw()
            except Exception:
                pass
            if self._handle is not None:
                try:
                    self._handle.releaseInterface(0)
                except Exception:
                    pass
                try:
                    self._handle.close()
                except Exception:
                    pass
                self._handle = None
            if self._ctx is not None:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
            self.status.running = False


def _full_meas(y1: np.ndarray, y2: np.ndarray | None, rate: float) -> dict[str, Any]:
    from .measure import measure_channel

    def one(name: str, y: np.ndarray) -> dict[str, Any]:
        m = measure_channel(name, y, rate, probe_factor=1.0)
        d = m.to_dict()
        mid = None
        if d["vmin"] is not None and d["vmax"] is not None:
            mid = 0.5 * (d["vmin"] + d["vmax"])
        d["mid"] = mid
        if d.get("period_s"):
            d["period_ms"] = d["period_s"] * 1000.0
            d["period_us"] = d["period_s"] * 1e6
        return d

    out: dict[str, Any] = {"CH1": one("CH1", y1), "rate_hz": rate}
    if y2 is not None:
        out["CH2"] = one("CH2", y2)
    return out


_SMOOTH_KEYS = (
    "vmin",
    "vmax",
    "vpp",
    "vavg",
    "vrms",
    "mid",
    "frequency_hz",
    "duty_high_pct",
    "period_s",
    "rise_time_s",
    "fall_time_s",
)


def _smooth_meas(
    meas: dict[str, Any],
    store: dict[str, dict[str, float]],
    alpha: float = 0.25,
) -> dict[str, Any]:
    """EMA numeric fields so UI digits don't jump every frame."""
    out = dict(meas)
    for ch in ("CH1", "CH2"):
        src = meas.get(ch)
        if not isinstance(src, dict):
            continue
        prev = store.setdefault(ch, {})
        dst = dict(src)
        for k in _SMOOTH_KEYS:
            v = src.get(k)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if k in prev:
                fv = (1.0 - alpha) * prev[k] + alpha * fv
            prev[k] = fv
            dst[k] = fv
        if dst.get("period_s"):
            dst["period_ms"] = dst["period_s"] * 1000.0
            dst["period_us"] = dst["period_s"] * 1e6
        out[ch] = dst
    return out


def _cursors(
    y: np.ndarray,
    trig_lvl: float,
    rate: float,
    ch_meas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ch_meas = ch_meas or {}
    # Prefer smoothed channel peaks when available so cursor lines stay put
    vmin = ch_meas.get("vmin")
    vmax = ch_meas.get("vmax")
    if vmin is None or vmax is None:
        vmin = float(np.min(y))
        vmax = float(np.max(y))
    else:
        vmin = float(vmin)
        vmax = float(vmax)
    mid = ch_meas.get("mid")
    mid = float(mid) if mid is not None else 0.5 * (vmin + vmax)
    return {
        "vmin": vmin,
        "vmax": vmax,
        "mid": mid,
        "trigger": float(trig_lvl),
        "vpp": vmax - vmin,
        "frequency_hz": ch_meas.get("frequency_hz"),
        "duty_high_pct": ch_meas.get("duty_high_pct"),
        "period_s": ch_meas.get("period_s"),
        "rate_hz": rate,
    }


# Process-wide singleton
SCOPE = LiveScope()
