"""
Fast live logic analyzer via persistent sigrok-cli fx2lafw --continuous -O binary.

One long-lived process streams packed logic bytes (1 sample/byte, bit0=first
channel). A reader thread fills a ring buffer; a frame thread emits ~25 FPS
WebSocket frames with software edge-lock + digital measurements.

One-shot capture/decode still uses sigrok CSV via sigrok_bridge (stopped live).
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import measure as meas
from . import sigrok_bridge as bridge
from .usb_probe import find_sigrok_cli, probe as usb_probe

LA_RATES: dict[str, float] = {
    "100k": 100e3,
    "200k": 200e3,
    "500k": 500e3,
    "1m": 1e6,
    "2m": 2e6,
    "3m": 3e6,
    "4m": 4e6,
    "6m": 6e6,
    "8m": 8e6,
    "12m": 12e6,
    "16m": 16e6,
    "24m": 24e6,
    "48m": 48e6,
}

DEFAULT_CHANNELS = "D0,D1,D2,D3,D4,D5,D6,D7"


@dataclass
class LaLiveConfig:
    samplerate: str = "6m"
    channels: str = DEFAULT_CHANNELS
    # Display window: ~100 ms at 6 MS/s so UART bursts (e.g. every 50 ms) are visible
    samples: str = "600k"
    time_ms: int | None = None
    display_points: int = 8000
    # off | rising | falling | high | low | either  (software lock on ring)
    trigger_mode: str = "off"
    trigger_channel: str = "D0"
    trigger_timeout_s: float = 1.5
    history: int = 3
    target_fps: float = 30.0
    ring_seconds: float = 0.35  # keep ~350 ms of stream


@dataclass
class LaLiveStatus:
    running: bool = False
    error: str | None = None
    rate_hz: float = 6e6
    rate_label: str = "6 MHz"
    frames: int = 0
    drops: int = 0
    last_fps: float = 0.0
    channels: str = DEFAULT_CHANNELS
    trigger_mode: str = "off"
    last_path: str | None = None
    stream_bps: float = 0.0
    backend: str = "continuous-binary"


def _rate_hz(label: str) -> float:
    return float(LA_RATES.get(label.lower(), 6e6))


def _rate_label(samplerate: str) -> str:
    hz = _rate_hz(samplerate)
    if hz >= 1e6:
        return f"{hz / 1e6:g} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:g} kHz"
    return f"{hz:g} Hz"


def _parse_samples(s: str) -> int:
    raw = (s or "40k").strip().lower()
    if raw.endswith("m"):
        return int(float(raw[:-1]) * 1e6)
    if raw.endswith("k"):
        return int(float(raw[:-1]) * 1e3)
    return int(float(raw))


def _trigger_spec(mode: str, channel: str) -> str | None:
    ch = bridge.normalize_la_channels(channel).split(",")[0]
    m = (mode or "off").lower()
    if m in {"off", "none", "free", "free-run"}:
        return None
    edge = {
        "rising": "r",
        "rise": "r",
        "falling": "f",
        "fall": "f",
        "high": "1",
        "low": "0",
        "either": "e",
        "any": "e",
        "edge": "e",
    }.get(m)
    if not edge:
        return None
    return f"{ch}={edge}"


def _unpack_bits(packed: np.ndarray, n_ch: int) -> np.ndarray:
    """packed uint8 [N] → bits uint8 [N, n_ch], bit i = channel i."""
    out = np.empty((packed.size, n_ch), dtype=np.uint8)
    for i in range(n_ch):
        out[:, i] = (packed >> i) & 1
    return out


def _bit_channel(packed: np.ndarray, bit: int) -> np.ndarray:
    return ((packed >> bit) & 1).astype(np.uint8)


def _window_from_packed(
    packed: np.ndarray,
    names: list[str],
    mode: str,
    channel: str,
    window: int,
) -> np.ndarray:
    """Edge-lock on packed stream; return unpacked bits [window, n_ch] only."""
    n = packed.size
    n_ch = len(names)
    if n <= 0:
        return np.zeros((0, n_ch), dtype=np.uint8)
    window = min(window, n)
    mode = (mode or "off").lower()
    start = n - window
    if mode not in {"off", "none", "free", "free-run"}:
        try:
            ci = names.index(bridge.normalize_la_channels(channel).split(",")[0])
        except ValueError:
            ci = -1
        if ci >= 0:
            search_n = min(n, window * 3)
            search = _bit_channel(packed[-search_n:], ci)
            base = n - search.size
            if mode in {"rising", "rise"}:
                idx = np.where((search[:-1] == 0) & (search[1:] == 1))[0]
            elif mode in {"falling", "fall"}:
                idx = np.where((search[:-1] == 1) & (search[1:] == 0))[0]
            elif mode in {"either", "any", "edge"}:
                idx = np.where(search[:-1] != search[1:])[0]
            elif mode == "high":
                idx = np.where(search == 1)[0]
            elif mode == "low":
                idx = np.where(search == 0)[0]
            else:
                idx = np.array([], dtype=np.int64)
            if idx.size:
                edge = int(base + idx[-1] + 1)
                pre = int(window * 0.08)
                start = max(0, min(edge - pre, n - window))
    return _unpack_bits(packed[start : start + window], n_ch)


class LiveLA:
    """Continuous binary LA streamer — singleton-friendly."""

    def __init__(self) -> None:
        self.cfg = LaLiveConfig()
        self.status = LaLiveStatus()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._framer: threading.Thread | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._latest: dict[str, Any] | None = None
        self._fps_times: deque[float] = deque(maxlen=40)
        self._meas_ema: dict[str, dict[str, float]] = {}
        self._ring = np.zeros(0, dtype=np.uint8)
        self._ring_cap = 0
        self._ring_len = 0
        self._ring_write = 0
        self._bytes_total = 0
        self._bytes_t0 = 0.0
        self._chan_names: list[str] = []
        self._meas_every = 2  # measure every Nth frame (plot every frame)
        self._meas_i = 0
        self._last_measure: dict[str, Any] = {}

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

    def snapshot_status(self) -> dict[str, Any]:
        with self._lock:
            st = self.status
            return {
                "running": st.running,
                "error": st.error,
                "fps": st.last_fps,
                "frames": st.frames,
                "drops": st.drops,
                "rate_hz": st.rate_hz,
                "rate_label": st.rate_label,
                "channels": st.channels,
                "samplerate": self.cfg.samplerate,
                "samples": self.cfg.samples,
                "time_ms": self.cfg.time_ms,
                "display_points": self.cfg.display_points,
                "trigger_mode": self.cfg.trigger_mode,
                "trigger_channel": self.cfg.trigger_channel,
                "last_path": st.last_path,
                "stream_bps": st.stream_bps,
                "backend": st.backend,
            }

    def start(self, **kwargs: Any) -> dict[str, Any]:
        pr = usb_probe()
        if pr.mode != "la":
            self.status.error = (
                f"Need LA mode (got '{pr.mode}'). Unplug → release H/P → replug."
            )
            self.status.running = False
            return self.snapshot_status()
        try:
            from .live_stream import SCOPE

            if SCOPE.status.running:
                SCOPE.stop()
        except Exception:  # noqa: BLE001
            pass
        self.configure(**kwargs)
        with self._lock:
            if self._reader and self._reader.is_alive():
                return self.snapshot_status()
            self._stop.clear()
            self.status.error = None
            self.status.running = True
            self.status.frames = 0
            self.status.drops = 0
            self.status.backend = "continuous-binary"
            self._meas_ema.clear()
            self._init_ring()
            try:
                self._spawn_stream()
            except Exception as exc:  # noqa: BLE001
                self.status.error = str(exc)
                self.status.running = False
                return self.snapshot_status()
            self._reader = threading.Thread(target=self._read_loop, name="LiveLA-read", daemon=True)
            self._framer = threading.Thread(target=self._frame_loop, name="LiveLA-frame", daemon=True)
            self._reader.start()
            self._framer.start()
        return self.snapshot_status()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._kill_proc()
        for t in (self._reader, self._framer):
            if t and t.is_alive():
                t.join(timeout=2.0)
        with self._lock:
            self.status.running = False
            self._reader = None
            self._framer = None
            self._proc = None
        return self.snapshot_status()

    def configure(self, **kwargs: Any) -> None:
        with self._lock:
            for k, v in kwargs.items():
                if v is None:
                    continue
                if hasattr(self.cfg, k):
                    setattr(self.cfg, k, v)
            self.cfg.channels = bridge.normalize_la_channels(self.cfg.channels)
            self.status.channels = self.cfg.channels
            self.status.rate_hz = _rate_hz(self.cfg.samplerate)
            self.status.rate_label = _rate_label(self.cfg.samplerate)
            self.status.trigger_mode = self.cfg.trigger_mode
            self._chan_names = [c.strip() for c in self.cfg.channels.split(",") if c.strip()]

    def apply(self, **kwargs: Any) -> dict[str, Any]:
        """Rate/channel changes need stream restart; trigger/window are soft."""
        hard = {"samplerate", "channels"}
        keys = {k for k, v in kwargs.items() if v is not None}
        need_restart = bool(keys & hard) and self.status.running
        if need_restart:
            self.stop()
            # Let WinUSB/sigrok release the device before reopen
            time.sleep(0.15)
            self.configure(**kwargs)
            return self.start()
        self.configure(**kwargs)
        return self.snapshot_status()

    def capture_once(
        self,
        *,
        use_trigger: bool | None = False,
        out: Path | None = None,
    ) -> dict[str, Any]:
        """
        One-shot CSV capture (stops continuous briefly).

        Default use_trigger=False — free-run shot. Live edge-lock stays software
        on the continuous stream; HW --wait-trigger is easy to hang on idle lines.
        """
        pr = usb_probe()
        if pr.mode != "la":
            return {"ok": False, "error": f"Need LA mode (got '{pr.mode}')"}
        was = self.status.running
        if was:
            self.stop()
            time.sleep(0.45)  # WinUSB release after continuous stream
        trig = None
        wait = False
        if use_trigger is True:
            trig = _trigger_spec(self.cfg.trigger_mode, self.cfg.trigger_channel)
            wait = bool(trig)
        result: dict[str, Any]
        try:
            cap = bridge.la_capture(
                samples=self.cfg.samples,
                samplerate=self.cfg.samplerate,
                channels=self.cfg.channels,
                time_ms=self.cfg.time_ms,
                trigger=trig,
                wait_trigger=wait,
                out=out,
                timeout_s=self.cfg.trigger_timeout_s if wait else 60.0,
            )
            if not cap.ok or not cap.path:
                result = {
                    "ok": False,
                    "error": (cap.cmd.stderr or "capture failed").strip(),
                    "cmd": cap.cmd.to_dict(),
                }
            else:
                summary = meas.measure_la_csv(
                    Path(cap.path),
                    max_plot_points=self.cfg.display_points,
                )
                result = {"ok": True, "capture": cap.to_dict(), "summary": summary}
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc)}
        if was:
            time.sleep(0.2)
            self.start()
        return result

    def autoset(self, *, max_passes: int = 3) -> dict[str, Any]:
        pr = usb_probe()
        if pr.mode != "la":
            return {"ok": False, "error": f"Need LA mode (got '{pr.mode}')"}
        if self.status.running:
            self.stop()

        # Fast discovery via short continuous peek
        reasons: list[str] = []
        best: dict[str, Any] | None = None
        for rate in ("2m", "6m", "12m"):
            self.configure(samplerate=rate, channels=DEFAULT_CHANNELS, trigger_mode="off", samples="40k")
            peek = self._peek_stream(duration_s=0.12)
            if peek is None:
                reasons.append(f"{rate}: peek failed")
                continue
            names, bits, hz = peek
            chans = [
                meas.measure_la_channel(names[i], bits[:, i], hz) for i in range(len(names))
            ]
            active = [c for c in chans if not c.get("stuck")]
            if not active:
                reasons.append(f"{rate}: no edges")
                continue
            freqs = [c.get("frequency_hz") for c in active if c.get("frequency_hz")]
            f_lock = float(np.median(freqs)) if freqs else None
            best = {
                "rate": rate,
                "active": [c["name"] for c in active],
                "f_lock": f_lock,
                "channels": chans,
                "names": names,
                "bits": bits,
                "hz": hz,
            }
            reasons.append(
                f"{rate}: active={','.join(best['active'])}"
                + (f" f≈{f_lock:.1f}Hz" if f_lock else "")
            )
            break

        if not best:
            self.configure(samplerate="6m", samples="40k", channels=DEFAULT_CHANNELS)
            live = self.start()
            return {
                "ok": False,
                "error": "No digital activity on D0-D7 (floating/idle).",
                "reasons": reasons,
                "live": live,
            }

        f_lock = best["f_lock"]
        active_names = best["active"]
        if f_lock and f_lock > 0:
            want_s = (1.0 / f_lock) * 3.2
            chosen_rate = "12m"
            for rate, rhz in sorted(LA_RATES.items(), key=lambda kv: -kv[1]):
                if rhz > 24e6:
                    continue
                n = int(want_s * rhz)
                if 2000 <= n <= 200_000:
                    chosen_rate = rate
                    break
            samples_n = max(4000, min(200_000, int(want_s * _rate_hz(chosen_rate))))
            samples_s = f"{max(1, int(round(samples_n / 1000)))}k"
            reasons.append(f"detail: {chosen_rate} · {samples_s} (~{want_s*1e3:.2f} ms)")
        else:
            chosen_rate = "6m"
            samples_s = "40k"
            reasons.append("detail: no stable f → 6m / 40k")

        trig_ch = active_names[0]
        self.configure(
            samplerate=chosen_rate,
            samples=samples_s,
            channels=DEFAULT_CHANNELS,
            trigger_mode="rising",
            trigger_channel=trig_ch,
        )
        live = self.start()
        # Build summary from peek bits for immediate UI
        names, bits, hz = best["names"], best["bits"], best["hz"]
        win = min(bits.shape[0], _parse_samples(samples_s))
        view = bits[-win:]
        summary = self._frame_from_bits(view, names, hz, path=None)
        return {
            "ok": True,
            "reasons": reasons,
            "active_channels": active_names,
            "locked_frequency_hz": f_lock,
            "suggestion": {
                "samplerate": chosen_rate,
                "samples": samples_s,
                "channels": DEFAULT_CHANNELS,
                "trigger_mode": "rising",
                "trigger_channel": trig_ch,
            },
            "summary": summary,
            "live": live,
        }

    # ── stream internals ─────────────────────────────────────────────

    def _init_ring(self) -> None:
        hz = _rate_hz(self.cfg.samplerate)
        cap = max(8192, int(hz * float(self.cfg.ring_seconds)))
        self._ring = np.zeros(cap, dtype=np.uint8)
        self._ring_cap = cap
        self._ring_len = 0
        self._ring_write = 0
        self._bytes_total = 0
        self._bytes_t0 = time.perf_counter()
        self._chan_names = [c.strip() for c in self.cfg.channels.split(",") if c.strip()]

    def _spawn_stream(self) -> None:
        sk = find_sigrok_cli()
        if not sk:
            raise RuntimeError("sigrok-cli not found (HTK_SIGROK_CLI)")
        chans = self.cfg.channels
        argv = [
            str(sk),
            "--driver",
            "fx2lafw",
            "--config",
            f"samplerate={self.cfg.samplerate}",
            "--channels",
            chans,
            "--continuous",
            "--output-format",
            "binary",
        ]
        # Binary to stdout; stderr discarded (noisy)
        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if not self._proc.stdout:
            raise RuntimeError("sigrok stdout missing")

    def _kill_proc(self) -> None:
        p = self._proc
        self._proc = None
        if not p:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=0.5)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def _push_ring(self, chunk: bytes) -> None:
        if not chunk:
            return
        arr = np.frombuffer(chunk, dtype=np.uint8)
        n = arr.size
        cap = self._ring_cap
        if n >= cap:
            self._ring[:] = arr[-cap:]
            self._ring_write = 0
            self._ring_len = cap
        else:
            end = self._ring_write + n
            if end <= cap:
                self._ring[self._ring_write : end] = arr
            else:
                first = cap - self._ring_write
                self._ring[self._ring_write :] = arr[:first]
                self._ring[: n - first] = arr[first:]
            self._ring_write = (self._ring_write + n) % cap
            self._ring_len = min(cap, self._ring_len + n)
        self._bytes_total += n

    def _snapshot_ring(self) -> np.ndarray:
        with self._lock:
            n = self._ring_len
            if n <= 0:
                return np.zeros(0, dtype=np.uint8)
            cap = self._ring_cap
            w = self._ring_write
            if n < cap:
                return self._ring[:n].copy()
            # full ring: chronological from write pointer
            return np.concatenate((self._ring[w:], self._ring[:w])).copy()

    def _read_loop(self) -> None:
        p = self._proc
        if not p or not p.stdout:
            return
        try:
            while not self._stop.is_set():
                chunk = p.stdout.read(65536)
                if not chunk:
                    if p.poll() is not None:
                        with self._lock:
                            self.status.error = f"sigrok exited ({p.returncode})"
                            self.status.drops += 1
                        break
                    time.sleep(0.001)
                    continue
                with self._lock:
                    self._push_ring(chunk)
                    dt = time.perf_counter() - self._bytes_t0
                    if dt >= 0.5:
                        self.status.stream_bps = self._bytes_total / dt
                        self._bytes_total = 0
                        self._bytes_t0 = time.perf_counter()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.status.error = str(exc)
        finally:
            self._kill_proc()

    def _frame_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                frame = self._build_frame()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self.status.error = str(exc)
                    self.status.drops += 1
                time.sleep(0.05)
                continue
            if frame is None:
                time.sleep(0.01)
                continue
            now = time.perf_counter()
            self._fps_times.append(now)
            fps = 0.0
            if len(self._fps_times) >= 2:
                dt = self._fps_times[-1] - self._fps_times[0]
                if dt > 0:
                    fps = (len(self._fps_times) - 1) / dt
            with self._lock:
                self._latest = frame
                self.status.frames += 1
                self.status.last_fps = fps
                self.status.error = None
                listeners = list(self._listeners)
            for fn in listeners:
                try:
                    fn(frame)
                except Exception:  # noqa: BLE001
                    pass
            # Pace to target FPS
            period = 1.0 / max(5.0, float(self.cfg.target_fps))
            elapsed = time.perf_counter() - t0
            if elapsed < period:
                time.sleep(period - elapsed)
        with self._lock:
            self.status.running = False

    def _build_frame(self) -> dict[str, Any] | None:
        packed = self._snapshot_ring()
        if packed.size < 64:
            return None
        with self._lock:
            names = list(self._chan_names)
            hz = float(self.status.rate_hz)
            mode = self.cfg.trigger_mode
            tch = self.cfg.trigger_channel
            want = _parse_samples(self.cfg.samples)
            dpoints = int(self.cfg.display_points)
        if not names:
            return None
        view = _window_from_packed(packed, names, mode, tch, min(want, packed.size))
        return self._frame_from_bits(view, names, hz, path=None, display_points=dpoints)

    def _frame_from_bits(
        self,
        bits: np.ndarray,
        names: list[str],
        hz: float,
        path: str | None,
        display_points: int = 4000,
    ) -> dict[str, Any]:
        n = bits.shape[0]
        # Downsample for plot
        if n <= display_points:
            idx = np.arange(n)
            plotted = bits
        else:
            idx = np.linspace(0, n - 1, display_points).astype(np.int64)
            plotted = bits[idx]
        t = (idx.astype(np.float64) / hz) if hz else idx.astype(np.float64)
        series = {names[i]: plotted[:, i].astype(int).tolist() for i in range(len(names))}

        self._meas_i += 1
        do_meas = (self._meas_i % max(1, self._meas_every) == 0) or not self._last_measure
        if do_meas:
            # Measure on at most 80k samples for speed
            if n > 80000:
                step = n // 80000
                mbits = bits[::step]
                mhz = hz / step
            else:
                mbits = bits
                mhz = hz
            measure: dict[str, Any] = {}
            for i, name in enumerate(names):
                ch = meas.measure_la_channel(name, mbits[:, i], mhz)
                prev = self._meas_ema.get(name, {})
                out_ch = dict(ch)
                for key in ("frequency_hz", "duty_high_pct", "period_s", "pulse_width_s"):
                    v = ch.get(key)
                    if v is None:
                        continue
                    if key in prev:
                        out_ch[key] = 0.75 * prev[key] + 0.25 * float(v)
                    else:
                        out_ch[key] = float(v)
                    prev[key] = float(out_ch[key])
                self._meas_ema[name] = prev
                measure[name] = out_ch
            self._last_measure = measure
        else:
            measure = self._last_measure

        return {
            "type": "la",
            "t": t.tolist(),
            "series": series,
            "channels": names,
            "measure": measure,
            "samplerate_hz": hz,
            "n_samples": n,
            "duration_s": (n / hz) if hz else None,
            "rate_label": _rate_label(self.cfg.samplerate),
            "trigger": {
                "mode": self.cfg.trigger_mode,
                "channel": self.cfg.trigger_channel,
                "armed": self.cfg.trigger_mode not in {"off", "none", ""},
                "kind": "software",
            },
            "path": path,
            "backend": "continuous-binary",
            "ts": time.time(),
        }

    def _peek_stream(self, *, duration_s: float = 0.1) -> tuple[list[str], np.ndarray, float] | None:
        """Brief continuous grab for autoset discovery (not the long-lived stream)."""
        sk = find_sigrok_cli()
        if not sk:
            return None
        chans = bridge.normalize_la_channels(self.cfg.channels or DEFAULT_CHANNELS)
        names = [c.strip() for c in chans.split(",") if c.strip()]
        hz = _rate_hz(self.cfg.samplerate)
        argv = [
            str(sk),
            "--driver",
            "fx2lafw",
            "--config",
            f"samplerate={self.cfg.samplerate}",
            "--channels",
            chans,
            "--continuous",
            "--output-format",
            "binary",
        ]
        try:
            p = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            return None
        buf = bytearray()
        t0 = time.perf_counter()
        try:
            assert p.stdout is not None
            while time.perf_counter() - t0 < duration_s:
                chunk = p.stdout.read(65536)
                if chunk:
                    buf.extend(chunk)
                elif p.poll() is not None:
                    break
            # keep reading a hair more to drain
            if p.stdout:
                try:
                    chunk = p.stdout.read(65536)
                    if chunk:
                        buf.extend(chunk)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                p.terminate()
                p.wait(timeout=0.8)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        if len(buf) < 64:
            return None
        packed = np.frombuffer(bytes(buf), dtype=np.uint8)
        bits = _unpack_bits(packed, len(names))
        return names, bits, hz


LA = LiveLA()
