"""主机指标采样：全部在面板进程内完成。

旧实现每 3 秒轮询就 fork 一个 python3 跑 sys_helper.py（进程创建 + 解释器启动是面板
最大的固定 CPU 开销），而它的兜底路径又在事件循环上同步读 /proc。现在 psutil 优先、
/proc 兜底，两者都放线程里执行；顺带把每核占用、磁盘 IO、进程数一起采出来给仪表盘用。
"""
import asyncio
import os
import platform
import re
import shutil
import socket
import time
import weakref
from collections import deque

from app import config

try:  # 缺 psutil 也不能让监控整页挂掉，兜底走 /proc
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

_history: deque = deque(maxlen=60)
_prev: dict | None = None  # 上一次**采样**的累计量，用于算差值（占用率/网速/IO 速率）
_proc_objs: dict[int, object] = {}
_top_cache: tuple[float, list] = (0.0, [])


def _disk_sample() -> tuple[object, str]:
    """磁盘容量按**数据目录所在的那块盘**算，而不是写死根分区。

    面板真正会写满的是 data/（SQLite、备份暂存、上传）。它可能落在独立挂载的盘上，
    那时概览画的 "/" 与「存储占用」页签（storage_service 用 config.DATA_DIR）说的就是两块盘：
    数据盘 100%、根盘 20%，一处报要紧一处报正常。数据目录还没建（新装、被移走）时退回根分区，
    并把**实际读到的那条路径**一起返回，前端显示的就是它——不能换了盘还谎称是同一块。
    """
    path = config.DATA_DIR if os.path.isdir(config.DATA_DIR) else "/"
    return shutil.disk_usage(path), path
_TOP_TTL = 3.0

# psutil 的 cpu_times() 里这两项是「跑嵌套虚拟机花的时间」，内核已经把它计进 user/nice，
# 求总账时必须跳过（见 _cpu_total）。
GUEST_FIELDS = ("guest", "guest_nice")

# 实时采样的墙钟节奏（秒）。旧实现是"每个请求自己 diff 一次全局快照"，而读取者不止一个：
# Dashboard 每 3s 轮询，metrics_store 的后台采样器每 30s 也要调 snapshot()，多开一个标签页
# 就多一个写入者。它们抢同一个 _prev，落在别人之后那一刻 elapsed 只有几毫秒——
# 短分母把网速/IO 放大成尖峰，而 cpu 的 jiffies 差在该窗口内取整为 0，直接算出 0% 并作为
# "真实采样点"永久留在曲线里；_history 也按请求数增长，时间轴随客户端数量被压密。
# 现在到点才真的采样，区间内的调用复用同一份：速率分母恒为采样窗口，曲线口径与客户端数无关。
_RT_INTERVAL = 2.0
_latest: dict | None = None
_latest_at = 0.0  # time.monotonic()，不用墙钟：NTP 回拨会让仪表盘长时间不再更新
# 锁按事件循环存：常驻服务只有一个循环，测试里每个 asyncio.run 一个循环。模块级
# asyncio.Lock 会绑死在第一个循环上，第二次 run 就报 "bound to a different event loop"；
# WeakKeyDictionary 让用完的循环连同它的锁一起回收。
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lk = _locks.get(loop)
    if lk is None:
        lk = _locks[loop] = asyncio.Lock()
    return lk


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _is_whole_disk(name: str) -> bool:
    """这块是不是整盘（而不是分区）。整盘才代表设备真实吞吐，分区加起来会重复计数。

    命名规则各不相同，光看"末尾是不是数字"会把 NVMe/EMMC 的整盘自己滤掉：
    - sda / vda / xvda 是整盘，sda1 / xvda1 是分区；
    - nvme0n1 是整盘（末尾就是数字！），nvme0n1p1 才是分区；
    - mmcblk0 是整盘，mmcblk0p1 才是分区。
    """
    if name.startswith(("nvme", "mmcblk")):
        # 前缀之后的部分形如 `0n1` / `0`；出现 `p<数字>` 尾巴就是分区
        return not re.search(r"p\d+$", name)
    return not name[-1].isdigit()


def _cpu_total(times) -> float:
    """cpu_times 的总账：guest / guest_nice 已经计在 user / nice 里，整行求和会加两遍。

    多算的那两项让分母偏大，占用率（1 - idle/total）在跑嵌套虚拟化的机器上系统性偏低；
    而 `choyeonctl load` 与「负载与 CPU」页签走的是 load_service 的口径（只取前八项），
    同一台机器两块地方会给出两个不同的 CPU%。这里统一到 load_service 那一套。
    """
    return sum(getattr(times, k) for k in times._fields if k not in GUEST_FIELDS)


def _proc_raw() -> dict:
    """无 psutil 时的 /proc 兜底（仍在线程里跑，不碰事件循环）。"""
    stat_lines = [ln for ln in _read("/proc/stat").split("\n") if ln]
    nums = [int(x) for x in stat_lines[0].split()[1:]]
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    # 只取前八项：guest/guest_nice（第 9/10 项）已计在 user/nice 里，加上就重复了。
    total = sum(nums[:8])
    per_core = []
    for ln in stat_lines[1:]:
        if not ln.startswith("cpu"):
            break
        n = [int(x) for x in ln.split()[1:]]
        if not n:
            continue
        per_core.append((n[3] + (n[4] if len(n) > 4 else 0), sum(n[:8])))
    mem = {}
    for line in _read("/proc/meminfo").split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0]) * 1024
    nics: dict[str, tuple[int, int]] = {}
    rx = tx = 0
    for line in _read("/proc/net/dev").split("\n")[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        f = [int(x) for x in rest.split()]
        nics[name] = (f[0], f[8])
        rx += f[0]
        tx += f[8]
    du, disk_path = _disk_sample()
    io_r = io_w = 0
    for line in _read("/proc/diskstats").split("\n"):
        p = line.split()
        if len(p) < 10 or not p[2].startswith(("sd", "nvme", "vd", "xvd", "mmcblk")):
            continue
        if not _is_whole_disk(p[2]):
            continue
        # 字段序号是 kernel 的 Documentation/admin-guide/iostats.rst：
        # 4=读次数 5=读扇区 6=读耗时ms 7=写次数 8=写合并 9=写扇区 10=写耗时ms。
        # 这里要的是**扇区**（×512 才是字节），拿 6/10 就把毫秒当成了数据量。本机实测：
        # sda 的 p[9]*512 = 42775365120，与 psutil.disk_io_counters().write_bytes 逐位相同，
        # 而 p[6]*512 只有 1.5 亿；读侧同理（psutil 的 read_bytes 多出的 748 扇区是光驱 sr0，
        # 面板按 sd/nvme/vd/xvd/mmcblk 计，本来就不把光驱算进磁盘吞吐）。
        io_r += int(p[5]) * 512
        io_w += int(p[9]) * 512
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = [0.0, 0.0, 0.0]
    procs = sum(1 for d in os.listdir("/proc") if d.isdigit())
    return {
        "idle": idle, "total": total, "perCore": per_core,
        "memTotal": mem.get("MemTotal", 0), "memAvailable": mem.get("MemAvailable", 0),
        "rx": rx, "tx": tx, "nics": nics, "nicUp": _proc_nic_up(nics),
        "diskTotal": du.total, "diskUsed": du.used, "diskPath": disk_path,
        "ioRead": io_r, "ioWrite": io_w,
        "load": load, "uptime": _uptime(), "procs": procs, "source": "proc",
    }


def _proc_nic_up(names) -> dict[str, bool]:
    """sysfs 的 operstate。读不到（容器里常如此）时按 up 处理：把未知渲染成"断开"
    会让监控面板谎报网卡故障。"""
    out = {}
    for n in names:
        try:
            out[n] = _read(f"/sys/class/net/{n}/operstate").strip() != "down"
        except OSError:
            out[n] = True
    return out


def _psutil_raw() -> dict:
    ct = psutil.cpu_times()
    idle = ct.idle + getattr(ct, "iowait", 0.0)
    total = _cpu_total(ct)
    per_core = [
        (c.idle + getattr(c, "iowait", 0.0), _cpu_total(c))
        for c in psutil.cpu_times(percpu=True)
    ]
    vm = psutil.virtual_memory()
    nics: dict[str, tuple[int, int]] = {}
    rx = tx = 0
    for name, st in psutil.net_io_counters(pernic=True).items():
        if name == "lo":
            continue
        nics[name] = (st.bytes_recv, st.bytes_sent)
        rx += st.bytes_recv
        tx += st.bytes_sent
    du, disk_path = _disk_sample()
    dio = psutil.disk_io_counters()
    try:
        load = [round(x, 2) for x in psutil.getloadavg()]
    except (AttributeError, OSError):
        load = [0.0, 0.0, 0.0]
    try:
        nic_up = {n: bool(psutil.net_if_stats().get(n).isup) for n in nics}
    except Exception:  # noqa: BLE001 - 链路状态是附加信息，拿不到不该拖累整轮采样
        nic_up = {n: True for n in nics}
    return {
        "idle": idle, "total": total, "perCore": per_core,
        "memTotal": vm.total, "memAvailable": vm.available,
        "rx": rx, "tx": tx, "nics": nics, "nicUp": nic_up,
        "diskTotal": du.total, "diskUsed": du.used, "diskPath": disk_path,
        "ioRead": getattr(dio, "read_bytes", 0) if dio else 0,
        "ioWrite": getattr(dio, "write_bytes", 0) if dio else 0,
        "load": load, "uptime": time.time() - psutil.boot_time(),
        "procs": len(psutil.pids()), "source": "psutil",
    }


def _rate(cur, prev, dt_s: float) -> int:
    """累计字节 → 每秒字节数。计数器回绕（重启/换网卡）时按 0 处理而不是报负数。"""
    if dt_s <= 0:
        return 0
    delta = cur - prev
    return max(0, round(delta / dt_s)) if delta >= 0 else 0


async def snapshot() -> dict:
    """新鲜度由墙钟节奏决定，不由请求节奏决定（见 _RT_INTERVAL 注释）。"""
    global _latest, _latest_at
    async with _lock():
        if _latest is None or time.monotonic() - _latest_at >= _RT_INTERVAL:
            _latest = await _sample()
            _latest_at = time.monotonic()
        return _latest


async def _sample() -> dict:
    global _prev
    now_ms = int(time.time() * 1000)
    mono = time.monotonic()
    raw = await asyncio.to_thread(_psutil_raw if psutil else _proc_raw)

    cpu = 0.0
    per_core = [0.0] * len(raw["perCore"])
    net_rx = net_tx = io_r = io_w = 0
    elapsed = 0.0
    if _prev:
        dt = raw["total"] - _prev["total"]
        di = raw["idle"] - _prev["idle"]
        if dt > 0:
            cpu = max(0.0, round((1 - di / dt) * 1000) / 10)
        prev_core = _prev["perCore"]
        for i, (idl, tot) in enumerate(raw["perCore"]):
            if i < len(prev_core):
                pdt = tot - prev_core[i][1]
                if pdt > 0:
                    per_core[i] = max(0.0, round((1 - (idl - prev_core[i][0]) / pdt) * 1000) / 10)
        elapsed = mono - _prev["mono"]
        net_rx = _rate(raw["rx"], _prev["rx"], elapsed)
        net_tx = _rate(raw["tx"], _prev["tx"], elapsed)
        io_r = _rate(raw["ioRead"], _prev["ioRead"], elapsed)
        io_w = _rate(raw["ioWrite"], _prev["ioWrite"], elapsed)
    # 每网卡明细：首帧没有分母，速率记 0 而不是把开机累计量当成瞬时值。
    prev_nics = (_prev or {}).get("nics") or {}
    nic_rows = []
    for name, (c_rx, c_tx) in sorted(raw["nics"].items()):
        p = prev_nics.get(name)
        nic_rows.append({
            "name": name,
            "rx": _rate(c_rx, p[0], elapsed) if p else 0,
            "tx": _rate(c_tx, p[1], elapsed) if p else 0,
            "totalRx": c_rx,
            "totalTx": c_tx,
            "up": bool(raw["nicUp"].get(name, True)),
        })
    _prev = {
        "idle": raw["idle"], "total": raw["total"], "perCore": raw["perCore"],
        "rx": raw["rx"], "tx": raw["tx"], "ioRead": raw["ioRead"], "ioWrite": raw["ioWrite"],
        "nics": raw["nics"],
        "mono": mono,
        "at": now_ms,
    }

    mem_used = raw["memTotal"] - raw["memAvailable"]
    point = {
        "t": now_ms, "cpu": cpu, "memUsed": mem_used, "memTotal": raw["memTotal"],
        "netRx": net_rx, "netTx": net_tx,
    }
    _history.append(point)

    return {
        "cpu": cpu,
        "cpuPerCore": per_core,
        "cpuCores": len(per_core) or (os.cpu_count() or 0),
        "memUsed": mem_used,
        "memTotal": raw["memTotal"],
        "diskUsed": raw["diskUsed"],
        "diskTotal": raw["diskTotal"],
        "diskPath": raw.get("diskPath"),
        "netRx": net_rx,
        "netTx": net_tx,
        "netTotalRx": raw["rx"],
        "netTotalTx": raw["tx"],
        "nics": nic_rows,
        "ioRead": io_r,
        "ioWrite": io_w,
        "procs": raw["procs"],
        "load": raw["load"],
        "uptime": raw["uptime"],
        "source": raw["source"],
        "history": list(_history),
    }


def _os_pretty() -> str | None:
    try:
        for line in _read("/etc/os-release").split("\n"):
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        return None
    return None


def _cpu_model() -> str:
    try:
        for line in _read("/proc/cpuinfo").split("\n"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


async def info() -> dict:
    """主机信息。原来为拿一个 PRETTY_NAME 也要 fork 解释器，这里改成一次文件读取。"""
    core = await asyncio.to_thread(lambda: (_os_pretty(), _cpu_model(), _uptime()))
    pretty, model, up = core
    return {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpuModel": model,
        "cpuCores": (psutil.cpu_count(logical=True) if psutil else os.cpu_count()) or 0,
        "node": f"python {platform.python_version()}",
        "uptime": up,
        "prettyName": pretty,
    }


def _uptime() -> float:
    if psutil:
        return time.time() - psutil.boot_time()
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        return time.time() - ps_boot_time()


def ps_boot_time() -> float:
    return float(os.stat("/proc/1" if os.path.exists("/proc/1") else "/").st_mtime)


def _top_sync(limit: int) -> list:
    """按 CPU、内存排序的前 N 个进程。

    psutil 的 cpu_percent 是**两次调用之间的增量**，所以要跨请求留住 Process 对象；
    第一次见到某个 pid 时它是 0，这是 psutil 的固有语义，不是 bug。
    """
    if psutil is None:
        return []
    ncpu = os.cpu_count() or 1
    rows = []
    alive = set()
    for proc in psutil.process_iter():
        try:
            pid = proc.pid
            alive.add(pid)
            obj = _proc_objs.get(pid)
            if obj is not None and obj.create_time() == proc.create_time():
                cpu = obj.cpu_percent(None)
            else:
                _proc_objs[pid] = proc
                cpu = proc.cpu_percent(None)
            info = proc.as_dict(attrs=["name", "username", "memory_info", "num_threads"])
            rss = info.get("memory_info")
            rows.append({
                "pid": pid,
                "name": (info.get("name") or "")[:48],
                "user": (info.get("username") or "")[:24],
                # psutil 的单进程 CPU 可以超过 100%（多线程），除以核数才好和整机占用对齐
                "cpu": round(cpu / ncpu, 1),
                "memMb": round(rss.rss / 1048576, 1) if rss else 0.0,
                "threads": info.get("num_threads") or 0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    for pid in list(_proc_objs):
        if pid not in alive:
            del _proc_objs[pid]
    rows.sort(key=lambda r: (r["cpu"], r["memMb"]), reverse=True)
    return rows[:limit]


async def top_processes(limit: int = 12) -> list:
    """带 TTL 缓存：进程表是整表扫描，多个标签页同时轮询时不该各扫一遍。"""
    global _top_cache
    now = time.time()
    if _top_cache[1] and now - _top_cache[0] < _TOP_TTL:
        return _top_cache[1][:limit]
    rows = await asyncio.to_thread(_top_sync, max(1, min(limit, 50)))
    _top_cache = (now, rows)
    return rows[:limit]
