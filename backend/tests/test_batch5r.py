"""批次⑤R（认证与会话层收口）的回归测试。

这一轮收的是"登录之后的一切"：
1. 只读账号的边界——数据浏览（能翻任意 PG 表行、任意 Redis 键值，会话 token 就在里面）
   与防火墙规则表整块按 admin 收口，且收口挂在 router 上：逐条写 `if r := need_admin(req)`
   已经被证明会漏（那一整块 7 个 GET 一个都没写）；
2. 真登出——无状态 JWT 没有会话表可以只作废手里这一枚，所以服务端吊销该账号全部会话，
   并在返回里如实说明；viewer 的 logout 必须在自助白名单里，否则前端清了本地、
   服务端什么都没发生；
3. 吊销的优先级——全局 bump 与单账号墓碑都只跟时间比大小，字典序会骗人；
4. 限速采信——只听"确实来自反代"的对端转发的 X-Real-IP；
5. 账号名单不是秘密——用户名不存在时也要烧掉同样一次 scrypt；
6. 免登录/只读接口不再回显绝对路径与命令里的凭据（ready、backups.dir、apps.path/*_cmd、SSE 日志）。
"""

import asyncio
import contextlib
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5r_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_5r_roots_"))

from app import (  # noqa: E402
    config,
    deps,
    main,  # noqa: E402
    security,
)
from app import database as dbm  # noqa: E402
from app.routers import apps as apps_router  # noqa: E402
from app.routers import dbops, extras, meta, system  # noqa: E402

_seq = itertools.count()
LOGIN_IP = "10.0.0.9"


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _mkuser(role: str = "viewer", pw: str = "Password123") -> str:
    u = _uid(role)
    dbm.execute(
        "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
        (u, security.hash_pass(pw), role),
    )
    return u


def _deluser(*names: str):
    dbm.execute(f"DELETE FROM users WHERE username IN ({','.join('?' * len(names))})", names)


def _req(body=None, role="admin", sub="tester", peer=LOGIN_IP, headers=None):
    async def _json():
        return body

    return SimpleNamespace(
        state=SimpleNamespace(cp_role=role, cp_sub=sub),
        json=_json,
        headers=headers or {},
        client=SimpleNamespace(host=peer),
    )


def _auth(user: str, role: str) -> dict:
    return {"Authorization": f"Bearer {security.sign_token(user, role)}"}


class TestRealStack(unittest.TestCase):
    """过一遍真实 ASGI 栈：router 级依赖、中间件白名单、异常处理器都要是真的。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        self.viewer = _mkuser("viewer")
        self.admin = _mkuser("admin")
        self.addCleanup(_deluser, self.viewer, self.admin)
        security.clear_attempts("testclient")

    # -------------------------------------------------- 数据浏览整块 admin only
    def test_viewer_blocked_on_every_db_route(self):
        h = _auth(self.viewer, "viewer")
        for path in ("/api/db/pg/databases", "/api/db/pg/roles", "/api/db/redis",
                     "/api/db/pg/rosetta/tables", "/api/db/redis/keys", "/api/db/redis/key"):
            r = self.client.get(path, headers=h)
            self.assertEqual(r.status_code, 403, f"{path} 对只读账号开放：{r.text[:120]}")
            # 依赖里 raise 的 Forbidden 必须走统一错误体，不能变成 FastAPI 默认的 {"detail": ...}
            self.assertEqual(r.json().get("code"), "forbidden", r.text[:120])

    def test_admin_is_not_locked_out_by_the_router_dependency(self):
        r = self.client.get("/api/db/redis", headers=_auth(self.admin, "admin"))
        self.assertNotEqual(r.status_code, 403, r.text[:200])

    def test_the_gate_is_mounted_on_the_router_not_per_endpoint(self):
        """挂在 router 上的依赖会被后加的路由继承——这正是选它而不是逐条写的理由。"""
        deps_on_router = [getattr(d, "dependency", None) for d in dbops.router.dependencies]
        self.assertIn(deps.require_admin, deps_on_router, deps_on_router)

    # -------------------------------------------------- 防火墙
    def test_firewall_is_admin_only(self):
        v = self.client.get("/api/firewall", headers=_auth(self.viewer, "viewer"))
        self.assertEqual(v.status_code, 403, v.text[:120])

        async def fake_run(*a, **k):
            return {"code": 1, "out": "", "err": ""}

        with mock.patch.object(system, "run", fake_run):
            a = self.client.get("/api/firewall", headers=_auth(self.admin, "admin"))
        self.assertEqual(a.status_code, 200, a.text[:200])

    # -------------------------------------------------- 真登出
    def test_viewer_can_logout_and_its_token_dies(self):
        h = _auth(self.viewer, "viewer")
        self.assertEqual(self.client.get("/api/db/redis", headers=h).status_code, 403)
        # logout 是 POST：不在 SELF_SERVICE_PREFIXES 里的话，viewer 会当场吃到
        # 「只读账号不能执行写操作」，而前端照样清本地 → token 活到 TTL 结束。
        r = self.client.post("/api/auth/logout", headers=h, json={})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json().get("scope"), "all_devices")
        self.assertEqual(self.client.get("/api/apps", headers=h).status_code, 401)

    def test_logout_does_not_touch_other_accounts(self):
        h = _auth(self.viewer, "viewer")
        admin_h = _auth(self.admin, "admin")
        self.client.post("/api/auth/logout", headers=h, json={})
        self.assertEqual(self.client.get("/api/apps", headers=admin_h).status_code, 200)

    # -------------------------------------------------- 只读接口的信息泄露
    def test_backups_list_hides_dir_from_viewer(self):
        dir_ = "/root/backups/panel"

        def fake():
            return [{"id": 1, "kind": "sqlite", "name": "x", "dir": dir_, "files": []}]

        with mock.patch("app.services.backup_service.list_backups", fake):
            v = self.client.get("/api/backups", headers=_auth(self.viewer, "viewer")).json()
            a = self.client.get("/api/backups", headers=_auth(self.admin, "admin")).json()
        self.assertNotIn("dir", v[0], "备份落盘目录（/root 下）不该随只读列表出去")
        self.assertEqual(a[0]["dir"], dir_)

    def test_backup_files_hides_dir_from_viewer(self):
        def fake():
            return {"dir": "/root/backups/panel", "files": []}

        with mock.patch("app.services.backup_service.list_files", fake):
            v = self.client.get("/api/backups/files", headers=_auth(self.viewer, "viewer")).json()
            a = self.client.get("/api/backups/files", headers=_auth(self.admin, "admin")).json()
        self.assertNotIn("dir", v)
        self.assertEqual(a["dir"], "/root/backups/panel")

    def test_apps_list_hides_path_and_redacts_cmds(self):
        row = {
            "id": 7, "name": "demo", "repo_url": "https://u:ghp_SECRET@github.com/u/r.git",
            "path": "/root/www/demo", "start_cmd": "PGPASSWORD=hunter2 node server.js",
            "install_cmd": "npm install",
        }

        async def fake_list():
            return [dict(row)]

        with mock.patch.object(apps_router.apps_service, "list_apps", fake_list):
            v = self.client.get("/api/apps", headers=_auth(self.viewer, "viewer")).json()
            a = self.client.get("/api/apps", headers=_auth(self.admin, "admin")).json()
        blob = json.dumps(v[0], ensure_ascii=False)
        self.assertIsNone(v[0]["path"], "应用装在 /root 下的绝对路径随只读列表出去了")
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("ghp_SECRET", blob)
        self.assertEqual(a[0]["path"], "/root/www/demo")
        self.assertIn("hunter2", a[0]["start_cmd"], "admin 也要打码就说明收口写过头了")

    def test_app_detail_hides_path_and_env_from_viewer(self):
        row = {
            "id": 8, "name": "demo", "type": "node", "repo_url": "https://github.com/u/r.git",
            "path": "/root/www/demo", "port": 3000, "start_cmd": "AWS_SECRET_ID=wjj thing",
            "install_cmd": "npm install", "env": '[{"k":"DB_PASS","v":"s3cret"}]', "parent_id": None,
        }
        with mock.patch.object(apps_router.apps_service, "get_app", lambda i: dict(row)), \
             mock.patch.object(apps_router.dbm, "query", lambda *a, **k: []), \
             mock.patch.object(apps_router.dbm, "query_one", lambda *a, **k: None), \
             mock.patch.object(apps_router.systemd_ops, "is_active", lambda u: asyncio.sleep(0, False)):
            v = self.client.get("/api/apps/8", headers=_auth(self.viewer, "viewer")).json()
            a = self.client.get("/api/apps/8", headers=_auth(self.admin, "admin")).json()
        self.assertIsNone(v["path"])
        self.assertNotIn("wjj", json.dumps(v, ensure_ascii=False))
        self.assertIn("s3cret", a["env"])
        self.assertEqual(a["path"], "/root/www/demo")

    def test_ready_does_not_echo_exception_text(self):
        """/api/ready 免登录。SQLite 的 OSError 带绝对路径，PG 的连接失败带 host/port
        甚至用户名——原样回显等于把基础设施细节公开给任何能连上端口的人。"""
        def boom(*a, **k):
            raise OSError('connection to server at "10.0.0.5", port 5432 failed: '
                          'password authentication failed for user "rosetta"')

        with mock.patch.object(meta.dbm, "query_one", boom):
            r = self.client.get("/api/ready")
        self.assertEqual(r.status_code, 503, r.text[:200])
        self.assertEqual(r.json()["checks"]["database"], "fail")
        for leak in ("rosetta", "5432", "10.0.0.5"):
            self.assertNotIn(leak, r.text, r.text[:200])


class TestEpochPrecedence(unittest.TestCase):
    """全局吊销必须压过更早的单账号墓碑，比较要用 time_ns 的数值而不是字典序。"""

    def setUp(self):
        self.user = _mkuser("admin")
        self.addCleanup(_deluser, self.user)

    def test_global_bump_revokes_a_previously_bumped_user(self):
        """旧写法 `token_epoch:<user> or <全局>`：第一个改过密的人**永久**把全局值挡在门外，
        于是管理员按下"踢下所有人"之后，最可能持有长期 token 的那批人手里的旧票照样能用。"""
        before_reset = security.sign_token(self.user, "admin")
        security.bump_user_epoch(self.user)
        after_reset = security.sign_token(self.user, "admin")
        dbm.bump_token_epoch()
        for token, label in ((before_reset, "改密前的旧票"), (after_reset, "墓碑之后签发的票")):
            with self.assertRaises(Exception, msg=f"{label}在全局吊销后仍然可用"):
                security.verify_token(token)

    def test_per_user_bump_after_global_revokes_only_that_user(self):
        dbm.bump_token_epoch()
        token = security.sign_token(self.user, "admin")
        security.verify_token(token)  # 全局吊销之后重新签发的必须能用
        other = _mkuser("admin")
        self.addCleanup(_deluser, other)
        other_token = security.sign_token(other, "admin")
        security.bump_user_epoch(self.user)
        with self.assertRaises(Exception):
            security.verify_token(token)
        security.verify_token(other_token)  # 单账号吊销不能牵连别人

    def test_head_compare_is_numeric_not_lexicographic(self):
        # time_ns 的十六进制跨位长时字典序会反：'fff' > '1000'，数值上却更小。
        self.assertGreater(security._epoch_head("1000.aaaa"), security._epoch_head("fff.bbbb"))
        self.assertEqual(security._epoch_head("坏值"), -1)


class TestClientIp(unittest.TestCase):
    def test_forged_header_from_untrusted_peer_is_ignored(self):
        # 面板只听 127.0.0.1，但本机任何进程都能绕过 nginx 直连端口并随手编一个 X-Real-IP。
        # 对端不在 TRUSTED_PROXIES 时不认这些头，限速才不会被"每个伪造 IP 一个空桶"绕过。
        with mock.patch.object(config, "TRUST_PROXY", True):
            self.assertEqual(
                main.client_ip(_req(headers={"x-real-ip": "1.2.3.4"}, peer="127.0.0.1")), "1.2.3.4"
            )
            self.assertEqual(
                main.client_ip(_req(headers={"x-real-ip": "1.2.3.4"}, peer="10.1.2.3")),
                "10.1.2.3",
                "非反代对端的 X-Real-IP 被采信了",
            )
            self.assertEqual(
                main.client_ip(_req(headers={"x-forwarded-for": "9.9.9.9, 7.7.7.7"}, peer="10.1.2.3")),
                "10.1.2.3",
            )

    def test_trusted_peer_uses_rightmost_xff(self):
        # 最左值是对端自带的、可每次换一个；nginx 的 $proxy_add_x_forwarded_for 把真实
        # 对端追加在最右，所以只信最右一段。
        with mock.patch.object(config, "TRUST_PROXY", True):
            req = _req(headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"}, peer="127.0.0.1")
            self.assertEqual(main.client_ip(req), "2.2.2.2")

    def test_trust_proxy_off_ignores_headers_even_from_loopback(self):
        with mock.patch.object(config, "TRUST_PROXY", False):
            req = _req(headers={"x-real-ip": "1.2.3.4"}, peer="127.0.0.1")
            self.assertEqual(main.client_ip(req), "127.0.0.1")

    def test_loopback_is_trusted_by_default(self):
        self.assertIn("127.0.0.1", config.TRUSTED_PROXIES)


class TestLoginTiming(unittest.TestCase):
    def setUp(self):
        security.clear_attempts(LOGIN_IP)
        self.addCleanup(security.clear_attempts, LOGIN_IP)

    def _login(self, username, password):
        return asyncio.run(main.login(_req({"username": username, "password": password})))

    def test_unknown_user_still_burns_one_scrypt(self):
        """短路返回的话"未知用户 ~1ms / 已知用户 ~50ms"就是公开账号名单，
        401 文案一模一样也救不了这点。"""
        burns, checks = [], []

        async def spy_burn(p):
            burns.append(p)

        async def spy_check(p, h):
            checks.append(p)
            return False

        with mock.patch.object(security, "burn_kdf_async", spy_burn), \
             mock.patch.object(security, "check_pass_async", spy_check):
            r = self._login(_uid("nobody"), "Password123")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(burns, ["Password123"])
        self.assertEqual(checks, [])

    def test_known_user_does_not_burn(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        burns = []

        async def spy_burn(p):
            burns.append(p)

        async def spy_check(p, h):
            return False

        with mock.patch.object(security, "burn_kdf_async", spy_burn), \
             mock.patch.object(security, "check_pass_async", spy_check):
            self._login(u, "WrongPassword1")
        self.assertEqual(burns, [], "真用户走的是 check_pass，再烧一次只把耗时翻倍")

    def test_amplification_gates_still_short_circuit_the_burn(self):
        """空口令与离谱长口令不该触发任何一次哈希（那是对方挑着打的 CPU 放大）。"""
        for pw in ("", "x" * (security.LOGIN_PW_MAX_LEN + 1)):
            burns, checks = [], []

            async def spy_burn(p):
                burns.append(p)

            async def spy_check(p, h):
                checks.append(p)
                return False

            with mock.patch.object(security, "burn_kdf_async", spy_burn), \
                 mock.patch.object(security, "check_pass_async", spy_check):
                r = self._login(_uid("ghost"), pw)
            self.assertEqual(r.status_code, 401, repr(pw[:20]))
            self.assertEqual(burns + checks, [], f"口令长度 {len(pw)} 时仍然算了 scrypt")

    def test_unencodable_password_burns_instead_of_500(self):
        # 孤立代理字符在 JSON 里合法、encode() 就炸；burn_kdf 必须自己兜住，
        # 否则免登录的登录路由会因为"用户名不存在 + 畸形口令"返回 500。
        r = self._login(_uid("surg"), chr(0xD800) * 8)
        self.assertEqual(r.status_code, 401, r.body[:200])

    def test_burn_uses_the_same_cost_parameters_as_check_pass(self):
        seen = {}
        real = security.hashlib.scrypt

        def spy(*a, **kw):
            seen.setdefault("kw", dict(kw))
            return real(*a, **kw)

        with mock.patch.object(security.hashlib, "scrypt", spy):
            security.burn_kdf("Password123")
        kw = seen["kw"]
        self.assertEqual(
            (kw["n"], kw["r"], kw["p"], kw["dklen"], kw["maxmem"]),
            (2**14, 8, 1, 64, 32 * 1024 * 1024),
            "空烧的参数与真实校验不一致，两条分支的耗时就不一致",
        )


class TestSseRedaction(unittest.TestCase):
    def test_journal_lines_are_redacted(self):
        raw = [b"INFO app: db url postgresql://u:TOPSECRET@10.0.0.5:5432/app\n", b""]

        class Stdout:
            async def readline(self):
                return raw.pop(0) if raw else b""

        class Proc:
            stdout = Stdout()

            def kill(self):
                pass

        async def spawn(*a, **k):
            return Proc()

        async def drain():
            with mock.patch.object(asyncio, "create_subprocess_exec", spawn):
                return [chunk async for chunk in system._sse_journal("x.service", 10)]

        out = "".join(asyncio.run(drain()))
        self.assertNotIn("TOPSECRET", out)
        self.assertIn("INFO app:", out)


class TestAdminUserEditKeepsSession(unittest.TestCase):
    """昵称/头像不在票面里，跟着 role/password 一起吊销就是误伤：
    管理员只想改个显示名，对方下一个请求即 401、网页终端被 4401 关掉。"""

    def _patch(self, uid, body, sub="boss"):
        return asyncio.run(extras.users_update(str(uid), _req(body, role="admin", sub=sub)))

    def test_nickname_only_edit_does_not_revoke(self):
        u = _mkuser("viewer")
        self.addCleanup(_deluser, u)
        row = dbm.query_one("SELECT * FROM users WHERE username=?", (u,))
        token = security.sign_token(u, "viewer")
        r = self._patch(row["id"], {"nickname": "小陈"})
        self.assertEqual(getattr(r, "status_code", 200), 200, getattr(r, "body", r))
        security.verify_token(token)
        self.assertEqual(dbm.query_one("SELECT nickname FROM users WHERE id=?", (row["id"],))["nickname"], "小陈")

    def test_role_change_still_revokes(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        row = dbm.query_one("SELECT * FROM users WHERE username=?", (u,))
        token = security.sign_token(u, "admin")
        self._patch(row["id"], {"role": "viewer"}, sub=_uid("boss"))
        with self.assertRaises(Exception):
            security.verify_token(token)


class TestCliAccountCommands(unittest.TestCase):
    def setUp(self):
        import cli

        self.cli = cli
        self.parser = cli.build_parser()

    def _run(self, argv, stdin_text=None):
        ns = self.parser.parse_args(argv)
        captured, err, out = {}, io.StringIO(), io.StringIO()
        buf = SimpleNamespace(readline=lambda: "" if stdin_text is None else stdin_text)

        def fake_result(data, text=""):
            captured["data"] = data

        with mock.patch.object(sys, "stdin", buf), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys, "stderr", err), \
             mock.patch.object(self.cli.Out, "result", staticmethod(fake_result)):
            try:
                asyncio.run(self.cli.cmd_user(ns))
            except SystemExit as e:
                captured["exit"] = e.code
        return captured, err.getvalue()

    def test_password_stdin_is_used(self):
        u = _uid("clipw")
        self.addCleanup(_deluser, u)
        res, err = self._run(["user", "create", u, "--password-stdin", "--role", "viewer"], "Password123\n")
        self.assertTrue(res["data"].get("ok"), res)
        self.assertEqual(err, "", "stdin 形态不该再有警告")
        row = dbm.query_one("SELECT * FROM users WHERE username=?", (u,))
        self.assertTrue(security.check_pass("Password123", row["pass_hash"]))

    def test_argv_password_warns(self):
        u = _uid("cliargv")
        self.addCleanup(_deluser, u)
        res, err = self._run(["user", "create", u, "--password", "Password123"])
        self.assertTrue(res["data"].get("ok"), res)
        self.assertIn("--password-stdin", err, "argv 形态必须留下警告，否则没人知道该换")

    def test_both_forms_is_usage_error(self):
        res, err = self._run(["user", "create", _uid("both"), "--password", "Password123", "--password-stdin"], "x\n")
        self.assertEqual(res.get("exit"), self.cli.EXIT_USAGE, res)

    def test_empty_stdin_is_usage_error(self):
        res, _ = self._run(["user", "create", _uid("emptystd"), "--password-stdin"], "")
        self.assertEqual(res.get("exit"), self.cli.EXIT_USAGE, res)

    def test_revoke_single_user_kills_only_that_token(self):
        u, other = _mkuser("admin"), _mkuser("admin")
        self.addCleanup(_deluser, u, other)
        t_u, t_other = security.sign_token(u, "admin"), security.sign_token(other, "admin")
        res, _ = self._run(["user", "revoke", u])
        self.assertTrue(res["data"].get("ok"), res)
        with self.assertRaises(Exception):
            security.verify_token(t_u)
        security.verify_token(t_other)

    def test_revoke_all_kills_every_token(self):
        u, other = _mkuser("admin"), _mkuser("admin")
        self.addCleanup(_deluser, u, other)
        tokens = [security.sign_token(u, "admin"), security.sign_token(other, "admin")]
        res, _ = self._run(["user", "revoke", "all"])
        self.assertEqual(res["data"].get("scope"), "all", res)
        for t in tokens:
            with self.assertRaises(Exception):
                security.verify_token(t)

    def test_revoke_unknown_user_fails(self):
        res, _ = self._run(["user", "revoke", _uid("ghost")])
        self.assertEqual(res.get("exit"), self.cli.EXIT_FAIL, res)


class TestDeployContract(unittest.TestCase):
    """AGENTS.md 是给人和 AI 照抄执行的：它推荐 stdin 形态，schema 与 argparse 都要跟得上。"""

    def test_schema_documents_revoke_and_stdin(self):
        import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            schema = asyncio.run(cli.cmd_schema(SimpleNamespace()))
        user = next(c for c in schema["commands"] if c["name"].startswith("user"))
        self.assertIn("revoke", user["name"])
        self.assertIn("--password-stdin", user["args"])
        self.assertIn("--password-stdin", " ".join(schema["deployRecipe"]))
        self.assertIn("--password-stdin", schema["conventions"]["nonInteractive"])

    def test_agents_md_recipe_uses_stdin(self):
        text = (BACKEND.parent / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("--password-stdin", text)
        self.assertNotIn('user create admin --password "', text, "三十秒版还在推荐 argv 形态")


if __name__ == "__main__":
    unittest.main()
