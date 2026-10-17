"""批次⑱B：一条告警配置的「默认值 + 可填区间」只有一个出处。

⑱A 把磁盘那条线收进 `disk_thresholds()`，但那条线**本身**还写着两份：`_int_setting(
"alert_disk_pct", 85, 50, 99)` 的三个实参，和 `extras.ALERT_INT_RANGE` 里的 (50, 99)。
CPU / 内存 / 负载 / 保留天数同样如此，前端还各抄了一遍：`Settings.vue` 的
`ALERT_DEFAULTS` 写着八个默认值，模板里 `NInputNumber` 又写着八对 `:min/:max`。
四份之间没有任何机制保证一起改，坏法是三类：
- 库里没写过这个键时，页面显示 A 那份默认、巡检按 B 那份默认办事；
- 管理员照页面填的数被保存校验（C 那份区间）拒绝，或者存进去后被读取侧悄悄钳掉；
- 概览仪表上 CPU/内存的变色档位（70/85、80/90）和设置页那两条配置毫无关系，
  把 CPU 线调到 60 的机器告警早就在响，仪表却还是品牌蓝。

这批把登记表立起来：`alerts_service.ALERT_LIMITS`（key → 默认/下限/上限）是唯一的数字出处，
`setting_int()` 按它读，`alert_limits()` 按它给页面显示，`extras.ALERT_INT_RANGE` 从它派生，
`/api/system/stats` 把 CPU/内存的两档线随采样带出去。用例里的数字一律显式钉住，
不引用产品常量——否则改了默认值用例还跟着绿，那是最坏的假绿。
"""
import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from app import database as dbm
from app import main, security
from app.routers import extras, system
from app.services import alerts_service as als

SRC = Path(__file__).resolve().parents[2]

# 登记表应有的内容。这是一份**快照**：改这里的数字就是"我要改这条配置的默认值或可填范围"，
# 而产品侧只该改 ALERT_LIMITS 一处——两边不一致时这条用例会红，提醒还有别的出处没跟上。
SHIPPED = {
    "alert_disk_pct": (85, 50, 99),
    "alert_ssl_days": (14, 0, 60),
    "alert_cpu_pct": (80, 1, 99),
    "alert_mem_pct": (90, 1, 99),
    "alert_load1_x": (2, 1, 32),
    "alert_probe": (1, 0, 1),
    "alert_keep_days": (30, 1, 365),
    "alert_keep_rows": (2000, 100, 20000),
    "alert_email_port": (587, 1, 65535),
}
NUMERIC_KEYS = sorted(SHIPPED)

# 一批假的登记表取值：证明"读的是这张表"，而不是证明"默认值就是 33"。
STUB = {
    "alert_disk_pct": (70, 50, 99),
    "alert_ssl_days": (21, 0, 60),
    "alert_cpu_pct": (60, 1, 99),
    "alert_mem_pct": (55, 1, 99),
    "alert_load1_x": (3, 1, 32),
    "alert_probe": (0, 0, 1),
    "alert_keep_days": (7, 1, 365),
    "alert_keep_rows": (500, 100, 20000),
    "alert_email_port": (2525, 1, 65535),
}


def _patched_registry(case, table):
    patcher = mock.patch.dict(als.ALERT_LIMITS, table)
    patcher.start()
    case.addCleanup(patcher.stop)


def _clear_keys(case):
    """真实库（线上跑测试时是会话临时库）里可能留着显式配置，用例之间要各归各的默认。"""
    keys = tuple(SHIPPED)
    dbm.execute("DELETE FROM settings WHERE key IN ({})".format(",".join("?" * len(keys))), keys)

    def restore():
        dbm.execute("DELETE FROM settings WHERE key IN ({})".format(",".join("?" * len(keys))), keys)
    case.addCleanup(restore)


class RegistryIsTheSourceTests(unittest.TestCase):
    """读、显示、校验三侧都从 ALERT_LIMITS 取数。"""

    def test_reads_follow_the_registry_not_a_literal(self):
        _clear_keys(self)
        _patched_registry(self, STUB)
        for key, (default, _lo, _hi) in STUB.items():
            self.assertEqual(als.setting_int(key), default, key)

    def test_out_of_range_is_clamped_to_the_registry_bounds(self):
        _clear_keys(self)
        _patched_registry(self, STUB)
        dbm.set_setting("alert_cpu_pct", "0")     # 低于下限 → 钳到 1
        self.assertEqual(als.setting_int("alert_cpu_pct"), 1)
        dbm.set_setting("alert_cpu_pct", "500")   # 高于上限 → 钳到 99
        self.assertEqual(als.setting_int("alert_cpu_pct"), 99)

    def test_junk_falls_back_to_the_registry_default(self):
        _clear_keys(self)
        _patched_registry(self, STUB)
        dbm.set_setting("alert_mem_pct", "abc")
        self.assertEqual(als.setting_int("alert_mem_pct"), STUB["alert_mem_pct"][0],
                         "脏值必须回落到登记表默认，而不是巡检整轮抛 ValueError 后静默")

    def test_shipped_table_is_the_documented_one(self):
        self.assertEqual(dict(als.ALERT_LIMITS), SHIPPED)

    def test_limits_payload_shape(self):
        _patched_registry(self, STUB)
        got = als.alert_limits()
        self.assertEqual(sorted(got), NUMERIC_KEYS)
        for key, (default, lo, hi) in STUB.items():
            self.assertEqual(got[key], {"default": default, "min": lo, "max": hi}, key)

    def test_save_validation_range_is_derived_not_recopied(self):
        self.assertEqual(extras.ALERT_INT_RANGE,
                         {k: (lo, hi) for k, (_, lo, hi) in als.ALERT_LIMITS.items()},
                         "校验区间必须是登记表的投影；两份数字一旦不同步，"
                         "就会出现「页面让你存、巡检按钳制后的值办事」")
        src = (SRC / "backend/app/routers/extras.py").read_text(encoding="utf-8")
        self.assertIn("for k, (_, lo, hi) in alerts_service.ALERT_LIMITS.items()", src,
                      "ALERT_INT_RANGE 又写回字面量元组，就是重开第二个出处")
        for key in ("alert_cpu_pct", "alert_mem_pct", "alert_keep_days"):
            self.assertNotIn(f'"{key}": (', src, f"{key} 的区间不该在 extras 里再出现一次")


class LineComputationsTests(unittest.TestCase):
    """两档线：警戒就是配置线，要紧档比它高 CRIT_STEP 个点（封顶 99）。"""

    def test_disk_lines_follow_the_registry(self):
        _clear_keys(self)
        _patched_registry(self, STUB)
        self.assertEqual(als.disk_thresholds(), {"warn": 70, "crit": 80})

    def test_resource_lines_follow_the_registry(self):
        _clear_keys(self)
        _patched_registry(self, STUB)
        rt = als.resource_thresholds()
        self.assertEqual(rt["cpu_warn"], STUB["alert_cpu_pct"][0])
        self.assertEqual(rt["cpu_crit"], STUB["alert_cpu_pct"][0] + als.CRIT_STEP)
        self.assertEqual(rt["mem_warn"], STUB["alert_mem_pct"][0])
        self.assertEqual(rt["mem_crit"], STUB["alert_mem_pct"][0] + als.CRIT_STEP)

    def test_crit_is_capped_at_99(self):
        _clear_keys(self)
        _patched_registry(self, {"alert_cpu_pct": (95, 1, 99), "alert_mem_pct": (99, 1, 99)})
        rt = als.resource_thresholds()
        self.assertEqual(rt["cpu_crit"], 99)
        self.assertEqual(rt["mem_crit"], 99, "贴着顶格时不能算出 109 这种永远越不过的档")

    def test_load_is_deliberately_not_in_the_gauge_lines(self):
        # 负载环画的是「相对核数」，alert_load1_x 是饱和之上的倍数（默认 ×2 → 200%）。
        # 把它当变色档位等于让负载环永远不会红，所以这一格留物理口径，不并进这张表。
        rt = als.resource_thresholds()
        self.assertFalse([k for k in rt if "load" in k], str(rt))

    def test_the_step_is_defined_once(self):
        src = (SRC / "backend/app/services/alerts_service.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("CRIT_STEP"), 4,
                         "定义一次，磁盘/CPU/内存各用一次；再多一处就是又开了一个出处")
        for rel in ("app/services/storage_service.py", "app/routers/meta.py",
                    "app/routers/system.py",
                    "../web/src/views/Settings.vue", "../web/src/views/Dashboard.vue"):
            body = (SRC / "backend" / rel).read_text(encoding="utf-8")
            self.assertNotIn("CRIT_STEP", body, rel)
        # extras 是把它转交给页面的出口，允许引用、不允许自己算：
        # `alerts_service.CRIT_STEP` 出现，`+ 10` 之类的复刻不出现。
        route = (SRC / "backend/app/routers/extras.py").read_text(encoding="utf-8")
        self.assertIn("alerts_service.CRIT_STEP", route)
        self.assertNotIn("crit_step = 10", route)


class StatsPayloadTests(unittest.TestCase):
    """/api/system/stats 带出 CPU 与内存的两档线，概览仪表只读它。"""

    def setUp(self):
        patcher = mock.patch.object(als, "resource_thresholds",
                                    lambda: {"cpu_warn": 60, "cpu_crit": 70,
                                             "mem_warn": 90, "mem_crit": 99})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stats(self):
        async def fake_snap():
            return {"cpu": 65.0, "memUsed": 8, "memTotal": 10, "load": [1.0, 0.5, 0.2]}
        with mock.patch.object(system.system_metrics, "snapshot", fake_snap):
            return asyncio.run(system.sys_stats())

    def test_all_four_lines_are_in_the_payload(self):
        out = self._stats()
        self.assertEqual((out["cpuWarnPct"], out["cpuCritPct"]), (60, 70))
        self.assertEqual((out["memWarnPct"], out["memCritPct"]), (90, 99))

    def test_the_sampled_numbers_are_untouched(self):
        out = self._stats()
        self.assertEqual(out["cpu"], 65.0)
        self.assertEqual(out["memUsed"], 8)
        self.assertEqual(out["load"], [1.0, 0.5, 0.2], "只是补四个键，不能动采样结果")

    def test_the_route_is_the_one_attaching_them(self):
        # 打在真出口上：⑰B 曾把键加在 `_collect()` 里，全套用例照绿而线上前端读到 null。
        src = (SRC / "backend/app/routers/system.py").read_text(encoding="utf-8")
        self.assertIn("data = await system_metrics.snapshot()", src)
        self.assertIn('data["cpuWarnPct"], data["cpuCritPct"]', src)
        self.assertIn('data["memWarnPct"], data["memCritPct"]', src)


class AlertsRoutePayloadTests(unittest.TestCase):
    """GET /api/settings/alerts 把默认值、可填区间与要紧档步长一起给出。"""

    def setUp(self):
        self.client = TestClient(main.app)
        user = f"lim_{self.__class__.__name__[-6:]}_u"
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        self.token = security.sign_token(user, "admin")
        _clear_keys(self)

    def _get(self):
        return self.client.get("/api/settings/alerts",
                               headers={"Authorization": f"Bearer {self.token}"}).json()

    def test_limits_and_step_are_in_the_response(self):
        _patched_registry(self, STUB)
        d = self._get()
        self.assertEqual(d["crit_step"], als.CRIT_STEP)
        self.assertEqual(d["limits"]["alert_cpu_pct"],
                         {"default": STUB["alert_cpu_pct"][0], "min": 1, "max": 99})
        self.assertEqual(sorted(d["limits"]), NUMERIC_KEYS,
                         "少一个键，设置页那一项就退回空框，看起来像没配置")

    def test_saving_a_value_outside_the_registry_range_is_rejected(self):
        r = self.client.post("/api/settings/alerts", json={"alert_load1_x": "999"},
                             headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(dbm.get_setting("alert_load1_x"))
        r = self.client.post("/api/settings/alerts", json={"alert_load1_x": "3"},
                             headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.get_setting("alert_load1_x"), "3")


class ResourceCheckMessageTests(unittest.TestCase):
    """巡检推给管理员的那句话要带上要紧档，且和仪表读的是同一对数。"""

    SNAP = {"cpu": 95.0, "memTotal": 1000, "memUsed": 950, "load": [0.4, 0.3, 0.2],
            "cpuCores": 4, "history": [{"t": 0, "cpu": 11.0}, {"t": 3000, "cpu": 95.0}]}

    def setUp(self):
        _clear_keys(self)
        for code in ("cpu", "mem"):
            dbm.execute("DELETE FROM alerts_log WHERE code=?", (code,))
            self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("sent:cpu%",))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("sent:mem%",))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("streak:cpu%",))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("streak:mem%",))

    def _rounds(self, n=2):
        sent: list[str] = []

        async def fake_notify(text):
            sent.append(text)
            return False

        async def fake_snap():
            return dict(self.SNAP)

        out = []
        for _ in range(n):
            with (mock.patch.object(als, "run", self._run_stub),
                  mock.patch.object(als, "disk_usage_pct", lambda p: 10),
                  mock.patch.object(als, "run_probes", self._no_probes),
                  mock.patch.object(als, "notify", fake_notify),
                  mock.patch.object(als.system_metrics, "snapshot", fake_snap)):
                asyncio.run(als._check_resources())
            out.append(list(sent))
        return out

    async def _run_stub(self, *_a, **_kw):
        return {"code": 0, "out": ""}

    async def _no_probes(self):
        return []

    def test_notify_names_the_same_tiers_the_gauge_uses(self):
        _patched_registry(self, STUB)   # cpu 60 → 要紧档 70
        sent = self._rounds()[-1]
        cpu = [t for t in sent if t.startswith("CPU")]
        self.assertEqual(len(cpu), 1, str(sent))
        self.assertIn("阈值 60%", cpu[0])
        self.assertIn("要紧档 70%", cpu[0],
                      "通知里只报警戒线，收到告警的人就不知道页面上为什么还没红")

    def test_history_row_keeps_the_stable_wording(self):
        _patched_registry(self, STUB)
        self._rounds()
        row = dbm.query("SELECT message, meta FROM alerts_log WHERE code='cpu' ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(row["message"], "CPU 使用率超过阈值（阈值 60%）",
                         "历史是去重键，不能带每轮都在变的数值")
        self.assertEqual(json.loads(row["meta"])["crit"], 70)

    def test_moving_the_setting_moves_the_notified_line(self):
        # 配置线是唯一出处：把它从登记表默认挪到 40，推送与仪表档位要一起动。
        _patched_registry(self, dict(STUB, alert_cpu_pct=(40, 1, 99)))
        dbm.set_setting("alert_cpu_pct", "40")
        sent = self._rounds()[-1]
        self.assertIn("阈值 40%", [t for t in sent if t.startswith("CPU")][0])


class FrontendSingleSourceTests(unittest.TestCase):
    """页面读载荷，不再自己抄默认值与区间。"""

    def test_settings_page_no_longer_copies_defaults(self):
        src = (SRC / "web/src/views/Settings.vue").read_text(encoding="utf-8")
        self.assertNotIn("ALERT_DEFAULTS", src)
        self.assertIn("v?.limits", src)
        self.assertIn("critStep", src)
        for literal in (':min="50"', ':max="99"', ':min="1" :max="365"', ':min="100" :max="20000"'):
            self.assertNotIn(literal, src, f"{literal} 是区间的第二份出处")
        for key in ("alert_disk_pct", "alert_cpu_pct", "alert_mem_pct", "alert_keep_rows"):
            self.assertIn(f"lim('{key}').min", src, key)
            self.assertIn(f"lim('{key}').max", src, key)

    def test_gauges_read_the_lines_from_the_sample_payload(self):
        src = (SRC / "web/src/views/Dashboard.vue").read_text(encoding="utf-8")
        self.assertIn("stat.value.cpuWarnPct", src)
        self.assertIn("stat.value.memWarnPct", src)
        self.assertNotIn("band(cpuVal.value, 70, 85)", src, "CPU 仪表的档位不该再是自己抄的数")
        self.assertNotIn("band(memVal.value, 80, 90)", src, "内存仪表同理")
        self.assertIn("物理口径", src,
                      "负载那一格留物理档位是有意的（倍数线当档位会让环永远不红），"
                      "这个决定必须写在代码边上，否则下一个人会把它「统一」掉")


if __name__ == "__main__":
    unittest.main()
