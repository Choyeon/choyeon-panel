"""批次108：速率算不出分母的那一轮不许写成 0——CPU／网速／IO 记 NULL，界面写「未读到」。

坏法是每一轮重启都会来一次的那一种：`_sample()` 的首帧没有 `_prev`，占用率与速率都是
「两次累计量的差 ÷ 时间」，没有差值就没有答案。原来这些位置写死 0.0/0，于是：

1. 概览第一格画成 `0 B/s`、CPU 环指着 0%，看着像一台闲得发慌的机器，而它只是刚起来；
2. `metrics_store` 把 0 落进 raw 行的 net_rx/io_r，小时均值 `_avg` 只挡 None，
   于是这个从没发生过的测量进了分母，那一小时被系统性算轻；
3. 巡检 `len(history) < 2` 那一档把 CPU 判成「使用率正常」并写进告警历史——
   面板自己在启动后的第一分钟里给出一条绿灯结论，依据是一个占位数。

`0` 保留给「有分母且确实没涨」：那是真读数，改成 null 反而会把真实的空闲藏起来。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_OWN_DATA_DIR = "CP_DATA_DIR" not in os.environ
if _OWN_DATA_DIR:
    os.environ["CP_DATA_DIR"] = tempfile.mkdtemp(prefix="cp_b108_data_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as dbm  # noqa: E402
from app.routers import system as system_router  # noqa: E402
from app.services import alerts_service as ALS  # noqa: E402
from app.services import metrics_store, system_metrics  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
UI_TS = (SRC / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
DASHBOARD_VUE = (SRC / "web" / "src" / "views" / "Dashboard.vue").read_text(encoding="utf-8")
GAUGE_VUE = (SRC / "web" / "src" / "components" / "StatGauge.vue").read_text(encoding="utf-8")


def _raw(rx: int, tx: int, idle: int = 1000, total: int = 2000, io_r: int = 0, io_w: int = 0) -> dict:
    return {
        "idle": idle, "total": total, "perCore": [(idle, total)],
        "memTotal": 10000, "memAvailable": 6000,
        "rx": rx, "tx": tx, "nics": {"eth0": (rx, tx)}, "nicUp": {"eth0": True},
        "diskTotal": 20000, "diskUsed": 5000, "ioRead": io_r, "ioWrite": io_w,
        "load": [1.0, 1.0, 1.0], "uptime": 100, "procs": 100, "source": "test",
    }


class TestRateMath(unittest.TestCase):
    """`_rate` 的四种输入：能算的算，算不出的说算不出。"""

    def test_normal_window(self):
        self.assertEqual(system_metrics._rate(3000, 1000, 2.0), 1000)

    def test_idle_still_reads_zero(self):
        self.assertEqual(system_metrics._rate(1000, 1000, 2.0), 0,
                         "有分母且累计量确实没涨，这个 0 是真读数，不能被并进「未读到」")

    def test_no_denominator_is_none(self):
        self.assertIsNone(system_metrics._rate(9000, 1000, 0.0), "dt=0 是在同一瞬间连采两次")
        self.assertIsNone(system_metrics._rate(9000, 1000, -1.0), "时钟倒退同样给不出分母")

    def test_counter_wrap_is_none_not_zero(self):
        self.assertIsNone(system_metrics._rate(100, 5000, 2.0),
                          "回绕这一轮差值不成立；写成 0 会被当成真的零流量并进小时均值")


class TestSnapshotFirstFrame(unittest.TestCase):
    """首帧整份载荷：绝对量照旧是准的，差值量全是 null，且不进实时曲线。"""

    BASE = 1_700_000_000.0

    def setUp(self):
        for attr, val in (("_prev", None), ("_latest", None), ("_latest_at", 0.0)):
            setattr(system_metrics, attr, val)
        system_metrics._history.clear()
        self.mono = self.BASE

    def _snap(self, raw, advance: float):
        self.mono += advance
        clock = mock.Mock()
        clock.time.return_value = self.mono
        clock.monotonic.return_value = self.mono
        with mock.patch.object(system_metrics, "_psutil_raw", return_value=raw), \
             mock.patch.object(system_metrics, "time", clock), \
             mock.patch.object(system_metrics, "_RT_INTERVAL", 0.0):
            return asyncio.run(system_metrics.snapshot())

    def test_first_frame_carries_no_difference_values(self):
        d = self._snap(_raw(5_000_000, 9_000_000, io_r=10**10, io_w=10**11), 0.0)
        self.assertIsNone(d["cpu"])
        self.assertFalse(d["measured"], "读取者要能一眼看出这轮没量到，而不是拿历史长度猜")
        self.assertEqual((d["netRx"], d["netTx"], d["ioRead"], d["ioWrite"]), (None,) * 4)
        self.assertEqual(d["cpuPerCore"], [None], "每核占用同样是差值，一起记 null")
        self.assertEqual((d["nics"][0]["rx"], d["nics"][0]["tx"]), (None, None))
        self.assertEqual((d["netTotalRx"], d["netTotalTx"]), (5_000_000, 9_000_000),
                         "开机累计字节是绝对量，量得到了就得照给")
        self.assertEqual((d["memTotal"], d["memUsed"], d["diskUsed"]), (10000, 4000, 5000))
        self.assertEqual(len(system_metrics._history), 0, "没有分母的一帧不许占实时曲线一格")

    def test_second_frame_reports_the_real_zero(self):
        self._snap(_raw(5_000_000, 9_000_000), 0.0)
        # idle 与 total 同量增长 = 这一窗确实一点没忙：0% 是真读数
        d = self._snap(_raw(5_000_000, 9_000_000, idle=3000, total=4000), 2.0)
        self.assertTrue(d["measured"])
        self.assertEqual(d["cpu"], 0.0)
        self.assertEqual(d["cpuPerCore"], [0.0])
        self.assertEqual((d["netRx"], d["netTx"]), (0, 0), "确实没流量就是 0，不是未读到")
        self.assertEqual(len(system_metrics._history), 1)


class TestStatsRoute(unittest.TestCase):
    """/api/system/stats 把「这轮量没量到」原样带给前端，不在路由里补 0。"""

    def test_null_rates_and_measured_flag_survive_the_route(self):
        async def cold():
            return {"cpu": None, "measured": False, "netRx": None, "netTx": None,
                    "ioRead": None, "ioWrite": None, "memUsed": 4000, "memTotal": 10000,
                    "diskUsed": 5000, "diskTotal": 20000, "load": [0.0, 0.0, 0.0],
                    "nics": [{"name": "eth0", "rx": None, "tx": None, "totalRx": 1, "totalTx": 2, "up": True}]}

        thr = mock.patch.object(
            system_router.alerts_service, "resource_thresholds",
            lambda: {"cpu_warn": 80, "cpu_crit": 90, "mem_warn": 90, "mem_crit": 95})
        disk = mock.patch.object(system_router.alerts_service, "disk_thresholds",
                                 lambda: {"warn": 70, "crit": 80})
        with (mock.patch.object(system_router.system_metrics, "snapshot", cold), thr, disk):
            out = asyncio.run(system_router.sys_stats())
        self.assertIsNone(out["cpu"])
        self.assertFalse(out["measured"])
        self.assertIsNone(out["netRx"])
        self.assertIsNone(out["nics"][0]["rx"])


class TestMetricsStoreWritesNull(unittest.TestCase):
    """落盘：量不出的一轮写 NULL，且它不能进小时均值。"""

    def setUp(self):
        metrics_store._prev_cum = None
        self.hour = (int(time.time()) // 3600 - 5) * 3600
        self.addCleanup(dbm.execute, "DELETE FROM metrics WHERE gran='raw' AND ts>=? AND ts<?",
                        (self.hour, self.hour + 3600))
        self.addCleanup(dbm.execute, "DELETE FROM metrics WHERE gran='hour' AND ts=?", (self.hour,))

    def _ins(self, rows: list[tuple]) -> None:
        """rows: (ts, cpu, mem_used, mem_total, net_rx, net_tx, io_r, io_w, procs)，容量列填常量。"""
        with dbm._lock:
            dbm.conn.executemany(
                "INSERT OR REPLACE INTO metrics(ts,gran,cpu,mem_used,mem_total,net_rx,net_tx,"
                "disk_used,disk_total,load1,io_r,io_w,procs)"
                " VALUES(?,'raw',?,?,?,?,?,20000,10000,1.5,?,?,?)",
                rows,
            )
            dbm.conn.commit()

    def test_first_sample_row_is_null_not_zero(self):
        async def cold():
            return {"cpu": None, "measured": False, "memUsed": 4000, "memTotal": 10000,
                    "diskUsed": 5000, "diskTotal": 20000, "load": [1.0, 1.0, 1.0], "procs": 100}

        ts = int(time.time())
        self.addCleanup(dbm.execute, "DELETE FROM metrics WHERE gran='raw' AND ts=?", (ts,))
        with (mock.patch.object(system_metrics, "snapshot", cold),
              mock.patch.object(metrics_store, "_read_counters",
                                lambda: {"rx": 10**10, "tx": 10**10, "ioRead": 10**10, "ioWrite": 10**10})):
            row = asyncio.run(metrics_store.sample(ts=ts))
        self.assertEqual((row["cpu"], row["netRx"], row["netTx"], row["ioR"], row["ioW"]),
                         (None,) * 5)
        stored = dbm.query_one("SELECT * FROM metrics WHERE gran='raw' AND ts=?", (ts,))
        for col in ("cpu", "net_rx", "net_tx", "io_r", "io_w"):
            self.assertIsNone(stored[col], f"{col} 要真的是 NULL：0 会被小时均值当成一次测量")

    def test_rollup_skips_unmeasured_rounds(self):
        # 一轮没量到 + 一轮 100 B/s：均值必须是 100，不是被 NULL 摊成 50
        self._ins([(self.hour + 30, 20.0, 4000, 10000, None, None, 0, 0, 100),
                   (self.hour + 60, 60.0, 4000, 10000, 100, 200, 300, 400, 100)])
        out = asyncio.run(metrics_store.rollup_hour(self.hour))
        self.assertEqual(out["netRx"], 100.0, "NULL 不进分母，均值只算真测到的那轮")
        self.assertEqual(out["cpu"], 40.0)

    def test_hour_with_only_unmeasured_rounds_has_no_average(self):
        self._ins([(self.hour + 30, None, 4000, 10000, None, None, None, None, 100)])
        out = asyncio.run(metrics_store.rollup_hour(self.hour))
        self.assertIsNone(out["cpu"], "整小时都没量到 → 这一小时的 CPU 均值也是未读到")
        self.assertIsNone(out["netRx"])
        self.assertEqual(out["memUsed"], 4000, "容量是绝对量，照样有均值")


class TestAlertsSkipTheUnmeasured(unittest.TestCase):
    """巡检：CPU 没差值就不判它，但内存／负载这些绝对量照常判，且这一节要进「本轮没查成」。"""

    SNAP = {"cpu": None, "measured": False, "memTotal": 1000, "memUsed": 200,
            "load": [0.4, 0.3, 0.2], "cpuCores": 4}

    def setUp(self):
        self.codes = ("cpu", "mem", "load1")
        dbm.execute("DELETE FROM alerts_log WHERE code IN (?,?,?)", self.codes)
        dbm.execute("DELETE FROM settings WHERE key LIKE ?", ("streak:%",))
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code IN (?,?,?)", self.codes)
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("streak:%",))

    def _round(self, snap: dict) -> tuple[set[str], list[str]]:
        async def fake_snap():
            return dict(snap)

        async def no_notify(_text):
            return False

        with (ALS.collect_unchecked() as sink,
              mock.patch.object(ALS.system_metrics, "snapshot", fake_snap),
              mock.patch.object(ALS, "notify", no_notify)):
            asyncio.run(ALS._check_resources())
        marks = ",".join("?" for _ in self.codes)
        rows = dbm.query(f"SELECT code FROM alerts_log WHERE code IN ({marks})", self.codes)
        return {r["code"] for r in rows}, list(sink)

    def test_null_cpu_is_not_judged_and_says_so(self):
        codes, unchecked = self._round(self.SNAP)
        self.assertNotIn("cpu", codes, "没有分母的这一轮，既不能报「CPU 过高」也不能报「CPU 正常」")
        self.assertEqual(codes, {"mem", "load1"}, "内存与负载是绝对量，没有分母也量得准，不能跟着跳过")
        self.assertEqual(len(unchecked), 1, f"CPU 量不出要进本轮清单：{unchecked}")
        self.assertIn("CPU／内存／负载阈值", unchecked[0])
        self.assertIn("CPU", unchecked[0])

    def test_measured_zero_is_still_judged(self):
        codes, unchecked = self._round(dict(self.SNAP, cpu=0.0, measured=True))
        self.assertEqual(codes, {"cpu", "mem", "load1"}, "量到 0% 也是一次真测量，该留一行「正常」的痕")
        self.assertEqual(unchecked, [])


class TestFrontendReadsTheFlag(unittest.TestCase):
    """前端不许替后端把 null 印成 0（措辞仍只有 UNKNOWN_LABEL 一个出处）。"""

    def test_rate_helpers_exist_and_reuse_the_unknown_label(self):
        self.assertIn("export function rateNum", UI_TS)
        self.assertIn("export function rateText", UI_TS)
        self.assertEqual(UI_TS.count("export const UNKNOWN_LABEL"), 1, "「未读到」还是只有一个出处")
        self.assertIn("n === null ? UNKNOWN_LABEL", UI_TS,
                      "速率格的第三态必须走那个短名，不能在页面里再写一份")

    def test_dashboard_no_longer_coalesces_rates(self):
        for bad in ("fmtBytes(s.ioRead || 0)", "fmtBytes(s.netTx || 0)", "fmtBytes(s.netRx || 0)",
                    "fmtBytes(one.tx || 0)", "fmtBytes(one.rx || 0)", "?.[i] ?? 0"):
            self.assertNotIn(bad, DASHBOARD_VUE, f"「{bad}」把没量到印成了 0")
        self.assertIn("rateText(s.netTx)", DASHBOARD_VUE)
        self.assertIn("rateNum(s.netRx)", DASHBOARD_VUE)
        self.assertNotIn("未读到", DASHBOARD_VUE, "短名从 ui.ts 引进来，页面不另抄一份")

    def test_gauge_unit_is_not_printed_after_a_word(self):
        self.assertIn("v-if=\"unit && numeric !== null\"", GAUGE_VUE,
                      "圆心写「未读到」时不能跟着一个 %：那是「未读到%」")
        self.assertIn("numeric.value === null ? '' : props.unit", GAUGE_VUE,
                      "读屏走同一判法，别让人听见「CPU 未读到%」")


if __name__ == "__main__":
    unittest.main()
