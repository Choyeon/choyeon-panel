"""存储占用对账：在"还剩多少"之外，说清谁在吃掉它。

已有的「磁盘余量」自检项回答的是"还剩多少"，答不出管理员紧接着要问的那句
"那几十 GB 是被什么占掉的"。对一台自己跑构建、自己攒备份的机器，答案通常落在
三类东西上：应用目录里的依赖树、越攒越多的 journald、以及保留策略没管住的旧备份产物。
这一层把这三类摊开成一张带判词的清单，并且如实标出哪些目录没扫完。

严格只读：不删文件、不改配额、不动 keep 策略。清理一律给可以复制走的命令——
"这个目录还需不需要"只有管理员知道，面板替它按 rm 不是自动化，是赌博。
真要清旧备份，路径也指向 choyeonctl backup（那里有保留策略），不是 rm -rf。

两处如实承认局限，不装成全知：
① 每个根目录最多扫 ROOT_BUDGET_SEC 秒、MAX_DEPTH 层，扫不完就标 partial，
   给出的是下界而不是全量；② 只看得见本机文件系统，远端对象存储不在这张表里。

挂载点单独占一节，因为"还剩多少"问的从来不是数据目录那一块盘。别的分区写满、inode 用尽
（容量列还剩几十 GB，却已经建不出任何新文件）、内核把出错的文件系统重挂成只读——这三种情况
过去在这块面板上完全不可见：余量自检只看数据目录所在的那块盘，占用扫描只看登记的目录。
"""

from __future__ import annotations

import asyncio
import heapq
import os
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from .. import config, probe
from ..severity import SEV_INFO, SEV_RISK, SEV_WARN
from ..util import PROBE_TIMEOUT, human_bytes, run, tallied
from . import alerts_service

CACHE_TTL = 300.0
ROOT_BUDGET_SEC = 2.0
MAX_DEPTH = 4
TOP_FILES = 8
TOP_SUBDIRS = 6

JOURNAL_WARN = 2 * 1024**3
BACKUP_WARN = 4 * 1024**3
BIG_FILE_WARN = 2 * 1024**3
# 分区「要满了」这条线不在这里定义：出处是设置页的 alert_disk_pct，读
# alerts_service.disk_thresholds()。过去这里另写一份 90，管理员调到 70 之后告警在响、
# 这一页却写「没有异常占用」。
INODE_WARN_PCT = 90
INODE_RISK_PCT = 97
TOP_MOUNTS = 24
MOUNTS_PATH = "/proc/mounts"
# /proc/mounts 里九成行是内核自己挂的东西，它们的 statvfs 毫无运维意义；overlay 更坏：
# 它报的是下层目录所在那块盘的数字，多列一行会让人以为是两块盘各吃掉一半。
PSEUDO_FSTYPES = frozenset({
    "proc", "sysfs", "cgroup", "cgroup2", "devtmpfs", "devpts", "securityfs", "pstore",
    "bpf", "autofs", "hugetlbfs", "mqueue", "debugfs", "tracefs", "fusectl", "configfs",
    "selinuxfs", "ramfs", "efivarfs", "binfmt_misc", "rpc_pipefs", "nsfs", "overlay",
})
# snap 的 squashfs、光驱镜像这类天生"100% 已用"：那是只读镜像的性质，不是磁盘告急。
SNAPSHOT_FSTYPES = frozenset({"squashfs", "iso9660", "udf", "cdfs"})
# RAM 盘要进表（写满是真的会把构建和临时文件打死），但它天生带 ro 挂载
# （这台机器上 /run/credentials/* 就是 5 个只读 tmpfs，systemd 设计如此），
# 所以"只读"那一档跳过它，否则会常驻 5 条没意义的告警。
RAM_FSTYPES = frozenset({"tmpfs"})


NOTES = (
    f"每个目录最多扫 {ROOT_BUDGET_SEC:.0f} 秒、{MAX_DEPTH} 层，标了「未扫完」的给出的是下界而不是全量。",
    "不跟随符号链接、不跨文件系统计数；备份产物只统计不删除。",
    "盘扫的只是登记的目录；挂载点表读的是面板进程自己的挂载命名空间（unit 开着 PrivateTmp，"
      "它的 /tmp 与终端里那个不一定是同一个），列每个挂载点的容量、inode、是否只读，"
      "同一块盘挂两处会标出来；远端对象存储仍然不在视野内。",
    "登记的根可以互相包含（数据目录就在应用根里面）：逐行各算各的，合计只算最外层那一份。",
)

_GIB = 1024**3
_UNITS = {"bytes": 1, "b": 1, "k": 1024, "m": _GIB // 1024, "g": _GIB, "t": _GIB * 1024, "p": _GIB * 1024 * 1024}
_JOURNAL_RE = re.compile(r"([\d.]+)\s*(bytes|[bkmtgp])i?b?", re.IGNORECASE)

_LOCK = asyncio.Lock()
_cache: dict = probe.new_cache()


def _entry_size(st) -> int:
    """磁盘真实占用：有 block 就用 block 计数，稀疏文件才不会报成 0。"""
    blocks = int(getattr(st, "st_blocks", 0) or 0)
    if blocks:
        return blocks * 512
    return max(int(getattr(st, "st_size", 0) or 0), 0)


def _walk(root: str, budget: float = ROOT_BUDGET_SEC, max_depth: int = MAX_DEPTH) -> dict:
    """有界遍历一个目录，返回占用下界、一级子目录榜、大文件榜。

    符号链接只按链接本身记账、绝不跟进去；跨挂载点的条目跳过。这两条不是为了省时间，
    是为了"只读体检"真的只读——被审计目录里一条指向 / 的软链就足以让遍历停不下来。
    一级子目录榜靠 branch 标记在同一次遍历里累加，不再对每个子目录重走一遍。
    """
    out: dict = {
        "path": root,
        "bytes": 0,
        "files": 0,
        "dirs": 0,
        "errors": 0,
        "partial": False,
        "subdirs": [],
        "branch_count": 0,
        "big_files": [],
    }
    base = Path(root)
    if not base.is_dir():
        return out
    try:
        base_dev = base.stat().st_dev
    except OSError:
        out["errors"] += 1
        out["partial"] = True
        return out

    deadline = time.monotonic() + max(float(budget), 0.0)
    heap: list[tuple[int, str]] = []
    branch_bytes: dict[str, int] = {}
    stack: list[tuple[Path, int, str]] = [(base, 0, "")]
    stopped = False

    def account(size: int, branch: str) -> None:
        out["bytes"] += size
        if branch:
            branch_bytes[branch] = branch_bytes.get(branch, 0) + size

    while stack and not stopped:
        path, depth, branch = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            out["errors"] += 1
            continue
        with it:
            for entry in it:
                if time.monotonic() >= deadline:
                    out["partial"] = True
                    stopped = True
                    break
                try:
                    link = entry.is_symlink()
                    st = entry.stat(follow_symlinks=False)
                    if link:
                        account(_entry_size(st), branch)
                        out["files"] += 1
                        continue
                    if st.st_dev != base_dev:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        out["dirs"] += 1
                        if depth + 1 >= max_depth:
                            out["partial"] = True
                            continue
                        child_branch = branch or entry.name
                        stack.append((Path(entry.path), depth + 1, child_branch))
                    elif entry.is_file(follow_symlinks=False):
                        size = _entry_size(st)
                        account(size, branch)
                        out["files"] += 1
                        item = (size, entry.path)
                        if len(heap) < TOP_FILES:
                            heapq.heappush(heap, item)
                        elif size > heap[0][0]:
                            heapq.heapreplace(heap, item)
                except OSError:
                    out["errors"] += 1
                    continue

    out["subdirs"] = [
        {"name": n, "bytes": b}
        for n, b in sorted(branch_bytes.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SUBDIRS]
        if b > 0
    ]
    # 榜单只留最大的 TOP_SUBDIRS 个，但没列出来的个数得如实给出去：`dirs` 是全部层级的目录数，
    # 拿它减掉榜单行数会把"另有 N 个子目录"夺大成几百，而它实际只有几个。
    out["branch_count"] = sum(1 for b in branch_bytes.values() if b > 0)
    out["big_files"] = [{"path": p, "bytes": s} for s, p in sorted(heap, key=lambda item: -item[0])]
    return out


def _parse_journal(text: str) -> int | None:
    """解析 `journalctl --disk-usage` 里那句 "take up 1.3G in the file system"。

    必须带单位才算数：少了这个约束，输出里的路径、年份里的数字都会被当成大小。
    """
    m = _JOURNAL_RE.search(text or "")
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = _UNITS.get(m.group(2).lower())
    if mult is None:
        return None
    return int(num * mult)


def _roots() -> list[dict]:
    """要盘点哪些目录：面板自己的数据与备份、journald 落盘处、应用与配置登记的根。"""
    seen: set[str] = set()
    out: list[dict] = []
    for label, path in (
        ("面板数据", config.DATA_DIR),
        ("面板备份", config.BACKUP_DIR),
        ("系统日志", "/var/log/journal"),
    ):
        np = os.path.normpath(str(path))
        if np in seen:
            continue
        seen.add(np)
        out.append({"label": label, "path": str(path)})
    for raw in config.FILE_ROOTS or []:
        p = str(raw or "").strip()
        if not p:
            continue
        np = os.path.normpath(p)
        if np in seen:
            continue
        seen.add(np)
        out.append({"label": "应用与配置目录", "path": p})
    return out


def _backup_stats(path: str) -> dict:
    """备份目录单独看一眼：份数与最旧/最新日期，判词里要说"攒了多久"。"""
    root = Path(path)
    stamps: list[float] = []
    if not root.is_dir():
        return {"count": 0, "oldest": None, "newest": None}
    try:
        for entry in root.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    stamps.append(entry.stat().st_mtime)
            except OSError:
                continue
            if len(stamps) > 5000:
                break
    except OSError:
        pass
    if not stamps:
        return {"count": 0, "oldest": None, "newest": None}
    stamps.sort()
    return {
        "count": len(stamps),
        "oldest": datetime.fromtimestamp(stamps[0], UTC).strftime("%Y-%m-%d"),
        "newest": datetime.fromtimestamp(stamps[-1], UTC).strftime("%Y-%m-%d"),
    }


def _disk_info() -> dict:
    try:
        u = shutil.disk_usage(config.DATA_DIR)
    except OSError:
        return {"ok": False, "path": config.DATA_DIR}
    used_pct = round((u.total - u.free) * 100 / u.total) if u.total else None
    return {
        "ok": True,
        "path": config.DATA_DIR,
        "total": u.total,
        "total_human": human_bytes(u.total),
        "free": u.free,
        "free_human": human_bytes(u.free),
        "used_pct": used_pct,
    }


def _unescape_mount(name: str) -> str:
    """/proc/mounts 把挂载点里的空格、制表符、换行写成八进制转义（反斜杠加三位八进制数）。

    不还原的话，带空格的挂载点（外接盘上常见）会以转义原文出现在表里，既没法照着敲进
    命令，也没法拿去 statvfs。
    """
    out: list[str] = []
    i, n = 0, len(name)
    while i < n:
        if name[i] == "\\" and i + 4 <= n and all(c in "01234567" for c in name[i + 1 : i + 4]):
            out.append(chr(int(name[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(name[i])
            i += 1
    return "".join(out)


def _parse_mounts(text: str) -> list[dict]:
    """把 /proc/mounts 折成"真正挂着的一块块盘"，同一个挂载点取最后一次挂载。

    顺序即语义：后写的挂载盖住前面的，取第一次会把已经被盖掉的那块盘也算进来。
    只读判定必须逐项比对选项，不能拿子串找 ro：内核挂在 errors=remount-ro 上的盘此刻
    是读写着的（这台机器的根分区就是），而 squashfs 这类只读镜像天生带 ro，那是性质不是故障。
    """
    by_mp: dict[str, dict] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mp = _unescape_mount(parts[1])
        fstype, opts = parts[2], parts[3]
        if not mp.startswith("/") or fstype in PSEUDO_FSTYPES:
            continue
        ro = "ro" in opts.split(",")
        if fstype in RAM_FSTYPES and ro:
            # 只读的 RAM 盘写不进任何东西，永远不会成为"这块盘满了"。这台机器上
            # /run/credentials/* 就是五个这样的挂载点（systemd 设计如此），列出来只是噪声。
            continue
        by_mp[mp] = {"path": mp, "device": parts[0], "fstype": fstype, "ro": ro,
                    "snapshot": fstype in SNAPSHOT_FSTYPES}
    return list(by_mp.values())


def _mount_of(path: str, items: list[dict]) -> str | None:
    """某个路径落在哪块盘上：按最长前缀匹配，跟内核的判法一致。"""
    norm = os.path.normpath(str(path))
    best: str | None = None
    for it in items:
        mp = it["path"]
        inside = mp == "/" or norm == mp or norm.startswith(mp.rstrip("/") + "/")
        if inside and (best is None or len(mp) > len(best)):
            best = mp
    return best


def _flag_shared_device(items: list[dict]) -> None:
    """同一块文件系统挂了不止一处，就在后出现的那一行标出它和谁是同一块盘。

    面板自己就踩在这条上：unit 里开着 PrivateTmp，本进程的挂载命名空间因此多出一个
    /var/tmp，它就是根分区（这台机器上实测 st_dev 都是 2049）。那一行的容量、inode 和
    / 一模一样，不标注看上去像"两块盘都用到 21%"，管理员会去找一块并不存在的盘。
    """
    anchor: dict[int, str] = {}
    for it in items:
        try:
            dev = os.stat(it["path"]).st_dev
        except OSError:
            it["same_dev_as"] = None
            continue
        first = anchor.setdefault(dev, it["path"])
        it["same_dev_as"] = None if first == it["path"] else first


def _mounts_text() -> str:
    """原始 /proc/mounts 文本。路径与解码方式只有这一处，读法变了两个读者不会分叉。"""
    with open(MOUNTS_PATH, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def read_mounts() -> list[dict]:
    """解析过的挂载表；读不到就是空表（调用方自己决定"未知"怎么显示）。"""
    try:
        return _parse_mounts(_mounts_text())
    except OSError:
        return []


def mount_of(path: str) -> str | None:
    """某个路径**落在哪块盘上**（挂载点），按最长前缀匹配。

    概览的磁盘仪表要的是这个答案：它量的是数据目录所在分区，可标签上写
    `/root/www/choyeon-panel/data` 既读不出是哪块盘，又把面板自己的安装路径
    暴露到每个登录者的首页上。存储页签列的也是这张表，两处必须同一个判法。
    """
    return _mount_of(path, read_mounts())


def _mounts(raw: str | None = None) -> dict:
    """逐个挂载点当场 statvfs：容量、inode、是否只读。

    inode 是这一节存在的理由：ext4 的 inode 表在 mkfs 时就定死了，用尽之后新建**任何**
    文件都报 No space left on device，而 df 的容量列还显示剩几十 GB。只看余量的面板在那种
    机器上会一直说"盘很空"。f_files 为 0 是 vfat 这类文件系统根本没有 inode 表，如实给 None，
    不能折成 0%——0% 会被读成"没有问题"。
    百分比与磁盘余量那一栏同口径：写不进去的都算已用（含 ext4 预留块），所以已用% 加剩余
    正好是整块盘，同一页上两个百分比不会互相矛盾。df 的 Use% 会低几个点——它把预留块既
    不算 Used 也不算 Avail；对数时以这一页「写不进去就算已用」为准。
    """
    if raw is None:
        try:
            raw = _mounts_text()
        except OSError:
            return {"ok": False, "items": [], "total": 0, "omitted": 0,
                    "panel_mount": None, "reason": "读不到 " + MOUNTS_PATH}
    items: list[dict] = []
    for m in _parse_mounts(raw):
        rec = dict(m)
        try:
            s = os.statvfs(m["path"])
        except OSError:
            # 挂着却读不到，也要如实列出来：/proc/mounts 上有它、statvfs 却失败，通常正是
            # 那块盘出问题的样子（设备掉线、网络存储断了、挂载点权限被改）。藏起来等于把线索丢掉。
            rec.update({"unknown": True, "total": 0, "total_human": "—", "used": 0,
                        "used_human": "—", "free": 0, "free_human": "—",
                        "used_pct": None, "inode_pct": None})
            items.append(rec)
            continue
        fr = s.f_frsize or s.f_bsize
        total = int(s.f_blocks) * fr
        avail = int(s.f_bavail) * fr
        used = max(total - avail, 0)
        rec.update({
            "unknown": False,
            "total": total,
            "total_human": human_bytes(total),
            "used": used,
            "used_human": human_bytes(used),
            "free": avail,
            "free_human": human_bytes(avail),
            "used_pct": round(used * 100 / total) if total else None,
            "inode_pct": round((int(s.f_files) - int(s.f_ffree)) * 100 / int(s.f_files)) if s.f_files else None,
        })
        items.append(rec)
    panel = _mount_of(config.DATA_DIR, items)
    for it in items:
        it["panel"] = it["path"] == panel
    # 最吃紧的排前面，读不到的和 0% 的沉底：这张表的用途是先看见哪块盘要出事。
    items.sort(key=lambda it: (it["used_pct"] is None, -(it["used_pct"] or 0), it["path"]))
    shown = items[:TOP_MOUNTS]
    _flag_shared_device(shown)
    return {"ok": True, "items": shown, "total": len(items),
            "omitted": max(len(items) - TOP_MOUNTS, 0), "panel_mount": panel}


def _findings(roots: list[dict], journal: dict, disk: dict, big_files: list[dict],
              mounts: list[dict] | None = None,
              thresholds: dict | None = None) -> list[dict]:
    """把数字折成判词。每条都带一句"为什么"和一个能复制的动作，不写"建议关注"这种空话。

    thresholds 由调用方给（一轮只读一次配置），留空时现读——判"这块盘要满了"的线
    不在这里定义，出处是 alerts_service.disk_thresholds()。曾经这里写着 90、自检写着
    75/90、告警写着 alert_disk_pct，管理员把配置调到 70，于是告警在响而这一页写
    「没有异常占用」。
    """
    out: list[dict] = []
    dt = thresholds or alerts_service.disk_thresholds()
    jb = journal.get("bytes")
    if isinstance(jb, int) and jb >= JOURNAL_WARN:
        out.append({
            "id": "journal",
            "severity": SEV_WARN,
            "title": f"systemd 日志已占 {human_bytes(jb)}",
            "detail": "journal 本来就按时间滚动，占这么多通常不代表还要留着排障。"
                      "先收上限，别去删当前文件：vacuum 只会裁掉最老的那部分。",
            "fix": "journalctl --vacuum-size=500M",
        })

    backup = next((r for r in roots if r.get("label") == "面板备份"), None)
    if backup and backup.get("exists") and int(backup.get("bytes") or 0) >= BACKUP_WARN:
        bs = backup.get("backups") or {}
        out.append({
            "id": "backups",
            "severity": SEV_WARN,
            "title": f"备份目录累计 {human_bytes(int(backup['bytes']))}",
            "detail": f"共 {bs.get('count', 0)} 份产物，最早 {bs.get('oldest') or '—'}、最新 "
                      f"{bs.get('newest') or '—'}。每个任务各有 keep 名额，超了名额还留着"
                      "一般是任务被停用或改过名字。面板不代删，先核对再动手。",
            "fix": "choyeonctl backup list",
        })

    if disk.get("ok") and isinstance(disk.get("used_pct"), int) and disk["used_pct"] >= dt["warn"]:
        out.append({
            "id": "tight",
            "severity": SEV_RISK if disk["used_pct"] >= dt["crit"] else SEV_WARN,
            "title": f"分区已用 {disk['used_pct']}%，只剩 {human_bytes(int(disk['free']))}",
            "detail": "备份、数据库写入和构建缓存都落在这一块的盘上。接近满的时候最先出事的"
                      "往往是备份，而备份失败不会有任何提示音。这条线取自设置页的磁盘阈值"
                      f"（警戒线 {dt['warn']}%，要紧线 {dt['crit']}%），改一次各处一起跟着变。",
            "fix": "choyeonctl storage",
        })

    for m in mounts or []:
        path, dev, ftype = m["path"], m.get("device") or "?", m.get("fstype") or "?"
        if m.get("unknown"):
            out.append({
                "id": "mount:" + path,
                "severity": SEV_WARN,
                "title": f"挂载点 {path} 挂着却读不到（{dev}，{ftype}）",
                "detail": "/proc/mounts 说它在那儿，statvfs 却失败。设备掉线、网络存储断了、"
                          "挂载点权限被改过都会是这样。这一条问的不是占用，而是这块盘还能不能用："
                          "往上面写的每一笔都会直接报错。",
                "fix": f"findmnt --target '{path}'",
            })
            continue
        ipct = m.get("inode_pct")
        if isinstance(ipct, int) and ipct >= INODE_WARN_PCT:
            cap = m.get("used_pct")
            out.append({
                "id": "inode:" + path,
                "severity": SEV_RISK if ipct >= INODE_RISK_PCT else SEV_WARN,
                "title": f"{path} 的 inode 已用 {ipct}%"
                        + (f"，而容量才用 {cap}%" if isinstance(cap, int) else ""),
                "detail": "ext4 的 inode 表在 mkfs 时就定死了，用尽之后新建任何文件都报 No space "
                          "left on device，容量列看着还剩一大截也一样。吃掉它的通常是海量小文件"
                          "（缓存、session、邮件目录、node_modules）。先数清楚是谁在占，"
                          "删文件是快办法，改 inode 数要重建文件系统。",
                "fix": f"find '{path}' -xdev -printf '%h\\n' | sort | uniq -c | sort -rn | head -20",
            })
        pct = m.get("used_pct")
        if (isinstance(pct, int) and pct >= dt["warn"]
                and not m.get("panel") and not m.get("snapshot")):
            out.append({
                "id": "mount:" + path,
                "severity": SEV_RISK if pct >= dt["crit"] else SEV_WARN,
                "title": f"{path}（{dev}）已用 {pct}%，剩 {m['free_human']}",
                "detail": "这一块的紧张不会惊动面板自己的余量自检——那一项看的是数据目录所在的那块盘。"
                          "先出事的是落在这上面的东西：上传临时文件、数据库 WAL、构建产物，"
                          "而且备份失败是不响的。判它紧用的是和数据目录同一把尺"
                          f"（警戒线 {dt['warn']}%，要紧线 {dt['crit']}%）。"
                          "别照数据目录的榜单来清这块盘。",
                "fix": f"du -xh --max-depth=1 '{path}' 2>/dev/null | sort -h | tail -10",
            })
        if m.get("ro") and not m.get("snapshot") and ftype not in RAM_FSTYPES:
            out.append({
                "id": "ro:" + path,
                "severity": SEV_WARN,
                "title": f"{path}（{dev}，{ftype}）现在是只读挂载",
                "detail": "ext4 默认带 errors=remount-ro：内核一旦检测到文件系统错误就把这块盘改成"
                          "只读把自己冻住。此时所有写入报 Read-only file system，而容量和 inode 看着"
                          "一切正常，是最容易看漏的一种坏法。先读内核日志确认是不是 I/O 错误，"
                          "不要直接 remount rw 把问题盖过去。",
                "fix": "dmesg --level=err,warn | tail -50",
            })

    partial = [r["path"] for r in roots if r.get("exists") and r.get("partial")]
    if partial:
        shown = "、".join(partial[:4]) + ("…" if len(partial) > 4 else "")
        out.append({
            "id": "partial",
            "severity": SEV_INFO,
            "title": f"{len(partial)} 个目录没扫完",
            "detail": f"下面这些目录的占用是下界：{shown}。层级深、文件多的目录（node_modules "
                      "这类）本来就不该为了一个体检数字把盘刷满。",
            "fix": "",
        })

    for f in [x for x in big_files if int(x.get("bytes") or 0) >= BIG_FILE_WARN][:3]:
        out.append({
            "id": "bigfile:" + f["path"],
            "severity": SEV_INFO,
            "title": f"单个文件 {human_bytes(int(f['bytes']))}：{f['path']}",
            "detail": "只点出来，不替你判断它还需不需要。确认要清理的话请回到备份或应用页面"
                      "按流程处理，不要直接 rm。",
            "fix": "",
        })
    return out


def _collect() -> dict:
    """同步遍历部分，交给 asyncio.to_thread 执行（别在事件循环里走盘）。"""
    disk = _disk_info()
    roots: list[dict] = []
    big_pool: list[dict] = []
    for spec in _roots():
        rec: dict = {"label": spec["label"], "path": spec["path"], "exists": False,
                     "bytes": 0, "files": 0, "dirs": 0, "errors": 0,
                     "partial": False, "subdirs": [], "note": "", "branch_count": 0}
        p = Path(spec["path"])
        if p.is_dir():
            rec["exists"] = True
            walked = _walk(spec["path"])
            for key in ("bytes", "files", "dirs", "errors", "partial", "subdirs", "branch_count"):
                rec[key] = walked[key]
            big_pool.extend(walked["big_files"])
            if spec["label"] == "面板备份":
                rec["backups"] = _backup_stats(spec["path"])
        else:
            rec["note"] = "路径不存在或不是目录"
        rec["bytes_human"] = human_bytes(rec["bytes"])
        for sd in rec["subdirs"]:
            sd["bytes_human"] = human_bytes(sd["bytes"])
        roots.append(rec)

    # 登记的根会互相包含：DATA_DIR 就落在 FILE_ROOTS 的 /root/www 里面。逐行数字都各自
    # 是对的，把它们直接相加却会让同一份数据在"合计"里出现两次——那一行是这一页的标题
    # 数字，宁可少算也不能重算，所以给下级根挂上 nested_in，合计只算最外层。
    listed = [(r["path"], r) for r in roots if r.get("exists")]
    for rec in roots:
        if not rec.get("exists"):
            rec["nested_in"] = None
            continue
        mine = os.path.normpath(rec["path"])
        rec["nested_in"] = next(
            (other for other, r in listed
             if r["path"] != rec["path"] and mine.startswith(os.path.normpath(other) + os.sep)),
            None,
        )

    big_files: list[dict] = []
    seen: set[str] = set()
    for bf in sorted(big_pool, key=lambda b: -int(b["bytes"])):
        if bf["path"] in seen:
            continue
        seen.add(bf["path"])
        bf["bytes_human"] = human_bytes(int(bf["bytes"]))
        big_files.append(bf)
    return {"roots": roots, "disk": disk, "big_files": big_files[:TOP_FILES], "mounts": _mounts()}


async def _journal_bytes() -> tuple[int | None, str]:
    # `--disk-usage` 只 stat journal 目录，不扫日志、也不找 journald 说话，所以它是探针而不是
    # 预算另立门面的扫描命令（memory_service 的 `-k -b -n 3000`、login_audit_service 的
    # `--since "24 hours ago"` 才要各自在调用点声明更大的数）。
    r = await run("journalctl", ["--disk-usage"], timeout=PROBE_TIMEOUT)
    text = str(r.get("out") or "")
    if int(r.get("code") or 0) != 0:
        return None, text
    return _parse_journal(text), text


async def audit(refresh: bool = False) -> dict:
    """带 TTL 缓存的对账：自检页反复刷新不该反复走盘（合并规则见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _reprobe, refresh=refresh, ttl=CACHE_TTL)


async def _reprobe() -> dict:
    jb, jraw = await _journal_bytes()
    picked = await asyncio.to_thread(_collect)
    roots, disk = picked["roots"], picked["disk"]
    # 一轮扫描只读一次配置线，判词和载荷用的是同一个值。
    dt = alerts_service.disk_thresholds()
    journal = {
        "bytes": jb,
        "bytes_human": human_bytes(jb) if isinstance(jb, int) else None,
        "raw": jraw[:200],
    }
    findings = _findings(roots, journal, disk, picked["big_files"],
                         picked["mounts"]["items"], thresholds=dt)
    total = sum(int(r.get("bytes") or 0) for r in roots if not r.get("nested_in"))
    counts = {
        "risk": sum(1 for f in findings if f["severity"] == SEV_RISK),
        "warn": sum(1 for f in findings if f["severity"] == SEV_WARN),
        "info": sum(1 for f in findings if f["severity"] == SEV_INFO),
        "bytes_total": total,
        "bytes_total_human": human_bytes(total),
        "roots": len([r for r in roots if r.get("exists")]),
        "mounts": len(picked["mounts"]["items"]),
    }
    return {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "cached": False,
        "disk": disk,
        "mounts": picked["mounts"],
        "journal": journal,
        "roots": roots,
        "big_files": picked["big_files"],
        "findings": findings,
        "counts": counts,
        "notes": list(NOTES),
        # 阈值只有一个出处：要紧线取自设置页那条配置（见 alerts_service.disk_thresholds），
        # 这里只是把它随载荷带出去。过去前端自己写死 90、inode 又写死 90，后端把配置
        # 调到 85 的话，同一条"分区已用 87%"在自检里是要紧项、在页签上是绿灯，
        # 而 inode 那一列的"紧"和判词用的还不是同一条线。
        # 注意要放在这个函数上——/api/storage 返回的是 audit()，放在 _collect() 里前端读不到。
        "tight_pct": dt["warn"],
        "crit_pct": dt["crit"],
        "inode_warn_pct": INODE_WARN_PCT,
        "inode_risk_pct": INODE_RISK_PCT,
    }


def reset_cache() -> None:
    probe.reset(_cache)


def doctor_item(data: dict) -> dict:
    """自检项折法：要紧与值得处理都落 warn，这一项不因"占用大"就报 fail。

    fail 留给真的坏掉的东西（余量那条线在 disk 项上已经有了）。这一项的意义是告诉人
    往哪儿看，而不是把同一件事红两次。
    """
    counts = data.get("counts") or {}
    disk = data.get("disk") or {}
    if not counts.get("roots"):
        return {
            "status": "warn",
            "detail": "存储占用一个目录都没读到（数据目录与登记目录都不存在？）",
            "fix": "choyeonctl storage",
        }
    risky = [f for f in data.get("findings") or [] if f["severity"] in (SEV_RISK, SEV_WARN)]
    used = f"，分区已用 {disk['used_pct']}%" if disk.get("ok") and isinstance(disk.get("used_pct"), int) else ""
    if risky:
        return {
            "status": "warn",
            "detail": tallied([f["title"] for f in risky], " 处占用值得处理", "处") + used,
            "fix": "choyeonctl storage",
        }
    info = int(counts.get("info") or 0)
    detail = (
        f"盘点了 {counts.get('roots')} 个目录，合计 {human_bytes(counts.get('bytes_total'))}，没有异常占用{used}"
    )
    if info:
        detail += f"（另有 {info} 条只是让你知道）"
    return {"status": "pass", "detail": detail, "fix": ""}
