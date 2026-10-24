"""Resource and user-data paths for source runs and frozen Windows builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "hantek-bench"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def package_dir() -> Path:
    """Directory that contains this package (and ``static/``)."""
    if is_frozen():
        return Path(sys._MEIPASS) / "hantek_navigator"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def user_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / APP_NAME


def default_artifact_dir() -> Path:
    env = os.environ.get("HTK_ARTIFACT_DIR")
    if env:
        return Path(env)
    repo = os.environ.get("HTK_REPO")
    if repo:
        return Path(repo) / "assets" / "bench" / "hantek-6022bl"
    return user_data_dir() / "captures"


def default_probe_db() -> Path:
    env = os.environ.get("HTK_PROBE_DB")
    if env:
        return Path(env)
    return default_artifact_dir() / "probes.sqlite3"


def ensure_user_dirs() -> Path:
    root = user_data_dir()
    default_artifact_dir().mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return root
