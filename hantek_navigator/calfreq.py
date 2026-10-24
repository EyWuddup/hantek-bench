"""
Set Hantek 6022BL calibration-output frequency (USB control 0xE6).

Critical: OpenHantek / Ho-Ro firmware calls stop_sampling() before handling
vendor commands. Sending 0xE6 while bulk capture is running ACKs but does
*not* change the pin — pause 0xE3 first, then SETCALFREQ, then resume.
"""

from __future__ import annotations

import time
from struct import pack
from typing import Any

# Discrete steps shown in the bench UI (Hz)
CAL_FREQ_STEPS_HZ = [
    50,
    60,
    100,
    200,
    500,
    1000,
    2000,
    5000,
    10_000,
    20_000,
    50_000,
    100_000,
]

_SCOPE_IDS = [
    (0x04B5, 0x602A),  # OpenHantek / Ho-Ro firmware (SETCALFREQ works)
    (0x1D50, 0x608E),  # sigrok-named scope fw (often fixed ~1 kHz)
    (0x04B4, 0x602A),  # loader
]


def encode_cal_freq(freq_hz: float, *, dialect: str = "horo") -> int:
    """
    Encode Hz → one control byte.

    dialect=\"horo\": Ho-Ro / PyHT6022 extended (32 Hz..100 kHz).
    dialect=\"openhantek\": classic OH steps (50/60/100/200/500 + 1..100 kHz).
    """
    f = float(freq_hz)
    if dialect == "openhantek":
        if f < 1:
            raise ValueError("cal frequency must be >= 1 Hz")
        cf = int(f) // 1000
        if cf == 0:
            cf = 100 + int(f) // 10
            if cf == 110:  # 100 Hz → 0 for sigrok compatibility
                cf = 0
        if cf > 255:
            raise ValueError(f"cal frequency out of range: {freq_hz}")
        return cf

    # Ho-Ro extended encoding (matches LibUsbScope.set_calibration_frequency)
    if f < 32 or f > 100_000:
        raise ValueError("cal frequency must be 32 Hz .. 100 kHz")
    if f < 1000:
        return int((f + 5) // 10) + 100  # 103..199 → 32..990 Hz
    if f < 5600:
        return int((f + 50) // 100) + 200  # 201..255 → 100..5500 Hz
    return int((f + 500) // 1000)  # 6..100 → 6..100 kHz


def ensure_openhantek_firmware() -> dict[str, Any]:
    """
    If the scope is on sigrok VID (1D50:608E), re-flash Ho-Ro DSO6022BL firmware
    in-place via FX2 0xA0 download (no unplug required).
    """
    try:
        import usb1
        from PyHT6022.Firmware import dso6022bl_firmware
    except ImportError as exc:
        return {"ok": False, "error": f"missing deps: {exc}"}

    with usb1.USBContext() as ctx:
        # Already OH?
        h = ctx.openByVendorIDAndProductID(0x04B5, 0x602A, skip_on_error=True)
        if h is not None:
            vid_pid = "04B5:602A"
            try:
                bcd = h.getDevice().getbcdDevice()
            except Exception:
                bcd = None
            h.close()
            return {"ok": True, "flashed": False, "vid_pid": vid_pid, "bcd": bcd}

        h = ctx.openByVendorIDAndProductID(0x1D50, 0x608E, skip_on_error=True)
        if h is None:
            # Loader?
            h = ctx.openByVendorIDAndProductID(0x04B4, 0x602A, skip_on_error=True)
            if h is None:
                return {"ok": False, "error": "No Hantek 6022BL found"}
            from_loader = True
        else:
            from_loader = False

        claimed = False
        try:
            try:
                if h.kernelDriverActive(0):
                    h.detachKernelDriver(0)
            except Exception:
                pass
            h.claimInterface(0)
            claimed = True
            # Halt 8051 then upload Ho-Ro BL firmware packets
            h.controlWrite(0x40, 0xA0, 0xE600, 0, b"\x01", timeout=1000)
            for packet in dso6022bl_firmware:
                n = h.controlWrite(0x40, 0xA0, packet.value, 0, packet.data, timeout=5000)
                if n != packet.size:
                    return {
                        "ok": False,
                        "error": f"firmware packet short write {n}/{packet.size}",
                    }
        finally:
            if claimed:
                try:
                    h.releaseInterface(0)
                except Exception:
                    pass
            try:
                h.close()
            except Exception:
                pass

    # Renumeration can take a few seconds on Windows
    h = None
    bcd = None
    for _ in range(20):
        time.sleep(0.25)
        with usb1.USBContext() as ctx:
            h = ctx.openByVendorIDAndProductID(0x04B5, 0x602A, skip_on_error=True)
            if h is not None:
                try:
                    bcd = h.getDevice().getbcdDevice()
                except Exception:
                    bcd = None
                h.close()
                return {
                    "ok": True,
                    "flashed": True,
                    "vid_pid": "04B5:602A",
                    "bcd": bcd,
                    "from_loader": from_loader,
                }
    return {
        "ok": False,
        "error": "Flash attempted but 04B5:602A did not appear",
        "from_loader": from_loader,
    }


def _write_e6(handle: Any, byte: int) -> int:
    return handle.controlWrite(0x40, 0xE6, 0, 0, pack("B", byte), timeout=1000)


def set_cal_freq_on_handle(
    handle: Any,
    freq_hz: float,
    *,
    pause_sampling: bool = True,
    dialect: str = "horo",
) -> dict[str, Any]:
    """
    SETCALFREQ on an already-claimed device handle.
    If pause_sampling: send 0xE3=0, E6, 0xE3=1 (required while live streaming).
    """
    byte = encode_cal_freq(freq_hz, dialect=dialect)
    was_paused = False
    if pause_sampling:
        try:
            handle.controlWrite(0x40, 0xE3, 0, 0, b"\x00", timeout=1000)
            was_paused = True
            time.sleep(0.05)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"stop sampling before SETCALFREQ failed: {exc}",
                "freq_hz": freq_hz,
                "byte": byte,
            }
    try:
        written = _write_e6(handle, byte)
    except Exception as exc:
        if was_paused:
            try:
                handle.controlWrite(0x40, 0xE3, 0, 0, b"\x01", timeout=1000)
            except Exception:
                pass
        return {
            "ok": False,
            "error": str(exc),
            "freq_hz": freq_hz,
            "byte": byte,
        }
    if was_paused:
        try:
            handle.controlWrite(0x40, 0xE3, 0, 0, b"\x01", timeout=1000)
            time.sleep(0.05)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"SETCALFREQ ok but resume sampling failed: {exc}",
                "freq_hz": freq_hz,
                "byte": byte,
                "written": written,
            }
    return {
        "ok": True,
        "freq_hz": freq_hz,
        "byte": byte,
        "written": written,
        "paused_sampling": was_paused,
        "dialect": dialect,
        "method": "data",
    }


def set_cal_freq(freq_hz: float, *, ensure_fw: bool = True) -> dict[str, Any]:
    """
    Set cal output frequency.

    Prefers the live SCOPE handle (pause/resume). Otherwise opens the device,
    optionally flashes OpenHantek firmware if still on sigrok VID.
    """
    # Prefer in-process live stream handle
    try:
        from .live_stream import SCOPE

        if SCOPE.status.running and SCOPE._handle is not None:
            if ensure_fw and (SCOPE.status.vid_pid or "").upper().startswith("1D50"):
                SCOPE.stop()
                time.sleep(0.5)
                fw = ensure_openhantek_firmware()
                if not fw.get("ok"):
                    return {**fw, "freq_hz": freq_hz}
                st = SCOPE.start()
                if not st.get("running"):
                    return {
                        "ok": False,
                        "error": st.get("error") or "live restart after FW flash failed",
                        "firmware": fw,
                        "freq_hz": freq_hz,
                    }
                time.sleep(0.5)
            r = SCOPE.set_cal_freq(freq_hz)
            r["vid_pid"] = SCOPE.status.vid_pid
            r["via"] = "live_handle"
            return r
    except Exception as exc:
        live_err = str(exc)
    else:
        live_err = None

    if ensure_fw:
        fw = ensure_openhantek_firmware()
        if not fw.get("ok"):
            return {**fw, "freq_hz": freq_hz, "live_error": live_err}
    else:
        fw = None

    try:
        import usb1
    except ImportError as exc:
        return {"ok": False, "error": f"libusb1/usb1 missing: {exc}", "freq_hz": freq_hz}

    byte = encode_cal_freq(freq_hz)
    attempts: list[dict[str, Any]] = []
    with usb1.USBContext() as ctx:
        for vid, pid in _SCOPE_IDS:
            handle = ctx.openByVendorIDAndProductID(vid, pid, skip_on_error=True)
            if handle is None:
                continue
            claimed = False
            try:
                try:
                    if handle.kernelDriverActive(0):
                        handle.detachKernelDriver(0)
                except usb1.USBError:
                    pass
                handle.claimInterface(0)
                claimed = True
                # Not streaming on this path — still pause for safety
                r = set_cal_freq_on_handle(handle, freq_hz, pause_sampling=True)
                r["vid_pid"] = f"{vid:04X}:{pid:04X}"
                r["via"] = "direct"
                if fw:
                    r["firmware"] = fw
                if live_err:
                    r["live_error"] = live_err
                return r
            except Exception as exc:
                attempts.append({"vid_pid": f"{vid:04X}:{pid:04X}", "error": str(exc)})
            finally:
                if claimed:
                    try:
                        handle.releaseInterface(0)
                    except usb1.USBError:
                        pass
                handle.close()

    return {
        "ok": False,
        "error": "SETCALFREQ failed on all device IDs",
        "freq_hz": freq_hz,
        "byte": byte,
        "attempts": attempts,
        "firmware": fw,
        "hint": (
            "Need OpenHantek/Ho-Ro firmware (04B5:602A) and pause sampling around 0xE6. "
            "Unplug/replug if flash did not renumerate."
        ),
    }
