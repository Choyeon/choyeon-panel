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
# .gz 不进这份名单：tail 与单次统计都是"按字节读末尾再解码"，gzip 内容只会吐出乱码。
# 需要回看历史的只有按天趋势，它走下面那份 SERIES_NAME_RE 与 log_series()，
# 在那里解压才有明确的字节上界。
LOG_NAME_RE = re.compile(r"^(?:(?P<site>[A-Za-z0-9._-]{1,80})\.)?(?P<kind>access|error)\.log(?:\.1)?$")
# 轮替世代名：<同一个名>.<代数> 与 <同一个名>.<代数>.gz（logrotate 的 rotate N）。
SERIES_NAME_RE = re.compile(r"^(?P<stem>.+?)\.(?P<gen>\d{1,3})(?P<gz>\.gz)?$")
# 最多回看几份世代：装了几年没清的老日志不该把一个"看趋势"变成整盘扫描。
MAX_SERIES_FILES = 31
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


def log_series(path: str) -> list:
    """同一个日志的轮替世代，从新到旧：当前文件、`.1`、`.2.gz`……

    只在原文件所在的那一个目录里找同名世代，并且每份都要重新过一遍白名单与
    "普通文件"判定——"回看历史"不构成第二条任意文件读取的路径。世代数封顶
    `MAX_SERIES_FILES`，按代数排序，所以 `.10` 排在 `.2` 之后而不是按字符串排。
    """
    real = os.path.realpath(path)
    directory = os.path.dirname(real)
    base = os.path.basename(real)
    stem, _ = _stem_and_gen(base)
    real_roots = [os.path.realpath(r) for r in log_roots()]
    out: list = []
    try:
        names = sorted(e.name for e in os.scandir(directory))
    except OSError:
        return out
    for name in names:
        if name == stem:
            gen, gz = 0, False
        else:
            m = SERIES_NAME_RE.match(name)
            if not m or m.group("stem") != stem:
                continue
            gen, gz = int(m.group("gen")), bool(m.group("gz"))
            if gen < 1:
                # logrotate 的代数从 1 起：`access.log.0.gz` 这种名字只会和"当前文件"抢第 0 代，
                # 让它进来就等于给同一个来源凭空多算一份历史。
                continue
        cand = os.path.realpath(os.path.join(directory, name))
        if not _inside_roots(cand, real_roots):
            continue
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if not S_ISREG(st.st_mode):
            continue
        out.append({"path": cand, "name": name, "gen": gen, "gz": gz, "bytes": st.st_size})
    out.sort(key=lambda f: (f["gen"], f["name"]))
    return out[:MAX_SERIES_FILES]


def _stem_and_gen(name: str) -> tuple:
    """`access.log.7.gz` → ("access.log", 7)；没有代数后缀就是当前文件（第 0 代）。"""
    m = SERIES_NAME_RE.match(name)
    if m:
        return m.group("stem"), int(m.group("gen"))
    return name, 0


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


def tail_file(path: str, lines: int, filt: str, max_chars: int) -> dict:
    """从文件末尾最多读 TAIL_BYTES，再取后 lines 行（大日志不会把内存打满）。

    脱敏口径在这里，不在路由里：tail 出来的行会进审计、进浏览器、进截图，
    一行里带 `https://bob:S3cr3tT0ken@host` 是访问日志的常态。

    过滤排在截尾**之前**：旧写法先 `[-lines:]` 再按关键词筛，于是"最近 200 行里只有 2 行
    命中"会被当成"这个关键词只有 2 次命中"——第 201 行往前的命中永远看不见，而这正是
    排障时最想知道的那一条。返回的四个数分开说清三件事：给了几行（items）、窗口里命中几行
    （matched）、其中被行数上限裁掉几行（dropped）、字节窗口有没有到底
    （window_truncated）。只有 dropped 与 window_truncated 都是 0/false 时才是完整答案。
    """
    size = os.path.getsize(path)
    start = max(0, size - TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read()
    rows = raw.decode(errors="replace").splitlines()
    if filt:
        kf = filt.lower()
        rows = [r for r in rows if kf in r.lower()]
    matched = len(rows)
    keep = rows[max(0, matched - lines):] if lines > 0 else []
    return {
        "items": [redact_creds(r)[:max_chars] for r in keep],
        "matched": matched,
        "dropped": matched - len(keep),
        "window_truncated": start > 0,
    }
