"""SQLite store for probe calibration profiles."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_db_path() -> Path:
    from .paths import default_probe_db

    return default_probe_db()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS probes (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          model TEXT,
          attenuation REAL NOT NULL DEFAULT 1.0,
          channel_default TEXT NOT NULL DEFAULT 'CH2',
          notes TEXT DEFAULT '',
          created_utc TEXT NOT NULL,
          updated_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cal_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          probe_id INTEGER NOT NULL REFERENCES probes(id) ON DELETE CASCADE,
          started_utc TEXT NOT NULL,
          finished_utc TEXT,
          ok INTEGER NOT NULL DEFAULT 0,
          summary_json TEXT
        );
        CREATE TABLE IF NOT EXISTS cal_points (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL REFERENCES cal_runs(id) ON DELETE CASCADE,
          cal_freq_hz REAL NOT NULL,
          measured_freq_hz REAL,
          vpp REAL,
          vmin REAL,
          vmax REAL,
          duty_pct REAL,
          gain_error_pct REAL,
          freq_error_pct REAL,
          pass INTEGER NOT NULL DEFAULT 0,
          raw_json TEXT
        );
        """
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_probes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, model, attenuation, channel_default, notes, created_utc, updated_utc "
        "FROM probes ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_probe(
    conn: sqlite3.Connection,
    *,
    name: str,
    model: str = "",
    attenuation: float = 1.0,
    channel_default: str = "CH2",
    notes: str = "",
) -> dict[str, Any]:
    now = _now()
    existing = conn.execute("SELECT id FROM probes WHERE name = ?", (name,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE probes SET model=?, attenuation=?, channel_default=?, notes=?, updated_utc=? WHERE id=?",
            (model, attenuation, channel_default, notes, now, existing["id"]),
        )
        pid = int(existing["id"])
    else:
        cur = conn.execute(
            "INSERT INTO probes (name, model, attenuation, channel_default, notes, created_utc, updated_utc) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, model, attenuation, channel_default, notes, now, now),
        )
        pid = int(cur.lastrowid)
    conn.commit()
    row = conn.execute("SELECT * FROM probes WHERE id=?", (pid,)).fetchone()
    return dict(row)


def delete_probe(conn: sqlite3.Connection, probe_id: int) -> None:
    conn.execute("DELETE FROM probes WHERE id=?", (probe_id,))
    conn.commit()


def start_run(conn: sqlite3.Connection, probe_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO cal_runs (probe_id, started_utc, ok) VALUES (?,?,0)",
        (probe_id, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    ok: bool,
    summary: dict[str, Any],
) -> None:
    conn.execute(
        "UPDATE cal_runs SET finished_utc=?, ok=?, summary_json=? WHERE id=?",
        (_now(), 1 if ok else 0, json.dumps(summary, ensure_ascii=True), run_id),
    )
    conn.commit()


def add_point(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    cal_freq_hz: float,
    measured_freq_hz: float | None,
    vpp: float | None,
    vmin: float | None,
    vmax: float | None,
    duty_pct: float | None,
    gain_error_pct: float | None,
    freq_error_pct: float | None,
    passed: bool,
    raw: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO cal_points "
        "(run_id, cal_freq_hz, measured_freq_hz, vpp, vmin, vmax, duty_pct, "
        "gain_error_pct, freq_error_pct, pass, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            cal_freq_hz,
            measured_freq_hz,
            vpp,
            vmin,
            vmax,
            duty_pct,
            gain_error_pct,
            freq_error_pct,
            1 if passed else 0,
            json.dumps(raw or {}, ensure_ascii=True),
        ),
    )
    conn.commit()


def latest_run(conn: sqlite3.Connection, probe_id: int) -> dict[str, Any] | None:
    run = conn.execute(
        "SELECT * FROM cal_runs WHERE probe_id=? ORDER BY id DESC LIMIT 1",
        (probe_id,),
    ).fetchone()
    if not run:
        return None
    points = conn.execute(
        "SELECT * FROM cal_points WHERE run_id=? ORDER BY cal_freq_hz",
        (run["id"],),
    ).fetchall()
    out = dict(run)
    out["points"] = [dict(p) for p in points]
    if out.get("summary_json"):
        try:
            out["summary"] = json.loads(out["summary_json"])
        except json.JSONDecodeError:
            out["summary"] = None
    return out
