"""nginx 访问/错误日志的来源发现与有界尾读。

这一层存在的理由只有一条：**任何"再开一个读日志的接口"都必须复用同一份白名单**。
文件管理页的白名单（CP_FILE_ROOTS）管的是"人能打开哪些目录"，日志白名单管的是"哪些目录里
有 access.log"——两者默认值不一样：线上 nginx 的日志在 /var/log/nginx，而默认
CP_FILE_ROOTS 不含它，所以把它们混成一个键的代价就是"访问/错误日志"页签在全新安装里永远是空的
（批次㉟B 的 CHANGELOG 记过这条：当时只剩接口值和单测为证，没人看得见画面）。

发现结果只认三件事：realpath 落在白名单内、文件名像日志、是普通文件（不是 fifo/socket，
那种 read() 会一直等下去）。
"""

import os
import re
from stat import S_ISREG

from .. import config
from ..util import redact_creds

# 日志文件名：access.log / error.log、按站点分开的 <site>.access.log，
# 以及未压缩的轮替件 <...>.log.1。
# .gz 不收：tail 与统计都是按字节读末尾再解码，gzip 内容只会吐出乱码，
# 而"看昨天"这件事由轮替前的 .log.1 承担，不该由这一层去解压。
LOG_NAME_RE = re.compile(r"^(?:(?P<site>[A-Za-z0-9._-]{1,80})\.)?(?P<kind>access|error)\.log(?:\.1)?$")
# 扫描时跳过的目录：应用依赖目录里同名文件成千上万，
# 既慢又只会把真正想看的日志挤出结果列表。
_SKIP_DIRS = {"node_modules", ".git", ".venv", "dist", "build", "__pycache__", ".next", ".output"}
# tail 的字节上限：给人看几十行，读到 512KB 尾部足够。
TAIL_BYTES = 512 * 1024
# 最多列几个来源：装了 200 个站点的机器会把几百条塞进下拉框，
# 既没人能选，也让每次刷新多扫一遍盘。
MAX_LOG_FILES = 60


def log_roots() -> list[str]:
    """日志可以来自哪些目录：CP_LOG_ROOTS 加上文件管理白名单，去重保序。

    并起来是有意的：应用写在自己项目目录里的 access.log（CP_FILE_ROOTS 内）本来就能看，
    nginx 的默认位置靠 CP_LOG_ROOTS 补——两个键各自的语义不变，文件管理页不会因为
    多了一个日志根就能打开新目录。
    """
    out: list[str] = []
    for root in [*config.LOG_ROOTS, *config.FILE_ROOTS]:
        if root not in out:
            out.append(root)
    return out


def _inside_roots(real: str, roots: list[str]) -> bool:
    return any(real == root or real.startswith(root.rstrip("/") + "/") for root in roots)


def nginx_log_files() -> list[dict]:
    """白名单目录里的 nginx/应用访问日志（每个根最多扫到一级子目录）。

    只从这份白名单里找文件，等价于「文件管理页能打开的目录才能被 tail」：
    新加一个日志接口就新开一条任意文件读取的思路，不复用白名单迟早出事。
    结果按 label 排序——网页端、CLI 与统计接口都用同一份序号，
    所以顺序必须稳定，否则同一份日志在三个地方是三个号。
    """
    roots = log_roots()
    real_roots = [os.path.realpath(r) for r in roots]
    out: list[dict] = []
    seen: set[str] = set()
    for root in real_roots:
        bases = [root]
        try:
            with os.scandir(root) as it:
                bases += sorted(e.path for e in it if e.is_dir() and e.name not in _SKIP_DIRS)
        except OSError:
            pass
        for base in bases:
            try:
                names = sorted(e.name for e in os.scandir(base))
            except OSError:
                continue
            for name in names:
                m = LOG_NAME_RE.match(name)
                if not m:
                    continue
                real = os.path.realpath(os.path.join(base, name))
                if real in seen or not _inside_roots(real, real_roots):
                    continue
                try:
                    st = os.stat(real)
                except OSError:
                    continue
                # 只认普通文件：fifo 与 socket 的 read() 会一直等下去，
                # 而 tail 跑在线程池里，等待的就是这个进程的并发额度。
                if not S_ISREG(st.st_mode):
                    continue
                seen.add(real)
                out.append({
                    "path": real,
                    "label": f"{os.path.basename(base) or 'root'}/{name}",
                    "kind": m.group("kind"),
                    "site": m.group("site") or "-",
                })
                if len(out) >= MAX_LOG_FILES:
                    return sorted(out, key=lambda f: f["label"])
    return sorted(out, key=lambda f: f["label"])


def discovery_hint() -> str:
    """一个来源都没找到时给用户的修复线索（tail、统计与 CLI 共用这一份判词）。

    只说键名与命名规则，不拼出扫描到的绝对路径：这个字符串会进浏览器，
    而"白名单里有哪些目录"不该是页面能读到的信息。
    """
    return ("日志白名单里没有找到 access.log / error.log。nginx 的日志默认在 /var/log/nginx，"
            "它由 CP_LOG_ROOTS 配置（和文件管理页的 CP_FILE_ROOTS 是两个键，"
            "日志能读不等于文件管理页能打开那个目录）；按站点分开的 <站点>.access.log "
            "与未压缩的轮替件 <…>.log.1 也会被识别。改完 .env 要执行 "
            "choyeonctl upgrade --no-pull 重新渲染并重启才生效。")


def tail_file(path: str, lines: int, filt: str, max_chars: int) -> list:
    """从文件末尾最多读 TAIL_BYTES，再取后 lines 行（大日志不会把内存打满）。

    脱敏口径在这里，不在路由里：tail 出来的行会进审计、进浏览器、进截图，
    一行里带 `https://bob:S3cr3tT0ken@host` 是访问日志的常态。
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        raw = f.read()
    out = []
    for line in raw.decode(errors="replace").splitlines()[-lines:]:
        if filt and filt.lower() not in line.lower():
            continue
        out.append(redact_creds(line)[:max_chars])
    return out
