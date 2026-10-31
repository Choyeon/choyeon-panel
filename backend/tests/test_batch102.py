"""批次102：改密 / 提权降权 / 删号 / 关第二因子与「吊销旧 token」必须落在同一个事务里。

批次100 把「一次保存的多个字段」收进了一次 `execute_many`，但那几处最关键的一条写被留在了
事务外面：`security.bump_user_epoch(...)` 自己 commit。于是每条凭据变更都是两次提交：

- bump 失败（库被别的进程锁到超时、盘满）→ 库里已经改成新状态，而旧票在 TTL 内（最长 72h）
  照用：管理员被降成 viewer 却仍是 admin、被重置口令的人手里那张票还能开网页终端拿 root shell；
- UPDATE 失败而 bump 成功 → 口令还是旧的，人却被踢下线，重登时用新口令进不去；
- 删号没写进 `token_epoch:<name>` 墓碑 → 这个名字转给下一个人时旧票的 ver 又和
  `_user_epoch(name)` 相等，离职管理员权限原地复活（test_hardening 那组实测过）。

而且 CLI 当场把 `revokedTokens: True` / `revokedSessions: True` 打成结果——它说的是"两件事都做完了"，
在两次提交的写法下这句话本来就不可信。

这一批把那一条写并进调用方已有的事务里（`security.user_epoch_stmt` + `dbm.execute_many(...,
invalidate=…)`），并钉住三件事：改动与吊销同生同死、缓存和库在同一次持锁里对齐（否则刚吊销的
票还要在 `_auth_cache` 里活满 TTL），以及键名 `token_epoch:<user>` 只有一个拼法。
"""

import asyncio
import io
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

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp102_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp102_roots_"))

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.routers import extras  # noqa: E402

OLD_PW = "OldPass-102a"
NEW_PW = "NewPass-102b"


def _req(body: dict, sub: str = "boss", role: str = "admin"):
    async def _json():
        return body

    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub),
                           json=_json, headers={}, client=SimpleNamespace(host="ut"))


def _mk_user(name: str, role: str = "viewer", pw: str = OLD_PW) -> int:
    dbm.execute("DELETE FROM users WHERE username=?", (name,))
    dbm.execute("DELETE FROM settings WHERE key=?", (security.user_epoch_key(name),))
    return dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                       (name, security.hash_pass(pw), role))


class _FlakyConn:
    """包一层真连接：语句或参数里含 needle 的那条写直接失败，其余原样转发（同批次100）。"""

    def __init__(self, real, needle: str):
        self._real, self._needle = real, needle

    def execute(self, sql, params=()):
        if self._needle in sql or self._needle in str(params):
            raise sqlite3.OperationalError("模拟：database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _flaky(needle: str):
    """needle 用 "token_epoch"：只有那条吊销写会失败，被它保护的凭据写照常走到 commit 前。"""
    return mock.patch.object(dbm, "conn", _FlakyConn(dbm.conn, needle))


def _epoch_in_db(name: str):
    return dbm._read_setting(security.user_epoch_key(name))


class DbLayerInvalidateTests(unittest.TestCase):
    """底层先证明两件事：epoch 写坏了整体回滚，写好了缓存立刻对齐。"""

    def test_吊销那条失败时改动一起回滚(self):
        name = "cp102_rollback"
        uid = _mk_user(name)
        before = dbm.query_one("SELECT pass_hash FROM users WHERE id=?", (uid,))["pass_hash"]
        with _flaky("token_epoch"), self.assertRaises(sqlite3.OperationalError):
            dbm.execute_many([
                ("UPDATE users SET pass_hash=? WHERE id=?", (security.hash_pass(NEW_PW), uid)),
                security.user_epoch_stmt(name),
            ], (security.user_epoch_key(name),))
        self.assertEqual(dbm.query_one("SELECT pass_hash FROM users WHERE id=?", (uid,))["pass_hash"],
                         before, "口令没写成，角色/版本号那条却回滚了——必须整体不算")
        self.assertIsNone(_epoch_in_db(name))

    def test_invalidate在提交之后掉缓存(self):
        name = "cp102_cache"
        _mk_user(name)
        key = security.user_epoch_key(name)
        dbm.get_setting(key)  # 先让旧值（None）进缓存
        dbm._auth_cache[key] = ("stale-epoch", time.monotonic())
        dbm.execute_many([security.user_epoch_stmt(name)], (key,))
        self.assertNotIn(key, dbm._auth_cache, "缓存没跟着这次提交失效：刚吊销的票还要活满 TTL")
        self.assertTrue(_epoch_in_db(name))

    def test_回滚时不碰缓存(self):
        name = "cp102_cache_fail"
        _mk_user(name)
        key = security.user_epoch_key(name)
        dbm._auth_cache[key] = ("stale-epoch", time.monotonic())
        with _flaky("token_epoch"), self.assertRaises(sqlite3.OperationalError):
            dbm.execute_many([security.user_epoch_stmt(name)], (key,))
        self.assertEqual(dbm._auth_cache[key][0], "stale-epoch",
                         "库被回滚了却把缓存清了：下一次读会回填旧值，等于白忙，还把窗口拉长")


class SecurityStmtTests(unittest.TestCase):
    def test_键名只有一个拼法(self):
        self.assertEqual(security.user_epoch_key("bob"), "token_epoch:bob")
        for rel in ("app/main.py", "app/routers/extras.py", "cli.py"):
            src = (BACKEND / rel).read_text(encoding="utf-8")
            self.assertNotIn('"token_epoch:', src, f"{rel} 自己拼了 epoch 键名")
            self.assertNotIn("f'token_epoch:", src, f"{rel} 自己拼了 epoch 键名")

    def test_stmt本身不落库(self):
        name = "cp102_stmt_only"
        _mk_user(name)
        before = _epoch_in_db(name)
        sql, params = security.user_epoch_stmt(name)
        self.assertIn("INSERT INTO settings", sql)
        self.assertEqual(params[0], security.user_epoch_key(name))
        self.assertEqual(_epoch_in_db(name), before, "组语句的函数不该顺手把库写了")

    def test_并进事务后旧票当场失效(self):
        name = "cp102_revoke_ok"
        _mk_user(name, role="admin")
        token = security.sign_token(name, "admin")
        security.verify_token(token)  # 起点：这张票是有效的
        # 故意把缓存灌成"刚读过的旧 epoch"：只有 invalidate 在同一次持锁里生效，
        # 这个请求返回后才能立刻挡住旧票，而不是等 3 秒 TTL 到点。
        dbm._auth_cache[security.user_epoch_key(name)] = (
            str(dbm.get_setting(security.user_epoch_key(name))), time.monotonic())
        dbm.execute_many([
            ("UPDATE users SET role=? WHERE username=?", ("viewer", name)),
            security.user_epoch_stmt(name),
        ], (security.user_epoch_key(name),))
        with self.assertRaises(Exception):
            security.verify_token(token)


class EndpointAtomicTests(unittest.TestCase):
    def test_改密时吊销失败则口令没改(self):
        name = "cp102_pwd_fail"
        _mk_user(name, role="admin")
        before = dbm.query_one("SELECT pass_hash FROM users WHERE username=?", (name,))["pass_hash"]
        with _flaky("token_epoch"), self.assertRaises(sqlite3.OperationalError):
            asyncio.run(main.change_password(
                _req({"old": OLD_PW, "new": NEW_PW}, sub=name)))
        self.assertEqual(dbm.query_one("SELECT pass_hash FROM users WHERE username=?", (name,))["pass_hash"],
                         before, "半套：新口令已经落库，旧 token 却还有效（bump 失败了）")
        self.assertIsNone(_epoch_in_db(name))

    def test_改密成功后旧票当场失效(self):
        name = "cp102_pwd_ok"
        _mk_user(name, role="admin")
        token = security.sign_token(name, "admin")
        r = asyncio.run(main.change_password(_req({"old": OLD_PW, "new": NEW_PW}, sub=name)))
        self.assertIn("token", r)
        self.assertTrue(security.check_pass(NEW_PW, dbm.query_one(
            "SELECT pass_hash FROM users WHERE username=?", (name,))["pass_hash"]))
        with self.assertRaises(Exception):
            security.verify_token(token)

    def test_提权时吊销失败则角色没改(self):
        name = "cp102_demote_fail"
        uid = _mk_user(name, role="viewer")
        with _flaky("token_epoch"), self.assertRaises(sqlite3.OperationalError):
            asyncio.run(extras.users_update(str(uid), _req({"role": "admin"}, sub="boss")))
        self.assertEqual(dbm.query_one("SELECT role FROM users WHERE id=?", (uid,))["role"], "viewer",
                         "半套：提权落库了而旧票没吊销，反过来降权失败时就是「人已降但仍是 admin」")

    def test_改角色成功即吊销(self):
        name = "cp102_demote_ok"
        uid = _mk_user(name, role="admin")
        token = security.sign_token(name, "admin")
        r = asyncio.run(extras.users_update(str(uid), _req({"role": "viewer"}, sub="boss")))
        self.assertTrue(r["ok"])
        with self.assertRaises(Exception):
            security.verify_token(token)

    def test_只改昵称不写epoch(self):
        name = "cp102_nickname"
        uid = _mk_user(name, role="viewer")
        token = security.sign_token(name, "viewer")
        r = asyncio.run(extras.users_update(str(uid), _req({"nickname": "小张"}, sub="boss")))
        self.assertTrue(r["ok"])
        self.assertIsNone(_epoch_in_db(name), "改个显示名不该把人踢下线")
        security.verify_token(token)

    def test_删号时吊销失败则行还在(self):
        name = "cp102_del_fail"
        uid = _mk_user(name, role="viewer")
        with _flaky("token_epoch"), self.assertRaises(sqlite3.OperationalError):
            asyncio.run(extras.users_delete(str(uid), _req({}, sub="boss")))
        self.assertIsNotNone(dbm.query_one("SELECT id FROM users WHERE id=?", (uid,)),
                             "行删了而墓碑没写：这个名字转给下一个人，旧 admin 票又会验得过")

    def test_删号成功留下epoch墓碑(self):
        name = "cp102_del_ok"
        uid = _mk_user(name, role="admin")
        r = asyncio.run(extras.users_delete(str(uid), _req({}, sub="boss")))
        self.assertTrue(r["ok"])
        self.assertIsNone(dbm.query_one("SELECT id FROM users WHERE id=?", (uid,)))
        self.assertTrue(_epoch_in_db(name))


class CliAtomicTests(unittest.TestCase):
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
            except sqlite3.OperationalError as e:
                captured["raise"] = str(e)
        return captured, err.getvalue(), out.getvalue()

    def test_reset时吊销失败则口令没改(self):
        name = "cp102_cli_reset"
        _mk_user(name, role="admin")
        before = dbm.query_one("SELECT pass_hash FROM users WHERE username=?", (name,))["pass_hash"]
        with _flaky("token_epoch"):
            res, _, _ = self._run(["user", "reset", name, "--password", NEW_PW])
        self.assertIn("raise", res, "CLI 把失败咽掉了：它照常打印了 revokedTokens:true")
        self.assertEqual(dbm.query_one("SELECT pass_hash FROM users WHERE username=?", (name,))["pass_hash"],
                         before, "半套：新口令生效而旧票照用")

    def test_reset成功时同时吊销(self):
        name = "cp102_cli_reset_ok"
        _mk_user(name, role="admin")
        token = security.sign_token(name, "admin")
        res, _, _ = self._run(["user", "reset", name, "--password", NEW_PW])
        self.assertTrue(res["data"]["revokedTokens"], res)
        self.assertTrue(_epoch_in_db(name))
        with self.assertRaises(Exception):
            security.verify_token(token)

    def test_关第二因子时吊销失败则密钥还在(self):
        from app import totp

        name = "cp102_cli_2fa"
        _mk_user(name, role="admin")
        secret = totp.new_secret()
        dbm.set_totp(name, secret)
        with _flaky("token_epoch"):
            res, _, _ = self._run(["user", "2fa-disable", name, "--yes"])
        self.assertIn("raise", res)
        self.assertEqual(dbm.totp_state(name), secret,
                         "半套：第二因子已经关掉，而不确定是不是本人的那张票还在用")

    def test_关第二因子成功时同时吊销会话(self):
        from app import totp

        name = "cp102_cli_2fa_ok"
        _mk_user(name, role="admin")
        dbm.set_totp(name, totp.new_secret())
        token = security.sign_token(name, "admin")
        res, _, _ = self._run(["user", "2fa-disable", name, "--yes"])
        self.assertTrue(res["data"]["revokedSessions"], res)
        self.assertEqual(dbm.totp_state(name), "")
        with self.assertRaises(Exception):
            security.verify_token(token)

    def test_删号时吊销失败则行还在(self):
        name = "cp102_cli_del"
        uid = _mk_user(name, role="admin")
        with _flaky("token_epoch"):
            res, _, _ = self._run(["user", "delete", name, "--yes"])
        self.assertIn("raise", res)
        self.assertIsNotNone(dbm.query_one("SELECT id FROM users WHERE id=?", (uid,)))
        dbm.execute("DELETE FROM users WHERE id=?", (uid,))

    def test_删号成功留下墓碑(self):
        name = "cp102_cli_del_ok"
        _mk_user(name, role="admin")
        res, _, _ = self._run(["user", "delete", name, "--yes"])
        self.assertTrue(res["data"]["ok"], res)
        self.assertTrue(_epoch_in_db(name))


class NoStandaloneBumpTests(unittest.TestCase):
    """「改完再单独 bump」这个写法本身要消失，否则下一个改凭据的地方会照抄它。"""

    # 只许这两处单独 bump：logout 与 `user revoke` 的这次写就是吊销本身，没有别的字段要并。
    ALLOWED = {"app/main.py": 1, "app/routers/extras.py": 0, "cli.py": 1}

    def _body(self, rel, func):
        src = (BACKEND / rel).read_text(encoding="utf-8")
        m = re.search(rf"(async )?def {func}\(", src)
        self.assertIsNotNone(m, f"{rel} 里找不到 {func}")
        rest = src[m.end():]
        cut = re.search(r"\n(?:@|(async )?def )", rest)
        return rest[: cut.start() if cut else len(rest)]

    def test_bump_user_epoch只剩登出与应急吊销两处调用(self):
        for rel, want in self.ALLOWED.items():
            src = (BACKEND / rel).read_text(encoding="utf-8")
            got = len(re.findall(r"security\.bump_user_epoch\(", src))
            self.assertEqual(got, want, f"{rel} 里还有 {got} 处单独 bump（应为 {want}）")

    def test_每个改凭据的地方都把吊销并进了同一批(self):
        for rel, func in (("app/main.py", "change_password"),
                          ("app/routers/extras.py", "users_update"),
                          ("app/routers/extras.py", "users_delete"),
                          ("cli.py", "cmd_user")):
            body = self._body(rel, func)
            if func == "cmd_user":
                # CLI 三个子命令各自成批：整段里出现三次即可，不去数嵌套
                self.assertEqual(body.count("security.user_epoch_stmt("), 3, rel)
                self.assertEqual(body.count("dbm.execute_many(["), 3, rel)
                continue
            self.assertIn("security.user_epoch_stmt(", body, f"{rel}::{func} 没把吊销写进这一批")
            self.assertIn("execute_many(", body, f"{rel}::{func} 还在逐条写")
            self.assertNotIn("bump_user_epoch(", body, f"{rel}::{func} 还在用单独提交的那次 bump")

    def test_aio层把invalidate透传下去(self):
        src = (BACKEND / "app" / "aio_db.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"async def execute_many\(stmts, invalidate=\(\)\)")
        self.assertIn("dbm.execute_many, stmts, tuple(invalidate)", src)
