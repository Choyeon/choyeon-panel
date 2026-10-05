"""统一配置：环境变量 → 类型化常量。

加载顺序（后者覆盖前者）：
1. 内置默认值
2. 仓库根 `.env`（存在时读取；systemd 的 Environment= 优先级更高，故 .env 不覆盖已存在的环境变量）
3. 进程环境变量

约定：所有路径常量均为绝对路径字符串，布尔开关统一用 `is_*` 语义的函数解析。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent  # repo root: choyeon-panel/
ENV_FILE = BASE / ".env"


def _parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与 export 前缀，不做变量展开。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _load_env() -> None:
    for key, value in _parse_env_file(ENV_FILE).items():
        os.environ.setdefault(key, value)


_load_env()


def _int(key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    if minimum is not None and val < minimum:
        return minimum
    if maximum is not None and val > maximum:
        return maximum
    return val


def _bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _path(key: str, default: str) -> str:
    raw = (os.environ.get(key) or "").strip()
    return str(Path(raw).expanduser()) if raw else default


def _paths(key: str, default: str) -> list[str]:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        raw = default
    out: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        p = str(Path(item).expanduser())
        if not p.startswith("/"):
            continue
        if p not in out:
            out.append(p)
    return out or [str(Path(default.split(",")[0]).expanduser())]


# ---------- 服务监听 ----------
HOST = os.environ.get("CP_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = _int("CP_PORT", 3210, 1, 65535)
TRUST_PROXY = _bool("CP_TRUST_PROXY")
LOG = _bool("CP_LOG")

# ---------- 路径 ----------
DATA_DIR = _path("CP_DATA_DIR", str(BASE / "data"))
APP_ROOT = _path("CP_APP_ROOT", "/root/www")
BACKUP_DIR = _path("CP_BACKUP_DIR", "/root/backups/panel")
FILE_ROOTS = _paths("CP_FILE_ROOTS", "/root/www,/etc/nginx,/root/backups/panel")
WEB_DIST = BASE / "web" / "dist"
SYS_HELPER = BASE / "backend" / "python" / "sys_helper.py"

# ---------- 外部命令 ----------
PYTHON = os.environ.get("CP_PYTHON", "python3").strip() or "python3"
UNIT_DIR = _path("CP_UNIT_DIR", "/etc/systemd/system")
NGINX_CONF_DIRS = _paths("CP_NGINX_CONF_DIRS", "/etc/nginx/conf.d,/etc/nginx/sites-available")
TLS_DIR = _path("CP_TLS_DIR", "/etc/letsencrypt/live")

# ---------- 安全策略 ----------
TOKEN_TTL_HOURS = _int("CP_TOKEN_TTL_HOURS", 12, 1, 72)
LOGIN_LIMIT = _int("CP_LOGIN_LIMIT", 10, 1, 100)          # 每 IP 每分钟
LOGIN_WINDOW_SEC = 60
AUDIT_KEEP = _int("CP_AUDIT_KEEP", 5000, 100)             # 审计日志保留条数
MAX_UPLOAD_BYTES = _int("CP_MAX_UPLOAD_MB", 8, 1, 512) * 1024 * 1024
TERMINAL_MAX_SESSIONS = _int("CP_TERMINAL_MAX_SESSIONS", 4, 1, 32)

# 是否发送 HSTS 头（仅在 HTTPS 直接暴露时开启；反代场景由 nginx 负责）
HSTS = _bool("CP_HSTS")

VERSION = "1.1.0"


def as_dict() -> dict:
    """用于 /api/health 与排障：仅暴露非敏感配置。"""
    return {
        "version": VERSION,
        "host": HOST,
        "port": PORT,
        "trustProxy": TRUST_PROXY,
        "log": LOG,
        "dataDir": DATA_DIR,
        "appRoot": APP_ROOT,
        "backupDir": BACKUP_DIR,
        "fileRoots": FILE_ROOTS,
        "unitDir": UNIT_DIR,
        "python": PYTHON,
        "webDist": str(WEB_DIST),
        "webBuilt": WEB_DIST.exists(),
        "tokenTtlHours": TOKEN_TTL_HOURS,
    }
