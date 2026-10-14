"""批次㉓（面板登录两步验证）的回归测试。

这一批收的是"口令之外还剩什么"：
1. TOTP 只有 RFC 6238 那一份算式，参数（步长 30 秒、6 位、±1 窗口）只在 `app/totp.py`
   定义一次，同时经 `provisioning_uri()` 写进验证器 App 读的 URI、经 `totp_params()`
   随响应下发给前端填 `maxlength` —— 前端自己抄一个"6"，改参数那天就会变成"输入框只让
   填 6 位、校验却要 8 位"这种没人看得懂的坏法；
2. 第二因子只在**口令已经对了**之后才开口要，而口令错的那句响应不带任何第二步信息——
   `/api/auth/login` 是全面板唯一免认证入口；
3. 开启/关闭都要求再核一次本人口令，关闭还要一枚有效动态口令；密钥只进不出，
   任何响应、任何列表、任何审计 detail 都不回显它；
4. 验证器丢失时的逃生口是 root 才能跑的 `choyeonctl user 2fa-disable`，它顺带吊销
   该账号全部会话。
"""

import asyncio
import io
import itertools
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_23_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_23_roots_"))

from app import (  # noqa: E402
    config,
    deps,
    main,
    security,
    totp,
)
from app import database as dbm  # noqa: E402
from app.routers import extras  # noqa: E402
from app.services import login_audit_service as la  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_seq = itertools.count()
LOGIN_IP = "10.23.0.9"
PW = "Password123"


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _mkuser(role: str = "admin", pw: str = PW) -> str:
    u = _uid(role)
    dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)", (u, security.hash_pass(pw), role))
    return u


def _deluser(*names: str):
    dbm.execute(f"DELETE FROM users WHERE username IN ({','.join('?' * len(names))})", names)


def _req(body=None, role="admin", sub="tester", peer=LOGIN_IP):
    async def _json():
        return body

    return SimpleNamespace(
        state=SimpleNamespace(cp_role=role, cp_sub=sub),
        json=_json,
        headers={},
        client=SimpleNamespace(host=peer),
    )


def _auth(user: str, role: str) -> dict:
    return {"Authorization": f"Bearer {security.sign_token(user, role)}"}


def _resp(resp) -> tuple[int, dict]:
    """直调路由时，失败分支是 JSONResponse、成功分支是裸 dict，这里统一成 (状态码, 数据)。"""
    if hasattr(resp, "body"):
        return resp.status_code, json.loads(resp.body.decode())
    return 200, resp


def _code(secret: str, at: float | None = None) -> str:
    """按当前时间（或指定时刻）算一枚合法口令：测试用真算式，不写死数字。"""
    return totp.hotp(totp.normalize_secret(secret), totp.current_step(time.time() if at is None else at))


# RFC 6238 附录 B 的向量：密钥 "12345678901234567890"，SHA1，8 位口令。
# 断言取后 6 位（面板的 DIGITS）；数字来自标准本身，不是"我们算出来自己核对"。
RFC_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
               (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130")]


class TestTotpMath(unittest.TestCase):
    def setUp(self):
        totp.reset_used()
        self.addCleanup(totp.reset_used)

    def test_rfc6238_known_vectors(self):
        for t, full in RFC_VECTORS:
            with self.subTest(T=t):
                self.assertEqual(totp.hotp(RFC_B32, t // totp.STEP_SECONDS), full[-totp.DIGITS:])
                self.assertEqual(_code(RFC_B32, t), full[-totp.DIGITS:])

    def test_window_is_exactly_one_step(self):
        s = totp.new_secret()
        now = 1_000.0  # 落在第 33 步
        self.assertEqual(totp.current_step(now), 33)
        # 上一步/下一步的口令都还接受：手机与服务器总有几秒到几十秒的钟差，0 步窗口
        # 就是"快了 20 秒的人登不进"。
        for delta in (-1, 0, 1):
            with self.subTest(delta=delta):
                self.assertTrue(totp.verify(s, _code(s, now + delta * totp.STEP_SECONDS), now=now))
        # 再往外一步就是拿安全换方便——±1 已经是 90 秒的接受窗口。
        self.assertFalse(totp.verify(s, _code(s, now - 2 * totp.STEP_SECONDS), now=now))
        self.assertFalse(totp.verify(s, _code(s, now + 2 * totp.STEP_SECONDS), now=now))

    def test_code_is_used_only_once(self):
        """同一枚口令在窗口里重复提交必须拒——否则抓到一次口令的人在 90 秒内可以反复进门。"""
        s = totp.new_secret()
        now = 1_700_000_000.0
        c = _code(s, now)
        self.assertTrue(totp.verify(s, c, username="replay-user", now=now))
        self.assertFalse(totp.verify(s, c, username="replay-user", now=now))
        # 记账按账号：换一个名字不受影响（同一枚口令在别处本来也不该被复用，
        # 但密钥不同，这里核对的是"桶不是全局的"）。
        self.assertTrue(totp.verify(s, c, username="other-user", now=now))

    def test_used_bucket_moves_forward_not_stuck(self):
        """口径是"步数严格递增"，不是"只认最新一步"：用过 33 之后 34 仍然要能过。"""
        s = totp.new_secret()
        u = "move-up"
        self.assertTrue(totp.verify(s, _code(s, 990.0), username=u, now=990.0))  # 第 33 步
        self.assertTrue(totp.verify(s, _code(s, 1020.0), username=u, now=1020.0))  # 第 34 步
        self.assertFalse(totp.verify(s, _code(s, 990.0), username=u, now=1020.0))  # 回看的 -1 已被记过

    def test_anonymous_verify_has_no_replay_memory(self):
        """不传 username 时不记账：setup/enable 阶段还没有"这个人的上一步"可言。"""
        s = totp.new_secret()
        c = _code(s, 500.0)
        self.assertTrue(totp.verify(s, c, now=500.0))
        self.assertTrue(totp.verify(s, c, now=500.0))

    def test_normalize_secret_accepts_pasted_forms(self):
        raw = totp.new_secret()
        spaced = " ".join(raw[i:i + 4] for i in range(0, len(raw), 4))
        for variant in (raw.lower(), f" {raw} ", raw + "====", spaced,
                        "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))):
            with self.subTest(form=variant[:8]):
                self.assertEqual(totp.normalize_secret(variant), raw)

    def test_normalize_secret_rejects_garbage_as_valueerror(self):
        """非法密钥一律 ValueError（调用方按 400 处理），不能漏到 hotp() 里变 500。"""
        for bad in ("", "!!!not-base32!!!", "0189", "A" * 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as cm:
                totp.normalize_secret(bad)
            self.assertIn("base32", str(cm.exception))
        # 太短（熵不够）与太长都要拒——长度区间也只有一个出处。
        with self.assertRaises(ValueError) as short:
            totp.normalize_secret("ABCDEFGH")  # 8 个 base32 字符 = 5 字节，低于 SECRET_MIN_BYTES
        self.assertIn("太短", str(short.exception))
        # 合法但偏短的边界：16 个字符 = 10 字节，够 SECRET_MIN_BYTES，不该被误杀。
        self.assertEqual(totp.normalize_secret("ABCDEFGHIJKLMNOP"), "ABCDEFGHIJKLMNOP")
        with self.assertRaises(ValueError) as long_:
            totp.normalize_secret("A" * 200)
        self.assertIn("太长", str(long_.exception))

    def test_normalize_code_shape(self):
        self.assertEqual(totp.normalize_code("123456"), "123456")
        self.assertEqual(totp.normalize_code(" 123 456 "), "123456")
        self.assertEqual(totp.normalize_code("123-456"), "123456")
        for bad in ("12345", "1234567", "abcdef", "", "１２３４５６", 123456, None, ["123456"]):
            self.assertEqual(totp.normalize_code(bad), "", f"{bad!r} 不该被当成口令")

    def test_verify_never_raises_on_junk(self):
        for junk in ("", "!!!", "A" * 5000):
            self.assertFalse(totp.verify(junk, "123456"), f"{junk[:8]!r} 该判失败而不是抛异常")
        self.assertFalse(totp.verify(totp.new_secret(), "not-a-code"))

    def test_uri_and_params_agree_with_the_math(self):
        s = totp.new_secret()
        uri = totp.provisioning_uri(s, "admin")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(f"secret={s}", uri)
        # 验证器 App 读的是 URI 里的 period/digits：它们必须与算式用的那一份常量相同，
        # 否则 App 按 30 秒生成、面板按 60 秒校验，用户看到的是"扫了码永远对不上"。
        self.assertIn(f"digits={totp.DIGITS}", uri)
        self.assertIn(f"period={totp.STEP_SECONDS}", uri)
        p = totp.totp_params()
        self.assertEqual((p["digits"], p["period"], p["issuer"]), (totp.DIGITS, totp.STEP_SECONDS, totp.ISSUER))

    def test_uri_escapes_the_account_name(self):
        uri = totp.provisioning_uri(totp.new_secret(), "a-user", issuer="x y")
        self.assertIn("issuer=x%20y", uri)
        # label 是 `issuer:account` 一整段，冒号转义成 %3A（Google key-uri 规范的写法）：
        # 留裸冒号的话，账号名里再带一个冒号就会被 App 切错 issuer。
        self.assertIn("x%20y%3Aa-user", uri)
        self.assertNotIn("x y:a-user", uri)

    def test_cleanup_used_drops_impossible_steps(self):
        u = _uid("cleanup")
        totp.note_used(u, totp.current_step(0.0))
        totp.cleanup_used(now=time.time())
        self.assertNotIn(u, totp._used)

    def test_new_secret_is_the_recommended_length(self):
        self.assertEqual(len(totp._b32_decode(totp.new_secret())), totp.SECRET_BYTES)


class TestLoginGateway(unittest.TestCase):
    """口令对了之后才轮到第二因子。"""

    def setUp(self):
        security.clear_attempts(LOGIN_IP)
        totp.reset_used()
        self.addCleanup(security.clear_attempts, LOGIN_IP)
        self.addCleanup(totp.reset_used)

    def _login(self, username, password, code=None, peer=LOGIN_IP):
        body = {"username": username, "password": password}
        if code is not None:
            body["code"] = code
        return _resp(asyncio.run(main.login(_req(body, peer=peer))))

    def _two_factor_user(self) -> tuple[str, str]:
        u = _mkuser("admin")
        s = totp.new_secret()
        dbm.set_totp(u, s)
        self.addCleanup(_deluser, u)
        return u, s

    def test_no_code_means_totp_required(self):
        u, _ = self._two_factor_user()
        status, got = self._login(u, PW)
        self.assertEqual(status, 401, got)
        self.assertEqual(got.get("code"), "totp_required", got)
        # 位数随响应下发（前端拿它填输入框与提示语），而不是前端自己写死。
        self.assertIn(str(totp.DIGITS), got["error"])

    def test_right_code_logs_in(self):
        u, s = self._two_factor_user()
        status, got = self._login(u, PW, _code(s))
        self.assertEqual(status, 200, got)
        self.assertEqual(security.verify_token(got["token"])["sub"], u)

    def test_wrong_or_replayed_code_is_rejected(self):
        u, s = self._two_factor_user()
        bad = "000000" if _code(s) != "000000" else "111111"
        status, got = self._login(u, PW, bad)
        self.assertEqual((status, got.get("code")), (401, "totp_invalid"))
        good = _code(s)
        self.assertEqual(self._login(u, PW, good)[0], 200)
        # 第二次拿同一枚口令：会话已经建立过，重放必须失败。
        self.assertEqual(self._login(u, PW, good)[1].get("code"), "totp_invalid")

    def test_wrong_password_never_reveals_2fa_state(self):
        """口令错就是"用户名或密码错误"，不带 code 字段——否则这条分支就成了
        "哪些账号开了第二因子"的免认证查询接口。"""
        two, _ = self._two_factor_user()
        plain = _mkuser("admin")
        self.addCleanup(_deluser, plain)
        ghost = _uid("ghost")
        for user in (two, plain, ghost):
            with self.subTest(user="2fa" if user == two else ("plain" if user == plain else "ghost")):
                status, got = self._login(user, "WrongPassword1")
                self.assertEqual(status, 401, got)
                self.assertNotIn("code", got, f"泄露了第二步状态：{got}")
                self.assertEqual(got["error"], "用户名或密码错误")

    def test_account_without_2fa_is_unaffected(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        self.assertEqual(self._login(u, PW)[0], 200)

    def test_bad_code_costs_the_account_bucket(self):
        """动态口令只有 10^DIGITS 种，不计入账号桶就等于留了一条"每次猜都不花钱"的旁路。"""
        u, s = self._two_factor_user()
        bad = "000000" if _code(s) != "000000" else "111111"
        seen = {self._login(u, PW, bad)[0] for _ in range(config.LOGIN_LIMIT + 3)}
        self.assertEqual(seen, {401, 429}, f"账号桶没生效：{seen}")

    def test_the_ip_bucket_still_gates_before_any_of_this(self):
        """第二因子排在口令之后，也就排在 IP 限速之后：限速不该因为"这一步还没走到"而失忆。"""
        ghost = _uid("ghost")
        peer = "10.23.0.77"
        seen = {self._login(ghost, "WrongPassword1", peer=peer)[0] for _ in range(config.LOGIN_LIMIT + 3)}
        self.assertIn(429, seen)
        security.clear_attempts(peer)


class TestSelfServiceEndpoints(unittest.TestCase):
    """开与关都要本人口令；只读账号也必须管得了自己的第二因子。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        self.user = _mkuser("viewer")
        self.h = _auth(self.user, "viewer")
        self.admin = _mkuser("admin")
        self.addCleanup(_deluser, self.user, self.admin)
        dbm.set_totp(self.user, "")
        totp.reset_used()
        self.addCleanup(totp.reset_used)

    def _post(self, path, **body):
        return self.client.post(path, headers=self.h, json=body)

    def _enable(self) -> str:
        secret = self._post("/api/auth/2fa/setup", password=PW).json()["secret"]
        self.assertEqual(self._post("/api/auth/2fa/enable", password=PW, secret=secret, code=_code(secret)).status_code,
                         200)
        return secret

    def _next_window(self) -> None:
        """开启时那一枚口令已经被记账（一枚只用一次是设计），下一步要用另一枚。
        真人是等 30 秒，测试等不起也不该等：这里清的是进程内"用过的步数"，不是被测行为本身。"""
        totp.reset_used()

    def test_status_on_a_fresh_account(self):
        r = self.client.get("/api/auth/2fa", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text[:200])
        got = r.json()
        self.assertFalse(got["enabled"])
        self.assertEqual((got["digits"], got["period"], got["issuer"]),
                         (totp.DIGITS, totp.STEP_SECONDS, totp.ISSUER))

    def test_viewer_can_reach_its_own_2fa_routes(self):
        """SELF_SERVICE_PREFIXES 漏了 2fa 的话，只读账号收到的是 403「只读账号不能执行写操作」，
        设置页看起来像坏了——而只读账号往往正是日常登录用的那个。"""
        self.assertIn("/api/auth/2fa", deps.SELF_SERVICE_PREFIXES)
        for path in ("/api/auth/2fa/setup", "/api/auth/2fa/enable", "/api/auth/2fa/disable"):
            with self.subTest(path=path):
                r = self._post(path)
                self.assertNotEqual(r.status_code, 403, f"{path} 被只读门禁挡了：{r.text[:120]}")

    def test_setup_requires_the_password(self):
        for body in ({}, {"password": "Nope12345"}):
            with self.subTest(**body):
                r = self._post("/api/auth/2fa/setup", **body)
                self.assertEqual(r.status_code, 400, r.text[:200])
                self.assertNotIn("secret", r.json(), "口令不对时连密钥都不该生成")

    def test_enable_needs_password_and_a_valid_code(self):
        secret = self._post("/api/auth/2fa/setup", password=PW).json()["secret"]
        # 没有口令：光凭一枚会话票不能替主人开启 2FA（会话被劫持时反过来把真主人锁在门外）。
        self.assertEqual(self._post("/api/auth/2fa/enable", secret=secret, code=_code(secret)).status_code, 400)
        self.assertEqual(self.client.get("/api/auth/2fa", headers=self.h).json()["enabled"], False)
        # 口令对、动态口令错：也不写库。
        bad = "000000" if _code(secret) != "000000" else "111111"
        self.assertEqual(self._post("/api/auth/2fa/enable", password=PW, secret=secret, code=bad).status_code, 400)
        self.assertEqual(dbm.totp_state(self.user), "", "没验中的口令不能写库")
        self.assertEqual(self._post("/api/auth/2fa/enable", password=PW, secret=secret, code=_code(secret)).status_code,
                         200)
        self.assertEqual(dbm.totp_state(self.user), totp.normalize_secret(secret))
        self.assertEqual(self.client.get("/api/auth/2fa", headers=self.h).json()["enabled"], True)

    def test_enable_rejects_a_broken_secret(self):
        r = self._post("/api/auth/2fa/enable", password=PW, secret="!!!", code="123456")
        self.assertEqual(r.status_code, 400, r.text[:200])
        self.assertEqual(r.json()["error"], "密钥不合法，请重新生成后再扫")

    def test_enable_on_an_already_enabled_account_is_refused(self):
        self._enable()
        r = self._post("/api/auth/2fa/enable", password=PW, secret=totp.new_secret(), code="123456")
        self.assertEqual(r.status_code, 400, r.text[:200])

    def test_setup_on_an_already_enabled_account_is_refused(self):
        self._enable()
        r = self._post("/api/auth/2fa/setup", password=PW)
        self.assertEqual(r.status_code, 400, r.text[:200])

    def test_the_secret_never_comes_back_out(self):
        """除了 setup 那一次（用户必须拿它去扫码），任何接口都不回显密钥——
        包括开启/关闭自己的响应、状态查询、用户列表，以及审计表的 detail。"""
        secret = self._post("/api/auth/2fa/setup", password=PW).json()["secret"]
        enable = self._post("/api/auth/2fa/enable", password=PW, secret=secret, code=_code(secret))
        self.assertEqual(enable.status_code, 200, enable.text[:200])
        self._next_window()  # 关闭要用另一枚口令，否则这里检查到的是被拒分支的响应体
        for label, resp in (
            ("enable", enable),
            ("status", self.client.get("/api/auth/2fa", headers=self.h)),
            ("users", self.client.get("/api/users", headers=_auth(self.admin, "admin"))),
            ("disable", self._post("/api/auth/2fa/disable", password=PW, code=_code(secret))),
        ):
            with self.subTest(endpoint=label):
                self.assertNotIn(secret, resp.text)

        rows = dbm.query("SELECT action,detail FROM audit WHERE username=?", (self.user,))
        self.assertNotIn(secret, json.dumps([r["detail"] for r in rows], ensure_ascii=False))
        self.assertTrue(any(r["action"] == "twofa_enable" for r in rows), rows)

    def test_disable_needs_password_and_current_code(self):
        self._enable()
        self._next_window()
        self.assertEqual(self._post("/api/auth/2fa/disable", code="123456").status_code, 400)
        self.assertEqual(self._post("/api/auth/2fa/disable", password=PW, code="111111").status_code, 400)
        self.assertNotEqual(dbm.totp_state(self.user), "", "被拒的关闭不许生效")
        r = self._post("/api/auth/2fa/disable", password=PW, code=_code(dbm.totp_state(self.user)))
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(dbm.totp_state(self.user), "")

    def test_disable_names_the_escape_hatch(self):
        """手机不在手边时，页面上唯一该出现的下一句是 CLI 逃生口，不是"再试一次"。"""
        self._enable()
        r = self._post("/api/auth/2fa/disable", password=PW, code="111111")
        self.assertIn("choyeonctl user 2fa-disable", r.json()["error"])

    def test_disable_on_an_account_without_2fa_says_so(self):
        r = self._post("/api/auth/2fa/disable", password=PW, code="123456")
        self.assertEqual(r.json()["error"], "该账号没有开启两步验证")

    def test_it_is_the_account_in_the_token_not_the_name_in_the_body(self):
        """自助端点只看 token 的 sub：body 里塞别人的用户名不该动别人的第二因子。"""
        other = _mkuser("admin")
        self.addCleanup(_deluser, other)
        secret = totp.new_secret()
        self.assertEqual(self._post("/api/auth/2fa/enable", password=PW, secret=secret,
                                    code=_code(secret), username=other).status_code, 200)
        self.assertEqual(dbm.totp_state(self.user), totp.normalize_secret(secret))
        self.assertEqual(dbm.totp_state(other), "")

    def test_admin_users_list_exposes_only_the_boolean(self):
        self._enable()
        rows = self.client.get("/api/users", headers=_auth(self.admin, "admin")).json()
        mine = next(r for r in rows if r["username"] == self.user)
        self.assertTrue(mine["totp"])
        self.assertNotIn("totp_secret", mine, "列表里只该有开没开，不该有密钥")

    def test_login_page_still_gets_a_token_after_the_code(self):
        """开完 2FA 后自助端点自己不能被拿来做端到端登录，这里补一条：真实登录链路走通。"""
        self._enable()
        self._next_window()
        r = self.client.post("/api/auth/login",
                             json={"username": self.user, "password": PW, "code": _code(dbm.totp_state(self.user))})
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertIn("token", r.json())


class TestMigration(unittest.TestCase):
    def test_totp_column_comes_from_migrate_not_from_schema(self):
        """老库升级只靠 ALTER：SCHEMA 里不写这一列，全新库也由 migrate 补齐，
        否则"新库有列、老库没列"会在第一条 SELECT totp_secret 的查询上炸开。"""
        src = (BACKEND / "app/database.py").read_text(encoding="utf-8")
        users_block = src.split("CREATE TABLE IF NOT EXISTS users")[1].split(");")[0]
        self.assertNotIn("totp_secret", users_block)
        self.assertIn('if "totp_secret" not in cols', src)

    def test_migrate_roundtrip_on_an_old_database(self):
        try:
            dbm.execute("ALTER TABLE users DROP COLUMN totp_secret")
        except sqlite3.OperationalError as e:
            self.skipTest(f"这台机器的 sqlite 不支持 DROP COLUMN：{e}")
        cols = {r["name"] for r in dbm.query("PRAGMA table_info(users)")}
        self.assertNotIn("totp_secret", cols)
        dbm.migrate()
        cols = {r["name"] for r in dbm.query("PRAGMA table_info(users)")}
        self.assertIn("totp_secret", cols)
        # 迁移前建立的账号默认就是"没开"，而不是 NULL——判据只有 `<>''` 这一条。
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        self.assertEqual(dbm.totp_state(u), "")

    def test_totp_helpers_tolerate_a_missing_account(self):
        self.assertEqual(dbm.totp_state(_uid("ghost")), "")
        self.assertEqual(dbm.set_totp(_uid("ghost"), "ABCDEFGHJKLM"), 0, "rowcount 不是 lastrowid")


class TestDeleteResidue(unittest.TestCase):
    def test_users_delete_clears_the_used_step_bucket(self):
        """⑤L 的同名继承口径：步数桶按用户名记，删号不清就会被下一个同名账号继承。"""
        u = _mkuser("viewer")
        uid = dbm.query_one("SELECT id FROM users WHERE username=?", (u,))["id"]
        totp.note_used(u, totp.current_step())
        resp = asyncio.run(extras.users_delete(str(uid), _req(role="admin", sub="admin")))
        self.assertEqual(_resp(resp)[0], 200)
        self.assertNotIn(u, totp._used)


class TestLoginAuditFinding(unittest.TestCase):
    """面板账号的第二因子覆盖面要出现在 ssh-audit 的判词里：同一扇门的两层加固一屏看全。"""

    SSH = {"source": "sshd -T", "effective": True, "notes": [],
           "values": {"passwordauthentication": ["no"], "permitrootlogin": ["prohibit-password"],
                      "pubkeyauthentication": ["yes"], "port": ["22"], "listenaddress": ["0.0.0.0:22"]}}
    KEYS = {"files": [], "scanned": 0, "total_keys": 0, "truncated": False}
    ACC = {"login_shells": ["root"], "login_count": 1, "uid0_extra": [], "empty_password": [],
           "shadow_unreadable": False, "sudo_groups": {}, "notes": []}
    ATT = {"ok": True, "window": "24h", "failed": 0, "distinct_ips": 0, "top": [], "accepted": [], "note": ""}
    GUARD = {"units": [], "any_active": True, "note": ""}
    EXP = {"port": 22, "reachable": False, "verdict": "", "reason": ""}

    @staticmethod
    def _panel(accounts=3, with_totp=0, recent_failed=0, ok=True):
        return {"ok": ok, "accounts": accounts, "with_totp": with_totp, "recent_failed": recent_failed, "note": ""}

    def _find(self, panel):
        return la._findings(self.SSH, self.KEYS, self.ACC, self.ATT, self.GUARD, self.EXP, panel)

    def _one(self, panel, fid="panel-2fa"):
        return next(f for f in self._find(panel) if f["id"] == fid)

    def test_older_callers_without_the_panel_arg_still_work(self):
        """挂载点是新增的可选参数：不传时不判，而不是崩或粉饰成"没问题"。"""
        self.assertNotIn("panel-2fa", [f["id"] for f in self._find(None)])

    def test_no_data_is_not_ok(self):
        self.assertEqual([f["id"] for f in self._find(None) if f["id"].startswith("panel")], [])
        broken = self._panel(ok=False) | {"recent_failed": 0}
        self.assertEqual([f["id"] for f in self._find(broken) if f["id"].startswith("panel")], [])
        # 一个账号都没有（还没建管理员）时不该喊"没人开 2FA"。
        self.assertEqual([f["id"] for f in self._find(self._panel(accounts=0)) if f["id"].startswith("panel")], [])

    def test_severity_ladder(self):
        self.assertEqual(self._one(self._panel())["severity"], "note", "没人在撞门时这只是加固建议")
        busy = self._one(self._panel(recent_failed=la.PANEL_BRUTE_RISK - 1))
        self.assertEqual(busy["severity"], "warn")
        risk = self._one(self._panel(recent_failed=la.PANEL_BRUTE_RISK))
        self.assertEqual(risk["severity"], "risk", "有人反复试口令时第二因子是唯一还挡得住的一层")
        partial = self._one(self._panel(with_totp=1), "panel-2fa-partial")
        self.assertEqual(partial["severity"], "note")
        self.assertEqual([f["id"] for f in self._find(self._panel(with_totp=3)) if f["id"].startswith("panel")], [])

    def test_the_text_quotes_the_single_source_numbers(self):
        """限速数字来自 config，位数来自 totp：文案里不许有第二份。"""
        f = self._one(self._panel(recent_failed=7))
        self.assertIn(str(config.LOGIN_LIMIT), f["detail"])
        self.assertIn(str(config.LOGIN_WINDOW_SEC), f["detail"])
        self.assertIn("7 次", f["detail"])
        self.assertIn("choyeonctl user list", f["fix"])

    def test_panel_2fa_reads_only_counts(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        secret = totp.new_secret()
        dbm.set_totp(u, secret)
        d = la._panel_2fa()
        self.assertTrue(d["ok"], d)
        self.assertGreaterEqual(d["accounts"], 1)
        self.assertGreaterEqual(d["with_totp"], 1)
        self.assertNotIn(secret, json.dumps(d, ensure_ascii=False))

    def test_panel_2fa_counts_recent_failures(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        before = la._panel_2fa()["recent_failed"]
        dbm.audit(u, "login_totp_missing", "10.0.0.1")
        dbm.audit(u, "login_failed", "10.0.0.1")
        self.assertEqual(la._panel_2fa()["recent_failed"], before + 2)
        # 成功登录与开关 2FA 的记录不算"撞门"。
        dbm.audit(u, "login", "10.0.0.1")
        dbm.audit(u, "twofa_enable", "两步验证已开启")
        self.assertEqual(la._panel_2fa()["recent_failed"], before + 2)

    def test_collect_wires_the_panel_layer_in(self):
        """服务算好了但没人调用，等于没做：源码级钉住 _collect 把 panel 传进判词。"""
        src = (BACKEND / "app/services/login_audit_service.py").read_text(encoding="utf-8")
        body = src.split("async def _collect", 1)[1]
        self.assertIn("asyncio.to_thread(_panel_2fa)", body, "同步 sqlite 调用不能留在事件循环上")
        self.assertIn("guard, exposure, panel)", body)
        self.assertIn('"panel": panel', body)


class TestFailureActionRegistry(unittest.TestCase):
    """login_audit_service 按 LOGIN_FAILED_ACTIONS 统计撞门次数，写这些名字的是 main.py。
    两处各写一遍的结局是加一条失败分支忘了登记（判词少算），或多列一个不存在的名字（判词多算）。"""

    @staticmethod
    def _audited_login_actions() -> set[str]:
        src = (BACKEND / "app/main.py").read_text(encoding="utf-8")
        return {m for line in src.splitlines() if ".audit(" in line for m in re.findall(r'"(login[a-z_]*)"', line)}

    def test_registry_matches_the_login_route(self):
        self.assertEqual(self._audited_login_actions(),
                         set(security.LOGIN_FAILED_ACTIONS) | {"login", "login_rejected"})

    def test_every_registered_action_is_written(self):
        for action in security.LOGIN_FAILED_ACTIONS:
            self.assertIn(f'"{action}"', (BACKEND / "app/main.py").read_text(encoding="utf-8"),
                          f"登记表里有 {action}，登录路由却从来不写它")


class TestCliEscapeHatch(unittest.TestCase):
    def setUp(self):
        import cli

        self.cli = cli
        self.parser = cli.build_parser()

    def _run(self, argv):
        ns = self.parser.parse_args(argv)
        captured, err, out = {}, io.StringIO(), io.StringIO()

        def fake_result(data, text=""):
            captured["data"] = data
            captured["text"] = text

        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
             mock.patch.object(self.cli.Out, "result", staticmethod(fake_result)):
            try:
                asyncio.run(self.cli.cmd_user(ns))
            except SystemExit as e:
                captured["exit"] = e.code
        return captured, err.getvalue(), out.getvalue()

    def test_yes_is_required(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        dbm.set_totp(u, totp.new_secret())
        res, _, _ = self._run(["user", "2fa-disable", u])
        self.assertEqual(res.get("exit"), self.cli.EXIT_USAGE, res)
        self.assertNotEqual(dbm.totp_state(u), "", "没确认就不许动")
        res, _, _ = self._run(["user", "2fa-disable", u, "--yes"])
        self.assertTrue(res["data"].get("ok"), res)
        self.assertEqual(dbm.totp_state(u), "")

    def test_it_revokes_the_sessions_too(self):
        """跑这条命令的原因是"手机不在手边"，而这种时候并不确定手里有票的是不是本人。"""
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        dbm.set_totp(u, totp.new_secret())
        token = security.sign_token(u, "admin")
        res, _, _ = self._run(["user", "2fa-disable", u, "--yes"])
        self.assertTrue(res["data"]["revokedSessions"], res)
        with self.assertRaises(Exception):
            security.verify_token(token)

    def test_nothing_to_disable_fails_instead_of_lying_about_success(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        res, _, _ = self._run(["user", "2fa-disable", u, "--yes"])
        self.assertEqual(res.get("exit"), self.cli.EXIT_FAIL, res)

    def test_missing_name_and_all_are_usage_errors(self):
        res, _, _ = self._run(["user", "2fa-disable"])
        self.assertEqual(res.get("exit"), self.cli.EXIT_USAGE, res)
        res, _, _ = self._run(["user", "2fa-disable", "all", "--yes"])
        self.assertEqual(res.get("exit"), self.cli.EXIT_USAGE, "逃生口一次只关一个账号")

    def test_unknown_account_fails(self):
        res, _, _ = self._run(["user", "2fa-disable", _uid("ghost"), "--yes"])
        self.assertEqual(res.get("exit"), self.cli.EXIT_FAIL, res)

    def test_it_leaves_an_audit_trail(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        dbm.set_totp(u, totp.new_secret())
        self._run(["user", "2fa-disable", u, "--yes"])
        rows = dbm.query("SELECT action FROM audit WHERE username=? AND action LIKE 'cli:%'", (u,))
        self.assertIn("cli:user:2fa-disable", [r["action"] for r in rows])

    def test_list_shows_the_state_and_never_the_secret(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        secret = totp.new_secret()
        dbm.set_totp(u, secret)
        res, _, _ = self._run(["user", "list"])
        # text 取捕获的第二个参数：Out.result 被替换掉了，stdout 是空的，而这段文本正是
        # "贴进工单、被拍照"的那一份——密钥泄露就发生在这里，不是发生在 JSON 结构里。
        mine = next(r for r in res["data"] if r["username"] == u)
        self.assertTrue(mine["totp"], mine)
        self.assertNotIn("totp_secret", mine)
        self.assertIn("2fa", res["text"])
        self.assertNotIn(secret, res["text"])
        self.assertNotIn(secret, json.dumps(res))

    def test_contract_is_stated_in_three_places(self):
        """schema、AGENTS.md、argparse 必须说同一批命令（智能体只读文档那张表）。"""
        import cli

        ns = self.parser.parse_args(["user", "2fa-disable", "somebody", "--yes"])
        self.assertEqual(ns.user_cmd, "2fa-disable")
        schema = asyncio.run(cli.cmd_schema(SimpleNamespace()))
        entry = next(c for c in schema["commands"] if c["name"].startswith("user "))
        self.assertIn("2fa-disable", entry["name"])
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in agents.splitlines() if ln.startswith("| `user list"))
        self.assertIn("2fa-disable", row)


class TestFrontendContract(unittest.TestCase):
    """位数/口令长度这些数字只有一个出处：前端拿响应里的 digits，不自己抄。"""

    def _read(self, rel: str) -> str:
        return (ROOT / "web/src" / rel).read_text(encoding="utf-8")

    def test_login_page_takes_the_digit_count_from_the_response(self):
        src = self._read("views/Login.vue")
        # naive-ui 的 NInput 把原生属性收在 :input-props 对象里，所以这里断言对象字面量形态，
        # 写成模板上的静态 autocomplete="…" 反而永远传不进 <input>。
        self.assertRegex(src, r"""autocomplete:\s*['"]one-time-code['"]""", "密码管理器该自动填验证码")
        self.assertRegex(src, r"""inputmode:\s*['"]numeric['"]""", "移动端要弹数字键盘")
        self.assertNotIn('maxlength="6"', src, "位数要来自响应的 digits，不能写死 6")
        # maxlength 必须是 NInput 上的 :maxlength，不能塞进 :input-props：线上探针实测
        # 后一种写法既没落成 attribute 也没落成 el.maxLength（一直是 -1），上限静默失效。
        self.assertRegex(src, r':maxlength="digits \|\| undefined"', "输入框的位数没取后端下发的 digits")
        self.assertNotIn("maxlength: digits", src, "input-props 里的 maxlength 到不了原生 <input>")
        self.assertIn("totp_required", src, "登录页要认得出「这一步该要口令」的分支")
        self.assertIn("totp_invalid", src, "口令不对要留在这个框里重试，不能退回口令帧")

    def test_api_carries_the_code_and_keeps_the_error_code(self):
        src = self._read("api.ts")
        self.assertRegex(src, r"login:\s*\(username: string, password: string, code", "login 的签名要带第三个参数 code")
        self.assertRegex(src, r"\.code\s*=", "parse() 要把后端的 code 挂到 Error 上，否则前端只能看文案猜")
        for path in ("/auth/2fa", "/auth/2fa/setup", "/auth/2fa/enable", "/auth/2fa/disable"):
            self.assertIn(path, src, f"api.ts 缺 {path}")

    def test_settings_page_defers_the_qr_library(self):
        src = self._read("views/Settings.vue")
        # 仓库的 TS/Vue 风格是单引号，这里两种都认：断言的是"动态 import"，不是引号样式。
        self.assertRegex(src, r"""import\(['"]qrcode['"]\)""", "二维码库只在开 2FA 时用得上，不该进首屏 chunk")
        self.assertNotIn('from "qrcode"', src)
        self.assertNotIn("from 'qrcode'", src)
        # vendor 里装着 @vicons（icons.ts 静态引用），首屏就要下载：qrcode 必须另成一块，
        # 否则"动态 import"只是写法上的懒，下载时机一点没变。
        cfg = (ROOT / "web/vite.config.ts").read_text(encoding="utf-8")
        self.assertRegex(cfg, r"return ['\"]qrcode['\"]", "manualChunks 没把 qrcode 单独分块")

    def test_users_page_shows_the_column(self):
        src = self._read("views/Users.vue")
        self.assertRegex(src, r"\btotp\b")

    def test_the_code_field_only_appears_when_it_can_be_filled(self):
        """抽屉第一步（核口令）时验证器里还没有这个账号，那时摆一个动态口令框就是让人填错。

        这条断言盯的是模板上的 v-if：上次走查就是靠肉眼发现框和"密钥只给一次"的提示
        一起出现在口令帧上，而按钮还写着"生成密钥"。
        """
        src = self._read("views/Settings.vue")
        self.assertRegex(
            src,
            r"""v-if="twMode === 'disable' \|\| twStep === 'scan'"[\s\S]{0,120}?动态口令""",
            "动态口令框要等到密钥生成完（scan）或关闭流程里才出现",
        )
        self.assertRegex(
            src,
            r"""v-if="twMode === 'enable' && twStep === 'scan'"[\s\S]{0,80}?tw-note""",
            "「绑定后别关抽屉」的提示只在 scan 帧有意义，关闭流程里它是错的",
        )


if __name__ == "__main__":
    unittest.main()
