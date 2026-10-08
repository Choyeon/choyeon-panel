import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import config

os.makedirs(config.DATA_DIR, exist_ok=True)

_lock = threading.RLock()
conn = sqlite3.connect(f"{config.DATA_DIR}/panel.db", check_same_thread=False, timeout=15)
conn.row_factory = sqlite3.Row
# WAL + busy_timeout：面板进程与 backup_runner（独立进程）可安全并发读写
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA synchronous = NORMAL")
conn.execute("PRAGMA busy_timeout = 15000")
conn.execute("PRAGMA foreign_keys = ON")

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

MAX_DEPLOY_LOG = 256 * 1024  # 单条部署日志上限，防止失控增长


def _columns(table: str) -> list[str]:
    return [r["name"] for r in query(f"PRAGMA table_info({table})")]


def migrate() -> None:
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
        dcols = _columns("deployments")
        if "commit_sha" not in dcols:
            conn.execute("ALTER TABLE deployments ADD COLUMN commit_sha TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_deployments_app ON deployments(app_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(id)")
        conn.commit()


def query(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params=()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()):
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def get_setting(key: str) -> str | None:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row and row["value"] is not None else None


def set_setting(key: str, value) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, "" if value is None else str(value)),
    )


def jwt_secret() -> str:
    s = get_setting("jwt_secret")
    if not s:
        s = secrets.token_hex(32)
        set_setting("jwt_secret", s)
    return s


def new_epoch() -> str:
    """单调且唯一的版本号。用 time_ns + 随机后缀，避免同一次调用内多次 bump 产生相同值。"""
    return f"{time.time_ns():x}.{secrets.token_hex(4)}"


def token_epoch() -> str:
    """密码版本号：改密 / 删除用户后自增，使已签发 token 立即失效。"""
    v = get_setting("token_epoch")
    if not v:
        v = new_epoch()
        set_setting("token_epoch", v)
    return v


def bump_token_epoch() -> str:
    v = new_epoch()
    set_setting("token_epoch", v)
    return v


def audit(username, action: str, detail=None) -> None:
    # 三个字段都要截断：action/detail 有一部分调用方直接把请求体拼进去
    # （如 f"file:{body['action']}"），一条 1MB 的输入就能把审计表撑大、
    # 并把 /api/audit 页面变成自伤式 DoS。username 同理来自 token 声明。
    if detail is not None and len(str(detail)) > 500:
        detail = str(detail)[:500]
    execute(
        "INSERT INTO audit(username,action,detail) VALUES(?,?,?)",
        (str(username or "")[:64], str(action or "")[:64], detail),
    )


def append_deploy_log(dep_id: int, text: str) -> None:
    """追加部署日志并裁剪到上限，避免大输出撑爆数据库。"""
    with _lock:
        row = conn.execute("SELECT log FROM deployments WHERE id=?", (dep_id,)).fetchone()
        current = row["log"] if row else ""
        merged = f"{current}\n{text}\n"
        if len(merged) > MAX_DEPLOY_LOG:
            merged = "...(已截断)...\n" + merged[-MAX_DEPLOY_LOG:]
        conn.execute("UPDATE deployments SET log=? WHERE id=?", (merged, dep_id))
        conn.commit()


def prune_audit(keep: int | None = None) -> int:
    """保留最近 N 条审计记录，返回清理条数。"""
    keep = keep or config.AUDIT_KEEP
    with _lock:
        cur = conn.execute(
            "DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        conn.commit()
        return cur.rowcount or 0


def prune_deployments(keep_per_app: int = 20) -> int:
    with _lock:
        cur = conn.execute(
            "DELETE FROM deployments WHERE id NOT IN ("
            "  SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY app_id ORDER BY id DESC) rn FROM deployments)"
            "  WHERE rn <= ?)",
            (keep_per_app,),
        )
        conn.commit()
        return cur.rowcount or 0


def db_path() -> str:
    return str(Path(config.DATA_DIR) / "panel.db")


migrate()
