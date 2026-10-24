"""Wrap sigrok-cli for Hantek 6022BL scope and LA."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import measure as meas
from .usb_probe import find_sigrok_cli, probe as usb_probe


def default_artifact_dir(repo: Path | None = None) -> Path:
    from .paths import default_artifact_dir as _default

    if repo is not None:
        return Path(repo) / "assets" / "bench" / "hantek-6022bl"
    if os.environ.get("HTK_REPO") and not os.environ.get("HTK_ARTIFACT_DIR"):
        return Path(os.environ["HTK_REPO"]) / "assets" / "bench" / "hantek-6022bl"
    return _default()


@dataclass
class CmdResult:
    ok: bool
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureResult:
    ok: bool
    mode: str
    path: str | None
    cmd: CmdResult
    measure: dict[str, Any] | None = None
    cal: dict[str, Any] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "path": self.path,
            "cmd": self.cmd.to_dict(),
            "measure": self.measure,
            "cal": self.cal,
            "extras": self.extras,
        }


def _sigrok(explicit: str | None = None) -> Path:
    p = find_sigrok_cli(explicit)
    if not p:
        raise FileNotFoundError(
            "sigrok-cli not found. Set HTK_SIGROK_CLI or install under "
            "%LOCALAPPDATA%\\hantek-bench\\sigrok-cli\\"
        )
    return p


def run_sigrok(
    args: list[str],
    *,
    sigrok_path: str | None = None,
    timeout_s: float = 120.0,
) -> CmdResult:
    sk = _sigrok(sigrok_path)
    argv = [str(sk), *args]
    t0 = time.perf_counter()
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout_s,
    )
    return CmdResult(
        ok=completed.returncode == 0,
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        duration_s=time.perf_counter() - t0,
    )


def scope_show(sigrok_path: str | None = None) -> dict[str, Any]:
    cmd = run_sigrok(["--driver", "hantek-6xxx", "--show"], sigrok_path=sigrok_path)
    return {"ok": cmd.ok, "cmd": cmd.to_dict(), "text": cmd.stdout or cmd.stderr}


def _stamp() -> str:
    # UTC with milliseconds so rapid captures never collide
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def scope_capture(
    *,
    samples: str = "20k",
    samplerate: str = "1m",
    channels: str = "CH1",
    vdiv: str = "1V",
    time_ms: int | None = None,
    out: Path | None = None,
    probe_factor: float = 1.0,
    measure: bool = False,
    sigrok_path: str | None = None,
    artifact_dir: Path | None = None,
    retries: int = 2,
) -> CaptureResult:
    art = artifact_dir or default_artifact_dir()
    art.mkdir(parents=True, exist_ok=True)
    out_path = out or (art / f"scope_{_stamp()}_{channels.replace(',', '-')}.csv")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Note: per-channel vdiv must use --channel-group + --set. That path is flaky on
    # Windows nightly sigrok (ACCESS_VIOLATION after --set), so capture uses the
    # device's current vdiv. Prefer OpenHantek GUI to change vertical scale, or
    # pass set_vdiv=True to attempt --set (best-effort).
    set_cmds: list[dict[str, Any]] = []
    if os.environ.get("HTK_SET_VDIV", "").strip() in {"1", "true", "yes"}:
        for ch in [c.strip() for c in channels.split(",") if c.strip()]:
            set_cmd = run_sigrok(
                [
                    "--driver",
                    "hantek-6xxx",
                    "--channel-group",
                    ch,
                    "--config",
                    f"vdiv={vdiv}",
                    "--set",
                ],
                sigrok_path=sigrok_path,
                timeout_s=30.0,
            )
            set_cmds.append(set_cmd.to_dict())
            time.sleep(0.3)

    args: list[str] = [
        "--driver",
        "hantek-6xxx",
        "--config",
        f"samplerate={samplerate}",
        "--channels",
        channels,
        "--output-file",
        str(out_path),
        "--output-format",
        "csv",
    ]
    if time_ms is not None:
        args.extend(["--time", str(int(time_ms))])
    else:
        args.extend(["--samples", str(samples)])

    cmd: CmdResult | None = None
    attempts: list[dict[str, Any]] = []
    for attempt in range(max(1, retries) + 1):
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        t_before = time.time()
        cmd = run_sigrok(args, sigrok_path=sigrok_path, timeout_s=180.0)
        attempts.append(cmd.to_dict())
        fresh = (
            out_path.is_file()
            and out_path.stat().st_size > 64
            and out_path.stat().st_mtime >= (t_before - 1.0)
        )
        if cmd.ok and fresh:
            break
        # Windows nightly sigrok sometimes ACCESS_VIOLATIONs (0xC0000005) mid-write
        time.sleep(0.4 * (attempt + 1))

    assert cmd is not None
    fresh_ok = (
        out_path.is_file()
        and out_path.stat().st_size > 64
    )
    result = CaptureResult(
        ok=bool(cmd.ok and fresh_ok),
        mode="scope",
        path=str(out_path) if fresh_ok else None,
        cmd=cmd,
        extras={
            "samplerate": samplerate,
            "samples": samples,
            "channels": channels,
            "vdiv_requested": vdiv,
            "vdiv_note": "Device keeps last vdiv; set HTK_SET_VDIV=1 to attempt --set (unstable on some Win builds)",
            "probe_factor": probe_factor,
            "time_ms": time_ms,
            "vdiv_set": set_cmds,
            "attempts": attempts,
        },
    )
    if result.ok and measure:
        m = meas.measure_csv(out_path, probe_factor=probe_factor)
        result.measure = m.to_dict()
    return result


def scope_measure(**kwargs: Any) -> CaptureResult:
    kwargs["measure"] = True
    return scope_capture(**kwargs)


def scope_cal_test(
    *,
    repeat: int = 10,
    probe_factor: float = 1.0,
    samplerate: str = "1m",
    samples: str = "20k",
    vdiv: str = "1V",
    channel: str = "CH1",
    sigrok_path: str | None = None,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    art = artifact_dir or default_artifact_dir()
    runs: list[dict[str, Any]] = []
    for i in range(max(1, repeat)):
        cap = scope_capture(
            samples=samples,
            samplerate=samplerate,
            channels=channel,
            vdiv=vdiv,
            probe_factor=probe_factor,
            measure=True,
            sigrok_path=sigrok_path,
            artifact_dir=art,
            out=art / f"cal_{_stamp()}_r{i+1:02d}.csv",
        )
        cal = None
        if cap.measure and cap.measure.get("channels"):
            ch0 = meas.ChannelMeas(**{k: v for k, v in cap.measure["channels"][0].items()})
            cal = meas.cal_pass_fail(ch0)
            cap.cal = cal
        runs.append(cap.to_dict())
        time.sleep(0.15)

    passed = sum(1 for r in runs if (r.get("cal") or {}).get("pass"))
    freqs = [
        r["measure"]["channels"][0]["frequency_hz"]
        for r in runs
        if r.get("measure") and r["measure"].get("channels")
        and r["measure"]["channels"][0].get("frequency_hz") is not None
    ]
    vpps = [
        r["measure"]["channels"][0]["vpp"]
        for r in runs
        if r.get("measure") and r["measure"].get("channels")
    ]
    summary = {
        "ok": passed == len(runs) and len(runs) > 0,
        "repeat": len(runs),
        "passed": passed,
        "failed": len(runs) - passed,
        "frequency_hz": {
            "mean": float(sum(freqs) / len(freqs)) if freqs else None,
            "min": float(min(freqs)) if freqs else None,
            "max": float(max(freqs)) if freqs else None,
        },
        "vpp": {
            "mean": float(sum(vpps) / len(vpps)) if vpps else None,
            "min": float(min(vpps)) if vpps else None,
            "max": float(max(vpps)) if vpps else None,
            "note": "as-found; document, do not invent",
        },
        "expect": {
            "frequency_hz": "900..1100",
            "duty_high_pct": "40..60",
            "vpp": ">=0.3 V at probe tip with matching --probe-factor",
        },
        "runs": runs,
    }
    return summary


def normalize_la_channels(channels: str) -> str:
    """
    Windows nightly sigrok rejects '0-7' / 'D0-D7' ranges on fx2lafw.
    Expand common shorthands to comma lists: D0,D1,...,D7.
    """
    raw = (channels or "").strip()
    if not raw:
        return ",".join(f"D{i}" for i in range(8))
    m = re.fullmatch(r"(?:D)?(\d+)\s*-\s*(?:D)?(\d+)", raw, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return ",".join(f"D{i}" for i in range(a, b + 1))
    parts: list[str] = []
    for tok in re.split(r"[,\s]+", raw):
        if not tok:
            continue
        m2 = re.fullmatch(r"(?:D)?(\d+)", tok, re.I)
        if m2:
            parts.append(f"D{int(m2.group(1))}")
        else:
            parts.append(tok)
    return ",".join(parts)


def la_show(sigrok_path: str | None = None) -> dict[str, Any]:
    pr = usb_probe(sigrok_path)
    cmd = run_sigrok(["--driver", "fx2lafw", "--show"], sigrok_path=sigrok_path)
    return {
        "ok": cmd.ok and pr.mode == "la",
        "mode": pr.mode,
        "cmd": cmd.to_dict(),
        "text": cmd.stdout or cmd.stderr,
        "probe": pr.to_dict(),
    }


def la_capture(
    *,
    samples: str = "100k",
    samplerate: str = "6m",
    channels: str = "D0,D1,D2,D3,D4,D5,D6,D7",
    time_ms: int | None = None,
    trigger: str | None = None,
    wait_trigger: bool = False,
    out: Path | None = None,
    output_format: str = "csv",
    sigrok_path: str | None = None,
    artifact_dir: Path | None = None,
    timeout_s: float = 180.0,
    keep_file: bool = True,
    measure: bool = False,
) -> CaptureResult:
    pr = usb_probe(sigrok_path)
    if pr.mode != "la":
        cmd = CmdResult(
            ok=False,
            argv=[],
            returncode=2,
            stdout="",
            stderr=(
                f"Device mode is '{pr.mode}', need 'la'. "
                "Unplug USB, release H/P button, replug, then retry."
            ),
            duration_s=0.0,
        )
        return CaptureResult(ok=False, mode="la", path=None, cmd=cmd, extras={"probe": pr.to_dict()})

    chans = normalize_la_channels(channels)
    art = artifact_dir or default_artifact_dir()
    art.mkdir(parents=True, exist_ok=True)
    out_path = out or (art / f"la_{_stamp()}.csv")
    out_path = Path(out_path)
    args = [
        "--driver",
        "fx2lafw",
        "--config",
        f"samplerate={samplerate}",
        "--channels",
        chans,
    ]
    if time_ms is not None and int(time_ms) > 0:
        args.extend(["--time", str(int(time_ms))])
    else:
        args.extend(["--samples", str(samples)])
    if trigger:
        args.extend(["--triggers", str(trigger)])
        if wait_trigger:
            args.append("--wait-trigger")
    args.extend(
        [
            "--output-file",
            str(out_path),
            "--output-format",
            output_format,
        ]
    )
    try:
        cmd = run_sigrok(args, sigrok_path=sigrok_path, timeout_s=timeout_s)
    except subprocess.TimeoutExpired:
        cmd = CmdResult(
            ok=False,
            argv=[str(_sigrok(sigrok_path)), *args],
            returncode=124,
            stdout="",
            stderr=f"sigrok timeout after {timeout_s}s (trigger wait?)",
            duration_s=float(timeout_s),
        )
    ok = cmd.ok and out_path.is_file() and out_path.stat().st_size > 0
    measure_dict = None
    if ok and measure:
        try:
            measure_dict = meas.measure_la_csv(out_path)
        except Exception as exc:  # noqa: BLE001
            measure_dict = {"error": str(exc)}
    if ok and not keep_file and out is None:
        # caller used default artifact — keep it; only delete temp when asked
        pass
    return CaptureResult(
        ok=ok,
        mode="la",
        path=str(out_path) if ok else None,
        cmd=cmd,
        measure=measure_dict,
        extras={
            "samplerate": samplerate,
            "samples": samples,
            "time_ms": time_ms,
            "channels": chans,
            "trigger": trigger,
            "wait_trigger": wait_trigger,
        },
    )


def la_measure(
    *,
    samples: str = "100k",
    samplerate: str = "6m",
    channels: str = "D0,D1,D2,D3,D4,D5,D6,D7",
    time_ms: int | None = None,
    trigger: str | None = None,
    wait_trigger: bool = False,
    out: Path | None = None,
    sigrok_path: str | None = None,
) -> dict[str, Any]:
    cap = la_capture(
        samples=samples,
        samplerate=samplerate,
        channels=channels,
        time_ms=time_ms,
        trigger=trigger,
        wait_trigger=wait_trigger,
        out=out,
        sigrok_path=sigrok_path,
        measure=True,
    )
    summary = None
    if cap.ok and cap.path:
        summary = meas.measure_la_csv(Path(cap.path))
    return {
        "ok": cap.ok,
        "path": cap.path,
        "cmd": cap.cmd.to_dict(),
        "extras": cap.extras,
        "measure": summary or cap.measure,
    }


# Bus id → default sigrok -P stack + channel roles (AI / UI)
LA_DECODER_PRESETS: dict[str, dict[str, Any]] = {
    "uart": {
        "protocol": "uart:rx=D0:baudrate=115200",
        "hint": "Map rx= (and optional tx=) to wired pins. Set baudrate.",
    },
    "rs232": {
        "protocol": "uart:rx=D0:baudrate=9600",
        "hint": "RS-232 after level shift to 3.3/5 V logic on D0.",
    },
    "i2c": {
        "protocol": "i2c:scl=D0:sda=D1",
        "hint": "SCL→D0, SDA→D1 (open-drain + pull-ups on DUT).",
    },
    "spi": {
        "protocol": "spi:clk=D0:mosi=D1:miso=D2:cs=D3",
        "hint": "CLK/MOSI/MISO/CS on D0–D3.",
    },
    "can": {
        "protocol": "can:rx=D0",
        "hint": "CAN after transceiver RX → D0 (not raw CANH/L).",
    },
    "lin": {
        "protocol": "lin:uart=D0",
        "hint": "LIN after level adapt to logic on D0.",
    },
    "modbus": {
        "protocol": "uart:rx=D0:baudrate=9600",
        "hint": "Modbus RTU = UART framing; use uart decoder + inspect bytes.",
    },
}


def la_decode(
    *,
    protocol: str,
    samples: str = "100k",
    samplerate: str = "6m",
    channels: str = "D0,D1,D2,D3,D4,D5,D6,D7",
    time_ms: int | None = None,
    trigger: str | None = None,
    wait_trigger: bool = False,
    channel_map: str | None = None,
    annotations: bool = True,
    sigrok_path: str | None = None,
) -> dict[str, Any]:
    """
    Capture + sigrok protocol decoder.

    `protocol` may be a bare id (uart/i2c/…) — expanded via LA_DECODER_PRESETS —
    or a full -P string like `i2c:scl=D0:sda=D1`.
    Optional `channel_map` appends `:key=D#` fragments.
    """
    pr = usb_probe(sigrok_path)
    if pr.mode != "la":
        return {
            "ok": False,
            "error": f"Device mode is '{pr.mode}', need 'la' (release H/P, replug).",
            "probe": pr.to_dict(),
        }
    chans = normalize_la_channels(channels)
    proto = (protocol or "").strip()
    preset = LA_DECODER_PRESETS.get(proto.lower())
    if preset and ":" not in proto:
        proto = preset["protocol"]
    if channel_map:
        # channel_map like "rx=D0,tx=D1" or "scl=D0:sda=D1"
        frag = channel_map.replace(",", ":")
        if not frag.startswith(":"):
            proto = f"{proto}:{frag}" if ":" in proto else f"{proto}:{frag}"
        else:
            proto = f"{proto}{frag}"
    args = [
        "--driver",
        "fx2lafw",
        "--config",
        f"samplerate={samplerate}",
        "--channels",
        chans,
    ]
    if time_ms is not None and int(time_ms) > 0:
        args.extend(["--time", str(int(time_ms))])
    else:
        args.extend(["--samples", str(samples)])
    if trigger:
        args.extend(["--triggers", str(trigger)])
        if wait_trigger:
            args.append("--wait-trigger")
    args.extend(["-P", proto])
    # Do not pass -A …=all — many decoders reject the synthetic class name.
    # Default PD stdout already includes annotations.
    cmd = run_sigrok(args, sigrok_path=sigrok_path, timeout_s=180.0)
    lines = [ln for ln in (cmd.stdout or "").splitlines() if ln.strip()]
    return {
        "ok": cmd.ok,
        "protocol": proto,
        "channels": chans,
        "preset": preset,
        "annotations": lines[:500],
        "text": cmd.stdout,
        "cmd": cmd.to_dict(),
    }


def doctor(sigrok_path: str | None = None) -> dict[str, Any]:
    pr = usb_probe(sigrok_path)
    report: dict[str, Any] = {
        "probe": pr.to_dict(),
        "checks": [],
    }
    checks: list[dict[str, Any]] = report["checks"]

    checks.append(
        {
            "name": "usb_mode",
            "pass": pr.mode in {"scope", "la"},
            "detail": pr.mode,
        }
    )
    checks.append(
        {
            "name": "sigrok_cli",
            "pass": pr.sigrok_cli is not None,
            "detail": pr.sigrok_cli or "missing",
        }
    )
    winusb_ok = all(
        (not d.service) or d.service.upper() == "WINUSB" for d in pr.devices
    ) and bool(pr.devices)
    checks.append({"name": "winusb", "pass": winusb_ok, "detail": [d.service for d in pr.devices]})

    smoke: dict[str, Any] | None = None
    if pr.mode == "scope" and pr.sigrok_cli:
        show = scope_show(sigrok_path)
        checks.append({"name": "scope_show", "pass": show["ok"], "detail": "hantek-6xxx --show"})
        cap = scope_capture(
            samples="10k",
            samplerate="1m",
            channels="CH1",
            vdiv="1V",
            measure=True,
            sigrok_path=sigrok_path,
        )
        smoke = cap.to_dict()
        checks.append(
            {
                "name": "scope_smoke_capture",
                "pass": cap.ok,
                "detail": cap.path,
            }
        )
    elif pr.mode == "la" and pr.sigrok_cli:
        show = la_show(sigrok_path)
        checks.append({"name": "la_show", "pass": show["ok"], "detail": (show.get("text") or "")[:500]})
        cap = la_capture(
            samples="10k",
            samplerate="1m",
            channels="D0,D1,D2,D3,D4,D5,D6,D7",
            measure=True,
            sigrok_path=sigrok_path,
        )
        smoke = cap.to_dict()
        checks.append(
            {
                "name": "la_smoke_capture",
                "pass": cap.ok,
                "detail": cap.path,
            }
        )

    report["smoke"] = smoke
    report["ok"] = all(c["pass"] for c in checks)
    report["policy"] = "See `htk policy` / htk_policy MCP tool."
    return report
