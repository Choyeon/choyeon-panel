"""深度审查后的回归测试：每锁定一条本轮修掉的真实缺陷。

覆盖：应用安装目录不得指向面板自身要害目录、部署模板端口/安装命令自洽、
证书续期失败必须报错、二进制上传不被毁、viewer 看不到应用密钥与审计、
编辑应用会把 unit/.panel.env/站点真正重写并在服务运行时重启（改名时先拆旧再建新）、
仓库地址里的凭据不外泄、备份目标应用被删后要在列表里露出来、
公开状态接口不回显管理员用户名、CLI 的 --json 契约与部署等待、
文件接口对 viewer 全面关闭、写入不跟随符号链接、审计字段长度受约束、
终端会话计数不漏、psql 管道不互等，接口错误不留半成品变更，
以及下拉菜单的动作确实挂在能被触发的那个钩子上、编辑端口时启动命令里的旧端口被一起换掉。
"""

import asyncio
import contextlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_fix_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_fix_roots_"))

from fastapi.responses import JSONResponse  # noqa: E402

from app import config, templates  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import apps as apps_router  # noqa: E402
from app.routers import extras
from app.services import apps_service, backup_service, cert_ops, files_service, nginx_ops, pg_service, systemd_ops  # noqa: E402
from app.util import redact_creds, sub_port  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent

_seq = itertools.count()


def _uid(prefix: str) -> str:
    """唯一且合法的标识（用户名/应用名/backups.target 都走 3-32 位 [A-Za-z0-9_-]）。"""
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _req(role: str = "admin", sub: str = "tester", query: dict | None = None, body=None, headers: dict | None = None):
    async def _json():
        return body

    return SimpleNamespace(
        state=SimpleNamespace(cp_role=role, cp_sub=sub),
        query_params=query or {},
        headers=headers or {},
        json=_json,
    )


class TestProtectedInstallPath(unittest.TestCase):
    """回归：path 指向面板仓库/数据目录时，部署会 git reset --hard 甚至 purge 掉面板自己。"""

    def test_rejects_panel_repo_and_data_dir(self):
        for target in (str(config.BASE), str(config.DATA_DIR), str(config.UNIT_DIR)):
            with self.subTest(target=target), self.assertRaises(RuntimeError) as ctx:
                apps_service._check_not_protected(target)
            self.assertIn("不能位于", str(ctx.exception))

    def test_rejects_nested_inside_panel_repo(self):
        with self.assertRaises(RuntimeError):
            apps_service._check_not_protected(f"{config.BASE}/sub/dir")

    def test_allows_ordinary_app_dir(self):
        root = tempfile.mkdtemp(prefix="cp_app_ok_")
        self.addCleanup(shutil.rmtree, root, True)
        apps_service._check_not_protected(f"{root}/myapp")

    def test_validate_calls_the_guard(self):
        with mock.patch.object(apps_service, "_check_not_protected") as guard:
            apps_service._validate(
                {"name": "guard-probe", "type": "node", "start_cmd": "node s.js", "path": f"{config.BASE}/x"},
                ignore_id=-1,
            )
        guard.assert_called_once_with(f"{config.BASE}/x")
        self.assertEqual(dbm.query_one("SELECT id FROM apps WHERE name=?", ("guard-probe",)), None)


class TestDeployTemplateConsistency(unittest.TestCase):
    def test_static_site_has_noop_install(self):
        t = templates.get_template("static-site")
        self.assertTrue(t["install_cmd"])
        # 空串会被部署流程解释成"按 type 回落默认"，python 的默认要求有 requirements.txt
        self.assertNotIn("requirements.txt", t["install_cmd"])

    def test_port_override_rewrites_start_cmd_and_env(self):
        merged = templates.apply_template({"template": "static-site", "name": "p1", "port": 8088})
        self.assertIn("8088", merged["start_cmd"])
        self.assertNotIn(" 8000 ", merged["start_cmd"] + " ")
        self.assertEqual(merged["port"], 8088)
        node = templates.apply_template({"template": "node-next", "name": "p2", "port": 3101})
        self.assertEqual([e for e in node["env"] if e["k"] == "PORT"][0]["v"], "3101")

    def test_explicit_start_cmd_still_wins(self):
        merged = templates.apply_template(
            {"template": "static-site", "name": "p3", "port": 9000, "start_cmd": "caddy run --config ./Caddyfile"}
        )
        self.assertEqual(merged["start_cmd"], "caddy run --config ./Caddyfile")


class TestPortEditKeepsCommandInSync(unittest.TestCase):
    """回归：建应用时模板会把端口写进 start_cmd/env，但**编辑端口时不会**。
    结果是库里记新端口、进程还在听旧端口，nginx 反代过去直接 502，
    而面板上一切显示正常（端口、状态、部署成功）。
    """

    def _seed(self, name: str, port: int, start_cmd: str, env: list) -> int:
        res = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,start_cmd,env)"
            " VALUES(?,?,?,?,?,?,NULL,NULL,?,?)",
            (name, "python", "", "main", f"/tmp/{name}", port, start_cmd, json.dumps(env)),
        )
        app_id = res if isinstance(res, int) else res.lastrowid
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        return app_id

    def test_port_only_patch_retargets_command_and_env(self):
        app_id = self._seed(
            _uid("port-a"), 3456, "python3 -m http.server 3456 --bind 127.0.0.1",
            [{"k": "PORT", "v": "3456"}],
        )
        out = apps_service.update_app(app_id, {"port": 3457})
        self.assertEqual(out["port"], 3457)
        self.assertIn("http.server 3457", out["start_cmd"])
        self.assertNotIn("3456", out["start_cmd"])
        self.assertEqual(json.loads(out["env"])[0]["v"], "3457")

    def test_explicit_start_cmd_wins(self):
        """用户自己重写过启动命令时不再猜端口，避免把显式输入改坏。"""
        app_id = self._seed(_uid("port-b"), 3456, "python3 -m http.server 3456", [])
        out = apps_service.update_app(app_id, {"port": 3457, "start_cmd": "gunicorn -b 127.0.0.1:9000 app:app"})
        self.assertEqual(out["start_cmd"], "gunicorn -b 127.0.0.1:9000 app:app")

    def test_explicit_env_wins(self):
        app_id = self._seed(_uid("port-c"), 3456, "python3 -m http.server 3456", [{"k": "PORT", "v": "3456"}])
        out = apps_service.update_app(app_id, {"port": 3457, "env": [{"k": "PORT", "v": "8080"}]})
        self.assertEqual(json.loads(out["env"]), [{"k": "PORT", "v": "8080"}])

    def test_contained_port_digits_not_corrupted(self):
        """13456 里包含 3456：必须按整词替换，不能 str.replace 把它改成 13457。"""
        app_id = self._seed(_uid("port-d"), 3456, "python3 -m http.server 3456 --other-app-port 13456", [])
        out = apps_service.update_app(app_id, {"port": 3457})
        self.assertIn("--other-app-port 13456", out["start_cmd"])
        self.assertIn("http.server 3457", out["start_cmd"])

    def test_helper_edges(self):
        self.assertEqual(sub_port("node dist/index.js", 3000, 3001), "node dist/index.js")
        self.assertEqual(sub_port(None, 3000, 3001), None)
        self.assertEqual(sub_port("", 3000, 3001), "")
        self.assertEqual(sub_port("PORT=3000", 3000, None), "PORT=3000")
        self.assertEqual(sub_port("PORT=3000", 3000, 3000), "PORT=3000")
        self.assertEqual(sub_port("--port 3000,", 3000, 3001), "--port 3001,")


class TestCertbotRenewFailsLoudly(unittest.TestCase):
    """回归：`certbot renew` 全部失败时，接口仍返回"执行完成"（run 的退出码被丢弃）。"""

    def test_raises_on_nonzero(self):
        async def fake_run(*_a, **_k):
            return {"code": 1, "out": "all attempts failed", "err": ""}

        with mock.patch.object(nginx_ops, "run", fake_run), self.assertRaises(RuntimeError) as ctx:
            asyncio.run(nginx_ops.renew_certs())
        self.assertIn("续期失败", str(ctx.exception))

    def test_ok_on_zero(self):
        async def fake_run(*_a, **_k):
            return {"code": 0, "out": "Certificate not yet due for renewal", "err": ""}

        async def _certs():
            return {"certs": [{"name": "demo.example.com", "expiry": "2026-11-01"}]}

        with mock.patch.object(nginx_ops, "run", fake_run), mock.patch.object(cert_ops, "list_certs", _certs):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertIn("renewal", r["log"])
        # 退出码 0 且到期日没变 = certbot 什么都没做。旧实现把它报成"已续期"，
        # 用户以为证书换了，实际上原证书还在跑。
        self.assertEqual(r["renewed"], [])
        self.assertFalse(r["reloaded"])
        self.assertIn("没有证书到期", r["summary"])

    def test_real_renewal_is_detected_and_nginx_reloaded(self):
        """到期日真的往后推了才算续到，并且必须 reload nginx（文件换了≠进程在用）。"""
        seq = iter(["2026-10-20", "2027-01-19"])
        calls: list[str] = []

        async def fake_run(cmd, args, **_k):
            calls.append(" ".join([cmd, *args]))
            return {"code": 0, "out": "Certificate is due for renewal, updating", "err": ""}

        async def _certs():
            return {"certs": [{"name": "demo.example.com", "expiry": next(seq)}]}

        with mock.patch.object(nginx_ops, "run", fake_run), mock.patch.object(cert_ops, "list_certs", _certs):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertEqual(r["renewed"], ["demo.example.com"])
        self.assertTrue(r["reloaded"])
        self.assertIn("systemctl reload nginx", calls)
        self.assertIn("已续期 1 张", r["summary"])

    def test_route_returns_400_and_no_audit_on_failure(self):
        before = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("certbot:renew",))["c"]
        with mock.patch.object(nginx_ops, "renew_certs", side_effect=RuntimeError("boom")):
            resp = asyncio.run(extras.certbot_renew(_req()))
        self.assertEqual(resp.status_code, 400)
        after = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("certbot:renew",))["c"]
        self.assertEqual(after, before, "失败的续期不该留下一条已执行审计")

    def test_route_audits_the_conclusion(self):
        """审计里要能看到这次到底续没续上，只记"已执行"事后无从区分。"""
        res = {"log": "x", "renewed": ["demo.example.com"], "unchanged": [],
               "reloaded": True, "summary": "已续期 1 张：demo.example.com，nginx 已重载"}
        with mock.patch.object(nginx_ops, "renew_certs", mock.AsyncMock(return_value=res)), \
             mock.patch.object(extras.aio_db, "audit") as audit_stub:
            r = asyncio.run(extras.certbot_renew(_req()))
        self.assertEqual(r["renewed"], ["demo.example.com"])
        self.assertEqual(audit_stub.call_args.args[2], res["summary"])


class TestBodySizeAcceptsGigabyte(unittest.TestCase):
    """回归：提示语写"应如 50m / 1g"，正则却不接受 g。"""

    def test_regex(self):
        import re

        rx = re.compile(r"^\d{1,4}[kmg]$", re.I)
        self.assertTrue(rx.match("1g"))
        self.assertTrue(rx.match("50m"))
        self.assertFalse(rx.match("10000m"))


class TestBinaryUploadNotCorrupted(unittest.TestCase):
    """回归：上传曾走 file.text() + utf8 解码，二进制文件被静默替换成 U+FFFD 仍报成功。"""

    def test_bytes_roundtrip(self):
        root = config.FILE_ROOTS[0]
        os.makedirs(root, exist_ok=True)
        target = f"{root}/cp_bin_probe.bin"
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        payload = bytes(range(256)) * 4
        files_service.write_text(target, payload)
        self.assertEqual(Path(target).read_bytes(), payload, "写入必须按原字节，不能经 utf8 往返")


class TestViewerPermission(unittest.TestCase):
    def test_audit_is_admin_only(self):
        resp = asyncio.run(extras.audit_list(_req(role="viewer")))
        self.assertEqual(resp.status_code, 403)

    def test_app_detail_hides_env_from_viewer(self):
        res = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,start_cmd,env)"
            " VALUES('perm-probe','node','','main','/tmp/cp-perm-probe',8123,NULL,NULL,'node x.js',?)",
            (json.dumps([{"k": "SECRET", "v": "s3cr3t"}]),),
        )
        app_id = res if isinstance(res, int) else res.lastrowid
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        with mock.patch.object(apps_service, "detect_port", lambda *_: asyncio.sleep(0, None)), \
             mock.patch.object(apps_service.systemd_ops, "is_active", lambda *_: asyncio.sleep(0, False)):
            admin = asyncio.run(apps_router.app_get(app_id, _req(role="admin")))
            viewer = asyncio.run(apps_router.app_get(app_id, _req(role="viewer")))
        self.assertIn("s3cr3t", str(admin["env"]))
        self.assertEqual(json.loads(viewer["env"]), [], "viewer 不该看到应用密钥")
        self.assertNotIn("s3cr3t", json.dumps(viewer))


class TestApplyEdited(unittest.IsolatedAsyncioTestCase):
    """PATCH 之后要把改动真正落到 systemd / .panel.env / nginx，并如实报告生效没有。

    两处都是"一半问题"：
    - 只拆不建：旧 unit disable --now 停掉、新名字的 unit 还没写，服务当场消失、
      域名 502，而 PATCH 只回一句"已保存"。
    - 改名以外的字段干脆不同步：数据库里换了启动命令/端口/域名/环境变量，进程还按
      老的 ExecStart 跑着，"改了没反应"成了常态——用户只能再点一次几分钟的「部署」。
    """

    OLD = {"id": 7, "name": "before", "type": "node", "path": "/tmp/cp-x", "port": 8124,
           "domain": "a.example.com", "start_cmd": "node x.js", "env": "[]"}

    async def _run(self, changes=None, active=True, fail_on=None, old_override=None):
        new = {**self.OLD, **(changes or {})}
        old = {**self.OLD, "unit_override": old_override} if old_override else self.OLD
        patches = {
            "is_active": mock.patch.object(apps_service, "is_active", lambda *_: asyncio.sleep(0, active)),
            # restart_and_verify 判的是"主进程持续存活"，测试机上没有真进程，给一个固定 pid
            "_live_pid": mock.patch.object(apps_service, "_live_pid", lambda *_: asyncio.sleep(0, 4321)),
            "UNIT_SAMPLE_SEC": mock.patch.object(apps_service, "UNIT_SAMPLE_SEC", 0.01),
            "remove_unit": mock.patch.object(apps_service, "remove_unit"),
            "ensure_unit": mock.patch.object(apps_service, "ensure_unit"),
            "write_env_file": mock.patch.object(apps_service, "write_env_file"),
            # 真实实现会去读 /etc/systemd/system，测试里只关心"调用没调用、参数对不对"
            "ensure_env_link": mock.patch.object(apps_service, "ensure_env_link"),
            "remove_env_link": mock.patch.object(apps_service, "remove_env_link"),
            "service_action": mock.patch.object(apps_service, "service_action"),
            "remove_vhost": mock.patch.object(apps_service.nginx_ops, "remove_vhost"),
            "apply_vhost": mock.patch.object(apps_service.nginx_ops, "apply_vhost"),
        }
        with contextlib.ExitStack() as st:
            m = {k: st.enter_context(p) for k, p in patches.items()}
            if fail_on:
                m[fail_on].side_effect = RuntimeError("systemd-analyze verify 不通过")
            out = await apps_service.apply_edited(old, new)
        return SimpleNamespace(**m, out=out)

    async def test_old_unit_and_vhost_removed_on_rename(self):
        r = await self._run({"name": "after"})
        r.remove_unit.assert_called_once()
        self.assertEqual(r.remove_unit.call_args.args[0]["name"], "before")
        r.remove_vhost.assert_called_once_with("before")

    async def test_new_unit_env_and_vhost_rebuilt_on_rename(self):
        r = await self._run({"name": "after"})
        r.ensure_unit.assert_called_once()
        self.assertEqual(r.ensure_unit.call_args.args[0]["name"], "after")
        self.assertEqual(r.write_env_file.call_args.args[0]["name"], "after")
        self.assertEqual(r.apply_vhost.call_args.args[0]["name"], "after")
        self.assertTrue(r.out["applied"])
        # 改名前在跑 → 新名字得接着跑，否则改名等于把服务停了
        self.assertEqual(r.service_action.call_args.args, ("panel-after.service", "restart"))
        self.assertTrue(r.out["restarted"])

    async def test_inactive_app_is_not_started_by_rename(self):
        r = await self._run({"name": "after"}, active=False)
        r.ensure_unit.assert_called_once()
        r.service_action.assert_not_called()
        self.assertFalse(r.out["restarted"])
        self.assertIn("服务当前没有运行", r.out["note"], "停止状态保存要说清改动何时生效，别让人以为已经生效")

    async def test_same_name_edit_still_reapplies_unit_and_vhost(self):
        """本批次的核心：不改名也要重新渲染 unit、重写 .panel.env、重配站点并重启。"""
        r = await self._run({"start_cmd": "node y.js --port 8124", "port": 8124})
        r.remove_unit.assert_not_called()
        r.remove_vhost.assert_not_called()
        r.ensure_unit.assert_called_once()
        r.write_env_file.assert_called_once()
        r.apply_vhost.assert_called_once()
        self.assertEqual(r.service_action.call_args.args, ("panel-before.service", "restart"))
        self.assertTrue(r.out["restarted"], "正在运行的服务必须跟着这次编辑重启，否则改动不生效")

    async def test_stopped_service_is_not_started_by_an_edit(self):
        r = await self._run({"start_cmd": "node y.js"}, active=False)
        r.ensure_unit.assert_called_once()
        r.service_action.assert_not_called()
        self.assertIn("下一次启动或部署时生效", r.out["note"])

    async def test_cleared_domain_removes_the_vhost(self):
        """域名清空后留着那个 server 块，就是一个指向死端口的 502。"""
        r = await self._run({"domain": None})
        r.apply_vhost.assert_not_called()
        r.remove_vhost.assert_called_once_with("before")

    async def test_unit_override_writes_env_but_not_the_unit(self):
        """外部 unit：unit 本体与 nginx 不归面板写，但 .panel.env 属于应用目录，必须落地。

        批次⑳之前的写法是直接 return "只改了记录"，连环境变量文件都不写——界面上
        ADMIN_PASSWORD 显示已经改好，磁盘上没有 .panel.env，进程读的也还是老配置。
        """
        r = await self._run({"unit_override": "their-app.service", "env": '[{"k":"A","v":"1"}]'})
        r.ensure_unit.assert_not_called()
        r.apply_vhost.assert_not_called()
        r.remove_vhost.assert_not_called()
        r.write_env_file.assert_called_once()
        r.ensure_env_link.assert_called_once()
        self.assertEqual(r.service_action.call_args.args, ("their-app.service", "restart"))
        self.assertTrue(r.out["restarted"])
        self.assertTrue(r.out["applied"])

    async def test_unit_override_reports_unit_scoped_fields_as_record_only(self):
        """启动命令/端口活在 unit 本体里，这部分对外部 unit 确实只进了记录，要照实说。"""
        r = await self._run({"unit_override": "their-app.service", "start_cmd": "node z.js"})
        self.assertIn("启动命令", r.out["note"])
        self.assertIn("面板不写外部 unit", r.out["note"])
        # 同一句话里不能把已经生效的环境变量也说成没生效
        self.assertTrue(r.out["restarted"])

    async def test_external_to_managed_drops_the_old_env_link(self):
        """切回面板托管后 unit 名换了（panel-*），旧 unit 的 drop-in 不会再有人读。"""
        r = await self._run({"unit_override": None}, old_override="external-x.service")
        r.remove_env_link.assert_called_once()
        self.assertEqual(r.remove_env_link.call_args.args[0]["unit_override"], "external-x.service")
        r.ensure_unit.assert_called_once()

    async def test_sync_failure_is_reported_instead_of_silent_saved(self):
        r = await self._run({"start_cmd": "node y.js"}, fail_on="ensure_unit")
        r.service_action.assert_not_called()
        self.assertFalse(r.out["applied"])
        self.assertIn("没能同步", r.out["note"])
        self.assertIn("systemd-analyze verify 不通过", r.out["note"])

    async def test_failed_restart_keeps_note_but_leaves_config_written(self):
        r = await self._run({"start_cmd": "node y.js"}, fail_on="service_action")
        r.ensure_unit.assert_called_once()
        self.assertTrue(r.out["applied"], "unit 已经写成功了，不能报成没保存")
        self.assertFalse(r.out["restarted"])
        self.assertIn("没能重启成功", r.out["note"])


class TestUnitStatesParsing(unittest.IsolatedAsyncioTestCase):
    """systemctl show 的属性顺序由 systemd 自己定，MainPID 实测排在 Id **前面**。

    旧解析器"见到 Id 才开始建块"，于是排在 Id 之前的 MainPID 被整块丢弃：
    应用列表的 pid 恒为 0，而 restart_and_verify 靠主进程判活性，于是
    "服务明明在跑、面板却说主进程没稳定存活"（多 unit 时还会串到上一个块）。
    """

    REAL = ("MainPID=571180\nId=choyeon-panel.service\nActiveState=active\n\n"
            "MainPID=6507\nId=nginx.service\nActiveState=active\n\n"
            "MainPID=0\nId=gone.service\nActiveState=inactive")

    async def _call(self, out, units):
        async def fake_run(cmd, args, cwd=None, timeout=120, env=None):
            return {"code": 0, "out": out}

        with mock.patch.object(systemd_ops, "run", fake_run):
            return await systemd_ops.unit_states(units)

    async def test_properties_before_id_are_kept(self):
        """只钉住这条回归本来的判据（MainPID 不能丢）；状态字典的完整形状由批次㉜ 守。"""
        got = await self._call(self.REAL, ["choyeon-panel.service", "nginx.service", "gone.service"])
        for unit, want in (("choyeon-panel.service", (True, 571180)),
                           ("nginx.service", (True, 6507)),
                           ("gone.service", (False, 0))):
            with self.subTest(unit=unit):
                self.assertEqual((got[unit]["active"], got[unit]["mainPid"]), want)

    async def test_missing_unit_gets_not_running_default(self):
        got = await self._call("MainPID=1\nId=a.service\nActiveState=active", ["a.service", "b.service"])
        self.assertEqual(got["a.service"]["mainPid"], 1)
        # 批次㉜ 之后是"未知"而不是"已停止"：systemctl 没回话时不能说这台机器上服务停了
        self.assertEqual(got["b.service"], systemd_ops.UNKNOWN_STATE)

    async def test_single_block_without_separators(self):
        got = await self._call("MainPID=42\nId=x.service\nActiveState=active", ["x.service"])
        self.assertEqual((got["x.service"]["active"], got["x.service"]["mainPid"]), (True, 42))


class TestCredentialRedaction(unittest.IsolatedAsyncioTestCase):
    """回归：仓库地址可以写成 https://user:token@host/x.git，token 会随列表接口、
    部署日志和详情页外泄给只读账号。写入与读取两侧都要打码，历史行也一并清掉。
    """

    SECRET = "ghp_SUpErSecret123"

    def test_url_with_password_is_masked(self):
        self.assertEqual(
            redact_creds(f"git clone https://octo:{self.SECRET}@github.com/a/b.git"),
            "git clone https://octo:***@github.com/a/b.git",
        )

    def test_plain_urls_untouched(self):
        for s in (
            "https://github.com/a/b.git",
            "git@github.com:a/b.git",
            "https://octo@github.com/a/b.git",  # 只有用户名，不是密钥
            "",
            None,
        ):
            self.assertEqual(redact_creds(s), s)

    def test_deploy_log_never_stores_the_token(self):
        dep = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(0,'running')", ())
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE id=?", (dep,))
        apps_service._append_log(dep, f"致命错误：无法访问 'https://x:{self.SECRET}@github.com/a/b.git/'")
        log = dbm.query_one("SELECT log FROM deployments WHERE id=?", (dep,))["log"]
        self.assertNotIn(self.SECRET, log)
        self.assertIn("https://x:***@github.com/a/b.git", log)

    async def test_list_endpoint_masks_for_viewer_only(self):
        name = _uid("cred")
        res = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,start_cmd,env)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, "node", f"https://ci:{self.SECRET}@github.com/a/b.git", "main",
             f"/tmp/cp-{name}", 8130, None, None, "node x.js", "[]"),
        )
        app_id = res if isinstance(res, int) else res.lastrowid
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        with mock.patch.object(apps_service.systemd_ops, "unit_states",
                               lambda *_: asyncio.sleep(0, {})), \
             mock.patch.object(apps_service.nginx_ops, "domains_by_port", lambda: {}):
            viewer = await apps_router.apps_list(_req(role="viewer"))
            admin = await apps_router.apps_list(_req(role="admin"))
        row_v = next(r for r in viewer if r["id"] == app_id)
        row_a = next(r for r in admin if r["id"] == app_id)
        self.assertNotIn(self.SECRET, json.dumps(row_v))
        self.assertIn("https://ci:***@github.com/a/b.git", row_v["repo_url"])
        self.assertIn(self.SECRET, row_a["repo_url"], "admin 需要能改回原地址")


class TestBackupPlanCompensation(unittest.IsolatedAsyncioTestCase):
    """回归：systemd 落地失败时不能留半条计划。

    旧写法 INSERT 之后直接 _sync_timer，抛了也把行留在库里：界面显示"已启用"，
    实际 timer 不存在，用户以为有备份。update 同理，改不过去就还原改前的值。
    """

    async def test_create_failure_leaves_no_row(self):
        before = len(dbm.query("SELECT id FROM backups"))
        with mock.patch.object(backup_service, "_sync_timer",
                               side_effect=RuntimeError("systemctl 不可用")), \
             mock.patch.object(backup_service, "delete_backup",
                               lambda *_: asyncio.sleep(0, None)), \
             self.assertRaises(RuntimeError) as ctx:
            await backup_service.create_backup({"kind": "pg", "target": "all", "schedule": "daily"})
        self.assertIn("已撤销", str(ctx.exception))
        self.assertEqual(len(dbm.query("SELECT id FROM backups")), before, "失败的备份计划必须整条撤销")

    async def test_update_failure_restores_previous_config(self):
        res = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES('pg','all','daily',3,30,7,0)",
            ())
        bid = res if isinstance(res, int) else res.lastrowid
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (bid,))
        with mock.patch.object(backup_service, "_sync_timer",
                               side_effect=RuntimeError("写入 unit 失败")), \
             self.assertRaises(RuntimeError) as ctx:
            await backup_service.update_backup(bid, {"hour": 9, "enabled": True})
        self.assertIn("已还原", str(ctx.exception))
        row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
        self.assertEqual((row["hour"], row["enabled"]), (3, 0), "校验不过不能留半个变更")


class TestStaticCacheHeaders(unittest.IsolatedAsyncioTestCase):
    """回归：静态产物不发 Cache-Control，浏览器按 Last-Modified 启发式缓存 index.html，
    发版后旧标签页继续请求已被新构建删掉的 /assets/*.js，表现为 404 + 动态 import 失败 + 白屏。
    """

    def setUp(self):
        from app.main import DistFiles

        self.root = tempfile.mkdtemp(prefix="cp_dist_")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(f"{self.root}/assets")
        Path(f"{self.root}/index.html").write_text("<html>ok</html>")
        Path(f"{self.root}/assets/Apps-abc123.js").write_text("console.log(1)")
        Path(f"{self.root}/favicon.svg").write_text("<svg/>")
        self.app = DistFiles(directory=self.root, html=True)

    async def _get(self, path: str):
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [], "client": ("127.0.0.1", 50000), "server": ("test", 80),
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        await self.app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
        return start["status"], headers

    async def test_html_revalidates_and_assets_are_immutable(self):
        status, html = await self._get("/")
        self.assertEqual(status, 200)
        self.assertEqual(html["cache-control"], "no-cache", "入口 HTML 不能启发式缓存")
        _, asset = await self._get("/assets/Apps-abc123.js")
        self.assertEqual(asset["cache-control"], "public, max-age=31536000, immutable")
        _, icon = await self._get("/favicon.svg")
        self.assertEqual(icon["cache-control"], "public, max-age=3600")


class TestSslNeedsPort(unittest.TestCase):
    async def _call(self):
        app = {"id": 9, "name": "noport", "domain": "c.example.com", "port": None}
        with mock.patch.object(apps_service, "get_app", lambda *_: app), \
             mock.patch.object(nginx_ops, "issue_cert") as issue, self.assertRaises(RuntimeError) as ctx:
            await apps_service.attach_ssl(9)
        issue.assert_not_called()
        self.assertIn("端口", str(ctx.exception))

    def test(self):
        asyncio.run(self._call())


class TestCliJsonContract(unittest.TestCase):
    """回归：choyeonctl doctor/deploy --json 曾经 0 字节输出且 exit 0，AGENTS.md 的契约不成立。"""

    def _cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(BACKEND)
        return subprocess.run(
            [sys.executable, str(BACKEND / "cli.py"), *args, "--json"],
            capture_output=True, text=True, timeout=180, env=env,
            cwd=str(BACKEND), encoding="utf-8", errors="replace",
        )

    def test_doctor_prints_json(self):
        p = self._cli("doctor")
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertIn("counts", data)
        self.assertIn("items", data)

    def test_status_prints_json(self):
        p = self._cli("status")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("healthy", json.loads(p.stdout))


class TestBackupStaleTarget(unittest.TestCase):
    """回归：应用被删掉后备份任务照旧"已启用"，timer 每晚 SystemExit，面板上看不出任何异常。"""

    def test_flags_missing_app_target(self):
        from app.services import backup_service as B

        name = _uid("bkapp")
        dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled)"
            " VALUES('app',?,'daily',3,30,7,1)",
            (name,),
        )
        dbm.execute("INSERT INTO apps(name,type,repo_url,path,start_cmd) VALUES(?,'node','r','/tmp/x','s')", (name,))
        row = dbm.query_one("SELECT id FROM backups WHERE target=?", (name,))
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (row["id"],))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE name=?", (name,))
        listed = {b["id"]: b for b in B.list_backups()}
        self.assertFalse(listed[row["id"]]["target_missing"], "应用还在时不该报缺失")

        dbm.execute("DELETE FROM apps WHERE name=?", (name,))
        listed = {b["id"]: b for b in B.list_backups()}
        self.assertTrue(listed[row["id"]]["target_missing"], "应用被删后必须标记目标缺失")

    def test_pg_target_not_flagged(self):
        from app.services import backup_service as B

        name = _uid("bkpg")
        dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled)"
            " VALUES('pg',?,'daily',3,30,7,1)",
            (name,),
        )
        row = dbm.query_one("SELECT id FROM backups WHERE target=?", (name,))
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (row["id"],))
        listed = {b["id"]: b for b in B.list_backups()}
        # pg 库存在性要连服务器才知道，列表里误报只会制造噪音
        self.assertFalse(listed[row["id"]]["target_missing"])


class TestAuthStatusPrivacy(unittest.IsolatedAsyncioTestCase):
    """回归：/api/auth/status 未登录即可访问，回显账号名等于把管理员用户名公开。"""

    async def test_public_status_has_no_username(self):
        from app import main

        with mock.patch.object(main.dbm, "query_one", return_value={"username": "admin"}):
            res = await main.auth_status()
        self.assertFalse(res["needsSetup"])
        self.assertNotIn("admin", json.dumps(res))

    async def test_empty_database_still_reports_setup(self):
        from app import main

        with mock.patch.object(main.dbm, "query_one", return_value=None):
            self.assertTrue((await main.auth_status())["needsSetup"])


class TestFileApiIsAdminOnly(unittest.IsolatedAsyncioTestCase):
    """回归：文件接口能直接读到应用目录下的 .env，把 app_get 的脱敏整份绕过。"""

    async def test_viewer_blocked_on_every_endpoint(self):
        from app.routers import files as files_router

        for name in ("files_list", "files_read", "files_write", "files_download", "files_action"):
            with self.subTest(endpoint=name):
                resp = await getattr(files_router, name)(_req(role="viewer"))
                self.assertEqual(resp.status_code, 403, f"{name} 必须要求管理员")
                self.assertIn("管理员", resp.body.decode())

    async def test_admin_list_reports_server_side_roots(self):
        from app.routers import files as files_router

        res = await files_router.files_list(_req(query={"path": ""}))
        self.assertNotIsInstance(res, JSONResponse, "空 path 应回落到白名单第一项，而不是报错")
        self.assertEqual(res["roots"], list(files_service.ROOTS))
        self.assertEqual(
            res["path"],
            files_service.safe_path(files_service.ROOTS[0]),
            "起始目录必须由服务层按白名单给出，不能由前端写死",
        )


class TestWriteRefusesSymlinkTarget(unittest.TestCase):
    """回归：检查与写入之间把目标换成符号链接，内容就会被写到白名单之外（TOCTOU）。

    O_NOFOLLOW 只在"已经算好的规范路径此时又变成了链接"这一窗口里生效，
    而 safe_path 本身会先解析链接，所以这里用打桩复现那个窗口，
    并另加一条用例确认正常（指向白名单内文件）的链接编辑没被打破。
    """

    def _paths(self):
        root = files_service.ROOTS[0]
        os.makedirs(root, exist_ok=True)
        return root, f"{root}/cp_follow_real.txt", f"{root}/cp_follow_link.txt"

    def test_swapped_target_rejected(self):
        root, real, link = self._paths()
        self.addCleanup(lambda: os.path.exists(real) and os.remove(real))
        self.addCleanup(lambda: os.path.lexists(link) and os.remove(link))
        Path(real).write_text("original")
        os.symlink(real, link)
        # 模拟竞态：safe_path 已返回规范路径，随后那个路径被换成了链接
        with mock.patch.object(files_service, "safe_path", return_value=link), self.assertRaises(RuntimeError) as ctx:
            files_service.write_text(link, "overwritten")
        self.assertIn("符号链接", str(ctx.exception))
        self.assertEqual(Path(real).read_text(), "original", "拒绝写入时原文件必须没被动过")

    def test_ordinary_write_still_lands(self):
        root, real, _ = self._paths()
        self.addCleanup(lambda: os.path.exists(real) and os.remove(real))
        r = files_service.write_text(real, "content")
        self.assertEqual(r["size"], len("content"))
        self.assertEqual(Path(real).read_text(), "content")


class TestAuditFieldsTruncated(unittest.TestCase):
    """回归：audit 只截断 detail，action 有一部分直接拼请求体，一条超长输入就能撑爆审计表。"""

    def test_username_and_action_clamped(self):
        dbm.audit("u" * 400, "a" * 400, "d" * 4000)
        row = dbm.query_one("SELECT username,action,detail FROM audit ORDER BY id DESC LIMIT 1")
        self.assertEqual(len(row["username"]), 64)
        self.assertEqual(len(row["action"]), 64)
        self.assertEqual(len(row["detail"]), 500)
        dbm.execute("DELETE FROM audit WHERE id=(SELECT MAX(id) FROM audit)")


class TestNginxQuickKindWhitelisted(unittest.IsolatedAsyncioTestCase):
    """回归：kind 是拼进审计动作名的请求体字段，且未知值会被当成"强制 HTTPS 跳转"执行。"""

    @staticmethod
    def _app():
        # 路由现在先取应用记录（保存范围要按这个应用的端口/域名收口），
        # 这里只关心 kind/file 的校验，给一个假记录即可。
        from app.routers import apps as apps_router

        return mock.patch.object(
            apps_router.apps_service,
            "get_app",
            lambda _id: {"id": 1, "name": "a", "unit_override": None, "port": 8000, "domain": "x.example.com"},
        )

    async def test_unknown_kind_rejected_before_touching_nginx(self):
        from app.routers import apps as apps_router

        with self._app(), mock.patch.object(nginx_ops, "quick_edit_config") as edit:
            resp = await apps_router.app_nginx_quick(1, _req(body={"file": "/etc/nginx/conf.d/a", "kind": "rm-all"}))
        self.assertEqual(resp.status_code, 400)
        edit.assert_not_called()
        self.assertEqual(
            dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action LIKE ?", ("app:nginx-rm%",))["c"],
            0,
            "未知 kind 不该留下一条以它命名的审计",
        )

    async def test_missing_file_rejected(self):
        from app.routers import apps as apps_router

        with self._app(), mock.patch.object(nginx_ops, "quick_edit_config") as edit:
            resp = await apps_router.app_nginx_quick(1, _req(body={"kind": "ws"}))
        self.assertEqual(resp.status_code, 400)
        edit.assert_not_called()

    def test_service_layer_also_rejects(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(nginx_ops.quick_edit_config("/etc/nginx/conf.d/x.conf", "nonsense"))


class TestAlertsSaveAuditOnlyOnSuccess(unittest.IsolatedAsyncioTestCase):
    """回归：校验不过时一个字段都没落库，却先写了一条 settings:alerts 审计。"""

    async def test_rejected_save_leaves_no_audit(self):
        before = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("settings:alerts",))["c"]
        resp = await extras.alerts_save(_req(body={"alert_disk_pct": "abc"}))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("settings:alerts",))["c"],
            before,
            "没有任何变更时不该留下一条看起来改过配置的审计",
        )

    async def test_good_save_is_audited(self):
        before = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("settings:alerts",))["c"]
        self.assertTrue(await extras.alerts_save(_req(body={"alert_disk_pct": "90"})))
        self.assertEqual(
            dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("settings:alerts",))["c"], before + 1
        )


class TestRedisPasswordCanBeCleared(unittest.IsolatedAsyncioTestCase):
    """回归：set_redis_password 忽略空值，填错口令后在界面上永远清不掉。"""

    async def test_empty_clears(self):
        from app.routers import dbops

        dbm.set_setting("redis_password", "wrong-on-purpose")
        self.addCleanup(dbm.set_setting, "redis_password", "")
        await dbops.redis_password(_req(body={"password": ""}))
        self.assertEqual(dbm.get_setting("redis_password"), "", "空值必须真的清掉旧口令")
        self.assertGreater(
            dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("db:redis-password",))["c"],
            0,
            "改动凭据必须留痕",
        )

    async def test_whitespace_rejected(self):
        from app.services import pg_service

        with self.assertRaises(RuntimeError):
            pg_service.set_redis_password("has space")


class TestRedisPasswordCache(unittest.TestCase):
    """回归：redis_password() 曾站在每条 Redis 命令的热路径上，每次都做一遍
    「带全局锁的 SELECT + 读整份 /etc/redis/redis.conf」。改成带 TTL 的进程内缓存后，
    要锁死两件事：热路径不重复读盘，而改口令必须立刻生效（不能等缓存过期）。
    """

    def setUp(self):
        from app.services import pg_service

        self.pg = pg_service
        pg_service.forget_redis_password()
        self.addCleanup(pg_service.forget_redis_password)
        self.addCleanup(dbm.set_setting, "redis_password", "")

    def _counting(self, value):
        hits = []

        def fake():
            hits.append(1)
            return value

        return hits, fake

    def test_hot_path_reads_once(self):
        hits, fake = self._counting("pw")
        with mock.patch.object(self.pg, "_read_redis_password", fake):
            for _ in range(20):
                self.assertEqual(self.pg.redis_password(), "pw")
        self.assertEqual(len(hits), 1, "20 条命令不该读 20 次盘")

    def test_unset_password_is_also_cached(self):
        # 没配口令才是最常见状态：若把 None 当"没查过"，每条命令还是会落到盘上。
        hits, fake = self._counting(None)
        with mock.patch.object(self.pg, "_read_redis_password", fake):
            for _ in range(5):
                self.assertIsNone(self.pg.redis_password())
        self.assertEqual(len(hits), 1)

    def test_expired_cache_rereads(self):
        hits, fake = self._counting("pw")
        with mock.patch.object(self.pg, "_read_redis_password", fake):
            self.pg.redis_password()
            # 运维手工改了 redis.conf 时没有任何进程内事件可依，只能靠 TTL 到点重读。
            self.pg._redis_pw_cache = (
                self.pg.time.monotonic() - self.pg._REDIS_PW_TTL - 1, "pw")
            self.pg.redis_password()
        self.assertEqual(len(hits), 2)

    def test_set_invalidates_immediately(self):
        dbm.set_setting("redis_password", "old-pw")
        self.assertEqual(self.pg.redis_password(), "old-pw")
        self.pg.set_redis_password("new-pw")
        self.assertEqual(self.pg.redis_password(), "new-pw", "改完口令必须立刻生效")


class TestTerminalSessionAccounting(unittest.IsolatedAsyncioTestCase):
    """回归：accept() 抛异常时额度不归还，终端会被永久占满。"""

    def _ws(self, role="admin", fail_accept=False):
        class FakeWS:
            scope = {"type": "websocket", "state": {"cp_role": role, "cp_sub": "t"}}

            def __init__(self):
                self.closed = None

            async def accept(self):
                # 客户端在握手中途断开时，真实 EventSource 就是这个表现
                if fail_accept:
                    raise RuntimeError("client went away")

            async def close(self, code=None, reason=""):
                self.closed = code

        return FakeWS()

    async def test_counter_released_when_accept_fails(self):
        from app.routers import terminal

        before = terminal._SESSIONS
        with self.assertRaises(RuntimeError):
            await terminal.terminal(self._ws(fail_accept=True))
        self.assertEqual(terminal._SESSIONS, before, "握手中途失败也必须归还额度")

    async def test_no_close_audit_for_session_that_never_opened(self):
        from app.routers import terminal

        before = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("terminal:close",))["c"]
        with self.assertRaises(RuntimeError):
            await terminal.terminal(self._ws(fail_accept=True))
        self.assertEqual(
            dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("terminal:close",))["c"],
            before,
            "没建立过的会话不该在审计里留下一次关闭",
        )

    async def test_viewer_closed_without_session(self):
        from app.routers import terminal

        before = terminal._SESSIONS
        ws = self._ws(role="viewer")
        await terminal.terminal(ws)
        self.assertEqual(ws.closed, 4403)
        self.assertEqual(terminal._SESSIONS, before)

    def test_resize_frame_is_validated(self):
        from app.routers import terminal

        self.assertEqual(terminal._dim("abc", 100, 400), 100)
        self.assertEqual(terminal._dim(-5, 28, 200), 28)
        self.assertEqual(terminal._dim(9999, 100, 400), 400)
        self.assertEqual(terminal._dim(120, 100, 400), 120)


class TestPsqlPipesDoNotDeadlock(unittest.TestCase):
    """回归：psql 先写完 stdin 再读 stdout，长 SQL + 大输出时两边互等，只剩一句"psql 超时"。"""

    def test_stdin_write_and_stdout_read_overlap(self):
        events = []

        class Stdin:
            def write(self, _data):
                events.append("write")

            async def drain(self):
                # 真管道写满就阻塞，直到子进程开始读；这里用同一条件复现
                while "read" not in events:
                    await asyncio.sleep(0.01)
                events.append("drained")

            def close(self):
                events.append("closed")

        class Stdout:
            def __init__(self):
                self.sent = False

            async def read(self, _n):
                if self.sent:
                    return b""
                events.append("read")
                while "closed" not in events:  # 没拿到完整输入，子进程不会 EOF
                    await asyncio.sleep(0.01)
                self.sent = True
                return b"1 row\n"

        class Stderr:
            async def read(self, _n):
                return b""

        class Proc:
            def __init__(self):
                self.pid = 4242
                self.returncode = 0
                self.stdin = Stdin()
                self.stdout = Stdout()
                self.stderr = Stderr()

            async def wait(self):
                return 0

        async def fake_exec(*_a, **_kw):
            return Proc()

        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            out = asyncio.run(pg_service.psql("SELECT 1"))

        self.assertEqual(out, "1 row")
        self.assertLess(
            events.index("read"), events.index("drained"),
            "读 stdout 必须与写 stdin 并发；旧写法串行时这里根本走不到",
        )


class TestDoctorCoversGlobalCli(unittest.IsolatedAsyncioTestCase):
    """回归：AGENTS.md 每条命令都写 choyeonctl，但入口缺失/悬空时自检仍然全绿。"""

    def _setup(self, d: str):
        Path(d, "bin").mkdir(parents=True, exist_ok=True)
        Path(d, "bin", "choyeonctl").write_text("#!/usr/bin/env bash\n")
        return Path(d, "bin", "choyeonctl")

    async def _check(self, link: Path, root: str):
        from app.routers import meta

        target = self._setup(root)
        with mock.patch.object(meta, "CLI_LINK", link), \
             mock.patch.object(meta.os, "geteuid", return_value=0), \
             mock.patch.object(meta.config, "BASE", root):
            res = await meta._check_cli()
        self.assertTrue(res["detail"])
        self.assertTrue(target.exists())
        return res

    async def test_missing_link_warns(self):
        d = tempfile.mkdtemp(prefix="cp_doc_cli_")
        self.addCleanup(shutil.rmtree, d, True)
        res = await self._check(Path(d, "choyeonctl"), d)
        self.assertEqual(res["status"], "warn", "文档入口不存在时不能报 pass")
        self.assertIn("ln -sf", res["fix"])

    async def test_dangling_link_fails(self):
        d = tempfile.mkdtemp(prefix="cp_doc_cli2_")
        self.addCleanup(shutil.rmtree, d, True)
        link = Path(d, "choyeonctl")
        link.symlink_to(Path(d, "removed-target"))
        res = await self._check(link, d)
        self.assertEqual(res["status"], "fail", "悬空软链接比缺失更误导，必须报 fail")

    async def test_good_link_passes(self):
        d = tempfile.mkdtemp(prefix="cp_doc_cli3_")
        self.addCleanup(shutil.rmtree, d, True)
        link = Path(d, "choyeonctl")
        link.symlink_to(Path(d, "bin", "choyeonctl"))
        res = await self._check(link, d)
        self.assertEqual(res["status"], "pass")

    async def test_dev_environment_skipped(self):
        d = tempfile.mkdtemp(prefix="cp_doc_cli4_")
        self.addCleanup(shutil.rmtree, d, True)
        from app.routers import meta

        with mock.patch.object(meta, "CLI_LINK", Path(d, "choyeonctl")), \
             mock.patch.object(meta.os, "geteuid", return_value=1000), \
             mock.patch.object(meta.config, "BASE", d):
            res = await meta._check_cli()
        self.assertEqual(res["status"], "pass", "普通开发者机器不该因为没装全局 CLI 被报错")


class TestDropdownActionsWired(unittest.TestCase):
    """回归：naive-ui 的 DropdownOption 上没有 onClick，多写的键会被静默丢掉，
    菜单项点开什么也不发生（顶栏「修改密码/退出登录」曾整组失效），
    而 vue-tsc 也不会报错——TreeOption 带索引签名。动作只能挂在 NDropdown 的 @select 上。
    """

    WEB_SRC = BACKEND.parent / "web" / "src"

    def test_every_dropdown_listens_for_select(self):
        missing = []
        # `@select="fn"` 与 `:on-select="fn"` 都算接上：naive-ui 的 Dropdown.mjs 里
        # onSelect 是声明过的 prop，并在选中时 `if (onSelect) call(onSelect, key, node)`，
        # 所以两种写法等价；漏掉后一种会把已经能用的菜单判成坏的。
        wired = re.compile(r"(@|:|v-on:)on?[-_]?select", re.I)
        for f in sorted(self.WEB_SRC.rglob("*.vue")):
            text = f.read_text(encoding="utf-8")
            for tag in re.finditer(r"<NDropdown\b[^>]*>", text, re.S):
                if not wired.search(tag.group(0)):
                    line = text[: tag.start()].count("\n") + 1
                    missing.append(f"{f.relative_to(self.WEB_SRC.parent)}:{line}")
        self.assertEqual(missing, [], f"以下 NDropdown 没有 select 处理函数，菜单项点了不会有任何动作：{missing}")

    def test_option_lists_do_not_carry_onClick(self):
        # 只查传给 NDropdown 的那类选项字面量：`{ label: '…', key: '…', onClick: … }`
        bad = []
        for f in sorted(self.WEB_SRC.rglob("*.vue")):
            for m in re.finditer(r"\{\s*label:\s*[^{}]*onClick:", f.read_text(encoding="utf-8")):
                line = m.string[: m.start()].count("\n") + 1
                bad.append(f"{f.relative_to(self.WEB_SRC.parent)}:{line}")
        self.assertEqual(bad, [], f"下拉选项上的 onClick 不生效，请改用 NDropdown @select：{bad}")


class TestScryptOffLoop(unittest.IsolatedAsyncioTestCase):
    """口令哈希必须既不出现在事件循环上、也不无限并行。

    两个方向都真坏过：
    - 直接在 async 里算 scrypt：两三人同时登录把整站轮询拖死；
    - 无脑扔线程池：每次 scrypt 要 32MB 临时内存，实测 6 并发把 cgroup 顶到 108MB。
    """

    async def test_capped_concurrency_and_free_loop(self):
        import statistics
        import time

        from app import security as sec

        HASH_SEC = 0.15
        inflight = []
        peak = []

        def slow_hash(pw):
            inflight.append(1)
            peak.append(len(inflight))
            time.sleep(HASH_SEC)
            inflight.pop()
            return f"fake:{pw}"

        ticks = []

        async def ticker(stop):
            prev = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                ticks.append(now - prev)
                prev = now

        stop = asyncio.Event()
        tick_task = asyncio.create_task(ticker(stop))
        with mock.patch.object(sec, "hash_pass", slow_hash):
            await asyncio.gather(*(sec.hash_pass_async(f"pw-{i}") for i in range(8)))
        stop.set()
        await tick_task

        self.assertLessEqual(max(peak), sec._SCRYPT_CONCURRENCY, "scrypt 并发没压住，内存会被顶飞")
        # 「最大间隔 < 60ms」这种写法量的是机器不是代码：全量跑起来同机负载能把某一次
        # 5ms 的 sleep 顶到 62ms（2026-10-14 全量跑到这条红过一次，单独跑永远绿），
        # 而这跟哈希在不在循环上毫无关系。改成看中位数：哈希若在循环上算，每 150ms
        # 才轮得到 ticker 一次，中位数直接落在 HASH_SEC 量级，负载怎么抖都翻不回来。
        median = statistics.median(ticks)
        self.assertLess(median, HASH_SEC / 4, "哈希仍在事件循环上跑")
        self.assertGreater(len(ticks), 20, "ticker 没跑起来，这条断言就是空转")

    def test_sync_helpers_still_available(self):
        # CLI（choyeonctl user create）是同步进程，走的是 hash_pass/check_pass
        from app import security as sec

        stored = sec.hash_pass("abcdefghijkl")
        self.assertTrue(sec.check_pass("abcdefghijkl", stored))
        self.assertFalse(sec.check_pass("abcabcdefghijk", stored))


class TestUnitFilesState(unittest.IsolatedAsyncioTestCase):
    """回归：服务管理页的「自启」按钮原先没有任何状态来源。

    `systemctl list-units` 输出的五列是 UNIT/LOAD/ACTIVE/SUB/DESCRIPTION，**不含**
    开机自启状态；前端那个按钮既显示不了当前是否已启用，enable/disable 之后也读不回来。
    补上的 unit_files() 要锁死四件事：解析对齐、整表一次调用（带 TTL 复用）、
    空结果不被当成缓存、systemctl 失败时沿用上一份而不是显示成「全部未启用」；
    外加接口写完必须把新状态回显给前端（否则按钮要点两次才变）。
    """

    LIST = (
        "choyeon-panel.service           enabled    enabled\n"
        "nginx.service                   disabled   disabled\n"
        "serial-getty@ttyS0.service      indirect   disabled\n"
        "systemd-resolved.service        static     enabled\n"
    )

    def setUp(self):
        from app.services import systemd_ops

        self.sys = systemd_ops
        self.addCleanup(setattr, systemd_ops, "_uf_cache", (0.0, {}))
        systemd_ops._uf_cache = (0.0, {})

    def _fake_run(self, *, uf_out=None, uf_code=0):
        calls = []

        async def run(cmd, args, cwd=None, timeout=120, env=None):
            calls.append(list(args))
            if args and args[0] == "list-unit-files":
                return {"code": uf_code, "out": self.LIST if uf_out is None else uf_out}
            return {"code": 0, "out": ""}

        return calls, run

    async def test_parsed_and_reused_inside_ttl(self):
        calls, run = self._fake_run()
        with mock.patch.object(self.sys, "run", run):
            first = await self.sys.unit_files()
            for _ in range(5):
                second = await self.sys.unit_files()
        self.assertEqual(calls.count(["list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"]), 1,
                         "6 次读取只该起 1 个 systemctl；逐行 is-enabled 在 15s 刷新周期里会打满进程")
        self.assertIs(second, first)
        self.assertEqual(first["choyeon-panel.service"], "enabled")
        # STATE 是第二个字段，PRESET 在后面：按空白切列时不能把 preset 读成 state
        self.assertEqual(first["nginx.service"], "disabled")
        self.assertEqual(first["systemd-resolved.service"], "static")

    async def test_instance_names_survive(self):
        calls, run = self._fake_run()
        with mock.patch.object(self.sys, "run", run):
            d = await self.sys.unit_files()
        self.assertEqual(d["serial-getty@ttyS0.service"], "indirect", "@ 实例名被切成两半就查不到状态")

    async def test_empty_result_is_not_cached(self):
        """code=0 但没输出（unit 文件表为空/被截断）不能被当作一份可用缓存。"""
        calls, run = self._fake_run(uf_out="")
        with mock.patch.object(self.sys, "run", run):
            await self.sys.unit_files()
            await self.sys.unit_files()
        self.assertEqual(len([c for c in calls if c[0] == "list-unit-files"]), 2)

    async def test_failure_keeps_previous_snapshot(self):
        _, run = self._fake_run()
        with mock.patch.object(self.sys, "run", run):
            good = await self.sys.unit_files()
        self.assertEqual(good["choyeon-panel.service"], "enabled")
        _, fail = self._fake_run(uf_code=1)
        with mock.patch.object(self.sys, "run", fail):
            again = await self.sys.unit_files()
        self.assertEqual(again, good, "systemctl 失败时把全部服务显示成「未启用」比停留在旧状态更糟")

    async def test_force_rereads_after_a_write(self):
        calls, run = self._fake_run()
        with mock.patch.object(self.sys, "run", run):
            await self.sys.unit_files()
            _, after = self._fake_run(uf_out="choyeon-panel.service  disabled  disabled\n")
            with mock.patch.object(self.sys, "run", after):
                fresh = await self.sys.unit_files(force=True)
        self.assertEqual(fresh["choyeon-panel.service"], "disabled")
        self.assertEqual(len(calls), 1, "force=True 不该真的回源过一次以上")

    async def test_endpoint_returns_mapping(self):
        from app.routers import system as system_router

        _, run = self._fake_run()
        with mock.patch.object(self.sys, "run", run):
            self.assertEqual((await system_router.unit_files())["nginx.service"], "disabled")

    async def test_enable_response_echoes_new_state(self):
        from app.routers import system as system_router

        calls, run = self._fake_run(uf_out="choyeon-panel.service  enabled  enabled\n")

        async def act(unit, verb):
            return ""

        async def inactive(unit):
            return False

        with (
            mock.patch.object(self.sys, "run", run),
            mock.patch.object(self.sys, "service_action", act),
            mock.patch.object(self.sys, "is_active", inactive),
        ):
            out = await system_router.service_action(
                "choyeon-panel.service",
                _req(body={"verb": "enable"}, headers={"content-type": "application/json"}),
            )
        self.assertEqual(out.get("enabled"), "enabled", "前端点完「自启」要当场变状态，不能等 30s 缓存过期")
        self.assertTrue(out["ok"])
        self.assertEqual([c for c in calls if c[0] == "list-unit-files"], [["list-unit-files", "--type=service", "--no-legend", "--plain", "--no-pager"]])

    async def test_restart_response_has_no_enabled(self):
        from app.routers import system as system_router

        calls, run = self._fake_run()

        async def act(unit, verb):
            return ""

        async def active(unit):
            return True

        with (
            mock.patch.object(self.sys, "run", run),
            mock.patch.object(self.sys, "service_action", act),
            mock.patch.object(self.sys, "is_active", active),
        ):
            out = await system_router.service_action("nginx.service", _req(body={"verb": "restart"}))
        self.assertNotIn("enabled", out, "restart 不改自启，回读一次 list-unit-files 是白起一个进程")
        self.assertEqual([c for c in calls if c[0] == "list-unit-files"], [])


if __name__ == "__main__":
    unittest.main()
