import os
import tempfile
import unittest

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
        with self.assertRaises(Exception):
            security.verify_token(tok + "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
