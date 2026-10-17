"""改动未生效对账：补丁装上了、配置改了，但还在跑的进程没吃到它。

系统补丁、nginx、登录安全、内存压力这几层回答的都是「现在是什么样」。管理员真正会踩的坑在
另一头：apt 把 liblzma 升到 5.8.1-1+deb13u2 了，可 postgres 与 nginx 是十几天前起的，还踩着
那份已经被 unlink 掉的旧库在跑；nginx 的 conf 昨晚改过，而它的 worker 是今早才换的。这类状态
在「服务状态」页上全是绿的——unit 在跑、端口在听、证书没过期——唯独「装上去的东西没进正在
运行的进程」这件事没人说。安全补丁尤其如此：CVE 修完了，跑的还是没有修复的那份内存映射。

这一层严格只读：不 restart 任何服务、不 reload nginx/sshd、不 daemon-reload、不写任何文件。
「什么时候重启 postgres」是业务决策，面板替你按下去就是在业务高峰里掐连接。处置一律是给
管理员自己复制走的命令，写清楚它会不会断流。

三处如实承认局限：
① 「被替换的文件」判据是 `/proc/<pid>/maps` 里以 ` (deleted)` 结尾的映射，且当前同路径文件的
   inode 与映射里的 inode 不同。这只能证明「那份文件被换过」，不能证明换的是安全更新——版本
   归属另取 `dpkg -S`，读不到就只说文件，不编包名。
② 内核是否待重启靠「已安装的 linux-image 比 running 新」推导。Debian 自己的
   `/run/reboot-required` 标记来自 update-notifier-common，很多服务器镜像根本没装它（这台
   机器就没装），只认那个标记等于永远报「不需要重启」。
③ 覆盖不到不需要重启、但确实要重启才生效的东西（例如某些 Python 应用的依赖升级），这类只能
   由应用自己声明。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from datetime import UTC, datetime

from .. import probe
from ..severity import SEV_INFO, SEV_RISK, SEV_WARN
from ..util import tallied
from . import systemd_ops

CACHE_TTL = 120.0
MAX_STALE_UNITS = 12
MAX_PKG_LOOKUPS = 12
PROC_SCAN_CAP = 4000
SCAN_BUDGET_SEC = 3.0
SUBPROCESS_TIMEOUT = 6

REBOOT_MARKER = "/run/reboot-required"
REBOOT_MARKER_PKGS = "/run/reboot-required.pkgs"
SSHD_CONFIG = "/etc/ssh/sshd_config"
SSHD_DIR = "/etc/ssh"
APT_HISTORY = "/var/log/apt/history.log"

# 这些包一旦被替换还踩着旧映射跑，判「要紧」：它们要么直接解析不可信输入（压缩、TLS、XML、
# 字体、curl），要么是提权/认证路径上的（pam、sudo、openssh、systemd、dpkg）。名单不是穷举，
# 命不中的走 warn——宁可让安全库的过期映射红一次让人看一眼，也不要躺在 warn 里没人翻。
SECURITY_PKGS = (
    "libc6", "libc-bin", "libssl", "openssl", "libgnutls", "libnss3", "libpam",
    "openssh", "sudo", "systemd", "libsystemd", "dpkg", "apt", "liblzma", "xz-utils",
    "zlib1g", "libxml2", "libxslt", "libfreetype", "libpng", "libjpeg", "libtiff",
    "libcurl", "libnghttp2", "libexpat1", "sqlite", "libpcre2", "libkrb5", "libgssapi",
    "dovecot", "postfix", "nginx", "apr", "libidn2", "libunistring", "glib", "dbus",
    "polkit", "shadow", "util-linux", "bind9", "libldns", "libyaml",
)

# 不是「文件被替换」的映射：设备、SysV/POSIX 共享内存、匿名与 memfd。postgres 的每个后端都
# 映射 /dev/zero 与 /SYSVxxxxx 并且天然带 (deleted)，不滤掉的话整机每一条都是假阳性。
def _is_noise(path: str) -> bool:
    if not path or path.startswith("["):
        return True
    return (
        path.startswith("/dev/")
        or path.startswith("/SYSV")
        or path.startswith("/memfd:")
        or path.startswith("anon_inode:")
        or path.startswith("/dev/shm")
    )


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().replace(microsecond=0).isoformat()


def _human(sec: float | None) -> str:
    """把秒数说成人话，只到「天/小时/分钟」这一档，给判词用。"""
    if not isinstance(sec, (int, float)) or sec < 0:
        return ""
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400} 天"
    if sec >= 3600:
        return f"{sec // 3600} 小时"
    return f"{max(1, sec // 60)} 分钟"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _boot_epoch() -> int | None:
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _proc_start_epoch(pid: int, btime: int, hz: int) -> int | None:
    """进程启动的墙上时间 = btime + starttime/CLK_TCK。

    不用 `stat /proc/<pid>` 的 mtime：那个目录会被反复改写，实测拿到的是「刚刚有人读过它」的
    时间，nginx 与 sshd 的目录 mtime 一模一样，全都不是启动时刻。
    """
    fields = _read(f"/proc/{pid}/stat").rsplit(")", 1)
    if len(fields) < 2 or not btime:
        return None
    parts = fields[1].split()
    if len(parts) < 20:
        return None
    try:
        return int(btime) + int(parts[19]) // hz
    except ValueError:
        return None


def _unit_of(pid: int) -> str:
    """这进程属于哪个 .service；解析规则在 systemd_ops，这里只认 .service。

    处置建议是 `systemctl restart <unit>`，而 session-*.scope、init.scope 没有"重启它"这个动作，
    所以归不到 .service 的一律留空。旧写法在整行里正则抓第一个 `.service`，于是
    `0::/user.slice/user-0.slice/user@0.service/init.scope` 被归到 user@0.service——那是 systemd
    的用户管理器，不是这个进程自己的单元，页面会劝人去重启它。
    """
    name, kind = systemd_ops.unit_of_cgroup(_read(f"/proc/{pid}/cgroup"))
    return name if kind == "service" else ""


def _cmd_of(pid: int) -> str:
    raw = _read(f"/proc/{pid}/cmdline").replace("\0", " ")
    return re.sub(r"\s+", " ", raw).strip()[:90]


def _maps_deleted_files(pid: int) -> list[tuple[str, str, int]]:
    """返回 (路径, 映射里的 dev 字符串, inode)。maps 的字段是定长的：addr perms offset dev inode path。"""
    text = _read(f"/proc/{pid}/maps")
    if "(deleted)" not in text:
        return []
    out = []
    seen = set()
    for line in text.splitlines():
        if not line.endswith(" (deleted)"):
            continue
        f = line.split()
        if len(f) < 7:
            continue
        path = " ".join(f[5:-1])
        if _is_noise(path) or path in seen:
            continue
        seen.add(path)
        out.append((path, f[3], int(f[4])))
    return out


def _dpkg_owner(path: str) -> tuple[str, str] | None:
    """路径 → (包名, 版本)。只有 /usr 与 /lib 下的文件可能是 dpkg 装的，别为构建残留白付子进程。"""
    if not path.startswith(("/usr/", "/lib/", "/bin/", "/sbin/")):
        return None
    try:
        r = subprocess.run(
            ["dpkg", "-S", path],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    # 形如 "liblzma5:amd64: /usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1"，分号可能是全角/多行命中
    for line in (r.stdout or "").splitlines():
        m = re.match(r"^([^:]+):(?:\S+)?\s*:\s*(.+)$", line.strip())
        if m and m.group(2).strip() == path:
            name = m.group(1).strip()
            ver = _pkg_version(name)
            return (name, ver)
    return None


def _pkg_version(pkg: str) -> str:
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", pkg],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip()


def _security_pkg(name: str) -> bool:
    base = (name or "").split(":")[0]
    return any(base.startswith(p) for p in SECURITY_PKGS)


def _scan_stale(btime: int, self_unit: str) -> dict:
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 250
    try:
        pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
    except OSError:
        return {"ok": False, "reason": "读不到 /proc", "scanned": 0, "budget_hit": False,
                "procs_total": 0, "elapsed_ms": 0, "units": [], "unit_total": 0,
                "procs_stale": 0, "orphan_total": 0}
    by_unit: dict[str, dict] = {}
    stat_cache: dict[tuple[str, str, int], dict] = {}
    scanned = 0
    budget_hit = False
    t0 = time.monotonic()
    for pid in pids[:PROC_SCAN_CAP]:
        if time.monotonic() - t0 > SCAN_BUDGET_SEC:
            budget_hit = True
            break
        found = _maps_deleted_files(pid)
        scanned += 1
        if not found:
            continue
        started = _proc_start_epoch(pid, btime, hz)
        if started is None:
            # 进程在扫描中途退出了：/proc/<pid>/stat 读不到，但它名下的 deleted 映射还在。
            # 这类一次性进程（dpkg 的解压器、构建脚本）报出来只会让人误查半天，直接跳过。
            continue
        unit = _unit_of(pid)
        rec = by_unit.setdefault(unit or "", {
            "unit": unit,
            "procs": 0,
            "pids": [],
            "cmd": _cmd_of(pid),
            "started": started,
            "files": [],
            "_paths": set(),
        })
        rec["procs"] += 1
        if len(rec["pids"]) < 6:
            rec["pids"].append(pid)
        # 参照时刻取「这个单元里最老的那个进程」：只要有一个进程还踩着旧文件，就得按它的启动时间说话。
        rec["started"] = min(rec["started"], started)
        for path, dev, ino in found:
            if path in rec["_paths"]:
                continue
            rec["_paths"].add(path)
            key = (path, dev, ino)
            if key not in stat_cache:
                stat_cache[key] = _stat_current(path, dev, ino)
            rec["files"].append({"path": path, "mapped_inode": ino, **stat_cache[key]})
    items = []
    for unit, rec in by_unit.items():
        kept = [f for f in rec["files"] if f["replaced"]]
        if not kept:
            continue
        started = rec["started"]
        age = int(time.time() - started)
        items.append({
            "unit": unit,
            "procs": rec["procs"],
            "pids": rec["pids"],
            "cmd": rec["cmd"],
            "started": started,
            "started_iso": _epoch_iso(started),
            "age_sec": age,
            "age_human": _human(age),
            "self": bool(unit) and unit == self_unit,
            "files": [{"path": f["path"], "gone": not f["exists"], "ctime": f["ctime"],
                       "ctime_iso": _epoch_iso(f["ctime"]),
                       "replaced_at": bool(f["ctime"] and f["ctime"] > started)}
                      for f in kept],
        })
    items.sort(key=lambda r: (r["unit"] == "", -r["age_sec"]))
    return {
        "ok": True,
        "reason": None,
        "scanned": scanned,
        "procs_total": len(pids),
        "budget_hit": budget_hit or len(pids) > PROC_SCAN_CAP,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "units": items[:MAX_STALE_UNITS],
        "unit_total": len(items),
        "procs_stale": sum(r["procs"] for r in items),
        "orphan_total": sum(1 for r in items if r["unit"] == ""),
    }


def _stat_current(path: str, dev: str, ino: int) -> dict:
    """同路径当前文件与映射里那份对不对得上。dev 也要比：跨设备撞上同 inode 号是巧合，不是同一个文件。"""
    try:
        st = os.stat(path)
    except OSError:
        # 路径整个没了（比如 node_modules 被重装成别的名字）：映射的那份确实已经不存在了。
        return {"exists": False, "inode": None, "replaced": True, "ctime": None}
    same_dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}" == dev
    return {"exists": True, "inode": st.st_ino, "replaced": (not same_dev) or st.st_ino != ino,
            "ctime": int(st.st_ctime)}


def _kernel_state(btime: int) -> dict:
    """需要重启 = 装着比 running 更新的内核包，或者发行版自己挂了标记。

    只认 /run/reboot-required 是不行的：那个标记由 update-notifier-common 在 apt 事件里写，
    服务器镜像默认不装它，本机 `ls /run/reboot-required` 直接 No such file。于是「装了新内核
    没重启」在面板上永远显示绿灯。
    """
    running = os.uname().release
    out = {"ok": False, "reason": None, "running": running, "installed": [], "newer": [],
           "partial": [],
           "marker": {"present": False, "path": REBOOT_MARKER, "files": []},
           "needs_reboot": None, "boot_epoch": btime or None, "boot_iso": _epoch_iso(btime)}
    try:
        r = subprocess.run(
            ["dpkg-query", "-W", "-f=${Package}|${Version}|${db:Status-Abbrev}\n", "linux-image-*"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        out["reason"] = "dpkg-query 调不起来"
        return out
    if r.returncode != 0 and not (r.stdout or "").strip():
        out["reason"] = (r.stderr or "").strip()[:120] or "读不到 linux-image 包"
        return out
    rows = []
    broken: list[dict] = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, ver, status = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not ver:
            continue
        if status == "ii":
            rows.append({"pkg": name, "version": ver})
        elif status != "rc":
            # rc 是「已卸载、只剩配置文件」，盘上没有可启动的内核，跳过它是对的。其余状态
            # （iU 已解包未配置、iF 配置失败）意味着新内核的文件已经在盘上：把它当不存在，
            # 就等于在 apt 停在 configure 阶段时告诉管理员「不需要重启」。
            broken.append({"pkg": name, "version": ver, "status": status})
    out["installed"] = rows
    out["ok"] = True  # 「读到包列表」与「能不能判定要不要重启」是两件事，后者判不了走 needs_reboot=None + reason
    running_pkg = next((x for x in rows if x["pkg"] == f"linux-image-{running}"), None)
    # 真正的内核包名字是 linux-image-<uname 里的版本串>，紧跟版本号；发行版的元包
    # （linux-image-amd64、linux-image-cloud-amd64）名字后面是架构名，自己也带一个不断变新的
    # 版本号，拿它当「新内核」会把每台正常机器都判成待重启。用「linux-image- 后面接数字」区分。
    kernels = [x for x in rows if re.match(r"^linux-image-\d", x["pkg"]) and x["pkg"] != f"linux-image-{running}"]
    if os.path.exists(REBOOT_MARKER):
        out["marker"]["present"] = True
        out["marker"]["files"] = _read_marker_pkgs()
        out["needs_reboot"] = True
        out["reason"] = None
        return out
    if broken:
        # 半装的内核既不能判成「需要重启」（也许它装坏了、还得先回滚），也不能判成「不用管」。
        out["partial"] = broken
        out["needs_reboot"] = None
        out["reason"] = (f"{len(broken)} 个 linux-image 包不是「已安装」状态"
                         f"（{'、'.join(x['status'] for x in broken[:3])}），"
                         "半装的内核文件已经在盘上，先修 dpkg 再回来看这一项")
        return out
    if running_pkg is None and kernels:
        # running 内核没有对应的已装包：可能是自定义内核，也可能是旧包被 autoremove 掉了。
        # 这种情况没法用版本号比，只能把「装了哪几个内核」如实列出来。
        out["newer"] = kernels
        out["needs_reboot"] = None
        out["reason"] = f"读不到与运行中内核 {running} 对应的 linux-image 包，已装的 {len(kernels)} 个无法比对新旧"
    elif running_pkg:
        newer = [k for k in kernels if _dpkg_gt(k["version"], running_pkg["version"])]
        out["newer"] = newer
        out["needs_reboot"] = bool(newer)
    elif kernels:
        out["newer"] = []
        out["needs_reboot"] = None
        out["reason"] = f"运行中内核 {running} 没有对应的 linux-image 包，且未找到可比对的其它内核"
    else:
        out["newer"] = []
        out["needs_reboot"] = None
        out["reason"] = f"读不到与运行中内核 {running} 对应的 linux-image 包，无法判定"
    return out


def kernel_reboot_verdict() -> dict:
    """给别的页签借用的最小重启判定：只跑一次 dpkg-query，不扫 /proc。

    「系统补丁」页签过去自己只看 `/run/reboot-required`，而服务器镜像默认不装写这个标记的
    update-notifier-common，于是它在这类机器上恒为「不用重启」，与本页签的内核包比对互相打脸。
    两处共用这一个来源就不可能再分歧；调用方拿 `needs_reboot`（True/False/None=判不出）。
    """
    return _kernel_state(_boot_epoch())


def _dpkg_gt(a: str, b: str) -> bool:
    """版本比较交给 dpkg 自己：Debian 的版本号规则（epoch、~、dfsg 后缀）自己写会错。"""
    try:
        r = subprocess.run(
            ["dpkg", "--compare-versions", a, "gt", b],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read_marker_pkgs() -> list[str]:
    return [ln.strip() for ln in _read(REBOOT_MARKER_PKGS).splitlines() if ln.strip()][:20]


def _nginx_state(btime: int, hz: int) -> dict:
    """nginx 的配置改动有没有真的进到 worker 里。

    判据不能用 master 进程的启动时间：`nginx -s reload` 不换 master，只换 worker，用 master
    作参照会把「改完并 reload 过了」判成「还没 reload」。worker 都在 reload 那一刻起来，所以
    取最晚的那个 worker 启动时刻作参照。
    """
    out = {"ok": False, "reason": None, "master_pid": None, "worker_pids": [], "ref_start": None,
           "ref_start_iso": None, "conf_files": 0, "newest_file": None, "newest_mtime": None,
           "newest_mtime_iso": None, "pending": None}
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return out
    master = None
    workers = []
    for pid in pids:
        if _read(f"/proc/{pid}/comm").strip() != "nginx":
            continue
        cmd = _cmd_of(pid)
        if "master process" in cmd:
            master = pid
        elif "worker process" in cmd:
            workers.append(pid)
    if not master:
        out["reason"] = "机器上没有 nginx 主进程"
        return out
    out["master_pid"] = master
    out["worker_pids"] = workers
    ref = None
    for pid in [*workers, master]:
        st = _proc_start_epoch(pid, btime, hz)
        if st and (ref is None or st > ref):
            ref = st
    confs = _nginx_conf_paths()
    if confs is None:
        out["reason"] = "nginx -T 读不到已加载的配置文件"
        return out
    newest = (None, 0)
    for p in confs:
        try:
            mt = int(os.stat(p).st_mtime)
        except OSError:
            continue
        if mt > newest[1]:
            newest = (p, mt)
    out["conf_files"] = len(confs)
    out["newest_file"] = newest[0]
    out["newest_mtime"] = newest[1] or None
    out["newest_mtime_iso"] = _epoch_iso(newest[1])
    if ref and newest[0]:
        out["ref_start"] = ref
        out["ref_start_iso"] = _epoch_iso(ref)
        out["pending"] = newest[1] > ref
    out["ok"] = True
    return out


def _nginx_conf_paths() -> list[str] | None:
    try:
        r = subprocess.run(
            ["nginx", "-T"], capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return sorted({os.path.realpath(m) for m in re.findall(r"^# configuration file (.+?):", r.stdout or "", re.M)})


def _sshd_state(btime: int, hz: int) -> dict:
    """sshd 的配置文件比进程新，说明改了没生效（这一节严格只读，面板永远不碰 sshd）。"""
    out = {"ok": False, "reason": None, "pid": None, "started": None, "started_iso": None,
           "files": [], "newest_file": None, "newest_mtime": None, "newest_mtime_iso": None,
           "pending": None}
    pid = None
    for cand in [int(p) for p in os.listdir("/proc") if p.isdigit()]:
        if _read(f"/proc/{cand}/comm").strip() != "sshd":
            continue
        cmd = _cmd_of(cand)
        if "listener" in cmd or "-D" in cmd:
            pid = cand
            break
        pid = pid or cand
    if not pid:
        out["reason"] = "机器上没有 sshd 主进程"
        return out
    paths = _sshd_conf_paths()
    if not paths:
        out["reason"] = f"读不到 {SSHD_CONFIG}"
        return out
    newest = (None, 0)
    for p in paths:
        try:
            mt = int(os.stat(p).st_mtime)
        except OSError:
            continue
        if mt > newest[1]:
            newest = (p, mt)
    started = _proc_start_epoch(pid, btime, hz)
    out["ok"] = True
    out["pid"] = pid
    out["files"] = len(paths)
    out["started"] = started
    out["started_iso"] = _epoch_iso(started)
    out["newest_file"] = newest[0]
    out["newest_mtime"] = newest[1] or None
    out["newest_mtime_iso"] = _epoch_iso(newest[1])
    if started and newest[0]:
        out["pending"] = newest[1] > started
    return out


def _sshd_conf_paths() -> list[str]:
    """主配置 + Include 进来的片段。Include 的值相对 /etc/ssh 解析，支持 glob。"""
    import glob as _glob

    if not os.path.exists(SSHD_CONFIG):
        return []
    out = [SSHD_CONFIG]
    text = _read(SSHD_CONFIG)
    for m in re.finditer(r"^\s*Include\s+(.+)$", text, re.M | re.I):
        for token in m.group(1).split():
            p = token if os.path.isabs(token) else os.path.join(SSHD_DIR, token)
            if any(ch in p for ch in "*?["):
                out.extend(sorted(_glob.glob(p)))
            elif os.path.isfile(p):
                out.append(p)
    seen, dedup = set(), []
    for p in out:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            dedup.append(rp)
    return dedup


def _systemd_state() -> dict:
    """unit 文件改过但没 daemon-reload：systemd 自己知道，直接问它。"""
    out = {"ok": False, "reason": None, "checked": 0, "need_reload": []}
    try:
        listed = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        out["reason"] = "systemctl 调不起来"
        return out
    units = [ln.split()[0] for ln in (listed.stdout or "").splitlines() if ln.strip()]
    if not units:
        out["reason"] = "没有可读的 service 单元"
        return out
    out["checked"] = len(units)
    try:
        shown = subprocess.run(
            ["systemctl", "show", "-p", "Id", "-p", "NeedDaemonReload", "--", *units],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        out["reason"] = "systemctl show 超时"
        return out
    if shown.returncode != 0:
        out["reason"] = (shown.stderr or "").strip()[:120] or "systemctl show 失败"
        return out
    # 按块解析（systemd_ops.parse_show），不按行顺序认领：属性顺序是 systemd 自己定的，
    # 本机实测 MainPID 就排在 Id 前面。逐行认领的写法一旦把 NeedDaemonReload 排在 Id 之前，
    # 就把上一个 unit 的名字挂到这一个的"要重载"上——点名点错服务比不点名更糟。
    for props in systemd_ops.parse_show(shown.stdout or ""):
        if (props.get("NeedDaemonReload") or "").strip() == "yes":
            out["need_reload"].append(props["Id"])
    out["need_reload"] = out["need_reload"][:MAX_STALE_UNITS]
    out["ok"] = True
    return out


def _apt_context() -> dict:
    """最近一次 apt 事务的时间与条目数，给判词一句「什么时候装的」。"""
    out = {"ok": False, "reason": None, "last_start_date": None, "transactions": 0}
    text = _read(APT_HISTORY)
    if not text:
        out["reason"] = "读不到 /var/log/apt/history.log"
        return out
    starts = re.findall(r"^Start-Date:\s*(.+)$", text, re.M)
    out["transactions"] = len(starts)
    out["last_start_date"] = starts[-1].strip() if starts else None
    out["ok"] = True
    return out


def _epoch_iso(v) -> str:
    return datetime.fromtimestamp(v, UTC).astimezone().replace(microsecond=0).isoformat() if v else ""


def _iso_hm(iso) -> str:
    """判词是给人读的中文句子，ISO 的 T 和 +08:00 夹在中间是噪音。
    表格那几列前端会格式化，文案这一路没人接手，所以后端自己缩成 2026-10-08 06:50:51；
    缺字段返回空串，调用方会跳过整句。"""
    return str(iso or "").replace("T", " ")[:19]


def _attach_packages(stale: dict) -> None:
    """给前若干条命中路径补上 dpkg 归属；补不到就留空，判词不编包名。

    dpkg -S 一次要 0.2-0.5 秒，所以按路径缓存、整轮最多查 MAX_PKG_LOOKUPS 个不同路径；
    同一个 liblzma 被 30 个 postgres 后端映射时只查一次。
    """
    budget = MAX_PKG_LOOKUPS
    owners: dict[str, tuple[str, str] | None] = {}
    for rec in stale.get("units") or []:
        for f in rec.get("files") or []:
            f["pkg"] = ""
            f["pkg_version"] = ""
            f["security"] = False
            path = f["path"]
            if path not in owners:
                if budget <= 0:
                    owners[path] = None
                else:
                    budget -= 1
                    owners[path] = _dpkg_owner(path)
            owner = owners[path]
            if not owner:
                continue
            f["pkg"], f["pkg_version"] = owner[0], owner[1] or ""
            f["security"] = _security_pkg(owner[0])


def _findings(kernel: dict, stale: dict, reloads: dict) -> list[dict]:
    out = []
    if kernel.get("needs_reboot"):
        newer = kernel.get("newer") or []
        names = ", ".join(k["pkg"] for k in newer[:3])
        det = (f"运行中的内核是 {kernel.get('running')}"
               + (f"，已装上但没进用的：{names}" if names else "，发行版挂了重启标记"))
        out.append({
            "id": "reboot-kernel", "severity": SEV_RISK,
            "title": "装了新内核，机器还没重启",
            "detail": det + "。内核不在跑的那份里，任何针对旧内核的缓解都不生效。",
            "fix": "uname -r && ls /run/reboot-required.pkgs 2>/dev/null; 选个业务低峰 reboot",
        })
    elif kernel.get("needs_reboot") is None:
        out.append({
            "id": "kernel-unknown", "severity": SEV_INFO,
            "title": "内核是否待重启：判定不了",
            "detail": kernel.get("reason") or "拿不到已装内核列表。",
            "fix": "uname -r && dpkg-query -W 'linux-image-*'",
        })
    for rec in stale.get("units") or []:
        sec = [f for f in rec["files"] if f.get("security")]
        named = [f for f in rec["files"] if f.get("pkg")]
        picked = sec or named or rec["files"]
        labels = sorted({(f.get("pkg") or f["path"].rsplit("/", 1)[-1]) for f in picked})
        unit = rec["unit"]
        shown = unit or "(不属于任何 systemd 单元)"
        when = f"，进程从 {_iso_hm(rec['started_iso'])} 起一直在跑（{rec['age_human']}）"
        replaced = [t for t in (_iso_hm(f["ctime_iso"]) for f in picked) if t]
        at = f"，文件在 {min(replaced)} 被换掉" if replaced else ""
        if sec:
            sev = SEV_RISK
            head = "安全相关的库装上了新版，进程还踩着换掉之前的那份映射"
            fix = f"systemctl status {unit}   # 确认什么时候能断这批连接，再 systemctl restart {unit}"
        elif unit:
            sev = SEV_WARN
            head = "这个服务踩着已被替换的文件在跑"
            fix = f"systemctl status {unit}   # 先看会不会断业务，再 systemctl restart {unit}"
        else:
            sev = SEV_INFO
            head = "有不属于任何 systemd 单元的进程踩着已删除的文件"
            fix = (f"ps -o lstart= -p {','.join(str(p) for p in rec['pids'])}   "
                   "# 这类进程由容器/dev server/手工启动带起来，systemctl 管不到，得由启动它的人重起")
        out.append({
            "id": f"stale-lib:{unit or '(none)'}", "severity": sev,
            "title": f"{shown}：{head}",
            "detail": f"{rec['procs']} 个进程仍映射着 {len(rec['files'])} 个已被替换的文件（{', '.join(labels[:4])}）"
                      f"{at}{when}。新版本装在磁盘上，但不在这个进程的地址空间里。",
            "fix": fix,
            "files": [{"path": f["path"], "pkg": f.get("pkg") or "", "version": f.get("pkg_version") or "",
                       "security": bool(f.get("security")), "gone": f["gone"], "ctime_iso": f["ctime_iso"]}
                      for f in rec["files"]],
        })
    nginx = reloads.get("nginx") or {}
    if nginx.get("pending"):
        out.append({
            "id": "nginx-reload", "severity": SEV_WARN,
            "title": "nginx 配置改了，worker 还是旧的",
            "detail": f"最晚改动的已加载文件是 {os.path.basename(str(nginx.get('newest_file')))}"
                      f"（{nginx.get('conf_files')} 个文件参与比较），比现在这批 worker 的启动时刻还晚。",
            "fix": "nginx -t && nginx -s reload   # reload 不断连接，面板不替你按",
        })
    sshd = reloads.get("sshd") or {}
    if sshd.get("pending"):
        out.append({
            "id": "sshd-reload", "severity": SEV_WARN,
            "title": "sshd 配置比 sshd 进程新，改动没进监听进程",
            "detail": f"{sshd.get('files')} 个配置文件里最晚的比 sshd（pid {sshd.get('pid')}）启动还晚。"
                      "新连接可能仍然按旧配置处理。面板不写 sshd_config、不 reload sshd。",
            "fix": "sshd -t && systemctl reload ssh   # 先 sshd -t，配置有错时 reload 会留下旧进程还是新进程的不确定态",
        })
    sysd = reloads.get("systemd") or {}
    if sysd.get("need_reload"):
        out.append({
            "id": "daemon-reload", "severity": SEV_WARN,
            "title": f"{len(sysd['need_reload'])} 个 unit 文件改过但没 daemon-reload",
            "detail": f"systemd 自己报 NeedDaemonReload=yes：{', '.join(sysd['need_reload'][:5])}。"
                      "在这之前 systemctl restart 用的还是旧版的 unit 定义。",
            "fix": "systemctl daemon-reload",
        })
    return out


def _counts(findings: list[dict]) -> dict:
    return {
        "risk": sum(1 for f in findings if f["severity"] == SEV_RISK),
        "warn": sum(1 for f in findings if f["severity"] == SEV_WARN),
        "info": sum(1 for f in findings if f["severity"] == SEV_INFO),
    }


NOTES = [
    "判据是 /proc/<pid>/maps 里结尾带 (deleted) 的映射，再和同路径当前文件的 inode 对一遍："
    "inode 不同才叫「被替换」。设备、/SYSV*、memfd、匿名映射一律先滤掉——postgres 的每个后端都会命中它们。",
    "nginx 那一节比的是 worker 的启动时刻，不是 master：nginx -s reload 只换 worker，"
    "拿 master 作参照会把「reload 过了」误报成「还没 reload」。",
    "需要重启这一条是自己推的（已装 linux-image 与 running 内核做 dpkg 版本比较）。"
    "Debian 的 /run/reboot-required 来自 update-notifier-common，服务器镜像默认不装；只认那个标记就等于永远报绿。",
    "文件被换成新版本不等于那次升级是安全更新。包名归属取自 dpkg，读不到就只说文件路径，"
    "不编一个看起来更像的版本号。",
    "dpkg 会保留包里文件的原始 mtime，所以这里判断「什么时候被装上」用的是 ctime 不是 mtime。",
    "严格只读：面板不 restart、不 reload、不 daemon-reload、不 reboot。什么时候断业务连接重启一个服务，"
    "只有管理员知道；页面上每条处置都是给你复制走的。",
]


def _collect(self_unit: str) -> dict:
    btime = _boot_epoch() or 0
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 250
    kernel = _kernel_state(btime)
    stale = _scan_stale(btime, self_unit)
    _attach_packages(stale)
    reloads = {"nginx": _nginx_state(btime, hz), "sshd": _sshd_state(btime, hz), "systemd": _systemd_state()}
    findings = _findings(kernel, stale, reloads)
    dead = [k for k, v in (("kernel", kernel), ("stale", stale), *reloads.items()) if not v.get("ok")]
    return {
        "as_of": _now_iso(),
        "cached": False,
        "boot_epoch": btime or None,
        "uptime_sec": int(time.time() - btime) if btime else None,
        "kernel": kernel,
        "stale": stale,
        "reloads": reloads,
        "apt": _apt_context(),
        "self_unit": self_unit,
        "unreadable": dead,
        "findings": findings,
        "counts": _counts(findings),
        "notes": list(NOTES),
    }


_cache: dict = probe.new_cache()
_LOCK = asyncio.Lock()


async def audit(refresh: bool = False) -> dict:
    """带 TTL 的探测入口；并发与连点合并成一轮 /proc + lsof 扫描（见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _reprobe, refresh=refresh, ttl=CACHE_TTL)


async def _reprobe() -> dict:
    return await asyncio.to_thread(_collect, systemd_ops.panel_unit())


def reset_cache() -> None:
    probe.reset(_cache)


def doctor_item(data: dict) -> dict:
    """自检折法：新内核待重启、或安全库踩着旧映射 → fail；其它未生效 → warn；都没有 → pass。

    这一项会 fail 是有意的：补丁装上而进程没吃到，是「以为修了」和「真修了」之间最容易出事的
    那道缝，而其它任何一页都不会主动告诉你。
    """
    kernel = data.get("kernel") or {}
    stale = data.get("stale") or {}
    counts = data.get("counts") or {}
    if not kernel.get("ok") and not stale.get("ok"):
        return {"status": "warn",
                "detail": kernel.get("reason") or stale.get("reason") or "读不到内核与映射信息",
                "fix": "choyeonctl pending"}
    risky = [f for f in data.get("findings") or [] if f["severity"] == SEV_RISK]
    if risky:
        return {"status": "fail",
                "detail": tallied([f["title"] for f in risky], " 项改动没进正在跑的进程"),
                "fix": "choyeonctl pending"}
    warns = [f for f in data.get("findings") or [] if f["severity"] == SEV_WARN]
    if warns:
        return {"status": "warn",
                "detail": tallied([f["title"] for f in warns], " 项改动可能还没生效"),
                "fix": "choyeonctl pending"}
    info = int(counts.get("info") or 0)
    detail = f"改动都生效了：内核 {kernel.get('running')}，没有踩旧映射的服务，nginx/sshd/systemd 都没有待重载"
    if info:
        detail += f"（另有 {info} 条只是让你知道）"
    if data.get("unreadable"):
        detail += f"；读不到的节：{'、'.join(data['unreadable'])}"
    return {"status": "pass", "detail": detail, "fix": ""}
