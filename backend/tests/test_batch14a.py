"""批次⑭A：内存压力与 OOM 对账（memory_service）+ 自检项 + `choyeonctl memory`。

这一页锁的方向和「数字差一点」无关，锁的是「数字看起来准其实骗了人」：

- 测不到不等于 0：读不到的 PSI、cgroup、速率一律给 None 并写明原因。把「没采到」写成 0，
  就等于是宣布「这台机器没有在换页」——而 ⑫⑬ 两批已经吃过同类亏（journalctl 读不到报 0）。
- 计数器倒退也必须是 None：负速率会被 round 成一个很小的正数，比 0 更难看出来。
- 阈值锁边界：已用 80/90、swap 50/80、PSI 1%/5%、单元上限 80/95、线程配额 80、
  换出 2MB/秒、直接回收 256 页/秒。差一档就不出声，误报三遍之后没人再看这一页。
- OOM 是这一项唯一会报 fail 的分支：内核杀过业务进程是事故，不是提示；其它项最多 warn。
- 严格只读：不 kill 进程、不改 sysctl、不动 unit 的 MemoryMax/MemoryHigh，也不进一键修复
  白名单。所有 fix 必须是能当场复制走的只读命令，测试逐条扫一遍禁止词。

夹具里的 meminfo / vmstat / pressure / swaps 都是 2026-10-12 从生产机原样抓回来的
（3.8GB 机器、2GB swapfile、pgscan_direct 累计 55 万页、PSI some 累计 82 秒、oom_kill 0）。
自己编样例行造不出 HugePages 是页数、vfat 没有 inode 表、`memory.max` 写着 `max` 这几个坑。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch14a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch14a_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app import aio_db, main, security, severity  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import memory_service as ms
from app.services import systemd_ops as so  # noqa: E402
from app.util import human_bytes  # noqa: E402

KIB = 1024
MIB = 1024**2
GIB = 1024**3

# ---- 生产机 /proc/meminfo 原文（前 30 行，2026-10-12 抓取）----
MEMINFO_REAL = """MemTotal:        4016532 kB
MemFree:         1863360 kB
MemAvailable:    2713504 kB
Buffers:           78112 kB
Cached:           987564 kB
SwapCached:        56624 kB
Active:          1179592 kB
Inactive:         712912 kB
Active(anon):     799308 kB
Inactive(anon):    59740 kB
Active(file):     380284 kB
Inactive(file):   653172 kB
Unevictable:           0 kB
Mlocked:               0 kB
SwapTotal:       2097148 kB
SwapFree:        1741960 kB
Dirty:               344 kB
Writeback:             0 kB
AnonPages:        806788 kB
Mapped:           161688 kB
Shmem:             32324 kB
KReclaimable:     117176 kB
Slab:             177620 kB
SReclaimable:     117176 kB
SUnreclaim:        60444 kB
KernelStack:        3664 kB
PageTables:        20052 kB
SecPageTables:         0 kB
NFS_Unstable:          0 kB
Bounce:                0 kB
"""

# ---- 生产机 /proc/vmstat 里与内存压力相关的行 ----
VMSTAT_REAL = """pswpin 1357482
pswpout 3100248
allocstall_dma 0
allocstall_dma32 0
allocstall_normal 11
allocstall_movable 981
allocstall_device 0
pgmajfault 798327
pgscan_direct 550363
pgscan_direct_throttle 0
oom_kill 0
"""

PSI_REAL = ("some avg10=0.00 avg60=0.00 avg300=0.00 total=82192998\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=44243115\n")

SWAPS_REAL = ("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
              "/swapfile                               file\t\t2097148\t\t355188\t\t-2\n")

ROOT = "/sys/fs/cgroup"


@contextlib.contextmanager
def fakefs(files=None, listings=None):
    """把 _read / os.listdir 指向内存里的假 /proc 与假 cgroup，其它路径仍走真盘。"""
    files = files or {}
    real_listdir = os.listdir

    def _read(path):
        return files.get(path, "")

    def _listdir(path):
        if listings is not None and path in listings:
            return list(listings[path])
        return real_listdir(path)

    with mock.patch.object(ms, "_read", _read), mock.patch("os.listdir", _listdir):
        yield


def _mem(**over):
    d = {"ok": True, "total": 4 * GIB, "used": 1 * GIB, "used_pct": 25,
         "available": 3 * GIB, "available_pct": 75, "reclaimable": 1 * GIB,
         "free": 3 * GIB, "anon": 1 * GIB, "swap_total": 2 * GIB, "swap_used": 0,
         "swap_pct": 17, "commit_limit": 4 * GIB, "committed": 1 * GIB}
    d.update(over)
    return d


def _press(ok=True, avg10=0.0, avg300=0.0, total=0, reason=""):
    if not ok:
        return {"ok": False, "reason": reason or "读不到 /proc/pressure/memory"}
    return {"ok": True, "some": {"avg10": avg10, "avg60": 0.0, "avg300": avg300,
                                 "total": total},
            "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 0}}


def _rates(ok=True, **over):
    d = {"ok": ok, "secs": 0.8, "swap_in": 0, "swap_out": 0, "direct_reclaim": 0,
         "allocstall": 0, "majfault": 0, "oom_kill_delta": 0}
    d.update(over)
    return d


def _unit(**over):
    d = {"unit": "demo.service", "app": None, "kind": "service", "self": False,
         "readable": True, "current": 10 * MIB, "peak": None, "max": None, "high": None,
         "swap": None, "pids": 10, "pids_max": 4676, "pct_of_cap": None,
         "pct_of_host": None, "pct_of_pids": None,
         "events": {"oom_kill": 0, "max": 0, "high": 0, "oom": 0}}
    d.update(over)
    return d


def _units(*rows, **over):
    d = {"ok": True, "items": list(rows), "managed": 0, "seen": len(rows), "omitted": 0}
    d.update(over)
    return d


def _oom(**over):
    d = {"kernel_total": 0, "unit_kills": {}, "events": [], "source": "counter", "reason": ""}
    d.update(over)
    return d


def _tun(**over):
    d = {"ok": True, "swappiness": 10, "overcommit_memory": 0, "overcommit_ratio": 50,
         "min_free_kbytes": 67584, "panic_on_oom": 0, "oom_kill_allocating_task": 0}
    d.update(over)
    return d


def _ids(findings):
    return [f["id"] for f in findings]


def _by_id(findings, fid):
    return next((f for f in findings if f["id"] == fid), None)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        for k in ("json", "refresh", "quiet"):
            self.__dict__.setdefault(k, False)


def _run(coro, json_mode=False):
    """跑一个 CLI 协程命令，返回 (输出文本, 错误文本, SystemExit 码或 None)。"""
    out, err = io.StringIO(), io.StringIO()
    code = None
    cli.Out.json_mode = json_mode
    try:
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            asyncio.run(coro)
    except SystemExit as exc:
        code = exc.code
    finally:
        cli.Out.json_mode = False
    return out.getvalue(), err.getvalue(), code


class HumanTests(unittest.TestCase):
    def test_units_match_storage_page(self):
        self.assertEqual(human_bytes(0), "0B")
        self.assertEqual(human_bytes(1023), "1023B")
        self.assertEqual(human_bytes(2 * MIB), "2.0MB")
        self.assertEqual(human_bytes(49 * GIB), "49.0GB")

    def test_production_swap_size(self):
        self.assertEqual(human_bytes(355188 * KIB), "346.9MB")

    def test_none_and_negative_and_garbage(self):
        self.assertEqual(human_bytes(None), "—")
        self.assertEqual(human_bytes(-5), "0B")
        self.assertEqual(human_bytes("x"), "—")


class MeminfoTests(unittest.TestCase):
    def test_real_fixture_parsed(self):
        mi = ms._meminfo(MEMINFO_REAL)
        self.assertEqual(mi["MemTotal"], 4016532)
        self.assertEqual(mi["MemAvailable"], 2713504)

    def test_hugepages_are_page_counts_not_kb(self):
        mi = ms._meminfo(MEMINFO_REAL + "HugePages_Total:    1024\nHugepagesize:  2048 kB\n")
        self.assertNotIn("HugePages_Total", mi, "HugePages 是页数，混进来会把数字放大三个数量级")
        self.assertNotIn("Hugepagesize", mi)

    def test_lines_without_colon_and_non_numeric_skipped(self):
        mi = ms._meminfo("garbage line\nBogus: not a number kB\nOk: 12 kB\n")
        self.assertEqual(mi, {"Ok": 12})

    def test_empty_and_none_raw(self):
        self.assertEqual(ms._meminfo(""), {})
        self.assertEqual(ms._meminfo(None), {})

    def test_memory_block_real_numbers(self):
        mem = ms._memory_block(ms._meminfo(MEMINFO_REAL), ms._swaps(SWAPS_REAL))
        self.assertTrue(mem["ok"])
        self.assertEqual(mem["total"], 4016532 * KIB)
        self.assertEqual(mem["used"], 1303028 * KIB)
        self.assertEqual(mem["used_pct"], 32)
        self.assertEqual(mem["available_pct"], 68)
        self.assertEqual(mem["reclaimable"], (987564 + 78112 + 117176) * KIB)
        self.assertEqual(mem["reclaimable_pct"], 29)
        self.assertEqual(mem["swap_used"], 355188 * KIB)
        self.assertEqual(mem["swap_pct"], 17)
        self.assertEqual(len(mem["devices"]), 1)

    def test_used_excludes_reclaimable_cache(self):
        """已用 = MemTotal - MemAvailable：口径必须与概览一致，比 free(1) 低一截。"""
        mem = ms._memory_block({"MemTotal": 1000, "MemAvailable": 900, "MemFree": 10}, [])
        self.assertEqual(mem["used"], 100 * KIB)
        self.assertEqual(mem["used_pct"], 10)

    def test_no_memtotal_is_not_ok(self):
        mem = ms._memory_block({}, [])
        self.assertFalse(mem["ok"])
        self.assertIn("MemTotal", mem["reason"])

    def test_no_swap_reports_none_not_zero(self):
        mem = ms._memory_block({"MemTotal": 1000, "MemAvailable": 500}, [])
        self.assertIsNone(mem["swap_pct"], "没有 swap 时折成 0% 会被读成「swap 很空」")
        self.assertEqual(mem["swap_total"], 0)

    def test_available_above_total_clamps(self):
        """快照之间内存变大也不能出现负的已用。"""
        mem = ms._memory_block({"MemTotal": 1000, "MemAvailable": 1200}, [])
        self.assertEqual(mem["used"], 0)


class VmstatTests(unittest.TestCase):
    def test_real_lines(self):
        vm = ms._vmstat(VMSTAT_REAL)
        self.assertEqual(vm["pgscan_direct"], 550363)
        self.assertEqual(vm["oom_kill"], 0)
        self.assertEqual(vm["allocstall_movable"], 981)

    def test_non_numeric_and_single_field_skipped(self):
        vm = ms._vmstat("a 1\nonlyone\nb noq 2\nc 3\n")
        self.assertEqual(vm, {"a": 1, "c": 3})


class PsiTests(unittest.TestCase):
    def test_real_fixture(self):
        p = ms._psi(PSI_REAL)
        self.assertTrue(p["ok"])
        self.assertEqual(p["some"]["total"], 82192998)
        self.assertEqual(p["some"]["avg10"], 0.0)
        self.assertIn("full", p)

    def test_old_kernel_without_full(self):
        p = ms._psi("some avg10=1.20 avg60=0.50 avg300=0.10 total=999\n")
        self.assertTrue(p["ok"])
        self.assertNotIn("full", p)
        self.assertEqual(p["some"]["avg10"], 1.2)

    def test_missing_file_is_not_ok(self):
        p = ms._psi("")
        self.assertFalse(p["ok"])
        self.assertIn("CONFIG_PSI", p["reason"])

    def test_file_without_some_line(self):
        p = ms._psi("avg10=0.0 total=1\n")
        self.assertFalse(p["ok"])
        self.assertIn("some", p["reason"])


class SwapsTests(unittest.TestCase):
    def test_real_fixture(self):
        devs = ms._swaps(SWAPS_REAL)
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0]["name"], "/swapfile")
        self.assertEqual(devs[0]["type"], "file")
        self.assertEqual(devs[0]["priority"], "-2")
        self.assertEqual(devs[0]["total"], 2097148 * KIB)
        self.assertEqual(devs[0]["used_pct"], 17)

    def test_header_only(self):
        self.assertEqual(ms._swaps(SWAPS_REAL.splitlines()[0] + "\n"), [])

    def test_zero_size_device_gives_none_pct(self):
        devs = ms._swaps("hdr\n/dev/sda3 partition 0 0 0\n")
        self.assertIsNone(devs[0]["used_pct"])

    def test_non_path_and_short_lines_skipped(self):
        self.assertEqual(ms._swaps("hdr\nfoo partition 1 1 1\n/short file 1\n"), [])


class RatesTests(unittest.TestCase):
    def test_page_counters_scaled_to_bytes(self):
        r = ms._rates({"pswpin": 100, "pswpout": 200}, {"pswpin": 200, "pswpout": 400}, 1.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["swap_in"], 100 * ms._PAGE)
        self.assertEqual(r["swap_out"], 200 * ms._PAGE)

    def test_pages_per_second_for_reclaim(self):
        r = ms._rates({"pgscan_direct": 10, "pgmajfault": 1},
                      {"pgscan_direct": 110, "pgmajfault": 3}, 1.0)
        self.assertEqual(r["direct_reclaim"], 100)
        self.assertEqual(r["majfault"], 2)

    def test_zero_window_not_ok_and_all_none(self):
        r = ms._rates({"pswpin": 1}, {"pswpin": 5}, 0)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["swap_in"])

    def test_missing_snapshot_not_ok(self):
        self.assertFalse(ms._rates({}, {"pswpin": 1}, 1.0)["ok"])
        self.assertFalse(ms._rates({"pswpin": 1}, {}, 1.0)["ok"])

    def test_missing_key_gives_none_not_zero(self):
        """这个内核没有 pgscan_direct 这一行时，速率是「测不出」，不是「没有在回收」。"""
        r = ms._rates({"pswpin": 0}, {"pswpin": 0}, 1.0)
        self.assertIsNone(r["direct_reclaim"])

    def test_counter_going_backwards_gives_none(self):
        r = ms._rates({"pswpin": 500}, {"pswpin": 100}, 1.0)
        self.assertIsNone(r["swap_in"], "倒退的计数器填不出负速率，也不能折成 0")
        self.assertIsNone(r["oom_kill_delta"])

    def test_allocstall_sums_every_zone_present(self):
        prev = {"allocstall_normal": 1, "allocstall_movable": 1}
        nxt = {"allocstall_normal": 12, "allocstall_movable": 19}
        self.assertEqual(ms._rates(prev, nxt, 1.0)["allocstall"], 29)

    def test_allocstall_none_when_no_zone_keys(self):
        self.assertIsNone(ms._rates({"pswpin": 0}, {"pswpin": 0}, 1.0)["allocstall"])

    def test_oom_kill_delta(self):
        self.assertEqual(ms._rates({"oom_kill": 0}, {"oom_kill": 2}, 1.0)["oom_kill_delta"], 2)

    def test_rounding_of_sub_second_window(self):
        r = ms._rates({"pgscan_direct": 0}, {"pgscan_direct": 128}, 0.8)
        self.assertEqual(r["secs"], 0.8)
        self.assertEqual(r["direct_reclaim"], 160)


class TunablesTests(unittest.TestCase):
    def test_reads_all_six(self):
        files = {
            "/proc/sys/vm/swappiness": "10\n",
            "/proc/sys/vm/overcommit_memory": "0\n",
            "/proc/sys/vm/overcommit_ratio": "50\n",
            "/proc/sys/vm/min_free_kbytes": "67584\n",
            "/proc/sys/vm/panic_on_oom": "0\n",
            "/proc/sys/vm/oom_kill_allocating_task": "0\n",
        }
        with fakefs(files):
            tun = ms._tunables()
        self.assertTrue(tun["ok"])
        self.assertEqual(tun["swappiness"], 10)

    def test_unreadable_is_not_ok(self):
        with fakefs({}):
            tun = ms._tunables()
        self.assertFalse(tun["ok"])


class NumTests(unittest.TestCase):
    def test_plain_and_multi_field(self):
        files = {"/a": "123\n", "/b": "45 67\n", "/c": "\n"}
        with fakefs(files):
            self.assertEqual(ms._num("/a"), 123)
            self.assertEqual(ms._num("/b"), 45)
            self.assertIsNone(ms._num("/c"))

    def test_max_means_no_limit(self):
        with fakefs({"/m": "max\n"}):
            self.assertIsNone(ms._num("/m"), "max 是「没有上限」，不是数字")

    def test_non_numeric_and_missing(self):
        with fakefs({"/x": "abc\n", "/y": ""}):
            self.assertIsNone(ms._num("/x"))
            self.assertIsNone(ms._num("/missing"))


class UnitOfCgroupTests(unittest.TestCase):
    def test_service(self):
        self.assertEqual(so.unit_of_cgroup("0::/system.slice/panel-rosetta-backend.service"),
                         ("panel-rosetta-backend.service", "service"))

    def test_scope_leaf_is_the_session(self):
        """生产机上 SSH 会话里的进程 cgroup 就是这个形态：归到 scope，不丢掉。"""
        self.assertEqual(so.unit_of_cgroup("0::/user.slice/user-0.slice/session-4328.scope"),
                         ("session-4328.scope", "scope"))

    def test_root(self):
        self.assertEqual(so.unit_of_cgroup("0::/"), (None, "root"))

    def test_v1_multiline_not_guessed(self):
        raw = "11:memory:/system.slice/foo.service\n2:cpu:/\n"
        self.assertEqual(so.unit_of_cgroup(raw), (None, "none"))

    def test_unknown_leaf(self):
        """整条路径里没有一个带后缀的组件，才算归不到单元（非 systemd 的 cgroup 形态）。"""
        self.assertEqual(so.unit_of_cgroup("0::/kubepods/besteffort/pod6c1d/1f2e3d4c"),
                         (None, "other"))

    def test_单元目录下的子组归到那个单元(self):
        """`systemd-udevd.service/udev`（生产机 pid 351 原样）：叶子没后缀，归属仍在。

        只看叶子会把一台正在跑的 udevd 报成「归不到单元」，从进程归因表里整个消失。
        """
        self.assertEqual(so.unit_of_cgroup("0::/system.slice/systemd-udevd.service/udev"),
                         ("systemd-udevd.service", "service"))

    def test_empty(self):
        self.assertEqual(so.unit_of_cgroup(""), (None, "none"))


class ProcsTests(unittest.TestCase):
    FILES = {
        "/proc/100/statm": "1000 500 0 0 0 0 0",
        "/proc/100/cgroup": "0::/system.slice/panel-rosetta-backend.service",
        "/proc/100/comm": "python\n",
        "/proc/101/statm": "1000 250 0 0 0 0 0",
        "/proc/101/cgroup": "0::/user.slice/user-0.slice/session-1.scope",
        "/proc/101/comm": "node",
        "/proc/102/statm": "0 0 0 0 0 0 0",
        "/proc/102/cgroup": "0::/system.slice/x.service",
        "/proc/102/comm": "kthread",
        "/proc/103/statm": "",
    }
    LIST = {"/proc": ["100", "101", "102", "103", "self", "104", "up"]}

    def test_sorted_rss_and_unit_attribution(self):
        with fakefs(self.FILES, self.LIST):
            d = ms._procs(limit=5)
        self.assertTrue(d["ok"])
        rows = d["items"]
        self.assertEqual([r["pid"] for r in rows], [100, 101])
        self.assertEqual(rows[0]["rss"], 500 * ms._PAGE)
        self.assertEqual(rows[0]["unit"], "panel-rosetta-backend.service")
        self.assertEqual(rows[0]["kind"], "service")
        self.assertEqual(rows[1]["kind"], "scope")

    def test_count_is_all_numeric_dirs(self):
        with fakefs(self.FILES, self.LIST):
            d = ms._procs(limit=5)
        self.assertEqual(d["count"], 5, "只有数字目录算进程，self/up 不算")
        self.assertEqual(d["scanned"], 2, "内核线程与读不到 statm 的不进榜")

    def test_limit_applied(self):
        with fakefs(self.FILES, self.LIST):
            self.assertEqual(len(ms._procs(limit=1)["items"]), 1)

    def test_budget_hit_is_reported(self):
        with fakefs(self.FILES, self.LIST):
            d = ms._procs(limit=5, budget=-1.0)
        self.assertTrue(d["budget_hit"])
        self.assertEqual(d["items"], [])

    def test_comm_truncated(self):
        files = dict(self.FILES, **{"/proc/105/statm": "1000 10 0 0 0 0 0",
                                    "/proc/105/cgroup": "0::/",
                                    "/proc/105/comm": "z" * 100})
        with fakefs(files, {"/proc": ["105"]}):
            self.assertEqual(len(ms._procs()["items"][0]["comm"]), 64)

    def test_unreadable_proc_is_not_ok(self):
        with mock.patch("os.listdir", side_effect=OSError("no proc")):
            d = ms._procs()
        self.assertFalse(d["ok"])
        self.assertEqual(d["items"], [])


class EventsTests(unittest.TestCase):
    def test_real_shape(self):
        with fakefs({"/e": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 2\noom_group_kill 0\n"}):
            ev = ms._events("/e")
        self.assertEqual(ev["oom_kill"], 2)
        self.assertEqual(ev["max"], 0)

    def test_garbage_skipped(self):
        with fakefs({"/e": "low 0\nbroken\nno number here\nx y\n"}):
            self.assertEqual(ms._events("/e"), {"low": 0})


CG_FILES = {
    f"{ROOT}/system.slice/choyeon-panel.service/memory.current": "42000000\n",
    f"{ROOT}/system.slice/choyeon-panel.service/memory.max": "536870912\n",
    f"{ROOT}/system.slice/choyeon-panel.service/memory.events":
        "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n",
    f"{ROOT}/system.slice/choyeon-panel.service/pids.current": "4\n",
    f"{ROOT}/system.slice/choyeon-panel.service/pids.max": "256\n",
    f"{ROOT}/system.slice/panel-rosetta-backend.service/memory.current": "383795072\n",
    f"{ROOT}/system.slice/panel-rosetta-backend.service/memory.max": "max\n",
    f"{ROOT}/system.slice/panel-rosetta-backend.service/memory.events":
        "low 0\nhigh 0\nmax 3\noom 0\noom_kill 0\n",
    f"{ROOT}/system.slice/panel-rosetta-backend.service/pids.current": "21\n",
    f"{ROOT}/system.slice/system-postgresql.slice/memory.current": "106700000\n",
    f"{ROOT}/system.slice/system-postgresql.slice/memory.events": "oom_kill 0\n",
    f"{ROOT}/system.slice/tiny.service/memory.current": "1000\n",
    f"{ROOT}/system.slice/tiny.service/memory.events": "oom_kill 0\n",
    f"{ROOT}/system.slice/extra.service/memory.current": "7000\n",
    f"{ROOT}/system.slice/extra.service/memory.events": "oom_kill 0\n",
}
CG_LIST = {f"{ROOT}/system.slice": ["choyeon-panel.service", "panel-rosetta-backend.service",
                                    "system-postgresql.slice", "tiny.service", "ghost.service",
                                    "cgroup.controllers"]}


class UnitRowTests(unittest.TestCase):
    def test_real_panel_row(self):
        with fakefs(CG_FILES):
            row = ms._unit_row(ROOT, "choyeon-panel.service", None, "choyeon-panel.service")
        self.assertEqual(row["current"], 42000000)
        self.assertEqual(row["max"], 536870912)
        self.assertEqual(row["pids"], 4)
        self.assertEqual(row["pids_max"], 256)
        self.assertEqual(row["kind"], "service")
        self.assertTrue(row["self"])

    def test_peak_absent_on_old_kernels(self):
        """6.5 之前没有 memory.peak：给 None，不能折成 0（0 会被读成「从没高过」）。"""
        with fakefs(CG_FILES):
            row = ms._unit_row(ROOT, "choyeon-panel.service", None, None)
        self.assertIsNone(row["peak"])
        self.assertFalse(row["self"])

    def test_stopped_unit_returns_none(self):
        with fakefs({}):
            self.assertIsNone(ms._unit_row(ROOT, "ghost.service", None, None))

    def test_events_only_unit_still_listed(self):
        files = {f"{ROOT}/system.slice/hurt.service/memory.events": "oom_kill 1\n"}
        with fakefs(files):
            row = ms._unit_row(ROOT, "hurt.service", None, None)
        self.assertIsNotNone(row)
        self.assertFalse(row["readable"])
        self.assertEqual(row["events"]["oom_kill"], 1)

    def test_slice_kind(self):
        with fakefs(CG_FILES):
            row = ms._unit_row(ROOT, "system-postgresql.slice", None, None)
        self.assertEqual(row["kind"], "slice")


class UnitsTests(unittest.TestCase):
    def test_not_cgroup_v2(self):
        d = ms._units(None, {}, None)
        self.assertFalse(d["ok"])
        self.assertIn("cgroup v2", d["reason"])

    def test_unreadable_slice_dir(self):
        with mock.patch("os.listdir", side_effect=OSError("nope")):
            d = ms._units(ROOT, {}, None)
        self.assertFalse(d["ok"])
        self.assertEqual(d["items"], [])

    def test_ranked_by_current_not_by_name(self):
        with fakefs(CG_FILES, CG_LIST):
            d = ms._units(ROOT, {}, None)
        self.assertEqual([r["unit"] for r in d["items"]][:2],
                         ["panel-rosetta-backend.service", "system-postgresql.slice"])
        self.assertEqual(d["seen"], 5, "只有带单元后缀的条目参与对账")
        self.assertEqual(d["omitted"], 0)

    def test_top_n_reports_omitted(self):
        with fakefs(CG_FILES, CG_LIST):
            d = ms._units(ROOT, {}, None, top=2)
        self.assertEqual(len(d["items"]), 2)
        self.assertEqual(d["omitted"], 2)

    def test_panel_self_row_never_dropped(self):
        """面板自己被上限杀掉整个界面就没了，所以哪怕只占 42MB 也必须常驻这一页。"""
        with fakefs(CG_FILES, CG_LIST):
            d = ms._units(ROOT, {}, "choyeon-panel.service", top=1)
        units = [r["unit"] for r in d["items"]]
        self.assertIn("choyeon-panel.service", units)
        self.assertEqual(units[0], "panel-rosetta-backend.service")
        self.assertTrue([r for r in d["items"] if r["unit"] == "choyeon-panel.service"][0]["self"])

    def test_self_unit_outside_listing_still_tried(self):
        with fakefs(CG_FILES, CG_LIST):
            d = ms._units(ROOT, {}, "extra.service", top=20)
        self.assertIn("extra.service", [r["unit"] for r in d["items"]])

    def test_app_attribution(self):
        app_map = {"panel-rosetta-backend.service": "rosetta-backend"}
        with fakefs(CG_FILES, CG_LIST):
            d = ms._units(ROOT, app_map, None)
        row = [r for r in d["items"] if r["unit"] == "panel-rosetta-backend.service"][0]
        self.assertEqual(row["app"], "rosetta-backend")
        self.assertEqual(d["managed"], 1)

    def test_decorate_percentages(self):
        with fakefs(CG_FILES, CG_LIST):
            d = ms._units(ROOT, {}, "choyeon-panel.service", top=20)
            ms._decorate_units(d, 4016532 * KIB)
        panel = [r for r in d["items"] if r["unit"] == "choyeon-panel.service"][0]
        backend = [r for r in d["items"] if r["unit"] == "panel-rosetta-backend.service"][0]
        self.assertEqual(panel["pct_of_cap"], 8)
        self.assertEqual(panel["pct_of_host"], 1)
        self.assertEqual(panel["pct_of_pids"], 2)
        self.assertIsNone(backend["pct_of_cap"], "memory.max 是 max 时不该算出百分比")

    def test_decorate_needs_positive_whole(self):
        d = _units(_unit(current=10, pids=5))
        ms._decorate_units(d, 0)
        self.assertIsNone(d["items"][0]["pct_of_host"])
        self.assertIsNone(d["items"][0]["pct_of_cap"])


JOURNAL_OOM_TEXT = """2026-10-11T03:12:44 host kernel: Out of memory: Killed process 4242 (node) total-vm:1234567kB, anon-rss:512344kB
2026-10-11T03:12:44 host kernel: Memory cgroup out of memory: Killed process 4243 (python3) total-vm:999kB
2026-10-11T03:12:45 host kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),global_oom,task:python,pid=4244,uid=0
2026-10-11T03:12:46 host kernel: Out of memory: some line we cannot parse
"""


class OomEventsTests(unittest.TestCase):
    def test_both_kernel_spellings(self):
        evs = ms._oom_events(JOURNAL_OOM_TEXT)
        self.assertEqual(evs[0]["comm"], "node")
        self.assertEqual(evs[0]["pid"], 4242)
        self.assertEqual(evs[0]["at"], "2026-10-11T03:12:44")
        self.assertEqual(evs[2]["comm"], "python")
        self.assertEqual(evs[2]["pid"], 4244)

    def test_unparsable_line_kept_as_raw(self):
        """猜错的受害者比没有受害者更容易把人引到错方向，所以只留原文。"""
        evs = ms._oom_events(JOURNAL_OOM_TEXT)
        self.assertIsNone(evs[3]["comm"])
        self.assertIsNone(evs[3]["pid"])
        self.assertIn("cannot parse", evs[3]["line"])

    def test_oom_reaper_lines_included(self):
        evs = ms._oom_events("2026-10-11T03:12:44 host kernel: oom_reaper: reaped process 4242 (node)")
        self.assertEqual(len(evs), 1)

    def test_unrelated_lines_ignored(self):
        self.assertEqual(ms._oom_events("kernel: normal boot message\n"), [])

    def test_cap_moved_to_the_payload_and_declares_the_total(self):
        """解析不再自己砍：砍在载荷那一层，并同时报出总数与没列出的个数（批次86）。"""
        text = "\n".join(f"at{i} kernel: Out of memory: Killed process {i} (p{i})" for i in range(20))
        self.assertEqual(len(ms._oom_events(text)), 20)
        with mock.patch.object(ms.subprocess, "run", return_value=mock.Mock(stdout=text)):
            d = ms._oom({"oom_kill": 20}, [])
        self.assertEqual(len(d["events"]), ms.MAX_OOM_EVENTS)
        self.assertEqual(d["events"][-1]["pid"], 19)
        self.assertEqual(d["events_total"], 20)
        self.assertEqual(d["events_omitted"], 20 - ms.MAX_OOM_EVENTS)


class OomTests(unittest.TestCase):
    def test_zero_counts_never_spawn_journalctl(self):
        with mock.patch.object(ms.subprocess, "run") as run:
            d = ms._oom({"oom_kill": 0}, [])
        self.assertEqual(d["source"], "counter")
        run.assert_not_called()

    def test_journal_used_only_when_something_died(self):
        proc = mock.Mock(stdout=JOURNAL_OOM_TEXT)
        with mock.patch.object(ms.subprocess, "run", return_value=proc) as run:
            d = ms._oom({"oom_kill": 3}, [])
        self.assertEqual(d["kernel_total"], 3)
        self.assertEqual(d["source"], "journal")
        self.assertEqual(len(d["events"]), 4)
        run.assert_called_once()
        self.assertIn("journalctl -k -b", " ".join(run.call_args[0][0]))

    def test_unit_kills_alone_also_triggers_journal(self):
        rows = [_unit(unit="app.service", events={"oom_kill": 1, "max": 0, "high": 0, "oom": 0})]
        with mock.patch.object(ms.subprocess, "run", return_value=mock.Mock(stdout="")):
            d = ms._oom({"oom_kill": 0}, rows)
        self.assertEqual(d["unit_kills"], {"app.service": 1})
        self.assertEqual(d["source"], "counter-only")

    def test_timeout_is_reported_as_counter_only(self):
        with mock.patch.object(ms.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("journalctl", 8)):
            d = ms._oom({"oom_kill": 2}, [])
        self.assertEqual(d["source"], "counter-only")
        self.assertIn("TimeoutExpired", d["reason"])
        self.assertEqual(d["events"], [])

    def test_missing_journal_binary_is_counter_only(self):
        with mock.patch.object(ms.subprocess, "run", side_effect=OSError("no journalctl")):
            self.assertEqual(ms._oom({"oom_kill": 2}, [])["source"], "counter-only")

    def test_empty_output_says_logs_may_have_rotated(self):
        with mock.patch.object(ms.subprocess, "run", return_value=mock.Mock(stdout="   \n")):
            d = ms._oom({"oom_kill": 2}, [])
        self.assertIn("轮转", d["reason"])


class FindingsTests(unittest.TestCase):
    def _f(self, mem=None, press=None, rates=None, units=None, oom=None, tun=None):
        return ms._findings(mem or _mem(), press or _press(), rates or _rates(),
                            units or _units(), oom or _oom(), tun or _tun())

    def test_oom_first_with_victims(self):
        oom = _oom(kernel_total=2, source="journal",
                   events=[{"pid": 4242, "comm": "node", "at": "x", "line": "y"},
                           {"pid": 4243, "comm": "python3", "at": "x", "line": "y"}])
        f = self._f(oom=oom)
        self.assertEqual(f[0]["id"], "oom-kills")
        self.assertEqual(f[0]["severity"], ms.SEV_RISK)
        self.assertIn("node(4242)", f[0]["detail"])

    def test_unit_kill_without_kernel_count_still_risk(self):
        oom = _oom(unit_kills={"app.service": 1})
        f = _by_id(self._f(oom=oom), "oom-kills")
        self.assertEqual(f["severity"], ms.SEV_RISK)
        self.assertIn("app.service 被自己的上限杀 1 次", f["detail"])

    def test_clean_machine_has_no_findings(self):
        self.assertEqual(self._f(), [])

    def test_psi_boundaries(self):
        self.assertEqual(_by_id(self._f(press=_press(avg10=5.0)), "psi-active")["severity"],
                         ms.SEV_RISK)
        self.assertEqual(_by_id(self._f(press=_press(avg10=4.9, avg300=1.0)),
                                "psi-active")["severity"], ms.SEV_WARN)
        self.assertIsNone(_by_id(self._f(press=_press(avg10=0.0, avg300=0.9)), "psi-active"))

    def test_psi_unreadable_is_silent(self):
        """读不到 PSI 不该被写成「没有问题」，也不该在这里报警——那一句话由 pressure.reason 承担。"""
        self.assertIsNone(_by_id(self._f(press=_press(ok=False)), "psi-active"))

    def test_stalled_seconds_not_in_findings_but_in_cli(self):
        self.assertEqual(self._f(press=_press(total=82192998)), [],
                         "累计停顿是历史数字，不该据此报警")

    def test_swap_rate_threshold(self):
        self.assertEqual(_by_id(self._f(rates=_rates(swap_out=2 * MIB)), "swapping")["severity"],
                         ms.SEV_WARN)
        self.assertIsNone(_by_id(self._f(rates=_rates(swap_out=2 * MIB - 1)), "swapping"))

    def test_direct_reclaim_threshold(self):
        self.assertIsNotNone(_by_id(self._f(rates=_rates(direct_reclaim=256)), "direct-reclaim"))
        self.assertIsNone(_by_id(self._f(rates=_rates(direct_reclaim=255)), "direct-reclaim"))

    def test_none_rate_is_not_zero_rate(self):
        self.assertEqual(self._f(rates=_rates(swap_out=None, direct_reclaim=None,
                                               swap_in=None, allocstall=None, majfault=None)), [])

    def test_mem_used_boundaries(self):
        self.assertIsNone(_by_id(self._f(mem=_mem(used_pct=79)), "mem-used"))
        self.assertEqual(_by_id(self._f(mem=_mem(used_pct=80)), "mem-used")["severity"], ms.SEV_WARN)
        self.assertEqual(_by_id(self._f(mem=_mem(used_pct=90)), "mem-used")["severity"], ms.SEV_RISK)

    def test_swap_pct_boundaries(self):
        self.assertIsNone(_by_id(self._f(mem=_mem(swap_pct=17)), "swap-full"))
        self.assertEqual(_by_id(self._f(mem=_mem(swap_pct=50)), "swap-full")["severity"], ms.SEV_WARN)
        self.assertEqual(_by_id(self._f(mem=_mem(swap_pct=80)), "swap-full")["severity"], ms.SEV_RISK)
        self.assertIsNone(_by_id(self._f(mem=_mem(swap_pct=None)), "swap-full"))

    def test_commit_hard_mode_is_risk(self):
        f = _by_id(self._f(mem=_mem(commit_limit=1 * GIB, committed=2 * GIB),
                           tun=_tun(overcommit_memory=2)), "commit")
        self.assertEqual(f["severity"], ms.SEV_RISK)
        self.assertIn("cannot fork", f["detail"])

    def test_commit_heuristic_is_info_only(self):
        """启发式模式下超上限是真有的（这台机器就是），报 risk 就是误报。"""
        f = _by_id(self._f(mem=_mem(commit_limit=1 * GIB, committed=2 * GIB),
                           tun=_tun(overcommit_memory=0)), "commit")
        self.assertEqual(f["severity"], ms.SEV_INFO)
        self.assertIn("不会因此拒绝分配", f["detail"])

    def test_commit_within_limit_silent(self):
        self.assertIsNone(_by_id(self._f(), "commit"))

    def test_unit_cap_boundaries(self):
        u = _units(_unit(unit="a.service", current=80, max=100, pct_of_cap=80))
        self.assertEqual(_by_id(self._f(units=u), "cap:a.service")["severity"], ms.SEV_WARN)
        u = _units(_unit(unit="a.service", current=95, max=100, pct_of_cap=95))
        self.assertEqual(_by_id(self._f(units=u), "cap:a.service")["severity"], ms.SEV_RISK)
        u = _units(_unit(unit="a.service", current=79, max=100, pct_of_cap=79))
        self.assertIsNone(_by_id(self._f(units=u), "cap:a.service"))

    def test_throttled_history_is_info(self):
        u = _units(_unit(unit="a.service", events={"oom_kill": 0, "max": 3, "high": 1, "oom": 0}))
        f = _by_id(self._f(units=u), "cap-throttled:a.service")
        self.assertEqual(f["severity"], ms.SEV_INFO)
        self.assertIn("拦过 3 次", f["title"])

    def test_throttled_history_silent_at_hard_cap(self):
        """已经 risk 的时候不再补一条 info，同一条判词不重复出声。"""
        u = _units(_unit(unit="a.service", current=96, max=100, pct_of_cap=96,
                         events={"oom_kill": 0, "max": 3, "high": 0, "oom": 0}))
        f = self._f(units=u)
        self.assertIsNone(_by_id(f, "cap-throttled:a.service"))
        self.assertEqual(len(f), 1)

    def test_pids_quota(self):
        u = _units(_unit(unit="a.service", pids=80, pids_max=100, pct_of_pids=80))
        self.assertIn("pids:a.service", _ids(self._f(units=u)))
        u = _units(_unit(unit="a.service", pids=79, pids_max=100, pct_of_pids=79))
        self.assertNotIn("pids:a.service", _ids(self._f(units=u)))
        u = _units(_unit(unit="a.service", pids=None, pids_max=100, pct_of_pids=None))
        self.assertNotIn("pids:a.service", _ids(self._f(units=u)))

    def test_app_name_in_title(self):
        u = _units(_unit(unit="a.service", app="rosetta-backend", pct_of_cap=95, current=95,
                         max=100))
        self.assertIn("rosetta-backend（a.service）", _by_id(self._f(units=u),
                                                             "cap:a.service")["title"])

    def test_ids_unique(self):
        u = _units(_unit(unit="a.service", pct_of_cap=95, current=95, max=100),
                   _unit(unit="b.service", pct_of_cap=90, current=90, max=100))
        f = self._f(mem=_mem(used_pct=95, swap_pct=85), press=_press(avg10=9.0),
                    rates=_rates(swap_out=10 * MIB, direct_reclaim=5000),
                    units=u, oom=_oom(kernel_total=1))
        self.assertEqual(len(_ids(f)), len(set(_ids(f))))

    def test_every_finding_explains_why_and_gives_readonly_fix(self):
        u = _units(_unit(unit="a.service", pct_of_cap=95, current=95, max=100,
                         pids=90, pids_max=100, pct_of_pids=90))
        f = self._f(mem=_mem(used_pct=95, swap_pct=85), press=_press(avg10=9.0),
                    rates=_rates(swap_out=10 * MIB, direct_reclaim=5000),
                    units=u, oom=_oom(kernel_total=1, source="counter-only", reason="取不到日志"))
        self.assertGreaterEqual(len(f), 6)
        for one in f:
            self.assertTrue(one["detail"], one["id"])
            self.assertTrue(one["fix"], one["id"])
            self.assertIn(one["severity"], severity.SEV_LABEL)

    def test_no_finding_suggests_a_write(self):
        """这一页严格只读：判词里不许出现 kill、sysctl -w、systemctl restart、改上限的命令。"""
        banned = ("kill ", "sysctl -w", "systemctl restart", "rm ", "> /proc", "echo",
                  "set-property", "truncate", "--vacuum")
        u = _units(_unit(unit="a.service", pct_of_cap=95, current=95, max=100,
                         pids=90, pids_max=100, pct_of_pids=90,
                         events={"oom_kill": 1, "max": 2, "high": 0, "oom": 1}))
        for one in self._f(mem=_mem(used_pct=95, swap_pct=85), press=_press(avg10=9.0),
                           rates=_rates(swap_out=10 * MIB, direct_reclaim=5000),
                           units=u, oom=_oom(kernel_total=1, unit_kills={"a.service": 1})):
            for word in banned:
                self.assertNotIn(word, one["fix"], f"{one['id']} 的处置命令越界：{one['fix']}")


class CountsTests(unittest.TestCase):
    def test_tallies(self):
        c = ms._counts([{"severity": "risk"}, {"severity": "warn"}, {"severity": "warn"},
                        {"severity": "info"}])
        self.assertEqual(c, {"risk": 1, "warn": 2, "info": 1})

    def test_empty(self):
        self.assertEqual(ms._counts([]), {"risk": 0, "warn": 0, "info": 0})


COLLECT_FILES = dict(CG_FILES)
COLLECT_FILES.update({
    "/proc/meminfo": MEMINFO_REAL,
    "/proc/vmstat": VMSTAT_REAL,
    "/proc/pressure/memory": PSI_REAL,
    "/proc/swaps": SWAPS_REAL,
    "/proc/sys/vm/swappiness": "10\n",
    "/proc/sys/vm/overcommit_memory": "0\n",
    "/proc/200/statm": "1000 900 0 0 0 0 0",
    "/proc/200/cgroup": "0::/system.slice/panel-rosetta-backend.service",
    "/proc/200/comm": "python",
    "/proc/201/statm": "1000 100 0 0 0 0 0",
    "/proc/201/cgroup": "0::/user.slice/user-0.slice/session-9.scope",
    "/proc/201/comm": "node",
})
COLLECT_LISTS = dict(CG_LIST)
COLLECT_LISTS["/proc"] = ["1", "200", "201", "stat"]


# 「面板自己」钉在这里，不跟跑测试的这台机器的 cgroup 走：见
# CollectTests.test_self_unit_comes_from_the_pinned_source（批次㊿D，CI 上红过）。
SELF_UNIT = "choyeon-panel.service"


class CollectTests(unittest.TestCase):
    def _collect(self, app_map=None, self_unit=SELF_UNIT):
        with fakefs(COLLECT_FILES, COLLECT_LISTS), \
             mock.patch.object(ms, "_cgroup_root", return_value=ROOT), \
             mock.patch.object(ms.systemd_ops, "panel_unit", lambda: self_unit):
            return ms._collect(app_map or {}, window=0)

    def test_top_level_shape(self):
        d = self._collect()
        for key in ("as_of", "cached", "mem", "pressure", "rates", "tunables", "units",
                    "procs", "oom", "findings", "counts", "notes", "self_unit"):
            self.assertIn(key, d)
        self.assertEqual(d["notes"], list(ms.NOTES))
        self.assertFalse(d["cached"])

    def test_production_numbers_reproduce(self):
        """夹具必须能复现生产机 CLI 上的那几个数：32% / 17% / 82 秒 / oom 0。"""
        d = self._collect()
        self.assertEqual(d["mem"]["used_pct"], 32)
        self.assertEqual(d["mem"]["swap_pct"], 17)
        self.assertEqual(d["pressure"]["some"]["total"], 82192998)
        self.assertEqual(d["oom"]["kernel_total"], 0)
        self.assertEqual(d["counts"], {"risk": 0, "warn": 0, "info": 1, "procs": 3, "units": 4})

    def test_backend_throttle_history_surfaces_as_info(self):
        """memory.events 的 max 非零是「曾被自己的上限拦过」，整机不缺内存时也要说出来。"""
        d = self._collect()
        one = _by_id(d["findings"], "cap-throttled:panel-rosetta-backend.service")
        self.assertIsNotNone(one)
        self.assertEqual(one["severity"], ms.SEV_INFO)
        self.assertIn("memory.events", one["fix"])

    def test_self_unit_comes_from_the_pinned_source(self):
        """self_unit 的出处是 systemd_ops.panel_unit()，`_collect` 只负责把它带出来。

        这条以前叫 `test_self_unit_resolved_from_cgroup`，读的是宿主的 /proc/self/cgroup：
        本机在 sshd 的 scope 里跑 → panel_unit() 退回安装名 choyeon-panel.service，正好等于
        fixture，于是它一直绿；GitHub runner 的进程跑在自己的 .service 里，同一个 commit
        就报 'actions.runner….service' != 'choyeon-panel.service'（批次㊿D）。
        上面那行假的 "/proc/self/cgroup" 夹具也是同一段历史的残留——`_read` 从来不去读它。
        """
        self.assertEqual(self._collect()["self_unit"], SELF_UNIT)
        self.assertEqual(self._collect(self_unit="renamed-panel.service")["self_unit"],
                         "renamed-panel.service", "改名必须立刻体现在判词上，否则这里又是影子")

    def test_app_map_reaches_both_tables(self):
        d = self._collect({"panel-rosetta-backend.service": "rosetta-backend"})
        row = [r for r in d["units"]["items"] if r["unit"] == "panel-rosetta-backend.service"][0]
        self.assertEqual(row["app"], "rosetta-backend")
        proc = [p for p in d["procs"]["items"] if p["pid"] == 200][0]
        self.assertEqual(proc["app"], "rosetta-backend")

    def test_window_zero_means_no_rates(self):
        """没采样就是 None，不能报 0/秒——那会被读成「这台机器没有在换页」。"""
        r = self._collect()["rates"]
        self.assertFalse(r["ok"])
        self.assertIsNone(r["swap_out"])

    def test_collect_spawns_nothing_when_no_oom(self):
        """整条采集链路只读 /proc 与 /sys：没有 OOM 记录就不该起 journalctl 子进程。"""
        with fakefs(COLLECT_FILES, COLLECT_LISTS), \
             mock.patch.object(ms, "_cgroup_root", return_value=ROOT), \
             mock.patch.object(ms.subprocess, "run") as run:
            ms._collect({}, window=0)
        run.assert_not_called()


class AuditTests(unittest.TestCase):
    def setUp(self):
        ms.reset_cache()
        self.addCleanup(ms.reset_cache)

    def test_cache_hit_marks_cached(self):
        seen = []

        def spy(app_map=None, window=ms.RATE_WINDOW_SEC):
            seen.append(1)
            return {"mem": {"ok": True}, "counts": {}, "findings": [], "notes": []}

        with mock.patch.object(ms, "_collect", spy), \
             mock.patch.object(ms, "_unit_map", new=mock.AsyncMock(return_value={})):
            first = asyncio.run(ms.audit())
            second = asyncio.run(ms.audit())
            third = asyncio.run(ms.audit(refresh=True))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(seen), 2, "TTL 内命中缓存，refresh 才重采")
        self.assertFalse(third["cached"])

    def test_reset_cache_forces_resample(self):
        with mock.patch.object(ms, "_collect", return_value={"mem": {"ok": True}}), \
             mock.patch.object(ms, "_unit_map", new=mock.AsyncMock(return_value={})):
            asyncio.run(ms.audit())
            ms.reset_cache()
            self.assertFalse(asyncio.run(ms.audit())["cached"])

    def test_unit_map_survives_db_error(self):
        with mock.patch.object(aio_db, "query", new=mock.AsyncMock(side_effect=RuntimeError("db"))):
            self.assertEqual(asyncio.run(ms._unit_map()), {})

    def test_unit_map_uses_unit_name_helper(self):
        rows = [{"name": "rosetta-backend", "unit_override": None}]
        with mock.patch.object(aio_db, "query", new=mock.AsyncMock(return_value=rows)):
            out = asyncio.run(ms._unit_map())
        from app.services.apps_service import unit_name
        self.assertEqual(out, {unit_name(rows[0]): "rosetta-backend"})

    def test_collect_runs_off_the_event_loop(self):
        """_collect 里有 0.8 秒采样窗口和全机 /proc 扫描，绝不能挡住事件循环。"""
        with mock.patch.object(ms.asyncio, "to_thread",
                               new=mock.AsyncMock(return_value={"mem": {"ok": True}})) as to_thread:
            asyncio.run(ms.audit(refresh=True))
        to_thread.assert_awaited()


class DoctorTests(unittest.TestCase):
    def test_clean_pass(self):
        d = {"mem": _mem(), "oom": _oom(), "findings": [], "counts": {"info": 0}}
        item = ms.doctor_item(d)
        self.assertEqual(item["status"], "pass")
        self.assertIn("已用 25%", item["detail"])

    def test_info_only_still_pass(self):
        d = {"mem": _mem(), "oom": _oom(),
             "findings": [{"id": "commit", "severity": "info", "title": "承诺超上限"}],
             "counts": {"info": 1}}
        item = ms.doctor_item(d)
        self.assertEqual(item["status"], "pass")
        self.assertIn("1 条只是让你知道", item["detail"])

    def test_pressure_is_warn_not_fail(self):
        d = {"mem": _mem(), "oom": _oom(),
             "findings": [{"id": "mem-used", "severity": "risk", "title": "内存已用 92%"},
                          {"id": "psi-active", "severity": "warn", "title": "有停顿"}],
             "counts": {"risk": 1, "warn": 1}}
        item = ms.doctor_item(d)
        self.assertEqual(item["status"], "warn")
        self.assertIn("另有 1 项", item["detail"])

    def test_oom_kill_is_the_only_fail(self):
        d = {"mem": _mem(),
             "oom": _oom(kernel_total=2, unit_kills={"a.service": 1}),
             "findings": [{"id": "oom-kills", "severity": "risk", "title": "内核 OOM 杀过 2 次进程"}],
             "counts": {}}
        item = ms.doctor_item(d)
        self.assertEqual(item["status"], "fail")
        self.assertIn("3 个进程", item["detail"])
        self.assertIn("内核 OOM 杀过 2 次进程", item["detail"])

    def test_unreadable_mem_is_warn_with_fix(self):
        item = ms.doctor_item({"mem": {"ok": False, "reason": "读不到 /proc/meminfo"}})
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["fix"], "choyeonctl memory")

    def test_never_fails_without_oom(self):
        d = {"mem": _mem(used_pct=99, swap_pct=99), "oom": _oom(),
             "findings": [{"id": "mem-used", "severity": "risk", "title": "快满了"}],
             "counts": {"risk": 1}}
        self.assertNotEqual(ms.doctor_item(d)["status"], "fail")


class CliTextTests(unittest.TestCase):
    def _payload(self, **over):
        d = {"as_of": "2026-10-12T20:00:00+00:00", "cached": False, "self_unit": "choyeon-panel.service",
             "mem": _mem(), "pressure": _press(total=82192998), "rates": _rates(),
             "tunables": _tun(), "units": _units(), "procs": {"ok": True, "items": [], "count": 140,
                                                              "scanned": 140, "budget_hit": False},
             "oom": _oom(), "findings": [], "counts": {"risk": 0, "warn": 0, "info": 0},
             "notes": list(ms.NOTES)}
        d.update(over)
        return d

    def test_clean_machine(self):
        text = cli._memory_text(self._payload())
        self.assertIn("内存压力与 OOM 对账（只读", text)
        self.assertIn("总量 4.0GB", text)
        self.assertIn("阻塞 82 秒", text)
        self.assertIn("OOM：自启动以来没有杀过进程", text)
        self.assertIn("没有内存压力判词", text)
        self.assertIn("检查时间", text)
        self.assertNotIn("None", text)

    def test_cached_flag(self):
        self.assertIn("（命中缓存）", cli._memory_text(self._payload(cached=True)))

    def test_no_swap_says_no_buffer(self):
        text = cli._memory_text(self._payload(mem=_mem(swap_total=0, swap_pct=None,
                                                       swap_used=0)))
        self.assertIn("没有启用 swap", text)
        self.assertNotIn("None", text)

    def test_rates_none_is_not_zero(self):
        text = cli._memory_text(self._payload(rates=_rates(ok=False, swap_in=None, swap_out=None,
                                                            direct_reclaim=None, allocstall=None,
                                                            majfault=None, oom_kill_delta=None)))
        self.assertIn("本轮没有采到有效窗口", text)

    def test_psi_unreadable(self):
        text = cli._memory_text(self._payload(pressure={"ok": False, "reason": "内核未开 CONFIG_PSI"}))
        self.assertIn("内核未开 CONFIG_PSI", text)

    def test_units_table(self):
        rows = _units(
            _unit(unit="panel-rosetta-backend.service", app="rosetta-backend",
                  current=383795072, pct_of_host=9),
            _unit(unit="system-postgresql.slice", current=106700000, kind="slice"),
            _unit(unit="choyeon-panel.service", current=42000000, max=536870912,
                  pct_of_cap=8, pids=4, pids_max=256, **{"self": True}),
            seen=22, omitted=9, managed=1)
        text = cli._memory_text(self._payload(units=rows))
        self.assertIn("占内存最多的 3 个单元（system.slice 共 22 个，另有 9 个更小的没列）", text)
        self.assertIn("[rosetta-backend]", text)
        self.assertIn("[面板自己]", text)
        self.assertIn("分组，含子单元", text)
        self.assertIn("自己上限 8%", text)
        self.assertNotIn("None", text)

    def test_units_unreadable(self):
        text = cli._memory_text(self._payload(
            units={"ok": False, "reason": "不是 cgroup v2", "items": []}))
        self.assertIn("单元表：不是 cgroup v2", text)

    def test_procs_table(self):
        procs = {"ok": True, "count": 140, "scanned": 140, "budget_hit": False, "items": [
            {"pid": 200, "comm": "python", "rss": 180600000, "unit": "panel-rosetta-backend.service",
             "kind": "service", "app": "rosetta-backend"},
            {"pid": 201, "comm": "node", "rss": 32000000, "unit": None, "kind": "none", "app": None},
            {"pid": 202, "comm": "sh", "rss": 1000000, "unit": "session-1.scope", "kind": "scope",
             "app": None}]}
        text = cli._memory_text(self._payload(procs=procs))
        self.assertIn("占内存最多的 3 个进程（全机 140 个）", text)
        self.assertIn("归不到单元", text)
        self.assertIn("session-1.scope", text)

    def test_procs_partial_scan(self):
        procs = {"ok": True, "count": 500, "scanned": 400, "budget_hit": True, "items": []}
        text = cli._memory_text(self._payload(procs=procs))
        self.assertIn("这一轮没扫完", text)

    def test_procs_unreadable(self):
        text = cli._memory_text(self._payload(procs={"ok": False, "reason": "读不到 /proc",
                                                     "items": []}))
        self.assertIn("进程表：读不到 /proc", text)

    def test_oom_events_and_reason(self):
        oom = _oom(kernel_total=2, source="counter-only",
                   reason="取不到 kernel 日志（TimeoutExpired）",
                   events=[{"pid": 4242, "comm": "node", "at": "2026-10-11T03:12:44", "line": "x"},
                           {"pid": None, "comm": None, "at": "y", "line": "Out of memory: ?" * 40}],
                   unit_kills={"a.service": 1})
        text = cli._memory_text(self._payload(oom=oom))
        self.assertIn("杀掉过 2 个进程", text)
        self.assertIn("上限另杀 1 次", text)
        self.assertIn("node(4242)", text)
        self.assertIn("Out of memory", text)
        self.assertIn("TimeoutExpired", text)

    def test_findings_printed_with_severity_and_fix(self):
        d = self._payload(
            findings=[{"id": "mem-used", "severity": "risk", "title": "内存已用 92%",
                       "detail": "只剩 5% 能腾出来", "fix": "choyeonctl memory"},
                      {"id": "commit", "severity": "info", "title": "承诺超上限",
                       "detail": "", "fix": ""}],
            counts={"risk": 1, "warn": 0, "info": 1})
        text = cli._memory_text(d)
        self.assertIn("[要紧] 内存已用 92%", text)
        self.assertIn("只剩 5% 能腾出来", text)
        self.assertIn("处置：choyeonctl memory", text)
        self.assertIn("知道就好", text)
        self.assertNotIn("None", text)

    def test_mem_unreadable(self):
        text = cli._memory_text(self._payload(mem={"ok": False, "reason": "读不到 MemTotal"}))
        self.assertIn("读不到 MemTotal", text)

    def test_no_destructive_command_in_output(self):
        d = self._payload(
            mem=_mem(used_pct=92, swap_pct=85, commit_limit=1 * GIB, committed=3 * GIB),
            oom=_oom(kernel_total=1, source="journal",
                     events=[{"pid": 1, "comm": "node", "at": "x", "line": "y"}]),
            findings=[{"id": "oom-kills", "severity": "risk", "title": "杀过",
                       "detail": "看日志", "fix": "journalctl -k -b | grep -iE 'out of memory' | tail -20"}],
            counts={"risk": 1})
        text = cli._memory_text(d)
        fixes = "\n".join(ln.split("处置：", 1)[1] for ln in text.splitlines() if "处置：" in ln)
        self.assertIn("journalctl", fixes)
        for word in ("rm -rf", "kill ", "sysctl -w", "systemctl restart", "set-property",
                     "truncate", "--vacuum"):
            self.assertNotIn(word, fixes)


class CliCommandTests(unittest.TestCase):
    def test_success_path(self):
        payload = {"mem": {"ok": True}, "units": {"ok": True, "items": []}, "procs": {"ok": True},
                   "pressure": {"ok": True}, "rates": {"ok": False}, "oom": {}, "findings": [],
                   "counts": {}, "notes": [], "as_of": "x"}
        with mock.patch.object(cli.memory_service, "audit",
                               new=mock.AsyncMock(return_value=payload)):
            out, _err, code = _run(cli.cmd_memory(_Args(refresh=False)))
        self.assertIsNone(code)
        self.assertIn("内存压力与 OOM 对账", out)

    def test_json_mode_emits_object(self):
        payload = {"mem": {"ok": True, "used_pct": 33}, "counts": {}}
        with mock.patch.object(cli.memory_service, "audit",
                               new=mock.AsyncMock(return_value=payload)):
            out, _err, code = _run(cli.cmd_memory(_Args(refresh=False)), json_mode=True)
        self.assertIsNone(code)
        self.assertIn('"used_pct": 33', out)

    def test_unreadable_mem_is_precondition_exit(self):
        payload = {"mem": {"ok": False, "reason": "读不到 /proc/meminfo"}}
        with mock.patch.object(cli.memory_service, "audit",
                               new=mock.AsyncMock(return_value=payload)):
            _out, err, code = _run(cli.cmd_memory(_Args(refresh=False)))
        self.assertEqual(code, cli.EXIT_PRECONDITION)
        self.assertIn("读不到 /proc/meminfo", err)

    def test_audit_refresh_flag_passed_through(self):
        audit = mock.AsyncMock(return_value={"mem": {"ok": True}, "units": {"ok": True},
                                             "procs": {"ok": True}, "pressure": {"ok": True},
                                             "rates": {"ok": False}, "oom": {}, "findings": [],
                                             "counts": {}, "notes": [], "as_of": "x"})
        with mock.patch.object(cli.memory_service, "audit", new=audit):
            _run(cli.cmd_memory(_Args(refresh=True)))
        audit.assert_awaited_once_with(refresh=True)


class SchemaContractTests(unittest.TestCase):
    def _schema(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(cli.cmd_schema(_Args()))

    def test_memory_in_command_list(self):
        names = [c["name"] for c in self._schema()["commands"]]
        self.assertIn("memory", names)

    def test_memory_entry_shape(self):
        entry = [c for c in self._schema()["commands"] if c["name"] == "memory"][0]
        self.assertEqual(entry["args"], ["--refresh"])
        self.assertIn("只读", entry["desc"])
        self.assertGreaterEqual(len(entry["notes"]), 6)
        joined = " ".join(entry["notes"])
        self.assertIn("MemAvailable", joined)
        self.assertIn("oom_kill", joined)
        self.assertIn("退出码 3", joined)
        self.assertIn("不会折成 0", joined)

    def test_schema_declares_readonly_scope(self):
        entry = [c for c in self._schema()["commands"] if c["name"] == "memory"][0]
        blob = entry["desc"] + " ".join(entry["notes"])
        self.assertIn("只读", entry["desc"])
        self.assertIn("不 kill 进程", entry["desc"])
        for promise in ("自动修复", "一键", "帮你重启"):
            self.assertNotIn(promise, blob, f"契约里出现了写入承诺：{promise}")


class ParserTests(unittest.TestCase):
    def test_memory_subcommand_parses(self):
        args = cli.build_parser().parse_args(["memory"])
        self.assertIs(args.fn, cli.cmd_memory)
        self.assertFalse(args.refresh)

    def test_refresh_flag(self):
        args = cli.build_parser().parse_args(["memory", "--refresh"])
        self.assertTrue(args.refresh)

    def test_help_text_declares_readonly(self):
        """`choyeonctl -h` 的那一行就要说清只读——管理员不用先跑一次才知道会不会动手。"""
        lines = [ln for ln in cli.build_parser().format_help().splitlines()
                 if ln.strip().startswith("memory")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("只读", lines[0])


class MetaWiringTests(unittest.TestCase):
    def test_endpoint_registered(self):
        self.assertIn("/api/memory", [r.path for r in meta.router.routes])

    def test_doctor_item_present(self):
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("memory", ids)
        self.assertEqual(meta._CHECKS[ids.index("memory")][1], "内存压力")
        self.assertLess(ids.index("storage"), ids.index("memory"), "内存压力排在存储占用之后")

    def test_not_one_click_fixable(self):
        """一键修复白名单里不许出现内存：加上限、重启服务都是容量决策。"""
        self.assertNotIn("memory", meta.FIXABLE)

    def test_check_memory_uses_audit_and_doctor(self):
        seen = {}

        async def audit(refresh=False):
            seen["refresh"] = refresh
            return {"mem": {"ok": True, "used_pct": 33}, "oom": {"kernel_total": 0},
                    "findings": [], "counts": {"info": 0}}

        with mock.patch.object(meta.memory_service, "audit", audit):
            item = asyncio.run(meta._check_memory())
        self.assertEqual(item["status"], "pass")
        self.assertIn("refresh", seen)

    def test_endpoint_forwards_refresh(self):
        calls = []

        async def audit(refresh=False):
            calls.append(refresh)
            return {"mem": {"ok": True}}

        with mock.patch.object(meta.memory_service, "audit", audit):
            asyncio.run(meta.memory())
            asyncio.run(meta.memory(refresh=1))
        self.assertEqual(calls, [False, True])


class RouteAccessTests(unittest.TestCase):
    """/api/memory 是只读探测：viewer 能看，匿名不能看。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        async def audit(refresh=False):
            return {"as_of": "x", "cached": True, "mem": {"ok": True},
                    "pressure": {"ok": False}, "rates": {"ok": False}, "tunables": {"ok": True},
                    "units": {"ok": True, "items": []}, "procs": {"ok": True, "items": []},
                    "oom": {"kernel_total": 0}, "findings": [], "counts": {"risk": 0},
                    "notes": [], "self_unit": None}

        p = mock.patch.object(meta.memory_service, "audit", audit)
        p.start()
        self.addCleanup(p.stop)
        self.user = "p14a" + uuid.uuid4().hex[:10]
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "viewer"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.user, 'viewer')}"}

    def test_viewer_can_read(self):
        r = self.client.get("/api/memory", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("mem", r.json())

    def test_anonymous_denied(self):
        self.assertIn(self.client.get("/api/memory").status_code, (401, 403))


class DocsTests(unittest.TestCase):
    def test_agents_md_documents_command_and_item_count(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("`memory`", text)
        self.assertTrue(f"当前 {len(meta._CHECKS)} 项" in text,
                        "AGENTS.md 的自检项数与代码不一致")

    def test_readme_documents_command_and_item_count(self):
        text = (Path(cli.ROOT) / "README.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl memory", text)
        self.assertIn(f"共 {len(meta._CHECKS)} 项只读检查", text)
        self.assertNotIn("16 项", text, "README 里还留着上一批的项数")

    def test_security_md_item_count(self):
        text = (Path(cli.ROOT) / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(meta._CHECKS)} 项只读自检", text)

    def test_agents_md_memory_row_declares_readonly(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `memory`"))
        for phrase in ("不 kill 进程", "不改 sysctl", "MemoryMax", "退出码 3"):
            self.assertIn(phrase, row)


class NotesTests(unittest.TestCase):
    def test_notes_cover_the_three_limits(self):
        joined = " ".join(ms.NOTES)
        self.assertIn("0.8 秒", joined)
        self.assertIn("MemAvailable", joined)
        self.assertIn("不要相加", joined)
        self.assertIn("cgroup 需 v2", joined)
        self.assertIn("不改 sysctl", joined)
        self.assertIn("slice", joined)

    def test_thresholds_live_in_one_place(self):
        """判词里不许出现裸阈值数字，否则改一处漏一处。"""
        src = (Path(ms.__file__)).read_text(encoding="utf-8")
        body = src.split("def _findings", 1)[1].split("def _decorate_units", 1)[0]
        for token in ("80%", "90%", "95%", "5.0", "256"):
            self.assertNotIn(token, body, f"_findings 里出现了裸阈值 {token}")


if __name__ == "__main__":
    unittest.main()
