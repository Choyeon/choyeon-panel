"""批次105：巡检里每一节没查成都要进本轮清单，界面不许只报「完成」。

`_run_checks` 有一半的节在读不到时 `warn()` 一句就跳过：磁盘分区取不到占用、systemctl
退出码非 0、certbot 不在、系统指标抛异常、备份留痕查失败、应用列表查失败。`warn()` 只进
journal，而三个入口（后台每 30 分钟一轮、告警页「立即巡检」、设置页「运行检查」）的回应
原本只说「巡检完成，新增 N 条历史」——于是"这一轮什么都没读成"和"全读完了都没问题"在
界面上是同一句话，正是批次101 禁止的那类替后端粉饰。

修法：这些分支改用 `_unchecked(节名, 为什么)`，它在写 journal 的同时把这一句记进当轮正在
收集的清单；`run_checks` 用 `collect_unchecked()` 开一份清单收口，路由把 `unchecked` 原样
带进响应，前端 `uncheckedNote()` 把它拼进反馈语。清单挂在模块级的栈上而不是逐层传形参：
`run_probes` / `run_backup_checks` 既被巡检调也被自己的独立入口调，测试里还有大量零参数替身
patch 它们，加必填参数会把那些替身全打成 TypeError——而漏传的那一处恰好是静默跳过的那节。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 只有本轮真是这个模块自己开的目录才归它删：见 tearDownModule。
_OWN_DATA_DIR = "CP_DATA_DIR" not in os.environ
if _OWN_DATA_DIR:
    os.environ["CP_DATA_DIR"] = tempfile.mkdtemp(prefix="cp_b105_data_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import alerts as alerts_router  # noqa: E402
from app.routers import extras as extras_router  # noqa: E402
from app.services import alerts_service as ALS  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
SERVICE_PY = (SRC / "backend" / "app" / "services" / "alerts_service.py").read_text(encoding="utf-8")
ALERTS_PY = (SRC / "backend" / "app" / "routers" / "alerts.py").read_text(encoding="utf-8")
EXTRAS_PY = (SRC / "backend" / "app" / "routers" / "extras.py").read_text(encoding="utf-8")
UI_TS = (SRC / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
ALERTS_VUE = (SRC / "web" / "src" / "views" / "Alerts.vue").read_text(encoding="utf-8")
SETTINGS_VUE = (SRC / "web" / "src" / "views" / "Settings.vue").read_text(encoding="utf-8")


class TestCollect(unittest.TestCase):
    """清单本身：写 journal、进每一层、退出即收。"""

    def test_message_names_the_section_and_the_reason(self):
        with ALS.collect_unchecked() as sink:
            ALS._unchecked("磁盘使用率阈值", f"{ALS.config.DATA_DIR} 所在分区读不到占用")
        self.assertEqual(len(sink), 1)
        self.assertIn("磁盘使用率阈值本轮没查成", sink[0])
        self.assertIn("读不到占用", sink[0])

    def test_without_a_sink_it_only_logs(self):
        # 直接跑 `_run_checks`（测试替身、将来的别的调用方）时不该炸，也不该往上一轮的清单里塞
        with ALS.collect_unchecked() as sink:
            pass
        ALS._unchecked("测试节", "没有人在收")
        self.assertEqual(sink, [])
        self.assertEqual(ALS._unchecked_sinks, [])

    def test_nested_scope_gets_its_own_copy(self):
        with ALS.collect_unchecked() as outer:
            with ALS.collect_unchecked() as inner:
                ALS._unchecked("应用存活探测", "读取应用列表失败")
            ALS._unchecked("磁盘使用率阈值", "读不到")
        self.assertEqual(len(inner), 1)
        self.assertEqual(len(outer), 2)

    def test_two_empty_sinks_do_not_eat_each_other(self):
        # 内层退出时是空清单，值上和外层相等；按值摘就会把外层那份摘走，外层再写进来的
        # 账就没人收了——下一轮界面看到的是上一轮的旧清单（或干脆什么都看不到）。
        with ALS.collect_unchecked() as outer:
            with ALS.collect_unchecked():
                pass
            ALS._unchecked("磁盘使用率阈值", "读不到占用")
        self.assertEqual(outer, ["磁盘使用率阈值本轮没查成（读不到占用）"])
        self.assertEqual(ALS._unchecked_sinks, [])

    def test_sink_is_removed_even_if_the_section_raises(self):
        with self.assertRaises(RuntimeError):
            with ALS.collect_unchecked():
                raise RuntimeError("boom")
        self.assertEqual(ALS._unchecked_sinks, [])
        with ALS.collect_unchecked() as sink:
            ALS._unchecked("下一轮", "上一轮的账不该串进来")
        self.assertEqual(len(sink), 1)


class TestSectionsMarkThemselves(unittest.TestCase):
    """每一节的读不到分支必须走 `_unchecked`，而不是只 warn 一句就过去。"""

    def test_run_checks_returns_the_rounds_unchecked_sections(self):
        async def partial():
            ALS._unchecked("磁盘使用率阈值", "读不到占用")
            ALS._unchecked("failed 状态服务清单", "systemctl 退出码 1")

        with mock.patch.object(ALS, "_run_checks", partial), \
             mock.patch.object(ALS, "_checks_running", False):
            out = asyncio.run(ALS.run_checks())
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["unchecked"]), 2)
        self.assertIn("systemctl 退出码 1", " ".join(out["unchecked"]))

    def test_clean_round_has_no_unchecked_key(self):
        async def quiet():
            pass

        with mock.patch.object(ALS, "_run_checks", quiet), \
             mock.patch.object(ALS, "_checks_running", False):
            out = asyncio.run(ALS.run_checks())
        self.assertNotIn("unchecked", out, "全都读成了还带清单，等于自己制造噪音")

    def test_skipped_round_does_not_invent_unchecked(self):
        with mock.patch.object(ALS, "_checks_running", True):
            out = asyncio.run(ALS.run_checks())
        self.assertIn("skipped", out)
        self.assertNotIn("unchecked", out)

    def _run_full_round(self, *, pct=None, syscode=1, certcode=1):
        async def run_bin(*_a, **_kw):
            return {"code": syscode, "out": "System has not been booted with systemd", "err": ""}

        async def run_cert(*_a, **_kw):
            return {"code": certcode, "out": "certbot: command not found", "err": ""}

        async def snapshot():
            raise RuntimeError("/proc 读不到")

        async def boom_query(*_a, **_kw):
            raise RuntimeError("database is locked")

        def blind():
            raise RuntimeError("no such table")

        def setting(key):
            return {"alert_ssl_days": 30, "alert_probe": 1}.get(key, 0)

        with mock.patch.object(ALS, "_checks_running", False), \
             mock.patch.object(ALS, "disk_usage_pct", lambda *_a: pct), \
             mock.patch.object(ALS, "run", lambda *a, **kw: run_cert() if a and a[0] == "certbot" else run_bin()), \
             mock.patch.object(ALS, "setting_int", setting), \
             mock.patch.object(ALS, "disk_thresholds", lambda: {"warn": 80, "crit": 90}), \
             mock.patch.object(ALS.system_metrics, "snapshot", snapshot), \
             mock.patch.object(ALS, "scheduled_runs", blind), \
             mock.patch.object(ALS.aio_db, "query", boom_query):
            return asyncio.run(ALS.run_checks())

    def test_a_round_where_every_probe_source_is_blind(self):
        out = self._run_full_round()
        joined = " ".join(out.get("unchecked") or [])
        for section in ("磁盘使用率阈值", "failed 状态服务清单", "证书到期天数",
                        "CPU／内存／负载阈值", "备份任务执行留痕", "应用存活探测"):
            self.assertIn(section, joined, f"{section} 没读成却没进清单：{joined}")
        self.assertEqual(len(out["unchecked"]), 6, f"多出来的条目就是重复记账：{joined}")

    def test_disk_readable_is_not_reported(self):
        out = self._run_full_round(pct=12.0)
        joined = " ".join(out.get("unchecked") or [])
        self.assertNotIn("磁盘使用率阈值", joined)
        self.assertIn("证书到期天数", joined)


class TestServiceBranches(unittest.TestCase):
    def test_resources_without_baseline_are_not_quiet(self):
        async def snapshot():
            # 批次108 后"量不出差值"由后端直说：cpu 是 None，不再拿 0.0 冒充一个读数
            return {"cpu": None, "measured": False, "memTotal": 0, "memUsed": 0, "load": [0.0]}

        async def go():
            with ALS.collect_unchecked() as sink:
                await ALS._check_resources()
            return sink

        with mock.patch.object(ALS.system_metrics, "snapshot", snapshot):
            sink = asyncio.run(go())
        self.assertEqual(len(sink), 1, "没有差值基线时 CPU 是量不出，不是空闲")
        self.assertIn("CPU", sink[0])

    def test_probes_with_no_apps_configured_is_not_unchecked(self):
        async def query(*_a, **_kw):
            return []

        async def go():
            with ALS.collect_unchecked() as sink:
                results = await ALS.run_probes()
            return sink, results

        with mock.patch.object(ALS.aio_db, "query", query):
            sink, results = asyncio.run(go())
        self.assertEqual(results, [])
        self.assertEqual(sink, [], "没配端口的应用是「无事可探」，不是「探测没查成」")

    def test_backup_rows_read_failure_is_reported(self):
        def boom():
            raise RuntimeError("no such table")

        async def go():
            with ALS.collect_unchecked() as sink:
                await ALS.run_backup_checks()
            return sink

        with mock.patch.object(ALS, "scheduled_runs", boom):
            sink = asyncio.run(go())
        self.assertEqual(len(sink), 1)
        self.assertIn("备份任务执行留痕", sink[0])


class TestRoutesPassItThrough(unittest.TestCase):
    @staticmethod
    def _req():
        return SimpleNamespace(state=SimpleNamespace(cp_sub="admin"), query_params={})

    def test_alert_run_route_forwards_the_list(self):
        async def ran():
            return {"ok": True, "unchecked": ["证书到期天数本轮没查成（certbot 退出码 127）"]}

        audits = []
        with mock.patch.object(alerts_router, "need_admin", lambda _r: None), \
             mock.patch.object(alerts_router.alerts_service, "run_checks", ran), \
             mock.patch.object(alerts_router.aio_db, "query_one",
                               lambda *a, **kw: asyncio.sleep(0, {"m": 7})), \
             mock.patch.object(alerts_router.aio_db, "query", lambda *a, **kw: asyncio.sleep(0, [])), \
             mock.patch.object(alerts_router.aio_db, "audit",
                               lambda *a, **kw: asyncio.sleep(0, audits.append(a))):
            out = asyncio.run(alerts_router.alerts_run(self._req()))
        self.assertEqual(out["count"], 0)
        self.assertEqual(len(out["unchecked"]), 1)
        self.assertIn("未查成 1 节", audits[-1][2], "审计要留下这一轮没查完，事后查得到")

    def test_probe_route_collects_its_own_round(self):
        async def probes():
            ALS._unchecked("应用存活探测", "读取应用列表失败：OperationalError")
            return []

        with mock.patch.object(alerts_router, "need_admin", lambda _r: None), \
             mock.patch.object(alerts_router.alerts_service, "run_probes", probes), \
             mock.patch.object(alerts_router.aio_db, "query_one",
                               lambda *a, **kw: asyncio.sleep(0, {"m": 3})), \
             mock.patch.object(alerts_router.aio_db, "query", lambda *a, **kw: asyncio.sleep(0, [])), \
             mock.patch.object(alerts_router.aio_db, "audit", lambda *a, **kw: asyncio.sleep(0, None)):
            out = asyncio.run(alerts_router.alerts_probe(self._req()))
        self.assertEqual(out["results"], [])
        self.assertIn("应用存活探测", " ".join(out["unchecked"]))
        self.assertEqual(ALS._unchecked_sinks, [], "路由退出时要把自己那份清单关掉")

    def test_settings_run_checks_route_forwards_the_list(self):
        async def ran():
            return {"ok": True, "unchecked": ["磁盘使用率阈值本轮没查成（读不到占用）"]}

        audits = []
        with mock.patch.object(extras_router, "_need_admin", lambda _r: None), \
             mock.patch.object(extras_router.alerts_service, "run_checks", ran), \
             mock.patch.object(extras_router.aio_db, "audit",
                               lambda *a, **kw: asyncio.sleep(0, audits.append(a))):
            out = asyncio.run(extras_router.alerts_run_checks(self._req()))
        self.assertIn("磁盘使用率阈值", " ".join(out["unchecked"]))
        self.assertIn("未查成 1 节", audits[-1][2])

    def test_settings_route_still_ok_without_the_list(self):
        async def ran():
            return {"ok": True}

        with mock.patch.object(extras_router, "_need_admin", lambda _r: None), \
             mock.patch.object(extras_router.alerts_service, "run_checks", ran), \
             mock.patch.object(extras_router.aio_db, "audit", lambda *a, **kw: asyncio.sleep(0, None)):
            out = asyncio.run(extras_router.alerts_run_checks(self._req()))
        self.assertNotIn("unchecked", out)


class TestNoSilentBranchesLeft(unittest.TestCase):
    """剩下的静默跳过只有「用户自己关掉」这一类，读不到的分支不能再有。"""

    def test_the_old_warn_only_skips_are_gone(self):
        for needle in ('warn("certbot 不可用', 'warn("读取系统指标失败',
                       'warn("读取备份任务留痕失败', 'warn("读取应用列表失败',
                       "warn(\"无法读取磁盘使用率"):
            self.assertNotIn(needle, SERVICE_PY, f"{needle} 还在只写 journal 就跳过")

    def test_replacements_actually_call_the_collector(self):
        # 只删旧文案不算修好：每一条都得真的走 _unchecked
        self.assertGreaterEqual(SERVICE_PY.count("_unchecked("), 8,
                                "服务层的 _unchecked 调用点少于一处一节")
        self.assertIn("_unchecked(\"证书到期天数\"", SERVICE_PY)
        self.assertIn("_unchecked(\"应用存活探测\"", SERVICE_PY)
        self.assertIn("_unchecked(\"备份任务执行留痕\"", SERVICE_PY)

    def test_the_routes_do_not_invent_their_own_section_names(self):
        # 路由只转述服务层给的句子；自己编一份就出现「接口说有 2 节、界面上数不出是哪两节」
        for src in (ALERTS_PY, EXTRAS_PY):
            for section in ("磁盘使用率阈值", "证书到期天数", "应用存活探测", "failed 状态服务清单"):
                self.assertNotIn(section, src, f"路由里硬编码了「{section}」")
        self.assertIn('out["unchecked"] = ran["unchecked"]', ALERTS_PY)
        self.assertIn('out["unchecked"] = unchecked', ALERTS_PY)
        self.assertIn('out["unchecked"] = ran["unchecked"]', EXTRAS_PY)


class TestFrontendContract(unittest.TestCase):
    def test_the_phrase_has_one_owner(self):
        self.assertIn("export function uncheckedNote", UI_TS)
        self.assertEqual(UI_TS.count("export function uncheckedNote"), 1)

    def test_both_buttons_ask_before_they_celebrate(self):
        for vue, head, done in ((ALERTS_VUE, "async function runRound", "msg.success(base)"),
                                (SETTINGS_VUE, "async function runChecks",
                                 "msg.success('巡检已执行，若触发阈值会立即推送')")):
            body = vue.split(head, 1)[1].split("\n}\n", 1)[0]
            self.assertIn("uncheckedNote", body, head)
            self.assertLess(body.index("uncheckedNote("), body.index(done),
                            f"{head}：绿灯话术排在清单之前，等于没查完也说查完了")

    def test_alerts_page_says_what_is_missing(self):
        # 「新增 0 条历史」+「本轮有 N 节没查成」才是这一轮的真话
        self.assertIn("${base}，但${note}", ALERTS_VUE)
        self.assertIn("`巡检已执行，但${note}`", SETTINGS_VUE)

    def test_probe_and_run_both_go_through_the_note(self):
        # 探测入口过去只报「探测完成，0 个应用」，而那个 0 也可能是应用列表压根没查到
        body = ALERTS_VUE.split("async function runRound", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("kind === 'run'", body)
        self.assertEqual(body.count("uncheckedNote"), 1, "两个入口共用一次判定，别各写一份")


def tearDownModule():
    # 只删自己建的那一份。`run_tests.py` 会把 CP_DATA_DIR 设成整轮共用的守护目录，
    # 按「在临时目录下」判断就会把别轮的 panel.db 连目录一起端掉：排在后面的
    # batch6a 权限用例于是把「目录被删后重建的 0755」读成面板没锁权限。
    if _OWN_DATA_DIR:
        shutil.rmtree(os.environ["CP_DATA_DIR"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
