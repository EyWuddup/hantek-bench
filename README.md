# Hantek Bench

Windows bench UI + CLI for the **Hantek 6022BL** USB oscilloscope / logic analyzer.

Live dual-channel scope (native USB / OpenHantek-class firmware), optional sigrok captures, probe profiles, and bus decode — without needing OpenHantek or PulseView open.

## Quick start (release)

1. Install the WinUSB driver from [OpenHantek6022](https://github.com/OpenHantek/OpenHantek6022/releases) (`driver/OpenHantek.inf`).
2. Download the latest **Windows setup** from [Releases](https://github.com/EyWuddup/hantek-bench/releases).
3. Run **Hantek Bench** — it opens **http://127.0.0.1:8787**.
4. Keep the physical **H/P** button **pressed** for scope mode. Release + USB replug for LA.

Portable zip: unzip and run `HantekBench.exe`. Captures / probe DB live under `%LOCALAPPDATA%\hantek-bench\`.

## H/P button

| H/P | Mode | USB IDs | Notes |
| --- | --- | --- | --- |
| **Pressed (P)** | Oscilloscope | `04b4:602a` → `04b5:602a` (OpenHantek) or `1d50:608e` (sigrok) | Live stream + `scope *` |
| **Released** | Logic analyzer (8 ch) | `0925:3881` | `la *` / PulseView / fx2lafw |

Not an MSO. Unplug USB → change H/P → replug when switching.

## Develop from source

```powershell
cd hantek-bench
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "."
python -m hantek_navigator bench
```

Optional sigrok-cli for one-shot / LA decode:

```powershell
$env:HTK_SIGROK_CLI = "$env:LOCALAPPDATA\hantek-bench\sigrok-cli\sigrok-cli.exe"
```

CLI examples:

```text
python -m hantek_navigator probe
python -m hantek_navigator doctor
python -m hantek_navigator scope measure --channels CH1,CH2 --probe-factor 1
python -m hantek_navigator la capture --samples 100k
```

## Windows build (local)

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm packaging/hantek-bench.spec
# optional installer (Inno Setup 6):
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=0.5.0 packaging\hantek-bench.iss
```

CI builds both the portable zip and the setup EXE on every `v*` tag (see `.github/workflows/release.yml`).

## MCP

See `mcp.example.json` and `mcp-launch.cmd` for Cursor / Claude Desktop wiring.

## License

MIT
