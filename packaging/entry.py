"""Frozen EXE entry: start the web bench and open the default browser."""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser


def main() -> int:
    # Allow `HantekBench.exe doctor` / `probe` etc. for support.
    if len(sys.argv) > 1 and sys.argv[1] not in {"bench", "--"}:
        from hantek_navigator.cli import main as cli_main

        return cli_main(sys.argv[1:])

    from hantek_navigator.paths import ensure_user_dirs

    ensure_user_dirs()

    host = os.environ.get("HTK_BENCH_HOST", "127.0.0.1")
    port = int(os.environ.get("HTK_BENCH_PORT", "8787"))
    os.environ["HTK_BENCH_HOST"] = host
    os.environ["HTK_BENCH_PORT"] = str(port)
    url = f"http://{host}:{port}/"

    def _open() -> None:
        time.sleep(1.0)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()

    from hantek_navigator.webapp import main as bench_main

    bench_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
