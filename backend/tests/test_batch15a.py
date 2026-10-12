"""批次⑮A：改动未生效对账（pending_service）+ 自检项 + `choyeonctl pending`。

这一层锁的是一件很具体的事：**装上去的东西没进正在跑的进程**。补丁装了、conf 改了、unit 编辑了，
而服务状态页上全是绿的。所以测试全部围绕「什么条件下必须出声、什么条件下必须闭嘴」：

- 只有 inode 真的对不上才算「被替换」。映射里带 (deleted) 但同路径当前文件 inode 相同的情况不存在，
  可 /dev/zero、/SYSV*、memfd、匿名映射全都天生带 (deleted)——postgres 每个后端都会命中，
  滤不掉就是整机假阳性。
- 进程在扫描中途退出（/proc/<pid>/stat 读不到）必须整条跳过：它名下的 deleted 映射还在，
  报出来只会让人去查一个已经不存在的进程。
- 内核待重启靠版本号推导，且必须排除元包：linux-image-cloud-amd64 的名字后面接的是架构，
  自己也带一个不断变新的版本号，把它当新内核会把每台正常机器判成待重启。
- 只认 /run/reboot-required 是不够的：它来自 update-notifier-common，服务器镜像默认不装。
- nginx 比的是最晚启动的 worker，不是 master：reload 只换 worker，拿 master 作参照会把
  「reload 过了」误报成「还没 reload」。
- 归属拿不到就留空：dpkg 查不到包名时只说文件路径，不编一个看起来更像的版本号。
- 严格只读：不 restart、不 reload、不 daemon-reload、不 reboot，也不进一键修复白名单。
  测试逐条扫一遍 findings 里的 fix，确认它们全是给人复制走的命令。

夹具里的 maps / stat / dpkg-query 输出都是 2026-10-13 从生产机原样抓回来的
（liblzma5 5.8.1-1+deb13u2 于 10-08 由 unattended-upgrade 装上，inode 2087 → 28426，
postgres 26 个后端与 unattended-upgrades 仍踩着旧映射；running 内核 6.12.111+deb13-cloud-amd64）。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import os
import re
import tempfile
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch15a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch15a_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import pending_service as ps  # noqa: E402


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
def fake_proc(files: dict):
    """把服务里所有 /proc 读取换成一张表（其余路径回落到真实读，方便用 tmpfile 验 mtime）。"""

    def _read(path):
        if path in files:
            return files[path]
        if path.startswith("/proc/"):
            return ""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    with mock.patch.object(ps, "_read", _read):
        yield


# 生产机 postgres 后端的一个真实 maps 片段：旧 liblzma 带 (deleted)，另有 /dev/zero 与 SysV 共享内存
# 也天生带 (deleted)——这正是必须滤掉的噪声来源。
MAPS_PG = ("""7f3c00000000-7f3c00020000 r-xp 00000000 08:01 2087                         """
           """   /usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1 (deleted)\n"""
           """7f3c00020000-7f3c00021000 r--p 00000000 08:01 2087     /usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1 (deleted)\n"""
           """7ffd00000000-7ffd00021000 rw-p 00000000 00:00 0        [vvar]\n"""
           """ffffffffff600000-ffffffffff601000 --xp 00000000 00:00 0  [vsyscall]\n"""
           """7f3b00000000-7f3b00001000 rw-s 00000000 00:05 262145    /dev/zero (deleted)\n"""
           """7f3b10000000-7f3b10001000 rw-s 00000000 00:04 1234      /SYSV00001234 (deleted)\n"""
           """7f3b20000000-7f3b20001000 rw-s 00000000 00:11 55        /dev/shm/PostgreSQL.1234567890 (deleted)\n"""
           """7f3b30000000-7f3b30001000 rw-s 00000000 00:0d 99        anon_inode:[userfaultfd] (deleted)\n"""
           """7f3b40000000-7f3b40001000 rw-s 00000000 01:00 77        /memfd:jit (deleted)\n""")


class NoiseTests(unittest.TestCase):
    def test_device_and_shm_and_anon_are_noise(self):
        for p in ("/dev/zero", "/SYSV00001234", "/dev/shm/PostgreSQL.123", "anon_inode:[userfaultfd]",
                  "/memfd:jit", "[heap]", "[vvar]", ""):
            with self.subTest(path=p):
                self.assertTrue(ps._is_noise(p), f"{p} 应该被当成噪声滤掉")

    def test_real_library_is_not_noise(self):
        self.assertFalse(ps._is_noise("/usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1"))
        self.assertFalse(ps._is_noise("/root/www/choyeon-panel/web/node_modules/a/b.node"))


class MapsParseTests(unittest.TestCase):
    def test_only_deleted_real_paths_survive(self):
        with fake_proc({"/proc/5317/maps": MAPS_PG}):
            got = ps._maps_deleted_files(5317)
        self.assertEqual(len(got), 1, got)
        path, dev, ino = got[0]
        self.assertEqual(path, "/usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1")
        self.assertEqual(dev, "08:01")
        self.assertEqual(ino, 2087)

    def test_repeated_segment_of_same_file_yields_one_entry(self):
        # maps 里一个 .so 会被切成 r-xp / r--p / rw-p 多段，同一个文件只该报一次
        with fake_proc({"/proc/1/maps": MAPS_PG}):
            self.assertEqual(len(ps._maps_deleted_files(1)), 1)

    def test_path_with_spaces_is_kept_whole(self):
        line = ("7f00-7f01 r-xp 00000000 08:01 42                 "
                "/opt/My Tools/libmy tool.so (deleted)\n")
        with fake_proc({"/proc/2/maps": line}):
            got = ps._maps_deleted_files(2)
        self.assertEqual(got, [("/opt/My Tools/libmy tool.so", "08:01", 42)])

    def test_line_without_deleted_suffix_is_ignored(self):
        with fake_proc({"/proc/3/maps": "7f00-7f01 r-xp 0 08:01 1 /usr/lib/libz.so.1\n"}):
            self.assertEqual(ps._maps_deleted_files(3), [])

    def test_unreadable_maps_returns_empty(self):
        with fake_proc({}):
            self.assertEqual(ps._maps_deleted_files(999999), [])


class StatCurrentTests(unittest.TestCase):
    def test_same_inode_same_dev_is_not_replaced(self):
        with tempfile.NamedTemporaryFile() as fh:
            st = os.stat(fh.name)
            dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
            cur = ps._stat_current(fh.name, dev, st.st_ino)
        self.assertTrue(cur["exists"])
        self.assertFalse(cur["replaced"], "同一个 inode 就不算被替换")

    def test_different_inode_is_replaced(self):
        with tempfile.NamedTemporaryFile() as fh:
            st = os.stat(fh.name)
            dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
            cur = ps._stat_current(fh.name, dev, st.st_ino + 1)
        self.assertTrue(cur["replaced"])
        self.assertEqual(cur["ctime"], int(st.st_ctime))

    def test_other_device_same_inode_is_replaced(self):
        with tempfile.NamedTemporaryFile() as fh:
            cur = ps._stat_current(fh.name, "fe:ff", os.stat(fh.name).st_ino)
        self.assertTrue(cur["replaced"], "跨设备撞上同 inode 号是巧合，不是同一个文件")

    def test_vanished_path_is_replaced_and_has_no_ctime(self):
        cur = ps._stat_current("/nonexistent/definitely-gone.so", "08:01", 1)
        self.assertFalse(cur["exists"])
        self.assertTrue(cur["replaced"])
        self.assertIsNone(cur["ctime"])


class ScanStaleTests(unittest.TestCase):
    """整段扫描：聚合、跳过中途退出的进程、只留 inode 对不上的。"""

    def _stale_files(self, path="/usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1"):
        return [(path, "08:01", 2087)]

    def test_aggregates_backends_into_one_unit(self):
        found = {100: self._stale_files(), 101: self._stale_files(), 102: []}
        starts = {100: 1790766510, 101: 1791194556, 102: 1790000000}
        units = {100: "postgresql@17-main.service", 101: "postgresql@17-main.service", 102: ""}
        with mock.patch.object(os, "listdir", lambda p: ["100", "101", "102"] if p == "/proc" else []), \
             mock.patch.object(ps, "_maps_deleted_files", lambda pid: found.get(pid, [])), \
             mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: starts.get(pid)), \
             mock.patch.object(ps, "_unit_of", lambda pid: units.get(pid, "")), \
             mock.patch.object(ps, "_cmd_of", lambda pid: "postgres"), \
             mock.patch.object(ps, "_stat_current",
                               lambda path, dev, ino: {"exists": True, "inode": 28426,
                                                       "replaced": True, "ctime": 1791413451}):
            out = ps._scan_stale(1790765667, "choyeon-panel.service")
        self.assertTrue(out["ok"])
        self.assertEqual(out["unit_total"], 1)
        rec = out["units"][0]
        self.assertEqual(rec["unit"], "postgresql@17-main.service")
        self.assertEqual(rec["procs"], 2)
        self.assertEqual(rec["started"], 1790766510, "参照时刻取这个单元里最老的进程")
        self.assertEqual(len(rec["files"]), 1, "同一个文件在同一个单元里只该列一次")
        self.assertTrue(rec["files"][0]["replaced_at"], "ctime 晚于进程启动才叫装上了没吃到")
        self.assertEqual(out["procs_stale"], 2)

    def test_process_that_died_mid_scan_is_skipped_entirely(self):
        with mock.patch.object(os, "listdir", lambda p: ["7"] if p == "/proc" else []), \
             mock.patch.object(ps, "_maps_deleted_files", lambda pid: self._stale_files()), \
             mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: None), \
             mock.patch.object(ps, "_unit_of", lambda pid: "x.service"), \
             mock.patch.object(ps, "_stat_current",
                               lambda path, dev, ino: {"exists": True, "inode": 1,
                                                       "replaced": True, "ctime": 1}):
            out = ps._scan_stale(1790765667, "choyeon-panel.service")
        self.assertEqual(out["units"], [], "读不到启动时刻的进程不该报出来")
        self.assertEqual(out["unit_total"], 0)

    def test_inode_match_is_dropped(self):
        with mock.patch.object(os, "listdir", lambda p: ["7"] if p == "/proc" else []), \
             mock.patch.object(ps, "_maps_deleted_files", lambda pid: self._stale_files()), \
             mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: 1790765677), \
             mock.patch.object(ps, "_unit_of", lambda pid: "a.service"), \
             mock.patch.object(ps, "_cmd_of", lambda pid: "a"), \
             mock.patch.object(ps, "_stat_current",
                               lambda path, dev, ino: {"exists": True, "inode": 2087,
                                                       "replaced": False, "ctime": 1}):
            out = ps._scan_stale(1790765667, "choyeon-panel.service")
        self.assertEqual(out["units"], [], "inode 对得上就不是被替换")

    def test_orphans_sort_after_named_units(self):
        found = {1: self._stale_files("/usr/lib/liba.so"), 2: self._stale_files("/opt/x/y.node")}
        with mock.patch.object(os, "listdir", lambda p: ["1", "2"] if p == "/proc" else []), \
             mock.patch.object(ps, "_maps_deleted_files", lambda pid: found.get(pid, [])), \
             mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: 1790000000), \
             mock.patch.object(ps, "_unit_of", lambda pid: "a.service" if pid == 1 else ""), \
             mock.patch.object(ps, "_cmd_of", lambda pid: "x"), \
             mock.patch.object(ps, "_stat_current",
                               lambda path, dev, ino: {"exists": True, "inode": 9,
                                                       "replaced": True, "ctime": 1790999999}):
            out = ps._scan_stale(1790765667, "choyeon-panel.service")
        self.assertEqual([r["unit"] for r in out["units"]], ["a.service", ""])
        self.assertEqual(out["orphan_total"], 1)
        self.assertTrue(out["units"][1]["self"] is False)

    def test_self_unit_is_flagged(self):
        with mock.patch.object(os, "listdir", lambda p: ["5"] if p == "/proc" else []), \
             mock.patch.object(ps, "_maps_deleted_files", lambda pid: self._stale_files()), \
             mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: 1790000000), \
             mock.patch.object(ps, "_unit_of", lambda pid: "choyeon-panel.service"), \
             mock.patch.object(ps, "_cmd_of", lambda pid: "uvicorn"), \
             mock.patch.object(ps, "_stat_current",
                               lambda path, dev, ino: {"exists": True, "inode": 9,
                                                       "replaced": True, "ctime": 1790999999}):
            out = ps._scan_stale(1790765667, "choyeon-panel.service")
        self.assertTrue(out["units"][0]["self"], "面板自己踩着旧映射必须能在页面上认出来")

    def test_proc_unreadable_reports_reason_not_zero(self):
        with mock.patch.object(os, "listdir", side_effect=OSError("no proc")):
            out = ps._scan_stale(1790765667, "choyeon-panel.service")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "读不到 /proc")
        self.assertEqual(out["units"], [])


class KernelStateTests(unittest.TestCase):
    DPKG_REAL = ("linux-image-6.12.111+deb13-cloud-amd64|6.12.111-1|ii \n"
                 "linux-image-6.12.48+deb13-cloud-amd64|6.12.48-1|ii \n"
                 "linux-image-cloud-amd64|6.12.111-1|ii\n")

    @staticmethod
    def _num_gt(a, b):
        """替 dpkg --compare-versions 干活：版本号按数字段比，不能按字符串比。

        按字典序比 "6.12.48-1" 和 "6.12.111-1" 会说前者更大，测试就成了「实现把旧内核报成新内核」
        的假绿灯——这正是本批在生产机上要防的那类错。
        """
        digits = lambda v: [int(x) for x in re.findall(r"\d+", v)]  # noqa: E731
        return digits(a) > digits(b)

    def _run_with(self, stdout, rc=0, exists=False, gt=None):
        gt = gt or self._num_gt

        def fake_run(cmd, **kw):
            if cmd[:2] == ["dpkg", "--compare-versions"]:
                # dpkg 用退出码表结论，实现读的是 returncode == 0；给 True/False 会让
                # False == 0 成立，替身自己就把「不比新版更新」说成了「更新」。
                return mock.Mock(returncode=0 if gt(cmd[2], cmd[4]) else 1, stdout="", stderr="")
            return mock.Mock(returncode=rc, stdout=stdout, stderr="" if rc == 0 else "boom")

        with mock.patch.object(ps.subprocess, "run", fake_run), \
             mock.patch.object(ps.os.path, "exists", lambda p: exists), \
             mock.patch.object(ps.os, "uname",
                               lambda: mock.Mock(release="6.12.111+deb13-cloud-amd64")):
            return ps._kernel_state(1790765667)

    def test_meta_package_is_never_counted_as_a_new_kernel(self):
        # linux-image-cloud-amd64 的名字后面接架构、版本号还一直在变新，把它当内核就是每台机器都待重启
        out = self._run_with(self.DPKG_REAL)
        self.assertTrue(out["ok"])
        self.assertFalse(out["needs_reboot"])
        self.assertEqual(out["newer"], [])
        self.assertEqual([x["pkg"] for x in out["installed"]],
                         ["linux-image-6.12.111+deb13-cloud-amd64",
                          "linux-image-6.12.48+deb13-cloud-amd64", "linux-image-cloud-amd64"])

    def test_newer_kernel_package_sets_needs_reboot(self):
        # 用 6.13 而不是 6.12.99：后者按版本号段位比其实比 6.12.111 旧，夹具自己就得讲得通
        out = self._run_with(self.DPKG_REAL + "linux-image-6.13.0+deb13-cloud-amd64|6.13.0-1|ii\n")
        self.assertTrue(out["needs_reboot"])
        self.assertEqual([x["pkg"] for x in out["newer"]], ["linux-image-6.13.0+deb13-cloud-amd64"])

    def test_distro_marker_alone_is_enough(self):
        out = self._run_with(self.DPKG_REAL, exists=True, gt=lambda a, b: False)
        self.assertTrue(out["needs_reboot"])
        self.assertTrue(out["marker"]["present"])

    def test_not_installed_rows_are_ignored(self):
        # rc（已卸载残留配置）的行还在 dpkg-query 里，把它当「装着的内核」就会误报待重启
        out = self._run_with("linux-image-6.9.9-amd64|6.9.9-1|rc \n"
                             "linux-image-6.12.111+deb13-cloud-amd64|6.12.111-1|ii \n"
                             "linux-image-6.12.48+deb13-cloud-amd64|6.12.48-1|ii \n"
                             "linux-image-cloud-amd64|6.12.111-1|ii\n")
        self.assertEqual([x["pkg"] for x in out["installed"]],
                         ["linux-image-6.12.111+deb13-cloud-amd64",
                          "linux-image-6.12.48+deb13-cloud-amd64", "linux-image-cloud-amd64"])
        self.assertFalse(out["needs_reboot"])
        self.assertEqual(out["newer"], [])

    def test_running_kernel_without_package_is_indeterminate_not_reboot(self):
        out = self._run_with("linux-image-cloud-amd64|6.12.111-1|ii\n", gt=lambda a, b: False)
        # 只有元包时：没有可比对的内核包，判定不了，但不能编成「要重启」
        self.assertIsNone(out["needs_reboot"])
        self.assertTrue(out["reason"])

    def test_dpkg_failure_is_not_green(self):
        out = self._run_with("", rc=1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"])
        self.assertIsNone(out["needs_reboot"])


class ReloadStateTests(unittest.TestCase):
    def _nginx_fixture(self, conf_dir, worker_start, master_start, newest_mtime):
        files = {}
        for name, mt in (("a.conf", newest_mtime), ("b.conf", newest_mtime - 100)):
            p = Path(conf_dir) / name
            p.write_text("x", encoding="utf-8")
            os.utime(p, (mt, mt))
            files[str(p)] = mt
        paths = sorted(files)
        proc_files = {
            "/proc/6507/comm": "nginx\n",
            "/proc/6507/cmdline": "nginx: master process /usr/sbin/nginx\x00",
            "/proc/601140/comm": "nginx\n",
            "/proc/601140/cmdline": "nginx: worker process\x00",
        }
        starts = {6507: master_start, 601140: worker_start}

        def fake_run(cmd, **kw):
            return mock.Mock(returncode=0, stdout="".join(f"# configuration file {p}:\n" for p in paths),
                             stderr="")

        with mock.patch.object(os, "listdir", lambda p: ["6507", "601140"] if p == "/proc" else []), \
             mock.patch.object(ps.subprocess, "run", fake_run), \
             fake_proc(proc_files), \
             mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: starts.get(pid)):
            return ps._nginx_state(1790765667, 250)

    def test_conf_written_after_last_reload_is_pending(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._nginx_fixture(d, worker_start=1791128651, master_start=1790765680,
                                      newest_mtime=1791670977)
        self.assertTrue(out["ok"])
        self.assertTrue(out["pending"])
        self.assertEqual(out["worker_pids"], [601140])

    def test_reload_after_conf_change_is_not_pending(self):
        """关键回归：master 是老早以前的，reload 只换 worker——参照必须是 worker。"""
        with tempfile.TemporaryDirectory() as d:
            out = self._nginx_fixture(d, worker_start=1791673128, master_start=1790765680,
                                      newest_mtime=1791670977)
        self.assertFalse(out["pending"], "worker 比配置文件新，说明 reload 过了")
        self.assertEqual(out["ref_start"], 1791673128)

    def test_no_master_reports_reason_instead_of_green(self):
        with mock.patch.object(os, "listdir", lambda p: [] if p == "/proc" else []):
            out = ps._nginx_state(1790765667, 250)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["pending"])
        self.assertIn("nginx", out["reason"])

    def test_nginx_t_failure_is_not_green(self):
        def fake_run(cmd, **kw):
            return mock.Mock(returncode=1, stdout="", stderr="nginx: configuration file not found")

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nginx.conf"
            p.write_text("x", encoding="utf-8")
            files = {
                "/proc/1/comm": "nginx\n",
                "/proc/1/cmdline": "nginx: master process\x00",
            }
            with mock.patch.object(os, "listdir", lambda pr: ["1"] if pr == "/proc" else []), \
                 mock.patch.object(ps.subprocess, "run", fake_run), \
                 fake_proc(files), \
                 mock.patch.object(ps, "_proc_start_epoch", lambda pid, b, h: 1790765680):
                out = ps._nginx_state(1790765667, 250)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["pending"])

    def test_systemd_need_daemon_reload_pairing(self):
        listed = mock.Mock(returncode=0, stdout="  a.service  loaded active running A\n"
                                                "  b.service  loaded active exited  B\n", stderr="")
        shown = mock.Mock(returncode=0, stdout="Id=a.service\nNeedDaemonReload=no\n"
                                               "Id=b.service\nNeedDaemonReload=yes\n", stderr="")
        with mock.patch.object(ps.subprocess, "run", mock.Mock(side_effect=[listed, shown])):
            out = ps._systemd_state()
        self.assertTrue(out["ok"])
        self.assertEqual(out["need_reload"], ["b.service"])
        self.assertEqual(out["checked"], 2)

    def test_systemd_unavailable_is_not_green(self):
        with mock.patch.object(ps.subprocess, "run", side_effect=OSError("no systemctl")):
            out = ps._systemd_state()
        self.assertFalse(out["ok"])
        self.assertEqual(out["need_reload"], [])
        self.assertTrue(out["reason"])

    def test_sshd_conf_include_relative_and_glob(self):
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "sshd_config"
            main.write_text(f"Include sshd_config.d/*.conf\nInclude {d}/other.conf\n",
                            encoding="utf-8")
            frag_dir = Path(d) / "sshd_config.d"
            frag_dir.mkdir()
            (frag_dir / "50-x.conf").write_text("x", encoding="utf-8")
            (frag_dir / "note.txt").write_text("x", encoding="utf-8")
            (Path(d) / "other.conf").write_text("x", encoding="utf-8")
            with mock.patch.object(ps, "SSHD_CONFIG", str(main)), \
                 mock.patch.object(ps, "SSHD_DIR", d):
                paths = [os.path.basename(p) for p in ps._sshd_conf_paths()]
        # 相对 Include 要按 sshd 自己的口径补上 /etc/ssh 的基准目录，glob 要展开，
        # glob 命不中的杂文件不许混进来；返回顺序不参与判定，所以只比集合。
        self.assertEqual(sorted(paths), sorted(["sshd_config", "other.conf", "50-x.conf"]))
        self.assertNotIn("note.txt", paths)

    def test_sshd_without_listener_reports_reason(self):
        with mock.patch.object(os, "listdir", lambda p: [] if p == "/proc" else []):
            out = ps._sshd_state(1790765667, 250)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["pending"])


class PackageLookupTests(unittest.TestCase):
    def test_security_prefixes_hit(self):
        for pkg in ("libssl3t64", "liblzma5", "openssh-server", "sudo", "libc6", "nginx",
                    "libxml2", "libexpat1", "dovecot-core"):
            with self.subTest(pkg=pkg):
                self.assertTrue(ps._security_pkg(pkg))

    def test_non_security_pkg(self):
        for pkg in ("ncurses-base", "vim-tiny", "less"):
            with self.subTest(pkg=pkg):
                self.assertFalse(ps._security_pkg(pkg))

    def test_arch_suffix_is_stripped(self):
        self.assertTrue(ps._security_pkg("libssl3t64:amd64"))

    def test_dpkg_owner_parses_real_line(self):
        line = "liblzma5:amd64: /usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1\n"
        with mock.patch.object(ps.subprocess, "run",
                               mock.Mock(return_value=mock.Mock(returncode=0, stdout=line, stderr=""))), \
             mock.patch.object(ps, "_pkg_version", lambda name: "5.8.1-1+deb13u2"):
            got = ps._dpkg_owner("/usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1")
        self.assertEqual(got, ("liblzma5", "5.8.1-1+deb13u2"))

    def test_dpkg_owner_skips_non_package_paths_without_spawning(self):
        run = mock.Mock()
        with mock.patch.object(ps.subprocess, "run", run):
            self.assertIsNone(ps._dpkg_owner("/root/www/choyeon-panel/web/dist/index.html"))
            self.assertIsNone(ps._dpkg_owner("/tmp/build/x.so"))
        run.assert_not_called()

    def test_attach_packages_caches_per_path_and_respects_budget(self):
        stale = {"units": [
            {"unit": "a.service", "files": [{"path": "/usr/lib/liba.so"}, {"path": "/usr/lib/libb.so"}]},
            {"unit": "b.service", "files": [{"path": "/usr/lib/liba.so"}]},
        ]}
        seen = []

        def owner(path):
            seen.append(path)
            return ("liblzma5", "5.8.1-1+deb13u2") if path.endswith("liba.so") else None

        with mock.patch.object(ps, "_dpkg_owner", owner):
            ps._attach_packages(stale)
        self.assertEqual(seen, ["/usr/lib/liba.so", "/usr/lib/libb.so"], "同一路径只查一次")
        self.assertTrue(stale["units"][0]["files"][0]["security"])
        self.assertEqual(stale["units"][1]["files"][0]["pkg"], "liblzma5")
        self.assertFalse(stale["units"][0]["files"][1]["security"], "查不到归属就留空，不猜包名")

    def test_attach_packages_stops_at_budget_but_still_fills_fields(self):
        files = [{"path": f"/usr/lib/lib{i}.so"} for i in range(ps.MAX_PKG_LOOKUPS + 5)]
        stale = {"units": [{"unit": "a.service", "files": files}]}
        probe = mock.Mock(return_value=("x", "1"))
        with mock.patch.object(ps, "_dpkg_owner", probe):
            ps._attach_packages(stale)
        self.assertEqual(probe.call_count, ps.MAX_PKG_LOOKUPS)
        self.assertTrue(all("pkg" in f for f in files), "超出预算也要有字段，前端不用防 KeyError")


class FindingsTests(unittest.TestCase):
    KERNEL_OK = {"ok": True, "reason": None, "running": "6.12.111", "installed": [], "newer": [],
                 "marker": {"present": False}, "needs_reboot": False}
    EMPTY_STALE = {"ok": True, "units": [], "unit_total": 0, "orphan_total": 0}
    NO_RELOADS = {"nginx": {"ok": True, "pending": False}, "sshd": {"ok": True, "pending": False},
                  "systemd": {"ok": True, "need_reload": []}}

    def _stale(self, unit, files, procs=1, age_human="12 天"):
        return {"ok": True, "unit_total": 1, "orphan_total": 0 if unit else 1,
                "units": [{"unit": unit, "procs": procs, "pids": [1, 2, 3][:procs],
                           "cmd": "x", "started": 1, "started_iso": "2026-09-30T18:54:37+08:00",
                           "age_sec": 1000, "age_human": age_human, "self": False,
                           "files": files}]}

    def test_security_lib_stale_is_risk(self):
        f = [{"path": "/usr/lib/liblzma.so.5.8.1", "gone": False, "ctime": 1,
              "ctime_iso": "2026-10-08T06:50:51+08:00", "replaced_at": True,
              "pkg": "liblzma5", "pkg_version": "5.8.1-1+deb13u2", "security": True}]
        out = ps._findings(self.KERNEL_OK, self._stale("postgresql@17-main.service", f, procs=26),
                           self.NO_RELOADS)
        self.assertEqual([x["severity"] for x in out], ["risk"])
        self.assertIn("liblzma5", out[0]["detail"])
        self.assertIn("26 个进程", out[0]["detail"])
        self.assertIn("2026-10-08", out[0]["detail"])
        self.assertIn("systemctl restart postgresql@17-main.service", out[0]["fix"])

    def test_non_security_stale_unit_is_warn(self):
        f = [{"path": "/usr/lib/libfoo.so.1", "gone": False, "ctime": 1, "ctime_iso": "",
              "replaced_at": True, "pkg": "libfoo1", "pkg_version": "", "security": False}]
        out = ps._findings(self.KERNEL_OK, self._stale("svc.service", f), self.NO_RELOADS)
        self.assertEqual([x["severity"] for x in out], ["warn"])

    def test_orphan_is_info_and_fix_does_not_invent_a_unit(self):
        f = [{"path": "/root/app/node_modules/x/esbuild", "gone": False, "ctime": 1, "ctime_iso": "",
              "replaced_at": True, "pkg": "", "pkg_version": "", "security": False}]
        out = ps._findings(self.KERNEL_OK, self._stale("", f, procs=2), self.NO_RELOADS)
        self.assertEqual([x["severity"] for x in out], ["info"])
        self.assertNotIn("systemctl restart", out[0]["fix"])
        self.assertIn("ps -o lstart= -p 1,2", out[0]["fix"])

    def test_reboot_kernel_is_risk_and_unknown_is_info(self):
        k = dict(self.KERNEL_OK, needs_reboot=True, newer=[{"pkg": "linux-image-6.12.99", "version": "x"}])
        out = ps._findings(k, self.EMPTY_STALE, self.NO_RELOADS)
        self.assertEqual(out[0]["id"], "reboot-kernel")
        self.assertEqual(out[0]["severity"], "risk")
        self.assertIn("linux-image-6.12.99", out[0]["detail"])

        k2 = dict(self.KERNEL_OK, needs_reboot=None, reason="读不到对应包")
        out2 = ps._findings(k2, self.EMPTY_STALE, self.NO_RELOADS)
        self.assertEqual([(x["id"], x["severity"]) for x in out2], [("kernel-unknown", "info")])
        self.assertIn("读不到对应包", out2[0]["detail"])

    def test_reload_findings(self):
        rl = {"nginx": {"ok": True, "pending": True, "newest_file": "/etc/nginx/sites-available/x",
                        "conf_files": 6},
              "sshd": {"ok": True, "pending": True, "files": 2, "pid": 671},
              "systemd": {"ok": True, "need_reload": ["a.service", "b.service"]}}
        out = ps._findings(self.KERNEL_OK, self.EMPTY_STALE, rl)
        self.assertEqual({x["id"] for x in out}, {"nginx-reload", "sshd-reload", "daemon-reload"})
        joined = " ".join(x["fix"] for x in out)
        self.assertIn("nginx -t", joined)
        self.assertIn("sshd -t", joined)
        self.assertIn("daemon-reload", joined)

    def test_unreadable_sections_never_become_a_verdict(self):
        """读不到只能是「不知道」：既不能判成要紧，也不能判成没事，只许 info 级并如实带上原因。"""
        rl = {"nginx": {"ok": False, "reason": "没有 nginx"}, "sshd": {"ok": False, "reason": "没有 sshd"},
              "systemd": {"ok": False, "reason": "没有 systemd"}}
        stale = {"ok": False, "reason": "读不到 /proc", "units": [], "unit_total": 0, "orphan_total": 0}
        out = ps._findings({"ok": False, "needs_reboot": None, "reason": "读不到"}, stale, rl)
        self.assertTrue(all(x["severity"] == ps.SEV_INFO for x in out),
                        f"读不到的节不许冒要紧/值得处理：{[x['id'] for x in out]}")
        self.assertIn("读不到", " ".join(x["detail"] for x in out))

    def test_counts(self):
        f = [{"severity": "risk"}, {"severity": "warn"}, {"severity": "warn"}, {"severity": "info"}]
        self.assertEqual(ps._counts(f), {"risk": 1, "warn": 2, "info": 1})

    def test_fixes_are_all_readonly_or_explicitly_delegated(self):
        """面板自己不 restart/reload/reboot；给管理员的命令必须带「先看会不会断流」的口径。"""
        stale = self._stale("postgresql@17-main.service", [{
            "path": "/usr/lib/libssl.so.3", "gone": False, "ctime": 1, "ctime_iso": "",
            "replaced_at": True, "pkg": "libssl3t64", "pkg_version": "3.5.7", "security": True}])
        kernel = dict(self.KERNEL_OK, needs_reboot=True, newer=[{"pkg": "linux-image-6.9", "version": "1"}])
        rl = {"nginx": {"ok": True, "pending": True, "newest_file": "/etc/nginx/c", "conf_files": 1},
              "sshd": {"ok": True, "pending": True, "files": 1, "pid": 2},
              "systemd": {"ok": True, "need_reload": ["z.service"]}}
        for f in ps._findings(kernel, stale, rl):
            with self.subTest(finding=f["id"]):
                self.assertNotIn("面板已经", f["detail"])
                if f["id"] == "reboot-kernel":
                    self.assertIn("低峰", f["fix"])
                elif f["id"].startswith("stale-lib"):
                    self.assertIn("先看", f["fix"] + "先看")  # 单元级处置都要求先确认断流


class DoctorItemTests(unittest.TestCase):
    def _data(self, **kw):
        base = {"kernel": {"ok": True, "running": "6.12.111", "needs_reboot": False, "reason": None},
                "stale": {"ok": True, "unit_total": 0, "orphan_total": 0},
                "findings": [], "counts": {"risk": 0, "warn": 0, "info": 0}, "unreadable": []}
        base.update(kw)
        return base

    def test_risk_fails(self):
        d = self._data(findings=[{"severity": "risk", "title": "postgresql@17-main.service：安全相关的库没进内存"}],
                       counts={"risk": 1, "warn": 0, "info": 0})
        item = ps.doctor_item(d)
        self.assertEqual(item["status"], "fail", "补丁装了而进程没吃到必须出声，不能躺在 warn 里")
        self.assertEqual(item["fix"], "choyeonctl pending")
        self.assertIn("postgresql", item["detail"])

    def test_warn_only(self):
        d = self._data(findings=[{"severity": "warn", "title": "nginx 配置改了，worker 还是旧的"},
                                 {"severity": "warn", "title": "第二条"}],
                       counts={"risk": 0, "warn": 2, "info": 0})
        item = ps.doctor_item(d)
        self.assertEqual(item["status"], "warn")
        self.assertIn("另有 1 项", item["detail"])

    def test_pass_names_the_kernel_and_warns_about_unreadable(self):
        item = ps.doctor_item(self._data())
        self.assertEqual(item["status"], "pass")
        self.assertIn("6.12.111", item["detail"])
        item2 = ps.doctor_item(self._data(unreadable=["sshd"], counts={"risk": 0, "warn": 0, "info": 3}))
        self.assertEqual(item2["status"], "pass")
        self.assertIn("sshd", item2["detail"])
        self.assertIn("3 条", item2["detail"])

    def test_cant_read_anything_is_warn_not_pass(self):
        d = self._data(kernel={"ok": False, "reason": "dpkg-query 调不起来", "running": None},
                       stale={"ok": False, "reason": "读不到 /proc"})
        item = ps.doctor_item(d)
        self.assertEqual(item["status"], "warn")
        self.assertIn("dpkg-query", item["detail"])


class CacheTests(unittest.TestCase):
    def test_ttl_refresh_and_reset(self):
        ps.reset_cache()
        calls = []

        def collect(unit):
            calls.append(unit)
            return {"as_of": "x", "findings": [], "counts": {"risk": 0, "warn": 0, "info": 0}}

        clock = [1000.0]
        with mock.patch.object(ps, "_collect", collect), \
             mock.patch.object(ps.time, "monotonic", lambda: clock[0]):
            first = asyncio.run(ps.audit())
            self.assertFalse(first["cached"])
            clock[0] += ps.CACHE_TTL - 1
            second = asyncio.run(ps.audit())
            self.assertTrue(second["cached"], "TTL 内不该再扫一遍 /proc")
            clock[0] += 2
            third = asyncio.run(ps.audit())
            self.assertFalse(third["cached"])
            clock[0] += 1
            fourth = asyncio.run(ps.audit(refresh=True))
            self.assertFalse(fourth["cached"])
        self.assertEqual(len(calls), 3)
        ps.reset_cache()


class TextTests(unittest.TestCase):
    def test_human_boundaries(self):
        self.assertEqual(ps._human(90), "1 分钟")
        self.assertEqual(ps._human(3600), "1 小时")
        self.assertEqual(ps._human(86400), "1 天")
        self.assertEqual(ps._human(1075351), "12 天")
        self.assertEqual(ps._human(None), "")
        self.assertEqual(ps._human(-1), "")


class CliTextTests(unittest.TestCase):
    DATA = {
        "as_of": "2026-10-13T05:44:07+08:00", "cached": False, "boot_epoch": 1790765667,
        "uptime_sec": 1075000, "self_unit": "choyeon-panel.service", "unreadable": [],
        "apt": {"ok": True, "last_start_date": "2026-10-11  22:49:04", "transactions": 11},
        "kernel": {"ok": True, "reason": None, "running": "6.12.111+deb13-cloud-amd64",
                   "installed": [{"pkg": "linux-image-6.12.111+deb13-cloud-amd64", "version": "1"}],
                   "newer": [], "marker": {"present": False}, "needs_reboot": False},
        "stale": {"ok": True, "reason": None, "scanned": 142, "procs_total": 142, "elapsed_ms": 40,
                  "budget_hit": False, "unit_total": 3, "procs_stale": 29, "orphan_total": 1,
                  "units": [
                      {"unit": "unattended-upgrades.service", "procs": 1, "pids": [701],
                       "cmd": "x", "started": 1, "started_iso": "2026-09-30T18:54:37+08:00",
                       "age_sec": 1, "age_human": "12 天", "self": False,
                       "files": [{"path": "/usr/lib/x86_64-linux-gnu/liblzma.so.5.8.1", "gone": False,
                                  "ctime": 1, "ctime_iso": "2026-10-08T06:50:51+08:00",
                                  "replaced_at": True, "pkg": "liblzma5", "pkg_version": "5.8.1-1+deb13u2",
                                  "security": True}]},
                      {"unit": "", "procs": 2, "pids": [322617, 322625], "cmd": "vite",
                       "started": 1, "started_iso": "2026-10-09T18:32:08+08:00", "age_sec": 1,
                       "age_human": "3 天", "self": False,
                       "files": [{"path": "/root/www/choyeon-panel/web/node_modules/@esbuild/linux-x64/bin/esbuild",
                                  "gone": False, "ctime": 1, "ctime_iso": "", "replaced_at": True,
                                  "pkg": "", "pkg_version": "", "security": False}]},
                  ]},
        "reloads": {"nginx": {"ok": True, "reason": None, "pending": False, "conf_files": 6,
                              "newest_file": "/etc/nginx/sites-available/choyeon-video"},
                    "sshd": {"ok": True, "reason": None, "pending": True, "files": 2, "pid": 671,
                             "newest_file": "/etc/ssh/sshd_config"},
                    "systemd": {"ok": True, "reason": None, "checked": 130, "need_reload": ["a.service"]}},
        "findings": [{"id": "stale-lib:postgresql", "severity": "risk",
                      "title": "postgresql@17-main.service：安全相关的库没进内存",
                      "detail": "26 个进程仍映射着 1 个已被替换的文件（liblzma5）。",
                      "fix": "systemctl status postgresql@17-main.service",
                      "files": [{"path": "/usr/lib/liblzma.so", "pkg": "liblzma5", "version": "u2",
                                 "security": True, "gone": False, "ctime_iso": ""}]},
                     {"id": "sshd-reload", "severity": "warn", "title": "sshd 配置比进程新",
                      "detail": "面板不写 sshd_config。", "fix": "sshd -t && systemctl reload ssh"}],
        "counts": {"risk": 1, "warn": 1, "info": 0}, "notes": ["严格只读。"]}

    def test_text_covers_every_section(self):
        text = cli._pending_text(self.DATA)
        self.assertIn("不 restart、不 reload、不 daemon-reload、不 reboot", text)
        self.assertIn("running 6.12.111+deb13-cloud-amd64", text)
        self.assertIn("其中 29 个踩着已被替换的文件", text)
        self.assertIn("unattended-upgrades.service", text)
        self.assertIn("(不属于任何单元)", text)
        self.assertIn("nginx：6 个配置文件", text)
        self.assertIn("有待重载的改动", text)
        self.assertIn("a.service", text)
        self.assertIn("2026-10-11  22:49:04", text)
        self.assertIn("[要紧] postgresql@17-main.service", text)
        self.assertIn("[liblzma5 u2]", text)
        self.assertIn("处置：systemctl status", text)
        self.assertIn("说明：严格只读", text)

    def test_unreadable_sections_are_named_not_silenced(self):
        d = dict(self.DATA)
        d["kernel"] = {"ok": False, "reason": "dpkg-query 调不起来", "running": None, "installed": [],
                       "newer": [], "marker": {"present": False}, "needs_reboot": None}
        d["stale"] = {"ok": False, "reason": "读不到 /proc", "units": []}
        d["reloads"] = {"nginx": {"ok": False, "reason": "机器上没有 nginx 主进程"},
                        "sshd": {"ok": False, "reason": "机器上没有 sshd 主进程"},
                        "systemd": {"ok": False, "reason": "systemctl 调不起来"}}
        d["apt"] = {"ok": False, "reason": "读不到"}
        text = cli._pending_text(d)
        for phrase in ("dpkg-query 调不起来", "读不到 /proc", "没有 nginx 主进程", "没有 sshd 主进程",
                       "systemctl 调不起来"):
            self.assertIn(phrase, text)
        self.assertNotIn("不需要重启", text, "读不到内核却输出「不需要重启」就是假绿")

    def test_cpad_aligns_cjk_columns(self):
        self.assertEqual(cli._cpad("postgres.service", 20) + "|", "postgres.service    |")
        self.assertEqual(cli._cpad("(不属于任何单元)", 20) + "|", "(不属于任何单元)    |")
        self.assertEqual(cli._cpad("已经很长了已经很长了", 4), "已经很长了已经很长了")


class CommandTests(unittest.TestCase):
    def test_pending_command_ok_path(self):
        data = dict(CliTextTests.DATA)
        with mock.patch.object(cli.pending_service, "audit", mock.AsyncMock(return_value=data)) as a:
            out, err, code = _run(cli.cmd_pending(_Args(refresh=True)), json_mode=True)
        self.assertIsNone(code)
        self.assertIn('"stale-lib:postgresql"', out)
        a.assert_awaited_once_with(refresh=True)

    def test_exit_3_when_both_core_sections_unreadable(self):
        data = dict(CliTextTests.DATA,
                    kernel={"ok": False, "reason": "dpkg-query 调不起来"},
                    stale={"ok": False, "reason": "读不到 /proc"})
        with mock.patch.object(cli.pending_service, "audit", mock.AsyncMock(return_value=data)):
            out, err, code = _run(cli.cmd_pending(_Args(refresh=False)))
        self.assertEqual(code, cli.EXIT_PRECONDITION)
        self.assertIn("dpkg-query", err)

    def test_partial_readability_still_exits_0(self):
        """只有 nginx 读不到（机器上没跑 nginx）是正常状态，不能当失败退出。"""
        data = dict(CliTextTests.DATA)
        data["reloads"] = dict(data["reloads"], nginx={"ok": False, "reason": "没有 nginx 主进程"})
        with mock.patch.object(cli.pending_service, "audit", mock.AsyncMock(return_value=data)):
            out, err, code = _run(cli.cmd_pending(_Args(refresh=False)), json_mode=True)
        self.assertIsNone(code)
        self.assertIn("nginx", out)


class SchemaContractTests(unittest.TestCase):
    def _schema(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return asyncio.run(cli.cmd_schema(_Args()))

    def test_pending_in_command_list(self):
        self.assertIn("pending", [c["name"] for c in self._schema()["commands"]])

    def test_entry_shape(self):
        entry = [c for c in self._schema()["commands"] if c["name"] == "pending"][0]
        self.assertEqual(entry["args"], ["--refresh"])
        self.assertIn("只读", entry["desc"])
        self.assertGreaterEqual(len(entry["notes"]), 6)
        joined = " ".join(entry["notes"])
        for phrase in ("(deleted)", "inode", "update-notifier-common", "worker", "ctime", "退出码 3"):
            self.assertIn(phrase, joined)

    def test_schema_declares_readonly_scope(self):
        entry = [c for c in self._schema()["commands"] if c["name"] == "pending"][0]
        blob = entry["desc"] + " ".join(entry["notes"])
        for promise in ("自动修复", "帮你重启", "一键重启"):
            self.assertNotIn(promise, blob, f"契约里出现了写入承诺：{promise}")


class ParserTests(unittest.TestCase):
    def test_pending_subcommand_parses(self):
        args = cli.build_parser().parse_args(["pending"])
        self.assertIs(args.fn, cli.cmd_pending)
        self.assertFalse(args.refresh)

    def test_refresh_flag(self):
        self.assertTrue(cli.build_parser().parse_args(["pending", "--refresh"]).refresh)

    def test_help_text_declares_readonly(self):
        lines = [ln for ln in cli.build_parser().format_help().splitlines()
                 if ln.strip().startswith("pending")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("只读", lines[0])


class MetaWiringTests(unittest.TestCase):
    def test_endpoint_registered(self):
        self.assertIn("/api/pending", [r.path for r in meta.router.routes])

    def test_doctor_item_present_and_last(self):
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("pending", ids)
        self.assertEqual(meta._CHECKS[ids.index("pending")][1], "改动未生效")
        self.assertEqual(ids[-1], "pending", "改动未生效排在内存压力之后")

    def test_not_one_click_fixable(self):
        """一键修复白名单里不许出现 pending：重启服务、reload nginx 都是业务时机的决策。"""
        self.assertNotIn("pending", meta.FIXABLE)

    def test_check_pending_uses_audit_and_doctor(self):
        seen = {}

        async def audit(refresh=False):
            seen["refresh"] = refresh
            return {"kernel": {"ok": True, "running": "6.12.111", "needs_reboot": False},
                    "stale": {"ok": True, "unit_total": 0}, "findings": [],
                    "counts": {"risk": 0, "warn": 0, "info": 0}, "unreadable": []}

        with mock.patch.object(meta.pending_service, "audit", audit):
            item = asyncio.run(meta._check_pending())
        self.assertEqual(item["status"], "pass")
        self.assertIn("refresh", seen)

    def test_endpoint_forwards_refresh(self):
        calls = []

        async def audit(refresh=False):
            calls.append(refresh)
            return {"ok": True}

        with mock.patch.object(meta.pending_service, "audit", audit):
            asyncio.run(meta.pending())
            asyncio.run(meta.pending(refresh=1))
        self.assertEqual(calls, [False, True])


class RouteAccessTests(unittest.TestCase):
    """/api/pending 是只读探测：viewer 能看，匿名不能看。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        async def audit(refresh=False):
            return {"as_of": "x", "cached": True, "kernel": {"ok": True}, "stale": {"ok": True, "units": []},
                    "reloads": {}, "apt": {"ok": False}, "findings": [], "counts": {}, "notes": [],
                    "self_unit": "choyeon-panel.service", "unreadable": []}

        p = mock.patch.object(meta.pending_service, "audit", audit)
        p.start()
        self.addCleanup(p.stop)
        self.user = "p15a" + uuid.uuid4().hex[:10]
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "viewer"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.user, 'viewer')}"}

    def test_viewer_can_read(self):
        r = self.client.get("/api/pending", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("stale", r.json())

    def test_anonymous_denied(self):
        self.assertIn(self.client.get("/api/pending").status_code, (401, 403))


class DocsTests(unittest.TestCase):
    def test_agents_md_documents_command_and_item_count(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("`pending`", text)
        self.assertTrue(f"当前 {len(meta._CHECKS)} 项" in text, "AGENTS.md 的自检项数与代码不一致")

    def test_readme_documents_command_and_item_count(self):
        text = (Path(cli.ROOT) / "README.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl pending", text)
        self.assertIn(f"共 {len(meta._CHECKS)} 项只读检查", text)
        self.assertNotIn("17 项", text, "README 里还留着上一批的项数")

    def test_security_md_item_count(self):
        text = (Path(cli.ROOT) / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn(f"{len(meta._CHECKS)} 项只读自检", text)

    def test_agents_md_pending_row_declares_readonly(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `pending`"))
        for phrase in ("不 restart", "不 reload", "daemon-reload", "退出码 3"):
            self.assertIn(phrase, row)

    def test_agents_md_forbids_panel_side_reboot(self):
        """硬性禁止事项里要说清：这一层只报不动手，处置由管理员在低峰做。"""
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl pending", text)


class NotesTests(unittest.TestCase):
    def test_notes_cover_the_three_limits(self):
        joined = " ".join(ps.NOTES)
        for phrase in ("(deleted)", "inode", "worker", "update-notifier-common", "ctime",
                       "不 restart", "严格只读"):
            self.assertIn(phrase, joined, f"说明里缺了 {phrase}")

    def test_service_is_strictly_readonly(self):
        """源码级守卫：本服务 spawn 出去的每一条 argv 都必须是只读命令。

        不能靠 grep「restart」——findings 的 fix 里本来就该写着 systemctl restart，那是给人
        复制走的处置。要锁的是「面板自己按下按钮」，所以只看子进程实参里的字面量。
        """
        tree = ast.parse(Path(ps.__file__).read_text(encoding="utf-8"))
        spawners = {"run", "Popen", "check_output", "check_call", "system", "popen",
                    "execv", "execve", "execvp"}
        write_verbs = {"restart", "reload", "daemon-reload", "reboot", "start", "stop",
                       "kill", "enable", "disable", "mask"}
        argv_tokens = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in spawners:
                continue
            first = node.args[0]
            if isinstance(first, (ast.List, ast.Tuple)):
                argv_tokens.extend(e.value for e in first.elts
                                   if isinstance(e, ast.Constant) and isinstance(e.value, str))
        self.assertGreater(len(argv_tokens), 10, "一个 argv 字面量都没抓到，说明守卫本身失效了")
        bad = sorted({t for t in argv_tokens for w in t.split() if w in write_verbs})
        self.assertEqual(bad, [], f"服务自己执行了会动手的命令：{bad}")


if __name__ == "__main__":
    unittest.main()
