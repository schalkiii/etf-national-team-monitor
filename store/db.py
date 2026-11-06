# -*- coding: utf-8 -*-
"""SQLite 存储层：WAL + 原子写入 + 建表迁移。
学 etf-tracker：临时文件 + os.replace + fsync。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from core.config import DB_PATH, DATA_DIR

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS etf_daily (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL,
    shares REAL,
    adjust_factor REAL DEFAULT 1.0,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS index_daily (
    index_code TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,
    amount REAL,
    PRIMARY KEY (index_code, date)
);
CREATE TABLE IF NOT EXISTS margin_daily (
    date TEXT PRIMARY KEY,
    fin_balance_yi REAL,
    loan_balance_yi REAL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS calendar (
    date TEXT PRIMARY KEY,
    is_trading INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence TEXT,
    resonance TEXT,
    net_flow_yi REAL,
    flow_source TEXT,
    anomaly INTEGER DEFAULT 0,
    anomaly_sigma REAL,
    payload TEXT,              -- 完整 JSON(灯/依据/理由), 可回测可审计
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_signal ON signals(code, date);
CREATE TABLE IF NOT EXISTS fund_holders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    entity TEXT NOT NULL,
    shares_yi REAL NOT NULL,
    source TEXT DEFAULT '基金定期报告',
    UNIQUE (code, report_date, entity)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    etype TEXT,                -- official/protective/surprise
    note TEXT
);
CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now','localtime')),
    source TEXT, task TEXT,
    ok INTEGER, rows INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_code ON etf_daily(code, date);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.commit()


def atomic_write(path: Path, data: bytes) -> None:
    """原子写文件：先写临时文件再 os.replace，外部并发读不会读到半截。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def log_fetch(con: sqlite3.Connection, source: str, task: str, ok: bool, rows: int = 0, detail: str = "") -> None:
    con.execute(
        "INSERT INTO fetch_log (source, task, ok, rows, detail) VALUES (?,?,?,?,?)",
        (source, task, 1 if ok else 0, rows, detail[:500]),
    )
    con.commit()
