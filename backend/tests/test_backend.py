import os
import re
import tempfile
import unittest
from pathlib import Path

import jwt

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))

from app import security  # noqa: E402
from app.services import apps_service, nginx_ops  # noqa: E402
from app.services.files_service import safe_path  # noqa: E402
from app.util import is_domain, is_git_url, is_ident, is_name, is_port, is_unit, sh_escape  # noqa: E402

APP = {
    "id": 1,
    "name": "demo-app",
    "type": "node",
    "path": "/root/www/demo-app",
    "port": 3000,
    "start_cmd": 'node "server.js" --flag',
    "unit_template": None,
    "unit_override": None,
    "env": "[]",
}


class TestValidators(unittest.TestCase):
    def test_name(self):
        self.assertTrue(is_name("app1"))
        self.assertFalse(is_name("App"))
        self.assertFalse(is_name("1app"))
        self.assertFalse(is_name("a_b"))

    def test_domain(self):
        self.assertTrue(is_domain("blog.example.com"))
        self.assertFalse(is_domain("example"))
        self.assertFalse(is_domain("-bad.com"))

    def test_port(self):
        self.assertTrue(is_port(1024))
        self.assertTrue(is_port(65535))
        self.assertFalse(is_port(80))
        self.assertFalse(is_port("3000"))

    def test_unit_ident_giturl(self):
        self.assertTrue(is_unit("nginx.service"))
        self.assertTrue(is_unit("panel-demo-app.service"))
        self.assertTrue(is_ident("rosetta"))
        self.assertFalse(is_ident("Ro-setta"))
        self.assertTrue(is_git_url("https://github.com/a/b.git"))
        self.assertTrue(is_git_url("git@github.com:a/b.git"))
        self.assertTrue(is_git_url("/srv/local-repo"))
        self.assertFalse(is_git_url("ssh://bad with space"))


class TestShEscape(unittest.TestCase):
    def test_quotes(self):
        self.assertEqual(sh_escape("a b"), "'a b'")
        self.assertEqual(sh_escape("it's"), r"'it'\''s'")


class TestUnitRender(unittest.TestCase):
    def test_default_unit(self):
        out = apps_service.render_unit(dict(APP))
        self.assertIn("WorkingDirectory=/root/www/demo-app", out)
        self.assertIn('ExecStart=/bin/bash -lc "node \\"server.js\\" --flag"', out)
        self.assertIn("EnvironmentFile=-/root/www/demo-app/.panel.env", out)

    def test_template(self):
        tpl = "[Service]\nExecStart=/bin/bash -lc {{start_cmd}}\nEnvironment=PORT={{port}}\nWorkingDirectory={{path}}\n"
        out = apps_service.render_unit_from(tpl, dict(APP))
        self.assertIn("PORT=3000", out)
        self.assertIn('WorkingDirectory=/root/www/demo-app', out)
        self.assertIn('bash -lc "node \\"server.js\\" --flag"', out)

    def test_template_empty_port(self):
        out = apps_service.render_unit_from("P={{port}}", {**APP, "port": None})
        self.assertEqual(out, "P=")


class TestNginxAnalyze(unittest.TestCase):
    DIRECT = """
server {
    listen 443 ssl;
    server_name a.example.com _;
    client_max_body_size 50m;
    ssl_certificate /x;
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Upgrade $http_upgrade;
    }
}
"""
    UPSTREAM = """
upstream be {
    server 127.0.0.1:8000;
}
server {
    server_name b.example.com;
    location / { proxy_pass http://be; }
}
"""

    def test_direct(self):
        a = nginx_ops.analyze_config(self.DIRECT)
        self.assertEqual(a["serverNames"], ["a.example.com"])
        self.assertTrue(a["ssl"])
        self.assertTrue(a["websocket"])
        self.assertEqual(a["bodySize"], "50m")
        self.assertEqual(a["proxyPorts"], [3000])

    def test_upstream(self):
        a = nginx_ops.analyze_config(self.UPSTREAM)
        self.assertEqual(a["proxyPorts"], [8000])

    def test_no_false_port_from_hostname(self):
        a = nginx_ops.analyze_config("proxy_pass http://upstream1234;")
        self.assertEqual(a["proxyPorts"], [])


class TestSafePath(unittest.TestCase):
    def test_allowed(self):
        self.assertTrue(safe_path("/root/www/anything").startswith("/root/www/"))

    def test_escape_denied(self):
        with self.assertRaises(RuntimeError):
            safe_path("/etc/passwd")
        with self.assertRaises(RuntimeError):
            safe_path("/root/www/../../etc/shadow")


class TestAuth(unittest.TestCase):
    def test_scrypt_roundtrip(self):
        h = security.hash_pass("secret123")
        self.assertTrue(security.check_pass("secret123", h))
        self.assertFalse(security.check_pass("secret123 ", h))

    def test_jwt(self):
        tok = security.sign_token("alice", "viewer")
        payload = security.verify_token(tok)
        self.assertEqual(payload["sub"], "alice")
        self.assertEqual(payload["role"], "viewer")
        # 签名被篡改必须抛具体的 JWT 异常，而不是任意 Exception
        with self.assertRaises(jwt.InvalidTokenError):
            security.verify_token(tok + "x")


class TestDependencyWiring(unittest.TestCase):
    """依赖清单的接线守卫。

    存在的理由：push e47e829 的 CI 后端任务挂了 6 个 error，报的是 starlette
    testclient "requires the httpx2 package"，而本地全绿 —— 因为本地 venv 里躺着
    旧 httpx（starlette 1.7 只是 deprecation warning），CI 的干净环境里是硬
    RuntimeError。这类「本地绿 ≠ CI 绿」不能靠人记住，要有守卫。
    """

    BASE = Path(__file__).resolve().parents[1]  # backend/
    REPO = BASE.parent  # 仓库根，与 CI 的 checkout 一致

    @staticmethod
    def _canon(name):
        # PEP 503 规范化：PyJWT == pyjwt == Py_Jwt
        return re.sub(r"[-_.]+", "-", name).lower()

    @classmethod
    def _requirements(cls, relpath):
        """把 backend/ 下的 requirements 文件解析成 {规范包名: specifier}，
        跳过注释与空行；无法解析的行直接报错，不让守卫悄悄变成空集合。"""
        out = {}
        text = (cls.BASE / relpath).read_text(encoding="utf-8")
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", line)
            if not m:
                raise AssertionError(f"{relpath} 里出现无法解析的依赖行：{raw!r}")
            out[cls._canon(m.group(1))] = re.sub(r"\s+", "", m.group(2))
        return out

    def test_parser_ignores_comments_and_normalizes(self):
        # 非空断言：解析器不是「怎么喂都返回同一结果」的摆设
        self.assertEqual(self._canon("Py_JWT"), "py-jwt")
        prod = self._requirements("requirements.txt")
        self.assertEqual(prod["fastapi"], ">=0.115")
        # 顶部注释行不会被当成依赖（requirements.txt 前 2 行是 # 开头）
        self.assertNotIn("# choyeon-panel 后端运行时依赖", prod)

    def test_requirements_txt_matches_pyproject(self):
        """requirements.txt 顶部声明「与 pyproject 的 [project].dependencies 一致」，
        以前只是注释，没人验证 —— 少一行 CI 也照样绿，直到运行时缺包才炸。"""
        pyproject = (self.BASE / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r"dependencies\s*=\s*\[(.*?)\]", pyproject, re.S)
        self.assertIsNotNone(m, "pyproject.toml 里找不到 [project].dependencies")
        declared = {}
        for item in re.findall(r'"([^"]+)"', m.group(1)):
            mm = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", item)
            declared[self._canon(mm.group(1))] = re.sub(r"\s+", "", mm.group(2))
        self.assertEqual(declared, self._requirements("requirements.txt"))

    def test_httpx2_is_dev_only_and_ci_installs_it(self):
        """守卫 CI 的测试依赖接线：httpx2 声明在 requirements-dev.txt、不在生产
        requirements.txt，且 ci.yml 真的安装它、pip 缓存 key 也包含它。"""
        self.assertIn(
            "httpx2",
            self._requirements("requirements-dev.txt"),
            "requirements-dev.txt 必须声明 httpx2（starlette TestClient 的硬依赖）",
        )
        self.assertNotIn(
            "httpx2",
            self._requirements("requirements.txt"),
            "httpx2 只是测试依赖，不能进生产 requirements.txt",
        )
        ci = (self.REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertRegex(
            ci,
            r"pip install [^\n]*-r\s+requirements-dev\.txt",
            "ci.yml 必须安装 requirements-dev.txt，否则后端任务会在 TestClient 导入处炸 6 个 error",
        )
        self.assertRegex(
            ci,
            r"backend/requirements-dev\.txt",
            "pip 缓存 key 必须包含 requirements-dev.txt，不然改了它 CI 还吃旧缓存",
        )

    def test_testclient_import_actually_works(self):
        """哨兵：当前环境里 TestClient 真能 import。依赖没装好时这条先响，
        而不是让那 6 个用 lazy import 的用例各炸一次。"""
        from starlette.testclient import TestClient  # noqa: F401


if __name__ == "__main__":
    unittest.main(verbosity=2)
