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
# 只有 TCP 对端确实在这份名单里，才认它转来的 X-Real-IP / X-Forwarded-For。
# 面板只听 127.0.0.1，而本机任何进程（被攻破的业务应用、SSRF）都能绕过 nginx 直连端口，
# 随手换一个 X-Real-IP：不核对对端就采信的话，登录限速就变成"每个伪造 IP 一个空桶"。
# 反代装在别的机器上时，把那台机器的地址加进来。
TRUSTED_PROXIES = [
    ip.strip() for ip in os.environ.get("CP_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if ip.strip()
]
LOG = _bool("CP_LOG")

# ---------- 路径 ----------
DATA_DIR = _path("CP_DATA_DIR", str(BASE / "data"))
APP_ROOT = _path("CP_APP_ROOT", "/root/www")
BACKUP_DIR = _path("CP_BACKUP_DIR", "/root/backups/panel")
FILE_ROOTS = _paths("CP_FILE_ROOTS", "/root/www,/etc/nginx,/root/backups/panel")
WEB_DIST = BASE / "web" / "dist"
AVATAR_DIR = str(Path(DATA_DIR) / "avatars")
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
# 非上传接口的请求体上限：面板的 JSON 路由最大也就是设置页与 SQL 控制台，
# 2MB 足够宽松，又能挡住"未登录发几百 MB 让面板吞进内存"这种 OOM。
MAX_BODY_BYTES = _int("CP_MAX_BODY_MB", 2, 1, 64) * 1024 * 1024
# 解压上限：压缩包本身被 CP_MAX_UPLOAD_MB 卡住，但 8MB 的 zip 能在头部声明
# 5GB 的解压后尺寸（zip bomb）。面板是 root 进程，写满磁盘只需要用户点一次"解压"。
MAX_EXTRACT_BYTES = _int("CP_MAX_EXTRACT_MB", 512, 16, 8192) * 1024 * 1024
MAX_EXTRACT_FILES = _int("CP_MAX_EXTRACT_FILES", 5000, 10, 200000)
# 头像走浏览器本地压缩（256px JPEG 通常 20-80KB），2MB 足够宽松又能挡住误传的原始大图
AVATAR_MAX_BYTES = _int("CP_AVATAR_MAX_KB", 2048, 32, 16384) * 1024
TERMINAL_MAX_SESSIONS = _int("CP_TERMINAL_MAX_SESSIONS", 4, 1, 32)
# 终端输出缓冲水位：浏览器读得慢时 pty 的输出会在队列里堆积，`yes` 这类命令
# 几秒就能堆到几百 MB。超过这个水位主动断会话，而不是等 OOM 把面板一起带走。
TERMINAL_OUT_BUFFER = _int("CP_TERMINAL_OUT_BUFFER_KB", 4096, 256, 65536) * 1024

# 是否发送 HSTS 头（仅在 HTTPS 直接暴露时开启；反代场景由 nginx 负责）
HSTS = _bool("CP_HSTS")

VERSION = "1.1.10"


def as_dict() -> dict:
    """用于 /api/health 与排障：仅暴露非敏感配置。"""
    return {
        "version": VERSION,
        "host": HOST,
        "port": PORT,
        "trustProxy": TRUST_PROXY,
        "trustedProxies": TRUSTED_PROXIES,
        "log": LOG,
        "dataDir": DATA_DIR,
        "appRoot": APP_ROOT,
        "backupDir": BACKUP_DIR,
        "fileRoots": FILE_ROOTS,
        # 前端上传前先按这个额度拦一道，免得用户传完 300MB 才被服务端拒绝
        "maxUploadBytes": MAX_UPLOAD_BYTES,
        "maxBodyBytes": MAX_BODY_BYTES,
        "terminalMaxSessions": TERMINAL_MAX_SESSIONS,
        "unitDir": UNIT_DIR,
        "python": PYTHON,
        "webDist": str(WEB_DIST),
        "webBuilt": WEB_DIST.exists(),
        "tokenTtlHours": TOKEN_TTL_HOURS,
    }
