"""批次⑦B：面板写的 unit 必须进开机目标（enable），失败时如实提醒。

接管线上 Rosetta 时暴露的缺口：`ensure_unit` 只写 unit + daemon-reload，从不 enable，
而 systemd 不会把新写的 unit 自动纳入开机目标——面板「部署成功」只保证这次拉起来了，
机器一重启应用就没了。手工维护的两个 rosetta unit 原本都是 enabled 的，也就是说
"改成面板托管"反而会让服务少一层保障，这与托管的语义正相反。

enable 失败的唯一现实原因是自定义 `unit_template` 里没有 [Install] 段：那种 unit 照样能
手动启动，判部署失败会把一次已经上线的发布说成红的，所以走 warn（和端口对不上、域名被
别处托管这两条同一个口径）。但 warn 必须一路到达用户眼前——部署日志和 PATCH 的 note
是两个出口，两个都得带上，而且 note 不能后写的那条把前一条覆盖掉。
"""

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch7b_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch7b_roots_"))

import unittest  # noqa: E402

from app import database as dbm  # noqa: E402
from app.services import apps_service  # noqa: E402

_NO_REPO = {"code": 128, "out": "", "err": "not a git repository"}


def _systemctl_only(fail_enable=False):
    """真跑非 systemctl 命令，systemctl 全部记账后伪造返回。

    普通用户（CI runner）上 `systemctl daemon-reload` 会回 "Interactive authentication
    required."，真调用的话三条用例测的就全是"这台机器不让我 reload"。
    """
    real_run = apps_service.run
    calls = []

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        if cmd == "systemctl":
            calls.append(list(args))
            if args and args[0] == "enable" and fail_enable:
                return {"code": 1, "out": "The unit files have no installation stanzas", "err": ""}
            return {"code": 0, "out": ""}
        if cmd == "git":
            return dict(_NO_REPO)
        return await real_run(cmd, args, cwd=cwd, timeout=timeout, env=env)

    return run, calls


class _Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.unit_dir = tempfile.mkdtemp(prefix="cp_units7b_")
        self.addCleanup(shutil.rmtree, self.unit_dir, True)
        self._old_dir = apps_service.UNIT_DIR
        apps_service.UNIT_DIR = self.unit_dir
        self.addCleanup(setattr, apps_service, "UNIT_DIR", self._old_dir)

        self.path = tempfile.mkdtemp(prefix="cp_app7b_")
        self.addCleanup(shutil.rmtree, self.path, True)
        self.name = f"sevenb{os.getpid()}{id(self) % 10000}"
        self.app = {
            "id": 0,
            "name": self.name,
            "type": "node",
            "repo_url": None,
            "branch": "main",
            "path": self.path,
            "domain": None,
            "port": 3000,
            "install_cmd": "true",
            "start_cmd": "node server.mjs",
            "env": '[{"k":"PORT","v":"3000"}]',
            "unit_override": None,
            "unit_template": None,
        }
        self.unit = f"panel-{self.name}.service"

    def unit_text(self):
        return Path(self.unit_dir, self.unit).read_text(encoding="utf-8")


class TestEnable(_Case):
    async def test_unit_is_enabled_after_daemon_reload(self):
        run, calls = _systemctl_only()
        with mock.patch.object(apps_service, "run", new=run):
            await apps_service.ensure_unit(self.app)
        self.assertEqual(calls, [["daemon-reload"], ["enable", "--", self.unit]])
        self.assertIn("[Install]", self.unit_text(), "默认模板必须自带 WantedBy，否则 enable 注定失败")

    async def test_override_app_is_left_alone(self):
        """纳管模式（unit_override）下面板一个 systemctl 命令都不许发——那是别人的 unit。"""
        run, calls = _systemctl_only()
        self.app["unit_override"] = "manual-thing.service"
        with mock.patch.object(apps_service, "run", new=run):
            await apps_service.ensure_unit(self.app)
        self.assertEqual(calls, [])
        self.assertFalse(Path(self.unit_dir, self.unit).exists())

    async def test_enable_failure_warns_but_still_writes_unit(self):
        """没 [Install] 的自定义模板：unit 照样能手动启动，不能因此判部署失败，但必须吭声。"""
        self.app["unit_template"] = "[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\n"
        warned = []
        run, calls = _systemctl_only(fail_enable=True)
        with mock.patch.object(apps_service, "run", new=run):
            await apps_service.ensure_unit(self.app, warn=warned.append)
        self.assertEqual(len(calls), 2)
        self.assertTrue(Path(self.unit_dir, self.unit).exists())
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("开机自启", warned[0])
        self.assertIn("installation stanzas", warned[0], "要把 systemd 原话带出来，用户才知道改哪儿")


class TestEnableSurfacing(_Case):
    async def test_deploy_log_carries_the_enable_warning(self):
        """warn 只到 ensure_unit 为止是没用的——用户看的是部署日志。"""
        app_id = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,domain,install_cmd,start_cmd)"
            " VALUES(?,'node','','main',?,NULL,'true','node server.mjs')",
            (self.name, self.path),
        )
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE app_id=?", (app_id,))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        self.app["id"] = app_id

        dep = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app_id,))
        run, _calls = _systemctl_only(fail_enable=True)
        app = apps_service.get_app(app_id)
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(apps_service, "run", run))
            st.enter_context(mock.patch.object(apps_service, "restart_and_verify", mock.AsyncMock()))
            await apps_service._run_deployment(app, dep)
        row = dbm.query_one("SELECT status, log FROM deployments WHERE id=?", (dep,))
        self.assertEqual(row["status"], "success", f"部署失败：{row['log'][-600:]}")
        self.assertIn("开机自启", row["log"], "日志必须说清这次没进开机目标")

    async def test_apply_edited_keeps_both_notes(self):
        """自启没设上 + 服务没重启成功可以同时成立，旧写法后一条覆盖前一条。"""
        run, _calls = _systemctl_only(fail_enable=True)
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(apps_service, "run", run))
            st.enter_context(mock.patch.object(apps_service, "write_env_file"))
            st.enter_context(
                mock.patch.object(apps_service, "is_active", mock.AsyncMock(return_value=True))
            )
            st.enter_context(
                mock.patch.object(
                    apps_service,
                    "restart_and_verify",
                    mock.AsyncMock(side_effect=RuntimeError("崩了")),
                )
            )
            out = await apps_service.apply_edited(self.app, self.app)
        self.assertIn("开机自启", out["note"], out["note"])
        self.assertIn("崩了", out["note"], out["note"])


class TestPortWaitWindow(unittest.IsolatedAsyncioTestCase):
    """重启后查端口的窗口要盖得住一个 python 应用的冷启动。

    线上 Rosetta 后端（`uvicorn --workers 2`）在旧的 5 次窗口内还没 bind 完（FastAPI 的
    import、连接池、路由注册都在启动期），于是每次部署都固定带一句"域名反代可能 502"的
    假警报——而 restart_and_verify 已经确认过主进程稳定存活，端口只是提醒，不是判据。
    """

    async def _verify(self, bind_after: int):
        seen = {"n": 0}

        async def detect(_unit, force=False):
            seen["n"] += 1
            return None if seen["n"] <= bind_after else 8000

        warned = []
        with contextlib.ExitStack() as st:
            st.enter_context(
                mock.patch.object(apps_service, "is_active", mock.AsyncMock(return_value=True))
            )
            st.enter_context(mock.patch.object(apps_service, "service_action", mock.AsyncMock()))
            st.enter_context(mock.patch.object(apps_service, "_wait_stable", mock.AsyncMock()))
            st.enter_context(mock.patch.object(apps_service, "detect_port", new=detect))
            st.enter_context(mock.patch.object(apps_service, "UNIT_SAMPLE_SEC", 0.01))
            await apps_service.restart_and_verify("panel-x.service", 8000, warn=warned.append)
        return warned, seen["n"]

    async def test_slow_bind_is_not_reported(self):
        warned, tries = await self._verify(bind_after=9)
        self.assertEqual(warned, [], f"9 秒才 bind 的应用不该报警：{warned}")
        self.assertLess(tries, apps_service.PORT_WAIT_TRIES, "命中就应当立刻收工，不空转")

    async def test_nothing_listening_says_it_has_not_bound(self):
        warned, tries = await self._verify(bind_after=10**9)
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("没探到", warned[0])
        self.assertIn("502", warned[0])
        self.assertNotIn("不一致", warned[0], '一个端口都没监听时不能说"监听在别的端口"')
        self.assertEqual(tries, apps_service.PORT_WAIT_TRIES, "提醒前要把窗口用满")
