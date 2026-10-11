"""批次⑤K：告警历史/恢复通知与长连接授权复核。

覆盖这些真实坏法：
1. 存活探测的正常分支直接 `record("ok", …)`：发过「应用无响应」之后永远等不到
   「已恢复」，`notified:probe:<应用>` 也没有任何人清掉（模块约定 5）。
2. 历史去重按 code+level 比：crit → ok → 同一条 crit 复发时一行都不落，
   告警列表最新一行停在「正常」而对象又坏了（约定 4）。
3. housekeeping 用 `prune_alerts()` 的默认值每 10 分钟再裁一次，把管理员可调的
   `alert_keep_days` / `alert_keep_rows` 顶成死配置。
4. 登录/建号/改密/资料的审计是同步 INSERT+commit 打在事件循环上：免认证的撞库
   流量每一条都要循环停一停，撞上 WAL checkpoint 就是整站冻住。
5. GET /api/apps/{id}/unit 把 unit 全文给只读账号——面板自己生成的 unit 里就写着
   Environment=，而同文件的详情接口对 viewer 是剥离 env、nginx 只给分析结论。
6. SQL 控制台的语句原文进审计表，`ALTER ROLE x PASSWORD 'y'` 是裸字面量，
   redact_creds 那套 key=value 规则抓不到。
7. 网页终端只在握手那一刻校验角色/版本：降权、删号、改密吊销之后那台 root shell
   照样活着，直到浏览器标签被关掉。
"""

import asyncio
import itertools
import os
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))

import unittest

from app import database as dbm
from app import main, security
from app.routers import apps as apps_router
from app.routers import dbops, terminal
from app.services import alerts_service

_seq = itertools.count()


def _rows(code: str) -> list[dict]:
    return dbm.query("SELECT level,message FROM alerts_log WHERE code=? ORDER BY id", (code,))


def _req(role: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=f"u_{role}"))


def _cleanup_probe(name: str) -> None:
    dbm.execute("DELETE FROM alerts_log WHERE code=?", (f"probe:{name}",))
    for key in (f"sent:probe-{name}", f"notified:probe:{name}"):
        dbm.execute("DELETE FROM settings WHERE key=?", (key,))


# ---------------------------------------------------------------- 探活恢复通知
class _FakeStream:
    """假 HTTP 应答：asyncio.open_connection 返回 (reader, writer)。"""

    def __init__(self, status_line: bytes):
        self._line = status_line

    async def __call__(self, *_a, **_kw):
        outer = self

        class _Reader:
            async def readline(self):
                return outer._line

        class _Writer:
            def write(self, _d):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

        return _Reader(), _Writer()


async def _refused(*_a, **_kw):
    raise ConnectionRefusedError(111, "Connection refused")


class TestProbeRecoveryNotice(unittest.TestCase):
    """「应用无响应」必须有下文，且恢复标记不能永久残留。"""

    def _apps(self, name: str, port: int):
        async def fake(_sql, _params=()):
            return [{"id": 9001, "name": name, "port": port}]
        return fake

    def _run_probes(self, name, port, conn):
        sent: list[str] = []

        async def spy_notify(text):
            sent.append(text)
            return True

        with (mock.patch.object(alerts_service.aio_db, "query", self._apps(name, port)),
              mock.patch("asyncio.open_connection", conn),
              mock.patch.object(alerts_service, "notify", spy_notify)):
            asyncio.run(alerts_service.run_probes())
        return sent

    def test_recovery_sends_notice_and_clears_marker(self):
        name = f"utk{next(_seq)}"
        code = f"probe:{name}"
        self.addCleanup(_cleanup_probe, name)

        down = self._run_probes(name, 39990, _refused)
        self.assertEqual(len(down), 1, down)
        self.assertTrue(dbm.get_setting("notified:" + code), "触发时必须记下发过告警")

        up = self._run_probes(name, 39990, _FakeStream(b"HTTP/1.1 200 OK\r\n"))
        self.assertEqual(len(up), 1, up)
        self.assertTrue(up[0].startswith("已恢复"), up)
        self.assertIn("存活", up[0])
        self.assertFalse(dbm.get_setting("notified:" + code), "恢复后标记必须清掉")
        self.assertEqual([r["level"] for r in _rows(code)], ["crit", "ok"], _rows(code))

        # 之后一直是 200：不再重复落 ok 行，也不会再来一条「已恢复」
        again = self._run_probes(name, 39990, _FakeStream(b"HTTP/1.1 200 OK\r\n"))
        self.assertEqual(again, [], again)
        self.assertEqual([r["level"] for r in _rows(code)], ["crit", "ok"], _rows(code))

    def test_alive_but_unexpected_status_does_not_claim_recovery(self):
        """应答了但状态码不在预期内：只落 warn 历史，不能谎称已恢复。"""
        name = f"utk{next(_seq)}"
        code = f"probe:{name}"
        self.addCleanup(_cleanup_probe, name)
        self._run_probes(name, 39989, _refused)
        sent = self._run_probes(name, 39989, _FakeStream(b"HTTP/1.1 404 Not Found\r\n"))
        self.assertEqual(sent, [], "还没恢复成预期状态，不发「已恢复」")
        self.assertTrue(dbm.get_setting("notified:" + code))
        # 真正回到 200 才补上那条恢复
        sent2 = self._run_probes(name, 39989, _FakeStream(b"HTTP/1.1 200 OK\r\n"))
        self.assertEqual(len(sent2), 1, sent2)
        self.assertTrue(sent2[0].startswith("已恢复"), sent2)


# ---------------------------------------------------------------- 历史去重
class TestHistoryDedup(unittest.TestCase):
    def _run(self, *steps):
        code = f"utk{next(_seq)}"
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))

        async def seq():
            out = []
            for level, msg in steps:
                out.append(await alerts_service.record(level, code, msg))
            return out, code

        return asyncio.run(seq())

    def test_refire_after_recovery_writes_history(self):
        """crit → ok → 同一条 crit：复发必须重新落一行，否则列表停在「正常」。"""
        ids, code = self._run(("crit", "磁盘使用率超过阈值（阈值 85%）"),
                              ("ok", "磁盘使用率正常（阈值 85%）"),
                              ("crit", "磁盘使用率超过阈值（阈值 85%）"))
        self.assertIsNotNone(ids[2], ids)
        self.assertEqual([r["level"] for r in _rows(code)], ["crit", "ok", "crit"], _rows(code))

    def test_ongoing_alert_still_dedupes(self):
        ids, code = self._run(("crit", "持续中的告警"), ("crit", "持续中的告警"),
                              ("crit", "持续中的告警"))
        self.assertEqual(ids[1:], [None, None], ids)
        self.assertEqual(len(_rows(code)), 1, _rows(code))

    def test_recovery_after_recovery_is_deduped(self):
        ids, code = self._run(("ok", "一切正常"), ("ok", "一切正常"))
        self.assertIsNone(ids[1], ids)
        self.assertEqual(len(_rows(code)), 1, _rows(code))


# ---------------------------------------------------------------- 保留策略归属
class TestAlertRetentionOwner(unittest.TestCase):
    def test_housekeeping_does_not_re_prune_alerts(self):
        """告警历史只能由巡检循环按用户设置裁（模块约定 6）。"""
        called = []
        with mock.patch.object(main.dbm, "prune_alerts", lambda *a, **k: called.append(a)):
            main._prune_all()
        self.assertEqual(called, [], "housekeeping 里的默认值裁剪会顶掉 alert_keep_days/rows")

    def test_checker_loop_prunes_with_settings(self):
        """裁剪入口仍然只有一个，并且它读的是用户设置。"""
        import inspect

        src = inspect.getsource(alerts_service._checker_loop)
        self.assertIn("alert_keep_days", src)
        self.assertIn("prune_alerts", src)


# ---------------------------------------------------------------- 审计离开事件循环
class _BodyReq:
    """登录限速是按 IP 记的（进程内内存态），每条用例换一个 IP 才不会互相顶到 429。"""

    def __init__(self, body, ip=None):
        self._body = body
        self.ip = ip or f"10.44.{next(_seq) % 200}.{os.getpid() % 250}"
        self.client = SimpleNamespace(host=self.ip)
        self.headers = {}
        self.state = SimpleNamespace(cp_sub="nobody")

    async def json(self):
        return self._body


class TestAuthAuditOffLoop(unittest.IsolatedAsyncioTestCase):
    """免认证的登录路由里，审计写入不能占着事件循环。"""

    async def _audit_thread_of(self, coro_factory):
        seen: dict = {}
        loop_thread = threading.current_thread()
        real = dbm.audit

        def spy(*a, **kw):
            seen["thread"] = threading.current_thread()
            seen["args"] = a
            return real(*a, **kw)

        with mock.patch.object(dbm, "audit", spy):
            resp = await coro_factory()
        self.assertIn("thread", seen, "这条流程压根没写审计")
        self.assertIsNot(seen["thread"], loop_thread,
                         f"审计还在事件循环上跑（{seen['args']}）")
        return resp, seen

    async def test_login_failed_audit_is_off_the_loop(self):
        req = _BodyReq({"username": "nobody-here", "password": "x"})
        security.clear_attempts(req.ip)
        resp, seen = await self._audit_thread_of(lambda: main.login(req))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(seen["args"][:2], ("nobody-here", "login_failed"))

    async def test_profile_update_audit_is_off_the_loop(self):
        user = f"utk_{next(_seq)}"
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "viewer"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        req = _BodyReq({"nickname": "新昵称"})
        req.state.cp_sub = user
        resp, seen = await self._audit_thread_of(lambda: main.update_profile(req))
        self.assertEqual((resp["ok"], resp["nickname"]), (True, "新昵称"), resp)
        self.assertEqual(seen["args"][:2], (user, "profile:update"))


# ---------------------------------------------------------------- unit 全文门禁
class TestUnitFileGate(unittest.TestCase):
    def _call(self, role):
        async def go():
            return await apps_router.app_unit_read(1, _req(role))
        return asyncio.run(go())

    def test_viewer_is_refused_without_touching_the_file(self):
        boom = mock.Mock(side_effect=AssertionError("只读账号不该读 unit 文件"))
        with mock.patch.object(apps_router.apps_service, "read_unit", boom):
            r = self._call("viewer")
        self.assertEqual(r.status_code, 403)

    def test_admin_still_gets_content(self):
        sentinel = {"unit": "x.service", "content": "[Service]\n"}
        with mock.patch.object(apps_router.apps_service, "read_unit", lambda _id: sentinel):
            self.assertIs(self._call("admin"), sentinel)


# ---------------------------------------------------------------- SQL 审计脱敏
class TestSqlAuditRedaction(unittest.TestCase):
    def test_password_literals_are_masked(self):
        for sql, secret in [
            ("ALTER ROLE app PASSWORD 'hunter2';", "hunter2"),
            ('ALTER ROLE app PASSWORD "hunter2";', "hunter2"),
            ("CREATE ROLE bob IDENTIFIED BY 'hunter2';", "hunter2"),
            ("CREATE USER bob WITH PASSWORD hunter2;", "hunter2"),
            ("UPDATE t SET api_key=abc123 WHERE id=1", "abc123"),
        ]:
            out = dbops._sql_for_audit(sql)
            self.assertNotIn(secret, out, f"{sql} -> {out}")

    def test_benign_statement_survives(self):
        sql = "SELECT id, name FROM users ORDER BY id DESC LIMIT 10"
        self.assertEqual(dbops._sql_for_audit(sql), sql)

    def test_route_records_masked_sql(self):
        from app.services import pg_service

        audits = []

        async def fake_sql(_s, _db):
            return "ok"

        async def spy_audit(user, action, detail=None):
            audits.append((action, detail))

        req = _BodyReq({"sql": "ALTER ROLE app PASSWORD 'hunter2';", "db": "postgres"})

        async def go():
            with (mock.patch.object(pg_service, "query_sql", fake_sql),
                  mock.patch.object(dbops.aio_db, "audit", spy_audit)):
                return await dbops.pg_query(req)

        r = asyncio.run(go())
        self.assertEqual(r["db"], "postgres")
        self.assertEqual(len(audits), 1, audits)
        self.assertNotIn("hunter2", audits[0][1], audits[0])


# ---------------------------------------------------------------- 终端身份复验
class _Ws:
    def __init__(self, role="admin", sub="ghost", fail_accept=False):
        self.scope = {"type": "websocket", "state": {"cp_role": role, "cp_sub": sub}}
        self.closed = None
        self._fail = fail_accept

    async def accept(self):
        if self._fail:
            raise RuntimeError("client went away")

    async def close(self, code=None, reason=""):
        self.closed = code

    async def receive_text(self):
        raise AssertionError("不该走到读帧")


class TestTerminalIdentity(unittest.TestCase):
    """复验要比对的两个值必须真的随权限变化而变。"""

    def setUp(self):
        self.user = f"utkt_{next(_seq)}"
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?",
                        (f"token_epoch:{self.user}",))

    def test_snapshot_is_role_and_epoch(self):
        role, epoch = terminal._identity(self.user)
        self.assertEqual(role, "admin")
        self.assertEqual(epoch, security.session_epoch(self.user))

    def test_demotion_changes_snapshot(self):
        before = terminal._identity(self.user)
        dbm.execute("UPDATE users SET role='viewer' WHERE username=?", (self.user,))
        self.assertNotEqual(terminal._identity(self.user), before)

    def test_password_change_changes_snapshot(self):
        before = terminal._identity(self.user)
        security.bump_user_epoch(self.user)
        self.assertNotEqual(terminal._identity(self.user), before,
                            "改密吊销了旧 token，终端会话也必须认出来")

    def test_missing_user_raises(self):
        with self.assertRaises(LookupError):
            terminal._identity(f"nope_{next(_seq)}")


class TestTerminalRevalidate(unittest.IsolatedAsyncioTestCase):
    async def test_stale_identity_closes_session(self):
        ws = _Ws()
        with mock.patch.object(terminal, "_identity", lambda _u: ("viewer", "e2")), \
             mock.patch.object(terminal, "_RECHECK_SEC", 0.01):
            await asyncio.wait_for(terminal._revalidate(ws, "someone", ("admin", "e1")), 2)
        self.assertEqual(ws.closed, 4401)

    async def test_revoked_epoch_closes_session(self):
        ws = _Ws()
        with mock.patch.object(terminal, "_identity", side_effect=LookupError("删号了")), \
             mock.patch.object(terminal, "_RECHECK_SEC", 0.01):
            await asyncio.wait_for(terminal._revalidate(ws, "someone", ("admin", "e1")), 2)
        self.assertEqual(ws.closed, 4401)

    async def test_unchanged_identity_keeps_session(self):
        ws = _Ws()
        with mock.patch.object(terminal, "_identity", lambda _u: ("admin", "e1")), \
             mock.patch.object(terminal, "_RECHECK_SEC", 0.01):
            task = asyncio.create_task(terminal._revalidate(ws, "someone", ("admin", "e1")))
            await asyncio.sleep(0.12)
            self.assertFalse(task.done(), "身份没变就该继续看着")
            self.assertIsNone(ws.closed)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_handshake_refuses_unknown_user(self):
        """token 验得过但库里没人（例如另一进程删了号）：握手就该拒绝，不占额度。"""
        before = terminal._SESSIONS
        ws = _Ws(sub=f"ghost_{next(_seq)}")
        await terminal.terminal(ws)
        self.assertEqual(ws.closed, 4401)
        self.assertEqual(terminal._SESSIONS, before, "拒绝的握手必须归还并发额度")


if __name__ == "__main__":
    unittest.main()
