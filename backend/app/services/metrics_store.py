"""指标持久化采样：raw 每 30s 一行 + 小时 rollup + 仪表盘分桶查询。

system_metrics.snapshot() 的速率只在内存里、重启即失忆，仪表盘曲线需要历史行。
网络与磁盘 IO 的原始计数器是累计量，这里单独留住上一轮的累计值、逐轮做差得到
每秒速率：计数器回绕（主机重启/换网卡/兜底路径切换）时记 0，既不报负数，也不会
把"自开机累计量"当成一轮的流量 spike；进程内首帧同样记 0。
"""
import asyncio
import math
import time

from .. import database as dbm
from ..aio_db import query as aquery
from ..log import warn
from . import system_metrics

SAMPLE_INTERVAL = 30
MAX_POINTS = 300
RANGE_SECONDS = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 86400}
RANGE_GRAN = {"1h": "raw", "6h": "raw", "24h": "raw", "7d": "hour"}
_GRAN_FLOOR = {"raw": SAMPLE_INTERVAL, "hour": 3600}
_RETENTION = {"rawSeconds": 86400, "hourSeconds": 7 * 86400}

_prev_cum: dict | None = None  # 上一轮采样时刻的累计计数器（rx/tx/ioRead/ioWrite/at）
_beat: float = 0.0  # loop 最近一轮成功时刻，/metrics-status 据此判断采样器是否存活
_rollup_hour: int = -1
_prune_at: float = 0.0


def _read_counters() -> dict:
    """读累计计数器（在线程里执行）。测试通过替换本函数注入伪造计数器。"""
    ps = system_metrics.psutil
    if ps is None:
        raw = system_metrics._proc_raw()
    else:
        rx = tx = 0
        for name, st in ps.net_io_counters(pernic=True).items():
            if name != "lo":
                rx += st.bytes_recv
                tx += st.bytes_sent
        dio = ps.disk_io_counters()
        raw = {
            "rx": rx, "tx": tx,
            "ioRead": getattr(dio, "read_bytes", 0) if dio else 0,
            "ioWrite": getattr(dio, "write_bytes", 0) if dio else 0,
        }
    return {"rx": raw["rx"], "tx": raw["tx"], "ioRead": raw["ioRead"], "ioWrite": raw["ioWrite"]}


def _delta_bps(cur: int, prev: int, dt: int) -> int:
    """累计量差值 → 每秒字节数。dt 非正或计数器回绕时记 0（重置语义见模块头）。"""
    if dt <= 0 or cur < prev:
        return 0
    return round((cur - prev) / dt)


def _insert_raw(row: dict) -> None:
    dbm.execute(
        "INSERT OR REPLACE INTO metrics(ts,gran,cpu,mem_used,mem_total,net_rx,net_tx,"
        "disk_used,disk_total,load1,io_r,io_w,procs) VALUES(?,'raw',?,?,?,?,?,?,?,?,?,?,?)",
        (row["ts"], row["cpu"], row["memUsed"], row["memTotal"], row["netRx"], row["netTx"],
         row["diskUsed"], row["diskTotal"], row["load1"], row["ioR"], row["ioW"], row["procs"]),
    )


async def sample(ts: int | None = None) -> dict:
    """采一轮并落一行 raw。速率相对上一轮 sample 自己算，不依赖 snapshot 的调用节奏。"""
    global _prev_cum
    snap = await system_metrics.snapshot()
    cum = await asyncio.to_thread(_read_counters)
    ts = int(time.time()) if ts is None else int(ts)
    if _prev_cum is None:
        net_rx = net_tx = io_r = io_w = 0  # 重启后首帧没有可靠分母，记 0 而不是开机累计量
    else:
        dt = ts - _prev_cum["at"]
        net_rx = _delta_bps(cum["rx"], _prev_cum["rx"], dt)
        net_tx = _delta_bps(cum["tx"], _prev_cum["tx"], dt)
        io_r = _delta_bps(cum["ioRead"], _prev_cum["ioRead"], dt)
        io_w = _delta_bps(cum["ioWrite"], _prev_cum["ioWrite"], dt)
    _prev_cum = {**cum, "at": ts}
    load = snap.get("load") or [0.0, 0.0, 0.0]
    row = {
        "ts": ts, "cpu": snap.get("cpu"), "memUsed": snap.get("memUsed"), "memTotal": snap.get("memTotal"),
        "netRx": net_rx, "netTx": net_tx,
        "diskUsed": snap.get("diskUsed"), "diskTotal": snap.get("diskTotal"),
        "load1": load[0], "ioR": io_r, "ioW": io_w, "procs": snap.get("procs"),
    }
    await asyncio.to_thread(_insert_raw, row)
    return row


def _avg(rows: list[dict], field: str) -> float | None:
    vals = [r[field] for r in rows if r[field] is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _rollup_sync(hour_ts: int) -> dict | None:
    """一个小时的 raw → 一行 hour。速率列取均值，容量/进程数取该小时最后一行。"""
    rows = dbm.query("SELECT * FROM metrics WHERE gran='raw' AND ts>=? AND ts<? ORDER BY ts",
                     (hour_ts, hour_ts + 3600))
    if not rows:
        # 该小时没有 raw（或已被 prune）：保留既有 hour 行，不能让重跑把已固化的历史删掉
        return None
    last = rows[-1]
    cpus = [r["cpu"] for r in rows if r["cpu"] is not None]
    out = {
        "ts": hour_ts, "cpu": _avg(rows, "cpu"), "maxCpu": max(cpus) if cpus else None,
        "memUsed": _avg(rows, "mem_used"), "memTotal": last["mem_total"],
        "netRx": _avg(rows, "net_rx"), "netTx": _avg(rows, "net_tx"),
        "diskUsed": last["disk_used"], "diskTotal": last["disk_total"],
        "load1": _avg(rows, "load1"), "ioR": _avg(rows, "io_r"), "ioW": _avg(rows, "io_w"),
        "procs": last["procs"],
    }
    dbm.execute(
        "INSERT OR REPLACE INTO metrics(ts,gran,cpu,mem_used,mem_total,net_rx,net_tx,"
        "disk_used,disk_total,load1,io_r,io_w,procs) VALUES(?,'hour',?,?,?,?,?,?,?,?,?,?,?)",
        (out["ts"], out["cpu"], out["memUsed"], out["memTotal"], out["netRx"], out["netTx"],
         out["diskUsed"], out["diskTotal"], out["load1"], out["ioR"], out["ioW"], out["procs"]),
    )
    return out


async def rollup_hour(hour_ts: int | None = None) -> dict | None:
    """把指定小时（默认上一个完整小时）的 raw 聚成一行 hour。PK(gran,ts) 保证重跑即替换。"""
    if hour_ts is None:
        hour_ts = (int(time.time()) // 3600 - 1) * 3600
    hour_ts = int(hour_ts) - int(hour_ts) % 3600
    return await asyncio.to_thread(_rollup_sync, hour_ts)


def _backfill_sync() -> int:
    """进程停机期间错过的小时：raw 还在 24h 保留窗内，启动时补 rollup，7d 曲线不断档。"""
    now = int(time.time())
    since = now - _RETENTION["rawSeconds"]
    have = {r["ts"] for r in dbm.query("SELECT ts FROM metrics WHERE gran='hour' AND ts>=?", (since,))}
    done = 0
    for h in range(since // 3600, now // 3600):
        ts = h * 3600
        if ts in have:
            continue
        if _rollup_sync(ts) is not None:
            done += 1
    return done


def _bucketize(rows: list[dict], bucket: int) -> dict:
    """按 bucket 秒分桶：速率/CPU/负载取均值，容量/进程数取桶内最后一行。"""
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["ts"] // bucket, []).append(r)
    out: dict[str, list] = {k: [] for k in (
        "ts", "cpu", "memUsed", "memTotal", "netRx", "netTx", "diskUsed", "diskTotal", "load1", "ioR", "ioW", "procs")}
    for key in sorted(groups):
        g = groups[key]
        last = g[-1]
        out["ts"].append(key * bucket * 1000)  # echarts 直接用毫秒时间戳
        out["cpu"].append(_avg(g, "cpu"))
        out["memUsed"].append(_avg(g, "mem_used"))
        out["memTotal"].append(last["mem_total"])
        out["netRx"].append(_avg(g, "net_rx"))
        out["netTx"].append(_avg(g, "net_tx"))
        out["diskUsed"].append(last["disk_used"])
        out["diskTotal"].append(last["disk_total"])
        out["load1"].append(_avg(g, "load1"))
        out["ioR"].append(_avg(g, "io_r"))
        out["ioW"].append(_avg(g, "io_w"))
        out["procs"].append(last["procs"])
    return out


async def history(rng: str) -> dict:
    """range → echarts 就绪的等长数组。响应点数恒定不超过 MAX_POINTS。"""
    window = RANGE_SECONDS[rng]
    gran = RANGE_GRAN[rng]
    now = int(time.time())
    rows = await aquery("SELECT * FROM metrics WHERE gran=? AND ts>=? ORDER BY ts", (gran, now - window))
    # +1：桶按 floor(ts/bucket) 对齐，窗口首尾可能各多压出一个部分桶，不加就顶破 300
    bucket = max(math.ceil(window / MAX_POINTS) + 1, _GRAN_FLOOR[gran])
    data = _bucketize(rows, bucket)
    data["range"] = rng
    data["meta"] = {
        "gran": gran, "interval": bucket, "points": len(data["ts"]),
        "window": window, "maxPoints": MAX_POINTS, "from": (now - window) * 1000, "to": now * 1000,
    }
    return data


async def status() -> dict:
    counts = await aquery("SELECT gran, COUNT(*) AS c, MAX(ts) AS last_ts FROM metrics GROUP BY gran")
    per = {r["gran"]: r for r in counts}
    raw_last = (per.get("raw") or {}).get("last_ts")
    return {
        "rows": {"raw": (per.get("raw") or {}).get("c", 0), "hour": (per.get("hour") or {}).get("c", 0)},
        "lastSampleTs": raw_last,
        "lastSampleAgo": (int(time.time()) - raw_last) if raw_last else None,
        "samplerAlive": bool(_beat) and (time.time() - _beat) < SAMPLE_INTERVAL * 3,
        "sampleInterval": SAMPLE_INTERVAL,
        "retention": _RETENTION,
    }


async def loop(interval: float = SAMPLE_INTERVAL) -> None:
    """后台任务入口：asyncio.create_task(metrics_store.loop())。

    单轮失败只记日志——一次采样的偶发异常不能让曲线从此断供；
    取消（服务重启/热重载）从 sleep 处干净退出，并把心跳清零让 status 不再谎报存活。
    """
    global _beat, _rollup_hour, _prune_at
    _rollup_hour = int(time.time()) // 3600
    _prune_at = time.time()
    try:
        await asyncio.to_thread(_backfill_sync)
    except Exception as e:  # noqa: BLE001
        warn("metrics backfill failed:", e)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await sample()
                _beat = time.time()
                hour = int(time.time()) // 3600
                if hour != _rollup_hour:
                    await rollup_hour((hour - 1) * 3600)
                    _rollup_hour = hour
                if time.time() - _prune_at > 6 * 3600:
                    _prune_at = time.time()
                    await asyncio.to_thread(dbm.prune_metrics, _RETENTION["rawSeconds"], _RETENTION["hourSeconds"])
            except Exception as e:  # noqa: BLE001
                warn("metrics sample round failed:", e)
    except asyncio.CancelledError:
        _beat = 0.0
        raise
