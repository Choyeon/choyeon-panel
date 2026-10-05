"""针对本轮加固项的回归测试：配置解析、令牌吊销、登录限速、日志裁剪、路径与体积守卫。

说明：这些测试会写入临时 SQLite（CP_DATA_DIR 由 test_backend 或本文件设为临时目录），
不会触碰真实数据。命令执行相关的用例只在超时/权限校验层面验证，不做真实系统变更。
"""

import json
import os
import tempfile
import unittest
from unittest import mock

import jwt

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_roots_"))

from app import config, security  # noqa: E402
from app import database as dbm
from app.services import files_service, nginx_ops  # noqa: E402
from app.util import run, run_shell_async  # noqa: E402


# ---------------------------------------------------------------- 配置解析
class TestConfigParsers(unittest.TestCase):
    def test_int_default_and_clamp(self):
        self.assertEqual(config._int("CP_UNSET_VAR_XYZ", 7), 7)
        os.environ["CP_TEST_INT"] = "999"
        self.assertEqual(config._int("CP_TEST_INT", 7, 1, 10), 10, "超出最大值应被钳制")
        os.environ["CP_TEST_INT"] = "-5"
        self.assertEqual(config._int("CP_TEST_INT", 7, 1, 10), 1, "低于最小值应被钳制")

    def test_int_invalid_falls_back(self):
        os.environ["CP_TEST_INT"] = "not-a-number"
        self.assertEqual(config._int("CP_TEST_INT", 7), 7)
        os.environ["CP_TEST_INT"] = ""
        self.assertEqual(config._int("CP_TEST_INT", 7), 7, "空字符串应视为未设置")

    def test_bool_values(self):
        for raw, expected in [("1", True), ("0", False), ("false", False), ("no", False), ("", False)]:
            os.environ["CP_TEST_BOOL"] = raw
            self.assertEqual(config._bool("CP_TEST_BOOL"), expected, f"CP_TEST_BOOL={raw!r}")

    def test_paths_rejects_relative(self):
        os.environ["CP_TEST_PATHS"] = "/srv/a,relative/path,,/srv/b,/srv/a"
        got = config._paths("CP_TEST_PATHS", "/root/www")
        self.assertEqual(got, ["/srv/a", "/srv/b"], "相对路径应被丢弃且结果去重")

    def test_as_dict_leaks_no_secret(self):
        allowed = {
            "version", "host", "port", "trustProxy", "log", "dataDir", "appRoot",
            "backupDir", "fileRoots", "unitDir", "python", "webDist", "webBuilt",
            "tokenTtlHours",
        }
        self.assertEqual(set(config.as_dict()) - allowed, set(), "as_dict 出现了未登记的配置项")
        self.assertTrue(config.VERSION, "版本号不应为空")


# ---------------------------------------------------------------- 令牌吊销
class TestTokenRevocation(unittest.TestCase):
    def setUp(self):
        self.user = f"revoke_user_{os.getpid()}_{id(self)}"
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.user, security.hash_pass("Password123"), "viewer"),
        )

    def tearDown(self):
        dbm.execute("DELETE FROM users WHERE username=?", (self.user,))

    def test_bump_user_epoch_revokes_old_token(self):
        tok = security.sign_token(self.user, "viewer")
        self.assertEqual(security.verify_token(tok)["sub"], self.user)

        security.bump_user_epoch(self.user)
        with self.assertRaises(jwt.InvalidTokenError, msg="改密后旧 token 必须失效"):
            security.verify_token(tok)

        # 重新签发的 token 应当可用，证明只吊销了旧版本而非全局失效
        self.assertEqual(security.verify_token(security.sign_token(self.user, "viewer"))["sub"], self.user)

    def test_global_epoch_bump(self):
        a = dbm.token_epoch()
        b = dbm.bump_token_epoch()
        self.assertNotEqual(a, b)


# ---------------------------------------------------------------- 登录限速
class TestLoginRateLimit(unittest.TestCase):
    def setUp(self):
        self.ip = f"10.0.0.{id(self) % 250}"
        security.clear_attempts(self.ip)

    def tearDown(self):
        security.clear_attempts(self.ip)

    def test_limit_triggers(self):
        with mock.patch.object(config, "LOGIN_LIMIT", 2):
            self.assertFalse(security.rate_limited(self.ip), "第 1 次不应被限")
            self.assertFalse(security.rate_limited(self.ip), "第 2 次不应被限")
            self.assertTrue(security.rate_limited(self.ip), "第 3 次应触发限速")

    def test_clear_attempts(self):
        with mock.patch.object(config, "LOGIN_LIMIT", 1):
            self.assertFalse(security.rate_limited(self.ip))
            self.assertTrue(security.rate_limited(self.ip))
            security.clear_attempts(self.ip)
            self.assertFalse(security.rate_limited(self.ip), "成功后应清空计数")

    def test_window_expiry(self):
        with mock.patch.object(config, "LOGIN_LIMIT", 1):
            self.assertFalse(security.rate_limited(self.ip))
            self.assertTrue(security.rate_limited(self.ip))
            # 把记录时间戳推到窗口之外，模拟"一分钟后再试"
            security._attempts[self.ip][1] -= (config.LOGIN_WINDOW_SEC + 5)
            self.assertFalse(security.rate_limited(self.ip), "超出时间窗口后应重新计数")


# ---------------------------------------------------------------- 数据裁剪
class TestDataPruning(unittest.TestCase):
    def test_append_deploy_log_truncated(self):
        dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,?,?,?,?,?)",
            ("prune-app", "node", "https://x/y.git", "main", "/tmp/prune-app", "node index.js"),
        )
        app = dbm.query_one("SELECT id FROM apps WHERE name='prune-app'")
        dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app["id"],))
        dep = dbm.query_one("SELECT id FROM deployments WHERE app_id=? ORDER BY id DESC", (app["id"],))

        chunk = "x" * 4096
        for _ in range(100):  # 400KB > MAX_DEPLOY_LOG(256KB)
            dbm.append_deploy_log(dep["id"], chunk)

        row = dbm.query_one("SELECT log FROM deployments WHERE id=?", (dep["id"],))
        self.assertLessEqual(len(row["log"]), dbm.MAX_DEPLOY_LOG + 64, "部署日志必须被裁剪")
        self.assertIn("已截断", row["log"])

        dbm.execute("DELETE FROM deployments WHERE app_id=?", (app["id"],))
        dbm.execute("DELETE FROM apps WHERE id=?", (app["id"],))

    def test_prune_audit(self):
        for i in range(30):
            dbm.audit("prune-tester", f"action-{i}")
        dbm.prune_audit(keep=5)
        left = dbm.query_one("SELECT COUNT(*) c FROM audit WHERE username='prune-tester'")["c"]
        self.assertLessEqual(left, 5, "审计日志应按保留条数裁剪")


# ---------------------------------------------------------------- 文件守卫
class TestFileGuards(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cp_fs_")
        self.old_roots = files_service.ROOTS
        self.old_max = files_service.MAX_FILE_BYTES
        files_service.ROOTS = [self.root]
        files_service.MAX_FILE_BYTES = 128

    def tearDown(self):
        files_service.ROOTS = self.old_roots
        files_service.MAX_FILE_BYTES = self.old_max

    def test_write_over_limit_rejected(self):
        p = f"{self.root}/big.txt"
        with self.assertRaises(RuntimeError):
            files_service.write_text(p, "a" * 1024)

    def test_read_over_limit_rejected(self):
        p = f"{self.root}/big2.txt"
        with open(p, "w", encoding="utf-8") as f:
            f.write("a" * 1024)
        with self.assertRaises(RuntimeError):
            files_service.read_text(p)

    def test_write_within_limit_ok(self):
        p = f"{self.root}/ok.txt"
        res = files_service.write_text(p, "hello")
        self.assertEqual(res["size"], 5)
        self.assertEqual(files_service.read_text(p)["content"], "hello")

    def test_rename_cross_dir_rejected(self):
        os.makedirs(f"{self.root}/a", exist_ok=True)
        os.makedirs(f"{self.root}/b", exist_ok=True)
        with open(f"{self.root}/a/f.txt", "w", encoding="utf-8") as f:
            f.write("x")
        with self.assertRaises(RuntimeError):
            files_service.fs_action("rename", f"{self.root}/a/f.txt", f"{self.root}/b/f.txt")

    def test_delete_root_rejected(self):
        with self.assertRaises(RuntimeError):
            files_service.fs_action("delete", self.root)


# ---------------------------------------------------------------- 命令执行
class TestCommandExecution(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_124(self):
        res = await run("sleep", ["5"], timeout=1)
        self.assertEqual(res["code"], 124, "超时必须返回 124 且不能挂住事件循环")

    async def test_run_shell_async(self):
        out = await run_shell_async("echo panel-ok", timeout=5)
        self.assertIn("panel-ok", out)

    async def test_cwd_respected(self):
        res = await run("pwd", [], cwd="/tmp", timeout=5)
        self.assertIn("/tmp", res["out"])


# ---------------------------------------------------------------- nginx 渲染
class TestVhostRender(unittest.TestCase):
    def test_security_headers_without_ssl(self):
        out = nginx_ops.render_vhost({"name": "demo", "domain": "demo.example.com", "port": 3000})
        self.assertIn("http2 on;", out)
        self.assertIn("X-Content-Type-Options nosniff", out)
        self.assertIn("Referrer-Policy", out)
        self.assertNotIn("Strict-Transport-Security", out, "无证书时不应发送 HSTS")

    def test_invalid_domain_rejected(self):
        with self.assertRaises(RuntimeError):
            nginx_ops.render_vhost({"name": "x", "domain": "not a domain", "port": 3000})


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------- 部署模板
class TestTemplates(unittest.TestCase):
    def test_keys_unique_and_typed(self):
        from app.templates import TEMPLATES

        keys = [t["key"] for t in TEMPLATES]
        self.assertEqual(len(keys), len(set(keys)), "模板 key 必须唯一")
        for t in TEMPLATES:
            # type 受 apps 表 CHECK 约束，写错会导致建应用直接失败
            self.assertIn(t["type"], ("node", "python"))
            self.assertTrue(t["start_cmd"], f"{t['key']} 缺少 start_cmd")
            self.assertTrue(1024 <= t["port"] <= 65535, f"{t['key']} 端口越界")

    def test_apply_fills_defaults(self):
        from app.templates import apply_template

        out = apply_template({"name": "demo", "template": "python-fastapi"})
        self.assertEqual(out["type"], "python")
        self.assertEqual(out["port"], 8000)
        self.assertIn("uvicorn", out["start_cmd"])

    def test_explicit_values_win(self):
        from app.templates import apply_template

        out = apply_template({"name": "demo", "template": "node-service", "port": 4000, "start_cmd": "node x.js"})
        self.assertEqual(out["port"], 4000, "显式传参不能被模板覆盖")
        self.assertEqual(out["start_cmd"], "node x.js")

    def test_unknown_template_rejected(self):
        from app.templates import apply_template

        with self.assertRaises(RuntimeError):
            apply_template({"name": "demo", "template": "not-exist"})

    def test_without_template_is_noop(self):
        from app.templates import apply_template

        src = {"name": "demo", "type": "node", "start_cmd": "npm start"}
        self.assertEqual(apply_template(dict(src)), src)


# ---------------------------------------------------------------- 就绪探针
class TestReadyProbe(unittest.IsolatedAsyncioTestCase):
    async def test_ready_reports_database(self):
        from app.routers.meta import ready

        res = await ready()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(json.loads(res.body)["ready"])

    async def test_checks_shape(self):
        from app.routers.meta import run_checks

        r = await run_checks()
        self.assertIn(r["status"], ("pass", "warn", "fail"))
        self.assertEqual(sum(r["counts"].values()), len(r["items"]))
        for i in r["items"]:
            self.assertIn(i["status"], ("pass", "warn", "fail"))
            self.assertTrue(i["title"])
            self.assertTrue(i["detail"])
