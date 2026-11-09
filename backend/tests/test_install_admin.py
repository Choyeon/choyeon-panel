"""批次117：全新安装的初始化 / 建号 / 首次登录链路守卫。

这一层把「零用户态」相关的三处约定钉死：
1. install.sh 必须在服务第一次启动之前把管理员建好，且口令只经 stdin；
2. /api/auth/setup 只能被本机（或同机反代）调用，公网直连抢注要被拒；
3. 初始化与登录不共用限流桶，否则「首次创建时填错几次」会把紧接着的登录判成 429。
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "scripts" / "install.sh"


def _load_main():
    """app.main 在 import 期依赖 fcntl，Windows 上跑不了；本地只跳过这些用例。"""
    try:
        from app import main
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - Windows
        raise unittest.SkipTest(f"后端需要 Unix 运行时：{exc}") from exc
    return main


class TestInstallScriptAdminFlow(unittest.TestCase):
    """安装脚本：语法、顺序与凭据处理。"""

    def test_shell_syntax(self):
        if os.name != "posix":
            self.skipTest("bash -n 需要 Unix shell")
        r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_admin_created_before_service_starts(self):
        s = INSTALL.read_text(encoding="utf-8")
        self.assertIn("create_admin_now", s)
        # 服务起来之前必须先建号：晚一步就等于把免认证 setup 端点暴露在公网。
        self.assertLess(s.index("create_admin_now"), s.index("unit_restart"))

    def test_credentials_never_touch_argv_or_history(self):
        s = INSTALL.read_text(encoding="utf-8")
        self.assertIn("--password-stdin", s)
        self.assertIn("read -rs", s, "口令必须静默读取（-s）")
        self.assertIn("printf '%s\\n' \"$pw\" |", s, "口令只经管道交给 CLI")
        self.assertNotIn('--password "$pw"', s)
        self.assertNotIn('sqlite3 "$DATA_DIR/panel.db"', s, "旧写法缺 sqlite3 时会把已有账号误判成 0")

    def test_validation_and_skip_branches(self):
        s = INSTALL.read_text(encoding="utf-8")
        for needle in (
            "valid_admin_name",
            "valid_admin_password",
            "${#1}",
            "8-128",
            "两次输入的密码不一致",
            "CP_ADMIN_PASSWORD_FILE",
            "CP_ADMIN_USER",
            "ADMIN_STATE",
            "数据库已有账号，跳过创建",
            "尚未创建管理员",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, s)

    def test_env_example_documents_the_new_switch(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("CP_SETUP_ALLOW_REMOTE", example)
        # CP_LOG 曾经被写成「错误响应体回显异常详情」，与 errors.public_message 的实际行为相反。
        self.assertIn("不会把异常详情回显到响应体", example)


class TestSetupPeerGuard(unittest.TestCase):
    """/api/auth/setup 的本机门槛。"""

    @staticmethod
    def _req(host: str):
        from starlette.requests import Request

        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/setup",
                "scheme": "http",
                "server": ("testserver", 80),
                "client": (host, 43210),
                "query_string": b"",
                "headers": [],
            }
        )

    def test_loopback_and_testclient_are_allowed(self):
        main = _load_main()
        with mock.patch.object(main.config, "SETUP_ALLOW_REMOTE", False):
            for host in ("127.0.0.1", "127.0.0.2", "::1", "testclient", ""):
                with self.subTest(host=host):
                    self.assertTrue(main._setup_peer_allowed(self._req(host)))

    def test_remote_peer_is_rejected_by_default(self):
        main = _load_main()
        with mock.patch.object(main.config, "SETUP_ALLOW_REMOTE", False):
            for host in ("203.0.113.9", "10.0.0.5", "not-an-ip"):
                with self.subTest(host=host):
                    self.assertFalse(main._setup_peer_allowed(self._req(host)))

    def test_explicit_opt_in_opens_remote_setup(self):
        main = _load_main()
        with mock.patch.object(main.config, "SETUP_ALLOW_REMOTE", True):
            self.assertTrue(main._setup_peer_allowed(self._req("203.0.113.9")))

    def test_remote_setup_returns_403_over_http(self):
        """走真实 ASGI 栈：公网对端连数据库都不该碰到。"""
        from starlette.testclient import TestClient

        main = _load_main()
        with mock.patch.object(main.config, "SETUP_ALLOW_REMOTE", False):
            client = TestClient(main.app, client=("203.0.113.9", 43210))
            r = client.post("/api/auth/setup", json={"username": "newadmin", "password": "Password123"})
            self.assertEqual(r.status_code, 403, r.text)
            self.assertIn("只能从本机", r.json()["error"])

    def test_setup_uses_its_own_rate_bucket(self):
        src = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        setup_src = src[src.index("async def setup(req: Request):"):src.index("async def login(req: Request):")]
        self.assertIn('bucket = f"setup:{ip}"', setup_src)
        self.assertIn("security.rate_limited(bucket)", setup_src)
        self.assertIn("security.clear_attempts(bucket)", setup_src)
        # 远端请求连限流桶都不该占，更不该让它试探出「库里还有没有账号」。
        self.assertLess(setup_src.index("_setup_peer_allowed(req)"), setup_src.index("rate_limited"))
        self.assertLess(setup_src.index("_setup_peer_allowed(req)"), setup_src.index("COUNT(*) c FROM users"))


if __name__ == "__main__":
    unittest.main()
