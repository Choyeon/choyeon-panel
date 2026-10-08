#!/usr/bin/env python3
"""choyeon-panel system helper.

Collects local server metrics (Linux) and prints a JSON object to stdout.
Subcommands:
  snapshot  raw counters for one sampling tick (cpu jiffies, mem, net, disk, load)
  info      static host information (hostname, distro, kernel, cpu)

Stdlib only; no third-party dependencies. Called by the panel (app/services/system_metrics.py)
via `python3 sys_helper.py <cmd>`; the panel falls back to its own /proc parsing
if this script is unavailable.
"""

import argparse
import json
import os
import platform
import socket
import sys

PROC = "/proc"


def read_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def parse_stat(text: str) -> dict:
    """Parse the first `cpu` line of /proc/stat into idle/total jiffies."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:]]
            while len(parts) < 5:
                parts.append(0)
            idle = parts[3] + parts[4]  # idle + iowait
            return {"idle": idle, "total": sum(parts)}
    raise ValueError("no cpu line in /proc/stat")


def parse_meminfo(text: str) -> dict:
    """Parse /proc/meminfo into bytes for total/available."""
    wanted = {"MemTotal": None, "MemAvailable": None}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        key = key.strip()
        if key in wanted:
            wanted[key] = int(rest.strip().split()[0]) * 1024
    if wanted["MemTotal"] is None:
        raise ValueError("MemTotal not found")
    # Older kernels lack MemAvailable; approximate with MemFree+Buffers+Cached.
    if wanted["MemAvailable"] is None:
        raise ValueError("MemAvailable not found")
    return {"total": wanted["MemTotal"], "available": wanted["MemAvailable"]}


def parse_netdev(text: str) -> dict:
    """Sum rx/tx bytes across non-loopback interfaces of /proc/net/dev."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if not rest.strip() or name.strip() == "lo":
            continue
        fields = rest.split()
        rx += int(fields[0])
        tx += int(fields[8])
    return {"rx": rx, "tx": tx}


def parse_loadavg(text: str) -> list:
    return [round(float(x), 2) for x in text.split()[:3]]


def parse_uptime(text: str) -> float:
    return float(text.split()[0])


def disk_usage(path: str = "/") -> dict:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    # 用 f_bavail（非特权可用）而不是 f_bfree：ext4 默认给 root 保留 5% 块，
    # 用 f_bfree 时仪表盘显示的占用率会低于告警与 doctor 的口径（util.disk_usage_pct），
    # 同一块盘出现两个百分比，"88% 使用"和"未触发告警"看起来互相矛盾。
    avail = st.f_bavail * st.f_frsize
    return {"total": total, "used": total - avail}


def read_os_release() -> dict:
    info = {}
    for path in (f"{PROC}/../etc/os-release", "/etc/os-release"):
        try:
            text = read_file(path)
        except OSError:
            continue
        for line in text.splitlines():
            key, _, value = line.partition("=")
            info[key.strip()] = value.strip().strip('"')
        break
    return info


def snapshot() -> dict:
    stat_text = read_file(f"{PROC}/stat")
    mem_text = read_file(f"{PROC}/meminfo")
    net_text = read_file(f"{PROC}/net/dev")
    load_text = read_file(f"{PROC}/loadavg")
    up_text = read_file(f"{PROC}/uptime")
    out = parse_stat(stat_text)
    out["mem"] = parse_meminfo(mem_text)
    out["net"] = parse_netdev(net_text)
    out["disk"] = disk_usage(os.environ.get("CP_DISK_PATH", "/"))
    out["load"] = parse_loadavg(load_text)
    out["uptime"] = parse_uptime(up_text)
    return out


def info() -> dict:
    cpus = []
    try:
        cpuinfo = read_file(f"{PROC}/cpuinfo")
        for line in cpuinfo.splitlines():
            if line.startswith("model name"):
                cpus.append(line.split(":", 1)[1].strip())
                break
    except OSError:
        pass
    osr = read_os_release()
    return {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpuModel": cpus[0] if cpus else platform.processor() or "unknown",
        "cpuCores": os.cpu_count() or 0,
        "prettyName": osr.get("PRETTY_NAME"),
        "kernelVersion": osr.get("VERSION"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cmd", choices=["snapshot", "info"])
    args = parser.parse_args(argv)
    try:
        payload = snapshot() if args.cmd == "snapshot" else info()
    except (OSError, ValueError, IndexError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
