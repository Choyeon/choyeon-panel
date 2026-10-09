"""metrics_store 回归测试。

锁定五条真实语义：累计计数器回绕不产生负值/开机量 spike、首帧记 0；
小时 rollup 重跑只替换不重复、raw 过期后不销毁已固化的 hour 行；
history 分桶保证响应不超 300 点；非法 range 回 400 中文错误且不泄漏路径；
采样循环单轮报错不中断、取消后干净退出并把心跳清零。
"""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import quote

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_metrics_ut_"))

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app.routers import metrics as metrics_router  # noqa: E402
from app.services import metrics_store, system_metrics  # noqa: E402

SNAP = {
    "cpu": 40.0, "memUsed": 4000, "memTotal": 10000, "diskUsed": 5000, "diskTotal": 20000,
    "load": [1.5, 1.2, 1.0], "procs": 200,
}


async def _fake_snap():
    return dict(SNAP)


def _counters(rx, tx, rd, wr):
    return lambda: {"rx": rx, "tx": tx, "ioRead": rd, "ioWrite": wr}


def _clear_metrics(gran: str | None = None) -> None:
    sql = "DELETE FROM metrics" + (" WHERE gran=?" if gran else "")
    with dbm._lock:
        dbm.conn.execute(sql, (gran,) if gran else ())
        dbm.conn.commit()


def _insert_raw_rows(rows: list[tuple]) -> None:
    """rows: [(ts, cpu, net_rx, io_r, io_w, procs)]，其余列填常量。一次提交，避免逐行 fsync 拖慢测试。"""
    with dbm._lock:
        dbm.conn.executemany(
            "INSERT OR REPLACE INTO metrics(ts,gran,cpu,mem_used,mem_total,net_rx,net_tx,"
            "disk_used,disk_total,load1,io_r,io_w,procs) VALUES(?,'raw',?,4000,10000,?,20,5000,20000,1.5,?,?,?)",
            [(ts, cpu, rx, ir, iw, procs) for ts, cpu, rx, ir, iw, procs in rows],
        )
        dbm.conn.commit()


class MetricsCase(unittest.TestCase):
    def setUp(self):
        _clear_metrics()
        metrics_store._prev_cum = None
        metrics_store._beat = 0.0


class TestCounterDelta(MetricsCase):
    """累计量差值：首帧 0、正常轮出速率、回绕记 0、回绕后以新基线恢复。"""

    def test_delta_sequence(self):
        seq = [
            (10**10, 2 * 10**10, 10**11, 3 * 10**10),   # 开机以来的累计量，首帧必须忽略
            (10**10 + 3000, 2 * 10**10 + 6000, 10**11 + 30000, 3 * 10**10 + 15000),
            (500, 700, 900, 1100),                       # 计数器回绕（重启/换网卡/数据源切换）
            (2000, 700, 900, 1100),                      # 回绕后以新基线继续
        ]
        out = []
        with mock.patch.object(system_metrics, "snapshot", _fake_snap):
            for i, c in enumerate(seq):
                with mock.patch.object(metrics_store, "_read_counters", _counters(*c)):
                    out.append(asyncio.run(metrics_store.sample(ts=1000 + i * 30)))
        first = out[0]
        self.assertEqual((first["netRx"], first["netTx"], first["ioR"], first["ioW"]), (0, 0, 0, 0))
        self.assertEqual(first["cpu"], 40.0)
        self.assertEqual(first["load1"], 1.5)
        self.assertEqual((out[1]["netRx"], out[1]["netTx"], out[1]["ioR"], out[1]["ioW"]), (100, 200, 1000, 500))
        self.assertEqual((out[2]["netRx"], out[2]["netTx"], out[2]["ioR"], out[2]["ioW"]), (0, 0, 0, 0))
        self.assertEqual(out[3]["netRx"], 50)
        self.assertEqual((out[3]["netTx"], out[3]["ioR"], out[3]["ioW"]), (0, 0, 0))
        row = dbm.query_one("SELECT * FROM metrics WHERE gran='raw' AND ts=?", (1090,))
        self.assertEqual(row["net_rx"], 50)
        for r in out:
            for k in ("netRx", "netTx", "ioR", "ioW"):
                self.assertGreaterEqual(r[k], 0, "任何一轮都不得为负")

    def test_same_timestamp_yields_zero(self):
        with mock.patch.object(system_metrics, "snapshot", _fake_snap), \
             mock.patch.object(metrics_store, "_read_counters", _counters(100, 100, 100, 100)):
            asyncio.run(metrics_store.sample(ts=2000))
            with mock.patch.object(metrics_store, "_read_counters", _counters(999, 999, 999, 999)):
                again = asyncio.run(metrics_store.sample(ts=2000))  # dt=0：没有分母就没有速率
        self.assertEqual(again["netRx"], 0)


class TestRollup(MetricsCase):
    def test_idempotent_replace(self):
        hour = (int(time.time()) // 3600 - 2) * 3600
        _insert_raw_rows([(hour + i * 300, 10.0 + i * 10, 100, 200, 300, 100 + i) for i in range(10)])
        a = asyncio.run(metrics_store.rollup_hour(hour))
        b = asyncio.run(metrics_store.rollup_hour(hour))
        rows = dbm.query("SELECT * FROM metrics WHERE gran='hour'")
        self.assertEqual(len(rows), 1, "同一小时重跑必须只留一行")
        self.assertEqual(rows[0]["ts"], hour)
        self.assertAlmostEqual(rows[0]["cpu"], 55.0)  # 10..100 的均值
        self.assertEqual(a["maxCpu"], 100.0)
        self.assertEqual(b["maxCpu"], 100.0)
        self.assertEqual(rows[0]["procs"], 109)      # 容量/进程数取该小时最后一行

    def test_empty_hour_keeps_existing_row(self):
        hour = (int(time.time()) // 3600 - 3) * 3600
        _insert_raw_rows([(hour + i * 300, 20.0, 5, 6, 7, 8) for i in range(5)])
        asyncio.run(metrics_store.rollup_hour(hour))
        _clear_metrics("raw")  # 模拟 raw 已过 24h 保留期
        self.assertIsNone(asyncio.run(metrics_store.rollup_hour(hour)))
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM metrics WHERE gran='hour'")["c"], 1,
                         "重跑不能把已固化的 hour 行删掉")


class TestHistory(MetricsCase):
    def test_bucket_caps_points(self):
        now = int(time.time()) // 30 * 30
        rows = [(now - (2880 - i) * 30, 30.0 + i % 7, i, i, i, 100) for i in range(2880)]
        _insert_raw_rows(rows)
        for rng in ("1h", "6h", "24h"):
            data = asyncio.run(metrics_store.history(rng))
            self.assertLessEqual(data["meta"]["points"], metrics_store.MAX_POINTS, rng)
            self.assertEqual(data["meta"]["gran"], "raw")
            self.assertGreaterEqual(data["meta"]["interval"], metrics_store.SAMPLE_INTERVAL)
            lens = {len(data[k]) for k in ("ts", "cpu", "netRx", "ioR", "procs", "load1")}
            self.assertEqual(len(lens), 1, "所有数组必须等长")
            if data["ts"]:
                self.assertEqual(data["ts"][0] % 1000, 0)  # 毫秒时间戳
        d1h = asyncio.run(metrics_store.history("1h"))
        self.assertEqual(d1h["meta"]["interval"], 30, "1h 窗口按 raw 自然间隔分桶")

    def test_7d_reads_hour_rows(self):
        now_h = int(time.time()) // 3600 * 3600
        _insert_raw_rows([(now_h - i * 3600, 10.0, i, i, i, i) for i in range(3)])
        for i in range(3):
            asyncio.run(metrics_store.rollup_hour(now_h - i * 3600))
        data = asyncio.run(metrics_store.history("7d"))
        self.assertEqual(data["meta"]["gran"], "hour")
        self.assertEqual(data["meta"]["interval"], 3600)
        self.assertEqual(data["meta"]["points"], 3)

    def test_empty_window_returns_aligned_arrays(self):
        data = asyncio.run(metrics_store.history("6h"))
        self.assertEqual(data["meta"]["points"], 0)
        self.assertEqual(data["ts"], [])
        self.assertEqual(data["cpu"], [])


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _clear_metrics()
        app = FastAPI()
        app.include_router(metrics_router.router)
        cls.client = TestClient(app)

    def test_bad_range_400_chinese_no_path_leak(self):
        for bad in ("1d", "30m", "../../etc/passwd"):
            resp = self.client.get(f"/api/system/history?range={quote(bad)}")
            self.assertEqual(resp.status_code, 400, bad)
            err = resp.json()["error"]
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in err), err)
            self.assertNotIn(bad, err, "错误信息不得回显原始输入")
            self.assertNotIn("..", err)
            self.assertNotIn("app/", err)

    def test_history_ok(self):
        resp = self.client.get("/api/system/history?range=1h")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("meta", resp.json())

    def test_metrics_status_shape(self):
        resp = self.client.get("/api/system/metrics-status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("rows", "lastSampleTs", "samplerAlive", "sampleInterval", "retention"):
            self.assertIn(key, body)
        self.assertFalse(body["samplerAlive"], "loop 没跑过时不许谎报存活")


class TestSamplerLoop(unittest.IsolatedAsyncioTestCase):
    async def test_stops_cleanly_on_cancel(self):
        _clear_metrics()
        calls: list = []

        async def fake_sample(ts=None):
            calls.append(ts)
            return {}

        metrics_store._beat = 0.0
        with mock.patch.object(metrics_store, "sample", fake_sample):
            task = asyncio.create_task(metrics_store.loop(interval=0.01))
            await asyncio.sleep(0.06)
            self.assertFalse(task.done())
            self.assertGreater(len(calls), 0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(task.cancelled(), "取消必须以 CancelledError 退出，而不是静默吞掉")
            self.assertEqual(metrics_store._beat, 0.0, "退出后心跳清零，status 不再误报存活")

    async def test_error_in_one_round_does_not_kill_loop(self):
        _clear_metrics()
        state = {"n": 0}

        async def flaky_sample(ts=None):
            state["n"] += 1
            raise RuntimeError("单次采样炸了")

        with mock.patch.object(metrics_store, "sample", flaky_sample):
            task = asyncio.create_task(metrics_store.loop(interval=0.01))
            await asyncio.sleep(0.06)
            self.assertFalse(task.done(), "一轮异常不能终止后台任务")
            self.assertGreaterEqual(state["n"], 2, "异常后必须继续下一轮")
            self.assertEqual(metrics_store._beat, 0.0, "没有成功轮次就不能有心跳")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
