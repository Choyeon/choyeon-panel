"""内存压力与 OOM 对账：在「内存用了多少」之外，说清有没有正在缺内存。

概览的内存仪表回答的是「此刻占了多少」，答不出管理员紧接着要问的三句话：

① 机器有没有因为缺内存而变慢。直接回收（pgscan_direct）、换页（pswpin/pswpout）、PSI
   停顿这三样在总量图上完全隐身——一台「已用 60%」的机器照样可能因为一次突发分配卡住
   几百毫秒，而这串延迟会打在 web 请求、构建和数据库上。
② 内存被谁拿着。按进程给 RSS，并把每个进程归回它的 systemd 单元；面板管理的应用单元直接
   认领回应用名。「node 占了 190MB」没用，「Rosetta 后端（panel-rosetta-backend.service）
   366MB，是这台 3.8GB 机器的 9%」才是能决策的话。
③ 内核有没有已经杀过进程。vmstat 的 oom_kill 是「自启动以来被 OOM 杀掉的次数」，一个整数；
   非 0 才去 kernel 日志取受害者是谁。这类事故最贵：应用莫名其妙重启，而面板只会显示
   「服务又活了」。

严格只读：不 kill 进程、不改 sysctl、不动 unit 的 MemoryMax/MemoryHigh、不重启服务。
「该不该给这个服务加内存上限、加到多少」是容量决策，只有管理员知道业务峰值；面板替它写进
unit 不是自动化，是赌博。处置一律给能复制走的只读命令（systemctl status / journalctl / ps）。

三处如实承认局限，不装成全知：
① 速率只有一个 RATE_WINDOW_SEC 的采样窗口，它是「此刻有没有在抖」，不是历史曲线；历史曲线
   在概览那张内存图上，而那张图只有总量、没有压力。
② PSI 需要内核开 CONFIG_PSI，cgroup 的 memory.*/pids.* 需要 cgroup v2；读不到就整节标
   ok:false 并写明原因，绝不折成 0——0 会被读成「没有问题」。
③ 进程表按 RSS 逐行看，不能相加：共享页在每个进程的 RSS 里都算一遍，前几名之和必大于真实
   占用。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from datetime import UTC, datetime

from .. import aio_db
from ..util import tallied

CACHE_TTL = 90.0
RATE_WINDOW_SEC = 0.8
TOP_PROCS = 12
TOP_UNITS = 12
MAX_OOM_EVENTS = 8
JOURNAL_TIMEOUT = 8
PROC_BUDGET_SEC = 2.0

# 阈值集中在这一处，判词里不再出现裸数字。口径与概览一致：已用 = MemTotal - MemAvailable，
# 也就是「随时能腾出来的那些（页缓存、可回收 slab）不算已用」。free(1) 的 used 含页缓存，
# 会比这里高一截；两个数都自洽，但同一页上必须以这一条为准。
MEM_WARN_PCT = 80
MEM_RISK_PCT = 90
SWAP_WARN_PCT = 50
SWAP_RISK_PCT = 80
SWAP_RATE_WARN = 2 * 1024 * 1024      # 每秒往 swap 写 2MB：回收已经跟不上分配
RECLAIM_PAGES_WARN = 256              # 每秒直接回收 256 页（约 1MB）：分配线程在替内核干活
PSI_SOME_RISK = 5.0                   # 每 10 秒里有 5% 的时间任务因缺内存而阻塞
PSI_SOME_WARN = 1.0                   # 看 300 秒均值，一次突发不算
CAP_WARN_PCT = 80                     # 单元用到了自己声明上限的八成
CAP_RISK_PCT = 95
PID_WARN_PCT = 80                     # pids.current / TasksMax

SEV_RISK = "risk"
SEV_WARN = "warn"
SEV_INFO = "info"
SEV_LABEL = {SEV_RISK: "要紧", SEV_WARN: "值得处理", SEV_INFO: "知道就好"}

NOTES = (
    f"换页、直接回收的速率只采 {RATE_WINDOW_SEC:.1f} 秒窗口，回答的是「此刻有没有在抖」，"
    "不是历史曲线（曲线在概览的内存图上，那里只有总量没有压力）。",
    "已用按 MemTotal - MemAvailable 计：随时能腾出来的页缓存不算已用，所以这里的百分比比 "
    "free(1) 的 used 低一截，与概览同口径。",
    "进程表按 RSS 逐行看，不要相加：共享页在每个进程的 RSS 里都算一遍。",
    "单元表里 .slice 是分组，它的 memory.current 已经含组内所有子单元；把它和自己的子单元"
    "再加一次就重了。面板自己那一行无条件留在表里，哪怕只占几十 MB。",
    "PSI 与单元的 memory.*/pids.* 依赖内核（PSI 需 CONFIG_PSI，cgroup 需 v2）；读不到的节"
    "会写明原因，不会折成 0。",
    "严格只读：不 kill 进程、不改 sysctl、不动 unit 的 MemoryMax/MemoryHigh。上限该设多少"
    "只有管理员知道业务峰值。",
)

_OOM_KILL_RE = re.compile(r"Killed process (\d+) \(([^)]*)\)")
_OOM_TASK_RE = re.compile(r"task:([^,]+),pid=(\d+)")
_UNIT_SUFFIXES = (".service", ".scope", ".slice")

try:
    _PAGE = int(os.sysconf("SC_PAGE_SIZE")) or 4096
except (ValueError, OSError):
    _PAGE = 4096


def _human(n) -> str:
    """字节数折成人话。与存储占用那一页同一套单位，两页对照时不会看错一个数量级。"""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    if v < 0:
        v = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return "—"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _num(path: str) -> int | None:
    """/sys 上的单值计数器。读不到（不存在、非 root、该控制器没开）给 None；`max` 是"没有上限"，
    不是数字，同样给 None。"""
    text = _read(path).strip()
    if not text or text == "max":
        return None
    try:
        return int(text.split()[0])
    except (TypeError, ValueError):
        return None


def _meminfo(raw: str) -> dict:
    """meminfo 折成 {键: kB}。HugePages_* 是页数不是 kB，混进来会把数字放大三个数量级。"""
    out: dict[str, int] = {}
    for line in (raw or "").splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = rest.split()
        key = key.strip()
        if not fields or key.startswith("Huge"):
            continue
        try:
            out[key] = int(fields[0])
        except ValueError:
            continue
    return out


def _vmstat(raw: str) -> dict:
    out: dict[str, int] = {}
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


def _psi(raw: str) -> dict:
    """/proc/pressure/memory → {"some": {avg10,avg60,avg300,total}, "full": {...}}。

    total 单位是微秒，含义是「自启动以来累计有多少时间因缺内存而阻塞」。5.x 内核没有 full 行，
    缺就缺，不假装它是 0。
    """
    if not (raw or "").strip():
        return {"ok": False,
                "reason": "读不到 /proc/pressure/memory（内核未开 CONFIG_PSI，6.x 之前常没有）"}
    out: dict = {"ok": True}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("some", "full"):
            continue
        kv: dict = {}
        for tok in parts[1:]:
            k, _, v = tok.partition("=")
            try:
                kv[k] = float(v) if "." in v else int(v)
            except ValueError:
                continue
        if kv:
            out[parts[0]] = kv
    if "some" not in out:
        return {"ok": False, "reason": "/proc/pressure/memory 里没有 some 行"}
    return out


def _swaps(raw: str) -> list[dict]:
    """/proc/swaps 的设备表。表头那行不是设备；分区名带路径也只展示不解析。"""
    out = []
    for line in (raw or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5 or not parts[0].startswith("/"):
            continue
        try:
            size, used = int(parts[2]), int(parts[3])
        except ValueError:
            continue
        out.append({
            "name": parts[0], "type": parts[1], "priority": parts[4],
            "total": size * 1024, "used": used * 1024,
            "used_pct": round(used * 100 / size) if size else None,
        })
    return out


def _memory_block(mi: dict, devs: list[dict]) -> dict:
    total = mi.get("MemTotal", 0) * 1024
    if total <= 0:
        return {"ok": False, "reason": "读不到 /proc/meminfo 的 MemTotal"}
    avail = mi.get("MemAvailable", 0) * 1024
    used = max(total - avail, 0)
    swap_total = mi.get("SwapTotal", 0) * 1024
    swap_used = max(swap_total - mi.get("SwapFree", 0) * 1024, 0)
    reclaimable = (mi.get("Cached", 0) + mi.get("Buffers", 0) + mi.get("SReclaimable", 0)) * 1024
    return {
        "ok": True,
        "total": total,
        "used": used,
        "used_pct": round(used * 100 / total),
        "available": avail,
        "available_pct": round(avail * 100 / total),
        "free": mi.get("MemFree", 0) * 1024,
        "anon": mi.get("AnonPages", 0) * 1024,
        "reclaimable": reclaimable,
        "reclaimable_pct": round(reclaimable * 100 / total),
        "dirty": mi.get("Dirty", 0) * 1024,
        "writeback": mi.get("Writeback", 0) * 1024,
        "kernel_stack": mi.get("KernelStack", 0) * 1024,
        "pagetables": mi.get("PageTables", 0) * 1024,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_pct": round(swap_used * 100 / swap_total) if swap_total else None,
        "swap_cached": mi.get("SwapCached", 0) * 1024,
        "commit_limit": mi.get("CommitLimit", 0) * 1024,
        "committed": mi.get("Committed_AS", 0) * 1024,
        "devices": devs,
    }


def _rates(prev: dict, nxt: dict, secs: float) -> dict:
    """两个 vmstat 快照之间折成每秒。

    测不出来给 None 而不是 0：0 会被读成「没有在换页」，而真实情况是「这个窗口没采到」。
    计数器倒退（读到两个不同的 /proc 实例、或机器刚重启过）同样给 None——倒退回填不出负的速率。
    """
    base = {"ok": False, "secs": round(secs, 3), "swap_in": None, "swap_out": None,
            "direct_reclaim": None, "allocstall": None, "majfault": None,
            "oom_kill_delta": None}
    if secs <= 0 or not prev or not nxt:
        return base

    def _delta(key: str) -> int | None:
        a, b = prev.get(key), nxt.get(key)
        if a is None or b is None or b < a:
            return None
        return b - a

    def _per_sec(key: str, scale: int = 1):
        d = _delta(key)
        return None if d is None else round(d * scale / secs)

    # allocstall 按 zone 分成几项，老内核只有其中几个：把所有存在的相加，一个都没有才给 None。
    zones = ("allocstall_dma", "allocstall_dma32", "allocstall_normal",
             "allocstall_movable", "allocstall_device")
    present = [v for v in (_delta(z) for z in zones) if v is not None]
    return {
        "ok": True,
        "secs": round(secs, 3),
        "swap_in": _per_sec("pswpin", _PAGE),
        "swap_out": _per_sec("pswpout", _PAGE),
        "direct_reclaim": _per_sec("pgscan_direct"),
        "allocstall": None if not present else round(sum(present) / secs),
        "majfault": _per_sec("pgmajfault"),
        "oom_kill_delta": _delta("oom_kill"),
    }


def _tunables() -> dict:
    """只读展示的内存策略。面板不改它们，读出来是为了让判词能解释「为什么这样算」。"""
    keys = {
        "swappiness": "/proc/sys/vm/swappiness",
        "overcommit_memory": "/proc/sys/vm/overcommit_memory",
        "overcommit_ratio": "/proc/sys/vm/overcommit_ratio",
        "min_free_kbytes": "/proc/sys/vm/min_free_kbytes",
        "panic_on_oom": "/proc/sys/vm/panic_on_oom",
        "oom_kill_allocating_task": "/proc/sys/vm/oom_kill_allocating_task",
    }
    out = {k: _num(p) for k, p in keys.items()}
    out["ok"] = any(v is not None for v in out.values())
    return out


def _unit_of_cgroup(raw: str) -> tuple[str | None, str]:
    """/proc/<pid>/cgroup → (单元名, 类型)。

    cgroup v2 只有一行 `0::/system.slice/panel-rosetta-backend.service`。取最后一个带已知后缀
    的组件：.service 是单元；.scope 是一次性会话（sshd 的 session-*.scope 就在这里——它不属于
    任何面板应用，但内存是真占了，所以如实标成 scope 而不是丢掉）；.slice 是分组。解析不出
    （v1 的多行形态、根 cgroup）给 (None, ...)，让调用方按「归不到单元」处理。
    """
    line = ""
    for one in (raw or "").splitlines():
        if one.startswith("0::"):
            line = one
            break
    if not line:
        return None, "none"
    leaf = line.partition("::")[2].strip().rstrip("/").rsplit("/", 1)[-1]
    for suf in _UNIT_SUFFIXES:
        if leaf.endswith(suf):
            return leaf, suf[1:]
    if not leaf:
        return None, "root"
    return None, "other"


def _procs(limit: int = TOP_PROCS, budget: float = PROC_BUDGET_SEC) -> dict:
    """按 RSS 取前几名，并把每个进程归回它的 systemd 单元。

    RSS 取 statm 第二字段（常驻页）× 页大小：一次读取就够，而这里要扫全机进程。进程在这一瞬间
    退出是常态（快照式扫描），读不到就跳过，不当成错误；内核线程 statm 全零，被 resident>0 那条
    自然滤掉。超过预算就停并把该页扫不完这件事标出来，不给「已经扫完」的假象。
    """
    try:
        names = os.listdir("/proc")
    except OSError:
        return {"ok": False, "reason": "读不到 /proc", "items": [], "count": 0,
                "scanned": 0, "budget_hit": False}
    start = time.monotonic()
    rows: list[dict] = []
    count = 0
    budget_hit = False
    for entry in names:
        if not entry.isdigit():
            continue
        count += 1
        if time.monotonic() - start > budget:
            budget_hit = True
            break
        pid = entry
        statm = _read(f"/proc/{pid}/statm").split()
        if len(statm) < 2:
            continue
        try:
            resident = int(statm[1])
        except ValueError:
            continue
        if resident <= 0:
            continue
        unit, kind = _unit_of_cgroup(_read(f"/proc/{pid}/cgroup"))
        rows.append({
            "pid": int(pid),
            "comm": (_read(f"/proc/{pid}/comm").strip() or "?")[:64],
            "rss": resident * _PAGE,
            "unit": unit,
            "kind": kind,
            "app": None,
        })
    rows.sort(key=lambda r: -r["rss"])
    return {"ok": True, "items": rows[:limit], "count": count, "scanned": len(rows),
            "budget_hit": budget_hit}


def _cgroup_root() -> str | None:
    """cgroup v2 的统一挂载点。

    v1 上内存计数在 /sys/fs/cgroup/memory 且没有 memory.events，形态完全不同。这里不做兼容
    猜测，直接返回 None 让这一节如实缺席——把 v1 的字段当成 v2 来读，会得到一张看着正常、其实
    什么都不代表的表。
    """
    if os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return "/sys/fs/cgroup"
    return None


def _events(path: str) -> dict:
    """memory.events：每行「名字 计数」。oom_kill / max 非零意味着这个单元被自己的上限处理过。"""
    out: dict[str, int] = {}
    for line in _read(path).splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


def _unit_row(root: str, unit: str, app: str | None, self_unit: str | None) -> dict | None:
    base = f"{root}/system.slice/{unit}"
    cur = _num(f"{base}/memory.current")
    ev = _events(f"{base}/memory.events")
    if cur is None and not ev:
        return None                     # 没在跑、或没有内存统计：列出来只是噪声
    kind = next((s[1:] for s in _UNIT_SUFFIXES if unit.endswith(s)), "other")
    return {
        "unit": unit,
        "app": app,
        "kind": kind,
        "self": unit == self_unit,
        "readable": cur is not None,
        "current": cur,
        "peak": _num(f"{base}/memory.peak"),       # 6.5 之前的内核没有这个文件
        "max": _num(f"{base}/memory.max"),
        "high": _num(f"{base}/memory.high"),
        "swap": _num(f"{base}/memory.swap.current"),
        "pids": _num(f"{base}/pids.current"),
        "pids_max": _num(f"{base}/pids.max"),
        "pct_of_cap": None,
        "pct_of_host": None,
        "pct_of_pids": None,
        "events": {"oom_kill": int(ev.get("oom_kill") or 0), "max": int(ev.get("max") or 0),
                   "high": int(ev.get("high") or 0), "oom": int(ev.get("oom") or 0)},
    }


def _percent(part, whole):
    if not isinstance(part, int) or not isinstance(whole, int) or whole <= 0:
        return None
    return round(part * 100 / whole)


def _units(root: str | None, app_map: dict, self_unit: str | None, top: int = TOP_UNITS) -> dict:
    """每个 systemd 单元当前的内存与线程占用；面板管理的应用单元认领回应用名。

    排在前面的永远是「当前真的占着内存的那几个」，不是按名字排序——这张表的用途是先看见谁在吃。
    面板自己的单元无条件留在表里（哪怕只占 40MB）：unit 里带着自己声明的 MemoryMax，一旦被上限
    杀掉，整个面板就没了，这一行必须常驻。
    """
    if not root:
        return {"ok": False, "reason": "不是 cgroup v2（memory.* 形态不同，面板不做兼容猜测）",
                "items": [], "managed": 0, "seen": 0}
    scan = f"{root}/system.slice"
    try:
        entries = os.listdir(scan)
    except OSError:
        return {"ok": False, "reason": f"读不到 {scan}", "items": [], "managed": 0, "seen": 0}
    units = [e for e in entries if e.endswith(_UNIT_SUFFIXES)]
    if self_unit and self_unit not in units:
        units.append(self_unit)
    rows = []
    for unit in units:
        row = _unit_row(root, unit, app_map.get(unit), self_unit)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: -(r["current"] or 0))
    shown = rows[:top]
    if self_unit and all(r["unit"] != self_unit for r in shown):
        mine = next((r for r in rows if r["unit"] == self_unit), None)
        if mine:
            shown.append(mine)
            shown.sort(key=lambda r: -(r["current"] or 0))
    return {"ok": True, "items": shown, "managed": sum(1 for r in shown if r["app"]),
            "seen": len(units), "omitted": max(len(rows) - len(shown), 0)}


def _oom(vm_now: dict, units: list[dict]) -> dict:
    """OOM 事件。计数读一个整数就够，只有真出过事才起一次 journalctl。

    内核计数（vmstat 的 oom_kill）覆盖全局与 memcg 两种杀法；单元自己 memory.events 里那个
    oom_kill 只说明「这个单元的上限杀过人」。两个都要列，因为处置不同：前者是整机缺内存，
    后者是某个单元的天花板设低了。
    """
    kernel_total = int(vm_now.get("oom_kill") or 0)
    unit_kills = {u["unit"]: int(u["events"].get("oom_kill") or 0) for u in units
                  if u.get("events") and int(u["events"].get("oom_kill") or 0)}
    out = {"kernel_total": kernel_total, "unit_kills": unit_kills,
           "events": [], "source": "counter", "reason": ""}
    if not kernel_total and not unit_kills:
        return out
    cmd = "journalctl -k -b -n 3000 --no-pager -o short-iso"
    try:
        proc = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True,
                              timeout=JOURNAL_TIMEOUT)
        text = proc.stdout or ""
    except (subprocess.TimeoutExpired, OSError) as exc:
        out["source"] = "counter-only"
        out["reason"] = f"取不到 kernel 日志（{type(exc).__name__}），只有次数没有受害者"
        return out
    if not text.strip():
        out["source"] = "counter-only"
        out["reason"] = "计数说杀过进程，但当前 boot 的 kernel 日志里没有 OOM 行（日志可能已轮转）"
        return out
    out["source"] = "journal"
    out["events"] = _oom_events(text)
    return out


def _oom_events(text: str) -> list[dict]:
    """从 kernel 日志里挑 OOM 那几行，尽量还原「杀了谁、在第几秒」。

    两种写法都见过：`Out of memory: Killed process 1234 (node) total-vm:...` 与
    `oom-kill:constraint=...,task:python,pid=1234`。认不出这两种的行只留原文不填名字——
    猜错的受害者比没有受害者更容易把人引到错的方向。
    """
    out = []
    for line in (text or "").splitlines():
        low = line.lower()
        if "out of memory" not in low and "oom-kill" not in low and "oom_reaper" not in low:
            continue
        ev = {"pid": None, "comm": None, "at": (line.split(" ") or [""])[0][:24],
              "line": line[:240]}
        kill = _OOM_KILL_RE.search(line)
        task = _OOM_TASK_RE.search(line)
        if kill:
            ev["pid"], ev["comm"] = int(kill.group(1)), kill.group(2)[:64]
        elif task:
            ev["comm"], ev["pid"] = task.group(1)[:64], int(task.group(2))
        out.append(ev)
    return out[-MAX_OOM_EVENTS:]


def _findings(mem: dict, press: dict, rates: dict, units: dict, oom: dict, tun: dict) -> list[dict]:
    """把数字折成判词：每条都带一句「为什么」和一个能复制的只读动作。"""
    out: list[dict] = []

    def add(fid, sev, title, detail, fix):
        out.append({"id": fid, "severity": sev, "title": title, "detail": detail, "fix": fix})

    # ① 已经杀过人：最贵的一类，放在最前。
    kills = int(oom.get("kernel_total") or 0)
    unit_kills = oom.get("unit_kills") or {}
    if kills or unit_kills:
        victims = "、".join(
            f"{e['comm']}({e['pid']})" for e in (oom.get("events") or []) if e.get("comm"))
        where = "、".join(f"{u} 被自己的上限杀 {n} 次" for u, n in unit_kills.items())
        title = (f"内核 OOM 杀过 {kills} 次进程" if kills
                 else f"{len(unit_kills)} 个单元被内存上限杀过进程")
        detail = "被杀掉的进程不会自己报告，应用侧只表现为莫名其妙重启。"
        if victims:
            detail += f"受害者：{victims}。"
        if where:
            detail += f"单元上限：{where}。"
        if oom.get("reason"):
            detail += f"（{oom['reason']}）"
        detail += "先看是谁在长大，再决定是加内存、给那个服务设上限，还是让它自己少占点。"
        add("oom-kills", SEV_RISK, title, detail,
            "journalctl -k -b | grep -iE 'out of memory|oom-kill' | tail -20")

    # ② 此刻正在缺：PSI 是唯一直接回答「有任务因缺内存而阻塞」的数字。
    some = press.get("some") if press.get("ok") else None
    if some:
        a10 = float(some.get("avg10") or 0)
        a300 = float(some.get("avg300") or 0)
        if a10 >= PSI_SOME_RISK:
            add("psi-active", SEV_RISK, f"内存正在停顿（some avg10 {a10:.1f}%）",
                f"每 10 秒里约 {a10:.0f}% 的时间有任务因为拿不到内存而阻塞。这一步在总量图上"
                "看不出来——已用可能只有 70%，但分配速度跟不上回收速度。",
                "cat /proc/pressure/memory")
        elif a300 >= PSI_SOME_WARN:
            add("psi-active", SEV_WARN, f"近五分钟有内存停顿（some avg300 {a300:.1f}%）",
                "300 秒均值不为零，说明这台机器近期真等过内存，只是眼下不严重。总量图上看不到它，"
                "别等 avg10 冲上来才想起这一页。",
                "grep -E 'pgscan_direct|allocstall|pswpin|pswpout' /proc/vmstat")

    # ③ 换页与直接回收：速率只在窗口里测，None 是没采到，不当成 0。
    if rates.get("ok"):
        swap_out = rates.get("swap_out")
        reclaim = rates.get("direct_reclaim")
        if isinstance(swap_out, int) and swap_out >= SWAP_RATE_WARN:
            add("swapping", SEV_WARN, f"正在往 swap 写（{_human(swap_out)}/秒）",
                f"换出非零说明匿名页已经放不进物理内存。这台机器 swappiness={tun.get('swappiness')}，"
                "写 swap 是内核的主动选择而不是故障；但持续写入意味着被换出的那部分一旦被访问就要"
                "等磁盘，延迟会打到业务上。",
                "cat /proc/swaps")
        if isinstance(reclaim, int) and reclaim >= RECLAIM_PAGES_WARN:
            add("direct-reclaim", SEV_WARN, f"分配线程在做直接回收（{reclaim} 页/秒）",
                "直接回收就是发起分配的线程自己停下来扫描并回收页面，这段时间是它的延迟。PG 和 Node "
                "的请求都会踩到，而 CPU 图上只显示成一小段 system/iowait。",
                "grep -E 'pgscan_direct|allocstall' /proc/vmstat")

    if mem.get("ok"):
        pct = mem.get("used_pct")
        if isinstance(pct, int) and pct >= MEM_RISK_PCT:
            add("mem-used", SEV_RISK, f"内存已用 {pct}%，只剩 {mem['available_pct']}% 能腾出来",
                f"已用 {_human(mem['used'])} / {_human(mem['total'])}，可腾出 "
                f"{_human(mem['available'])}。再往上就是 OOM 那条线。",
                "choyeonctl memory")
        elif isinstance(pct, int) and pct >= MEM_WARN_PCT:
            add("mem-used", SEV_WARN, f"内存已用 {pct}%",
                f"按 MemTotal - MemAvailable 口径到了 {pct}%，页缓存还能腾出 "
                f"{_human(mem['reclaimable'])}。留头不大，突发分配会先撞上回收。",
                "choyeonctl memory")
        sp = mem.get("swap_pct")
        if isinstance(sp, int) and sp >= SWAP_RISK_PCT:
            add("swap-full", SEV_RISK, f"swap 已用 {sp}%",
                f"换出空间只剩 {_human(max(mem['swap_total'] - mem['swap_used'], 0))}。swap 满了之后，"
                "下一次回收失败就直接走 OOM，而不是继续换页。",
                "cat /proc/swaps")
        elif isinstance(sp, int) and sp >= SWAP_WARN_PCT:
            add("swap-full", SEV_WARN, f"swap 已用 {sp}%（{_human(mem['swap_used'])}）",
                "放进去的不会自己回来：swap 长期高位通常意味着曾经缺过内存，或者有些进程早就该重启了。",
                "choyeonctl memory")
        cl, ca = mem.get("commit_limit"), mem.get("committed")
        if isinstance(cl, int) and cl > 0 and isinstance(ca, int) and ca > cl:
            if tun.get("overcommit_memory") == 2:
                add("commit", SEV_RISK, f"内存承诺超过上限（{_human(ca)} > {_human(cl)}）",
                    "vm.overcommit_memory=2 时内核会直接拒绝新的分配与 fork，表现为服务起不来、"
                    "cron 报 cannot fork，而物理内存可能还很空。",
                    "grep -E 'CommitLimit|Committed_AS' /proc/meminfo")
            else:
                add("commit", SEV_INFO, f"承诺内存已是 CommitLimit 的 {round(ca * 100 / cl)}%",
                    f"Committed_AS {_human(ca)} 超过内核的启发式上限 {_human(cl)}。当前 "
                    f"overcommit_memory={tun.get('overcommit_memory')}，内核不会因此拒绝分配，"
                    "这个数字只是提示：真到用的时候能不能拿到，看物理内存和回收。",
                    "grep -E 'CommitLimit|Committed_AS' /proc/meminfo")

    for row in (units.get("items") or []):
        who = f"{row['app']}（{row['unit']}）" if row.get("app") else row["unit"]
        cap = row.get("pct_of_cap")
        ev = row.get("events") or {}
        path = f"/sys/fs/cgroup/system.slice/{row['unit']}"
        if isinstance(cap, int) and cap >= CAP_RISK_PCT:
            add(f"cap:{row['unit']}", SEV_RISK, f"{who} 用到自己上限的 {cap}%",
                f"memory.current {_human(row['current'])} / MemoryMax {_human(row['max'])}。越过这条线"
                "内核就在这个单元里挑一个杀掉，别的单元不受影响——所以它的重启看起来毫无道理，而整机"
                "内存可能还很空。",
                f"systemctl status {row['unit']}; cat {path}/memory.events")
        elif isinstance(cap, int) and cap >= CAP_WARN_PCT:
            left = max((row["max"] or 0) - (row["current"] or 0), 0)
            add(f"cap:{row['unit']}", SEV_WARN, f"{who} 用到自己上限的 {cap}%",
                f"离 MemoryMax 还剩 {_human(left)}。先读 memory.peak 判断这是稳态还是尖峰，"
                "别照着峰值设上限。",
                f"cat {path}/memory.peak")
        elif int(ev.get("max") or 0):
            add(f"cap-throttled:{row['unit']}", SEV_INFO,
                f"{who} 被自己的内存上限拦过 {ev['max']} 次",
                "memory.events 的 max 计数非零，说明它曾顶到上限并被 throttle。现在没顶到，"
                "不代表这个上限设得合适。",
                f"cat {path}/memory.events")
        ppct = row.get("pct_of_pids")
        if isinstance(ppct, int) and ppct >= PID_WARN_PCT:
            add(f"pids:{row['unit']}", SEV_WARN,
                f"{who} 用满了线程配额（{row['pids']}/{row['pids_max']}）",
                "TasksMax 用满时报的是「资源暂时不可用」（EAGAIN），和内存无关但一样让人以为程序坏了："
                "fork/pthread_create 直接被拒，应用日志里通常只留一行莫名其妙的错误。",
                f"systemctl show {row['unit']} -p TasksMax -p NRestarts")

    return out


def _decorate_units(units: dict, total: int) -> None:
    """把依赖整机内存的百分比统一在这里算：_unit_row 拿不到 MemTotal，两处口径不许分叉。"""
    for row in units.get("items") or []:
        row["pct_of_cap"] = _percent(row.get("current"), row.get("max"))
        row["pct_of_host"] = _percent(row.get("current"), total)
        row["pct_of_pids"] = _percent(row.get("pids"), row.get("pids_max"))


def _counts(findings: list[dict]) -> dict:
    out = {"risk": 0, "warn": 0, "info": 0}
    for f in findings:
        out[f["severity"]] = out.get(f["severity"], 0) + 1
    return out


def _collect(app_map: dict | None = None, window: float = RATE_WINDOW_SEC) -> dict:
    app_map = app_map or {}
    mi = _meminfo(_read("/proc/meminfo"))
    vm_a = _vmstat(_read("/proc/vmstat"))
    secs = 0.0
    if window > 0:
        time.sleep(window)
        secs = window
    vm_b = _vmstat(_read("/proc/vmstat"))
    mem = _memory_block(mi, _swaps(_read("/proc/swaps")))
    press = _psi(_read("/proc/pressure/memory"))
    rates = _rates(vm_a, vm_b, secs)
    tun = _tunables()
    self_unit, _ = _unit_of_cgroup(_read("/proc/self/cgroup"))
    units = _units(_cgroup_root(), app_map, self_unit)
    if units.get("ok") and mem.get("ok"):
        _decorate_units(units, mem["total"])
    procs = _procs()
    for row in procs.get("items") or []:
        row["app"] = app_map.get(row.get("unit"))
    oom = _oom(vm_b, units.get("items") or [])
    findings = _findings(mem, press, rates, units, oom, tun)
    counts = _counts(findings)
    counts["procs"] = procs.get("count", 0)
    counts["units"] = len(units.get("items") or [])
    return {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "cached": False,
        "mem": mem,
        "pressure": press,
        "rates": rates,
        "tunables": tun,
        "units": units,
        "procs": procs,
        "oom": oom,
        "findings": findings,
        "counts": counts,
        "notes": list(NOTES),
        "self_unit": self_unit,
    }


_LOCK = asyncio.Lock()
_cache: dict = {"at": 0.0, "data": None}


async def _unit_map() -> dict:
    """单元名 → 应用名。直接查表，不走 apps_service.list_apps()：那一趟还要 systemctl、nginx 与
    deployments，这一页只需要一个名字归属，不该为它付三次子进程的代价。"""
    try:
        rows = await aio_db.query("SELECT name, unit_override FROM apps ORDER BY name")
    except Exception:  # noqa: BLE001 - 认领不到应用名不影响数字，那一列如实留空
        return {}
    from .apps_service import unit_name
    out = {}
    for r in rows or []:
        try:
            out[unit_name(dict(r))] = r["name"]
        except (KeyError, TypeError):
            continue
    return out


async def audit(refresh: bool = False) -> dict:
    now = time.monotonic()
    if not refresh and _cache["data"] and now - _cache["at"] < CACHE_TTL:
        data = dict(_cache["data"])
        data["cached"] = True
        return data
    async with _LOCK:
        now = time.monotonic()
        if not refresh and _cache["data"] and now - _cache["at"] < CACHE_TTL:
            data = dict(_cache["data"])
            data["cached"] = True
            return data
        app_map = await _unit_map()
        data = await asyncio.to_thread(_collect, app_map)
        _cache["at"] = now
        _cache["data"] = data
        out = dict(data)
        out["cached"] = False
        return out


def reset_cache() -> None:
    _cache["at"] = 0.0
    _cache["data"] = None


def doctor_item(data: dict) -> dict:
    """自检项折法：没出过事最多 warn，真被 OOM 杀过进程就报 fail。

    其它项（disk、login、storage）都是「值得处理」的口气，这一项不同：OOM 杀过人意味着有业务被
    内核中断过，而那件事只有这里会红。面板不替你重启被杀的服务，也不替你改上限，只保证这种事故
    不再只存在于 journal 里。
    """
    mem = data.get("mem") or {}
    if not mem.get("ok"):
        return {"status": "warn",
                "detail": mem.get("reason") or "读不到 /proc/meminfo",
                "fix": "choyeonctl memory"}
    oom = data.get("oom") or {}
    kills = int(oom.get("kernel_total") or 0)
    unit_kills = oom.get("unit_kills") or {}
    head = f"已用 {mem.get('used_pct')}%（{_human(mem.get('available'))} 可腾出）"
    if kills or unit_kills:
        total = kills + sum(int(v) for v in unit_kills.values())
        risky = [f["title"] for f in data.get("findings") or [] if f["severity"] == SEV_RISK]
        detail = f"OOM 杀掉过 {total} 个进程；{head}"
        if risky:
            detail += "。" + tallied(risky, " 项判词")
        return {"status": "fail", "detail": detail, "fix": "choyeonctl memory"}
    risky_f = [f for f in data.get("findings") or [] if f["severity"] in (SEV_RISK, SEV_WARN)]
    if risky_f:
        return {"status": "warn",
                "detail": tallied([f["title"] for f in risky_f], " 项内存压力值得处理") + f"；{head}",
                "fix": "choyeonctl memory"}
    info = int((data.get("counts") or {}).get("info") or 0)
    detail = f"内存没有压力：{head}，没有停顿、换页与 OOM 记录"
    if info:
        detail += f"（另有 {info} 条只是让你知道）"
    return {"status": "pass", "detail": detail, "fix": ""}
