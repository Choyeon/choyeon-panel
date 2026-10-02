import os
import re
import secrets
import sqlite3
import threading

from . import config

os.makedirs(config.DATA_DIR, exist_ok=True)
_lock = threading.RLock()
conn = sqlite3.connect(f"{config.DATA_DIR}/panel.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode = WAL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pass_hash TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL CHECK(type IN ('node','python')),
  repo_url TEXT NOT NULL,
  branch TEXT NOT NULL DEFAULT 'main',
  path TEXT NOT NULL,
  port INTEGER,
  domain TEXT,
  install_cmd TEXT,
  start_cmd TEXT NOT NULL,
  env TEXT NOT NULL DEFAULT '[]',
  unit_override TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS deployments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  log TEXT NOT NULL DEFAULT '',
  started_at TEXT DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS backups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('pg','app')),
  target TEXT NOT NULL,
  schedule TEXT NOT NULL DEFAULT 'daily',
  hour INTEGER NOT NULL DEFAULT 3,
  minute INTEGER NOT NULL DEFAULT 30,
  keep INTEGER NOT NULL DEFAULT 7,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);
"""


def _columns(table: str):
    return [r["name"] for r in query(f"PRAGMA table_info({table})")]


def migrate():
    with _lock:
        conn.executescript(SCHEMA)
        cols = _columns("users")
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        acols = _columns("apps")
        if "db_names" not in acols:
            conn.execute("ALTER TABLE apps ADD COLUMN db_names TEXT")
        if "unit_template" not in acols:
            conn.execute("ALTER TABLE apps ADD COLUMN unit_template TEXT")
        conn.commit()


def query(sql: str, params=()):
    with _lock:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()):
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def get_setting(key: str):
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] is not None else None


def set_setting(key: str, value: str):
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def jwt_secret() -> str:
    s = get_setting("jwt_secret")
    if not s:
        s = secrets.token_hex(32)
        set_setting("jwt_secret", s)
    return s


def audit(username, action: str, detail=None):
    execute("INSERT INTO audit(username,action,detail) VALUES(?,?,?)", (username, action, detail))


migrate()
