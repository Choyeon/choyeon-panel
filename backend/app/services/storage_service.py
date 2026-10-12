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
   给出的是下界而不是全量；② 只看本机文件系统，远端对象存储、别的挂载盘在视野之外。
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

from .. import config
from ..util import run

CACHE_TTL = 300.0
ROOT_BUDGET_SEC = 2.0
MAX_DEPTH = 4
TOP_FILES = 8
TOP_SUBDIRS = 6
JOURNAL_TIMEOUT = 15

JOURNAL_WARN = 2 * 1024**3
BACKUP_WARN = 4 * 1024**3
BIG_FILE_WARN = 2 * 1024**3
DISK_TIGHT_PCT = 90

SEV_RISK = "risk"
SEV_WARN = "warn"
SEV_INFO = "info"
SEV_LABEL = {SEV_RISK: "要紧", SEV_WARN: "值得处理", SEV_INFO: "知道就好"}

NOTES = (
    f"每个目录最多扫 {ROOT_BUDGET_SEC:.0f} 秒、{MAX_DEPTH} 层，标了「未扫完」的给出的是下界而不是全量。",
    "不跟随符号链接、不跨文件系统计数；备份产物只统计不删除。",
    "只看本机文件系统：远端对象存储和其他挂载盘不在这一页视野里。",
    "登记的根可以互相包含（数据目录就在应用根里面）：逐行各算各的，合计只算最外层那一份。",
)

_GIB = 1024**3
_UNITS = {"bytes": 1, "b": 1, "k": 1024, "m": _GIB // 1024, "g": _GIB, "t": _GIB * 1024, "p": _GIB * 1024 * 1024}
_JOURNAL_RE = re.compile(r"([\d.]+)\s*(bytes|[bkmtgp])i?b?", re.IGNORECASE)

_LOCK = asyncio.Lock()
_cache: dict = {"at": 0.0, "data": None}


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


def _human(n) -> str:
    val = float(max(int(n or 0), 0))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if val < 1024 or unit == "PB":
            return f"{val:.0f}B" if unit == "B" else f"{val:.1f}{unit}"
        val /= 1024
    return f"{val:.1f}PB"


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
        "total_human": _human(u.total),
        "free": u.free,
        "free_human": _human(u.free),
        "used_pct": used_pct,
    }


def _findings(roots: list[dict], journal: dict, disk: dict, big_files: list[dict]) -> list[dict]:
    """把数字折成判词。每条都带一句"为什么"和一个能复制的动作，不写"建议关注"这种空话。"""
    out: list[dict] = []
    jb = journal.get("bytes")
    if isinstance(jb, int) and jb >= JOURNAL_WARN:
        out.append({
            "id": "journal",
            "severity": SEV_WARN,
            "title": f"systemd 日志已占 {_human(jb)}",
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
            "title": f"备份目录累计 {_human(int(backup['bytes']))}",
            "detail": f"共 {bs.get('count', 0)} 份产物，最早 {bs.get('oldest') or '—'}、最新 "
                      f"{bs.get('newest') or '—'}。每个任务各有 keep 名额，超了名额还留着"
                      "一般是任务被停用或改过名字。面板不代删，先核对再动手。",
            "fix": "choyeonctl backup list",
        })

    if disk.get("ok") and isinstance(disk.get("used_pct"), int) and disk["used_pct"] >= DISK_TIGHT_PCT:
        out.append({
            "id": "tight",
            "severity": SEV_RISK,
            "title": f"分区已用 {disk['used_pct']}%，只剩 {_human(int(disk['free']))}",
            "detail": "备份、数据库写入和构建缓存都落在这一块的盘上。接近满的时候最先出事的"
                      "往往是备份，而备份失败不会有任何提示音。",
            "fix": "choyeonctl storage",
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
            "title": f"单个文件 {_human(int(f['bytes']))}：{f['path']}",
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
        rec["bytes_human"] = _human(rec["bytes"])
        for sd in rec["subdirs"]:
            sd["bytes_human"] = _human(sd["bytes"])
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
        bf["bytes_human"] = _human(int(bf["bytes"]))
        big_files.append(bf)
    return {"roots": roots, "disk": disk, "big_files": big_files[:TOP_FILES]}


async def _journal_bytes() -> tuple[int | None, str]:
    r = await run("journalctl", ["--disk-usage"], timeout=JOURNAL_TIMEOUT)
    text = str(r.get("out") or "")
    if int(r.get("code") or 0) != 0:
        return None, text
    return _parse_journal(text), text


async def audit(refresh: bool = False) -> dict:
    """带 TTL 缓存的对账：自检页反复刷新不该反复走盘（同 exposure_service 的理由）。"""

    def cached() -> dict:
        data = dict(_cache["data"])
        data["cached"] = True
        return data

    now = time.monotonic()
    if not refresh and _cache["data"] and now - float(_cache["at"]) < CACHE_TTL:
        return cached()
    async with _LOCK:
        now = time.monotonic()
        if not refresh and _cache["data"] and now - float(_cache["at"]) < CACHE_TTL:
            return cached()
        jb, jraw = await _journal_bytes()
        picked = await asyncio.to_thread(_collect)
        roots, disk = picked["roots"], picked["disk"]
        journal = {
            "bytes": jb,
            "bytes_human": _human(jb) if isinstance(jb, int) else None,
            "raw": jraw[:200],
        }
        findings = _findings(roots, journal, disk, picked["big_files"])
        total = sum(int(r.get("bytes") or 0) for r in roots if not r.get("nested_in"))
        counts = {
            "risk": sum(1 for f in findings if f["severity"] == SEV_RISK),
            "warn": sum(1 for f in findings if f["severity"] == SEV_WARN),
            "info": sum(1 for f in findings if f["severity"] == SEV_INFO),
            "bytes_total": total,
            "bytes_total_human": _human(total),
            "roots": len([r for r in roots if r.get("exists")]),
        }
        data = {
            "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
            "cached": False,
            "disk": disk,
            "journal": journal,
            "roots": roots,
            "big_files": picked["big_files"],
            "findings": findings,
            "counts": counts,
            "notes": list(NOTES),
        }
        _cache["at"] = time.monotonic()
        _cache["data"] = data
        return data


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["data"] = None


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
        more = f"，另有 {len(risky) - 1} 项" if len(risky) > 1 else ""
        return {
            "status": "warn",
            "detail": f"{len(risky)} 处占用值得处理：{risky[0]['title']}{more}{used}",
            "fix": "choyeonctl storage",
        }
    info = int(counts.get("info") or 0)
    detail = (
        f"盘点了 {counts.get('roots')} 个目录，合计 {_human(counts.get('bytes_total'))}，没有异常占用{used}"
    )
    if info:
        detail += f"（另有 {info} 条只是让你知道）"
    return {"status": "pass", "detail": detail, "fix": ""}
