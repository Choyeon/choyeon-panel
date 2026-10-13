"""批次⑯A：负载与 CPU 归因对账（load_service）+ 自检第 19 项 + `choyeonctl load` + `/api/load`。

这一层锁的是一件很容易读错的事：**load average 高到底意味着什么**。所以用例全部围绕
「同一个数字在两种相反的原因下必须给出相反的处置」：

- `/proc/loadavg` 的第 4 个字段是「可运行任务数 / 已注册任务总数」。把它当 blocked 是这一层
  最贵的一次误读——本批在生产机上就撞上过一次：2 核机器 load 0.05，面板会报「227 个任务正卡在
  等 IO」（整机线程数）。真正的 blocked 只有 `/proc/stat` 的 `procs_blocked` 一处，
  `LoadavgTests` 与 `CollectTests` 各钉一遍，且要求 loadavg 段里**不存在**同名键。
- 判据一律除以核数：`FindingsTests` 用 2 核机器上的 load 8（每核 4，要紧）与 64 核机器上的
  load 8（每核 0.125，没事）做同一份代码的两个断言。
- 「CPU 不够」与「磁盘跟不上」必须分叉：`procs_blocked` 非 0 才许走 load-io，否则走 load-cpu，
  两条判词的处置方向相反（一个查设备队列，一个查哪个进程在占核）。
- 被 CPU 配额限流是第三种忙：load 与 CPU 曲线都好看，只有 cgroup `cpu.stat` 的 `nr_throttled`
  在涨。`quota_cores` 必须由 `cpu.max` 的 quota/period 算出（80000/100000 = 0.8 核），
  窗口增量必须由两次快照相减，不能拿累计值当这一秒的限流率。
- `guest` / `guest_nice` 已计在 `user` / `nice` 里，不剔掉总账超 100%。
- 「谁在占」只列窗口增量 > 0 的进程：空闲机器上有上百个 0ms 内核线程，全列出来等于噪声。
- 读不到的节只许出 info 级判词并如实带原因，不能折成 0 冒充没事（也不能折成「没有问题」）。
- 严格只读：不改 CPUQuota=/cpu.weight、不 renice、不 kill、不 restart。用例逐条扫 findings 的
  fix，确认只用这台机器上真装了的工具（pidstat/mpstat/sar/iostat；**没有** iotop、atop）。

夹具里的 cpu 行、loadavg、PSI、cpu.max/cpu.stat、/proc/1/stat 与 cgroup 都是 2026-10-13 从
生产机（Debian 13 / 2 核 / 面板 unit 写 CPUQuota=80%，cpu.max=80000 100000、nr_throttled 84、
throttled_usec 3694975）原样抓回来的。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import io
import os
import re
import tempfile
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch16a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch16a_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import load_service as ls  # noqa: E402


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


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@contextlib.contextmanager
def fake_files(files: dict):
    """把服务里所有 `_read` 换成一张表；表里没有的路径当读不到（返回空串）。

    值给 list 时按读取次序依次发（`_collect` 会读两次 /proc/stat，两次必须是不同的数），
    发完就停在最后一个上。
    """
    seqs = {k: list(v) for k, v in files.items() if isinstance(v, list)}

    def _read(path):
        if path in seqs:
            return seqs[path].pop(0) if len(seqs[path]) > 1 else seqs[path][0]
        return files.get(path, "")

    with mock.patch.object(ls, "_read", _read):
        yield


class _FakeClock:
    """给 `load_service` 换一个会走表的单调时钟：`advance()` 一次走 `scan_sec`。

    ctxt/forks 的速率分母从标称 1 秒改成实测跨度之后（批次⑰A），夹具必须自己给出跨度，
    否则同一份 /proc 文本在快机器和慢机器上会算出两个不同的速率。
    """

    def __init__(self, scan_sec=1.0, start=1000.0):
        self.now = start
        self.scan_sec = scan_sec

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def advance(self):
        self.now += self.scan_sec


def _schema() -> dict:
    """cmd_schema 会把契约打到 stdout，这里只要它的返回值，噪声收掉。"""
    with contextlib.redirect_stdout(io.StringIO()):
        return asyncio.run(cli.cmd_schema(_Args()))


def _load_entry() -> dict:
    return [c for c in _schema()["commands"] if c["name"] == "load"][0]


def _cmd_tokens(cells) -> set[str]:
    """把命令名单元格拆成命令：`user list\\|create\\|reset` → user/list/create/reset。"""
    out: set[str] = set()
    for cell in cells:
        for tok in cell.replace("\\|", "|").split():
            out.update(x for x in tok.split("|") if x)
    return out


def _doc_cells(text: str) -> list[str]:
    """AGENTS.md 表格里第一列（反引号里的命令名）。"""
    return [ln.split("`")[1] for ln in text.splitlines()
            if ln.startswith("| `") and "|" in ln.split("`")[0]]


# --------------------------------------------------------------------------- 生产机原始夹具
STAT_REAL = """cpu  7764879 3776 1448891 203840639 93725 0 829162 1235551 0 0
cpu0 3815794 1732 711650 102480782 51679 0 111371 446708 0 0
intr 587717524 43 9 0 0 664
ctxt 497753812
btime 1790765667
processes 1256646
procs_running 1
procs_blocked 0
"""
# 同一台机器 1 秒后的第二次采样：cpu 各字段抬上去的增量合计正好 100 tick（百分比可直接核），
# ctxt 抬 510，forks 不动。`_collect` 算的是这两次之差，不是第二次那份累计值。
STAT_SECOND = """cpu  7764882 3776 1448892 203840729 93727 0 829163 1235554 0 0
cpu0 3815797 1732 711651 102480872 51681 0 111372 446711 0 0
intr 587718024 43 9 0 0 664
ctxt 497754322
btime 1790765667
processes 1256646
procs_running 1
procs_blocked 0
"""
LOADAVG_REAL = "0.05 0.08 0.20 1/227 1256624\n"
PSI_CPU_REAL = "some avg10=0.03 avg60=0.10 avg300=0.13 total=12542374460\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
PSI_IO_REAL = "some avg10=0.20 avg60=0.07 avg300=0.01 total=817351367\nfull avg10=0.19 avg60=0.05 avg300=0.01 total=551581141\n"
CG_PANEL = "/sys/fs/cgroup/system.slice/choyeon-panel.service"
CPU_MAX_PANEL = "80000 100000\n"
CPU_STAT_PANEL = ("usage_usec 19294619\nuser_usec 16389932\nsystem_usec 2904686\nnice_usec 0\n"
                  "nr_periods 10571\nnr_throttled 84\nthrottled_usec 3694975\nnr_bursts 0\nburst_usec 0\n")
PID1_STAT = ("1 (systemd) S 0 1 1 0 -1 4194560 17018141 110604360 4957 318926 226910 60989 "
             "2425599 432108 20 0 1 0 48 26091520 2581 18446744073709551615 94600351440896\n")


# --------------------------------------------------------------------------- 解析原语
class KvIntsTests(unittest.TestCase):
    def test_parses_key_value_pairs(self):
        out = ls._kv_ints(CPU_STAT_PANEL)
        self.assertEqual(out["nr_throttled"], 84)
        self.assertEqual(out["throttled_usec"], 3694975)

    def test_non_numeric_value_is_skipped_not_guessed(self):
        """值不是数字就跳过这个键：宁可缺这个数，也不要猜成 0 或无穷。"""
        out = ls._kv_ints("nr_throttled max\nburst_usec 0\n")
        self.assertEqual(out, {"burst_usec": 0})

    def test_cpu_max_is_not_a_key_value_file(self):
        """cpu.max 的 `max 100000` 里 max 是第一个字段（配额），不是键名。

        走 _kv_ints 只会读出 {'max': 100000} 这种没意义的数，所以 cpu.max 由 _cpu_max
        按位置解析；这条用例钉住「别把 cpu.max 当 kv 文件喂进来」这个前提。
        """
        self.assertEqual(ls._kv_ints("max 100000\n"), {"max": 100000})
        with fake_files({"/a": "max 100000\n"}):
            self.assertEqual(ls._cpu_max("/a"), (None, 100000))

    def test_keys_filter_narrows_the_set(self):
        out = ls._kv_ints(CPU_STAT_PANEL, ("nr_periods", "nr_throttled"))
        self.assertEqual(sorted(out), ["nr_periods", "nr_throttled"])

    def test_three_field_line_is_not_a_pair(self):
        self.assertEqual(ls._kv_ints("intr 587717524 43 9\n"), {})


class StatCpuTests(unittest.TestCase):
    def test_real_cpu_line_maps_every_field(self):
        with fake_files({ls.PROC_STAT: STAT_REAL}):
            out = ls._stat_cpu()
        self.assertTrue(out["ok"])
        self.assertEqual(out["ticks"]["user"], 7764879)
        self.assertEqual(out["ticks"]["steal"], 1235551)
        self.assertEqual(out["ticks"]["idle"], 203840639)

    def test_counters_and_blocked_come_from_this_file(self):
        with fake_files({ls.PROC_STAT: STAT_REAL}):
            out = ls._stat_cpu()
        self.assertEqual(out["ctxt"], 497753812)
        self.assertEqual(out["forks"], 1256646)
        self.assertEqual(out["procs_running"], 1)
        self.assertEqual(out["procs_blocked"], 0)

    def test_blocked_is_taken_verbatim(self):
        """/proc/stat 是 blocked 的唯一来源：非 0 时必须原样带出去。"""
        with fake_files({ls.PROC_STAT: STAT_REAL.replace("procs_blocked 0", "procs_blocked 7")}):
            self.assertEqual(ls._stat_cpu()["procs_blocked"], 7)

    def test_missing_cpu_line_is_not_ok(self):
        with fake_files({ls.PROC_STAT: "ctxt 1\n"}):
            out = ls._stat_cpu()
        self.assertFalse(out["ok"])
        self.assertIn("cpu", out["reason"])

    def test_truncated_cpu_line_is_ignored(self):
        """老内核尾部字段会缺；不足 5 个字段的 cpu 行不能当数据（宁可整节读不到）。"""
        with fake_files({ls.PROC_STAT: "cpu 1 2 3\nctxt 1\n"}):
            self.assertFalse(ls._stat_cpu()["ok"])


class LoadavgTests(unittest.TestCase):
    def test_real_line_splits_runnable_from_total_tasks(self):
        with fake_files({ls.PROC_LOADAVG: LOADAVG_REAL}):
            out = ls._loadavg()
        self.assertEqual((out["one"], out["five"], out["fifteen"]), (0.05, 0.08, 0.20))
        self.assertEqual(out["runnable"], 1)
        self.assertEqual(out["total_tasks"], 227)
        self.assertEqual(out["last_pid"], "1256624")

    def test_there_is_no_blocked_field_here(self):
        """本批最贵的一次误读：1/227 里的 227 是全机任务数，不是卡在 IO 的任务数。

        这里必须连同名键都不出现，免得下一个读的人（或下一版判词）又把它当 blocked 用。
        """
        with fake_files({ls.PROC_LOADAVG: LOADAVG_REAL}):
            out = ls._loadavg()
        self.assertNotIn("procs_blocked", out)
        self.assertNotIn("blocked", out)
        self.assertLess(out["runnable"], out["total_tasks"])

    def test_short_line_is_not_ok(self):
        with fake_files({ls.PROC_LOADAVG: "0.05 0.08 0.20\n"}):
            out = ls._loadavg()
        self.assertFalse(out["ok"])
        self.assertIsNone(out["one"])

    def test_non_numeric_task_pair_does_not_crash(self):
        with fake_files({ls.PROC_LOADAVG: "0.1 0.2 0.3 x/y 9\n"}):
            out = ls._loadavg()
        self.assertTrue(out["ok"])
        self.assertIsNone(out["runnable"])
        self.assertIsNone(out["total_tasks"])


class PsiTests(unittest.TestCase):
    def test_real_cpu_and_io_files_parse(self):
        with fake_files({ls.PSI_PATHS["cpu"]: PSI_CPU_REAL, ls.PSI_PATHS["io"]: PSI_IO_REAL}):
            out = ls._psi()
        self.assertTrue(out["ok"])
        self.assertEqual(out["controllers"]["cpu"]["some"]["avg10"], 0.03)
        self.assertEqual(out["controllers"]["io"]["full"]["avg300"], 0.01)
        self.assertEqual(out["controllers"]["io"]["some"]["total"], 817351367)

    def test_missing_io_still_ok_with_cpu(self):
        with fake_files({ls.PSI_PATHS["cpu"]: PSI_CPU_REAL}):
            out = ls._psi()
        self.assertTrue(out["ok"])
        self.assertNotIn("io", out["controllers"])

    def test_no_psi_at_all_is_unreadable_not_zero(self):
        """老内核没编译 CONFIG_PSI 时文件根本不存在——那是「不知道」，不能折成 0% 压力。"""
        with fake_files({}):
            out = ls._psi()
        self.assertFalse(out["ok"])
        self.assertIn("CONFIG_PSI", out["reason"])

    def test_malformed_row_is_skipped(self):
        with fake_files({ls.PSI_PATHS["cpu"]: "some avg10=1.0\n", ls.PSI_PATHS["io"]: ""}):
            self.assertFalse(ls._psi()["ok"])


class SplitPctTests(unittest.TestCase):
    def test_guest_is_not_counted_twice(self):
        """guest 已经计在 user 里：user=100 guest=100 时 user 必须是 100%，不是 200%。"""
        pct = ls._split_pct({"user": 100, "guest": 100, "idle": 0})
        self.assertEqual(pct["user"], 100.0)
        self.assertNotIn("guest", pct)

    def test_guest_nice_is_not_counted_twice(self):
        pct = ls._split_pct({"nice": 50, "guest_nice": 50, "idle": 50})
        self.assertEqual(pct["nice"], 50.0)
        self.assertEqual(pct["idle"], 50.0)

    def test_zero_window_yields_empty_dict(self):
        self.assertEqual(ls._split_pct({k: 0 for k in ls.CPU_FIELDS}), {})

    def test_percentages_sum_to_hundred(self):
        """生产机那份 cpu 行，再给 guest / guest_nice 填上非零值：重复计入就会超过 100%。"""
        ticks = {"user": 7764879, "nice": 3776, "system": 1448891, "idle": 203840639,
                 "iowait": 93725, "irq": 0, "softirq": 829162, "steal": 1235551,
                 "guest": 7764879, "guest_nice": 3776}
        pct = ls._split_pct(ticks)
        self.assertAlmostEqual(sum(pct.values()), 100.0, places=1)
        self.assertEqual(sorted(pct), ["idle", "iowait", "irq", "nice", "softirq", "steal", "system", "user"])


class PidStatTests(unittest.TestCase):
    def test_real_pid1_line(self):
        with fake_files({"/proc/1/stat": PID1_STAT}):
            out = ls._pid_stat(1)
        self.assertEqual(out["comm"], "systemd")
        self.assertEqual(out["state"], "S")
        self.assertEqual(out["cpu_ticks"], 226910 + 60989)
        self.assertEqual(out["starttime"], 48)

    def test_comm_with_parentheses_and_spaces(self):
        """comm 可以带空格甚至括号，只能按最后一个 `)` 切，按第一个切会把字段整体挪位。"""
        line = '42 (weird (name)) D 1 42 42 0 -1 2 3 4 5 6 700 8 9 10 11 12 13 0 55 6 7\n'
        with fake_files({"/proc/42/stat": line}):
            out = ls._pid_stat(42)
        self.assertEqual(out["comm"], "weird (name)")
        self.assertEqual(out["state"], "D")
        self.assertEqual(out["cpu_ticks"], 700 + 8)
        self.assertEqual(out["starttime"], 55)

    def test_unreadable_pid_returns_none(self):
        with fake_files({}):
            self.assertIsNone(ls._pid_stat(999999))

    def test_truncated_line_returns_none(self):
        with fake_files({"/proc/7/stat": "7 (x) S 1\n"}):
            self.assertIsNone(ls._pid_stat(7))


class UnitOfTests(unittest.TestCase):
    def test_service_slice_line_wins(self):
        with fake_files({"/proc/9/cgroup": "0::/system.slice/postgresql@17-main.service\n"}):
            self.assertEqual(ls._unit_of(9), "postgresql@17-main.service")

    def test_non_service_scope_is_kept_as_tail(self):
        """init.scope、session-4618.scope 也值得显示：它们说明这进程不归任何 .service 管。"""
        with fake_files({"/proc/1/cgroup": "0::/init.scope\n"}):
            self.assertEqual(ls._unit_of(1), "init.scope")

    def test_bare_slice_is_empty(self):
        with fake_files({"/proc/1/cgroup": "0::/system.slice\n"}):
            self.assertEqual(ls._unit_of(1), "")

    def test_nested_path_takes_deepest_service(self):
        with fake_files({"/proc/5/cgroup": "5:cpuacct,cpu/cpuset:/system.slice/nginx.service\n"
                                           "0::/system.slice/nginx.service\n"}):
            self.assertEqual(ls._unit_of(5), "nginx.service")

    def test_unreadable_is_empty_not_kernel(self):
        with fake_files({}):
            self.assertEqual(ls._unit_of(2), "")


class WchanTests(unittest.TestCase):
    def test_real_wchan(self):
        with fake_files({"/proc/1/wchan": "do_epoll_wait", "/proc/1/syscall": "232 0x4\n"}):
            self.assertEqual(ls._wchan(1), "do_epoll_wait")

    def test_zero_wchan_falls_back_to_running(self):
        """进程在跑的时候 wchan 是 0，`/proc/<pid>/syscall` 第一列才是 running。"""
        with fake_files({"/proc/1/wchan": "0", "/proc/1/syscall": "running\n"}):
            self.assertEqual(ls._wchan(1), "running")

    def test_nothing_readable_is_empty(self):
        with fake_files({}):
            self.assertEqual(ls._wchan(1), "")


# --------------------------------------------------------------------------- 进程采样
def _stat_line(state, utime, stime, comm="app", starttime=48):
    fields = [state, "1", "1", "1", "0", "-1", "0"] + ["0"] * 4
    fields += [str(utime), str(stime)] + ["0"] * 5
    fields.insert(19, str(starttime))
    return f"1 ({comm}) " + " ".join(fields) + "\n"


class ScanProcsTests(unittest.TestCase):
    def _run(self, prev, cur, pids=(1, 2), hz=100, deadline=None, units=None, wchans=None):
        seen = {}
        units = units or {}
        wchans = wchans or {}

        def fake_pid_stat(pid):
            n = seen.get(pid, 0)
            seen[pid] = n + 1
            table = prev if n == 0 else cur
            raw = table.get(pid)
            if raw is None:
                return None
            comm, state, utime, stime = raw
            return {"comm": comm, "state": state, "cpu_ticks": utime + stime, "starttime": 48}

        with mock.patch.object(ls.os, "listdir", lambda p: [str(x) for x in pids]), \
             mock.patch.object(ls, "_pid_stat", fake_pid_stat), \
             mock.patch.object(ls, "_unit_of", lambda pid: units.get(pid, "")), \
             mock.patch.object(ls, "_wchan", lambda pid: wchans.get(pid, "")), \
             mock.patch.object(ls.time, "sleep", lambda s: None):
            return ls._scan_procs(hz, deadline if deadline is not None else ls.time.monotonic() + 100)

    def test_window_delta_not_lifetime_average(self):
        """ps 的 %CPU 是全生命周期平均；这里必须是两次快照相减。"""
        out = self._run({1: ("redis", "S", 1000, 10)}, {1: ("redis", "S", 1009, 12)})
        self.assertEqual(out["busy"][0]["cpu_ms"], 110)
        self.assertEqual(out["busy"][0]["cpu_total_ms"], 10210)

    def test_idle_process_is_not_listed_as_busy(self):
        out = self._run({1: ("kworker", "I", 5, 5)}, {1: ("kworker", "I", 5, 5)})
        self.assertEqual(out["busy"], [])
        self.assertEqual(out["scanned"], 1)
        self.assertEqual(out["states"], {"I": 1})

    def test_process_that_died_mid_window_is_skipped_entirely(self):
        out = self._run({1: ("a", "S", 10, 0), 2: ("b", "S", 20, 0)}, {1: ("a", "S", 20, 0)}, pids=(1, 2))
        self.assertEqual(out["scanned"], 1)
        self.assertNotIn(2, [r["pid"] for r in out["busy"]])

    def test_d_state_sorts_before_r_and_carries_wchan(self):
        out = self._run({1: ("rproc", "R", 100, 0), 2: ("dproc", "D", 1, 0)},
                        {1: ("rproc", "R", 900, 0), 2: ("dproc", "D", 5, 0)},
                        wchans={2: "wait_on_page_bit"})
        self.assertEqual(out["blocked_total"], 2)
        self.assertEqual(out["blocked"][0]["comm"], "dproc")
        self.assertEqual(out["blocked"][0]["wchan"], "wait_on_page_bit")

    def test_unit_is_attached_to_rows(self):
        out = self._run({1: ("python", "S", 10, 0)}, {1: ("python", "S", 30, 0)}, units={1: "choyeon-panel.service"})
        self.assertEqual(out["busy"][0]["unit"], "choyeon-panel.service")

    def test_budget_hit_is_reported_not_silenced(self):
        out = self._run({1: ("a", "S", 1, 0)}, {1: ("a", "S", 2, 0)}, deadline=ls.time.monotonic() - 10)
        self.assertTrue(out["budget_hit"])
        self.assertTrue(out["ok"], "截断是「少看了几个」，不是「这一节读不到」")

    def test_no_proc_at_all_is_not_ok(self):
        with mock.patch.object(ls.os, "listdir", side_effect=OSError("gone")):
            out = ls._scan_procs(100, ls.time.monotonic() + 1)
        self.assertFalse(out["ok"])
        self.assertIn("/proc", out["reason"])

    def test_busy_is_capped(self):
        prev = {i: (f"p{i}", "S", 0, 0) for i in range(1, 40)}
        cur = {i: (f"p{i}", "S", 100, 0) for i in range(1, 40)}
        out = self._run(prev, cur, pids=tuple(range(1, 40)))
        self.assertEqual(out["busy_total"], 39)
        self.assertEqual(len(out["busy"]), ls.MAX_BLOCKED * 2)


# --------------------------------------------------------------------------- 配额与限流
class CpuMaxTests(unittest.TestCase):
    def test_quota_and_period(self):
        with fake_files({"/a": CPU_MAX_PANEL}):
            self.assertEqual(ls._cpu_max("/a"), (80000, 100000))

    def test_max_means_no_cap(self):
        with fake_files({"/a": "max 100000\n"}):
            self.assertEqual(ls._cpu_max("/a"), (None, 100000))

    def test_garbage_is_not_read_as_zero_or_unlimited(self):
        with fake_files({"/a": ""}):
            self.assertEqual(ls._cpu_max("/a"), (None, None))
        with fake_files({"/a": "80000 notanumber\n"}):
            self.assertEqual(ls._cpu_max("/a"), (80000, None))


class QuotaScanTests(unittest.TestCase):
    DIRS = [CG_PANEL, "/sys/fs/cgroup/system.slice/nginx.service",
            "/sys/fs/cgroup/system.slice/redis-server.service"]

    def _scan(self, files):
        with mock.patch.object(ls, "_cgroup_dirs", lambda: list(self.DIRS)), fake_files(files):
            return ls._quota_scan("choyeon-panel.service", ls.time.monotonic() + 5)

    def test_capped_unit_is_listed_with_core_count(self):
        out = self._scan({f"{CG_PANEL}/cpu.max": CPU_MAX_PANEL, f"{CG_PANEL}/cpu.stat": CPU_STAT_PANEL,
                          f"{CG_PANEL}/cpu.weight": "100\n"})
        snap = out["first"][CG_PANEL]
        self.assertEqual(snap["quota_us"], 80000)
        self.assertEqual(snap["period_us"], 100000)
        self.assertEqual(snap["weight"], 100)
        self.assertEqual(snap["nr_throttled"], 84)

    def test_uncapped_and_never_throttled_is_dropped(self):
        """没配额又没被限流过的 cgroup 列出来只是噪声（这台机器 35 个 cgroup 里就 1 个有配额）。"""
        out = self._scan({f"{CG_PANEL}/cpu.max": CPU_MAX_PANEL, f"{CG_PANEL}/cpu.stat": CPU_STAT_PANEL,
                          "/sys/fs/cgroup/system.slice/nginx.service/cpu.max": "max 100000\n",
                          "/sys/fs/cgroup/system.slice/nginx.service/cpu.stat":
                              "usage_usec 1\nnr_periods 10\nnr_throttled 0\n"})
        self.assertNotIn("/sys/fs/cgroup/system.slice/nginx.service", out["first"])

    def test_uncapped_but_throttled_before_is_kept(self):
        """cpu.max 是 max 而 nr_throttled 非 0：说明有人用别的方式（父 cgroup / 老内核）限过它。"""
        redis = "/sys/fs/cgroup/system.slice/redis-server.service"
        out = self._scan({f"{CG_PANEL}/cpu.max": CPU_MAX_PANEL, f"{CG_PANEL}/cpu.stat": CPU_STAT_PANEL,
                          f"{redis}/cpu.max": "max 100000\n",
                          f"{redis}/cpu.stat": "nr_periods 5\nnr_throttled 3\nthrottled_usec 900\n"})
        self.assertIn(redis, out["first"])
        self.assertIsNone(out["first"][redis]["quota_us"])

    def test_missing_cpu_stat_still_counts_as_capped(self):
        nginx = "/sys/fs/cgroup/system.slice/nginx.service"
        out = self._scan({f"{CG_PANEL}/cpu.max": CPU_MAX_PANEL, f"{CG_PANEL}/cpu.stat": CPU_STAT_PANEL,
                          f"{nginx}/cpu.max": "50000 100000\n"})
        self.assertIn(nginx, out["first"])
        self.assertIsNone(out["first"][nginx]["nr_throttled"])

    def test_dirs_count_is_reported(self):
        out = self._scan({})
        self.assertEqual(out["dirs"], 3)
        self.assertTrue(out["ok"])


class QuotaSecondPassTests(unittest.TestCase):
    OTHER = "/sys/fs/cgroup/system.slice/redis-server.service"

    def _prev(self, quota=80000, periods=10571, throttled=84, usec=3694975, usage=19294619):
        return {"quota_us": quota, "period_us": 100000, "weight": 100, "nr_periods": periods,
                "nr_throttled": throttled, "throttled_usec": usec, "usage_usec": usage}

    def _run(self, cur_text, prev=None, self_unit="choyeon-panel.service"):
        first = {CG_PANEL: prev or self._prev()}
        with fake_files({f"{CG_PANEL}/cpu.stat": cur_text}):
            return ls._quota_second_pass(first, self_unit)

    def test_window_is_a_difference_not_the_running_total(self):
        cur = CPU_STAT_PANEL.replace("nr_periods 10571", "nr_periods 10577").replace("nr_throttled 84",
                                                                                     "nr_throttled 85")
        row = self._run(cur)[0]
        self.assertEqual(row["window_periods"], 6)
        self.assertEqual(row["window_throttled"], 1)
        self.assertEqual(row["window_pct"], 16.7)
        self.assertEqual(row["total_throttled"], 84, "累计值来自首次快照，不能被窗口改掉")

    def test_quota_cores_from_cpu_max(self):
        row = self._run(CPU_STAT_PANEL)[0]
        self.assertEqual(row["quota_cores"], 0.8)
        self.assertTrue(row["capped"])
        self.assertTrue(row["self"])

    def test_self_flag_follows_the_unit_being_reported(self):
        row = self._run(CPU_STAT_PANEL, self_unit="redis-server.service")[0]
        self.assertFalse(row["self"])

    def test_usage_delta_is_reported(self):
        cur = CPU_STAT_PANEL.replace("usage_usec 19294619", "usage_usec 19299536")
        row = self._run(cur)[0]
        self.assertEqual(row["window_usage_usec"], 4917)

    def test_counter_reset_or_missing_snapshot_clamps_at_zero(self):
        """窗口内数值比首次快照小（cgroup 被重建）只能说明读数不可信，相减不能给负数。"""
        row = self._run(CPU_STAT_PANEL)[0]
        self.assertGreaterEqual(row["window_periods"], 0)
        cur = CPU_STAT_PANEL.replace("nr_periods 10571", "nr_periods 3")
        row2 = self._run(cur)[0]
        self.assertEqual(row2["window_periods"], 0)

    def test_rows_sort_by_window_rate_then_history(self):
        """正在被压的排在「只是历史上被压过」前面：先按窗口限流率，再看累计次数。"""
        first = {CG_PANEL: self._prev(),
                 self.OTHER: self._prev(quota=None, periods=100, throttled=1, usec=10)}
        files = {f"{CG_PANEL}/cpu.stat": CPU_STAT_PANEL,
                 f"{self.OTHER}/cpu.stat": CPU_STAT_PANEL.replace("nr_periods 10571", "nr_periods 200")
                 .replace("nr_throttled 84", "nr_throttled 184")}
        with fake_files(files):
            rows = ls._quota_second_pass(first, "choyeon-panel.service")
        self.assertEqual([r["unit"] for r in rows], ["redis-server.service", "choyeon-panel.service"])
        self.assertEqual(rows[0]["window_pct"], 183.0)
        self.assertIsNone(rows[1]["window_pct"], "窗口内没被限流就该是空，不是 0.0")
        self.assertEqual(rows[1]["total_throttled"], 84)


# --------------------------------------------------------------------------- 判词
class FindingsTests(unittest.TestCase):
    LOAD_OK = {"ok": True, "reason": None, "one": 0.05, "five": 0.08, "fifteen": 0.20,
               "runnable": 1, "total_tasks": 227, "last_pid": "1256624"}
    CPU_OK = {"ok": True, "reason": None, "pct": {"user": 3.3, "system": 0.5, "idle": 91.5,
                                                  "iowait": 0.0, "softirq": 0.0, "steal": 4.7},
              "ctxt_per_sec": 510, "forks_per_sec": 0, "procs_running": 1, "procs_blocked": 0}
    PSI_OK = {"ok": True, "reason": None,
              "controllers": {"cpu": {"some": {"avg10": 0.03, "avg60": 0.1, "avg300": 0.13, "total": 1}},
                              "io": {"some": {"avg10": 0.2, "avg60": 0.07, "avg300": 0.01, "total": 2}}}}
    PROCS_OK = {"ok": True, "reason": None, "scanned": 141, "budget_hit": False, "states": {"S": 94},
                "busy": [], "busy_total": 0, "blocked": [], "blocked_total": 0, "window_sec": 1.0}
    QUOTA_OK = {"ok": True, "reason": None, "dirs": 35, "units": [], "unit_total": 0,
                "capped_total": 0, "throttled_total": 0, "self_unit": "choyeon-panel.service"}

    def _f(self, load=None, cpu=None, psi=None, procs=None, quota=None, cores=2):
        return ls._findings(load if load is not None else dict(self.LOAD_OK),
                            cpu if cpu is not None else dict(self.CPU_OK),
                            psi if psi is not None else dict(self.PSI_OK),
                            procs if procs is not None else dict(self.PROCS_OK),
                            quota if quota is not None else dict(self.QUOTA_OK), cores)

    def _ids(self, findings):
        return [f["id"] for f in findings]

    def _one(self, findings, key):
        hit = [f for f in findings if key in f["id"]]
        self.assertEqual(len(hit), 1, f"应该只有一条 {key}：{self._ids(findings)}")
        return hit[0]

    def test_real_machine_numbers_produce_no_verdict(self):
        """生产机这一秒的真实读数（load 0.05 / steal 4.7 / 无限流）不该出任何要紧或值得处理。"""
        got = self._f()
        self.assertEqual(got, [], f"平静机器上冒出的判词：{self._ids(got)}")

    def test_per_core_scaling_is_the_same_code_on_different_core_counts(self):
        """load 8 在 2 核上是要紧，在 64 核上半载都不到——同一个数字，两种相反结论。"""
        busy = dict(self.LOAD_OK, one=8.0, five=7.0, fifteen=6.0)
        self.assertIn("load-cpu", self._ids(self._f(load=busy, cores=2)))
        self.assertEqual(self._f(load=busy, cores=64), [])

    def test_high_load_without_blocked_goes_to_cpu_branch(self):
        got = self._one(self._f(load=dict(self.LOAD_OK, one=8.0)), "load-cpu")
        self.assertEqual(got["severity"], ls.SEV_RISK)
        self.assertIn("每核排队 4.0", got["title"])
        self.assertIn("run queue", got["detail"])

    def test_high_load_with_blocked_goes_to_io_branch(self):
        cpu = dict(self.CPU_OK, procs_blocked=3, pct=dict(self.CPU_OK["pct"], iowait=18.0))
        got = self._one(self._f(load=dict(self.LOAD_OK, one=8.0), cpu=cpu), "load-io")
        self.assertEqual(got["severity"], ls.SEV_RISK)
        self.assertIn("3 个任务正卡在等 IO", got["title"])
        self.assertIn("加核、改配额都没用", got["detail"])
        self.assertIn("18.0%", got["detail"])

    def test_runnable_task_count_is_never_read_as_blocked(self):
        """整份回归的根：1/227 的 227 绝不能变成一条「227 个任务卡在等 IO」。"""
        cpu = dict(self.CPU_OK, procs_blocked=0)
        got = self._f(load=dict(self.LOAD_OK, one=8.0), cpu=cpu)
        ids = self._ids(got)
        self.assertIn("load-cpu", ids, "blocked 为 0、每核排队 4 时就该判成 CPU 不够")
        self.assertNotIn("load-io", ids)
        self.assertNotIn("blocked-d", ids)
        for f in got:
            blob = f["title"] + f["detail"] + f["fix"]
            if "227" in blob:
                self.assertNotIn("等 IO", blob, "整机任务总数被写成了 IO 事故")
                self.assertNotIn("blocked", blob, "227 被当成 procs_blocked 用了")

    def test_load_io_fix_without_d_pids_still_names_a_command(self):
        """blocked 计数来自 /proc/stat，D 名单来自本轮扫描；名单为空也不能拼出 `-p ` 空参数。"""
        cpu = dict(self.CPU_OK, procs_blocked=2)
        got = self._one(self._f(load=dict(self.LOAD_OK, one=8.0), cpu=cpu), "load-io")
        self.assertIn("pidstat -d 1 5", got["fix"])
        self.assertNotIn("-p ,", got["fix"])
        self.assertTrue(got["fix"].startswith("pidstat"))

    def test_load_io_fix_lists_d_pids(self):
        cpu = dict(self.CPU_OK, procs_blocked=1)
        procs = dict(self.PROCS_OK, blocked_total=1, blocked=[{"pid": 4242, "comm": "dd", "state": "D",
                                                              "cpu_ms": 0, "unit": "", "wchan": "x"}])
        got = self._one(self._f(load=dict(self.LOAD_OK, one=8.0), cpu=cpu, procs=procs), "load-io")
        self.assertIn("4242", got["fix"])

    def test_warn_band_and_long_window(self):
        got = self._one(self._f(load=dict(self.LOAD_OK, one=3.2, fifteen=0.4)), "load-warm")
        self.assertEqual(got["severity"], ls.SEV_WARN)
        self.assertNotIn("15 分钟档也超阈值", got["detail"])
        long_only = self._one(self._f(load=dict(self.LOAD_OK, one=1.0, fifteen=3.2)), "load-warm")
        self.assertIn("15 分钟档也超阈值", long_only["detail"])

    def test_d_state_roster_severity_scales_with_count(self):
        ds = [{"pid": 100 + i, "comm": f"blk{i}", "state": "D", "cpu_ms": i, "unit": "x.service",
               "wchan": "wait"} for i in range(6)]
        procs = dict(self.PROCS_OK, blocked=ds, blocked_total=6)
        got = self._one(self._f(procs=procs), "blocked-d")
        self.assertEqual(got["severity"], ls.SEV_RISK)
        self.assertIn("pidstat -d 1 5 -p 100,101,102,103,104", got["fix"])
        self.assertIn("/proc/100/stack", got["fix"])
        two = dict(self.PROCS_OK, blocked=ds[:2], blocked_total=2)
        self.assertEqual(self._one(self._f(procs=two), "blocked-d")["severity"], ls.SEV_WARN)

    def test_iowait_bands(self):
        self.assertEqual(self._one(self._f(cpu=dict(self.CPU_OK, pct={"iowait": 30.0})), "iowait")["severity"],
                         ls.SEV_RISK)
        self.assertEqual(self._one(self._f(cpu=dict(self.CPU_OK, pct={"iowait": 12.0})),
                                   "iowait-warm")["severity"], ls.SEV_WARN)
        self.assertNotIn("iowait", self._ids(self._f(cpu=dict(self.CPU_OK, pct={"iowait": 5.0}))))

    def test_iowait_risk_fix_uses_installed_tools(self):
        got = self._one(self._f(cpu=dict(self.CPU_OK, pct={"iowait": 30.0})), "iowait")
        self.assertIn("sar -d 1 3", got["fix"])
        self.assertIn("iostat -x 1 3", got["fix"])

    def test_steal_bands(self):
        self.assertEqual(self._one(self._f(cpu=dict(self.CPU_OK, pct={"steal": 25.0})), "steal")["severity"],
                         ls.SEV_RISK)
        self.assertEqual(self._one(self._f(cpu=dict(self.CPU_OK, pct={"steal": 6.0})),
                                   "steal-warm")["severity"], ls.SEV_WARN)
        self.assertNotIn("steal", self._ids(self._f(cpu=dict(self.CPU_OK, pct={"steal": 4.7}))))
        risk = self._one(self._f(cpu=dict(self.CPU_OK, pct={"steal": 25.0})), "steal")
        self.assertIn("问题不在机器内部", risk["detail"])

    def test_psi_cpu_stall(self):
        psi = {"ok": True, "reason": None,
               "controllers": {"cpu": {"some": {"avg10": 14.0, "avg60": 9.0, "avg300": 3.0, "total": 1}}}}
        got = self._one(self._f(psi=psi), "psi-cpu")
        self.assertEqual(got["severity"], ls.SEV_WARN)
        self.assertIn("/proc/pressure/io", got["fix"])
        self.assertNotIn("psi", self._ids(self._f(psi=dict(self.PSI_OK))))

    def _unit(self, unit="redis-server.service", pct=20.0, self_row=False, cores=0.8, periods=6,
              throttled=1, total=84, total_usec=3694975):
        return {"unit": unit, "path": f"/sys/fs/cgroup/system.slice/{unit}", "self": self_row,
                "capped": True, "quota_cores": cores, "period_us": 100000, "weight": 100,
                "total_periods": 10571, "total_throttled": total, "total_throttled_usec": total_usec,
                "window_periods": periods, "window_throttled": throttled, "window_throttled_usec": 900,
                "window_pct": pct, "usage_usec": 1, "window_usage_usec": 1}

    def test_throttle_warn_and_risk(self):
        q = dict(self.QUOTA_OK, units=[self._unit(pct=20.0)], capped_total=1, throttled_total=1)
        got = self._one(self._f(quota=q), "throttle:")
        self.assertEqual(got["severity"], ls.SEV_WARN)
        self.assertIn("cpu.stat", "、".join([got["detail"]]))
        self.assertIn("既不在 run queue 也不在 D 状态", got["detail"])
        hot = dict(self.QUOTA_OK, units=[self._unit(pct=45.0)], capped_total=1, throttled_total=1)
        self.assertEqual(self._one(self._f(quota=hot), "throttle:")["severity"], ls.SEV_RISK)

    def test_throttle_below_warn_is_silent(self):
        q = dict(self.QUOTA_OK, units=[self._unit(pct=4.0)], capped_total=1, throttled_total=1)
        self.assertNotIn("throttle:", "".join(self._ids(self._f(quota=q))))

    def test_self_throttle_tells_the_operator_where_the_quota_lives(self):
        """面板自己被限流时，处置必须写到 deploy/choyeon-panel.service 与 upgrade --no-pull，
        并明确禁止手工编辑 /etc/systemd 下那份渲染产物。"""
        q = dict(self.QUOTA_OK, units=[self._unit(pct=50.0, self_row=True)], capped_total=1,
                 throttled_total=1)
        got = self._one(self._f(quota=q), "throttle:")
        self.assertIn("面板自己", got["title"])
        self.assertIn("deploy/choyeon-panel.service", got["fix"])
        self.assertIn("choyeonctl upgrade --no-pull", got["fix"])
        self.assertIn("不要手工编辑 /etc/systemd", got["fix"])
        self.assertIn("CPUQuotaPerSecUSec", got["fix"])

    def test_other_unit_throttle_names_the_unit_and_the_blast_radius(self):
        q = dict(self.QUOTA_OK, units=[self._unit(unit="panel-rosetta-backend.service", pct=50.0)],
                 capped_total=1, throttled_total=1)
        got = self._one(self._f(quota=q), "throttle:")
        self.assertIn("systemctl show panel-rosetta-backend.service", got["fix"])
        self.assertIn("只有你知道", got["fix"])
        self.assertNotIn("面板已经", got["detail"])

    def test_quota_list_info_counts_all_capped_not_only_visible(self):
        units = [self._unit(pct=None), self._unit(unit="b.service", pct=None),
                 self._unit(unit="c.service", pct=None)]
        q = dict(self.QUOTA_OK, units=units[:1], unit_total=3, capped_total=3)
        got = self._one(self._f(quota=q), "quota-list")
        self.assertEqual(got["severity"], ls.SEV_INFO)
        self.assertIn("3 个单元带着 CPU 配额", got["title"])
        self.assertIn("另有 2 个没列出来", got["title"])
        self.assertIn("配额本身是保护", got["detail"])

    def test_a_throttled_unit_is_not_listed_twice(self):
        """被限流已经单独点名，就别再在同一轮的备查清单里出现一遍。"""
        q = dict(self.QUOTA_OK, units=[self._unit(pct=50.0)], capped_total=1, throttled_total=1)
        ids = self._ids(self._f(quota=q))
        self.assertIn("throttle:redis-server.service", ids)
        self.assertNotIn("quota-list", ids)

    def test_unreadable_sections_never_become_a_verdict(self):
        """读不到只能是「不知道」：既不能判成要紧，也不能判成没事，只许 info 并如实带原因。"""
        got = self._f(load={"ok": False, "reason": "读不到 /proc/loadavg"},
                      cpu={"ok": False, "reason": "采样窗口里 /proc/stat 没读到增量"},
                      psi={"ok": False, "reason": "内核没开 PSI"},
                      procs={"ok": False, "reason": "读不到 /proc"},
                      quota={"ok": False, "reason": "读不到 cgroup"})
        self.assertTrue(got, "读不到的节必须逐条点名，不能整体沉默")
        for f in got:
            self.assertEqual(f["severity"], ls.SEV_INFO, f"{f['id']} 在读不到的时候冒了要紧级")
            self.assertTrue(f["id"].startswith("unreadable:"))
            self.assertIn("别把它读成", f["detail"])
        joined = " ".join(f["detail"] for f in got)
        for why in ("读不到 /proc/loadavg", "没读到增量", "PSI", "cgroup"):
            self.assertIn(why, joined)

    def test_no_raw_iso_in_prose(self):
        """判词是给人读的句子：时间戳不许带 T 和时区（表格那几列前端会格式化，文案这一路没人接手）。"""
        procs = dict(self.PROCS_OK, blocked=[{"pid": 5, "comm": "x", "state": "D", "cpu_ms": 1,
                                             "unit": "", "wchan": "w"}], blocked_total=1)
        cpu = dict(self.CPU_OK, procs_blocked=1, pct={"iowait": 40.0, "steal": 30.0})
        q = dict(self.QUOTA_OK, units=[self._unit(pct=60.0, self_row=True)], capped_total=1)
        for f in self._f(load=dict(self.LOAD_OK, one=9.0), cpu=cpu, procs=procs, quota=q):
            self.assertNotIn("+08:00", f["detail"] + f["title"])
            self.assertNotIn("2026-10-", (f["detail"] + f["title"]).replace("2026-10-13 06:41:41", ""))

    def test_counts_split_by_severity(self):
        f = [{"severity": "risk"}, {"severity": "warn"}, {"severity": "warn"}, {"severity": "info"}]
        self.assertEqual(ls._counts(f), {"risk": 1, "warn": 2, "info": 1})

    def test_every_fix_is_a_readonly_command_that_exists_here(self):
        """处置要么是可复制走的只读诊断，要么写清由人自己执行；工具必须是这台机器装了的。"""
        procs = dict(self.PROCS_OK, blocked=[{"pid": 7, "comm": "dd", "state": "D", "cpu_ms": 2,
                                             "unit": "x.service", "wchan": "w"}], blocked_total=1)
        cpu = dict(self.CPU_OK, procs_blocked=2,
                   pct={"user": 5.0, "idle": 50.0, "iowait": 40.0, "steal": 30.0})
        psi = {"ok": True, "reason": None,
               "controllers": {"cpu": {"some": {"avg10": 40.0, "avg60": 30.0, "avg300": 20.0, "total": 1}}}}
        q = dict(self.QUOTA_OK,
                 units=[self._unit(pct=60.0, self_row=True), self._unit(unit="redis.service", pct=25.0),
                        self._unit(unit="nginx.service", pct=0.0)],
                 unit_total=3, capped_total=3, throttled_total=2)
        findings = self._f(load=dict(self.LOAD_OK, one=9.0, fifteen=9.0), cpu=cpu, psi=psi,
                           procs=procs, quota=q)
        self.assertGreaterEqual(len(findings), 6)
        allowed = ("pidstat", "mpstat", "sar", "iostat", "cat ", "uptime", "systemctl show", "for d in",
                   "head ")
        for f in findings:
            with self.subTest(finding=f["id"]):
                self.assertTrue(any(f["fix"].startswith(a) or " " + a in f["fix"] for a in allowed),
                                f["fix"])
                for banned in ("iotop", "atop", "kill", "renice", "systemctl restart", "daemon-reload -",
                               "tee ", ">"):
                    self.assertNotIn(banned, f["fix"], f"{f['id']} 的处置里出现了 {banned}")


class DoctorItemTests(unittest.TestCase):
    def _data(self, **kw):
        base = {"cores": 2, "load": dict(FindingsTests.LOAD_OK), "findings": [],
                "counts": {"risk": 0, "warn": 0, "info": 0}, "unreadable": []}
        base.update(kw)
        return base

    def test_risk_fails(self):
        d = self._data(findings=[{"severity": "risk", "title": "每核排队 4.0"}],
                       counts={"risk": 1, "warn": 0, "info": 0})
        item = ls.doctor_item(d)
        self.assertEqual(item["status"], "fail")
        self.assertIn("每核排队 4.0", item["detail"])
        self.assertEqual(item["fix"], "choyeonctl load")

    def test_warn_only(self):
        d = self._data(findings=[{"severity": "warn", "title": "甲"}, {"severity": "warn", "title": "乙"}],
                       counts={"risk": 0, "warn": 2, "info": 0})
        item = ls.doctor_item(d)
        self.assertEqual(item["status"], "warn")
        self.assertIn("另有 1 项", item["detail"])

    def test_pass_names_per_core_number_and_info(self):
        d = self._data(findings=[{"severity": "info", "title": "配额"}], counts={"risk": 0, "warn": 0, "info": 1})
        item = ls.doctor_item(d)
        self.assertEqual(item["status"], "pass")
        self.assertIn("每核排队 0.03", item["detail"])
        self.assertIn("（另有 1 条只是让你知道）", item["detail"])

    def test_unreadable_key_section_is_warn_not_pass(self):
        d = self._data(load={"ok": False, "reason": "读不到 /proc/loadavg"})
        item = ls.doctor_item(d)
        self.assertEqual(item["status"], "warn")
        self.assertIn("读不到 /proc/loadavg", item["detail"])

    def test_pass_still_lists_unreadable_sections(self):
        """判词里没有要紧项不等于全都读到了：读不到的节必须照样写在自检里。"""
        d = self._data(unreadable=["psi", "quota"],
                       findings=[{"severity": "info", "id": "unreadable:psi", "title": "t",
                                  "detail": "d"}], counts={"risk": 0, "warn": 0, "info": 1})
        item = ls.doctor_item(d)
        self.assertEqual(item["status"], "pass")
        self.assertIn("读不到的节", item["detail"])
        self.assertIn("psi", item["detail"])

    def test_missing_load_value_does_not_blow_up(self):
        d = self._data(load={"ok": True, "one": None, "five": None, "fifteen": None})
        self.assertEqual(ls.doctor_item(d)["status"], "pass")


class CacheTests(unittest.TestCase):
    def setUp(self):
        ls.reset_cache()
        self.addCleanup(ls.reset_cache)
        self.calls = []

        def fake_collect():
            self.calls.append(1)
            return {"as_of": "x", "cores": 2}

        self.p = mock.patch.object(ls, "_collect", fake_collect)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_second_call_hits_cache(self):
        first = asyncio.run(ls.audit())
        second = asyncio.run(ls.audit())
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.calls), 1)

    def test_refresh_bypasses_cache(self):
        asyncio.run(ls.audit())
        asyncio.run(ls.audit(refresh=True))
        self.assertEqual(len(self.calls), 2)

    def test_expired_cache_recollects(self):
        asyncio.run(ls.audit())
        with mock.patch.object(ls.time, "monotonic", lambda: ls._cache["at"] + ls.CACHE_TTL + 1):
            asyncio.run(ls.audit())
        self.assertEqual(len(self.calls), 2)

    def test_cached_copy_does_not_mutate_the_cache(self):
        asyncio.run(ls.audit())
        out = asyncio.run(ls.audit())
        out["findings"] = [{"id": "tampered"}]
        again = asyncio.run(ls.audit())
        self.assertNotIn("tampered", str(again))


class CollectTests(unittest.TestCase):
    """整节接线：payload 的形状就是前端与 CLI 读的形状。"""

    def _files(self, blocked="0", loadavg=LOADAVG_REAL, stat=STAT_REAL):
        stat_txt = stat.replace("procs_blocked 0", f"procs_blocked {blocked}")
        stat2_txt = STAT_SECOND.replace("procs_blocked 0", f"procs_blocked {blocked}")
        return {ls.PROC_STAT: [stat_txt, stat2_txt], ls.PROC_LOADAVG: loadavg,
                ls.PSI_PATHS["cpu"]: PSI_CPU_REAL, ls.PSI_PATHS["io"]: PSI_IO_REAL,
                "/proc/uptime": "1080888.42 2100000\n",
                f"{CG_PANEL}/cpu.max": CPU_MAX_PANEL, f"{CG_PANEL}/cpu.stat": CPU_STAT_PANEL,
                f"{CG_PANEL}/cpu.weight": "100\n"}

    def _collect(self, files, scan=None, dirs=None, scan_sec=1.0):
        stub = scan or {"ok": True, "reason": None, "scanned": 141, "budget_hit": False,
                        "states": {"S": 94}, "busy": [], "busy_total": 0, "blocked": [],
                        "blocked_total": 0, "window_sec": 1.0}
        # ⑰A 起 ctxt/forks 的分母是 stat1→stat2 的实测跨度，不再是标称 1 秒。时钟换成可控
        # 的一份（扫描那一步走 scan_sec），速率断言才是确定的。
        clock = _FakeClock(scan_sec)

        def fake_scan(hz, deadline):
            clock.advance()
            return stub

        with mock.patch.object(ls, "time", clock), \
             mock.patch.object(ls, "_cgroup_dirs", lambda: list(dirs if dirs is not None else [CG_PANEL])), \
             mock.patch.object(ls, "_scan_procs", fake_scan), \
             mock.patch.object(ls.os, "cpu_count", lambda: 2), \
             mock.patch.object(ls.os, "sysconf", lambda n: 100), \
             fake_files(files):
            return ls._collect()

    def test_blocked_comes_from_proc_stat_not_loadavg(self):
        """本批的根问题在集成层面再钉一遍：227 个任务不许被读成 227 个卡在 IO。"""
        out = self._collect(self._files())
        self.assertEqual(out["cpu"]["procs_blocked"], 0)
        self.assertEqual(out["load"]["total_tasks"], 227)
        self.assertNotIn("load-io", [f["id"] for f in out["findings"]])

    def test_blocked_flows_into_findings(self):
        files = self._files(blocked="3", loadavg="8.0 7.0 6.0 1/227 12\n")
        out = self._collect(files)
        self.assertEqual(out["cpu"]["procs_blocked"], 3)
        self.assertIn("load-io", [f["id"] for f in out["findings"]])

    def test_the_section_is_named_cpu_not_split(self):
        out = self._collect(self._files())
        self.assertIn("cpu", out)
        self.assertNotIn("split", out, "前端读的是 cpu 段；换名不换键会让页面静默读到 undefined")
        self.assertEqual(sorted(out["cpu"]["pct"]), sorted(
            ["idle", "iowait", "irq", "nice", "softirq", "steal", "system", "user"]))
        self.assertEqual(out["cpu"]["pct"]["idle"], 90.0)
        self.assertEqual(out["cpu"]["pct"]["user"], 3.0)
        self.assertEqual(out["cpu"]["ctxt_per_sec"], 510, "ctxt 要相减再除窗口，别把累计值当速率")
        self.assertEqual(out["window_sec"], 1.0, "速率的分母就是这里报出来的窗口，两处必须同源")

    def test_rates_divide_by_the_measured_span_not_the_nominal_second(self):
        """扫描走 2.5 秒时速率必须跟着降下来：⑰A 之前它恒按 1 秒算，页面写 2500/秒。"""
        out = self._collect(self._files(), scan_sec=2.5)
        self.assertEqual(out["window_sec"], 2.5)
        self.assertEqual(out["cpu"]["ctxt_per_sec"], 204, "510 次切换摊在 2.5 秒上是 204/秒")
        self.assertEqual(out["procs"]["window_sec"], 1.0, "进程表自己的窗口不受总跨度影响")

    def test_timestamp_is_iso_and_readable_by_the_frontend(self):
        out = self._collect(self._files())
        self.assertRegex(out["as_of"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(out["uptime_sec"], 1080888)

    def test_quota_section_shape(self):
        out = self._collect(self._files())
        q = out["quota"]
        self.assertEqual(q["dirs"], 1)
        self.assertEqual(q["unit_total"], 1)
        self.assertEqual(q["units"][0]["quota_cores"], 0.8)
        self.assertTrue(q["units"][0]["self"])
        self.assertEqual(q["capped_total"], 1)

    def test_units_are_capped_and_counts_stay_true(self):
        dirs = [f"/sys/fs/cgroup/system.slice/u{i}.service" for i in range(15)]
        files = self._files()
        for d in dirs:
            files[f"{d}/cpu.max"] = "80000 100000\n"
            files[f"{d}/cpu.stat"] = CPU_STAT_PANEL
        out = self._collect(files, dirs=dirs)
        self.assertEqual(len(out["quota"]["units"]), ls.MAX_QUOTA_UNITS)
        self.assertEqual(out["quota"]["unit_total"], 15)
        self.assertEqual(out["quota"]["capped_total"], 15)

    def test_unreadable_sections_are_listed(self):
        """空表 = 什么都读不到（fake_files 里没登记的路径返回空串）。"""
        out = self._collect({}, dirs=[])
        self.assertIn("load", out["unreadable"])
        self.assertIn("cpu_split", out["unreadable"])
        self.assertTrue(all(f["severity"] == ls.SEV_INFO for f in out["findings"]))

    def test_notes_are_carried_through(self):
        out = self._collect(self._files())
        self.assertEqual(out["notes"], ls.NOTES)
        self.assertNotIn("split", out["load"])


# --------------------------------------------------------------------------- CLI 文本
class CliTextTests(unittest.TestCase):
    DATA = {
        "as_of": "2026-10-13T07:08:03+08:00", "cached": False, "cores": 2, "hz": 100,
        "window_sec": 1.0, "uptime_sec": 1080427, "unreadable": [],
        "load": dict(FindingsTests.LOAD_OK),
        "cpu": dict(FindingsTests.CPU_OK),
        "psi": dict(FindingsTests.PSI_OK),
        "procs": dict(FindingsTests.PROCS_OK, scanned=141, states={"I": 46, "S": 95}, busy_total=5,
                      busy=[{"pid": 1131714, "comm": "python", "state": "S", "cpu_ms": 30,
                             "cpu_total_ms": 129010, "unit": "panel-rosetta-backend.service"},
                            {"pid": 90625, "comm": "redis-server", "state": "S", "cpu_ms": 20,
                             "cpu_total_ms": 7966830, "unit": "redis-server.service"}],
                      blocked=[{"pid": 4242, "comm": "dd", "state": "D", "cpu_ms": 0,
                                "unit": "", "wchan": "wait_on_page_bit"}], blocked_total=1),
        "quota": {"ok": True, "reason": None, "dirs": 35, "unit_total": 1, "capped_total": 1,
                  "throttled_total": 1, "self_unit": "choyeon-panel.service",
                  "units": [FindingsTests()._unit(pct=16.7, throttled=1, self_row=True)]},
        "findings": [{"id": "throttle:choyeon-panel.service", "severity": "warn",
                      "title": "面板自己 被自己的 CPU 配额限流：这一秒 16.7% 的周期被压住",
                      "detail": "配额是 0.8 核。",
                      "fix": "systemctl show choyeon-panel -p CPUQuotaPerSecUSec"}],
        "counts": {"risk": 0, "warn": 1, "info": 0}, "notes": ["严格只读：不改 CPUQuota=。"]}

    def test_text_covers_every_section(self):
        text = cli._load_text(self.DATA)
        self.assertIn("不 renice、不 kill、不 restart、不改 CPUQuota=", text)
        self.assertIn("每核排队 0.03", text)
        self.assertIn("此刻 1 个可运行 / 整机 227 个任务", text)
        self.assertIn("user 3.3%", text)
        self.assertIn("上下文切换 510 次/秒", text)
        self.assertIn("卡在等 IO 0", text)
        self.assertIn("cpu some avg10 0.03%", text)
        self.assertIn("I 46 · S 95", text)
        self.assertIn("panel-rosetta-backend.service", text)
        self.assertIn("窗口内     30ms", text)
        self.assertIn("[D] dd", text)
        self.assertIn("wait_on_page_bit", text)
        self.assertIn("35 个 cgroup 有 cpu.max", text)
        self.assertIn("0.8 核", text)
        self.assertIn("窗口 1/6 周期（16.7%）", text)
        self.assertIn("← 面板自己", text)
        self.assertIn("[值得处理]", text)
        self.assertIn("处置：systemctl show choyeon-panel", text)
        self.assertIn("说明：严格只读", text)
        self.assertIn("检查时间 2026-10-13T07:08:03+08:00", text)

    def test_cached_run_is_marked(self):
        self.assertIn("（命中缓存）", cli._load_text(dict(self.DATA, cached=True)))
        self.assertNotIn("（命中缓存）", cli._load_text(self.DATA))

    def test_idle_machine_says_so_instead_of_an_empty_table(self):
        d = dict(self.DATA)
        d["procs"] = dict(d["procs"], busy=[], busy_total=0, blocked=[], blocked_total=0)
        d["findings"] = []
        d["counts"] = {"risk": 0, "warn": 0, "info": 0}
        text = cli._load_text(d)
        self.assertIn("这一秒没有任何进程消耗 CPU", text)
        self.assertIn("这一轮没有归因结论", text)

    def test_truncated_busy_list_is_named_not_hidden(self):
        d = dict(self.DATA)
        d["procs"] = dict(d["procs"], busy_total=40)
        self.assertIn("另有 38 个进程的窗口增量没列出来", cli._load_text(d))

    def test_unreadable_sections_are_named_not_silenced(self):
        d = dict(self.DATA)
        d["load"] = {"ok": False, "reason": "读不到 /proc/loadavg"}
        d["cpu"] = {"ok": False, "reason": "采样窗口里 /proc/stat 没读到增量"}
        d["psi"] = {"ok": False, "reason": "内核没开 PSI（CONFIG_PSI 未编译），压力指标读不到"}
        d["procs"] = {"ok": False, "reason": "读不到 /proc"}
        d["quota"] = {"ok": False, "reason": "读不到 /sys/fs/cgroup"}
        # 判词跟着数据走：五节都读不到时不该留下任何带数字的结论（服务侧有专门用例，这里
        # 只验 CLI 不会把上一轮的数留着渲染）。
        d["findings"] = []
        d["counts"] = {"risk": 0, "warn": 0, "info": 0}
        text = cli._load_text(d)
        for phrase in ("读不到 /proc/loadavg", "没读到增量", "CONFIG_PSI", "读不到 /proc",
                       "读不到 /sys/fs/cgroup"):
            self.assertIn(phrase, text)
        self.assertNotIn("每核排队", text, "读不到 load 却输出每核排队就是假绿")
        self.assertNotIn("0.8 核", text)

    def test_no_undefined_or_nan_reaches_the_text(self):
        """判词里不许出现 None/undefined 这种字样——CLI 是最后一道能看见数字的地方。"""
        d = dict(self.DATA)
        d["cpu"] = {"ok": True, "reason": None, "pct": {}, "ctxt_per_sec": None, "forks_per_sec": None,
                    "procs_running": None, "procs_blocked": None}
        d["quota"] = dict(d["quota"], units=[{"unit": "x.service", "path": "/x", "self": False,
                                             "capped": False, "quota_cores": None, "period_us": 100000,
                                             "weight": None, "total_periods": None, "total_throttled": None,
                                             "total_throttled_usec": None, "window_periods": 0,
                                             "window_throttled": 0, "window_throttled_usec": 0,
                                             "window_pct": None, "usage_usec": None,
                                             "window_usage_usec": 0}])
        text = cli._load_text(d)
        self.assertNotIn("None 核", text)
        self.assertNotIn("None%", text)
        self.assertIn("未设限", text)
        self.assertIn("窗口里没有增量", text)


class CommandTests(unittest.TestCase):
    def test_load_command_ok_path(self):
        data = dict(CliTextTests.DATA)
        with mock.patch.object(cli.load_service, "audit", mock.AsyncMock(return_value=data)) as a:
            out, err, code = _run(cli.cmd_load(_Args(refresh=True)), json_mode=True)
        self.assertIsNone(code)
        self.assertIn('"throttle:choyeon-panel.service"', out)
        a.assert_awaited_once_with(refresh=True)

    def test_exit_3_when_both_core_sections_unreadable(self):
        data = dict(CliTextTests.DATA, load={"ok": False, "reason": "读不到 /proc/loadavg"},
                    cpu={"ok": False, "reason": "读不到 /proc/stat"})
        with mock.patch.object(cli.load_service, "audit", mock.AsyncMock(return_value=data)):
            out, err, code = _run(cli.cmd_load(_Args(refresh=False)))
        self.assertEqual(code, cli.EXIT_PRECONDITION)
        self.assertIn("读不到 /proc/loadavg", err)

    def test_partial_readability_still_exits_0(self):
        """机器上没跑 nginx、内核没开 PSI 都是正常状态，不能当失败退出。"""
        data = dict(CliTextTests.DATA, psi={"ok": False, "reason": "内核没开 PSI"},
                    quota={"ok": False, "reason": "读不到 /sys/fs/cgroup"})
        with mock.patch.object(cli.load_service, "audit", mock.AsyncMock(return_value=data)):
            out, err, code = _run(cli.cmd_load(_Args(refresh=False)), json_mode=True)
        self.assertIsNone(code)
        self.assertIn("内核没开 PSI", out)
        self.assertIn("读不到 /sys/fs/cgroup", out)


class SchemaContractTests(unittest.TestCase):
    def test_load_in_command_list(self):
        self.assertIn("load", [c["name"] for c in _schema()["commands"]])

    def test_entry_shape(self):
        entry = _load_entry()
        self.assertEqual(entry["args"], ["--refresh"])
        self.assertIn("只读", entry["desc"])
        self.assertGreaterEqual(len(entry["notes"]), 8)
        joined = " ".join(entry["notes"])
        for phrase in ("每核", "D 状态", "可运行/已注册任务总数", "nr_throttled", "CPUQuotaPerSecUSec",
                       "guest", "退出码 3", str(ls.PROC_SCAN_CAP), str(ls.SCAN_BUDGET_SEC)):
            self.assertIn(phrase, joined)

    def test_schema_declares_readonly_scope(self):
        entry = _load_entry()
        blob = entry["desc"] + " ".join(entry["notes"])
        for promise in ("自动修复", "帮你重启", "一键放宽配额", "自动 kill"):
            self.assertNotIn(promise, blob, f"契约里出现了写入承诺：{promise}")

    def test_schema_promises_only_real_subcommands(self):
        """契约里不许出现 argparse 没实现的命令（智能体照契约敲命令，命令不存在就是契约的错）。"""
        choices = set(next(a for a in cli.build_parser()._actions if a.dest == "cmd").choices)
        for c in _schema()["commands"]:
            head = c["name"].split()[0].split("|")[0]
            self.assertIn(head, choices, f"schema 承诺了不存在的命令：{c['name']}")


class ParserTests(unittest.TestCase):
    def test_load_subcommand_parses(self):
        args = cli.build_parser().parse_args(["load"])
        self.assertIs(args.fn, cli.cmd_load)
        self.assertFalse(args.refresh)

    def test_refresh_flag(self):
        self.assertTrue(cli.build_parser().parse_args(["load", "--refresh"]).refresh)

    def test_help_text_declares_readonly(self):
        lines = [ln for ln in cli.build_parser().format_help().splitlines() if ln.strip().startswith("load")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("只读", lines[0])


class MetaWiringTests(unittest.TestCase):
    def test_endpoint_registered(self):
        self.assertIn("/api/load", [r.path for r in meta.router.routes])

    def test_doctor_item_present_after_pending(self):
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("load", ids)
        self.assertEqual(meta._CHECKS[ids.index("load")][1], "负载与CPU")
        # 位置按前一项锚定，不写「必须是最后一项」：下一批再加自检项时这一条会假报警。
        self.assertEqual(ids.index("load"), ids.index("pending") + 1, "负载与CPU 排在改动未生效之后")

    def test_not_one_click_fixable(self):
        """一键修复白名单里不许出现 load：放宽配额、杀进程、重启服务都不是面板能定的时机。"""
        self.assertNotIn("load", meta.FIXABLE)

    def test_check_load_uses_audit_and_doctor(self):
        seen = {}

        async def audit(refresh=False):
            seen["refresh"] = refresh
            return {"load": {"ok": True, "one": 0.05, "five": 0.08, "fifteen": 0.2}, "cores": 2,
                    "findings": [], "counts": {"risk": 0, "warn": 0, "info": 0}, "unreadable": []}

        with mock.patch.object(meta.load_service, "audit", audit):
            item = asyncio.run(meta._check_load())
        self.assertEqual(item["status"], "pass")
        self.assertIn("refresh", seen)

    def test_endpoint_forwards_refresh(self):
        calls = []

        async def audit(refresh=False):
            calls.append(refresh)
            return {"ok": True}

        with mock.patch.object(meta.load_service, "audit", audit):
            asyncio.run(meta.load())
            asyncio.run(meta.load(refresh=1))
        self.assertEqual(calls, [False, True])


class RouteAccessTests(unittest.TestCase):
    """/api/load 是只读探测：viewer 能看，匿名不能看。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        async def audit(refresh=False):
            return {"as_of": "x", "cached": True, "cores": 2, "load": {"ok": True}, "cpu": {"ok": True},
                    "psi": {"ok": False}, "procs": {"ok": True, "busy": []}, "quota": {"ok": True, "units": []},
                    "findings": [], "counts": {}, "notes": [], "unreadable": []}

        p = mock.patch.object(meta.load_service, "audit", audit)
        p.start()
        self.addCleanup(p.stop)
        self.user = "p16a" + uuid.uuid4().hex[:10]
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "viewer"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.user, 'viewer')}"}

    def test_viewer_can_read(self):
        r = self.client.get("/api/load", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("cpu", body)
        self.assertIn("quota", body)

    def test_anonymous_denied(self):
        self.assertIn(self.client.get("/api/load").status_code, (401, 403))


class DocsTests(unittest.TestCase):
    def test_agents_md_documents_command_and_item_count(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("`load`", text)
        self.assertTrue(f"当前 {len(meta._CHECKS)} 项" in text, "AGENTS.md 的自检项数与代码不一致")

    def test_readme_documents_command_and_item_count(self):
        text = (Path(cli.ROOT) / "README.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl load", text)
        self.assertIn(f"共 {len(meta._CHECKS)} 项只读检查", text)
        self.assertNotIn(f"{len(meta._CHECKS) - 1} 项只读检查", text, "README 里还留着上一批的项数")

    def test_security_md_item_count(self):
        text = (Path(cli.ROOT) / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(meta._CHECKS)} 项只读自检", text)

    def test_agents_md_load_row_declares_readonly(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `load`"))
        for phrase in ("不 renice", "不 kill", "每核", "cpu.stat", "退出码 3"):
            self.assertIn(phrase, row)

    def test_agents_md_forbids_panel_side_quota_or_kill(self):
        """硬性禁止事项里要说清：这一层只归因不动手，配额与进程都由人决定。"""
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        rule = [ln for ln in text.splitlines() if ln.startswith("10. **禁止**")]
        self.assertEqual(len(rule), 1, rule)
        self.assertIn("choyeonctl load", rule[0])
        for phrase in ("容量决策", "renice", "restart"):
            self.assertIn(phrase, rule[0])

    def test_schema_cli_table_matches_parser(self):
        """AGENTS.md 的命令表、schema、argparse 三处必须说的是同一批命令。

        智能体只读 AGENTS.md 那张表：表里少一个命令，它就以为这台机器上的面板没有这个能力，
        于是去翻源码或者直接猜命令。反过来，schema 里承诺了 argparse 没有的命令更糟。
        """
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        rows = _cmd_tokens(_doc_cells(text))
        names = _cmd_tokens(c["name"] for c in _schema()["commands"])
        choices = set(next(a for a in cli.build_parser()._actions if a.dest == "cmd").choices)
        self.assertIn("load", choices)
        for cmd in sorted(choices):
            self.assertIn(cmd, rows, f"AGENTS.md 命令表缺了 {cmd}")
            self.assertIn(cmd, names, f"schema 缺了 {cmd}")


class NotesTests(unittest.TestCase):
    def test_notes_cover_the_limits(self):
        joined = " ".join(ls.NOTES)
        for phrase in ("每核排队", "D 状态", "procs_blocked", "cpu.max", "nr_throttled", "guest",
                       "严格只读", "不 restart", "窗口增量", "0ms"):
            self.assertIn(phrase, joined, f"说明里缺了 {phrase}")

    def test_notes_have_no_markdown_backticks(self):
        """NOTES 同时喂 CLI 与网页：反引号在网页里会原样画出来，不是斜体。"""
        for n in ls.NOTES:
            self.assertNotIn("`", n, f"说明文案里出现了反引号：{n[:40]}")

    def test_service_is_strictly_readonly(self):
        """源码级守卫：本服务不许 spawn 任何东西，也不许以写模式打开文件。

        不能靠 grep「restart」——findings 的 fix 里本来就该写着给人复制走的命令。要锁的是
        「面板自己按下按钮」，所以只看子进程调用与 open 的模式参数。
        """
        tree = ast.parse(Path(ls.__file__).read_text(encoding="utf-8"))
        banned_imports = {"subprocess", "shutil", "os.system", "popen"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & banned_imports, f"引入了会执行外部命令的模块：{imported}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "attr", None) or getattr(func, "id", "")
                self.assertNotIn(name, {"system", "popen", "Popen", "run", "check_output", "check_call",
                                        "remove", "rmdir", "chmod", "chown", "kill"},
                                 f"源码里出现了 {name}()")
                if name == "open":
                    mode = ""
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode = str(kw.value.value)
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        mode = mode or str(node.args[1].value)
                    self.assertNotIn("w", mode, f"以写模式打开文件：{mode}")
                    self.assertNotIn("a", mode, f"以追加模式打开文件：{mode}")
                    self.assertNotIn("+", mode, f"以读写模式打开文件：{mode}")

    def test_thresholds_are_documented_once(self):
        """阈值只在服务里定义一次：CLI 文案与 schema 是引用常量，不是各抄一份数字。

        抄出来的数会在下次调阈值时变成假承诺——契约上写着「每核 2 倍算要紧」，代码里却是别的数。
        """
        self.assertEqual(ls.LOAD_RISK_PER_CORE, 2.0)
        self.assertGreater(ls.LOAD_WARN_PER_CORE, 1.0)
        self.assertLess(ls.THROTTLE_WARN, ls.THROTTLE_RISK)
        before = " ".join(_load_entry()["notes"])
        self.assertIn(str(ls.LOAD_RISK_PER_CORE), before)
        with mock.patch.object(ls, "LOAD_RISK_PER_CORE", 7.75):
            after = " ".join(_load_entry()["notes"])
        self.assertIn("7.75", after, "schema 里的阈值是抄的，改常量它不跟着变")
        self.assertNotIn(str(ls.LOAD_RISK_PER_CORE) + " 倍算要紧", after)

class LoadFrontendWiringTests(unittest.TestCase):
    """⑯B 的前端接线守卫：构建与 vue-tsc 只证明类型对得上，证明不了三层说的是同一件事。

    负载页是纯展示，最容易被抄第二份的就是判词 id 与阈值：服务层改了常量，前端那一份
    会安静地留在旧值上，页面上的表现是颜色先于判词变红——同一件事给出两个档位。
    """

    ROOT = Path(__file__).resolve().parents[2]
    API = ROOT / "web" / "src" / "api.ts"
    SV = ROOT / "web" / "src" / "views" / "Services.vue"

    SCRIPT_START = "const ldForce = ref(false);"
    SCRIPT_END = "const ldAlert = computed(() => {"
    TPL_START = """<CpToolbarCard v-else-if="tab === 'load'"""
    TPL_END = """<CpToolbarCard v-else :title="logUnit"""

    def setUp(self):
        self.api_text = self.API.read_text(encoding="utf-8")
        self.sv_text = self.SV.read_text(encoding="utf-8")

    def api_load_line(self):
        for line in self.api_text.splitlines():
            if line.strip().startswith("load:"):
                return line
        return ""

    def script_region(self):
        """只取负载页的 <script> 段：模板段的「15 分钟档」这类文案会被比较正则误伤。"""
        i = self.sv_text.index(self.SCRIPT_START)
        j = self.sv_text.index(self.SCRIPT_END, i)
        return self.sv_text[i:self.sv_text.index("\n});", j)]

    def template_region(self):
        k = self.sv_text.index(self.TPL_START)
        m = self.sv_text.index(self.TPL_END, k)
        return self.sv_text[k:m]

    @staticmethod
    def service_ids():
        src = Path(ls.__file__).read_text(encoding="utf-8")
        # f-string 那两条（throttle / unreadable）在 "{" 处断掉，正好留下前缀本身
        return set(re.findall(r'"id":\s*f?"([A-Za-z0-9_:.-]+)', src))

    @classmethod
    def threshold_literals(cls):
        out = set()
        for name in ("LOAD_RISK_PER_CORE", "LOAD_WARN_PER_CORE", "LOAD_LONG_WARN",
                     "IOWAIT_WARN", "IOWAIT_RISK", "STEAL_WARN", "STEAL_RISK",
                     "PSI_CPU_WARN", "THROTTLE_WARN", "THROTTLE_RISK"):
            v = float(getattr(ls, name))
            out.add(f"{v:.1f}")
            if v == int(v):
                out.add(str(int(v)))
        return out

    def threshold_hits(self, text):
        hits = []
        for num in sorted(self.threshold_literals(), key=len, reverse=True):
            for m in re.finditer(r"[<>]=?\s*" + re.escape(num) + r"(?![0-9.])", text):
                hits.append(text[max(0, m.start() - 40):m.end() + 10].replace("\n", " "))
        return hits

    def test_api_router_and_view_agree_on_the_load_path(self):
        self.assertIn("req(`/load", self.api_load_line())
        self.assertIn('@router.get("/api/load")', inspect.getsource(meta))
        self.assertIn("api.load(", self.sv_text)
        self.assertIn(self.TPL_START, self.sv_text)

    def test_refresh_is_a_number_not_a_boolean(self):
        """路由签名是 refresh: int，前端传 true 会得到 422（⑯A 冒烟时真撞过一次）。"""
        self.assertIn("refresh: int", inspect.getsource(meta.load))
        self.assertIn("?refresh=1", self.api_load_line())
        self.assertNotIn("refresh=true", self.api_load_line())

    def test_cache_age_matches_service_ttl(self):
        """前端 maxAge 必须与服务层 TTL 同值，否则「缓存」标注与实际回源时机不一致。"""
        self.assertIn(f"maxAge: {int(ls.CACHE_TTL * 1000)}", self.script_region())

    def test_finding_ids_come_from_the_service(self):
        ids = self.service_ids()
        region = self.script_region()
        static_ids = set()
        for call in re.findall(r"ldSevOf\(([^)]*)\)", region):
            static_ids.update(re.findall(r"'([A-Za-z0-9_:.-]+)'", call))
        prefixes = set(re.findall(r"startsWith\('([A-Za-z0-9_:]+)'\)", region))
        self.assertGreaterEqual(len(static_ids), 8, "判词反查退化成了硬编码颜色")
        unknown = sorted(s for s in static_ids if s not in ids)
        self.assertFalse(unknown, f"前端用了服务层没有的判词 id：{unknown}")
        self.assertIn("throttle:", ids)
        self.assertIn("throttle:", prefixes, "动态判词族只在表里有颜色，chip 上不跟着变")

    def test_load_script_recomputes_no_service_threshold(self):
        hits = self.threshold_hits(self.script_region())
        self.assertFalse(hits, f"负载页自己算了一次阈值：{hits}")

    def test_threshold_guard_is_not_a_no_op(self):
        """反证：把阈值抄进页面必须被上一条拦住（否则守卫只是看起来很严）。"""
        fake = "const loadTone = computed(() => (perCore.value > 2.0 ? 'err' : ''));"
        self.assertTrue(self.threshold_hits(fake), "检测器抓不到抄出来的 2.0")

    def test_load_page_offers_no_write_action(self):
        """这一页一个修复动作都不给：改配额是容量决策，renice / kill / restart 会断业务。"""
        both = self.script_region() + self.template_region()
        for verb in ("serviceAction(", "doctorFix(", "appAction(", "certbotRenew("):
            self.assertNotIn(verb, both)
        self.assertIn("复制", self.template_region())

    def test_split_bar_leaves_out_guest_fields(self):
        block = self.script_region()[self.script_region().index("const SPLIT_FIELDS"):]
        block = block[:block.index("const psiSome")]
        self.assertNotIn("'guest'", block, "guest 会与 user 重复计账，堆叠条合计超过 100%")
        self.assertEqual(len(re.findall(r"key: '", block)), 8)

    def test_tick_ms_uses_backend_hz(self):
        self.assertIn("1000 / ldData.hz", self.template_region())

    def test_load_template_has_no_markdown_backticks(self):
        """模板的文本节点是 HTML 文本，反引号会原样显示成怪字符。

        属性值与插值里的反引号是 JS 模板字符串的语法（row-key 那一类），不是给人看的话，
        不是给人看的话，所以先把这两类整体摘掉再查。
        """
        prose = re.sub(r'"[^"]*"', "", self.template_region())
        prose = re.sub(r"\{\{.*?\}\}", "", prose, flags=re.S)
        self.assertNotIn("`", prose, "负载页模板里写了 markdown 反引号")

    def test_load_tab_is_not_gated_behind_admin(self):
        """路由 viewer 可读，页签也必须对 viewer 存在，否则等于把只读账号挡在门外。"""
        tabs = self.sv_text[self.sv_text.index("const tabs = computed("):]
        tabs = tabs[:tabs.index("]);")]
        self.assertLess(tabs.index("{ key: 'load'"), tabs.index("...(isAdmin"),
                        "负载页签被塞进 admin 分支，只读账号看不见")



if __name__ == "__main__":
    unittest.main()
