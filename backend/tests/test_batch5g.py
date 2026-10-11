"""批次⑤G：部署等待预算、取消与残留 running、systemctl argv 边界、日志与 nginx 编辑权限。

这一轮的共同点是"面板说的和机器上发生的不是一回事"：
- CLI 的等待时间比部署自己的最坏耗时短，于是 CLI 先走、把部署任务一起带走，
  记录停在 running，之后删除/回滚/改配置全部被"部署进行中"拦住；
- systemctl 把以 - 开头的 unit 名当命令行选项，`systemctl status -ping.service`
  实测返回 0 并打印整机状态，面板读成"这个应用在跑"；
- 只读账号能 tail 全机 journal，而 admin 从应用详情可以把别的（包括面板自己的）
  nginx 配置当成"我的配置"整份写进 nginx 并 reload；
- uvicorn 的访问日志会把带 ?token= 的完整请求目标原样写进 journald（已实测）。
- 建应用的目录守卫：默认目录 {APP_ROOT}/{name} 从来没做过要害检查，name 写成
  choyeon-panel 就得到一条指向面板自己代码目录的应用记录（点部署 = 把自己
  git reset / purge = rmtree 自己）；而不带仓库的应用谁也不建目录，部署必然
  卡在「目录不存在」，这条路径还是 CLI 明确支持的（--start-cmd 不带 --repo）。
"""

import asyncio
import inspect
import itertools
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5g_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_5g_roots_"))

from app import config  # noqa: E402
from app import database as dbm  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.routers import apps as apps_router  # noqa: E402
from app.routers import system as system_router  # noqa: E402
from app.services import apps_service, nginx_ops, systemd_ops  # noqa: E402


def _req(body=None, role="admin", sub="tester", query=None):
    async def _json():
        return body

    return SimpleNamespace(
        state=SimpleNamespace(cp_role=role, cp_sub=sub),
        json=_json,
        query_params=query or {},
    )


_seq = itertools.count()


def _mkapp(**cols) -> int:
    """插一条最小可用的应用记录（绕开 create_app 的目录/模板逻辑）。"""
    name = f"ut5g{next(_seq)}"
    fields = {
        "name": name,
        "type": "node",
        "repo_url": "",
        "path": tempfile.mkdtemp(prefix=f"cp_5g_{name}_"),
        "start_cmd": "true",
        **cols,
    }
    keys = ",".join(fields)
    marks = ",".join("?" * len(fields))
    return dbm.execute(f"INSERT INTO apps({keys}) VALUES({marks})", tuple(fields.values()))


def _mkdep(app_id: int, age_seconds: int = 0, status: str = "running") -> int:
    return dbm.execute(
        "INSERT INTO deployments(app_id,status,started_at) VALUES(?,?,datetime('now', ?))",
        (app_id, status, f"-{age_seconds} seconds"),
    )


def _drop(app_id: int, *dep_ids: int):
    dbm.execute("DELETE FROM deployments WHERE app_id=?", (app_id,))
    dbm.execute("DELETE FROM apps WHERE id=?", (app_id,))


class TestDeployBudget(unittest.TestCase):
    """CLI 的等待预算必须由部署自己的步骤超时推出来，而不是各写一个"看着够用"的数。"""

    def test_budget_covers_the_worst_case_path(self):
        worst = (
            max(
                apps_service.GIT_CLONE_T,
                apps_service.GIT_FETCH_T + apps_service.GIT_CHECKOUT_T + apps_service.GIT_RESET_T,
            )
            + apps_service.INSTALL_T
            + apps_service.DEPLOY_ONLINE_T
        )
        self.assertEqual(apps_service.DEPLOY_BUDGET_SEC, worst)

    def test_wait_deployment_defaults_to_the_budget(self):
        default = inspect.signature(apps_service.wait_deployment).parameters["timeout"].default
        self.assertEqual(
            default,
            apps_service.DEPLOY_BUDGET_SEC,
            "等待时间小于部署自身的上限，CLI 就会先把任务取消并留下 running 记录",
        )

    def test_every_step_timeout_fits_inside_the_budget(self):
        for name in ("GIT_CLONE_T", "GIT_FETCH_T", "GIT_CHECKOUT_T", "GIT_RESET_T", "INSTALL_T"):
            self.assertLess(getattr(apps_service, name), apps_service.DEPLOY_BUDGET_SEC)


class TestCancelledDeployment(unittest.IsolatedAsyncioTestCase):
    """任务被取消时也要落终态：CancelledError 不是 Exception，except Exception 抓不住。"""

    async def test_cancel_marks_the_deployment_failed(self):
        app_id = _mkapp(start_cmd="sleep 30")
        dep_id = _mkdep(app_id)
        started = asyncio.Event()

        async def hang(*_args, **_kw):
            started.set()
            await asyncio.sleep(30)
            return {"code": 0, "out": ""}

        app = apps_service.get_app(app_id)
        try:
            with mock.patch.object(apps_service, "run", hang):
                task = asyncio.create_task(apps_service._run_deployment(app, dep_id))
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            row = dbm.query_one("SELECT status,log FROM deployments WHERE id=?", (dep_id,))
            self.assertEqual(row["status"], "failed", f"取消后不能留在 running：{row['log'][-300:]}")
            self.assertIn("被取消", row["log"])
        finally:
            _drop(app_id, dep_id)

    async def test_registry_cleared_when_task_ends(self):
        """deploy_app 登记的 id 必须随任务结束撤掉，否则看门狗永远不会回收这一行。"""
        app_id = _mkapp()
        try:
            async def _noop(_app, _dep):
                return None

            with mock.patch.object(apps_service, "_run_deployment", _noop):
                dep_id = await apps_service.deploy_app(app_id)
                self.assertIn(dep_id, apps_service._RUNNING_DEPS)
                await asyncio.gather(*list(apps_service._DEPLOY_TASKS))
                await asyncio.sleep(0)  # done_callback 由 call_soon 派发
            self.assertNotIn(dep_id, apps_service._RUNNING_DEPS)
        finally:
            _drop(app_id)

    async def test_shutdown_cancels_inflight_deployments(self):
        """面板优雅退出时也要收尾：否则这一行要等回收器（最坏耗时上限）才解锁。"""
        app_id = _mkapp(start_cmd="sleep 30")
        dep_id = _mkdep(app_id)
        started = asyncio.Event()

        async def hang(*_args, **_kw):
            started.set()
            await asyncio.sleep(30)
            return {"code": 0, "out": ""}

        app = apps_service.get_app(app_id)
        try:
            with mock.patch.object(apps_service, "run", hang):
                task = asyncio.create_task(apps_service._run_deployment(app, dep_id))
                apps_service._DEPLOY_TASKS.add(task)
                apps_service._RUNNING_DEPS.add(dep_id)
                await asyncio.wait_for(started.wait(), 5)
                await apps_service.cancel_inflight_deployments()
                self.assertTrue(task.done())
            row = dbm.query_one("SELECT status,log FROM deployments WHERE id=?", (dep_id,))
            self.assertEqual(row["status"], "failed", f"退出时不能留在 running：{row['log'][-300:]}")
            self.assertIn("被取消", row["log"])
        finally:
            apps_service._DEPLOY_TASKS.clear()
            apps_service._RUNNING_DEPS.discard(dep_id)
            _drop(app_id, dep_id)


class TestReapOrphanDeployments(unittest.TestCase):
    """进程被 kill 留下的 running 记录不该等到下次重启才清。"""

    def test_old_row_without_a_task_is_reaped(self):
        app_id = _mkapp()
        dep_id = _mkdep(app_id, apps_service.DEPLOY_BUDGET_SEC + 600)
        try:
            self.assertIn(dep_id, apps_service.reap_orphan_deployments())
            row = dbm.query_one("SELECT status,log FROM deployments WHERE id=?", (dep_id,))
            self.assertEqual(row["status"], "failed")
            self.assertIn("回收为失败", row["log"])
            self.assertNotIn(dep_id, apps_service.reap_orphan_deployments(), "已落终态的行不该再动")
        finally:
            _drop(app_id, dep_id)

    def test_live_task_is_not_reaped(self):
        """本进程还在跑的部署哪怕超了上限也不能被写终态，否则日志里两条结论互相打脸。"""
        app_id = _mkapp()
        dep_id = _mkdep(app_id, apps_service.DEPLOY_BUDGET_SEC + 600)
        apps_service._RUNNING_DEPS.add(dep_id)
        try:
            self.assertNotIn(dep_id, apps_service.reap_orphan_deployments())
            self.assertEqual(
                dbm.query_one("SELECT status FROM deployments WHERE id=?", (dep_id,))["status"],
                "running",
            )
        finally:
            apps_service._RUNNING_DEPS.discard(dep_id)
            _drop(app_id, dep_id)

    def test_recent_row_is_not_reaped(self):
        app_id = _mkapp()
        dep_id = _mkdep(app_id, 10)
        try:
            self.assertNotIn(dep_id, apps_service.reap_orphan_deployments())
            self.assertEqual(
                dbm.query_one("SELECT status FROM deployments WHERE id=?", (dep_id,))["status"],
                "running",
            )
        finally:
            _drop(app_id, dep_id)


class TestHousekeepingWiring(unittest.IsolatedAsyncioTestCase):
    """回收器得真的被挂上定时器：只有启动时清一次的话，跑着被 kill 的实例永远不清。"""

    async def test_reap_runs_periodically(self):
        real_sleep = asyncio.sleep

        async def fast_sleep(sec, *a, **kw):
            return await real_sleep(0 if sec == 600 else sec, *a, **kw)

        with mock.patch.object(asyncio, "sleep", fast_sleep), \
             mock.patch.object(main_mod, "_prune_all", lambda: None), \
             mock.patch.object(apps_service, "reap_orphan_deployments") as reap:
            t = asyncio.create_task(main_mod._housekeeping())
            for _ in range(50):
                await real_sleep(0.01)
                if reap.called:
                    break
            t.cancel()
            await real_sleep(0)
            with suppress(BaseException):
                await t
        self.assertTrue(reap.called, "housekeeping 里没有调用 reap_orphan_deployments")


class TestCreateAppPathGuards(unittest.TestCase):
    """建应用这一步就要把目录问题说清楚，而不是留一条永远部署不通的记录。"""

    def test_default_dir_cannot_point_at_the_panel_itself(self):
        """不填 path 时默认值是 {APP_ROOT}/{name}，而 name 允许写成 choyeon-panel，
        于是那条记录指向面板自己的代码目录：点一次部署就把自己 git reset 掉。"""
        name = str(Path(config.BASE).name)
        with mock.patch.object(apps_service, "APP_ROOT", str(Path(config.BASE).parent)):
            with self.assertRaises(RuntimeError) as cm:
                apps_service._validate({"name": name, "type": "node", "start_cmd": "true"})
        self.assertIn("安装目录不能位于", str(cm.exception))

    def test_default_dir_is_created_when_there_is_no_repo(self):
        """线上实测：--start-cmd 建的应用既没仓库也没目录，部署必然报「目录不存在」。"""
        root = tempfile.mkdtemp(prefix="cp_5g_root_")
        with mock.patch.object(apps_service, "APP_ROOT", root):
            a = apps_service.create_app({"name": "p5g-node", "type": "node", "start_cmd": "sleep 1"})
            try:
                self.assertEqual(a["path"], os.path.join(root, "p5g-node"))
                self.assertTrue(os.path.isdir(a["path"]), "无仓库应用的默认目录应被建出来")
            finally:
                _drop(a["id"])
        shutil.rmtree(root, ignore_errors=True)

    def test_explicit_missing_dir_is_created_at_creation(self):
        """显式填一个还没建好的目录（先建应用再放代码）：建应用时补出来，
        而不是留到点部署那一刻才看到「目录不存在」。"""
        missing = os.path.join(tempfile.mkdtemp(prefix="cp_5g_out_"), "not-here")
        a = apps_service.create_app({
            "name": "p5g-missing", "type": "node", "start_cmd": "true", "path": missing,
        })
        try:
            self.assertTrue(os.path.isdir(missing), a["path"])
        finally:
            _drop(a["id"])
            shutil.rmtree(missing, ignore_errors=True)

    def test_existing_external_path_is_left_alone(self):
        """托管已在跑的目录（Rosetta 那类）：不该被拒绝，也不该被动过。"""
        root = tempfile.mkdtemp(prefix="cp_5g_ext_")
        a = apps_service.create_app({"name": "p5g-ext", "type": "node", "start_cmd": "true", "path": root})
        try:
            self.assertEqual(os.listdir(root), [])
        finally:
            _drop(a["id"])


class TestUnitOverrideGrammar(unittest.TestCase):
    """写入口的 unit 名校验必须与读接口/util.UNIT_RE 同一份语法。"""

    def _check(self, unit):
        apps_service._validate(
            {
                "name": f"ut5gu{next(_seq)}",
                "type": "node",
                "start_cmd": "true",
                "unit_override": unit,
            }
        )

    def test_dash_leading_name_rejected(self):
        for bad in ("-ping.service", "--all.service", "@x.service", ".hidden.service"):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self._check(bad)

    def test_suffix_and_length_still_required(self):
        with self.assertRaises(RuntimeError):
            self._check("nginx")  # 缺 .service 后缀：unit 文件名与 systemctl 名字会分叉
        with self.assertRaises(RuntimeError):
            self._check("a" * 70 + ".service")

    def test_normal_name_accepted(self):
        self._check("rosetta-backend.service")


class TestSystemctlEndOfOptions(unittest.IsolatedAsyncioTestCase):
    """systemctl 的 argv 一律带 `--`：unit 名再也不会被当选项解析。"""

    async def test_argv(self):
        calls = []

        async def fake_run(cmd, args, **_kw):
            calls.append((cmd, args))
            return {"code": 0, "out": "", "err": ""}

        with mock.patch.object(systemd_ops, "run", fake_run):
            await systemd_ops.service_action("nginx.service", "restart")
            await systemd_ops.is_active("nginx.service")
            await systemd_ops.unit_states(["nginx.service"])

        self.assertEqual(calls[0], ("systemctl", ["restart", "--", "nginx.service"]))
        self.assertEqual(calls[1], ("systemctl", ["is-active", "--", "nginx.service"]))
        self.assertEqual(
            calls[2],
            ("systemctl", ["show", "-p", "Id", "-p", "ActiveState", "-p", "MainPID", "--", "nginx.service"]),
        )


class TestServiceLogsNeedAdmin(unittest.IsolatedAsyncioTestCase):
    """journal 是全机内容：/api/system/services/{unit}/logs 与日志检索同口径，要 admin。"""

    async def test_viewer_rejected(self):
        resp = await system_router.service_logs("nginx.service", _req(role="viewer"))
        self.assertEqual(resp.status_code, 403)

    async def test_admin_streamed(self):
        sentinel = object()
        with mock.patch.object(system_router, "log_stream_response", return_value=sentinel) as stream:
            got = await system_router.service_logs("nginx.service", _req(role="admin"))
        self.assertIs(got, sentinel)
        stream.assert_called_once()


class TestUvicornAccessLogOff(unittest.TestCase):
    """?token= 只用于下载/SSE/WS 这类带不上 Header 的请求，不能让 uvicorn 把整条 URL 写进 journald。"""

    def test_run_disables_access_log(self):
        import uvicorn

        with mock.patch.object(uvicorn, "run") as u:
            main_mod.run()
        self.assertIs(u.call_args.kwargs["access_log"], False)


class TestAppNginxScopedToOwnFiles(unittest.IsolatedAsyncioTestCase):
    """应用详情里的 nginx 编辑器只能改这个应用自己的配置。"""

    OWN = "/etc/nginx/conf.d/app.conf"
    FOREIGN = "/etc/nginx/conf.d/choyeon-panel.conf"

    def _stack(self, extra: list):
        """路由会先取应用记录、再按端口/域名算配置范围，这里把这三步都换成假的。"""
        base = [
            mock.patch.object(
                apps_router.apps_service,
                "get_app",
                lambda _id: {"id": 1, "name": "a", "unit_override": None, "port": 8000, "domain": "app.example.com"},
            ),
            mock.patch.object(systemd_ops, "is_active", lambda *_: asyncio.sleep(0, True)),
            mock.patch.object(apps_router.apps_service, "detect_port", lambda *_: asyncio.sleep(0, 8000)),
            mock.patch.object(nginx_ops, "find_app_configs", lambda *_a: [{"file": self.OWN, "name": "app.conf"}]),
        ]
        base.extend(extra)
        return base

    async def _save(self, file, extra):
        """路由现在会先取应用记录并算配置范围，base 补丁与用例自己的补丁一起进栈。"""
        with ExitStack() as st:
            for p in self._stack(extra):
                st.enter_context(p)
            return await apps_router.app_nginx_save(1, _req(body={"file": file, "content": "x"}))

    async def test_foreign_file_rejected(self):
        saved = mock.AsyncMock()
        resp = await self._save(
            self.FOREIGN,
            [
                mock.patch.object(nginx_ops, "assert_app_conf", side_effect=RuntimeError("不属于该应用")),
                mock.patch.object(nginx_ops, "save_app_config", saved),
            ],
        )
        self.assertEqual(resp.status_code, 400)
        saved.assert_not_awaited()

    async def test_own_file_accepted(self):
        saved = mock.AsyncMock(return_value={"ok": True})
        resp = await self._save(
            self.OWN,
            [mock.patch.object(nginx_ops, "assert_app_conf", return_value=None),
             mock.patch.object(nginx_ops, "save_app_config", saved)],
        )
        self.assertEqual(resp, {"ok": True})
        saved.assert_awaited_once()

    async def test_quick_edit_scoped_too(self):
        edited = mock.AsyncMock()
        with ExitStack() as st:
            for p in self._stack(
                [
                    mock.patch.object(nginx_ops, "assert_app_conf", side_effect=RuntimeError("不属于该应用")),
                    mock.patch.object(nginx_ops, "quick_edit_config", edited),
                ]
            ):
                st.enter_context(p)
            resp = await apps_router.app_nginx_quick(1, _req(body={"file": self.FOREIGN, "kind": "ws"}))
        self.assertEqual(resp.status_code, 400)
        edited.assert_not_awaited()

    def test_scope_compares_the_files_this_app_actually_owns(self):
        with mock.patch.object(nginx_ops, "find_app_configs", lambda *_a: [{"file": self.OWN}]):
            nginx_ops.assert_app_conf(self.OWN, 8000, "app.example.com")
            with self.assertRaises(RuntimeError):
                nginx_ops.assert_app_conf(self.FOREIGN, 8000, "app.example.com")
            with self.assertRaises(RuntimeError):
                # 目录外的路径由 _safe_conf_path 先拦下（不是"不属于本应用"，而是根本不能写）
                nginx_ops.assert_app_conf("/tmp/x.conf", 8000, "app.example.com")
        with mock.patch.object(nginx_ops, "find_app_configs", lambda *_a: []), self.assertRaises(RuntimeError):
            # 没有反代（匹配不到任何文件）时，应用详情页也没有可编辑的配置
            nginx_ops.assert_app_conf(self.OWN, None, None)


if __name__ == "__main__":
    unittest.main()
