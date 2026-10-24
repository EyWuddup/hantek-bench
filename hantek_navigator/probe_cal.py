"""Multi-frequency probe calibration against the Hantek cal terminal."""

from __future__ import annotations

import time
from typing import Any

from . import calfreq
from . import measure as meas
from . import probe_db
from . import sigrok_bridge as bridge

# Nominal cal amplitude at tip with ×1 (as-found ~1.25 Vpp on Hersom 6022BL)
NOMINAL_VPP_X1 = 1.25


def run_probe_calibration(
    *,
    probe_name: str,
    model: str = "",
    attenuation: float = 1.0,
    channel: str = "CH2",
    frequencies_hz: list[int] | None = None,
    samples: str = "20k",
    notes: str = "",
) -> dict[str, Any]:
    """
    For each cal frequency: set cal out → capture channel → measure → store point.
    Expects probe tip on cal terminal, GND on GND tab, physical atten matching `attenuation`.
    """
    freqs = frequencies_hz or [
        100,
        200,
        500,
        1000,
        2000,
        5000,
        10_000,
        20_000,
    ]
    conn = probe_db.connect()
    probe = probe_db.upsert_probe(
        conn,
        name=probe_name,
        model=model,
        attenuation=attenuation,
        channel_default=channel,
        notes=notes,
    )
    run_id = probe_db.start_run(conn, int(probe["id"]))
    points_out: list[dict[str, Any]] = []
    expected_vpp = NOMINAL_VPP_X1 / float(attenuation)
    # Probe whether firmware actually changes cal frequency (USB ACK is not enough)
    probe_set = calfreq.set_cal_freq(2000.0)
    time.sleep(0.8)
    probe_cap = bridge.scope_capture(
        samples="10k",
        samplerate="1m",
        channels=channel,
        measure=True,
        retries=2,
    )
    measured_probe = None
    if probe_cap.measure and probe_cap.measure.get("channels"):
        measured_probe = probe_cap.measure["channels"][0].get("frequency_hz")
    calfreq_ok = bool(
        probe_set.get("ok")
        and measured_probe is not None
        and 1800 <= float(measured_probe) <= 2200
    )
    calfreq.set_cal_freq(1000.0)
    time.sleep(0.5)
    if not calfreq_ok:
        freqs = [1000]  # fixed ~1 kHz square on this firmware/build
        notes = (notes + " | calfreq_fixed_1khz").strip(" |")

    for f in freqs:
        set_r = calfreq.set_cal_freq(float(f)) if calfreq_ok or f == 1000 else {
            "ok": False,
            "skipped": True,
            "freq_hz": f,
        }
        # Release USB fully before sigrok re-opens the device
        time.sleep(0.8)
        # Samplerate: ≥20× cal frequency, capped to device rates we use
        if f <= 1000:
            sr = "1m"
        elif f <= 10000:
            sr = "8m"
        else:
            sr = "16m"
        cap = bridge.scope_capture(
            samples=samples,
            samplerate=sr,
            channels=channel,
            probe_factor=1.0,  # measure raw tip volts; profile stores attenuation
            measure=True,
            retries=3,
        )
        # One more try if capture looked empty (USB reclaim race)
        if not cap.ok or not (cap.measure and cap.measure.get("channels")):
            time.sleep(0.6)
            cap = bridge.scope_capture(
                samples=samples,
                samplerate=sr,
                channels=channel,
                probe_factor=1.0,
                measure=True,
                retries=2,
            )
        chm = None
        if cap.measure and cap.measure.get("channels"):
            chm = cap.measure["channels"][0]
        measured_f = chm.get("frequency_hz") if chm else None
        vpp = chm.get("vpp") if chm else None
        freq_err = None
        gain_err = None
        passed = False
        if measured_f and f > 0:
            freq_err = 100.0 * (measured_f - f) / f
        if vpp is not None and expected_vpp > 0:
            gain_err = 100.0 * (vpp - expected_vpp) / expected_vpp
        if (
            measured_f is not None
            and abs(freq_err or 99) <= 15.0
            and vpp is not None
            and vpp >= 0.15 / float(attenuation)
        ):
            passed = True
        raw = {
            "set_cal": set_r,
            "capture_ok": cap.ok,
            "path": cap.path,
            "measure": chm,
            "samplerate": sr,
        }
        probe_db.add_point(
            conn,
            run_id,
            cal_freq_hz=float(f),
            measured_freq_hz=measured_f,
            vpp=vpp,
            vmin=chm.get("vmin") if chm else None,
            vmax=chm.get("vmax") if chm else None,
            duty_pct=chm.get("duty_high_pct") if chm else None,
            gain_error_pct=gain_err,
            freq_error_pct=freq_err,
            passed=passed,
            raw=raw,
        )
        points_out.append(
            {
                "cal_freq_hz": f,
                "measured_freq_hz": measured_f,
                "vpp": vpp,
                "freq_error_pct": freq_err,
                "gain_error_pct": gain_err,
                "pass": passed,
                "set_cal_ok": set_r.get("ok"),
            }
        )

    # restore 1 kHz
    calfreq.set_cal_freq(1000.0)
    n_pass = sum(1 for p in points_out if p["pass"])
    summary = {
        "probe": probe,
        "points": points_out,
        "passed": n_pass,
        "total": len(points_out),
        "ok": n_pass >= max(1, len(points_out) // 2),
        "expected_vpp_at_tip": expected_vpp,
        "nominal_vpp_x1": NOMINAL_VPP_X1,
        "channel": channel,
        "attenuation": attenuation,
        "calfreq_control": calfreq_ok,
        "calfreq_probe": {
            "set": probe_set,
            "measured_after_2khz": measured_probe,
        },
        "note": (
            None
            if calfreq_ok
            else "Cal output stays ~1 kHz on current firmware (SETCALFREQ ACK but no rate change). "
            "Profile saved at 1 kHz. For multi-frequency sweeps use OpenHantek Calibration out, then re-run."
        ),
    }
    probe_db.finish_run(conn, run_id, ok=bool(summary["ok"]), summary=summary)
    summary["run_id"] = run_id
    return summary
