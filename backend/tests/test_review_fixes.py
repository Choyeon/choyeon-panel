"""深度审查后的回归测试：每锁定一条本轮修掉的真实缺陷。

覆盖：应用安装目录不得指向面板自身要害目录、部署模板端口/安装命令自洽、
证书续期失败必须报错、二进制上传不被毁、viewer 看不到应用密钥与审计、
改名后旧 unit/站点被拆除并按新名重建、仓库地址里的凭据不外泄、备份目标应用被删后要在列表里露出来、
公开状态接口不回显管理员用户名、CLI 的 --json 契约与部署等待、
文件接口对 viewer 全面关闭、写入不跟随符号链接、审计字段长度受约束、
终端会话计数不漏、psql 管道不互等，以及接口错误不留半成品变更。
"""

import asyncio
import itertools
import json
import os
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
from app.services import apps_service, backup_service, files_service, nginx_ops, pg_service  # noqa: E402
from app.util import redact_creds  # noqa: E402

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

        with mock.patch.object(nginx_ops, "run", fake_run):
            self.assertIn("renewal", asyncio.run(nginx_ops.renew_certs()))

    def test_route_returns_400_and_no_audit_on_failure(self):
        before = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("certbot:renew",))["c"]
        with mock.patch.object(nginx_ops, "renew_certs", side_effect=RuntimeError("boom")):
            resp = asyncio.run(extras.certbot_renew(_req()))
        self.assertEqual(resp.status_code, 400)
        after = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE action=?", ("certbot:renew",))["c"]
        self.assertEqual(after, before, "失败的续期不该留下一条已执行审计")


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


class TestRenameCleansStaleUnit(unittest.IsolatedAsyncioTestCase):
    """改名必须"拆旧 + 建新"成对发生。

    只拆不建：旧 unit disable --now 停掉、新名字的 unit 还没写，服务当场消失、
    域名 502，而 PATCH 只回一句"已保存"。
    """

    OLD = {"id": 7, "name": "before", "domain": "a.example.com", "path": "/tmp/cp-x", "port": 8124}

    async def _run(self, new_name, active):
        new = {**self.OLD, "id": 7, "name": new_name, "path": "/tmp/cp-x", "port": 8124,
               "domain": "a.example.com", "start_cmd": "node x.js", "type": "node"}
        with mock.patch.object(apps_service, "is_active", lambda *_: asyncio.sleep(0, active)), \
             mock.patch.object(apps_service, "remove_unit") as rm_unit, \
             mock.patch.object(apps_service, "ensure_unit") as en_unit, \
             mock.patch.object(apps_service, "service_action") as act, \
             mock.patch.object(apps_service.nginx_ops, "remove_vhost") as rm_vhost, \
             mock.patch.object(apps_service.nginx_ops, "apply_vhost") as ap_vhost:
            await apps_service.cleanup_renamed(self.OLD, new)
        return rm_unit, en_unit, rm_vhost, ap_vhost, act

    async def test_old_unit_and_vhost_removed(self):
        rm_unit, _, rm_vhost, _, _ = await self._run("after", True)
        rm_unit.assert_called_once()
        self.assertEqual(rm_unit.call_args.args[0]["name"], "before")
        rm_vhost.assert_called_once_with("before")

    async def test_new_unit_and_vhost_rebuilt(self):
        _, en_unit, _, ap_vhost, act = await self._run("after", True)
        en_unit.assert_called_once()
        self.assertEqual(en_unit.call_args.args[0]["name"], "after")
        ap_vhost.assert_called_once()
        self.assertEqual(ap_vhost.call_args.args[0]["name"], "after")
        # 改名前在跑 → 新名字得接着跑
        act.assert_called_once_with("panel-after.service", "restart")

    async def test_inactive_app_is_not_started_by_rename(self):
        _, en_unit, _, _, act = await self._run("after", False)
        en_unit.assert_called_once()
        act.assert_not_called()

    async def test_same_name_does_nothing(self):
        rm_unit, en_unit, rm_vhost, ap_vhost, act = await self._run(self.OLD["name"], True)
        rm_unit.assert_not_called()
        rm_vhost.assert_not_called()
        en_unit.assert_not_called()
        ap_vhost.assert_not_called()
        act.assert_not_called()


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
        with mock.patch.object(apps_service.systemd_ops, "is_active",
                               lambda *_: asyncio.sleep(0, False)), \
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

    async def test_unknown_kind_rejected_before_touching_nginx(self):
        from app.routers import apps as apps_router

        with mock.patch.object(nginx_ops, "quick_edit_config") as edit:
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

        with mock.patch.object(nginx_ops, "quick_edit_config") as edit:
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
            async def read(self):
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


if __name__ == "__main__":
    unittest.main()
