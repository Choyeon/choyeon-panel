"""仪表盘两条接口的回归测试：每网卡速率明细 + 概览计数。

锁定的语义：
- 每网卡速率与整机一样要防计数器回绕，新出现的网卡首帧记 0 而不是开机累计量；
- lo 不进列表（回环流量对运维没有意义，还会把"总发送"虚高）；
- 网卡链路状态读不到时按 up 处理，未知不等于故障；
- 概览的站点数在 nginx 缺失时是 null + reason，不是 0（0 会被读成"没有站点"）。
"""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_dash_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_dashboard_api_roots_"))

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app.routers import system as system_router  # noqa: E402
from app.services import system_metrics  # noqa: E402


def _raw(nics: dict[str, tuple[int, int]], nic_up: dict[str, bool] | None = None) -> dict:
    rx = sum(v[0] for k, v in nics.items())
    tx = sum(v[1] for k, v in nics.items())
    return {
        "idle": 1000, "total": 2000, "perCore": [(500, 1000)],
        "memTotal": 10000, "memAvailable": 6000,
        "rx": rx, "tx": tx, "nics": nics, "nicUp": nic_up if nic_up is not None else {},
        "diskTotal": 20000, "diskUsed": 5000, "ioRead": 0, "ioWrite": 0,
        "load": [1.0, 1.0, 1.0], "uptime": 100, "procs": 100, "source": "test",
    }


class NicRateCase(unittest.TestCase):
    BASE = 1_700_000_000.0

    def setUp(self):
        system_metrics._prev = None
        system_metrics._history.clear()
        system_metrics._latest = None
        system_metrics._latest_at = 0.0
        self._now = self.BASE

    def _snap(self, raw, advance: float = 0.0):
        """自己拨钟：速率断言一旦依赖真实墙钟就会因几毫秒抖动差 1。

        _RT_INTERVAL 置 0 是为了让每一帧都真的采样——本类测的是差值数学，
        复用节奏由 SampleReuseCase 按生产节奏单独测。
        """
        self._now += advance
        clock = mock.Mock()
        clock.time.return_value = self._now
        clock.monotonic.return_value = self._now
        with mock.patch.object(system_metrics, "_psutil_raw", return_value=raw), \
             mock.patch.object(system_metrics, "time", clock), \
             mock.patch.object(system_metrics, "_RT_INTERVAL", 0.0):
            return asyncio.run(system_metrics.snapshot())

    def test_first_frame_has_zero_rates_but_keeps_totals(self):
        d = self._snap(_raw({"eth0": (1000, 500)}))
        self.assertEqual(len(d["nics"]), 1)
        n = d["nics"][0]
        self.assertEqual(n["name"], "eth0")
        self.assertEqual((n["rx"], n["tx"]), (0, 0), "首帧没有分母，速率记 0")
        self.assertEqual((n["totalRx"], n["totalTx"]), (1000, 500))

    def test_second_frame_divides_by_elapsed(self):
        self._snap(_raw({"eth0": (1000, 500)}))
        d = self._snap(_raw({"eth0": (3000, 900)}), advance=2.0)
        n = d["nics"][0]
        self.assertEqual((n["rx"], n["tx"]), (1000, 200))
        self.assertEqual((d["netRx"], d["netTx"]), (1000, 200))
        self.assertEqual((d["netTotalRx"], d["netTotalTx"]), (3000, 900))

    def test_counter_wrap_and_new_nic_report_zero(self):
        self._snap(_raw({"eth0": (5000, 5000)}))
        d = self._snap(_raw({"eth0": (100, 100), "eth1": (9000, 4000)}), advance=1.0)
        by = {n["name"]: n for n in d["nics"]}
        self.assertEqual((by["eth0"]["rx"], by["eth0"]["tx"]), (0, 0), "回绕按 0 处理，不报负数")
        self.assertEqual((by["eth1"]["rx"], by["eth1"]["tx"]), (0, 0), "新出现的网卡首帧记 0")

    def test_loopback_excluded_and_order_stable(self):
        d = self._snap(_raw({"wlan0": (10, 10), "eth0": (20, 20)}))
        self.assertEqual([n["name"] for n in d["nics"]], ["eth0", "wlan0"])
        self.assertNotIn("lo", [n["name"] for n in d["nics"]])

    def test_missing_operstate_is_not_reported_as_down(self):
        d = self._snap(_raw({"eth0": (10, 10)}))
        self.assertTrue(d["nics"][0]["up"], "拿不到链路状态时不许谎报网卡断开")
        d = self._snap(_raw({"eth0": (10, 10)}, {"eth0": False}))
        self.assertFalse(d["nics"][0]["up"])

    def test_proc_fallback_collects_per_nic(self):
        files = {
            "/proc/stat": "cpu  100 0 0 900 0 0 0 0\ncpu0 100 0 0 900 0 0 0 0\n",
            "/proc/meminfo": "MemTotal: 10000 kB\nMemAvailable: 6000 kB\n",
            "/proc/net/dev": (
                "Inter-|   Receive                                                |  Transmit\n"
                " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets"
                " errs drop fifo colls carrier compressed\n"
                "    lo: 999999      100    0    0    0     0          0         0   999999    100"
                "    0    0    0     0       0          0\n"
                "  eth0:   1200       10    0    0    0     0          0         0     3400     20"
                "    0    0    0     0       0          0\n"
            ),
            "/proc/diskstats": "",
        }

        def fake_read(path):
            if path.startswith("/sys/class/net"):
                raise OSError("no operstate")
            return files[path]

        disk = type("D", (), {"total": 20000, "used": 5000})()
        with mock.patch.object(system_metrics, "_read", side_effect=fake_read), \
             mock.patch.object(system_metrics.shutil, "disk_usage", return_value=disk):
            raw = system_metrics._proc_raw()
        self.assertEqual(raw["nics"], {"eth0": (1200, 3400)})
        self.assertEqual((raw["rx"], raw["tx"]), (1200, 3400), "lo 不计入整机总量")
        self.assertEqual(raw["nicUp"], {"eth0": True}, "读不到 sysfs 时按 up 处理")


class SampleReuseCase(unittest.TestCase):
    """快照按墙钟节奏采样，不按请求节奏。

    读取者不止一个：Dashboard 每 3s 轮询，metrics_store 的后台采样器每 30s 也要调
    snapshot()，多开一个标签页就多一个写入者。旧实现让它们各自 diff 全局 `_prev`，
    抢在别人之后那一刻 elapsed 只有毫秒级——网速被短分母放大成尖峰，cpu 的 jiffies
    差在该窗口内取整为 0 就算出 0%，并作为"真实采样点"永久留在曲线里。
    """

    BASE = 1_700_000_000.0

    def setUp(self):
        system_metrics._prev = None
        system_metrics._history.clear()
        system_metrics._latest = None
        system_metrics._latest_at = 0.0
        self.mono = self.BASE

    @staticmethod
    def _counters(idle: int, total: int, rx: int, tx: int) -> dict:
        d = _raw({"eth0": (rx, tx)})
        d["idle"], d["total"], d["perCore"] = idle, total, [(idle, total)]
        return d

    def _run(self, raw, advance: float):
        """按生产节奏走：不压 _RT_INTERVAL，拨 monotonic。"""
        self.mono += advance
        clock = mock.Mock()
        clock.time.return_value = self.mono
        clock.monotonic.return_value = self.mono
        with mock.patch.object(system_metrics, "_psutil_raw", return_value=raw), \
             mock.patch.object(system_metrics, "time", clock):
            return asyncio.run(system_metrics.snapshot())

    def test_second_reader_inside_the_interval_reuses_one_sample(self):
        first = self._run(self._counters(1000, 2000, 1000, 500), 0.0)
        second = self._run(self._counters(1050, 2100, 91000, 45500), 0.4)
        self.assertIs(second, first, "区间内复用同一份快照，不再各算各的窗口")
        self.assertEqual(len(system_metrics._history), 1, "一个区间只落一个采样点")

    def test_a_late_reader_does_not_read_zero_or_a_spike(self):
        self._run(self._counters(1000, 2000, 1000, 500), 0.0)
        good = self._run(self._counters(1050, 2100, 3000, 1500), 2.0)
        self.assertAlmostEqual(good["cpu"], 50.0)
        self.assertEqual(good["netRx"], 1000)
        late = self._run(self._counters(1050, 2100, 3000, 1500), 0.1)
        self.assertIs(late, good)
        self.assertEqual((late["cpu"], late["netRx"]), (50.0, 1000), "短窗口既不该归零也不该放大")

    def test_window_accumulates_across_reused_reads(self):
        self._run(self._counters(1000, 2000, 1000, 500), 0.0)
        for _ in range(2):
            self._run(self._counters(1000, 2000, 1000, 500), 0.4)
        d = self._run(self._counters(1000, 2000, 3800, 1900), 2.0)
        self.assertEqual(d["netRx"], 1000, "分母是整个采样窗口 2.8s，不是最后一次请求的间隔")
        self.assertEqual(len(system_metrics._history), 2)

    def test_history_is_capped_for_the_realtime_curve(self):
        self.assertEqual(
            system_metrics._history.maxlen, 60,
            "仪表盘只画最后 60 点，多出的 180 点每 3s 白序列化+白解析一次",
        )


class OverviewCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(system_router.router)
        cls.client = TestClient(app)

    def _seed(self):
        with dbm._lock:
            dbm.conn.executemany(
                "INSERT OR IGNORE INTO apps(id,name,type,repo_url,path,start_cmd) VALUES(?,?,?,?,?,?)",
                [(9001, "cp-ut-a1", "node", "u", "/p", "s"), (9002, "cp-ut-a2", "python", "u", "/p", "s")],
            )
            dbm.conn.executemany(
                "INSERT OR IGNORE INTO cron_task(id,name,expr,enabled) VALUES(?,?,?,?)",
                [(9001, "cp-ut-t1", "* * * * *", 1), (9002, "cp-ut-t2", "0 * * * *", 0)],
            )
            dbm.conn.executemany(
                "INSERT OR IGNORE INTO backups(id,kind,target,enabled) VALUES(?,?,?,?)",
                [(9001, "sqlite", "cp-ut", 1), (9002, "pg", "cp-ut", 0)],
            )
            dbm.conn.executemany(
                "INSERT INTO alerts_log(ts,level,code,message) VALUES(?,?,?,?)",
                [(time.strftime("%Y-%m-%dT%H:%M:%S"), "crit", "cp-ut-c1", "m1"),
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), "ok", "cp-ut-c2", "m2")],
            )
            dbm.conn.commit()

    def _clean(self):
        with dbm._lock:
            for sql in ("DELETE FROM apps WHERE id>9000", "DELETE FROM cron_task WHERE id>9000",
                        "DELETE FROM backups WHERE id>9000", "DELETE FROM alerts_log WHERE code LIKE 'cp-ut-%'"):
                dbm.conn.execute(sql)
            dbm.conn.commit()

    def _counts(self):
        """本套测试共用一个临时库，别的用例也会留下行，所以断言增量而不是绝对值。"""
        with dbm._lock:
            q = lambda sql: dbm.conn.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "apps": q("SELECT COUNT(*) FROM apps"),
                "cron": q("SELECT COUNT(*) FROM cron_task"),
                "cronEnabled": q("SELECT COALESCE(SUM(enabled),0) FROM cron_task"),
                "backups": q("SELECT COUNT(*) FROM backups WHERE enabled=1"),
                "openAlerts": q("SELECT COUNT(*) FROM alerts_log WHERE acked=0"),
            }

    def test_counts_come_from_local_tables(self):
        self._clean()
        base = self._counts()
        self._seed()
        self.addCleanup(self._clean)
        with mock.patch("app.routers.sites._conf_dirs_available", return_value=False):
            body = self.client.get("/api/system/overview").json()
        self.assertEqual(body["apps"], base["apps"] + 2)
        self.assertEqual((body["cron"], body["cronEnabled"]), (base["cron"] + 2, base["cronEnabled"] + 1))
        self.assertEqual(body["backups"], base["backups"] + 1, "只数启用的备份任务")
        self.assertEqual(body["openAlerts"], base["openAlerts"] + 2)

    def test_missing_nginx_is_null_with_reason_not_zero(self):
        with mock.patch("app.routers.sites._conf_dirs_available", return_value=False):
            body = self.client.get("/api/system/overview").json()
        self.assertIsNone(body["sites"])
        self.assertTrue(body["sitesReason"])

    def test_site_count_dedupes_domains_across_configs(self):
        scanned = [{"domains": ["A.example.com", "b.example"], "https": True},
                   {"domains": ["a.example.com"], "https": False}]
        with mock.patch("app.routers.sites._conf_dirs_available", return_value=True), \
             mock.patch("app.routers.sites._scan_nginx_sites", return_value=scanned):
            body = self.client.get("/api/system/overview").json()
        self.assertEqual(body["sites"], 2, "同一域名在多个 vhost 里只算一个站点")
        self.assertIsNone(body["sitesReason"])

    def test_scan_failure_degrades_to_null(self):
        with mock.patch("app.routers.sites._conf_dirs_available", return_value=True), \
             mock.patch("app.routers.sites._scan_nginx_sites", side_effect=OSError("boom")):
            body = self.client.get("/api/system/overview").json()
        self.assertIsNone(body["sites"])
        self.assertIn("站点扫描失败", body["sitesReason"])


if __name__ == "__main__":
    unittest.main()
