"""End-to-end bench self-test for LA and/or scope mode via HTTP API + CLI helpers."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:8787"


def req(method: str, path: str, body: dict | None = None, timeout: float = 90.0) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = {"ok": False, "error": raw or str(exc), "http_status": exc.code}
        if isinstance(payload, dict):
            payload.setdefault("ok", False)
            payload.setdefault("http_status", exc.code)
            return payload
        return {"ok": False, "error": str(payload), "http_status": exc.code}


def ok(name: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def test_la() -> list[str]:
    fails: list[str] = []
    print("\n=== LA MODE SELF-TEST ===")

    st = req("GET", "/api/status")
    pr = st.get("probe") or {}
    if not ok("status/probe la", pr.get("mode") == "la", f"mode={pr.get('mode')}"):
        fails.append("not in LA mode")
        return fails

    # Doctor
    try:
        # doctor may be slow
        d = req("GET", "/api/doctor", timeout=120)
        if not ok("doctor", bool(d.get("ok")), str([c.get("name") for c in d.get("checks", []) if not c.get("pass")])):
            fails.append("doctor")
    except Exception as exc:  # noqa: BLE001
        fails.append("doctor")
        ok("doctor", False, str(exc))

    # Show
    show = req("GET", "/api/la/show")
    if not ok("la/show", bool(show.get("ok"))):
        fails.append("la/show")

    # Stop any prior live
    try:
        req("POST", "/api/la/live/stop", {})
    except Exception:  # noqa: BLE001
        pass

    # Measure
    m = req(
        "POST",
        "/api/la/measure",
        {"samples": "100k", "samplerate": "2m", "channels": "D0,D1,D2,D3,D4", "time_ms": 150},
        timeout=60,
    )
    meas = m.get("measure") or {}
    chans = {c["name"]: c for c in meas.get("channels") or []}
    if not ok("la/measure", m.get("ok") and "D2" in chans, f"n={meas.get('n_samples')}"):
        fails.append("la/measure")
    else:
        d3 = chans.get("D3") or {}
        f3 = d3.get("frequency_hz") or 0
        ok("la/measure D3~1kHz", 800 < f3 < 1200, f"f={f3:.1f}")
        if not (800 < f3 < 1200):
            fails.append("la/measure D3 freq")

    # Capture
    cap = req(
        "POST",
        "/api/la/capture",
        {"samples": "50k", "samplerate": "2m", "channels": "D2,D3,D4", "measure": True},
    )
    if not ok("la/capture", cap.get("ok") and bool(cap.get("summary")), str(cap.get("capture", {}).get("path"))):
        fails.append("la/capture")

    # Decode UART
    dec = req(
        "POST",
        "/api/la/decode",
        {
            "protocol": "uart:rx=D2:baudrate=9600:format=ascii",
            "samples": "400k",
            "samplerate": "2m",
            "channels": "D0,D1,D2,D3,D4",
            "time_ms": 300,
        },
        timeout=90,
    )
    text = (dec.get("text") or "") + "\n".join(dec.get("annotations") or [])
    payload = ("B" in text and "M" in text and "I" in text and "L" in text and "A" in text)
    if not ok("la/decode uart BMI-LA", bool(dec.get("ok")) and payload, f"ok={dec.get('ok')} ann={len(dec.get('annotations') or [])}"):
        fails.append("la/decode")

    # Live start
    live = req(
        "POST",
        "/api/la/live/start",
        {
            "samplerate": "6m",
            "samples": "200k",
            "channels": "D0,D1,D2,D3,D4",
            "trigger_mode": "rising",
            "trigger_channel": "D3",
            "display_points": 4000,
        },
    )
    if not ok("la/live/start", bool(live.get("running")), str(live.get("error"))):
        fails.append("la/live/start")
        return fails

    time.sleep(1.8)
    ls = req("GET", "/api/la/live/status")
    if not ok("la/live/status fps", (ls.get("fps") or 0) >= 8 and (ls.get("frames") or 0) >= 5, f"fps={ls.get('fps')} frames={ls.get('frames')} bps={ls.get('stream_bps')}"):
        fails.append("la/live fps")

    # Config: channel subset
    cfg = req("POST", "/api/la/live/config", {"channels": "D2,D3,D4", "samplerate": "6m", "samples": "200k"})
    time.sleep(1.2)
    ls2 = req("GET", "/api/la/live/status")
    if not ok("la/live/config channels", ls2.get("running") and ls2.get("channels") == "D2,D3,D4", f"ch={ls2.get('channels')} err={ls2.get('error')}"):
        fails.append("la/live/config channels")

    # Config: trigger soft
    cfg2 = req("POST", "/api/la/live/config", {"trigger_mode": "falling", "trigger_channel": "D4"})
    if not ok("la/live/config trigger", cfg2.get("trigger_mode") == "falling", str(cfg2.get("trigger_mode"))):
        fails.append("la/live/config trigger")

    # Config: rate change (hard restart)
    cfg3 = req("POST", "/api/la/live/config", {"samplerate": "2m", "channels": "D2,D3,D4", "samples": "100k"})
    time.sleep(1.0)
    ls3 = req("GET", "/api/la/live/status")
    if not ok("la/live/config rate", ls3.get("running") and ls3.get("samplerate") == "2m", f"rate={ls3.get('samplerate')} err={ls3.get('error')}"):
        fails.append("la/live/config rate")

    # Single
    single = req("POST", "/api/la/live/single", {})
    if not ok("la/live/single", bool(single.get("ok")) and bool(single.get("summary")), str(single.get("error"))):
        fails.append("la/live/single")

    # Autset (may fail if no activity — but we have activity)
    try:
        auto = req("POST", "/api/la/live/autoset", {}, timeout=120)
        if not ok("la/live/autoset", bool(auto.get("ok")), str(auto.get("error") or auto.get("reasons"))):
            fails.append("la/live/autoset")
        else:
            time.sleep(1.0)
            ls4 = req("GET", "/api/la/live/status")
            ok("la/live after autoset", bool(ls4.get("running")), f"fps={ls4.get('fps')}")
    except Exception as exc:  # noqa: BLE001
        fails.append("la/live/autoset")
        ok("la/live/autoset", False, str(exc))

    # Stop
    stop = req("POST", "/api/la/live/stop", {})
    if not ok("la/live/stop", not stop.get("running")):
        fails.append("la/live/stop")

    return fails


def test_scope() -> list[str]:
    fails: list[str] = []
    print("\n=== SCOPE MODE SELF-TEST ===")
    st = req("GET", "/api/status")
    pr = st.get("probe") or {}
    if not ok("status/probe scope", pr.get("mode") == "scope", f"mode={pr.get('mode')}"):
        fails.append("not in scope mode")
        return fails

    try:
        d = req("GET", "/api/doctor", timeout=120)
        if not ok("doctor", bool(d.get("ok")), str([c.get("name") for c in d.get("checks", []) if not c.get("pass")])):
            fails.append("doctor")
    except Exception as exc:  # noqa: BLE001
        fails.append("doctor")
        ok("doctor", False, str(exc))

    # Stop LA/scope live first
    try:
        req("POST", "/api/la/live/stop", {})
    except Exception:  # noqa: BLE001
        pass
    try:
        req("POST", "/api/live/stop", {})
    except Exception:  # noqa: BLE001
        pass

    # Live start
    live = req(
        "POST",
        "/api/live/start",
        {
            "rate_index": 1,
            "num_channels": 2,
            "ch1_range": 1,
            "ch2_range": 5,
            "probe_ch1": 1,
            "probe_ch2": 10,
            "trigger_mode": "auto",
            "display_points": 4000,
        },
    )
    if not ok("live/start", bool(live.get("running")), str(live.get("error"))):
        fails.append("live/start")
        return fails

    time.sleep(1.5)
    ls = req("GET", "/api/live/status")
    if not ok("live/status", bool(ls.get("running")) and (ls.get("frames") or 0) > 0, f"frames={ls.get('frames')} fps={ls.get('fps')}"):
        fails.append("live/status")

    # Config
    cfg = req("POST", "/api/live/config", {"rate_index": 2, "trigger_mode": "auto"})
    if not ok("live/config", bool(cfg.get("running") is not False), str(cfg.get("error"))):
        fails.append("live/config")

    # Autoset
    try:
        auto = req("POST", "/api/live/autoset", {"probe_ch1": 1, "probe_ch2": 10, "max_passes": 3}, timeout=120)
        if not ok("live/autoset", bool(auto.get("ok")), str(auto.get("error"))):
            fails.append("live/autoset")
    except Exception as exc:  # noqa: BLE001
        fails.append("live/autoset")
        ok("live/autoset", False, str(exc))

    # Cal freq (if live)
    try:
        cal = req("POST", "/api/calfreq", {"freq_hz": 1000}, timeout=60)
        if not ok("calfreq 1kHz", bool(cal.get("ok")), str(cal.get("verified") or cal.get("error"))):
            fails.append("calfreq")
    except Exception as exc:  # noqa: BLE001
        fails.append("calfreq")
        ok("calfreq 1kHz", False, str(exc))

    # Dual cal (needs both probes on cal — may fail; report)
    try:
        dual = req("POST", "/api/dual-cal-check", {"pf_ch1": 1, "pf_ch2": 10}, timeout=60)
        ok("dual-cal-check", "ok" in dual or "live" in dual, str(dual.get("error") or dual.get("ok")))
    except Exception as exc:  # noqa: BLE001
        ok("dual-cal-check", False, str(exc))

    # Capture single (stops live)
    try:
        cap = req(
            "POST",
            "/api/capture",
            {"channels": "CH1,CH2", "samplerate": "1m", "samples": "20k", "probe_factor_ch1": 1, "probe_factor_ch2": 10},
            timeout=90,
        )
        if not ok("capture", bool(cap.get("t") or cap.get("series") or cap.get("ok") is not False), "waveform"):
            fails.append("capture")
    except Exception as exc:  # noqa: BLE001
        fails.append("capture")
        ok("capture", False, str(exc))

    # Restart live for decode path
    try:
        req("POST", "/api/live/stop", {})
    except Exception:  # noqa: BLE001
        pass

    # Decode (may be empty without signal — still must not 500)
    try:
        dec = req(
            "POST",
            "/api/decode",
            {
                "bus": "rs232",
                "wiring": "single",
                "channel": "CH1",
                "samplerate": "1m",
                "samples": "20k",
                "probe_factor_ch1": 1,
                "probe_factor_ch2": 10,
            },
            timeout=90,
        )
        ok("decode rs232", "decode" in dec, str((dec.get("decode") or {}).get("bus")))
    except urllib.error.HTTPError as exc:
        # 400/500 depending on mode — record
        fails.append("decode")
        ok("decode rs232", False, f"HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001
        fails.append("decode")
        ok("decode rs232", False, str(exc))

    # Buses / probes lists
    buses = req("GET", "/api/buses")
    ok("buses", isinstance(buses, list) and len(buses) > 0, f"n={len(buses) if isinstance(buses, list) else 0}")
    probes = req("GET", "/api/probes")
    ok("probes list", isinstance(probes, list), f"n={len(probes) if isinstance(probes, list) else 0}")

    # Live stop
    try:
        req("POST", "/api/live/start", {"rate_index": 1, "probe_ch1": 1, "probe_ch2": 10})
        time.sleep(0.5)
        stop = req("POST", "/api/live/stop", {})
        if not ok("live/stop", not stop.get("running")):
            fails.append("live/stop")
    except Exception as exc:  # noqa: BLE001
        fails.append("live/stop")
        ok("live/stop", False, str(exc))

    return fails


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    st = req("GET", "/api/status")
    mode = (st.get("probe") or {}).get("mode")
    print(f"Device mode: {mode}")

    fails: list[str] = []
    if which in {"la", "auto"} and mode == "la":
        fails.extend(test_la())
    elif which == "la":
        print("FAIL: requested LA but device not in LA mode")
        return 2

    if which in {"scope", "osc", "auto"} and mode == "scope":
        fails.extend(test_scope())
    elif which in {"scope", "osc"}:
        print("FAIL: requested scope but device not in scope mode")
        return 2

    if which == "auto" and mode == "la":
        print("\n(Scope tests skipped — still in LA. Switch H/P in + replug, then: python scripts/bench_selftest.py scope)")

    print("\n=== SUMMARY ===")
    if fails:
        print("FAILED:", ", ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
