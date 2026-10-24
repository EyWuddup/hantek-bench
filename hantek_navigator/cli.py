"""CLI: python -m hantek_navigator probe|doctor|scope|la|plot|policy …"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import measure as meas
from . import sigrok_bridge as bridge
from .policy import POLICY_TEXT
from .usb_probe import probe as usb_probe


def _dump(obj: object) -> int:
    print(json.dumps(obj, ensure_ascii=True, indent=2))
    return 0


def cmd_policy(_: argparse.Namespace) -> int:
    print(POLICY_TEXT)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    return _dump(usb_probe(args.sigrok).to_dict())


def cmd_doctor(args: argparse.Namespace) -> int:
    report = bridge.doctor(args.sigrok)
    _dump(report)
    return 0 if report.get("ok") else 2


def cmd_scope_show(args: argparse.Namespace) -> int:
    r = bridge.scope_show(args.sigrok)
    if args.json:
        return _dump(r)
    print(r.get("text") or "")
    return 0 if r.get("ok") else 2


def cmd_scope_capture(args: argparse.Namespace) -> int:
    r = bridge.scope_capture(
        samples=args.samples,
        samplerate=args.samplerate,
        channels=args.channels,
        vdiv=args.vdiv,
        time_ms=args.time_ms,
        out=Path(args.out) if args.out else None,
        probe_factor=args.probe_factor,
        measure=args.measure,
        sigrok_path=args.sigrok,
    )
    _dump(r.to_dict())
    return 0 if r.ok else 2


def cmd_scope_measure(args: argparse.Namespace) -> int:
    r = bridge.scope_measure(
        samples=args.samples,
        samplerate=args.samplerate,
        channels=args.channels,
        vdiv=args.vdiv,
        time_ms=args.time_ms,
        out=Path(args.out) if args.out else None,
        probe_factor=args.probe_factor,
        sigrok_path=args.sigrok,
    )
    _dump(r.to_dict())
    return 0 if r.ok else 2


def cmd_scope_cal_test(args: argparse.Namespace) -> int:
    summary = bridge.scope_cal_test(
        repeat=args.repeat,
        probe_factor=args.probe_factor,
        samplerate=args.samplerate,
        samples=args.samples,
        vdiv=args.vdiv,
        channel=args.channel,
        sigrok_path=args.sigrok,
    )
    _dump(summary)
    return 0 if summary.get("ok") else 2


def _la_trigger_args(args: argparse.Namespace) -> tuple[str | None, bool]:
    trig = getattr(args, "trigger", None)
    wait = bool(getattr(args, "wait_trigger", False))
    return trig, wait


def cmd_la_show(args: argparse.Namespace) -> int:
    r = bridge.la_show(args.sigrok)
    if getattr(args, "json", False):
        return _dump(r)
    print(r.get("text") or "")
    return 0 if r.get("ok") else 2


def cmd_la_capture(args: argparse.Namespace) -> int:
    trig, wait = _la_trigger_args(args)
    r = bridge.la_capture(
        samples=args.samples,
        samplerate=args.samplerate,
        channels=args.channels,
        time_ms=getattr(args, "time_ms", None),
        trigger=trig,
        wait_trigger=wait,
        out=Path(args.out) if args.out else None,
        output_format=args.format,
        measure=bool(getattr(args, "measure", False)),
        sigrok_path=args.sigrok,
    )
    d = r.to_dict()
    if r.ok and r.path and getattr(args, "measure", False):
        d["summary"] = meas.measure_la_csv(Path(r.path))
    _dump(d)
    return 0 if r.ok else 2


def cmd_la_measure(args: argparse.Namespace) -> int:
    trig, wait = _la_trigger_args(args)
    r = bridge.la_measure(
        samples=args.samples,
        samplerate=args.samplerate,
        channels=args.channels,
        time_ms=getattr(args, "time_ms", None),
        trigger=trig,
        wait_trigger=wait,
        out=Path(args.out) if args.out else None,
        sigrok_path=args.sigrok,
    )
    _dump(r)
    return 0 if r.get("ok") else 2


def cmd_la_decode(args: argparse.Namespace) -> int:
    trig, wait = _la_trigger_args(args)
    r = bridge.la_decode(
        protocol=args.protocol,
        samples=args.samples,
        samplerate=args.samplerate,
        channels=args.channels,
        time_ms=getattr(args, "time_ms", None),
        trigger=trig,
        wait_trigger=wait,
        channel_map=getattr(args, "channel_map", None),
        sigrok_path=args.sigrok,
    )
    _dump(r)
    return 0 if r.get("ok") else 2


def cmd_la_autoset(args: argparse.Namespace) -> int:
    from .live_la import LA

    r = LA.autoset(max_passes=getattr(args, "max_passes", 3))
    _dump(r)
    return 0 if r.get("ok") else 2


def cmd_plot(args: argparse.Namespace) -> int:
    src = Path(args.csv)
    out = Path(args.out) if args.out else src.with_suffix(".png")
    png = meas.plot_csv(src, out, probe_factor=args.probe_factor)
    m = meas.measure_csv(src, probe_factor=args.probe_factor)
    return _dump({"ok": True, "png": str(png), "measure": m.to_dict()})


def cmd_measure_file(args: argparse.Namespace) -> int:
    p = Path(args.csv)
    try:
        names, _, _, _ = meas.load_sigrok_csv(p)
        if names and any(n.upper().startswith("D") for n in names):
            return _dump(meas.measure_la_csv(p))
    except Exception:
        pass
    m = meas.measure_csv(p, probe_factor=args.probe_factor)
    return _dump(m.to_dict())


def _add_la_capture_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--samples", default="100k")
    p.add_argument("--samplerate", default="6m")
    p.add_argument("--channels", default="D0,D1,D2,D3,D4,D5,D6,D7")
    p.add_argument("--time-ms", type=int, default=None, dest="time_ms")
    p.add_argument("--trigger", default=None, help="e.g. D0=r  (r/f/0/1/e)")
    p.add_argument("--wait-trigger", action="store_true", dest="wait_trigger")
    p.add_argument("--out", default=None)


def _add_sigrok(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--sigrok",
        default=None,
        help="Path to sigrok-cli.exe (or set HTK_SIGROK_CLI)",
    )


def _add_scope_capture_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--samples", default="20k")
    p.add_argument("--samplerate", default="1m")
    p.add_argument("--channels", default="CH1")
    p.add_argument("--vdiv", default="1V", help="Vertical scale, e.g. 1V, 500mV")
    p.add_argument("--time-ms", type=int, default=None, dest="time_ms")
    p.add_argument("--out", default=None, help="Output CSV path")
    p.add_argument(
        "--probe-factor",
        type=float,
        default=1.0,
        dest="probe_factor",
        help="Must match physical probe switch (1 or 10)",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="hantek_navigator",
        description="Hantek 6022BL CLI (sigrok-backed scope + LA)",
    )
    _add_sigrok(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("policy", help="Print safety / H/P policy")
    sp = sub.add_parser("probe", help="USB mode + tooling probe")
    _add_sigrok(sp)
    sp = sub.add_parser("doctor", help="Stability checklist + smoke capture")
    _add_sigrok(sp)

    scope = sub.add_parser("scope", help="Oscilloscope commands")
    scope_sub = scope.add_subparsers(dest="scope_cmd", required=True)

    sp = scope_sub.add_parser("show", help="sigrok hantek-6xxx --show")
    _add_sigrok(sp)
    sp.add_argument("--json", action="store_true")

    sp = scope_sub.add_parser("capture", help="Capture to CSV")
    _add_sigrok(sp)
    _add_scope_capture_args(sp)
    sp.add_argument("--measure", action="store_true")

    sp = scope_sub.add_parser("measure", help="Capture + measurements JSON")
    _add_sigrok(sp)
    _add_scope_capture_args(sp)

    sp = scope_sub.add_parser("cal-test", help="1 kHz cal terminal stability test")
    _add_sigrok(sp)
    sp.add_argument("--repeat", type=int, default=10)
    sp.add_argument("--samples", default="20k")
    sp.add_argument("--samplerate", default="1m")
    sp.add_argument("--vdiv", default="1V")
    sp.add_argument("--channel", default="CH1")
    sp.add_argument("--probe-factor", type=float, default=1.0, dest="probe_factor")

    la = sub.add_parser("la", help="Logic analyzer commands (H/P released)")
    la_sub = la.add_subparsers(dest="la_cmd", required=True)

    sp = la_sub.add_parser("show", help="sigrok fx2lafw --show")
    _add_sigrok(sp)
    sp.add_argument("--json", action="store_true")

    sp = la_sub.add_parser("capture", help="fx2lafw capture")
    _add_sigrok(sp)
    _add_la_capture_args(sp)
    sp.add_argument("--format", default="csv", dest="format")
    sp.add_argument("--measure", action="store_true")

    sp = la_sub.add_parser("measure", help="Capture + digital metrics JSON")
    _add_sigrok(sp)
    _add_la_capture_args(sp)

    sp = la_sub.add_parser("decode", help="Capture + protocol decoder (-P)")
    _add_sigrok(sp)
    sp.add_argument("protocol", help="e.g. i2c, uart, spi, or i2c:scl=D0:sda=D1")
    _add_la_capture_args(sp)
    sp.add_argument("--map", dest="channel_map", default=None, help="rx=D0,tx=D1")

    sp = la_sub.add_parser("autoset", help="Discover activity → rate/window/trigger + start live")
    _add_sigrok(sp)
    sp.add_argument("--max-passes", type=int, default=3, dest="max_passes")

    sp = sub.add_parser("plot", help="PNG from existing CSV")
    sp.add_argument("csv")
    sp.add_argument("--out", default=None)
    sp.add_argument("--probe-factor", type=float, default=1.0, dest="probe_factor")

    sp = sub.add_parser("measure-file", help="Measure existing CSV")
    sp.add_argument("csv")
    sp.add_argument("--probe-factor", type=float, default=1.0, dest="probe_factor")

    sp = sub.add_parser("bench", help="Launch local web bench UI (http://127.0.0.1:8787)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)

    args = p.parse_args(argv)
    # Parent --sigrok may be used; copy onto namespace if sub omitted
    if getattr(args, "sigrok", None) is None:
        args.sigrok = None

    if args.cmd == "policy":
        return cmd_policy(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "scope":
        if args.scope_cmd == "show":
            return cmd_scope_show(args)
        if args.scope_cmd == "capture":
            return cmd_scope_capture(args)
        if args.scope_cmd == "measure":
            return cmd_scope_measure(args)
        if args.scope_cmd == "cal-test":
            return cmd_scope_cal_test(args)
    if args.cmd == "la":
        if args.la_cmd == "show":
            return cmd_la_show(args)
        if args.la_cmd == "capture":
            return cmd_la_capture(args)
        if args.la_cmd == "measure":
            return cmd_la_measure(args)
        if args.la_cmd == "decode":
            return cmd_la_decode(args)
        if args.la_cmd == "autoset":
            return cmd_la_autoset(args)
    if args.cmd == "plot":
        return cmd_plot(args)
    if args.cmd == "measure-file":
        return cmd_measure_file(args)
    if args.cmd == "bench":
        import os

        os.environ["HTK_BENCH_HOST"] = args.host
        os.environ["HTK_BENCH_PORT"] = str(args.port)
        from .webapp import main as bench_main

        bench_main()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
