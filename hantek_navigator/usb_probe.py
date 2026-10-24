"""Detect Hantek 6022BL USB mode and local tooling."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Scope loader / vendor firmware (H/P pressed)
VID_SCOPE_LOADER = ("04B4", "602A")
VID_SCOPE_OH = ("04B5", "602A")  # OpenHantek firmware
VID_SCOPE_SIGROK = ("1D50", "608E")  # sigrok/fx2lafw Hantek 6022BL scope fw
# Logic analyzer (H/P released)
VID_LA = ("0925", "3881")  # Saleae-like EEPROM on some units
VID_LA_CYPRESS = ("04B4", "8613")  # bare Cypress FX2 — this bench unit in LA

SCOPE_PIDS = {VID_SCOPE_LOADER, VID_SCOPE_OH, VID_SCOPE_SIGROK}
LA_PIDS = {VID_LA, VID_LA_CYPRESS}


@dataclass
class UsbDeviceHit:
    vid: str
    pid: str
    instance_id: str
    friendly_name: str
    status: str
    mode: str  # scope_loader | scope | la | unknown
    service: str = ""


@dataclass
class ProbeResult:
    ok: bool
    mode: str  # scope | la | none | mixed | unknown
    devices: list[UsbDeviceHit] = field(default_factory=list)
    sigrok_cli: str | None = None
    sigrok_version: str | None = None
    openhantek_hint: str | None = None
    hints: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _run_ps(script: str) -> str:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        raise RuntimeError(completed.stderr.strip() or f"powershell exit {completed.returncode}")
    return completed.stdout


def _classify(vid: str, pid: str) -> str:
    key = (vid.upper(), pid.upper())
    if key == VID_SCOPE_LOADER:
        return "scope_loader"
    if key in {VID_SCOPE_OH, VID_SCOPE_SIGROK}:
        return "scope"
    if key in LA_PIDS:
        return "la"
    return "unknown"


def find_sigrok_cli(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    env = os.environ.get("HTK_SIGROK_CLI")
    if env:
        p = Path(env)
        if p.is_file():
            return p
    which = shutil.which("sigrok-cli")
    if which:
        return Path(which)
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        local / "hantek-bench" / "sigrok-cli" / "sigrok-cli.exe",
        local / "BMI-Hersom" / "sigrok-cli" / "sigrok-cli.exe",
        Path(r"C:\Program Files\sigrok\sigrok-cli\sigrok-cli.exe"),
        Path(r"C:\Program Files (x86)\sigrok\sigrok-cli\sigrok-cli.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def sigrok_version(sigrok: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(sigrok), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (completed.stdout or completed.stderr or "").splitlines()
    return line[0].strip() if line else None


def list_hantek_usb() -> list[UsbDeviceHit]:
    # One device per line: Status|FriendlyName|InstanceId|Service
    script = r"""
$devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
  Where-Object {
    $_.InstanceId -match 'VID_04B4&PID_602A|VID_04B4&PID_8613|VID_04B5&PID_602A|VID_1D50&PID_608E|VID_0925&PID_3881' -or
    $_.FriendlyName -match 'Hantek|OpenHantek|6022BL|fx2lafw|Cypress FX2'
  }
foreach ($d in $devs) {
  $svc = ''
  try {
    $svc = (Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data
  } catch {}
  '{0}|{1}|{2}|{3}' -f $d.Status, ($d.FriendlyName -replace '\|','/'), $d.InstanceId, $svc
}
"""
    out = _run_ps(script)
    hits: list[UsbDeviceHit] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue
        status, name, iid = parts[0], parts[1], parts[2]
        service = parts[3] if len(parts) > 3 else ""
        m = re.search(r"VID_([0-9A-Fa-f]{4}).*PID_([0-9A-Fa-f]{4})", iid)
        if not m:
            continue
        vid, pid = m.group(1).upper(), m.group(2).upper()
        hits.append(
            UsbDeviceHit(
                vid=vid,
                pid=pid,
                instance_id=iid,
                friendly_name=name,
                status=status,
                mode=_classify(vid, pid),
                service=service or "",
            )
        )
    return hits


def probe(sigrok_path: str | None = None) -> ProbeResult:
    result = ProbeResult(ok=False, mode="none")
    try:
        result.devices = list_hantek_usb()
    except Exception as exc:  # noqa: BLE001 — surface to JSON
        result.errors.append(f"usb_probe_failed: {exc}")

    modes = {d.mode for d in result.devices}
    if not result.devices:
        result.mode = "none"
        result.hints.append(
            "No Hantek 6022BL USB IDs found. Plug USB, keep H/P pressed for scope "
            "(04b4/04b5:602a / 1d50:608e) or release for LA (04b4:8613 or 0925:3881)."
        )
    elif modes & {"scope", "scope_loader"} and "la" in modes:
        result.mode = "mixed"
        result.hints.append("Mixed scope+LA USB IDs — unplug and reconnect after H/P change.")
    elif "la" in modes:
        result.mode = "la"
    elif modes & {"scope", "scope_loader"}:
        result.mode = "scope"
        if "scope_loader" in modes and "scope" not in modes:
            result.hints.append(
                "Device is in loader mode (04b4:602a). Opening with sigrok/OpenHantek uploads firmware."
            )
    else:
        result.mode = "unknown"

    for d in result.devices:
        if d.status and d.status.lower() != "ok":
            result.errors.append(f"device_status_{d.status}: {d.instance_id}")
            result.hints.append(
                "Install OpenHantek.inf (WinUSB) from OpenHantek6022 release driver/ folder "
                "(right-click INF → Install, or elevated pnputil /add-driver … /install)."
            )
        if d.service and d.service.upper() not in {"WINUSB", ""}:
            result.hints.append(
                f"USB service is '{d.service}' (want WINUSB). Stock Hantek driver conflicts — switch via OpenHantek.inf / Zadig."
            )

    sk = find_sigrok_cli(sigrok_path)
    if sk:
        result.sigrok_cli = str(sk)
        result.sigrok_version = sigrok_version(sk)
    else:
        result.errors.append("sigrok-cli_not_found")
        result.hints.append(
            "Install sigrok-cli (PulseView/sigrok nightly) or set HTK_SIGROK_CLI to sigrok-cli.exe. "
            "Default look paths: %LOCALAPPDATA%\\hantek-bench\\sigrok-cli\\ and "
            "%LOCALAPPDATA%\\BMI-Hersom\\sigrok-cli\\"
        )

    oh = Path(os.environ.get("USERPROFILE", "")) / "Downloads" / "hantek-setup" / "openhantek" / "OpenHantek.exe"
    if oh.is_file():
        result.openhantek_hint = str(oh)

    result.ok = (
        result.mode in {"scope", "la"}
        and not any(e.startswith("device_status_") for e in result.errors)
        and result.sigrok_cli is not None
    )
    if result.mode == "scope":
        result.hints.append("Keep H/P pressed for oscilloscope. Probe x1 on cal terminal for cal-test.")
    if result.mode == "la":
        result.hints.append(
            "H/P is released (LA mode / fx2lafw). Use `la capture`. "
            "Switch back (press H/P + replug) for scope."
        )
    return result
