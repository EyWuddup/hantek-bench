"""
Hantek Bench — local web UI (FastAPI) + live USB stream (OpenHantek replacement).

  python -m hantek_navigator.webapp
  → http://127.0.0.1:8787
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analog_decode
from . import bus_catalog
from . import calfreq
from . import measure as meas
from . import probe_cal
from . import probe_db
from . import sigrok_bridge as bridge
from .live_la import LA, LA_RATES, DEFAULT_CHANNELS as LA_DEFAULT_CHANNELS
from .live_stream import SCOPE, SAMPLE_RATES, VOLTAGE_RANGES
from .paths import package_dir
from .policy import POLICY_TEXT
from .usb_probe import probe as usb_probe

STATIC = package_dir() / "static"

app = FastAPI(title="Hantek Bench", version="0.5.0")


def _ensure_not_live() -> None:
    """Sigrok one-shots and live streams cannot share the USB device."""
    if SCOPE.status.running:
        SCOPE.stop()
    if LA.status.running:
        LA.stop()


class CaptureBody(BaseModel):
    channels: str = "CH1,CH2"
    samplerate: str = "1m"
    samples: str = "20k"
    probe_factor_ch1: float = 1.0
    probe_factor_ch2: float = 10.0


class CalFreqBody(BaseModel):
    freq_hz: float = 1000


class ProbeBody(BaseModel):
    name: str
    model: str = ""
    attenuation: float = 1.0
    channel_default: str = "CH2"
    notes: str = ""


class ProbeCalBody(BaseModel):
    name: str
    model: str = "PP80B"
    attenuation: float = 10.0
    channel: str = "CH2"
    notes: str = ""
    frequencies_hz: list[int] | None = None


class DecodeBody(BaseModel):
    bus: str = "rs485"
    wiring: Literal["single", "differential"] = "differential"
    channel: str = "CH1"
    baud: float | None = None
    threshold: float | None = None
    idle_high: bool = True
    samplerate: str = "2m"
    samples: str = "50k"
    probe_factor_ch1: float = 1.0
    probe_factor_ch2: float = 10.0


class LiveStartBody(BaseModel):
    rate_index: int = 1
    num_channels: int = 2
    ch1_range: int = 1
    ch2_range: int = 1
    probe_ch1: float = 1.0
    probe_ch2: float = 10.0
    display_points: int = 4000
    trigger_mode: str = "auto"
    trigger_level: float = 0.0
    trigger_source: str = "CH1"
    block_samples: int = 4096


class LiveConfigBody(BaseModel):
    rate_index: int | None = None
    num_channels: int | None = None
    ch1_range: int | None = None
    ch2_range: int | None = None
    probe_ch1: float | None = None
    probe_ch2: float | None = None
    display_points: int | None = None
    trigger_mode: str | None = None
    trigger_level: float | None = None
    trigger_source: str | None = None


def _waveform_payload(
    path: Path,
    *,
    pf1: float,
    pf2: float,
) -> dict[str, Any]:
    names, data, comments, sr = meas.load_sigrok_csv(path)
    t = (np.arange(data.shape[0]) / sr).tolist() if sr else list(range(data.shape[0]))
    series = []
    factors = [pf1, pf2]
    for i, name in enumerate(names):
        fac = factors[i] if i < len(factors) else 1.0
        y = (data[:, i] * fac).tolist()
        series.append({"name": name, "probe_factor": fac, "y": y})
    m = meas.measure_csv(path, probe_factor=1.0)
    # re-measure per channel with factors
    chans = []
    for i, c in enumerate(m.channels):
        fac = factors[i] if i < len(factors) else 1.0
        cm = meas.measure_channel(c.name, data[:, i], sr, probe_factor=fac)
        chans.append(cm.to_dict())
    return {
        "path": str(path),
        "samplerate_hz": sr,
        "t": t,
        "series": series,
        "comments": comments,
        "measure": {"channels": chans, "samplerate_hz": sr},
    }


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    pr = usb_probe()
    return {
        "probe": pr.to_dict(),
        "cal_steps_hz": calfreq.CAL_FREQ_STEPS_HZ,
        "policy": POLICY_TEXT.splitlines()[0],
        "live": SCOPE.snapshot_status(),
        "la_live": LA.snapshot_status(),
        "sample_rates": {str(k): v[0] for k, v in SAMPLE_RATES.items()},
        "voltage_ranges": VOLTAGE_RANGES,
        "la_rates": list(LA_RATES.keys()),
        "la_decoders": bridge.LA_DECODER_PRESETS,
        "mode_ui": {
            "scope_ready": pr.mode == "scope",
            "la_ready": pr.mode == "la",
            "toggle_hint": (
                "H/P pressed = scope. To enable LA: unplug USB → release H/P → replug."
                if pr.mode == "scope"
                else (
                    "LA mode active (fx2lafw). For scope: unplug → press H/P in → replug."
                    if pr.mode == "la"
                    else "Plug the 6022BL USB cable."
                )
            ),
        },
    }


class LaCaptureBody(BaseModel):
    samples: str = "100k"
    samplerate: str = "6m"
    channels: str = LA_DEFAULT_CHANNELS
    time_ms: int | None = None
    trigger: str | None = None
    wait_trigger: bool = False
    measure: bool = True


class LaDecodeBody(BaseModel):
    protocol: str = "uart"
    samples: str = "100k"
    samplerate: str = "6m"
    channels: str = LA_DEFAULT_CHANNELS
    time_ms: int | None = None
    trigger: str | None = None
    wait_trigger: bool = False
    channel_map: str | None = None


class LaLiveStartBody(BaseModel):
    samplerate: str = "6m"
    channels: str = LA_DEFAULT_CHANNELS
    samples: str = "600k"
    time_ms: int | None = None
    display_points: int = 8000
    trigger_mode: str = "off"
    trigger_channel: str = "D0"


class LaLiveConfigBody(BaseModel):
    samplerate: str | None = None
    channels: str | None = None
    samples: str | None = None
    time_ms: int | None = None
    display_points: int | None = None
    trigger_mode: str | None = None
    trigger_channel: str | None = None


class LaAutoSetBody(BaseModel):
    max_passes: int = 3


@app.post("/api/la/capture")
def api_la_capture(body: LaCaptureBody) -> dict[str, Any]:
    pr = usb_probe()
    if pr.mode != "la":
        raise HTTPException(
            400,
            f"Need LA mode (got '{pr.mode}'). Unplug → release H/P → replug.",
        )
    _ensure_not_live()
    cap = bridge.la_capture(
        samples=body.samples,
        samplerate=body.samplerate,
        channels=body.channels,
        time_ms=body.time_ms,
        trigger=body.trigger,
        wait_trigger=body.wait_trigger,
        measure=body.measure,
    )
    if not cap.ok or not cap.path:
        raise HTTPException(500, cap.cmd.stderr or "LA capture failed")
    summary = meas.measure_la_csv(Path(cap.path))
    return {"ok": True, "capture": cap.to_dict(), "summary": summary}


@app.post("/api/la/measure")
def api_la_measure(body: LaCaptureBody) -> dict[str, Any]:
    pr = usb_probe()
    if pr.mode != "la":
        raise HTTPException(400, f"Need LA mode (got '{pr.mode}')")
    _ensure_not_live()
    return bridge.la_measure(
        samples=body.samples,
        samplerate=body.samplerate,
        channels=body.channels,
        time_ms=body.time_ms,
        trigger=body.trigger,
        wait_trigger=body.wait_trigger,
    )


@app.post("/api/la/decode")
def api_la_decode(body: LaDecodeBody) -> dict[str, Any]:
    pr = usb_probe()
    if pr.mode != "la":
        raise HTTPException(
            400,
            f"Need LA mode (got '{pr.mode}'). Unplug → release H/P → replug.",
        )
    _ensure_not_live()
    return bridge.la_decode(
        protocol=body.protocol,
        samples=body.samples,
        samplerate=body.samplerate,
        channels=body.channels,
        time_ms=body.time_ms,
        trigger=body.trigger,
        wait_trigger=body.wait_trigger,
        channel_map=body.channel_map,
    )


@app.get("/api/la/show")
def api_la_show() -> dict[str, Any]:
    return bridge.la_show()


@app.get("/api/la/live/status")
def api_la_live_status() -> dict[str, Any]:
    return LA.snapshot_status()


@app.post("/api/la/live/start")
def api_la_live_start(body: LaLiveStartBody) -> dict[str, Any]:
    if SCOPE.status.running:
        SCOPE.stop()
    return LA.start(**body.model_dump())


@app.post("/api/la/live/stop")
def api_la_live_stop() -> dict[str, Any]:
    return LA.stop()


@app.post("/api/la/live/config")
def api_la_live_config(body: LaLiveConfigBody) -> dict[str, Any]:
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    return LA.apply(**data)


@app.post("/api/la/live/autoset")
def api_la_live_autoset(body: LaAutoSetBody) -> dict[str, Any]:
    if SCOPE.status.running:
        SCOPE.stop()
    return LA.autoset(max_passes=body.max_passes)


@app.post("/api/la/live/single")
def api_la_live_single() -> dict[str, Any]:
    """One-shot using current live LA config (pauses continuous briefly)."""
    r = LA.capture_once(use_trigger=False)
    if not r.get("ok"):
        # Still 200 — UI/self-test read ok:false; avoid opaque 500s on USB races
        return r
    return r


@app.websocket("/ws/la")
async def ws_la(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)

    def on_frame(frame: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, frame)
        except Exception:
            pass

    LA.add_listener(on_frame)
    try:
        await ws.send_json({"type": "status", "data": LA.snapshot_status()})
        latest = LA.latest()
        if latest:
            await ws.send_json({"type": "frame", "data": latest})
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.5)
                await ws.send_json({"type": "frame", "data": frame})
            except asyncio.TimeoutError:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=0.01)
                    if msg == "ping":
                        await ws.send_json({"type": "pong"})
                    elif msg == "status":
                        await ws.send_json({"type": "status", "data": LA.snapshot_status()})
                except asyncio.TimeoutError:
                    pass
                except WebSocketDisconnect:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        LA.remove_listener(on_frame)


@app.get("/api/live/status")
def api_live_status() -> dict[str, Any]:
    return SCOPE.snapshot_status()


@app.post("/api/live/start")
def api_live_start(body: LiveStartBody) -> dict[str, Any]:
    return SCOPE.start(**body.model_dump())


@app.post("/api/live/stop")
def api_live_stop() -> dict[str, Any]:
    return SCOPE.stop()


@app.post("/api/live/config")
def api_live_config(body: LiveConfigBody) -> dict[str, Any]:
    """Update live params. Rate/range changes require restart for hardware apply."""
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    return SCOPE.apply(**data)


class AutoSetBody(BaseModel):
    probe_ch1: float | None = None
    probe_ch2: float | None = None
    channels: str = "CH1,CH2"  # CH1 | CH2 | CH1,CH2 | both
    max_passes: int = 3


@app.post("/api/live/autoset")
def api_live_autoset(body: AutoSetBody) -> dict[str, Any]:
    """
    Analyze probe waveforms and iterate sensitivity / sample rate / trigger
    until the live view is reasonably filled without clipping.
    """
    return SCOPE.autoset(
        probe_ch1=body.probe_ch1,
        probe_ch2=body.probe_ch2,
        channels=body.channels,
        max_passes=body.max_passes,
    )


class DualCalBody(BaseModel):
    pf_ch1: float = 1.0
    pf_ch2: float = 10.0
    samples: str = "20k"
    samplerate: str = "1m"


@app.post("/api/dual-cal-check")
def api_dual_cal_check(body: DualCalBody) -> dict[str, Any]:
    """
    Both probes on the same cal terminal via live USB stream.
    (sigrok CH2 / dual-CSV is unreliable on this Windows build — live path is source of truth.)
    """
    import time

    import numpy as np

    from . import measure as meas_mod

    was_running = SCOPE.status.running
    SCOPE.stop()
    st = SCOPE.start(
        rate_index=1,
        num_channels=2,
        ch1_range=1,
        ch2_range=5,  # finer vertical for ×10 BNC (~0.2 V)
        probe_ch1=body.pf_ch1,
        probe_ch2=body.pf_ch2,
        display_points=8000,
        block_samples=8192,
        trigger_mode="auto",
    )
    if st.get("error") or not SCOPE.status.running:
        return {"ok": False, "error": st.get("error") or "live start failed", "live": st}

    time.sleep(1.2)
    v1s: list[float] = []
    v2s: list[float] = []
    f1s: list[float] = []
    f2s: list[float] = []
    for _ in range(20):
        lat = SCOPE.latest()
        time.sleep(0.05)
        if not lat or not lat.get("ch1") or not lat.get("ch2"):
            continue
        a = np.asarray(lat["ch1"], dtype=np.float64)
        b = np.asarray(lat["ch2"], dtype=np.float64)
        v1s.append(float(a.max() - a.min()))
        v2s.append(float(b.max() - b.min()))
        # frequency from live block
        rate = float(lat.get("rate_hz") or 1e6)
        m1 = meas_mod.measure_channel("CH1", a, rate, probe_factor=1.0)
        m2 = meas_mod.measure_channel("CH2", b, rate, probe_factor=1.0)
        if m1.frequency_hz:
            f1s.append(m1.frequency_hz)
        if m2.frequency_hz:
            f2s.append(m2.frequency_hz)

    if not was_running:
        SCOPE.stop()

    if not v1s or not v2s:
        return {"ok": False, "error": "no live frames", "live": SCOPE.snapshot_status()}

    v1 = float(np.median(v1s))
    v2 = float(np.median(v2s))
    f1 = float(np.median(f1s)) if f1s else None
    f2 = float(np.median(f2s)) if f2s else None
    ratio = (v2 / v1) if v1 > 1e-9 else None
    freq_ok = f1 is not None and f2 is not None and 900 <= f1 <= 1100 and 900 <= f2 <= 1100
    vpp_ok = ratio is not None and 0.85 <= ratio <= 1.15
    return {
        "ok": bool(freq_ok and vpp_ok),
        "source": "live_usb",
        "freq_ok": freq_ok,
        "vpp_ok": vpp_ok,
        "vpp_ratio_ch2_over_ch1": ratio,
        "CH1": {"probe_factor": body.pf_ch1, "vpp": v1, "frequency_hz": f1},
        "CH2": {"probe_factor": body.pf_ch2, "vpp": v2, "frequency_hz": f2},
        "expect": "Both tips+GND on cal; physical × match UI; Vpp ratio ≈ 1.0; f ≈ 1 kHz",
        "hint": (
            None
            if vpp_ok and freq_ok
            else "Check probe switches (×1/×10), tip+GND clips on cal, and UI × factors."
        ),
        "live": SCOPE.snapshot_status(),
    }


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)

    def on_frame(frame: dict[str, Any]) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, frame)
        except Exception:
            pass

    SCOPE.add_listener(on_frame)
    try:
        await ws.send_json({"type": "status", "data": SCOPE.snapshot_status()})
        latest = SCOPE.latest()
        if latest:
            await ws.send_json({"type": "frame", "data": latest})
        while True:
            # Also allow client pings / config via text
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=0.5)
                await ws.send_json({"type": "frame", "data": frame})
            except asyncio.TimeoutError:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=0.01)
                    if msg == "ping":
                        await ws.send_json({"type": "pong"})
                    elif msg == "status":
                        await ws.send_json({"type": "status", "data": SCOPE.snapshot_status()})
                except asyncio.TimeoutError:
                    pass
                except WebSocketDisconnect:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        SCOPE.remove_listener(on_frame)


@app.get("/api/buses")
def api_buses() -> list[dict[str, Any]]:
    return bus_catalog.catalog()


@app.post("/api/capture")
def api_capture(body: CaptureBody) -> dict[str, Any]:
    pr = usb_probe()
    if pr.mode != "scope":
        raise HTTPException(400, f"Need scope mode (H/P pressed); currently '{pr.mode}'")
    _ensure_not_live()
    # Capture both channels; apply per-channel factors in post
    cap = bridge.scope_capture(
        channels=body.channels,
        samplerate=body.samplerate,
        samples=body.samples,
        probe_factor=1.0,
        measure=False,
        retries=3,
    )
    if not cap.ok or not cap.path:
        raise HTTPException(500, cap.cmd.stderr or "capture failed")
    payload = _waveform_payload(
        Path(cap.path),
        pf1=body.probe_factor_ch1,
        pf2=body.probe_factor_ch2,
    )
    payload["capture"] = {"ok": True, "extras": cap.extras}
    return payload


@app.post("/api/calfreq")
def api_calfreq(body: CalFreqBody) -> dict[str, Any]:
    """
    Set calibration output frequency while live is allowed.

    Firmware ignores 0xE6 during bulk capture — we pause sampling, send
    SETCALFREQ, then resume. Auto-flashes Ho-Ro FW if still on sigrok VID.
    """
    import time

    # LA continuous must not hold the USB device during FW flash / EP0
    if LA.status.running:
        LA.stop()
        time.sleep(0.3)

    r = calfreq.set_cal_freq(body.freq_hz, ensure_fw=True)
    if not r.get("ok"):
        r["live"] = SCOPE.snapshot_status()
        return r
    verified = None
    target = float(body.freq_hz)
    if SCOPE.status.running:
        # Sub-kHz needs a longer time window or the period estimator fails
        prev = {
            "rate_index": SCOPE.cfg.rate_index,
            "display_points": SCOPE.cfg.display_points,
        }
        if target < 1000:
            SCOPE.apply(rate_index=110, display_points=8000)  # 100 kS/s · 80 ms
            time.sleep(1.0)
        else:
            # ~4–8 periods of 1 kHz on screen
            SCOPE.apply(rate_index=150, display_points=8000)  # 500 kS/s
            time.sleep(0.9)
        SCOPE._meas_ema.clear()
        SCOPE._trig_lvl_ema = None
        # Median of raw (non-EMA) measurements — live EMA can lag after FW/rate change
        freqs: list[float] = []
        vpps: list[float] = []
        for _ in range(10):
            frame = SCOPE._wait_frame(timeout_s=1.0, min_frames=2)
            if not frame or not frame.get("ch1"):
                continue
            try:
                y = np.asarray(frame["ch1"], dtype=np.float64)
                rate = float(frame.get("rate_hz") or SCOPE.status.rate_hz or 1e6)
                cm = meas.measure_channel("CH1", y, rate, probe_factor=1.0)
                if cm.frequency_hz:
                    freqs.append(float(cm.frequency_hz))
                if cm.vpp is not None:
                    vpps.append(float(cm.vpp))
            except Exception:  # noqa: BLE001
                continue
        if freqs:
            measured = float(np.median(freqs))
            tol = 0.15
            ok_f = target * (1.0 - tol) <= measured <= target * (1.0 + tol)
            verified = {
                "measured_hz": measured,
                "target_hz": target,
                "match": bool(ok_f),
                "vpp": float(np.median(vpps)) if vpps else None,
                "n": len(freqs),
            }
            r["verified"] = verified
            if not ok_f:
                r["ok"] = False
                r["error"] = (
                    f"SETCALFREQ sent but measured {measured:.1f} Hz "
                    f"(want ~{target} Hz). Tip on cal pin?"
                )
        # Restore prior rate if we changed it for verify
        if (
            prev["rate_index"] != SCOPE.cfg.rate_index
            or prev["display_points"] != SCOPE.cfg.display_points
        ):
            SCOPE.apply(**prev)
    r["live"] = SCOPE.snapshot_status()
    return r


@app.get("/api/probes")
def api_probes() -> list[dict[str, Any]]:
    return probe_db.list_probes(probe_db.connect())


@app.post("/api/probes")
def api_probes_upsert(body: ProbeBody) -> dict[str, Any]:
    return probe_db.upsert_probe(
        probe_db.connect(),
        name=body.name,
        model=body.model,
        attenuation=body.attenuation,
        channel_default=body.channel_default,
        notes=body.notes,
    )


@app.delete("/api/probes/{probe_id}")
def api_probes_delete(probe_id: int) -> dict[str, Any]:
    probe_db.delete_probe(probe_db.connect(), probe_id)
    return {"ok": True}


@app.get("/api/probes/{probe_id}/latest")
def api_probe_latest(probe_id: int) -> dict[str, Any]:
    row = probe_db.latest_run(probe_db.connect(), probe_id)
    if not row:
        raise HTTPException(404, "no cal runs")
    return row


@app.post("/api/probe-cal")
def api_probe_cal(body: ProbeCalBody) -> dict[str, Any]:
    """Run multi-frequency probe calibration (tip on cal terminal)."""
    _ensure_not_live()
    return probe_cal.run_probe_calibration(
        probe_name=body.name,
        model=body.model,
        attenuation=body.attenuation,
        channel=body.channel,
        frequencies_hz=body.frequencies_hz,
        notes=body.notes,
    )


@app.post("/api/decode")
def api_decode(body: DecodeBody) -> dict[str, Any]:
    pr = usb_probe()
    if pr.mode != "scope":
        raise HTTPException(
            400,
            "Oscilloscope decode needs scope mode. For digital LA decoders, release H/P.",
        )
    _ensure_not_live()
    channels = "CH1,CH2" if body.wiring == "differential" else body.channel
    cap = bridge.scope_capture(
        channels=channels,
        samplerate=body.samplerate,
        samples=body.samples,
        probe_factor=1.0,
        measure=False,
        retries=3,
    )
    if not cap.ok or not cap.path:
        raise HTTPException(500, cap.cmd.stderr or "capture failed")
    names, data, _, sr = meas.load_sigrok_csv(Path(cap.path))
    if sr is None:
        raise HTTPException(500, "missing samplerate in capture")
    # scale by probe factors before decode
    ch1 = data[:, 0] * body.probe_factor_ch1
    ch2 = data[:, 1] * body.probe_factor_ch2 if data.shape[1] > 1 else None
    dec = analog_decode.decode_bus(
        ch1,
        ch2,
        sr,
        bus=body.bus,
        wiring=body.wiring,
        channel=body.channel,
        baud=body.baud,
        threshold=body.threshold,
        idle_high=body.idle_high,
    )
    wave = _waveform_payload(
        Path(cap.path),
        pf1=body.probe_factor_ch1,
        pf2=body.probe_factor_ch2,
    )
    return {"decode": dec.to_dict(), "waveform": wave}


@app.get("/api/doctor")
def api_doctor() -> dict[str, Any]:
    return bridge.doctor()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


def main() -> None:
    import uvicorn

    host = os.environ.get("HTK_BENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("HTK_BENCH_PORT", "8787"))
    if STATIC.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    uvicorn.run(app, host=host, port=port, log_level="info")


# Mount static when imported under uvicorn -m
if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    main()
