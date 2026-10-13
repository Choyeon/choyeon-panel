"""批次⑰B：同一个事实只能有一个出处。

四条都是「两处各算一套，于是两处读数互相矛盾」这一类：

1. 概览的磁盘容量写死根分区，而「存储占用」页签读的是数据目录所在的那块盘；
2. 分区警戒线后端有常量 `DISK_TIGHT_PCT`，前端又自己抄了一个 90；
3. 应用详情自己写了第二套字节格式化器（GB/MB 阈值、精度、单位都和全站出处不一致）；
4. 内核包只认 `ii`，`iU`/`iF`（解包未配置／配置失败）被静默丢掉，于是「装到一半的新内核」
   在面板上是「不需要重启」。第 4 条和 ⑰A 同源：读不到不等于没有。

夹具里的 dpkg-query 输出与 test_batch15a 一样取自生产机实拍。
"""
import asyncio
import re
import unittest
from pathlib import Path
from unittest import mock

from app.services import pending_service as ps
from app.services import storage_service as ss
from app.services import system_metrics as sm

SRC = Path(__file__).resolve().parents[2]

DPKG_REAL = (
    "linux-image-6.12.111+deb13-cloud-amd64|6.12.111-1|ii \n"
    "linux-image-6.12.48+deb13-cloud-amd64|6.12.48-1|ii \n"
    "linux-image-cloud-amd64|6.12.111-1|ii\n"
)


def _num_gt(a, b):
    digits = lambda v: [int(x) for x in re.findall(r"\d+", v)]  # noqa: E731
    return digits(a) > digits(b)


def _kernel_state(stdout, rc=0, exists=False, gt=None):
    gt = gt or _num_gt

    def fake_run(cmd, **kw):
        if cmd[:2] == ["dpkg", "--compare-versions"]:
            return mock.Mock(returncode=0 if gt(cmd[2], cmd[4]) else 1, stdout="", stderr="")
        return mock.Mock(returncode=rc, stdout=stdout, stderr="" if rc == 0 else "boom")

    with mock.patch.object(ps.subprocess, "run", fake_run), \
         mock.patch.object(ps.os.path, "exists", lambda p: exists), \
         mock.patch.object(ps.os, "uname",
                           lambda: mock.Mock(release="6.12.111+deb13-cloud-amd64")):
        return ps._kernel_state(1790765667)


class HalfInstalledKernelTests(unittest.TestCase):
    """`iU` 的内核包不是「没装」，也不是「装好了」——它只能被如实说成判不出。"""

    def test_unpack_failed_kernel_is_not_clean(self):
        out = _kernel_state(DPKG_REAL + "linux-image-6.13.0+deb13-cloud-amd64|6.13.0-1|iU\n")
        self.assertTrue(out["ok"])
        self.assertIsNone(out["needs_reboot"], "半装的内核不能判成不需要重启")
        self.assertIn("半装", out["reason"])
        self.assertEqual([x["pkg"] for x in out["partial"]],
                         ["linux-image-6.13.0+deb13-cloud-amd64"])
        self.assertEqual(out["partial"][0]["status"], "iU")
        # 半装的包不能同时出现在「已装」里，否则前端把它当生效版本比出新旧
        self.assertNotIn("linux-image-6.13.0+deb13-cloud-amd64",
                         [x["pkg"] for x in out["installed"]])

    def test_config_failed_state_is_also_reported(self):
        out = _kernel_state(DPKG_REAL + "linux-image-6.13.0+deb13-cloud-amd64|6.13.0-1|iF\n")
        self.assertIsNone(out["needs_reboot"])
        self.assertEqual(out["partial"][0]["status"], "iF")

    def test_removed_with_config_is_still_ignored(self):
        """rc 是「已卸载只剩配置文件」，盘上没有可启动的内核；把它算进半装就每台机器都要重启。"""
        out = _kernel_state("linux-image-6.9.9-amd64|6.9.9-1|rc \n" + DPKG_REAL)
        self.assertEqual(out["partial"], [])
        self.assertFalse(out["needs_reboot"])

    def test_all_installed_is_still_a_plain_verdict(self):
        out = _kernel_state(DPKG_REAL)
        self.assertFalse(out["needs_reboot"])
        self.assertEqual(out["partial"], [])
        self.assertIsNone(out["reason"])

    def test_newer_installed_kernel_still_means_reboot(self):
        out = _kernel_state(DPKG_REAL + "linux-image-6.13.0+deb13-cloud-amd64|6.13.0-1|ii\n")
        self.assertTrue(out["needs_reboot"])
        self.assertEqual([x["pkg"] for x in out["newer"]],
                         ["linux-image-6.13.0+deb13-cloud-amd64"])

    def test_distro_marker_wins_over_a_half_installed_package(self):
        """发行版自己已经挂了重启标记，就不要因为另一个包装坏了改成「判不出」。"""
        out = _kernel_state(DPKG_REAL + "linux-image-6.13.0+deb13-cloud-amd64|6.13.0-1|iU\n",
                            exists=True)
        self.assertTrue(out["needs_reboot"])
        self.assertTrue(out["marker"]["present"])


class DiskPartitionSourceTests(unittest.TestCase):
    """概览和「存储占用」必须说的是同一块盘。"""

    def _capture(self, data_dir, isdir=True):
        seen = []

        def fake_usage(path):
            seen.append(path)
            return mock.Mock(total=1000, used=400, free=600)

        with mock.patch.object(sm.shutil, "disk_usage", fake_usage), \
             mock.patch.object(sm.os.path, "isdir", lambda p: isdir), \
             mock.patch.object(sm.config, "DATA_DIR", data_dir):
            du, path = sm._disk_sample()
        return du, path, seen

    def test_it_reads_the_data_dir_partition(self):
        du, path, seen = self._capture("/srv/cp-data")
        self.assertEqual(seen, ["/srv/cp-data"])
        self.assertEqual(path, "/srv/cp-data")
        self.assertEqual((du.total, du.used), (1000, 400))

    def test_missing_data_dir_falls_back_and_says_which_path_it_used(self):
        du, path, seen = self._capture("/srv/cp-data", isdir=False)
        self.assertEqual(seen, ["/"])
        self.assertEqual(path, "/", "退回根分区可以，但必须把实际读到的路径带出去")

    def test_both_sampling_paths_use_the_helper(self):
        src = (SRC / "backend/app/services/system_metrics.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("du, disk_path = _disk_sample()"), 2,
                         "psutil 与 /proc 两条采样路径都要走同一个出处")
        self.assertNotIn('disk_usage("/")', src.replace("shutil.disk_usage(path)", ""))

    def test_proc_raw_publishes_the_path(self):
        stat = ("cpu  100 0 50 800 0 0 10 0 0 0\n"
                "cpu0 100 0 50 800 0 0 10 0 0 0\n"
                "intr 1\n"
                "ctxt 1000\n"
                "btime 1\n"
                "processes 5\n"
                "procs_running 1\n"
                "procs_blocked 0\n")
        files = {
            "/proc/stat": stat,
            "/proc/meminfo": "MemTotal: 1000 kB\nMemAvailable: 500 kB\n",
            "/proc/net/dev": " face |bytes packets\n  eth0: 100 1 0 0 0 0 0 0 0 200 0\n",
            "/proc/diskstats": "   8        0 sda rd 1 2 3 4 5 6 7 8 9 10\n",
        }
        with mock.patch.object(sm, "_read", lambda p: files.get(p, "")), \
             mock.patch.object(sm.os, "listdir", lambda p: ["1"]), \
             mock.patch.object(sm.os, "getloadavg", lambda: (0.1, 0.2, 0.3)), \
             mock.patch.object(sm, "_uptime", lambda: 10.0), \
             mock.patch.object(sm, "_disk_sample",
                               lambda: (mock.Mock(total=1000, used=400, free=600), "/srv/d")):
            out = sm._proc_raw()
        self.assertEqual(out["diskPath"], "/srv/d")
        self.assertEqual(out["diskTotal"], 1000)

    def test_dashboard_names_the_partition_it_drew(self):
        src = (SRC / "web/src/views/Dashboard.vue").read_text(encoding="utf-8")
        self.assertIn("stat.value.diskPath", src)
        self.assertNotIn("label: '磁盘 /'", src, "标签写死 / 就是在指着一块没读过的盘")

    def test_snapshot_relayed_the_path_it_received(self):
        src = (SRC / "backend/app/services/system_metrics.py").read_text(encoding="utf-8")
        self.assertIn('"diskPath": raw.get("diskPath")', src,
                      "raw 读了哪块盘，快照就得带着哪块盘；丢了它前端只能继续写 /")


class TightThresholdSingleSourceTests(unittest.TestCase):
    """阈值由后端随载荷给出；前端再抄一个数字，后端改阈值时页面就比自检宽松。

    断言打在 `audit()` 的返回上，不打在 `_collect()` 的源码上：`/api/storage` 返回的是
    `audit()`，第一版把键加在 `_collect()` 里，全套用例照绿而线上前端读到 null，
    `tightPct` 退化成 0，「分区已用 95%」在页签上仍是绿灯。夹具因此故意不给 `_collect()`
    塞 tight_pct——阈值必须是出口自己给出的，不是从走盘结果里转手的。
    """

    def setUp(self):
        ss.reset_cache()
        self.addCleanup(ss.reset_cache)

    def _audit(self, used_pct=50):
        async def fake_journal():
            return 1024, "Archived and active journals take up 1.0K in the file system."

        collected = {
            "roots": [],
            "disk": {"ok": True, "path": "/data", "used_pct": used_pct,
                     "total": 40 * 1024**3, "free": 20 * 1024**3, "free_human": "20.0GB"},
            "big_files": [],
            "mounts": {"ok": True, "items": [], "total": 0, "omitted": 0, "panel_mount": "/data"},
        }
        with mock.patch.object(ss, "_journal_bytes", fake_journal), \
             mock.patch.object(ss, "_collect", lambda: collected):
            return asyncio.run(ss.audit(refresh=True))

    def test_payload_carries_the_threshold(self):
        self.assertEqual(self._audit()["tight_pct"], ss.DISK_TIGHT_PCT)

    def test_cache_hit_still_carries_it(self):
        # cached() 是 dict(_cache["data"]) 的浅拷贝，键在缓存里就得在响应里；
        # 只在首次计算时补键的话，TTL 内刷新页面就会把阈值弄丢。
        first = self._audit()
        second = asyncio.run(ss.audit())
        self.assertTrue(second["cached"])
        self.assertEqual(second["tight_pct"], first["tight_pct"])

    def test_the_page_and_the_finding_share_one_line(self):
        # 同一条"分区已用 90%"要么在自检里是要紧项、在页签上也得是紧；差一档就都不出声。
        self.assertEqual(self._audit(ss.DISK_TIGHT_PCT - 1)["counts"]["risk"], 0)
        d = self._audit(ss.DISK_TIGHT_PCT)
        self.assertIn("tight", [f["id"] for f in d["findings"]])
        self.assertEqual(d["counts"]["risk"], 1)
        self.assertEqual(d["tight_pct"], ss.DISK_TIGHT_PCT)

    def test_the_route_returns_this_payload_untouched(self):
        src = (SRC / "backend/app/routers/meta.py").read_text(encoding="utf-8")
        self.assertIn("return await storage_service.audit(", src,
                      "路由要是自己重拼字段，出口就变成第二个出处，这条修复会再次静默失效")

    def test_frontend_reads_the_payload_instead_of_a_literal(self):
        src = (SRC / "web/src/views/Services.vue").read_text(encoding="utf-8")
        self.assertIn("storageData.value?.tight_pct", src)
        self.assertNotIn("used_pct ?? 0) >= 90", src)
        self.assertNotIn("inode_pct ?? 0) >= 90", src)
        self.assertIn("tightPct.value", src)

    def test_storage_page_names_which_partition_is_clear(self):
        src = (SRC / "web/src/views/Services.vue").read_text(encoding="utf-8")
        self.assertIn("disk.path || '数据目录'", src,
                      "「没有异常占用」要说清是哪块盘没有异常")


class SharedByteFormatterTests(unittest.TestCase):
    def test_app_detail_no_longer_carries_a_second_formatter(self):
        src = (SRC / "web/src/views/AppDetail.vue").read_text(encoding="utf-8")
        self.assertNotIn("function fmtMem", src)
        self.assertIn("from '../composables/useFormat'", src)
        self.assertIn("fmtBytes(p.memoryBytes)", src)


class StorageThresholdStillUsedTests(unittest.TestCase):
    """前端换了出处，后端那条判定不能跟着漂：89% 不该被点名，阈值本身要算进去。"""

    def test_finding_threshold_is_the_constant(self):
        src = (SRC / "backend/app/services/storage_service.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(len(re.findall(r"DISK_TIGHT_PCT", src)), 3,
                                "判定与载荷都只能引用这一个常量")
        self.assertEqual(ss.DISK_TIGHT_PCT, 90)


if __name__ == "__main__":
    unittest.main()
