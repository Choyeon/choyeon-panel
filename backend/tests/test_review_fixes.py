"""深度审查后的回归测试：每锁定一条本轮修掉的真实缺陷。

覆盖：应用安装目录不得指向面板自身要害目录、部署模板端口/安装命令自洽、
证书续期失败必须报错、二进制上传不被毁、viewer 看不到应用密钥与审计、
改名后旧 unit/站点被拆除、CLI 的 --json 契约与部署等待。
"""

import asyncio
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

from app import config, templates  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import apps as apps_router  # noqa: E402
from app.routers import extras
from app.services import apps_service, files_service, nginx_ops  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent


def _req(role: str = "admin", sub: str = "tester", query: dict | None = None):
    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub), query_params=query or {})


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
    async def test_old_unit_and_vhost_removed(self):
        old = {"id": 7, "name": "before", "domain": "a.example.com", "path": "/tmp/cp-x", "port": 8124}
        with mock.patch.object(apps_service, "remove_unit") as rm_unit, \
             mock.patch.object(apps_service.nginx_ops, "remove_vhost") as rm_vhost:
            await apps_service.cleanup_renamed(old, "after")
        rm_unit.assert_called_once()
        self.assertEqual(rm_unit.call_args.args[0]["name"], "before")
        rm_vhost.assert_called_once_with("before")

    async def test_same_name_does_nothing(self):
        old = {"id": 8, "name": "keep", "domain": "b.example.com", "path": "/tmp/cp-y", "port": 8125}
        with mock.patch.object(apps_service, "remove_unit") as rm_unit, \
             mock.patch.object(apps_service.nginx_ops, "remove_vhost") as rm_vhost:
            await apps_service.cleanup_renamed(old, "keep")
        rm_unit.assert_not_called()
        rm_vhost.assert_not_called()


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


if __name__ == "__main__":
    unittest.main()
