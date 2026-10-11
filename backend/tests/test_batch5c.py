"""批次⑤C（备份恢复与账号流程收口）的回归测试。

这一轮收的是三类问题：
1. 恢复失败后留下没人接管的烂摊子——PG 的 drop/createdb 与灌数据写在同一段脚本里，
   一次非零退出既可能是"库没被碰过"也可能是"库已成空壳"，而这两种情况该做的事相反；
2. 账号接口的输入类型/长度：JSON 里 {"username": 123} 会让 123.strip() 抛 AttributeError
   （未登录请求换来 500 加一条脏审计），长到离谱的口令会把 scrypt 当成免费的 CPU 放大，
   而 128 位上限缺失意味着改密接口能把口令存成谁都复现不了的长度；
3. 只读账号在"改自己"这条路上被一刀切掉——上传头像的入口就在资料弹窗里，
   同一个弹窗中改昵称能保存、换头像报「只读账号不能执行写操作」；
   以及删号只删行不删头像文件，那张脸因为 /api/avatars/<id>.png 免登录而永久可枚举。
"""

import asyncio
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5c_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_5c_roots_"))

from app import (  # noqa: E402
    avatars,
    config,
    deps,
    main,  # noqa: E402
    security,
)
from app import database as dbm  # noqa: E402
from app.routers import extras  # noqa: E402
from app.services import backup_service as B  # noqa: E402

_seq = itertools.count()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 48
# 必须运行时拼出来：源码里写 "\ud800" 字面量会连这个文件自己的 .pyc 都写不进去
# （见 TestModulesAreBytecodeCacheable，那正是本轮同步时发现的真事故）。
LONE_SURROGATE = chr(0xD800) * 8


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _req(body=None, role="admin", sub="tester"):
    async def _json():
        return body

    return SimpleNamespace(
        state=SimpleNamespace(cp_role=role, cp_sub=sub),
        json=_json,
        headers={},
        client=SimpleNamespace(host="unittest"),
    )


class StreamReq:
    """够用的请求替身：upload_avatar 只读 headers 与 stream()。"""

    def __init__(self, data: bytes, content_length: str | None = None, sub="tester"):
        self._data = data
        self.headers = {"content-length": content_length} if content_length else {}
        self.state = SimpleNamespace(cp_role="admin", cp_sub=sub)

    async def json(self):
        return {}

    async def stream(self):
        for i in range(0, len(self._data), 8192):
            yield self._data[i:i + 8192]


async def _async_noop(*a, **k):
    return None


def _resp(r):
    """把直接调用处理器的返回拆成 (状态码, body dict)。

    这些路由成功时返回 dict（FastAPI 再序列化），失败时自己返回 JSONResponse，
    而 JSONResponse 没有 TestClient Response 的 .text/.json()，测试里要区别对待。
    """
    if isinstance(r, dict):
        return 200, r
    return r.status_code, json.loads(r.body.decode())


# ---------------------------------------------------------------- 模块可被编译缓存
class TestModulesAreBytecodeCacheable(unittest.TestCase):
    """每个 .py 都必须能 compile + marshal：这不只是"测试跑得过"的问题。

    uvicorn 导入模块时要把字节码写进 __pycache__，marshal 用严格 UTF-8 序列化字符串
    常量（含 docstring）。本轮同步 security.py 时就踩中过：docstring 里写了
    "\\ud800" 字面量，结果 `import app.security` 直接 UnicodeEncodeError——
    一次代码同步把服务变成起不来，而 pytest 的导入错误只表现为"收集失败"。
    """

    def test_every_module_is_marshalable(self):
        import marshal

        targets = [BACKEND / "cli.py"]
        targets += sorted((BACKEND / "app").rglob("*.py"))
        targets += sorted((BACKEND / "tests").glob("*.py"))
        for path in targets:
            with self.subTest(module=str(path.relative_to(BACKEND))):
                code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
                marshal.dumps(code)


# ---------------------------------------------------------------- 口令规则
class TestPasswordRule(unittest.TestCase):
    """password_error 是 setup / user create / user reset / 改密四条路径共用的尺子。"""

    def test_too_short_and_empty(self):
        self.assertIn("至少 8 位", security.password_error("abc"))
        self.assertIn("至少 8 位", security.password_error(""))

    def test_too_long(self):
        self.assertIn("最多 128 位", security.password_error("A" * 129))

    def test_boundary_values_accepted(self):
        self.assertIsNone(security.password_error("A" * 8))
        self.assertIsNone(security.password_error("A" * 128))

    def test_non_string_is_rejected_not_crashing(self):
        """{"password": 123} / null / 数组都是合法 JSON：
        不在这里挡住，后面的 len()/encode() 抛 ValueError/TypeError，接口以 500 收场。"""
        for bad in (None, 123, 12.5, ["Password123"], {"a": 1}, b"Password123"):
            with self.subTest(bad=repr(bad)):
                self.assertIn("至少 8 位", security.password_error(bad))

    def test_unencodable_rejected(self):
        """孤立代理字符能过 len()，但 encode() 抛 UnicodeEncodeError：
        不在这里挡住，哈希那一步就是 500。"""
        self.assertIn("无法编码", security.password_error(LONE_SURROGATE))

    def test_label_is_used_in_message(self):
        self.assertEqual(security.password_error("x", "新密码"), "新密码至少 8 位")

    def test_login_cap_is_looser_than_set_cap(self):
        """登录端只挡"离谱长度"，不能把历史遗留的长口令挡在门外。"""
        self.assertGreater(security.LOGIN_PW_MAX_LEN, security.PW_MAX_LEN)


class TestCheckPassEncoding(unittest.TestCase):
    def test_lone_surrogate_returns_false(self):
        stored = security.hash_pass("Password123")
        self.assertFalse(security.check_pass(LONE_SURROGATE, stored))

    def test_normal_roundtrip_still_works(self):
        stored = security.hash_pass("Password123")
        self.assertTrue(security.check_pass("Password123", stored))
        self.assertFalse(security.check_pass("Password124", stored))


# ---------------------------------------------------------------- 登录/建号入口
class TestAuthEndpoints(unittest.TestCase):
    """未登录接口必须以 4xx 收场：500 把栈写进 journal，审计里留下一条例如
    用户名为 "123" 的记录，事后根本分不清那是攻击还是别人打错了字。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        security.clear_attempts("testclient")

    def _setup(self, payload):
        real = dbm.query_one

        def fake(sql, params=()):
            # 只把"已有管理员"这一道 403 短路掉，好让入参校验分支必被执行；
            # 其余查询照走实现——setup 这条路径上还有 get_setting 之类的查询，
            # 一律返回 {"c": 0} 会把它们一起打坏。
            if "COUNT(*) c FROM users" in sql:
                return {"c": 0}
            return real(sql, params)

        with mock.patch.object(dbm, "query_one", fake):
            return self.client.post("/api/auth/setup", json=payload)

    def test_numeric_username_is_400_not_500(self):
        # 用 12 而不是 123：USERNAME_RE 允许纯数字，123 会被当成合法用户名建号。
        r = self._setup({"username": 12, "password": "Password123"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("用户名", r.json()["error"])

    def test_short_numeric_password_is_400_not_500(self):
        r = self._setup({"username": _uid("s"), "password": 1234})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("至少 8 位", r.json()["error"])

    def test_overlong_password_is_400(self):
        r = self._setup({"username": _uid("s"), "password": "A" * 200})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("最多 128 位", r.json()["error"])

    def test_login_type_confusion_does_not_500(self):
        r = self.client.post("/api/auth/login", json={"username": 999, "password": ["x"]})
        self.assertEqual(r.status_code, 401, r.text)

    def test_absurd_length_password_never_reaches_scrypt(self):
        """scrypt(n=2^14) 一次约 16MB 内存：对方发一个 200KB 的"密码"就能让我们
        白算一次哈希——不带用户名匹配也行。长度必须先于哈希判掉。"""
        user = _uid("len")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (user, security.hash_pass("Password123"), "admin"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        seen = []

        async def spy(pw, stored):
            seen.append(len(pw))
            return True

        with mock.patch.object(security, "check_pass_async", spy):
            big = self.client.post("/api/auth/login", json={"username": user, "password": "x" * 200000})
            after_big = list(seen)
            good = self.client.post("/api/auth/login", json={"username": user, "password": "Password123"})
        self.assertEqual(big.status_code, 401, big.text)
        self.assertEqual(after_big, [], "超长口令不该进哈希")
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(seen, [len("Password123")], "正常口令照常校验")


# ---------------------------------------------------------------- 改密
class TestChangePassword(unittest.TestCase):
    def setUp(self):
        self.user = _uid("cpw")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.user, security.hash_pass("Password123"), "viewer"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))

    def _call(self, body):
        return asyncio.run(main.change_password(_req(body, sub=self.user)))

    def _stored(self):
        return dbm.query_one("SELECT pass_hash FROM users WHERE username=?", (self.user,))["pass_hash"]

    def test_overlong_new_password_rejected_and_unchanged(self):
        before = self._stored()
        code, body = _resp(self._call({"old": "Password123", "new": "A" * 300}))
        self.assertEqual(code, 400, body)
        self.assertIn("新密码最多 128 位", body["error"])
        self.assertEqual(self._stored(), before, "校验没过时不该写库")

    def test_non_string_new_password_rejected(self):
        code, body = _resp(self._call({"old": "Password123", "new": 123456789}))
        self.assertEqual(code, 400, body)
        self.assertIn("新密码", body["error"])

    def test_overlong_old_password_treated_as_wrong(self):
        seen = []

        async def spy(pw, stored):
            seen.append(len(pw))
            return True

        with mock.patch.object(security, "check_pass_async", spy):
            code, body = _resp(self._call({"old": "x" * 100000, "new": "Password456"}))
        self.assertEqual(code, 400, body)
        self.assertIn("原密码错误", body["error"])
        self.assertEqual(seen, [], "超长原密码不该进哈希")

    def test_valid_rotation_issues_token_and_revokes_old(self):
        code, body = _resp(self._call({"old": "Password123", "new": "Password456"}))
        self.assertEqual(code, 200, body)
        self.assertIn("token", body)
        self.assertTrue(security.check_pass("Password456", self._stored()))
        self.assertFalse(security.check_pass("Password123", self._stored()))


# ---------------------------------------------------------------- viewer 自助写
class TestViewerSelfService(unittest.TestCase):
    """viewer 唯一能写的东西是自己的账号：改密、改资料、换头像。

    头像必须和 profile 同批放行——上传入口就在资料弹窗里，漏掉它的话同一个弹窗
    里改昵称能保存、换头像报「只读账号不能执行写操作」，用户只会认为功能是坏的。
    """

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        self.user = _uid("vw")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.user, security.hash_pass("Password123"), "viewer"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.user, 'viewer')}"}
        self.avatar_dir = tempfile.mkdtemp(prefix="cp_5c_av_")
        p = mock.patch.object(config, "AVATAR_DIR", self.avatar_dir)
        self.addCleanup(p.stop)
        p.start()
        self.addCleanup(shutil.rmtree, self.avatar_dir, True)
        security.clear_attempts("testclient")

    def _denied_as_readonly(self, resp) -> bool:
        return resp.status_code == 403 and "只读账号" in resp.text

    def test_profile_update_allowed(self):
        r = self.client.post("/api/auth/profile", json={"nickname": "只读的小张"}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["nickname"], "只读的小张")

    def test_avatar_upload_allowed(self):
        r = self.client.post("/api/auth/avatar", content=PNG,
                             headers={**self.headers, "Content-Type": "image/png"})
        self.assertFalse(self._denied_as_readonly(r), r.text)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["avatar"].startswith("/api/avatars/"), r.text)

    def test_password_change_reaches_handler(self):
        """断言的是"过了角色门禁"：原密码故意给错，返回 400 原密码错误而不是
        403 只读，说明门禁没把人拦在门外。"""
        r = self.client.post("/api/auth/password", json={"old": "Nope12345", "new": "Password999"},
                             headers=self.headers)
        self.assertFalse(self._denied_as_readonly(r), r.text)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("原密码错误", r.json()["error"])

    def test_other_writes_still_denied(self):
        """放行只针对这三个前缀：其余写操作对 viewer 依旧是 403。"""
        for method, url in (("POST", "/api/users"), ("DELETE", "/api/users/999999"),
                            ("POST", "/api/apps"), ("PATCH", "/api/settings/alert_disk_pct")):
            r = self.client.request(method, url, json={}, headers=self.headers)
            with self.subTest(url=url):
                self.assertEqual(r.status_code, 403, r.text)
                self.assertIn("只读账号", r.json()["error"])

    def test_prefix_list_is_exactly_self_service(self):
        self.assertEqual(
            set(deps.SELF_SERVICE_PREFIXES),
            # /auth/logout 是⑤R 加的：它只抬调用者自己的 epoch，动不了别人的会话，
            # 所以和改密/资料一样属于"只读账号也能改自己"的形态。
            {"/api/auth/password", "/api/auth/profile", "/api/auth/avatar", "/api/auth/logout"},
        )


# ---------------------------------------------------------------- 头像文件清理
class TestAvatarPurge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_5c_avdir_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(config, "AVATAR_DIR", self.dir)
        self.addCleanup(p.stop)
        p.start()

    def test_only_own_files_removed(self):
        for name in ("7.png", "7.jpg", "8.png", "77.png", "notes.txt"):
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(PNG)
        self.assertEqual(avatars.purge(7), 2)
        self.assertEqual(sorted(os.listdir(self.dir)), ["77.png", "8.png", "notes.txt"])

    def test_missing_dir_is_not_an_error(self):
        p = mock.patch.object(config, "AVATAR_DIR", os.path.join(self.dir, "nope"))
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(avatars.purge(1), 0)

    def test_reupload_with_different_format_leaves_one_file(self):
        """先传 PNG 再传 JPEG：旧格式必须被清掉，否则两张脸同时挂在公开路由上，
        而界面只有一个"当前头像"，旧的那张再没有任何地方能删掉它。"""
        user = _uid("upl")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (user, security.hash_pass("Password123"), "admin"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        me = dbm.query_one("SELECT id FROM users WHERE username=?", (user,))["id"]
        for data in (PNG, JPEG):
            asyncio.run(main.upload_avatar(StreamReq(data, str(len(data)), sub=user)))
        self.assertEqual([f"{me}.jpg"], sorted(os.listdir(self.dir)))

    def test_route_delete_purges_files(self):
        victim = _uid("vd")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (victim, security.hash_pass("Password123"), "viewer"),
        )
        vid = dbm.query_one("SELECT id FROM users WHERE username=?", (victim,))["id"]
        for name in (f"{vid}.png", f"{vid}.jpg"):
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(PNG)
        with mock.patch.object(extras.aio_db, "audit", _async_noop):
            r = asyncio.run(extras.users_delete(str(vid), _req({}, role="admin", sub="theadmin")))
        self.assertEqual(r, {"ok": True}, r)
        self.assertEqual(os.listdir(self.dir), [], "删号必须连头像一起删")
        self.assertIsNone(dbm.query_one("SELECT id FROM users WHERE id=?", (vid,)))

    def test_cli_delete_purges_files(self):
        import cli

        victim = _uid("vcli")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (victim, security.hash_pass("Password123"), "viewer"),
        )
        vid = dbm.query_one("SELECT id FROM users WHERE username=?", (victim,))["id"]
        with open(os.path.join(self.dir, f"{vid}.png"), "wb") as f:
            f.write(PNG)
        args = cli.build_parser().parse_args(["user", "delete", victim, "--yes", "--json"])
        with mock.patch.object(cli.Out, "json_mode", True):
            asyncio.run(cli.cmd_user(args))
        self.assertEqual(os.listdir(self.dir), [])
        self.assertIsNone(dbm.query_one("SELECT id FROM users WHERE username=?", (victim,)))

    def test_cli_create_rejects_out_of_range_password(self):
        import cli

        for pw, why in (("short", "至少 8 位"), ("A" * 200, "最多 128 位")):
            args = cli.build_parser().parse_args(["user", "create", _uid("cc"), "--password", pw, "--json"])
            with mock.patch.object(cli.Out, "json_mode", True), self.assertRaises(SystemExit) as cm:
                asyncio.run(cli.cmd_user(args))
            self.assertEqual(cm.exception.code, cli.EXIT_USAGE, why)
        self.assertEqual([f for f in os.listdir(self.dir)], [])


# ---------------------------------------------------------------- PG 恢复分段
class TestPgRestorePhases(unittest.TestCase):
    """drop/createdb 与灌数据必须分两段跑，失败时才知道"现库到底被碰过没有"。

    旧写法把三步和 pg_restore 塞进同一段脚本：一次非零退出既可能是"drop 被占用的人
    挡住、现库完好"，也可能是"库已删、新内容只 restore 了一半"。前者的正确处置是重试，
    后者的正确处置是拿恢复前的留档重建——而日志把两者写成同一句"恢复失败"。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_5c_pg_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p1 = mock.patch.object(B, "BACKUP_DIR", self.dir)
        self.addCleanup(p1.stop)
        p1.start()

        async def _psql(sql, *a, **k):
            return "appowner\n"

        p2 = mock.patch.object(B, "psql", _psql)
        self.addCleanup(p2.stop)
        p2.start()
        self.src = os.path.join(self.dir, "bk9-2026-01-01-00-00-00-appdb.dump.gz")
        with open(self.src, "wb") as f:
            f.write(b"\x1f\x8b")  # 文件只需存在：真正执行的 shell 已被替身接管
        self.bk = {"id": 9, "target": "appdb", "kind": "pg"}

    def _run(self, codes, bk=None, src=None):
        """codes 按调用顺序给退出码：1 留档 dump、2 换空库、3 灌数据、4 回退。

        ⑤Q 在最前面加了一次 `pg_restore -l` 只读预检：它不碰库，也不属于本类要断的
        四个阶段，所以由替身直接答"产物可读、含 1 项 TABLE DATA"，不计入 scripts。
        """
        scripts = []

        async def fake_script(script, timeout=1800):
            if "pg_restore -l" in script:
                return {"code": 0, "out": "3420; 0 18851 TABLE DATA public t appowner\n"}
            scripts.append(script)
            i = len(scripts) - 1
            return {"code": codes[i] if i < len(codes) else 0, "out": f"step{i + 1} output"}

        p = mock.patch.object(B, "_sh_script", fake_script)
        p.start()
        self.addCleanup(p.stop)
        log = []
        err = None
        try:
            asyncio.run(B._restore_pg(bk or self.bk, src or self.src, log))
        except RuntimeError as e:
            err = e
        return scripts, log, err

    def test_happy_path_is_two_phases(self):
        scripts, log, err = self._run([0, 0, 0])
        self.assertIsNone(err, log)
        self.assertEqual(len(scripts), 3, "1 次留档 dump + 1 次换空库 + 1 次灌数据")
        drop, load = scripts[1], scripts[2]
        self.assertIn("set -e", drop)
        self.assertIn("dropdb --if-exists --force appdb", drop)
        self.assertIn("createdb -O appowner appdb", drop)
        self.assertNotIn("pg_restore", drop, "换空库那段不该顺手灌数据")
        self.assertIn("pg_restore -d appdb --no-privileges --exit-on-error", load)
        self.assertTrue(any("已从" in line and "恢复" in line for line in log), log)

    def test_swap_failure_says_library_untouched_and_stops(self):
        scripts, log, err = self._run([0, 1])
        self.assertIsNotNone(err)
        self.assertIn("当前库未被改动", str(err))
        self.assertEqual(len(scripts), 2, "空库没换成，就不该尝试灌数据")

    def test_load_failure_rebuilds_from_pre_restore(self):
        scripts, log, err = self._run([0, 0, 1, 0])
        self.assertIsNotNone(err)
        self.assertIn("已用", str(err))
        self.assertIn("pre-restore.dump.gz", str(err))
        rollback = scripts[3]
        self.assertIn("-pre-restore.dump.gz", rollback)
        self.assertIn("dropdb", rollback, "库里已有一半新内容，回退要重建而不是叠加")
        self.assertNotIn("--exit-on-error", rollback, "回退是救火：个别对象报错也得先把数据装回去")

    def test_double_failure_gives_manual_command(self):
        scripts, log, err = self._run([0, 0, 1, 1])
        self.assertIsNotNone(err)
        self.assertIn("恢复与回退均失败", str(err))
        self.assertIn("pg_restore", str(err), "要把人工补救命令直接写进错误里")
        self.assertEqual(len(scripts), 4)

    def test_pre_restore_name_carries_a_current_timestamp(self):
        """留档名必须是 bk<id>-<当前时间戳>-pre-restore.dump.gz：prune 按名字排序删最旧的，
        少了时间戳就会排到所有真实备份之后，把 keep 名额永久吃掉一格。"""
        scripts, log, err = self._run([0, 0, 0])
        self.assertIsNone(err, log)
        self.assertRegex(
            scripts[0],
            r"bk9-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d+)?-pre-restore\.dump\.gz",
            scripts[0],
        )

    def test_all_target_refused_before_any_command(self):
        scripts, log, err = self._run([], bk={"id": 9, "target": "all", "kind": "pg"})
        self.assertEqual(scripts, [], "整库备份绝不该由一个按钮触发")
        self.assertIn("手工恢复", str(err))

    def test_illegal_db_name_refused_before_any_command(self):
        scripts, log, err = self._run([], bk={"id": 9, "target": "appdb; DROP ROLE x", "kind": "pg"})
        self.assertEqual(scripts, [])
        self.assertIn("库名不合法", str(err))

    def test_dump_failure_changes_nothing(self):
        scripts, log, err = self._run([1])
        self.assertEqual(len(scripts), 1)
        self.assertIn("未做任何改动", str(err))


if __name__ == "__main__":
    unittest.main()
