"""批次⑥C（认证、长连接与落地权限加固）的回归测试。

这一轮收的是四类问题：
1. 初始化的窗口关不严：setup 原来按"有没有 admin"判，管理员被 CLI 删光而机器上还留着
   viewer 时，未登录的 POST /api/auth/setup 会重新开放，谁先打到谁是 root；而且计数与
   INSERT 分属两次调用，两个并发请求能各自建一个管理员；
2. 爆破只按 IP 收口：代理池每个 IP 每分钟 9 次打在同一个 admin 上就是 9×N，而 admin
   等于 /api/terminal 的 root shell，所以按账号还得再来一道；
3. 长连接只在握手时验一次身份：终端 WS 与日志 SSE 之后都不再回头看库，改密/降权/删号
   乃至 token 自然过期都惊不动一条已经打开的 root shell 或全机 journal 流；
4. 密钥与哈希的落盘权限与出口脱敏：数据目录默认 0755 时同机任何用户都能把 panel.db
   （pass_hash + jwt_secret）整个读走，拿 jwt_secret 直接签 admin token 比爆破口令快得多。
"""

import asyncio
import itertools
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_6a_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_6a_roots_"))

from app import config, deps, main, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import extras, meta, system, terminal  # noqa: E402
from app.services import backup_service as B  # noqa: E402
from app.services import nginx_ops  # noqa: E402

_seq = itertools.count()
LOGIN_IP = "10.0.0.9"
FAR_FUTURE = time.time() + 3600


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _mkuser(role: str = "viewer", pw: str = "Password123", name: str | None = None) -> str:
    u = name or _uid(role)
    dbm.execute(
        "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
        (u, security.hash_pass(pw), role),
    )
    return u


def _deluser(*names: str):
    dbm.execute(f"DELETE FROM users WHERE username IN ({','.join('?' * len(names))})", names)


def _req(body=None, role="admin", sub="tester", peer=LOGIN_IP):
    async def _json():
        return body

    return SimpleNamespace(
        # cp_exp 是本轮新加的 state：中间件把 token 的到期时间透给路由，长连接自己判过期。
        state=SimpleNamespace(cp_role=role, cp_sub=sub, cp_exp=FAR_FUTURE),
        json=_json,
        headers={"content-type": "application/json"},
        client=SimpleNamespace(host=peer),
    )


def _err(resp) -> str:
    return json.loads(resp.body)["error"]


class TestCreateFirstAdmin(unittest.TestCase):
    """建第一个账号必须是"数一遍 + 插一行"在一个临界区里做完的原子动作。"""

    def setUp(self):
        # 判据是"整张表为空"，这一组就必须真的从空表开始：整轮测试共用同一个会话库，
        # 别的模块哪怕漏删一个账号，"空表"分支就永远走不到，用例只会假绿。
        self.snapshot = dbm.query("SELECT * FROM users")
        dbm.execute("DELETE FROM users")
        self.addCleanup(self._restore)

    def _restore(self):
        dbm.execute("DELETE FROM users")
        for u in self.snapshot:
            # 连 id 一起放回：用例里有人按 id 改账号，id 被 AUTOINCREMENT 顶到新值就改到别人身上。
            dbm.execute(
                "INSERT INTO users(id,username,pass_hash,created_at,role,nickname,avatar) VALUES(?,?,?,?,?,?,?)",
                (u["id"], u["username"], u["pass_hash"], u["created_at"], u["role"], u["nickname"], u["avatar"]),
            )

    def test_ok_when_table_empty(self):
        name = _uid("cfa")
        self.assertEqual(dbm.create_first_admin(name, "hash"), "ok")
        row = dbm.query_one("SELECT role FROM users WHERE username=?", (name,))
        self.assertEqual(row["role"], "admin", "第一个账号只能是 admin")

    def test_refused_when_any_account_exists(self):
        """判据是"表里有没有人"，不是"有没有 admin"——只剩 viewer 时公网不能再自助造 root。"""
        _mkuser("viewer")
        name = _uid("late")
        self.assertEqual(dbm.create_first_admin(name, "hash"), "not-empty")
        self.assertIsNone(dbm.query_one("SELECT 1 FROM users WHERE username=?", (name,)))

    def test_name_taken_is_not_500(self):
        """CLI 在另一个进程里抢先建了同名账号：INSERT 撞唯一约束要落回 400 那句话。"""
        _mkuser("viewer", name="shared_name")
        # 模拟 TOCTOU：预检看到的是"空表"，落库时才撞上。
        with mock.patch.object(dbm, "query_one", lambda sql, params=(): {"c": 0}):
            self.assertEqual(dbm.create_first_admin("shared_name", "hash"), "name-taken")
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM users WHERE username='shared_name'")["c"], 1)

    def test_concurrent_setup_yields_one_admin(self):
        """两个并发 setup 都通过预检的话，库里会多出两个管理员，而面板以为只有一个。"""
        a, b = _uid("race_a"), _uid("race_b")

        async def both():
            from starlette.concurrency import run_in_threadpool

            return await asyncio.gather(
                run_in_threadpool(dbm.create_first_admin, a, "h1"),
                run_in_threadpool(dbm.create_first_admin, b, "h2"),
            )

        results = asyncio.run(both())
        self.assertEqual(sorted(results), ["not-empty", "ok"], f"必须恰好一个成功：{results}")
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM users WHERE username IN (?,?)", (a, b))["c"], 1)


class TestSetupEndpoint(unittest.TestCase):
    """/api/auth/setup 的实际出口：403 的口径、与 needsSetup 的一致性。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def test_403_when_only_non_admin_remains(self):
        u = _mkuser("viewer")
        self.addCleanup(_deluser, u)
        r = self.client.post("/api/auth/setup", json={"username": _uid("s"), "password": "Password123"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("初始化已完成", r.json()["error"])

    def test_status_agrees_with_setup_gate(self):
        """needsSetup 与 setup 的判据必须同一条：UI 显示"已初始化"而接口还开着（或反之），
        都是没人会去查的漏洞窗口。"""
        u = _mkuser("viewer")
        self.addCleanup(_deluser, u)
        self.assertFalse(self.client.get("/api/auth/status").json()["needsSetup"])
        r = self.client.post("/api/auth/setup", json={"username": _uid("s2"), "password": "Password123"})
        self.assertEqual(r.status_code, 403, "status 说不需要初始化时，setup 必须同时关门")


class TestAccountLockout(unittest.TestCase):
    """按账号的失败桶：IP 桶挡不住代理池。"""

    def setUp(self):
        security.clear_attempts(LOGIN_IP)
        self.addCleanup(security.clear_attempts, LOGIN_IP)

    def _login(self, username, password="WrongPass999"):
        return asyncio.run(main.login(_req({"username": username, "password": password})))

    def test_known_account_locks_after_limit(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        limit = config.LOGIN_LIMIT
        with mock.patch.object(security, "_attempts", {}), mock.patch.object(security, "rate_limited", lambda ip: False):
            codes = [self._login(u).status_code for _ in range(limit + 3)]
            self.assertEqual(codes[0], 401, "刚错一次就 429 是把正常用户也拦在门外")
            self.assertIn(429, codes, "同一个账号被反复试错后必须单独收口")
            self.assertEqual(codes[-1], 429, "越线之后不能又放行")
            self.assertIn("该账号尝试过多", _err(self._login(u)))

    def test_lock_window_is_fixed_not_sliding(self):
        """窗口起点若随每次失败顺延，"每 50 秒错一次"的慢爆就把临时限制变成永久封禁。"""
        key = security.account_key(_uid("win"))
        with mock.patch.object(security, "_attempts", {}):
            security.note_failure(key)
            first_start = security._attempts[key][1]
            with mock.patch.object(time, "time", lambda: first_start + 30):
                security.note_failure(key)
            self.assertEqual(security._attempts[key][1], first_start, "记账不能把窗口起点推到本次失败上")

    def test_lock_expires_with_the_window(self):
        key = security.account_key(_uid("exp"))
        with mock.patch.object(security, "_attempts", {}):
            for _ in range(config.LOGIN_LIMIT + 5):
                security.note_failure(key)
            self.assertTrue(security.account_locked(key))
            with mock.patch.object(time, "time", lambda: security._attempts[key][1] + config.LOGIN_WINDOW_SEC + 1):
                self.assertFalse(security.account_locked(key), "超过窗口就该重新给机会，而不是永久锁")

    def test_unknown_account_does_not_fill_the_bucket(self):
        """不存在的名字没什么可爆破；照记的话陌生人可以随意撑大 _attempts 的键空间。"""
        name = _uid("ghost")
        with mock.patch.object(security, "_attempts", {}), mock.patch.object(security, "rate_limited", lambda ip: False):
            codes = [self._login(name).status_code for _ in range(config.LOGIN_LIMIT + 3)]
            self.assertEqual(set(codes), {401}, f"未知账号不该进账号桶：{codes}")
            self.assertEqual(security._attempts, {}, "账号桶里不该出现陌生名字")

    def test_successful_login_clears_the_bucket(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        with mock.patch.object(security, "_attempts", {}) as bucket, \
             mock.patch.object(security, "rate_limited", lambda ip: False):
            for _ in range(3):
                self._login(u)
            self.assertTrue(bucket, "先让账号桶里留下计数，否则清空与否都是假绿")
            out = self._login(u, "Password123")
        self.assertIn("token", out, f"成功登录该签发 token：{out}")
        self.assertEqual(bucket, {}, "登录成功必须清零，否则手滑错三次之后永远差一次就被锁")


class TestTerminalFrameGuard(unittest.IsolatedAsyncioTestCase):
    """终端接收循环：一帧坏数据不该把整个会话和 pty 一起打死。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        cls.client = TestClient(main.app)

    def setUp(self):
        self.user = _mkuser("admin")
        self.addCleanup(_deluser, self.user)
        self.token = security.sign_token(self.user, "admin")
        dbm.execute("DELETE FROM audit WHERE username=?", (self.user,))
        self.addCleanup(dbm.execute, "DELETE FROM audit WHERE username=?", (self.user,))

    async def test_non_object_json_frame_does_not_kill_session(self):
        """`123`、`"x"`、`[]` 都是合法 JSON，但 msg.get("d") 会在它们身上抛 AttributeError。"""
        with self.client.websocket_connect(f"/api/terminal?token={self.token}") as ws:
            for bad in ("123", '"x"', "[1,2]", "null"):
                ws.send_text(bad)
            ws.send_json({"d": "ping"})
            seen = []
            for _ in range(40):
                try:
                    msg = ws.receive_json()
                except Exception:  # noqa: BLE001 — 会话被打死时这里先炸，正是要检的
                    break
                seen.append(msg.get("d"))
                if msg.get("d") == "pong":
                    break
            self.assertIn("pong", seen, "坏帧之后 ping 仍要能得到回答，说明会话还活着")
        await asyncio.sleep(0.3)  # 服务端 finally 在 portal 线程里跑，等它把额度还回来
        self.assertEqual(terminal._SESSIONS, 0)

    async def test_idle_connection_is_reclaimed(self):
        """半死的连接（标签挂起、中间设备掉了会话）会一直占着一个 pty 与一个名额。"""
        with mock.patch.object(terminal, "_IDLE_SEC", 0.3):
            with self.client.websocket_connect(f"/api/terminal?token={self.token}") as ws:
                closed = False
                try:
                    for _ in range(60):
                        m = ws.receive()
                        if m["type"] in ("websocket.close", "websocket.disconnect"):
                            closed = True
                            break
                except Exception:  # noqa: BLE001 — TestClient 用异常表达断开
                    closed = True
                self.assertTrue(closed, "空闲到点服务端必须自己收尾")
        await asyncio.sleep(0.3)
        self.assertEqual(terminal._SESSIONS, 0, "空闲回收后额度要归还")
        row = dbm.query_one(
            "SELECT 1 hit FROM audit WHERE username=? AND action='terminal:idle_timeout'", (self.user,)
        )
        self.assertTrue(row, "回收要留痕，否则用户只看到终端断了且没有任何原因")


class TestSseRevalidation(unittest.TestCase):
    """日志 SSE 是第二条长连接：全机 journal 不能在授权收回后继续流。"""

    class _Stdout:
        def __init__(self, lines):
            self._it = iter(lines)

        async def readline(self):
            try:
                return next(self._it)
            except StopIteration:
                return b""

    class _Proc:
        def __init__(self, lines):
            self.stdout = TestSseRevalidation._Stdout(lines)
            self.killed = False

        def kill(self):
            self.killed = True

    def _collect(self, *args):
        proc = self._Proc([b"line one\n", b"line two\n", b"line three\n"])

        async def fake_exec(*a, **k):
            return proc

        async def go():
            with mock.patch.object(asyncio, "create_subprocess_exec", fake_exec):
                return [c async for c in system._sse_journal("x.service", 10, *args)]

        return asyncio.run(go()), proc

    def _texts(self, chunks):
        """把一帧帧 `data: "<json>"` 还原成文本，断言才不用和 \\uXXXX 较劲。"""
        out = []
        for c in chunks:
            body = c.strip()
            out.append(json.loads(body[len("data: "):]) if body.startswith("data: ") else body)
        return out

    def test_without_subject_it_behaves_as_before(self):
        chunks, _ = self._collect()
        self.assertEqual(self._texts(chunks), ["line one", "line two", "line three"], "不传主体就是不复核（只给单测用这条路）")

    def test_identity_change_stops_stream(self):
        # 第 1 个返回值是建流时的基线，之后每帧前比对一次：第三次读到降权，流就该在那一行停。
        with mock.patch.object(system, "_RECHECK_SEC", 0), \
             mock.patch.object(
                 security, "session_identity",
                 side_effect=[("admin", "e1"), ("admin", "e1"), ("viewer", "e1")],
             ):
            chunks, proc = self._collect("someone", FAR_FUTURE)
        texts = self._texts(chunks)
        self.assertEqual(len(texts), 2, f"该放出一行数据再加一句收尾：{texts}")
        self.assertEqual(texts[0], "line one")
        self.assertIn("已停止", texts[-1])
        self.assertTrue(proc.killed, "journalctl -f 必须跟着被杀掉，否则进程留着继续产输出")

    def test_expired_token_stops_stream(self):
        with mock.patch.object(system, "_RECHECK_SEC", 0), \
             mock.patch.object(security, "session_identity", return_value=("admin", "e1")):
            chunks, _ = self._collect("someone", 0)
        texts = self._texts(chunks)
        self.assertEqual(len(texts), 1, f"票过期后一行全机 journal 都不该再推出去：{texts}")
        self.assertIn("已停止", texts[0])

    def test_valid_session_streams_everything(self):
        with mock.patch.object(system, "_RECHECK_SEC", 0), \
             mock.patch.object(security, "session_identity", return_value=("admin", "e1")):
            chunks, _ = self._collect("someone", FAR_FUTURE)
        self.assertEqual(self._texts(chunks), ["line one", "line two", "line three"])


class TestSessionIdentity(unittest.TestCase):
    def test_snapshot_catches_demotion(self):
        u = _mkuser("admin")
        self.addCleanup(_deluser, u)
        before = security.session_identity(u)
        dbm.execute("UPDATE users SET role='viewer' WHERE username=?", (u,))
        self.assertNotEqual(security.session_identity(u), before, "降权不改 epoch，长连接必须靠角色这一路认出来")

    def test_missing_user_raises(self):
        with self.assertRaises(LookupError):
            security.session_identity(_uid("gone"))


class TestCspAndBootScript(unittest.TestCase):
    """CSP 收紧到 script-src 'self'：一处 HTML 注入不该直接升级成拿 token。"""

    def _directive(self, prefix: str) -> str:
        return [d for d in deps.CSP.split("; ") if d.startswith(prefix)][0]

    def test_script_src_has_no_unsafe_inline(self):
        d = self._directive("script-src")
        self.assertNotIn("unsafe-inline", d)
        self.assertIn("'self'", d)

    def test_style_src_keeps_unsafe_inline(self):
        """naive-ui 运行时注入 <style> 与内联 style 属性，收掉它等于把整站样式打没。"""
        self.assertIn("unsafe-inline", self._directive("style-src"))

    def test_index_html_has_no_inline_script(self):
        html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", html, "外链化后不该再有内联脚本，否则 CSP 只能留 unsafe-inline")
        self.assertIn("/theme-boot.js", html)
        self.assertTrue((REPO / "web" / "public" / "theme-boot.js").is_file(), "首屏防闪脚本必须真的存在")


class TestAuditRedaction(unittest.TestCase):
    def test_detail_and_action_masked(self):
        marker = "hunter2s3cret"
        dbm.audit("cp6a_u1", f"run PGPASSWORD={marker}", f"failed with password={marker}")
        self.addCleanup(dbm.execute, "DELETE FROM audit WHERE username='cp6a_u1'")
        row = dbm.query_one("SELECT action, detail FROM audit WHERE username='cp6a_u1' ORDER BY id DESC LIMIT 1")
        self.assertNotIn(marker, row["action"])
        self.assertNotIn(marker, row["detail"])

    def test_paths_survive_redaction(self):
        """脱敏不能把排查要用的路径一起遮掉，否则 doctor 与排障都变成猜。"""
        dbm.audit("cp6a_u2", "file:delete", "删除 /etc/nginx/conf.d/a.conf 失败")
        self.addCleanup(dbm.execute, "DELETE FROM audit WHERE username='cp6a_u2'")
        row = dbm.query_one("SELECT detail FROM audit WHERE username='cp6a_u2' ORDER BY id DESC LIMIT 1")
        self.assertIn("/etc/nginx/conf.d/a.conf", row["detail"])


class TestUserUpdateAudit(unittest.TestCase):
    def test_changed_fields_are_recorded(self):
        """一条只写人名的 user:update，事后既看不出谁被升成 admin，也看不出谁的口令被重置。"""
        u = _mkuser("viewer")
        self.addCleanup(_deluser, u)
        dbm.execute("DELETE FROM audit WHERE username='cp6a_boss'")
        self.addCleanup(dbm.execute, "DELETE FROM audit WHERE username='cp6a_boss'")
        uid = dbm.query_one("SELECT id FROM users WHERE username=?", (u,))["id"]
        with mock.patch.object(extras, "_need_admin", lambda r: None):
            out = asyncio.run(extras.users_update(str(uid), _req({"role": "admin", "nickname": "小赵"}, sub="cp6a_boss")))
        self.assertEqual(out, {"ok": True})
        row = dbm.query_one(
            "SELECT detail FROM audit WHERE username='cp6a_boss' AND action='user:update' ORDER BY id DESC LIMIT 1"
        )
        self.assertIn("role", row["detail"])
        self.assertIn("nickname", row["detail"])
        self.assertNotIn("小赵", row["detail"], "只记字段名，值本身不进审计表")


class TestStripSetuid(unittest.TestCase):
    def test_setuid_bit_is_flattened(self):
        """面板以 root 解包，归档里的 4755 落盘就是白给同机一个 setuid-root 提权落点。"""
        d = tempfile.mkdtemp(prefix="cp_6a_suid_")
        self.addCleanup(shutil.rmtree, d, True)
        p = Path(d) / "helper"
        p.write_bytes(b"#!/bin/sh\n")
        os.chmod(p, 0o4755)
        m = tarfile.TarInfo("helper")
        m.mode, m.type, m.size = 0o4755, tarfile.REGTYPE, p.stat().st_size
        self.assertEqual(B._strip_setuid(d, [m]), 1)
        self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o0755)

    def test_plain_file_untouched(self):
        d = tempfile.mkdtemp(prefix="cp_6a_plain_")
        self.addCleanup(shutil.rmtree, d, True)
        p = Path(d) / "app.js"
        p.write_bytes(b"//")
        os.chmod(p, 0o0644)
        m = tarfile.TarInfo("app.js")
        m.mode, m.type = 0o0644, tarfile.REGTYPE
        self.assertEqual(B._strip_setuid(d, [m]), 0)
        self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o0644)

    def test_symlinked_target_is_not_chmodded(self):
        """解包与收权限之间被人把落点换成符号链接时，chmod 会打到链接目标上去。"""
        d = tempfile.mkdtemp(prefix="cp_6a_link_")
        self.addCleanup(shutil.rmtree, d, True)
        outside = Path(d) / "real"
        outside.write_bytes(b"x")
        os.chmod(outside, 0o4755)
        Path(d, "helper").symlink_to(outside)
        m = tarfile.TarInfo("helper")
        m.mode, m.type = 0o4755, tarfile.REGTYPE
        self.assertEqual(B._strip_setuid(d, [m]), 0, "lstat 复核发现不是普通文件就不动")
        self.assertEqual(stat.S_IMODE(os.lstat(outside).st_mode), 0o4755, "链接目标必须原样不动")


class TestDataDirPermissions(unittest.TestCase):
    def test_data_dir_is_owner_only(self):
        mode = stat.S_IMODE(os.stat(config.DATA_DIR).st_mode)
        self.assertFalse(mode & 0o077, f"{config.DATA_DIR} 是 {oct(mode)}，同机用户能读走 pass_hash 与 jwt_secret")

    def test_doctor_reports_wide_data_dir(self):
        original = stat.S_IMODE(os.stat(config.DATA_DIR).st_mode)
        os.chmod(config.DATA_DIR, 0o755)
        self.addCleanup(os.chmod, config.DATA_DIR, original)
        out = asyncio.run(meta._check_permissions())
        self.assertEqual(out["status"], "warn")
        self.assertIn(str(config.DATA_DIR), out["detail"])
        self.assertIn(f"chmod 0700 {config.DATA_DIR}", out["fix"])

    def test_doctor_reports_wide_db_file(self):
        db = Path(config.DATA_DIR) / "panel.db"
        original = stat.S_IMODE(db.stat().st_mode)
        os.chmod(db, 0o644)
        self.addCleanup(os.chmod, db, original)
        out = asyncio.run(meta._check_permissions())
        self.assertEqual(out["status"], "warn")
        self.assertIn("panel.db", out["detail"])


class TestPanelProxyNotListedAsApp(unittest.TestCase):
    """面板自己的 nginx 入口不能混进"这个应用关联的站点"，否则改应用配置会改到面板头上。"""

    def test_is_panel_proxy_flags_own_port(self):
        mine = {"analysis": {"proxyPorts": [config.PORT]}, "file": "/etc/nginx/conf.d/choyeon-panel.conf"}
        other = {"analysis": {"proxyPorts": [8081]}, "file": "/etc/nginx/conf.d/app.conf"}
        self.assertTrue(nginx_ops.is_panel_proxy(mine))
        self.assertFalse(nginx_ops.is_panel_proxy(other))


if __name__ == "__main__":
    unittest.main()
