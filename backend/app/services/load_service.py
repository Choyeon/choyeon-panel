"""负载与 CPU 归因对账：机器有多忙、这份忙是谁造成的、有没有被自己的配额压住。

概览页的 CPU 曲线、内存压力页、改动未生效页回答的都不是这个问题。load average 是这台机器上最
容易被误读的一个数，而面板此前只画曲线不归因：

① 它是**绝对排队数**，不是百分比。2 核机器上 load 8 意味着平均有 4 个任务在等 CPU；64 核机器
   上 load 8 是半载。同一个数字含义差 32 倍，所以这里所有判据都除以核数，只报 `每核排队`。
② Linux 的 load 里还计入 **D 状态**（不可中断睡眠，实际是在等 IO）的任务。load 高有两类完全
   相反的原因：CPU 不够用，或者磁盘/NFS 跟不上。前者加核有用，后者加核毫无用处——所以必须把
   `procs_blocked`、D 状态名单和 iowait 占比单独摆出来，不能让人拿一条 CPU 曲线去判磁盘问题。
③ 一个**小于 1 的 CPU 配额**可以制造出「机器不忙但活干不完」的第三种状态：进程被限流
   （throttled）时既不在 run queue 也不在 D 状态，load 与曲线都好看，只有 cgroup 的
   `cpu.stat` 里 `nr_throttled` 在涨。这台机器上面板自己就写着 `CPUQuota=80%`（实测
   `cpu.max = 80000 100000`，即 0.8 核），且是整机唯一一个有配额、也唯一一个被限流过的 cgroup。

三处如实承认局限：
① 每个进程的 CPU 增量是**采样窗口内**的差值（默认 1 秒）。`ps` 的 `%CPU` 是进程全生命周期的
   平均值，拿它排「现在谁最忙」会把一个跑了 12 天、此刻闲着的 postgres 后端排到前面。
② 配额与限流只能读 cgroupfs：`systemctl show -p CPUQuota` 在这套 systemd 上不是有效属性名
   （返回空），真值在 `cpu.max` 与 `CPUQuotaPerSecUSec`。cgroup v1、或 unit 没开 CPU 会计时，
   这一节就只报读不到，不猜。
③ 覆盖不到「被限流」以外的调度损失（CPU 亲和、cpuset 隔离、实时优先级抢占），这些要 `mpstat -P ALL`
   与 `schedstat` 才看得见，页面只在处置里给出命令。

严格只读：不改 `CPUQuota=`、不改 `cpu.weight`、不 renice/nice、不 kill 进程、不 restart 任何
服务，也不写任何文件。把配额调高是容量决策（这台机器只有 2 核，配额本来就是拿去保业务进程的），
杀进程和重启服务会直接断业务——所以处置一律是给管理员自己复制走的命令，每条都写明它会不会断流。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime

from .. import probe
from ..severity import SEV_INFO, SEV_RISK, SEV_WARN
from ..util import tallied
from . import systemd_ops

CACHE_TTL = 60.0
SAMPLE_WINDOW_SEC = 1.0
PROC_SCAN_CAP = 4000
SCAN_BUDGET_SEC = 3.0
MAX_BLOCKED = 12
MAX_QUOTA_UNITS = 12

# 判据阈值：全部按「每核」或「百分比」给，写在一处，前端与 CLI 都只反查不重抄。
LOAD_RISK_PER_CORE = 2.0      # 每核排队 2 个：一半以上的时间有人在等
LOAD_WARN_PER_CORE = 1.5
LOAD_LONG_WARN = 1.5          # 15 分钟档也超这个倍率说明不是一下的尖峰
IOWAIT_WARN = 10.0            # %
IOWAIT_RISK = 25.0
STEAL_WARN = 5.0
STEAL_RISK = 20.0
PSI_CPU_WARN = 10.0           # some avg10，单位 %
THROTTLE_WARN = 15.0          # 窗口内被限流的周期占比 %
THROTTLE_RISK = 40.0

CG_ROOT = "/sys/fs/cgroup"
PROC_STAT = "/proc/stat"
PROC_LOADAVG = "/proc/loadavg"
PSI_PATHS = {"cpu": "/proc/pressure/cpu", "io": "/proc/pressure/io"}
# /proc/stat 的 cpu 行字段序（内核 4.x 起就是这个顺序，老内核尾部字段可能缺失）
CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq",
              "steal", "guest", "guest_nice")
# guest / guest_nice 已经计在 user / nice 里（内核文档写明），求总账时要减掉，否则占比会超 100%。
INCLUDED_IN = {"guest": "user", "guest_nice": "nice"}


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().replace(microsecond=0).isoformat()


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _round(v, digits: int = 1):
    if v is None:
        return None
    return round(float(v), digits)


def _kv_ints(text: str, keys: tuple[str, ...] | None = None) -> dict[str, int]:
    """`key value` 形式的内核文件（cpu.stat、pressure 不是这种写法就别走这里）。

    值一律先验数字再用，非数字就跳过那个键，不猜成 0。cpu.max 不是这种写法（`max 100000`
    里的 max 是配额字段而不是键名），它走 _cpu_max 按位置解析。
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) == 2 and p[1].lstrip("-").isdigit() and (keys is None or p[0] in keys):
            out[p[0]] = int(p[1])
    return out


# --------------------------------------------------------------------------- 采样原语
def _stat_cpu() -> dict:
    """/proc/stat 的 cpu 聚合行 + ctxt / processes / procs_running / procs_blocked。"""
    out: dict = {"ticks": {}, "ctxt": None, "forks": None,
                 "procs_running": None, "procs_blocked": None}
    for line in _read(PROC_STAT).splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "cpu" and len(p) >= 5:
            v = [int(x) for x in p[1:]]
            for i, name in enumerate(CPU_FIELDS):
                out["ticks"][name] = v[i] if i < len(v) else 0
        elif p[0] == "ctxt" and len(p) > 1:
            out["ctxt"] = int(p[1])
        elif p[0] == "processes" and len(p) > 1:
            out["forks"] = int(p[1])
        elif p[0] == "procs_running" and len(p) > 1:
            out["procs_running"] = int(p[1])
        elif p[0] == "procs_blocked" and len(p) > 1:
            out["procs_blocked"] = int(p[1])
    if not out["ticks"]:
        out["ok"] = False
        out["reason"] = "读不到 /proc/stat 的 cpu 行"
    else:
        out["ok"] = True
        out["reason"] = None
    return out


def _loadavg() -> dict:
    p = _read(PROC_LOADAVG).split()
    if len(p) < 5:
        return {"ok": False, "reason": "读不到 /proc/loadavg", "one": None,
                "five": None, "fifteen": None, "runnable": None,
                "total_tasks": None, "last_pid": None}
    # 第 4 个字段是「可运行任务数 / 已注册任务总数」，不是「卡在 IO 上的数量」——拿它当
    # procs_blocked 会把整机的线程数（这台机器 227）读成一场 IO 事故。真正的 blocked 只有
    # /proc/stat 里那一个，别处不要造第二个同名数。
    runnable, total_tasks = (p[3].split("/") + ["", ""])[:2]
    return {"ok": True, "reason": None,
            "one": float(p[0]), "five": float(p[1]), "fifteen": float(p[2]),
            "runnable": int(runnable) if runnable.isdigit() else None,
            "total_tasks": int(total_tasks) if total_tasks.isdigit() else None,
            "last_pid": p[4]}


def _psi() -> dict:
    """PSI 的两个控制器。老内核没编译 CONFIG_PSI 时整个文件不存在——那是读不到，不是 0。"""
    out: dict = {"ok": False, "reason": None, "controllers": {}}
    got = 0
    for kind, path in PSI_PATHS.items():
        raw = _read(path)
        if not raw:
            continue
        rows: dict = {}
        for line in raw.splitlines():
            p = line.split()
            if not p:
                continue
            if p[0] in ("some", "full"):
                vals = dict(x.split("=", 1) for x in p[1:] if "=" in x)
                try:
                    rows[p[0]] = {"avg10": float(vals["avg10"]), "avg60": float(vals["avg60"]),
                                  "avg300": float(vals["avg300"]), "total": int(vals["total"])}
                except (KeyError, ValueError):
                    continue
        if rows:
            out["controllers"][kind] = rows
            got += 1
    if got:
        out["ok"] = True
    else:
        out["reason"] = "内核没开 PSI（CONFIG_PSI 未编译），压力指标读不到"
    return out


def _pid_stat(pid: int) -> dict | None:
    """取一个 pid 的 state / utime+stime / starttime / comm。

    comm 可以带空格和括号，所以按最后一个 `)` 切，而不是 split()。
    """
    raw = _read(f"/proc/{pid}/stat")
    if not raw or "(" not in raw or ")" not in raw:
        return None
    comm = raw[raw.index("(") + 1:raw.rindex(")")]
    rest = raw[raw.rindex(")") + 1:].split()
    if len(rest) < 20:
        return None
    # rest[0] 是 state（man proc 的第 3 个字段），utime/stime 是第 14/15 个字段 → 这里 11/12
    try:
        cpu_ticks = int(rest[11]) + int(rest[12])
        starttime = int(rest[19])
    except (IndexError, ValueError):
        return None
    return {"comm": comm, "state": rest[0], "cpu_ticks": cpu_ticks, "starttime": starttime}


def _unit_of(pid: int) -> str:
    """这进程落在哪个 cgroup；解析规则在 systemd_ops，这里只管本站的标签口径。

    scope 留着（init.scope、session-4618.scope 说明这进程不归任何 .service 管，可 CPU 是真花了），
    裸 slice 与解析不出给空串。
    """
    name, kind = systemd_ops.unit_of_cgroup(_read(f"/proc/{pid}/cgroup"))
    return name if kind in ("service", "scope") else ""


def _wchan(pid: int) -> str:
    raw = _read(f"/proc/{pid}/wchan").strip()
    if raw and raw != "0":
        return raw
    sc = _read(f"/proc/{pid}/syscall").split()
    if sc and sc[0] == "running":
        return "running"
    return ""


def _scan_procs(hz: int, deadline: float) -> dict:
    """两次读 /proc 求每个进程在窗口内的 CPU 增量，同时给出状态计数与 D 状态名单。"""
    pids = []
    try:
        pids = [int(x) for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        return {"ok": False, "reason": "读不到 /proc", "scanned": 0, "budget_hit": False}
    first: dict[int, dict] = {}
    scanned = 0
    hit_budget = False
    for pid in pids[:PROC_SCAN_CAP]:
        if time.monotonic() > deadline:
            hit_budget = True
            break
        st = _pid_stat(pid)
        if st is None:
            continue
        first[pid] = st
        scanned += 1
    time.sleep(SAMPLE_WINDOW_SEC)
    states: dict[str, int] = {}
    rows: list[dict] = []
    blocked: list[dict] = []
    seen2 = 0
    for pid, prev in first.items():
        if time.monotonic() > deadline + SAMPLE_WINDOW_SEC:
            hit_budget = True
            break
        cur = _pid_stat(pid)
        if cur is None:
            continue          # 进程在窗口里退出了，不报
        seen2 += 1
        st = cur["state"][:1]
        states[st] = states.get(st, 0) + 1
        delta_ms = max(0, cur["cpu_ticks"] - prev["cpu_ticks"]) * 1000 // max(1, hz)
        cpu_ms = cur["cpu_ticks"] * 1000 // max(1, hz)
        rec = {"pid": pid, "comm": cur["comm"], "state": st,
               "cpu_ms": delta_ms, "cpu_total_ms": cpu_ms, "unit": _unit_of(pid)}
        rows.append(rec)
        if st in ("D", "R"):
            rec2 = dict(rec)
            rec2["wchan"] = _wchan(pid)
            blocked.append(rec2)
    rows.sort(key=lambda r: r["cpu_ms"], reverse=True)
    blocked.sort(key=lambda r: (r["state"] != "D", -r["cpu_ms"]))
    # 只列窗口内真的消耗过 CPU 的进程：一台 2 核机器空闲时全是 0ms 的行，
    # 拿它们占满表格等于让人去查十几个 kworker。
    active = [r for r in rows if r["cpu_ms"] > 0]
    return {"ok": True, "reason": None, "scanned": seen2, "budget_hit": hit_budget,
            "states": states, "busy": active[:MAX_BLOCKED * 2], "busy_total": len(active),
            "blocked": blocked[:MAX_BLOCKED], "blocked_total": len(blocked),
            "window_sec": SAMPLE_WINDOW_SEC}


# --------------------------------------------------------------------------- 配额与限流
def _cpu_max(path: str) -> tuple[int | None, int | None]:
    """cpu.max 的 `quota period`；`max` 表示没有配额上限。"""
    p = _read(path).split()
    if len(p) < 2:
        return None, None
    quota = None if p[0] == "max" else int(p[0])
    try:
        return quota, int(p[1])
    except ValueError:
        return quota, None


def _cgroup_dirs() -> list[str]:
    """带 cpu.max 的 cgroup 目录（限深两层，260 个 cgroup 实测一遍不到 20 毫秒）。"""
    out: list[str] = []
    for root, dirs, _files in os.walk(CG_ROOT):
        rel = os.path.relpath(root, CG_ROOT)
        if rel != "." and rel.count(os.sep) >= 2:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if os.path.exists(os.path.join(root, "cpu.max")):
            out.append(root)
    return out


def _under_unit(path: str, unit: str) -> bool:
    """这个 cgroup 目录是否落在 unit 的子树里。

    旧写法是 `tail.startswith(self_unit)`，于是 choyeon-panel-worker.service 这种"名字以面板开头
    的兄弟单元"会被标成面板自己，跟着给出改面板 CPUQuota 的处置建议。unit 目录下再开的子组
    （比如面板自己 fork 出来的 scope）仍然算。
    """
    rel = os.path.relpath(path, CG_ROOT)
    if rel.startswith(".."):
        return False
    return unit in [seg for seg in rel.split(os.sep) if seg not in ("", ".")]


def _quota_scan(self_unit: str, deadline: float) -> dict:
    """每个有限额或被限流过的 cgroup，给出配额核数与窗口内的 throttle 增量。"""
    dirs = _cgroup_dirs()
    snap: dict[str, dict] = {}
    for d in dirs:
        quota, period = _cpu_max(os.path.join(d, "cpu.max"))
        st = _kv_ints(_read(os.path.join(d, "cpu.stat")))
        if quota is None and not st.get("nr_throttled"):
            continue          # 没配额又没被限流过，不必列
        weight = None
        wraw = _read(os.path.join(d, "cpu.weight")).strip()
        if wraw.isdigit():
            weight = int(wraw)
        snap[d] = {"quota_us": quota, "period_us": period, "weight": weight,
                   "nr_periods": st.get("nr_periods"), "nr_throttled": st.get("nr_throttled"),
                   "throttled_usec": st.get("throttled_usec"), "usage_usec": st.get("usage_usec")}
    # 窗口就是 _scan_procs 睡的那 1 秒：首次快照在它之前取，第二次读在它之后，无需再 sleep。
    return {"ok": True, "reason": None, "dirs": len(dirs), "first": snap,
            "self_unit": self_unit, "budget_hit": time.monotonic() > deadline}


def _quota_second_pass(first: dict, self_unit: str) -> list[dict]:
    """窗口结束后再读一次，算出「这一秒里有多少周期被限流」。"""
    rows = []
    for d, prev in first.items():
        cur = _kv_ints(_read(os.path.join(d, "cpu.stat")),
                       ("nr_periods", "nr_throttled", "throttled_usec", "usage_usec"))
        per = prev.get("period_us") or 100000
        quota = prev.get("quota_us")
        d_per = max(0, cur.get("nr_periods", 0) - (prev.get("nr_periods") or 0))
        d_thr = max(0, cur.get("nr_throttled", 0) - (prev.get("nr_throttled") or 0))
        d_us = max(0, cur.get("throttled_usec", 0) - (prev.get("throttled_usec") or 0))
        tail = os.path.basename(d)
        rows.append({
            "unit": tail,
            "path": d,
            "self": _under_unit(d, self_unit),
            "capped": quota is not None,
            "quota_cores": _round(quota / per, 2) if quota else None,
            "period_us": per,
            "weight": prev.get("weight"),
            "total_periods": prev.get("nr_periods"),
            "total_throttled": prev.get("nr_throttled"),
            "total_throttled_usec": prev.get("throttled_usec"),
            "window_periods": d_per,
            "window_throttled": d_thr,
            "window_throttled_usec": d_us,
            "window_pct": _round(100.0 * d_thr / d_per, 1) if d_per else None,
            "usage_usec": cur.get("usage_usec"),
            "window_usage_usec": max(0, (cur.get("usage_usec") or 0) - (prev.get("usage_usec") or 0)),
        })
    rows.sort(key=lambda r: (-(r["window_pct"] or -1), -(r["total_throttled"] or 0)))
    return rows


# --------------------------------------------------------------------------- 判词
def _split_pct(ticks: dict) -> dict:
    """把 /proc/stat 的 tick 差值折成百分比；guest 已含在 user 里，不参与总账。"""
    keep = {k: float(v) for k, v in ticks.items() if k not in INCLUDED_IN}
    total = sum(keep.values())
    if total <= 0:
        return {}
    return {k: _round(100.0 * v / total, 1) for k, v in keep.items()}


def _findings(load: dict, cpu: dict, psi: dict, procs: dict,
              quota: dict, cores: int) -> list[dict]:
    split = cpu.get("pct") or {}
    blocked_now = int(cpu.get("procs_blocked") or 0)
    running_now = int(cpu.get("procs_running") or 0)
    out: list[dict] = []
    per_core = None
    if load.get("ok") and cores:
        per_core = _round(load["one"] / cores, 2)
        long_per = _round((load["fifteen"] or 0) / cores, 2)
        blk = blocked_now
        if per_core >= LOAD_RISK_PER_CORE and blk:
            d_pids = [str(b["pid"]) for b in (procs.get("blocked") or []) if b["state"] == "D"][:5]
            io_cmd = ("pidstat -d 1 5 -p " + ",".join(d_pids)) if d_pids else "pidstat -d 1 5"
            out.append({"id": "load-io", "severity": SEV_RISK,
                        "title": f"每核排队 {per_core}（整机 load {load['one']}，{cores} 核），"
                                 f"且 {blk} 个任务正卡在等 IO",
                        "detail": "Linux 的 load 把 D 状态（等 IO、不可中断）也算进去，所以这个数高不一定是 CPU 不够。"
                                  "这时候加核、改配额都没用，要看是谁在等哪块盘。"
                                  + (f"这一秒里 iowait 占 {split.get('iowait')}%。" if split.get("iowait") else ""),
                        "fix": io_cmd + "   # 看哪几个进程在读写在堵；再看设备本身：sar -d 1 3"})
        elif per_core >= LOAD_RISK_PER_CORE:
            out.append({"id": "load-cpu", "severity": SEV_RISK,
                        "title": f"每核排队 {per_core}（整机 load {load['one']}，{cores} 核）",
                        "detail": "平均下来每个核外头还排着一个以上，任务在等 CPU。"
                                  f"这一瞬间 {running_now} 个任务在 run queue，"
                                  f"整机 {load.get('total_tasks')} 个任务里多数在睡。",
                        "fix": f"mpstat -P ALL 1 5 && pidstat 1 5   # {cores} 核，先定是哪几个进程"})
        elif per_core >= LOAD_WARN_PER_CORE or long_per >= LOAD_LONG_WARN:
            warm = "没到要紧的程度，但持续在这个水位上排队会变长。"
            if long_per >= LOAD_LONG_WARN:
                warm += "15 分钟档也超阈值，说明不是一下就过去的尖峰。"
            out.append({"id": "load-warm", "severity": SEV_WARN,
                        "title": f"每核排队 {per_core}（15 分钟档 {long_per}）已经偏高",
                        "detail": warm,
                        "fix": "pidstat 1 5 && uptime   # 先看是谁在占，再决定限谁"})

    if procs.get("blocked_total"):
        ds = [b for b in procs.get("blocked") or [] if b["state"] == "D"]
        if ds:
            names = "、".join(f"{b['comm']}({b['pid']})" for b in ds[:3])
            out.append({"id": "blocked-d", "severity": SEV_WARN if len(ds) < 5 else SEV_RISK,
                        "title": f"{len(ds)} 个任务正卡在 D 状态等 IO：{names}",
                        "detail": "D 状态的进程 kill 不掉（信号要等它从内核里回来），"
                                  "它同时把 load 顶高。窗口内它们各消耗了 "
                                  + "、".join(f"{b['cpu_ms']}ms" for b in ds[:3]) + " CPU。"
                                  + (f" 所属单元：{ds[0]['unit']}" if ds[0].get("unit") else ""),
                        "fix": f"pidstat -d 1 5 -p {','.join(str(b['pid']) for b in ds[:5])}"
                               f" && cat /proc/{ds[0]['pid']}/stack   # 只读，看它堵在哪个内核调用"})

    iowait = split.get("iowait")
    if iowait is not None and iowait >= IOWAIT_RISK:
        out.append({"id": "iowait", "severity": SEV_RISK,
                    "title": f"这一秒里 {iowait}% 的 CPU 时间在等 IO",
                    "detail": "CPU 大部分空着但在等盘。这类负载要看设备队列与文件系统，"
                              "和核数、配额无关。",
                    "fix": "sar -d 1 3 && iostat -x 1 3   # 看 %util 与 await 落在哪块设备"})
    elif iowait is not None and iowait >= IOWAIT_WARN:
        out.append({"id": "iowait-warm", "severity": SEV_WARN,
                    "title": f"这一秒里 {iowait}% 的 CPU 时间在等 IO",
                    "detail": "还没到压垮调度的程度，但 IO 已经是排队的一部分原因。",
                    "fix": "sar -d 1 3"})

    steal = split.get("steal")
    if steal is not None and steal >= STEAL_RISK:
        out.append({"id": "steal", "severity": SEV_RISK,
                    "title": f"{steal}% 的时间被宿主机抢走（steal）",
                    "detail": "这台机器是虚拟机，guest 自己要不到核：面板里所有判据（load、配额、"
                              "限流）都会被这个噪声污染。问题不在机器内部。",
                    "fix": "mpstat 1 5 && grep '^cpu ' /proc/stat   # 记下来找 VPS 商家或换规格"})
    elif steal is not None and steal >= STEAL_WARN:
        out.append({"id": "steal-warm", "severity": SEV_WARN,
                    "title": f"{steal}% 的时间被宿主机抢走（steal）",
                    "detail": "虚拟化层的干扰，本机配置改不动它；排查时要把这段损失从自己的负载里分开。",
                    "fix": "mpstat 1 5"})

    psi_cpu = (psi.get("controllers") or {}).get("cpu") or {}
    some10 = (psi_cpu.get("some") or {}).get("avg10")
    if some10 is not None and some10 >= PSI_CPU_WARN:
        out.append({"id": "psi-cpu", "severity": SEV_WARN,
                    "title": f"PSI cpu some avg10 = {some10}%：这一分钟里有人在等 CPU",
                    "detail": "PSI 直接量「因为缺 CPU 而停滞的时间比例」，比 load 更能区分"
                              "「忙」和「挤」。io 那一节的数字在同一个文件里可以对照。",
                    "fix": "cat /proc/pressure/cpu /proc/pressure/io"})

    throttled = [r for r in quota.get("units") or [] if (r.get("window_pct") or 0) >= THROTTLE_WARN]
    for r in throttled:
        pct = r["window_pct"]
        sev = SEV_RISK if pct >= THROTTLE_RISK else SEV_WARN
        who = "面板自己" if r["self"] else r["unit"]
        if r["self"]:
            own = quota.get("self_unit") or systemd_ops.PANEL_UNIT
            stem = own[: -len(".service")] if own.endswith(".service") else own
            fix = (f"systemctl show {stem} -p CPUQuotaPerSecUSec   "
                   f"# 配额写在 deploy/{own} 的 CPUQuota=，"
                   "改完用 choyeonctl upgrade --no-pull 重渲染（不要手工编辑 /etc/systemd 下那份）")
        else:
            fix = (f"systemctl show {r['unit']} -p CPUQuotaPerSecUSec   "
                   "# 要放宽就改这个 unit 自己的 CPUQuota= 再 daemon-reload + restart，"
                   "什么时候能断这批连接只有你知道")
        out.append({"id": f"throttle:{r['unit']}", "severity": sev,
                    "title": f"{who} 被自己的 CPU 配额限流：这一秒 {pct}% 的周期被压住",
                    "detail": f"配额是 {r['quota_cores']} 核（cpu.max 的周期 {r['period_us']} 微秒），"
                              f"窗口内 {r['window_throttled']}/{r['window_periods']} 个周期没跑满，"
                              f"累计已限流 {r['total_throttled']} 次 / "
                              f"{_round((r['total_throttled_usec'] or 0) / 1e6, 2)} 秒。"
                              "被限流的进程既不在 run queue 也不在 D 状态，所以 load 与 CPU 曲线都看不出问题——"
                              "只有 cpu.stat 在涨。",
                    "fix": fix})

    capped = [r for r in quota.get("units") or [] if r.get("capped") and r not in throttled]
    if capped:
        total_capped = quota.get("capped_total", len(capped))
        hidden = total_capped - len(capped)
        names = "、".join(f"{r['unit']}({r['quota_cores']}核)" for r in capped[:4])
        more = f"，另有 {hidden} 个没列出来" if hidden > 0 else ""
        out.append({"id": "quota-list", "severity": SEV_INFO,
                    "title": f"{total_capped} 个单元带着 CPU 配额：{names}{more}",
                    "detail": "这一轮没有限流发生，只是把范围记下来：这些单元就算机器空着也跑不满"
                              "配额以外的核。配额本身是保护（防止一个应用吃光整机），不是缺陷。",
                    "fix": "for d in /sys/fs/cgroup/system.slice/*.service; do "
                           "[ -f $d/cpu.max ] && echo $(basename $d) $(cat $d/cpu.max); done"})

    sections = {"load": (load, "load average", "cat /proc/loadavg"),
                "psi": (psi, "PSI 压力指标", "cat /proc/pressure/cpu   # 内核没开 PSI 时这个文件不存在"),
                "procs": (procs, "进程状态与窗口内 CPU", "ps -eo pid,stat,comm | head"),
                "quota": (quota, "配额与限流",
                          "find /sys/fs/cgroup -maxdepth 3 -name cpu.max | head"),
                "cpu_split": ({"ok": bool(split), "reason": "采样窗口里 /proc/stat 没读到增量"},
                              "CPU 时间分解", "head -2 /proc/stat")}
    for key, (src, name, fix) in sections.items():
        if not src.get("ok"):
            out.append({"id": f"unreadable:{key}", "severity": SEV_INFO,
                        "title": f"{name}：读不到",
                        "detail": (src.get("reason") or "未知原因")
                                  + "。这一节等于空白，别把它读成「没有问题」。",
                        "fix": fix})
    return out


def _counts(findings: list[dict]) -> dict:
    return {"risk": sum(1 for f in findings if f["severity"] == SEV_RISK),
            "warn": sum(1 for f in findings if f["severity"] == SEV_WARN),
            "info": sum(1 for f in findings if f["severity"] == SEV_INFO)}


NOTES = [
    "load average 是「平均有多少任务在排队」的绝对数，不是百分比：所有判据都除以核数，"
    "页面上写的是每核排队。2 核机器 load 4 与 64 核机器 load 4 是两件相反的事。",
    "Linux 的 load 计入 D 状态（不可中断，通常在等 IO）的任务。load 高有两类相反的原因——"
    "CPU 不够与磁盘跟不上，处置方向完全不同，所以这里把 procs_blocked、D 名单、iowait 单独列出。",
    "每个进程的 CPU 数字是这次采样窗口（默认 1 秒）内的增量，不是 ps 那种全生命周期平均值；"
    "后者会把跑了几天而此刻空闲的进程排到最前面。",
    "配额与限流只读 cgroupfs 的 cpu.max / cpu.stat。「systemctl show -p CPUQuota」不是有效属性名"
    "（这台 Debian 13 上返回空），要问的是 CPUQuotaPerSecUSec；真值在 cpu.max（80000 100000 就是 0.8 核）。"
    "cgroup v1 或没开 CPU 会计的 unit 只能标读不到。",
    "被限流（throttled）的进程不出现在 run queue 也不进 D 状态：load 与 CPU 曲线都好看，"
    "只有 cpu.stat 的 nr_throttled 在涨。这就是「机器不忙但活干不完」的第三种情况。",
    "/proc/stat 的 guest / guest_nice 已经计在 user / nice 里，算百分比时剔掉，否则总账超 100%。",
    "「谁在占」只列窗口增量大于 0 的进程：2 核机器空闲时有上百个 0ms 的内核线程，"
    "把它们排进表里只会让人去查十几个 kworker。一个都不列出的时候会明说「这一秒没有进程消耗 CPU」，"
    "不让人把空白读成「采样坏了」。",
    "严格只读：不改 CPUQuota= / cpu.weight、不 renice、不 kill、不 restart 任何服务。"
    "把配额调高是容量决策（这台机器的配额本来就是拿来保业务进程的），杀进程和重启会直接断业务——"
    "处置一律是给你自己复制走的只读命令。",
]


def _collect() -> dict:
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 250
    cores = os.cpu_count() or 0
    deadline = time.monotonic() + SCAN_BUDGET_SEC
    stat_t1 = time.monotonic()
    stat1 = _stat_cpu()
    load = _loadavg()
    psi = _psi()
    self_unit = systemd_ops.panel_unit()
    q1 = _quota_scan(self_unit, deadline)
    procs = _scan_procs(hz, deadline)
    stat2 = _stat_cpu()
    stat_span = max(0.01, time.monotonic() - stat_t1)
    all_units = _quota_second_pass(q1.get("first") or {}, self_unit)
    units = all_units[:MAX_QUOTA_UNITS]

    ticks: dict = {}
    if stat1.get("ok") and stat2.get("ok"):
        ticks = {k: max(0, stat2["ticks"][k] - stat1["ticks"][k]) for k in CPU_FIELDS}
    split = _split_pct(ticks)
    # ctxt / forks 的增量是 stat1→stat2 之间攒下来的，而这一段不只有 1 秒采样窗：中间还夹着
    # 配额扫描和整轮 /proc 扫描。分母必须用真实跨度，拿标称的 1 秒去除会把每秒速率抬高
    # 两三倍（页面还会同时写着「窗口 1 秒」）。进程表自己的窗口是 procs.window_sec，另算。
    elapsed = stat_span
    ctxt_rate = None
    fork_rate = None
    if stat1.get("ctxt") is not None and stat2.get("ctxt") is not None:
        ctxt_rate = int(max(0, stat2["ctxt"] - stat1["ctxt"]) / elapsed)
    if stat1.get("forks") is not None and stat2.get("forks") is not None:
        fork_rate = int(max(0, stat2["forks"] - stat1["forks"]) / elapsed)

    cpu = {"ok": bool(ticks),
           "reason": None if ticks else "采样窗口里 /proc/stat 没读到增量",
           "pct": split, "ctxt_per_sec": ctxt_rate, "forks_per_sec": fork_rate,
           "procs_running": stat2.get("procs_running"),
           "procs_blocked": stat2.get("procs_blocked")}
    quota = {"ok": q1.get("ok"), "reason": q1.get("reason"),
             "dirs": q1.get("dirs"), "units": units, "unit_total": len(all_units),
             "capped_total": sum(1 for r in all_units if r.get("capped")),
             "throttled_total": sum(1 for r in all_units if (r.get("window_pct") or 0) > 0),
             "self_unit": self_unit}
    findings = _findings(load, cpu, psi, procs, quota, cores)
    dead = [k for k, v in (("load", load), ("psi", psi), ("procs", procs),
                           ("quota", quota), ("cpu_split", cpu)) if not v.get("ok")]
    upt = _read("/proc/uptime").split()
    return {
        "as_of": _now_iso(),
        "cached": False,
        "cores": cores,
        "hz": hz,
        "window_sec": round(elapsed, 2),
        "uptime_sec": int(float(upt[0])) if upt else None,
        "load": load,
        "cpu": cpu,
        "psi": psi,
        "procs": procs,
        "quota": quota,
        "unreadable": dead,
        "findings": findings,
        "counts": _counts(findings),
        "notes": list(NOTES),
    }


_cache = probe.new_cache()
_LOCK = asyncio.Lock()


async def audit(refresh: bool = False) -> dict:
    """带 TTL 的探测入口；并发与连点合并成一轮 1 秒采样（见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _reprobe, refresh=refresh, ttl=CACHE_TTL)


async def _reprobe() -> dict:
    return await asyncio.to_thread(_collect)


def reset_cache() -> None:
    probe.reset(_cache)


def doctor_item(data: dict) -> dict:
    """自检折法：要紧项 → fail；值得处理 → warn；读不到关键一节 → warn；否则 pass。

    这一项报 fail 是必要的：每核排队 2 倍以上、或面板自己被配额压住时，面板的取数、
    探活、日志检索会一起变慢甚至超时，那时别的数据源看起来「正常」其实是因为已经没人跑得动了。
    """
    counts = data.get("counts") or {}
    load = data.get("load") or {}
    cores = data.get("cores") or 0
    if not load.get("ok"):
        return {"status": "warn", "detail": load.get("reason") or "读不到 load average",
                "fix": "choyeonctl load"}
    risky = [f for f in data.get("findings") or [] if f["severity"] == SEV_RISK]
    if risky:
        return {"status": "fail",
                "detail": tallied([f["title"] for f in risky], " 项要紧的负载问题"),
                "fix": "choyeonctl load"}
    warns = [f for f in data.get("findings") or [] if f["severity"] == SEV_WARN]
    if warns:
        return {"status": "warn",
                "detail": tallied([f["title"] for f in warns], " 项值得处理"),
                "fix": "choyeonctl load"}
    one = load.get("one")
    per_core = _round(one / cores, 2) if cores and one is not None else None
    detail = (f"负载平稳：load {load.get('one')}/{load.get('five')}/{load.get('fifteen')}，"
              f"{cores} 核（每核排队 {per_core}）")
    if counts.get("info"):
        detail += f"（另有 {counts['info']} 条只是让你知道）"
    if data.get("unreadable"):
        detail += f"；读不到的节：{'、'.join(data['unreadable'])}"
    return {"status": "pass", "detail": detail, "fix": ""}
