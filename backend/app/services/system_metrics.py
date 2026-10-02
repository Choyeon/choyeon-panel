import asyncio
import json
import os
import platform
import shutil
import socket
import time
from collections import deque

from .. import config

_history: deque = deque(maxlen=240)
_prev_cpu = None  # (idle, total)
_prev_net = None  # (rx, tx)
_prev_at = 0.0
_python_healthy = platform.system() == "Linux"


async def _python_snapshot() -> dict:
    proc = await asyncio.create_subprocess_exec(
        config.PYTHON, str(config.SYS_HELPER), "snapshot",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    j = json.loads(out)
    if j.get("error"):
        raise RuntimeError(j["error"])
    return {
        "idle": j["idle"], "total": j["total"],
        "memTotal": j["mem"]["total"], "memAvailable": j["mem"]["available"],
        "rx": j["net"]["rx"], "tx": j["net"]["tx"],
        "diskTotal": j["disk"]["total"], "diskUsed": j["disk"]["used"],
        "load": j["load"], "uptime": j["uptime"], "source": "python",
    }


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _node_snapshot() -> dict:
    parts = _read("/proc/stat").split("\n")[0].split()[1:]
    nums = [int(x) for x in parts]
    idle_total = nums[3] + (nums[4] if len(nums) > 4 else 0)
    total = sum(nums)
    mem = {}
    for line in _read("/proc/meminfo").split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0]) * 1024
    rx = tx = 0
    for line in _read("/proc/net/dev").split("\n")[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip() == "lo":
            continue
        f = [int(x) for x in rest.split()]
        rx += f[0]
        tx += f[8]
    du = shutil.disk_usage("/")
    load = [round(x, 2) for x in os.getloadavg()]
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    return {
        "idle": idle_total, "total": total,
        "memTotal": mem.get("MemTotal", 0), "memAvailable": mem.get("MemAvailable", 0),
        "rx": rx, "tx": tx,
        "diskTotal": du.total, "diskUsed": du.used,
        "load": load, "uptime": uptime, "source": "node",
    }


async def _raw() -> dict:
    global _python_healthy
    if _python_healthy:
        try:
            return await _python_snapshot()
        except Exception:
            _python_healthy = False
    return _node_snapshot()


async def snapshot() -> dict:
    global _prev_cpu, _prev_net, _prev_at
    now_ms = int(time.time() * 1000)
    raw = await _raw()

    cpu = 0.0
    if _prev_cpu:
        dt = raw["total"] - _prev_cpu[1]
        di = raw["idle"] - _prev_cpu[0]
        if dt > 0:
            cpu = round((1 - di / dt) * 1000) / 10
    _prev_cpu = (raw["idle"], raw["total"])

    net_rx = net_tx = 0
    if _prev_net and _prev_at:
        dt = (now_ms - _prev_at) / 1000
        if dt > 0:
            net_rx = max(0, round((raw["rx"] - _prev_net[0]) / dt))
            net_tx = max(0, round((raw["tx"] - _prev_net[1]) / dt))
    _prev_net = (raw["rx"], raw["tx"])
    _prev_at = now_ms

    mem_used = raw["memTotal"] - raw["memAvailable"]
    _history.append({"t": now_ms, "cpu": cpu, "memUsed": mem_used, "memTotal": raw["memTotal"], "netRx": net_rx, "netTx": net_tx})

    return {
        "cpu": cpu,
        "memUsed": mem_used,
        "memTotal": raw["memTotal"],
        "diskUsed": raw["diskUsed"],
        "diskTotal": raw["diskTotal"],
        "netRx": net_rx,
        "netTx": net_tx,
        "load": raw["load"],
        "uptime": raw["uptime"],
        "source": raw["source"],
        "history": list(_history),
    }


async def info() -> dict:
    base = {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpuModel": "unknown",
        "cpuCores": os.cpu_count() or 0,
        "node": f"python {platform.python_version()}",
        "uptime": _uptime(),
        "prettyName": None,
    }
    if platform.system() == "Linux":
        try:
            for line in _read("/proc/cpuinfo").split("\n"):
                if line.startswith("model name"):
                    base["cpuModel"] = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    if _python_healthy:
        try:
            proc = await asyncio.create_subprocess_exec(
                config.PYTHON, str(config.SYS_HELPER), "info",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            j = json.loads(out)
            if not j.get("error"):
                base.update(j)
                base["prettyName"] = j.get("prettyName")
        except Exception:
            pass
    return base


def _uptime() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        return time.time() - ps_boot_time()


def ps_boot_time() -> float:
    return float(os.stat("/proc/1" if os.path.exists("/proc/1") else "/").st_mtime)
