"""批次㉜：服务存活口径统一——"正在启动"和"这台机器上没有这个服务"不再被说成"已停止"。

面板原来在四个地方各写了一遍"这个服务在跑吗"：
- `systemctl is-active` 的退出码（只会回答 0/非 0）；
- `ActiveState == "active"` 的字符串比较（列表页）；
- `MainPID > 0`（详情页的运行状态）。
三套判词都对了一半，坏在它们对同一件事给出不同答案，而且都把三种情况压成"没在跑"：

1. **activating**：Type=simple 起立的那几秒、Type=oneshot 正在执行的那一轮，ActiveState 都是
   activating（生产实测，跑完才回 inactive）。恢复备份时按"没在跑"跳过停服务，备份就是直接
   盖在一个正要 exec 的进程上写的文件；SQLite 那侧还会把旧连接留下的 -wal/-shm 当残留删掉。
2. **not-found**：unit 被删过、机器迁移过、名字写错时 systemd 报 LoadState=not-found，
   ActiveState 只是 inactive。于是列表页写着"已停止，点启动即可"，点下去只得到一句
   "not loaded"；自检项则给出 `systemctl restart` 这种必然失败的修复命令。
3. **探测失败**（本机没有 systemd、systemctl 超时/报错）：以前等于"已停止"，整屏假红；
   在恢复路径上更糟——"查不到"被当成"没在跑"，于是跳过停服务直接盖文件。

这一批把判词收进 systemd_ops 一处：`active`（健康运行中，列表与详情显示的）和
`busy`（此刻占着文件与端口，动手前必须确认的）是两个问题，调用点只问、不再各自比较字符串。
前端同样只留一份说法表（ui.ts 的 unitTag），列表页与详情页共用。

顺带修掉一个静默失效：`systemctl show -p MainPID …` **不会**回显 Id（生产实测），
而批量解析器靠 Id 归块——所以详情页那次 show 的每个属性都被丢掉了（pid 恒为 0）。
"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch32_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch32_roots_"))

from app import config  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import apps_service, backup_service, cron_service, systemd_ops  # noqa: E402

REPO = Path(config.BASE)
OPS_SRC = (REPO / "backend" / "app" / "services" / "systemd_ops.py").read_text(encoding="utf-8")
UI_TS = (REPO / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
APPS_VUE = (REPO / "web" / "src" / "views" / "Apps.vue").read_text(encoding="utf-8")
DETAIL_VUE = (REPO / "web" / "src" / "views" / "AppDetail.vue").read_text(encoding="utf-8")


def _props(state: str, load: str = "loaded", pid: str = "4242") -> dict:
    d = {"Id": "x.service", "LoadState": load, "ActiveState": state, "MainPID": pid}
    return d


class TestStateVocabulary(unittest.TestCase):
    """判词表：active/reloading 算"在跑"，activating/deactivating 只算"还占着"。"""

    def test_table(self):
        cases = {
            "active": (True, True),
            "reloading": (True, True),
            "activating": (False, True),
            "deactivating": (False, True),
            "inactive": (False, False),
            "failed": (False, False),
        }
        for state, (active, busy) in cases.items():
            with self.subTest(state=state):
                st = systemd_ops.derive_state(_props(state))
                self.assertEqual(st["state"], state)
                self.assertIs(st["active"], active)
                self.assertIs(st["busy"], busy)

    def test_transitioning_is_not_stopped(self):
        """崩溃重启的间隙里 ActiveState 是 activating：它既不是"运行中"，也不是"可以盖文件"。"""
        st = systemd_ops.derive_state(_props("activating"))
        self.assertFalse(st["active"], "正在启动不能说成运行中")
        self.assertTrue(st["busy"], "正在启动不能说成没在跑")

    def test_not_found_is_missing_not_inactive(self):
        st = systemd_ops.derive_state(_props("inactive", load="not-found"))
        self.assertTrue(st["missing"])
        self.assertFalse(st["active"])
        self.assertFalse(st["busy"])

    def test_loaded_not_found_is_not_missing(self):
        self.assertFalse(systemd_ops.derive_state(_props("active"))["missing"])

    def test_garbage_or_missing_pid_is_zero(self):
        for pid in ("", "n/a", "abc", "-1"):
            with self.subTest(pid=pid):
                self.assertEqual(systemd_ops.derive_state(_props("active", pid=pid))["mainPid"], 0)

    def test_unknown_is_never_claiming_anything(self):
        """探测失败的哨兵：三个判词全假，前端显示"状态未知"，恢复流程按最坏情况停服务。"""
        self.assertEqual(
            systemd_ops.UNKNOWN_STATE,
            {"state": "unknown", "load": "", "active": False, "busy": False, "missing": False, "mainPid": 0},
        )


class TestParseShow(unittest.TestCase):
    def test_property_before_id_is_kept(self):
        """实测 MainPID 排在 Id 前面：`if line.startswith("Id=")` 那种写法会把它整块丢掉。"""
        raw = "MainPID=99\nId=panel-demo.service\nActiveState=active\n"
        blocks = systemd_ops.parse_show(raw)
        self.assertEqual(blocks[0]["MainPID"], "99")
        self.assertEqual(blocks[0]["Id"], "panel-demo.service")

    def test_blocks_do_not_bleed_into_each_other(self):
        raw = ("MainPID=1\nId=a.service\nActiveState=active\n\n"
               "MainPID=0\nId=b.service\nActiveState=inactive\n")
        got = {b["Id"]: b for b in systemd_ops.parse_show(raw)}
        self.assertEqual(got["a.service"]["MainPID"], "1")
        self.assertEqual(got["b.service"]["MainPID"], "0")

    def test_block_without_id_is_dropped(self):
        self.assertEqual(systemd_ops.parse_show("MainPID=1\nActiveState=active\n"), [])

    def test_value_with_equals_sign(self):
        """ExecStart=/bin/sh -c "A=B" 这类属性值里就有等号：只能按第一个等号切。"""
        block = systemd_ops.parse_show('Id=x.service\nExecStart=/bin/sh -c "FOO=bar"\n')[0]
        self.assertEqual(block["ExecStart"], '/bin/sh -c "FOO=bar"')


class TestUnitStates(unittest.IsolatedAsyncioTestCase):
    async def test_one_call_for_many_units(self):
        seen = []

        async def fake_run(cmd, args, **kw):
            seen.append(args)
            return {"code": 0, "out": "MainPID=1\nId=a.service\nLoadState=loaded\nActiveState=active\n"}

        with mock.patch.object(systemd_ops, "run", fake_run):
            got = await systemd_ops.unit_states(["a.service", "b.service"])
        self.assertEqual(len(seen), 1, "N 个 unit 必须还是一次 systemctl 子进程")
        self.assertTrue(got["a.service"]["active"])
        self.assertEqual(got["b.service"], systemd_ops.UNKNOWN_STATE)

    async def test_id_is_always_requested(self):
        """不带 -pId 时 systemctl 不回显 Id，parse_show 会把每个块都丢掉→全部"未知"。"""
        seen = []

        async def fake_run(cmd, args, **kw):
            seen.append(args)
            return {"code": 0, "out": ""}

        with mock.patch.object(systemd_ops, "run", fake_run):
            await systemd_ops.unit_states(["a.service"])
        self.assertIn("Id", seen[0], "属性列表里必须有 Id，否则整批解析出空表")

    async def test_unit_separator_is_kept(self):
        """少了 `--`，以 - 开头的 unit 名会被当成选项：实测 status -x.service 返回整机状态。"""
        seen = []

        async def fake_run(cmd, args, **kw):
            seen.append(args)
            return {"code": 0, "out": ""}

        with mock.patch.object(systemd_ops, "run", fake_run):
            await systemd_ops.unit_states(["-weird.service"])
        self.assertIn("--", seen[0])
        self.assertEqual(seen[0][-1], "-weird.service")

    async def test_probe_failure_is_unknown_not_stopped(self):
        async def failing_run(cmd, args, **kw):
            return {"code": 1, "out": "Failed to connect to bus: No such file or directory"}

        with mock.patch.object(systemd_ops, "run", failing_run):
            st = await systemd_ops.unit_state("a.service")
        self.assertEqual(st["state"], "unknown")
        self.assertFalse(st["active"] or st["busy"] or st["missing"])

    async def test_empty_input_makes_no_subprocess(self):
        async def boom(*a, **kw):
            raise AssertionError("不该起子进程")

        with mock.patch.object(systemd_ops, "run", boom):
            self.assertEqual(await systemd_ops.unit_states([]), {})

    async def test_is_active_and_is_busy_read_the_same_source(self):
        out = "MainPID=7\nId=a.service\nLoadState=loaded\nActiveState=activating\n"

        async def fake_run(cmd, args, **kw):
            return {"code": 0, "out": out}

        with mock.patch.object(systemd_ops, "run", fake_run):
            self.assertFalse(await systemd_ops.is_active("a.service"))
        with mock.patch.object(systemd_ops, "run", fake_run):
            self.assertTrue(await systemd_ops.is_busy("a.service"))


class TestHasSystemd(unittest.TestCase):
    def test_both_conditions(self):
        cases = {
            (True, True): True,     # 正常主机
            (False, True): False,   # 有 /run 却没有 systemctl（最小镜像里手工装依赖）
            (True, False): False,   # 容器 / WSL：有二进制但没跑 systemd
            (False, False): False,
        }
        for (has_bin, has_dir), want in cases.items():
            with self.subTest(has_bin=has_bin, has_dir=has_dir), \
                 mock.patch.object(systemd_ops.shutil, "which", lambda _n: "/usr/bin/systemctl" if has_bin else None), \
                 mock.patch.object(systemd_ops.os.path, "isdir", lambda _p: has_dir):
                self.assertIs(systemd_ops.has_systemd(), want)


class TestResetFailed(unittest.IsolatedAsyncioTestCase):
    async def test_uses_separator_and_swallows_errors(self):
        seen = []

        async def ok(cmd, args, **kw):
            seen.append(args)
            return {"code": 0, "out": ""}

        with mock.patch.object(systemd_ops, "run", ok):
            await systemd_ops.reset_failed("a.service")
        self.assertEqual(seen[0], ["reset-failed", "--", "a.service"])

        async def bad(cmd, args, **kw):
            raise RuntimeError("boom")

        with mock.patch.object(systemd_ops, "run", bad):
            await systemd_ops.reset_failed("a.service")  # 不抛：start 那边才是真判据

    async def test_is_in_the_allowed_verbs_for_actions(self):
        """reset-failed 只走这个函数，不放进 VERBS：启停动词表仍是白名单。"""
        self.assertNotIn("reset-failed", systemd_ops.VERBS)


class TestStopForOverwrite(unittest.IsolatedAsyncioTestCase):
    """恢复路径问的是 busy，不是列表页那个 active。"""

    def _patch(self, *, has=True, state=None, missing=False):
        st = {"state": state, "active": state in ("active", "reloading"),
              "busy": state in ("active", "reloading", "activating", "deactivating"),
              "missing": missing, "load": "", "mainPid": 1}
        actions = []

        async def act(unit, verb):
            actions.append(verb)
            return ""

        return (
            mock.patch.object(systemd_ops, "has_systemd", lambda: has),
            mock.patch.object(systemd_ops, "unit_state", mock.AsyncMock(return_value=st)),
            mock.patch.object(systemd_ops, "service_action", act),
            actions,
        )

    async def _run(self, **kw):
        p = self._patch(**kw)
        log: list[str] = []
        with p[0], p[1], p[2]:
            got = await backup_service._stop_for_overwrite("panel-demo.service", log)
        return got, log, p[3]

    async def test_active_stops_and_restarts(self):
        (stopped, restart), log, actions = await self._run(state="active")
        self.assertEqual((stopped, restart), (True, True))
        self.assertEqual(actions, ["stop"])
        self.assertIn("已停止", "".join(log))

    async def test_activating_stops_too(self):
        """这一条是本批的由头：正在起立的服务在老口径里是"没在跑"，于是文件被直接盖上。"""
        (stopped, restart), log, actions = await self._run(state="activating")
        self.assertEqual(actions, ["stop"], "activating 必须先停再写")
        self.assertTrue(stopped and restart)

    async def test_deactivating_stops_too(self):
        (_s, _r), _log, actions = await self._run(state="deactivating")
        self.assertEqual(actions, ["stop"])

    async def test_unknown_state_stops_anyway(self):
        """查不到状态≠没在跑：照发 stop（真没跑它是空操作），systemctl 坏了就直接抛在动手之前。"""
        (_s, _r), log, actions = await self._run(state="unknown")
        self.assertEqual(actions, ["stop"])
        self.assertIn("按最坏情况处理", "".join(log))

    async def test_missing_unit_is_touched_neither(self):
        (_s, restart), log, actions = await self._run(state="inactive", missing=True)
        self.assertEqual(actions, [], "本机没有这个 unit，stop/start 都只会得到 not loaded")
        self.assertFalse(restart, "不该把一个从未部署成功的 unit 拉起来")
        self.assertIn("本机没有", "".join(log))

    async def test_failed_will_be_started_after(self):
        """failed 不是"运维特意停着"，恢复正是冲它来的：结束后要拉起（且先清启动失败计数）。"""
        (stopped, restart), log, actions = await self._run(state="failed")
        self.assertEqual((stopped, restart), (False, True))
        self.assertEqual(actions, [])
        self.assertIn("failed", "".join(log))

    async def test_intentionally_stopped_stays_stopped(self):
        (stopped, restart), log, actions = await self._run(state="inactive")
        self.assertEqual((stopped, restart), (False, False))
        self.assertEqual(actions, [])
        self.assertIn("跳过停止", "".join(log))

    async def test_no_systemd_starts_no_subprocess(self):
        """容器/WSL 里 systemctl 查失败是常态：以前靠"查失败"反推，systemctl 卡死的真机器就会
        把"不知道"当成"没在跑"。现在先问有没有 systemd，没有就只写文件、一个子进程都不起。"""
        p = self._patch(has=False)
        log: list[str] = []
        with p[0], mock.patch.object(systemd_ops, "unit_state", mock.AsyncMock(side_effect=AssertionError("不该查"))):
            got = await backup_service._stop_for_overwrite("panel-demo.service", log)
        self.assertEqual(got, (False, False))
        self.assertIn("没有 systemd", "".join(log))

    async def test_every_branch_says_something(self):
        """任何一条路都不能沉默：用户看日志要能判断服务后来到底有没有被重启。"""
        for kw in ({"state": "active"}, {"state": "activating"}, {"state": "unknown"},
                   {"state": "inactive", "missing": True}, {"state": "failed"}, {"state": "inactive"},
                   {"has": False}):
            with self.subTest(**kw):
                _got, log, _actions = await self._run(**kw)
                self.assertTrue(any(line.startswith(("==>", "!!")) for line in log))


class TestStartAfterRestore(unittest.IsolatedAsyncioTestCase):
    async def test_reset_failed_only_when_coming_from_failed(self):
        calls = []

        async def rf(unit):
            calls.append("reset")

        async def act(unit, verb):
            calls.append(verb)

        with mock.patch.object(systemd_ops, "reset_failed", rf), \
             mock.patch.object(systemd_ops, "service_action", act):
            await backup_service._start_after_restore("a.service", [])
            self.assertEqual(calls, ["start"])
            calls.clear()
            await backup_service._start_after_restore("a.service", [], from_failed=True)
        self.assertEqual(calls, ["reset", "start"])

    async def test_start_failure_is_logged_not_raised(self):
        """文件已经盖完了，抛错会让整个恢复接口失败而用户以为什么都没做。"""
        async def act(unit, verb):
            raise RuntimeError("Start request repeated too quickly")

        log: list[str] = []
        with mock.patch.object(systemd_ops, "service_action", act):
            await backup_service._start_after_restore("a.service", log)
        self.assertIn("请手工启动", "".join(log))
        self.assertIn("Start request repeated too quickly", "".join(log))


class TestAppProcUsesSharedVerdict(unittest.IsolatedAsyncioTestCase):
    async def test_pid_requires_busy_and_id_is_requested(self):
        out = ("MainPID=55\nId=panel-demo.service\nLoadState=loaded\nActiveState=activating\n"
               "SubState=start-pre\nNRestarts=0\nMemoryCurrent=100\nCPUUsageNSec=2000000\n"
               "ActiveEnterTimestamp=n/a\nExecMainStartTimestamp=Thu 2026-10-15 09:00:00 CST\n")
        seen = []

        async def fake_run(cmd, args, **kw):
            seen.append(args)
            return {"code": 0, "out": out}

        app = {"name": "demo", "unit_override": ""}
        with mock.patch.object(apps_service, "get_app", lambda _id: app), \
             mock.patch.object(apps_service, "run", fake_run):
            got = await apps_service.app_proc(1)
        self.assertIn("-pId", seen[0], "不带 Id 时 parse_show 认不出块，详情页的 pid 恒为 0")
        self.assertEqual(got["pid"], 55, "activating 的进程就是还活着，要显示")
        self.assertTrue(got["running"])
        self.assertEqual(got["state"], "activating")

    async def test_failed_pid_is_not_reported_as_running(self):
        out = ("MainPID=4242\nId=panel-demo.service\nLoadState=loaded\nActiveState=failed\n"
               "SubState=failed\nNRestarts=5\n")
        app = {"name": "demo", "unit_override": ""}

        async def fake_run(cmd, args, **kw):
            return {"code": 0, "out": out}

        with mock.patch.object(apps_service, "get_app", lambda _id: app), \
             mock.patch.object(apps_service, "run", fake_run):
            got = await apps_service.app_proc(1)
        self.assertFalse(got["running"], "failed 的 unit 会把死 pid 挂在 MainPID 上等清理")
        self.assertIsNone(got["pid"])
        self.assertTrue(got["restarts"])

    async def test_probe_failure_is_unknown(self):
        app = {"name": "demo", "unit_override": ""}

        async def fake_run(cmd, args, **kw):
            return {"code": 127, "out": "[Errno 2] No such file or directory: 'systemctl'"}

        with mock.patch.object(apps_service, "get_app", lambda _id: app), \
             mock.patch.object(apps_service, "run", fake_run):
            got = await apps_service.app_proc(1)
        self.assertEqual(got["state"], "unknown")
        self.assertFalse(got["running"])
        self.assertFalse(got["unitMissing"])

    async def test_proc_fields_include_the_state_properties(self):
        for f in ("Id", "ActiveState", "LoadState", "MainPID", "SubState"):
            self.assertIn(f, apps_service.PROC_FIELDS)


class TestListAppsCarriesState(unittest.IsolatedAsyncioTestCase):
    async def _list(self, states):
        rows = [{"id": 1, "name": "demo", "unit_override": "", "port": None, "domain": ""}]

        async def q(sql, params=None):
            return rows if "FROM apps" in sql else []

        with mock.patch.object(apps_service.aio_db, "query", q), \
             mock.patch.object(apps_service.systemd_ops, "unit_states", mock.AsyncMock(return_value=states)), \
             mock.patch.object(apps_service.nginx_ops, "domains_by_port", lambda: {}), \
             mock.patch.object(apps_service, "_listen_lines", mock.AsyncMock(return_value="")):
            return await apps_service.list_apps()

    async def test_unit_state_and_missing_reach_the_frontend(self):
        out = await self._list({"panel-demo.service": systemd_ops.derive_state(
            _props("inactive", load="not-found"))})
        self.assertEqual(out[0]["unitState"], "inactive")
        self.assertTrue(out[0]["unitMissing"])
        self.assertFalse(out[0]["running"])

    async def test_unit_missing_when_systemctl_said_nothing(self):
        """systemctl 没回话时列表里是 unknown，不能写成 active/inactive 中的任何一个。"""
        out = await self._list({})
        self.assertEqual(out[0]["unitState"], "unknown")
        self.assertFalse(out[0]["unitMissing"])

    async def test_activating_is_not_running_but_blocks_port_detection(self):
        out = await self._list({"panel-demo.service": systemd_ops.derive_state(_props("activating"))})
        self.assertFalse(out[0]["running"])
        self.assertIsNone(out[0]["port"])


class TestCronOverlapUsesBusy(unittest.IsolatedAsyncioTestCase):
    async def test_overlap_guard_counts_activating(self):
        """定时器上一轮还在跑（ActiveState=activating）时不能允许重入——判词必须走 systemd_ops。"""
        raw = ("Result=success\nExecMainStartTimestamp=n/a\nActiveState=activating\n"
               "Id=panel-cron-7.service\nMainPID=3\n")
        with mock.patch.object(cron_service, "run", mock.AsyncMock(return_value={"code": 0, "out": raw})):
            got = await cron_service._unit_results(["panel-cron-7"])
        self.assertTrue(got["panel-cron-7"]["running"])
        self.assertEqual(got["panel-cron-7"]["status"], "", "这一轮还没结束就没有结果可言")

    async def test_inactive_round_reports_its_result(self):
        raw = ("Result=success\nExecMainStatus=0\nExecMainStartTimestamp=Thu 2026-10-15 09:00:00 CST\n"
               "ActiveState=inactive\nId=panel-cron-7.service\nMainPID=0\n")
        with mock.patch.object(cron_service, "run", mock.AsyncMock(return_value={"code": 0, "out": raw})):
            got = await cron_service._unit_results(["panel-cron-7"])
        self.assertFalse(got["panel-cron-7"]["running"])
        self.assertEqual(got["panel-cron-7"]["status"], "ok")

    async def test_local_parser_delegates_to_the_shared_one(self):
        """本模块留了个同名壳给老测试打桩，实现必须只有一份。"""
        src = inspect.getsource(cron_service._parse_show)
        self.assertIn("systemd_ops.parse_show", src)
        self.assertEqual(cron_service._parse_show("MainPID=1\nId=x.service\n")[0]["MainPID"], "1")


class TestDoctorServiceItem(unittest.IsolatedAsyncioTestCase):
    async def _check(self, *, has=True, state=None, missing=False):
        st = {"state": state, "active": state in ("active", "reloading"),
              "busy": state in ("active", "reloading", "activating", "deactivating"),
              "missing": missing, "load": "", "mainPid": 1}
        with mock.patch.object(systemd_ops, "has_systemd", lambda: has), \
             mock.patch.object(systemd_ops, "unit_state", mock.AsyncMock(return_value=st)):
            return await meta._check_service()

    async def test_active_passes(self):
        self.assertEqual((await self._check(state="active"))["status"], "pass")

    async def test_no_systemd_is_warn_with_manual_fix(self):
        got = await self._check(has=False)
        self.assertEqual(got["status"], "warn")
        self.assertIn("systemd", got["detail"])

    async def test_missing_unit_does_not_suggest_restart(self):
        """照 fix 敲 `systemctl restart` 只会得到 not loaded：这里要指的是"没按脚本来装"。"""
        got = await self._check(state="inactive", missing=True)
        self.assertEqual(got["status"], "warn")
        self.assertIn("install.sh", got["fix"])
        self.assertNotIn("systemctl restart", got["fix"])

    async def test_probe_failure_is_warn_not_fail(self):
        got = await self._check(state="unknown")
        self.assertEqual(got["status"], "warn")
        self.assertIn("查不到", got["detail"])

    async def test_transitioning_is_warn(self):
        for state in ("activating", "deactivating"):
            with self.subTest(state=state):
                got = await self._check(state=state)
                self.assertEqual(got["status"], "warn")
                self.assertIn("状态切换中", got["detail"])

    async def test_stopped_and_failed_stay_fail(self):
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                got = await self._check(state=state)
                self.assertEqual(got["status"], "fail")
                self.assertIn("reset-failed", got["fix"], "failed 后光 restart 会被 StartLimitBurst 拒掉")


class TestVerdictHasOneSource(unittest.TestCase):
    """回归闸：判词只许写在 systemd_ops 里，别的模块只问不猜。"""

    CHECKED = [
        "backend/app/services/apps_service.py",
        "backend/app/services/backup_service.py",
        "backend/app/services/cron_service.py",
        "backend/app/routers/apps.py",
        "backend/app/routers/system.py",
        "backend/app/routers/meta.py",
    ]

    def test_no_second_copy_of_the_active_comparison(self):
        for rel in self.CHECKED:
            code = (REPO / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertNotIn('get("ActiveState")', code)
                self.assertNotIn('== "active"', code)
                self.assertNotIn('!= "active"', code)

    def test_no_new_parse_show(self):
        for rel in self.CHECKED + ["backend/app/services/systemd_ops.py"]:
            code = (REPO / rel).read_text(encoding="utf-8")
            with self.subTest(rel=rel):
                self.assertLessEqual(code.count("def parse_show") + code.count("def _parse_show"), 1)

    def test_the_two_verdicts_are_named_not_inlined(self):
        self.assertIn('RUNNING_STATES = ("active", "reloading")', OPS_SRC)
        self.assertIn('BUSY_STATES = RUNNING_STATES + ("activating", "deactivating")', OPS_SRC)

    def test_nginx_scope_still_asks_is_active(self):
        """反代作用域要的是"健康运行中"，不是 busy：这条不能被批量口径顺手改掉。"""
        routers = (REPO / "backend" / "app" / "routers" / "apps.py").read_text(encoding="utf-8")
        self.assertIn("systemd_ops.is_active", routers)


class TestFrontendSharesOneWording(unittest.TestCase):
    def test_ui_ts_is_the_only_table(self):
        self.assertIn("export function unitTag", UI_TS)
        self.assertEqual(UI_TS.count("const UNIT_TAG"), 1)
        for vue in (APPS_VUE, DETAIL_VUE):
            self.assertNotIn("const UNIT_TAG", vue, "说法表又长出了第二份")

    def test_both_views_import_it(self):
        self.assertIn("unitTag", APPS_VUE)
        self.assertIn("unitTag", DETAIL_VUE)
        self.assertIn("from '../ui'", APPS_VUE)
        self.assertIn("from '../ui'", DETAIL_VUE)

    def test_notfound_wording_exists(self):
        self.assertIn("单元不存在", UI_TS)
        self.assertIn("正在启动", UI_TS)
        self.assertIn("状态未知", UI_TS)

    def test_list_page_renders_state_not_boolean(self):
        self.assertIn("unitTag(r)", APPS_VUE)
        self.assertNotIn("r.running ? '运行中' : '已停止'", APPS_VUE)

    def test_detail_page_disables_start_for_missing_unit(self):
        self.assertIn("app.unitMissing", DETAIL_VUE)


if __name__ == "__main__":
    unittest.main()
