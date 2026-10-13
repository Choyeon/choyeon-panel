"""批次⑱A：「这块盘要满了」只有一条线。

⑰B 把前端那份 90 收成了载荷里的 `tight_pct`，但那条线本身仍然有两个出处：设置页存的
`alert_disk_pct`（默认 85、可配 50-99，巡检读它），和 storage_service 里的
`DISK_TIGHT_PCT = 90`（存储判词、自检的磁盘项、页签的条形读它）。自检项 `_check_disk`
更是直接写死 90/75 两个字面量。于是把线调到 70 的机器上：告警在响、概览的磁盘仪表仍是
品牌色、`choyeonctl doctor` 报 pass、存储页签写「没有异常占用」——同一块盘四个答案，
而其中三个是绿的。

这批把出处收成一个：`alerts_service.disk_thresholds()` 读那条配置给出
`{"warn": 配置线, "crit": 配置线 + 10（封顶 99）}`，巡检/自检/存储判词/概览仪表都引用它。
分成两档是因为「该发通知了」和「红/要紧项」本来就不该是同一个数：合成一个的话，
把线设到 60% 的机器会天天红。

用例里的百分比一律来自被显式钉住的那两条线，不引用产品里的数字——否则改了默认值
用例还跟着绿，那是最坏的假绿。
"""
import asyncio
import json
import unittest
from pathlib import Path
from unittest import mock

from app import database as dbm
from app.routers import meta, system
from app.services import alerts_service as als
from app.services import storage_service as ss

SRC = Path(__file__).resolve().parents[2]
GIB = 1024**3


def _clean_disk_lines(case):
    """`alert_disk_pct` 是这条线的唯一出处，用例之间必须各归各的默认。"""
    original = dbm.get_setting("alert_disk_pct")
    dbm.execute("DELETE FROM settings WHERE key='alert_disk_pct'")

    def restore():
        if original is None:
            dbm.execute("DELETE FROM settings WHERE key='alert_disk_pct'")
        else:
            dbm.set_setting("alert_disk_pct", original)
    case.addCleanup(restore)


class ThresholdSourceTests(unittest.TestCase):
    """两条线的算法只在这里定义一次。"""

    def setUp(self):
        _clean_disk_lines(self)

    def test_default_is_the_configured_line_plus_one_step(self):
        self.assertEqual(als.disk_thresholds(), {"warn": 85, "crit": 95})

    def test_moving_the_setting_moves_both_lines(self):
        dbm.set_setting("alert_disk_pct", "70")
        self.assertEqual(als.disk_thresholds(), {"warn": 70, "crit": 80})

    def test_crit_is_capped_at_99(self):
        # 线贴着顶格时不能算出 105 这种永远越不过的档，否则要紧项整档失效。
        dbm.set_setting("alert_disk_pct", "99")
        self.assertEqual(als.disk_thresholds(), {"warn": 99, "crit": 99})

    def test_junk_and_out_of_range_fall_back_then_clamp(self):
        dbm.set_setting("alert_disk_pct", "abc")
        self.assertEqual(als.disk_thresholds(), {"warn": 85, "crit": 95})
        dbm.set_setting("alert_disk_pct", "5")
        self.assertEqual(als.disk_thresholds(), {"warn": 50, "crit": 60})

    def test_the_step_is_not_reimplemented_anywhere_else(self):
        # 别处再写一次 +10 / min(99, ...) 就是又开一个出处。
        # ⑱B 起 extras 不在名单里：它把 `alerts_service.CRIT_STEP` 原样转交给设置页，
        # 引用允许、自己算不允许，那一条由 test_batch18b 守。
        for rel in ("app/services/storage_service.py", "app/routers/meta.py",
                    "app/routers/system.py"):
            src = (SRC / "backend" / rel).read_text(encoding="utf-8")
            self.assertNotIn("CRIT_STEP", src, rel)


class StorageFindingsTests(unittest.TestCase):
    """存储判词的两档：越过警戒线出声，越过要紧线才算要紧项。"""

    def _disk(self, pct):
        return {"ok": True, "path": "/data", "used_pct": pct, "total": 40 * GIB,
                "free": int((40 - 40 * pct / 100) * GIB), "free_human": "12.0GB"}

    def _mount(self, path, pct):
        return {"path": path, "device": "/dev/sdc1", "fstype": "ext4", "unknown": False,
                "ro": False, "panel": False, "snapshot": False, "used_pct": pct,
                "inode_pct": 10, "free_human": "1.0GB"}

    def _out(self, pct, lines=None, mounts=None):
        return ss._findings([], {"bytes": None}, self._disk(pct), [], mounts,
                            thresholds=lines or {"warn": 70, "crit": 80})

    def test_below_the_line_is_silent(self):
        self.assertEqual(self._out(69), [])

    def test_warn_line_gives_a_warn_tier_finding(self):
        out = self._out(75)
        self.assertEqual([f["id"] for f in out], ["tight"])
        self.assertEqual(out[0]["severity"], ss.SEV_WARN)
        self.assertIn("警戒线 70%", out[0]["detail"], "判词要说清是哪条线判的")

    def test_crit_line_promotes_it_to_risk(self):
        out = self._out(88)
        self.assertEqual(out[0]["severity"], ss.SEV_RISK)

    def test_other_mounts_use_the_same_two_tiers(self):
        # 非数据目录那块盘：过去只有 90 一档、越线一律 risk；现在和上面同一把尺。
        warn = self._out(10, mounts=[self._mount("/mnt/spare", 72)])
        self.assertEqual([(f["id"], f["severity"]) for f in warn],
                         [("mount:/mnt/spare", ss.SEV_WARN)])
        risk = self._out(10, mounts=[self._mount("/mnt/spare", 96)])
        self.assertEqual(risk[0]["severity"], ss.SEV_RISK)
        self.assertIn("警戒线 70%", warn[0]["detail"],
                      "别的挂载点也要说清是按哪把尺判的——它和数据目录用的是同一条线")

    def test_lower_line_catches_what_90_missed(self):
        """这条就是整批的动机：配置调到 70 之后，87% 必须在这一页出声。"""
        out = self._out(87, lines={"warn": 70, "crit": 80})
        self.assertEqual([f["id"] for f in out], ["tight"])

    def test_inode_line_is_its_own_number(self):
        # inode 和容量是两回事，不能被磁盘那条配置一起拉走（判词仍用 INODE_WARN_PCT）。
        out = ss._findings([], {"bytes": None}, {"ok": False, "path": "/data"}, [],
                           [dict(self._mount("/mnt/x", 10), inode_pct=90)],
                           thresholds={"warn": 95, "crit": 99})
        self.assertEqual([f["id"] for f in out], ["inode:/mnt/x"])


class AuditPayloadTests(unittest.TestCase):
    """`/api/storage` 的载荷把两条线都带出去，判词与载荷同源。"""

    LINES = {"warn": 70, "crit": 80}

    def setUp(self):
        ss.reset_cache()
        patcher = mock.patch.object(ss.alerts_service, "disk_thresholds",
                                    lambda: dict(self.LINES))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(ss.reset_cache)

    def _audit(self, used_pct):
        async def fake_journal():
            return 1024, "Archived and active journals take up 1.0K in the file system."

        collected = {
            "roots": [],
            "disk": {"ok": True, "path": "/data", "used_pct": used_pct,
                     "total": 40 * GIB, "free": 12 * GIB, "free_human": "12.0GB"},
            "big_files": [],
            "mounts": {"ok": True, "items": [], "total": 0, "omitted": 0, "panel_mount": "/data"},
        }
        with mock.patch.object(ss, "_journal_bytes", fake_journal), \
             mock.patch.object(ss, "_collect", lambda: collected):
            return asyncio.run(ss.audit(refresh=True))

    def test_both_lines_and_inode_lines_are_in_the_payload(self):
        d = self._audit(50)
        self.assertEqual(d["tight_pct"], self.LINES["warn"])
        self.assertEqual(d["crit_pct"], self.LINES["crit"])
        self.assertEqual(d["inode_warn_pct"], ss.INODE_WARN_PCT)
        self.assertEqual(d["inode_risk_pct"], ss.INODE_RISK_PCT)

    def test_counts_and_payload_agree_at_each_tier(self):
        # 75%：页签上是"值得处理"，不是要紧项；88%：才进 risk。两头都不能数错。
        self.assertEqual(self._audit(75)["counts"]["risk"], 0)
        self.assertEqual(self._audit(75)["counts"]["warn"], 1)
        self.assertEqual(self._audit(88)["counts"]["risk"], 1)
        self.assertEqual(self._audit(69)["counts"]["warn"], 0)

    def test_doctor_item_does_not_recompute_the_line(self):
        # 存储自检项只折判词；它再读一次阈值就会出现「页签黄、自检红」这类分叉。
        src = (SRC / "backend/app/services/storage_service.py").read_text(encoding="utf-8")
        body = src[src.index("def doctor_item"):]
        self.assertNotIn("disk_thresholds", body)


class DoctorDiskItemTests(unittest.TestCase):
    """`choyeonctl doctor` 的磁盘项：过去写死 90/75，配置调到 70 时它报 pass。"""

    def setUp(self):
        _clean_disk_lines(self)
        dbm.set_setting("alert_disk_pct", "70")

    def _item(self, pct):
        with mock.patch.object(meta, "disk_usage_pct", lambda p: pct):
            return asyncio.run(meta._check_disk())

    def test_three_tiers_come_from_the_configured_lines(self):
        self.assertEqual(self._item(69)["status"], "pass")
        self.assertEqual(self._item(72)["status"], "warn")
        self.assertEqual(self._item(81)["status"], "fail")

    def test_detail_names_the_line_it_used(self):
        self.assertIn("警戒线 70%", self._item(72)["detail"])
        self.assertIn("要紧线 80%", self._item(81)["detail"])
        self.assertIn("警戒线 70%", self._item(10)["detail"],
                      "pass 也要说清是按哪条线判的，否则读数与结论对不上")

    def test_unreadable_disk_is_warn_not_pass(self):
        with mock.patch.object(meta, "disk_usage_pct", lambda p: None):
            out = asyncio.run(meta._check_disk())
        self.assertEqual(out["status"], "warn")


class StatsPayloadTests(unittest.TestCase):
    """概览仪表的配色读的也是这一条线（随 /api/system/stats 给出）。"""

    def setUp(self):
        patcher = mock.patch.object(als, "disk_thresholds", lambda: {"warn": 70, "crit": 80})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stats_carries_both_lines(self):
        async def fake_snap():
            return {"cpu": 1.0, "diskUsed": 7 * GIB, "diskTotal": 10 * GIB}

        with mock.patch.object(system.system_metrics, "snapshot", fake_snap):
            out = asyncio.run(system.sys_stats())
        self.assertEqual(out["diskWarnPct"], 70)
        self.assertEqual(out["diskCritPct"], 80)
        self.assertEqual(out["cpu"], 1.0, "只是补两个键，不能改动采样结果")


class RunChecksTierTests(unittest.TestCase):
    """巡检：越过要紧线的那一档要落成 crit，历史与通知都按档走。"""

    SNAP = {"cpu": 12.0, "memTotal": 1000, "memUsed": 200, "load": [0.4, 0.3, 0.2],
            "cpuCores": 4, "history": [{"t": 0, "cpu": 11.0}, {"t": 3000, "cpu": 12.0}]}

    def setUp(self):
        _clean_disk_lines(self)
        dbm.execute("DELETE FROM alerts_log WHERE code='disk'")
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code='disk'")
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("sent:disk%",))

    async def _run_stub(self, *_a, **_kw):
        return {"code": 0, "out": ""}

    async def _no_probes(self):
        return []

    def _round(self, pct):
        """跑一轮巡检，返回磁盘这一 code 的历史行和这一轮真发出去的推送文本。

        历史和通知是两条线：历史按 `log_message`（不含逐轮变化的数字）去重，
        推送发的是带数值的 `message`。两边都要看才判得清档位。
        """
        sent: list[str] = []

        async def fake_notify(text):
            sent.append(text)
            return False

        async def fake_snap():
            return dict(self.SNAP)

        with (mock.patch.object(als, "run", self._run_stub),
              mock.patch.object(als, "disk_usage_pct", lambda p: pct),
              mock.patch.object(als, "run_probes", self._no_probes),
              mock.patch.object(als, "notify", fake_notify),
              mock.patch.object(als.system_metrics, "snapshot", fake_snap)):
            asyncio.run(als.run_checks())
        rows = dbm.query("SELECT level, message, meta FROM alerts_log WHERE code='disk' ORDER BY id")
        return rows, sent

    def test_below_the_line_only_records_ok(self):
        rows, sent = self._round(40)
        self.assertEqual([r["level"] for r in rows], ["ok"])
        self.assertEqual(sent, [], "没越线就不该有推送")

    def test_warn_then_crit_adds_a_row_because_the_level_changed(self):
        # record() 按「同 code 最近一行同级同文」去重，升档就是换级：必须留下一行 crit，
        # 否则告警列表最新一行还停在 warn，看不出盘已经从"该处理"变成"要出事"。
        self._round(40)
        self._round(88)
        rows, sent = self._round(96)
        self.assertEqual([r["level"] for r in rows], ["ok", "warn", "crit"], rows)
        self.assertEqual(rows[-1]["message"], rows[-2]["message"],
                         "两行的差别只在 level：历史键不能带逐轮变化的数字")
        # 推送文本必须把人一眼要用的两个数都写出来（现在是多少、要紧线在哪）。
        self.assertIn("磁盘使用率 96%", sent[0])
        self.assertIn("要紧线 95%", sent[0])

    def test_meta_carries_both_lines_at_the_crit_tier(self):
        self._round(96)
        rows, _ = self._round(96)
        m = json.loads(rows[-1]["meta"])
        self.assertEqual((m["threshold"], m["crit"], m["pct"]), (85, 95, 96))

    def test_same_tier_twice_is_not_rewritten(self):
        self._round(40)
        self._round(88)
        rows, _ = self._round(89)
        self.assertEqual([r["level"] for r in rows], ["ok", "warn"],
                         "89% 还是 warn 档，同 code 同级同文不重复落历史")


class FrontendSingleSourceTests(unittest.TestCase):
    """前端两处读数只能读载荷，不许再自己抄数字。"""

    def test_dashboard_gauge_reads_the_payload(self):
        src = (SRC / "web/src/views/Dashboard.vue").read_text(encoding="utf-8")
        self.assertIn("stat.value.diskWarnPct", src)
        self.assertIn("stat.value.diskCritPct", src)
        self.assertNotIn("band(diskVal.value, 80, 90)", src)

    def test_band_ignores_a_missing_threshold_instead_of_going_red(self):
        # 阈值 ≤0 是「没给」，写成 v >= 0 会把每个仪表都染红。
        src = (SRC / "web/src/views/Dashboard.vue").read_text(encoding="utf-8")
        self.assertIn("if (err > 0 && v >= err)", src)
        self.assertIn("if (warn > 0 && v >= warn)", src)

    def test_storage_tab_reads_both_lines_and_the_inode_line(self):
        src = (SRC / "web/src/views/Services.vue").read_text(encoding="utf-8")
        self.assertIn("storageData.value?.crit_pct", src)
        self.assertIn("storageData.value?.inode_warn_pct", src)
        self.assertNotIn("v >= 90", src, "条形的热档必须跟着载荷的线，不是又一个 90")

    def test_no_second_copy_of_the_number_survives_in_the_frontend(self):
        # 磁盘这条线在 .vue 里只允许出现在「读载荷」的位置。
        for rel in ("web/src/views/Services.vue", "web/src/views/Dashboard.vue"):
            src = (SRC / rel).read_text(encoding="utf-8")
            self.assertNotIn("used_pct >= 90", src, rel)
            self.assertNotIn("used_pct) >= 90", src, rel)


if __name__ == "__main__":
    unittest.main()
