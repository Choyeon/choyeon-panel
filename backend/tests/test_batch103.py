"""批次103：第二因子一换，已签发的会话必须一起处置——而且和写库同一次事务。

网页自助开/关两步验证原来只做一半：`dbm.set_totp()` 写完 totp_secret 就返回。库里从今天起
要动态口令，可这个人**正是因为怀疑口令泄露才来开 2FA** 的——他手里那张偷来的旧票在 TTL 内
（最长 72h）照旧只用口令就能进，加固对唯一需要它的会话没生效；而界面只说「下次登录起需要
动态口令」，谁也不会去查还有哪些票活着。同一台机器上的 `choyeonctl user 2fa-disable` 却是
连会话一起吊销的：同一件事两条路两个判据。

现在规则只有一条：第二因子的状态一变（不论方向），该账号已签发的全部 token 作废，本浏览器
由响应里换发的新 token 继续用。写密钥与轮换 token_epoch 走 `execute_many` 同一次事务，认证
缓存在同一次持锁里失效——分开 commit 的中间失败会留下「密钥已经写上、旧票照用」或反过来
「票吊销了、库里还是旧状态」，两种都是现场看不出来的半套。

顺带删掉 `dbm.set_totp()` 这个单条入口：留着它，下一处调用就能只写密钥不吊销，而这正是本批
要根治的形状。`execute_many` 因此要按语句顺序交出 rowcount（端点拿它判「那一行还在不在」）。
"""

import asyncio
import itertools
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp103_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp103_roots_"))

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import database as dbm  # noqa: E402
from app import main, security, totp  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PW = "Password123"
_seq = itertools.count()


def _uid() -> str:
    return f"cp103_{os.getpid() % 100000}{next(_seq)}"


def _resp(resp) -> tuple[int, dict]:
    """直调路由时，失败分支是 JSONResponse、成功分支是裸 dict，这里统一成 (状态码, 数据)。"""
    if hasattr(resp, "body"):
        return resp.status_code, json.loads(resp.body.decode())
    return 200, resp


def _code(secret: str, delta: int = 0) -> str:
    """一枚属于「当前步 + delta」的动态口令。

    `delta=1` 是给「同一个测试里连着验两次」用的：verify 的口径是步数严格递增，
    开 2FA 用掉当前窗口之后，紧接着关 2FA 必须拿下一窗口的口令，否则被拒的是
    重放保护（对的），而不是本批要验的吊销逻辑。
    """
    return totp.hotp(totp.normalize_secret(secret), totp.current_step(time.time()) + delta)


def _req(body: dict, sub: str, role: str = "viewer"):
    async def _json():
        return body

    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub),
                           json=_json, headers={}, client=SimpleNamespace(host="ut"))


def _mkuser(name: str) -> None:
    dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (name, security.hash_pass(PW), "viewer"))


class _FlakyConn:
    """语句或参数里含 needle 的那条写直接失败，其余原样转发（回滚走的还是真连接）。

    needle 用 "token_epoch" 时命中的是吊销那条的**参数**（键名），用 "totp_secret" 时命中的
    是写密钥那条的 SQL——两个方向各失败一次，才能把两种半套分别钉住。
    """

    def __init__(self, real, needle: str):
        self._real, self._needle = real, needle

    def execute(self, sql, params=()):
        if self._needle in sql or self._needle in str(params):
            raise sqlite3.OperationalError("模拟：database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _epoch_in_db(name: str):
    return dbm._read_setting(security.user_epoch_key(name))


class TwofaRevocationTests(unittest.TestCase):
    """开与关都吊销本账号已签发 token，并给这一路换发一枚可用的新票。"""

    def setUp(self):
        totp.reset_used()
        self.addCleanup(totp.reset_used)
        self.name = _uid()
        _mkuser(self.name)
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.name,))
        # 两台设备：token_a 是来开关 2FA 的这一台，token_b 是「另处还登着」的那一台
        self.token_a = security.sign_token(self.name, "viewer")
        self.token_b = security.sign_token(self.name, "viewer")

    def _enable(self):
        secret = asyncio.run(main.twofa_setup(_req({"password": PW}, sub=self.name)))["secret"]
        status, data = _resp(asyncio.run(main.twofa_enable(_req(
            {"password": PW, "secret": secret, "code": _code(secret)}, sub=self.name))))
        self.assertEqual(status, 200, f"开启被拒了：{data}")
        return secret, data

    def test_enable_revokes_sessions_issued_under_the_old_policy(self):
        security.verify_token(self.token_b)  # 动手之前另一台本来就该可用
        _secret, _r = self._enable()
        with self.assertRaises(security.jwt.InvalidTokenError):
            security.verify_token(self.token_b)
        with self.assertRaises(security.jwt.InvalidTokenError):
            security.verify_token(self.token_a)

    def test_enable_hands_this_browser_a_working_token(self):
        secret, r = self._enable()
        new = r["token"]
        self.assertNotIn(secret, str(r), "换发票不该把密钥带回去")
        self.assertNotEqual(new, self.token_a, "新票必须真的重新签发，不是把旧票再念一遍")
        payload = security.verify_token(new)
        self.assertEqual((payload["sub"], payload["role"]), (self.name, "viewer"))
        self.assertEqual(dbm.totp_state(self.name), totp.normalize_secret(secret))

    def test_disable_revokes_sessions_too(self):
        """关闭也是「第二因子换了状态」：手机丢了才来关的人，不该让丢在那台手机上的票继续活着。"""
        secret, r = self._enable()
        h1 = r["token"]
        token_c = security.sign_token(self.name, "viewer")  # 开启之后重新登录拿到的票
        security.verify_token(token_c)
        d = _resp(asyncio.run(
            main.twofa_disable(_req({"password": PW, "code": _code(secret, 1)}, sub=self.name))))
        self.assertEqual(d[0], 200, f"关闭被拒了：{d[1]}")
        self.assertTrue(d[1]["ok"])
        self.assertEqual(dbm.totp_state(self.name), "")
        with self.assertRaises(security.jwt.InvalidTokenError):
            security.verify_token(token_c)
        security.verify_token(d[1]["token"])
        self.assertNotEqual(d[1]["token"], h1)

    def test_旧票在epoch缓存还没过期时就失效(self):
        """token_epoch 的缓存必须在这次事务里掉掉。

        先把旧值喂进缓存（_AUTH_TTL 3 秒，正好是「改完之后紧接着的下一个请求」）：少了
        invalidate，验签读到的还是缓存里那个旧 epoch，另一台设备的票能在改完之后再多活几秒。
        """
        dbm.get_setting(security.user_epoch_key(self.name))  # 这次读把「还没有墓碑」记进缓存
        _secret, _r = self._enable()
        with self.assertRaises(security.jwt.InvalidTokenError):
            security.verify_token(self.token_b)

    def test_audit_records_the_revocation(self):
        secret, _r = self._enable()
        rows = dbm.query("SELECT action,detail FROM audit WHERE username=? ORDER BY id", (self.name,))
        note = next(r["detail"] for r in rows if r["action"] == "twofa_enable")
        self.assertIn("会话全部吊销", note, "审计要留下这次真的吊销了，事后才看得出加固落在哪一步")
        self.assertNotIn(secret, note, "detail 里不许出现密钥")

    def test_denied_enable_revokes_nothing(self):
        """口令不对的分支只是被拒：不能顺手把人的会话吊销掉（那是拒绝服务）。"""
        secret = totp.new_secret()
        status, _body = _resp(asyncio.run(main.twofa_enable(
            _req({"password": PW, "secret": secret, "code": "000000"}, sub=self.name))))
        self.assertEqual(status, 400)
        security.verify_token(self.token_b)
        self.assertEqual(dbm.totp_state(self.name), "")
        self.assertIsNone(_epoch_in_db(self.name))


class AtomicTwofaWritesTests(unittest.TestCase):
    """写密钥与吊销分开提交的那两种半套，这里一次挡掉。"""

    def setUp(self):
        totp.reset_used()
        self.addCleanup(totp.reset_used)
        self.name = _uid()
        _mkuser(self.name)
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.name,))
        self.token = security.sign_token(self.name, "viewer")
        self.secret = asyncio.run(main.twofa_setup(_req({"password": PW}, sub=self.name)))["secret"]

    def _flaky(self, needle):
        return mock.patch.object(dbm, "conn", _FlakyConn(dbm.conn, needle))

    def _enable(self):
        return asyncio.run(main.twofa_enable(_req(
            {"password": PW, "secret": self.secret, "code": _code(self.secret)}, sub=self.name)))

    def test_吊销失败时密钥一条都没写(self):
        before = dbm._read_setting(security.user_epoch_key(self.name))
        with self._flaky("token_epoch"), self.assertRaises(sqlite3.OperationalError):
            self._enable()
        self.assertEqual(dbm.totp_state(self.name), "", "回滚没兜住：密钥落库了而票还在")
        self.assertEqual(dbm._read_setting(security.user_epoch_key(self.name)), before)
        security.verify_token(self.token)

    def test_写密钥失败时旧票照用(self):
        """反方向的半套：只 bump 成功而密钥没写上——人被动了，库里什么都没变。"""
        with self._flaky("totp_secret"), self.assertRaises(sqlite3.OperationalError):
            self._enable()
        self.assertIsNone(_epoch_in_db(self.name), "事务没回滚：epoch 被单独换掉了")
        self.assertEqual(dbm.totp_state(self.name), "")
        security.verify_token(self.token)


class DbRowcountTests(unittest.TestCase):
    """execute_many 交出每条语句真正改动的行数——`set_totp` 删掉后这是唯一的判据。"""

    def test_按语句顺序交出_rowcount(self):
        name = _uid()
        _mkuser(name)
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (name,))
        rows = dbm.execute_many([
            dbm.set_totp_stmt(name, "ABCDEFGHJKLMNPQRSTUV"),
            ("UPDATE users SET nickname=? WHERE username=?", ("小张", name)),
        ])
        self.assertEqual(rows, [1, 1])
        self.assertEqual(dbm.execute_many([dbm.set_totp_stmt(_uid(), "ABCDEFGHJKLMNPQRSTUV")]), [0],
                         "账号不在就是 0 行；端点拿它判「用户不存在」")
        self.assertEqual(dbm.execute_many([]), [])


class NoSingleShotTotpWriterTests(unittest.TestCase):
    """「只写密钥」的入口不许复活；每个改第二因子的地方都得把吊销并进同一批。"""

    def test_database_不再提供单条_set_totp(self):
        self.assertFalse(hasattr(dbm, "set_totp"),
                         "单条写密钥的入口就是本批的缺陷形状：只留 set_totp_stmt + execute_many")

    def test_自助开关与改密都用_user_epoch_stmt(self):
        src = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
        # change_password + twofa_enable + twofa_disable 三处；logout 是唯一「吊销本身就是改动」的地方
        self.assertEqual(src.count("security.user_epoch_stmt("), 3)
        self.assertEqual(src.count("security.bump_user_epoch("), 1, "只有登出还该单独吊销")
        for needle in ('dbm.set_totp(me["username"], secret)', 'dbm.set_totp(me["username"], "")'):
            self.assertNotIn(needle, src, "开关第二因子又回到只写一列了")

    def test_前端接住换发的新_token并转述吊销(self):
        src = (ROOT / "web/src/views/Settings.vue").read_text(encoding="utf-8")
        body = src[src.index("async function commitTwofa"):src.index("async function copyTw")]
        self.assertIn("saveLoginData(r)", body, "新 token 不落地，下一个请求就是 401、终端被 4401 关掉")
        self.assertIn("其它地方的登录态已作废", body, "后端做了的事，界面要转述")
        self.assertIn("第二因子一换", src, "说明句要讲规则，不是只讲「下次登录」")


if __name__ == "__main__":
    unittest.main()
