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
# 访问/错误日志的发现根。它和 CP_FILE_ROOTS 是两个键，而且必须分开：后者管"文件管理页能打开哪些
# 目录"，前者只管"哪些目录里有 access.log/error.log"。线上 nginx 的日志在 /var/log/nginx，
# 把它塞进 FILE_ROOTS 才看得到日志，等于顺手把系统日志目录变成人能浏览的地方。
# 发现时取 LOG_ROOTS ∪ FILE_ROOTS：应用把 access.log 写在自己项目目录里（后者之内）照旧能看。
LOG_ROOTS = _paths("CP_LOG_ROOTS", "/var/log/nginx")
WEB_DIST = BASE / "web" / "dist"
AVATAR_DIR = str(Path(DATA_DIR) / "avatars")

# ---------- 外部命令 ----------
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
# 一次列目录最多回给前端多少条目：node_modules、构建缓存这类目录动辄几十万个 inode，
# 全量返回就是几十万次 stat 加一份几十 MB 的 JSON——面板自己未必先炸，浏览器一定先炸，
# 而且这个请求还是同步 I/O 走线程池的，多开几个标签页能把线程占满。
MAX_DIR_ENTRIES = _int("CP_MAX_DIR_ENTRIES", 5000, 100, 200000)
# SQL 的服务端上界。面板这侧的 asyncio 墙钟只能杀掉 psql 客户端，杀不掉 postgres
# 后端正在跑的语句：pg_sleep、深 OFFSET 的 count(*)、被行锁卡住的 UPDATE 会继续占着
# 一条 max_connections 连接和它已经拿到的锁，浏览器断开也没人替它取消。
# 能真正中止执行的是服务端自己的 statement_timeout / lock_timeout，所以上界必须下发。
# 0 = 不注入，留给确实需要跑很久的维护语句。
PG_STATEMENT_TIMEOUT_MS = _int("CP_PG_STATEMENT_TIMEOUT_MS", 20000, 0, 300000)
PG_LOCK_TIMEOUT_MS = _int("CP_PG_LOCK_TIMEOUT_MS", 5000, 0, 60000)
# 恢复 SQLite 备份前要在应用目录里 walk 一遍找候选库。一个几百万元素的 uploads/
# 或 vendor/ 树能把一次点击变成几分钟的 scandir 风暴，而线程池的 worker 只有几十个，
# 文件页、备份列表、概览都从同一池子里取。命中上界时是报错而不是返回半份结果：
# 恢复靠名字（旧格式靠"最大的库"）在候选里定位目标文件，半份结果会认错库、
# 把备份覆盖到另一个库上——那是数据丢失，不是慢一点。
SQLITE_SCAN_MAX_ENTRIES = _int("CP_SQLITE_SCAN_MAX_ENTRIES", 200000, 1000, 5000000)
SQLITE_SCAN_BUDGET_SEC = _int("CP_SQLITE_SCAN_BUDGET_SEC", 20, 1, 300)
# 异地推送的时限。这两个数是同一件事的两个档位，出处只有这里：一次 PutObject 要真的
# 把整份归档推过网络（几百 MB 在限速线路上就是几分钟），而"测试连通性"只该是几个
# HEAD/LIST 往返——给前者用的 900 秒点一次"测试"会把页面钉死十五分钟。
# runner 那边不封顶更糟：卡住的推送攥着 flock，之后每次定时备份都只打印一句跳过。
CLOUD_PUSH_TIMEOUT_SEC = _int("CP_CLOUD_PUSH_TIMEOUT_SEC", 900, 30, 7200)
CLOUD_PROBE_TIMEOUT_SEC = _int("CP_CLOUD_PROBE_TIMEOUT_SEC", 20, 3, 300)

# 是否发送 HSTS 头（仅在 HTTPS 直接暴露时开启；反代场景由 nginx 负责）
HSTS = _bool("CP_HSTS")

VERSION = "1.1.10"
