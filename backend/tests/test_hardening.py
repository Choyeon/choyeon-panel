"""针对本轮加固项的回归测试：配置解析、令牌吊销、登录限速、日志裁剪、路径与体积守卫。

说明：这些测试会写入临时 SQLite（CP_DATA_DIR 由 test_backend 或本文件设为临时目录），
不会触碰真实数据。命令执行相关的用例只在超时/权限校验层面验证，不做真实系统变更。
"""

import asyncio
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jwt

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_roots_"))

from app import config, main, security, templates  # noqa: E402
from app import database as dbm
from app.routers import terminal  # noqa: E402
from app.services import alerts_service, apps_service, files_service, nginx_ops  # noqa: E402
from app.util import parse_lines, run, run_shell_async  # noqa: E402


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


_seq = itertools.count()


def _uid(prefix: str) -> str:
    """测试用户名：唯一、只含 [A-Za-z0-9_-]（注册/改用户接口都按这个正则校验）。

    必须短：用户名正则是 3-32 位，`f"{os.getpid()}_{id(self)}"` 那种写法在
    长前缀下会超限，这里用自增序号控制长度。
    """
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


# ---------------------------------------------------------------- 终端资源回收
class TestTerminalCleanup(unittest.IsolatedAsyncioTestCase):
    """回归：WS 异常断开时 pty 主从两端 fd 与 bash 进程都必须回收。

    旧实现把关闭 fd 排在 finally 里的 await 之后，任务被取消时那段清理整段跳过，
    实测每次异常断开泄漏一个 /dev/ptmx 主端 fd；反复开关终端会耗尽 fd 拖垮面板。
    """

    def _pty_fd_count(self) -> int:
        return sum(
            1 for fd in os.listdir("/proc/self/fd")
            if any(k in self._readlink(fd) for k in ("/dev/ptmx", "/dev/pts/"))
        )

    @staticmethod
    def _readlink(fd: str) -> str:
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return ""

    async def test_abrupt_disconnect_does_not_leak_pty_fds(self):
        """回归：客户端中途断开时，pty 主端 fd 必须被回收。

        旧实现把 os.close(master) 排在 finally 里的 await 之后，
        取消会打断那段清理，实测每次断开泄漏一个 /dev/ptmx fd；
        反复开关终端会耗尽 fd，最后整个面板都起不来。
        """
        import asyncio as _a

        from starlette.testclient import TestClient

        from app import main as _main

        user = f"term_{os.getpid()}_{id(self)}"
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (user, security.hash_pass("Password123"), "admin"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        token = security.sign_token(user, "admin")

        client = TestClient(_main.app)
        baseline = self._pty_fd_count()
        for _ in range(3):
            with client.websocket_connect(f"/api/terminal?token={token}") as ws:
                ws.send_json({"d": "input", "data": "sleep 30\n"})
                # 不发 exit，直接离开 with 断开——正是要复现的路径
            await _a.sleep(0.2)  # 给后台回收线程收尾
        leaked = self._pty_fd_count() - baseline
        self.assertLessEqual(leaked, 0, f"3 次异常断开后仍持有 pty fd，泄漏 {leaked} 个")
        import app.routers.terminal as _t

        self.assertEqual(_t._SESSIONS, 0, "并发额度必须对称归还，否则终端会永久 4503")

    def test_shell_cwd_falls_back_when_root_home_unusable(self):
        """_shell_cwd 的分支逻辑要和 euid 无关地测到。

        只靠上面那条 WS 用例不够：以 root 跑测试时 `/root` 恰好可进，
        把 cwd 写死回 "/root" 也能通过（变异验证过：SURVIVED）。
        这里直接模拟"普通用户跑面板"的可见性，验证它会往下找可用目录，
        而不是拿一个 chdir 会 EACCES 的路径去喂 Popen。
        """
        import app.routers.terminal as _t

        real_access = os.access
        denied = ("/root",)

        def fake_access(path, mode, **kw):
            if str(path).rstrip("/") in denied:
                return False
            return real_access(path, mode, **kw)

        with (
            mock.patch.object(_t.os, "access", fake_access),
            mock.patch.object(_t.os.path, "expanduser", lambda _="~": "/root"),
        ):
            got = _t._shell_cwd()
        self.assertNotEqual(got, "/root", "不可访问的 /root 不该被选中")
        self.assertTrue(os.path.isdir(got) and real_access(got, os.X_OK), f"回落到的目录同样不可用：{got}")

    def test_shell_cwd_prefers_root_home_when_usable(self):
        """非空验证：root 跑面板时行为要和以前一致，别把回落写成"永远换目录"。"""
        if os.geteuid() != 0:
            self.skipTest("只有 root 环境下 /root 可用")
        import app.routers.terminal as _t

        self.assertEqual(_t._shell_cwd(), "/root")

    def test_shell_cwd_last_resort_is_slash(self):
        """所有候选都不可用时必须给出确定答案（"/"），而不是抛异常。"""
        import app.routers.terminal as _t

        with (
            mock.patch.object(_t.os, "access", lambda *a, **k: False),
            mock.patch.object(_t.os.path, "isdir", lambda _p: True),
            mock.patch.object(_t.os.path, "expanduser", lambda _="~": "/root"),
            mock.patch.object(_t.config, "APP_ROOT", "/nonexistent-cp-root"),
            mock.patch.object(_t.config, "DATA_DIR", "/nonexistent-cp-data"),
        ):
            self.assertEqual(_t._shell_cwd(), "/")

    def test_cwd_comes_from_fallback_helper(self):
        """守卫矩阵：Popen 的 cwd 必须来自 _shell_cwd()。

        以 root 跑测试时，把调用点直接写死回 `cwd="/root"` 也能全绿——函数没被改坏，
        WS 也照样起得来，只有源码断言能抓住"调用点回退"这一类改法。
        与 test_backup_service 里"三类产物都要走 _free_path"用的是同一套写法。
        """
        import inspect

        import app.routers.terminal as _t

        src = inspect.getsource(_t.terminal)
        self.assertIn("cwd=_shell_cwd()", src, "Popen 的 cwd 又变成写死的目录了")

    async def test_reap_kills_process_group(self):
        marker = f"/tmp/cp_reap_child_{os.getpid()}_{id(self)}"
        proc = subprocess.Popen(
            ["/bin/bash", "-c", f"sleep 300 & echo $! > {marker}; wait"],
            start_new_session=True, stdout=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: os.path.exists(marker) and os.unlink(marker))
        for _ in range(60):
            if os.path.exists(marker):
                break
            await asyncio.sleep(0.05)
        child = int(Path(marker).read_text().strip())

        terminal._reap_process(proc)

        self.assertIsNotNone(proc.poll(), "bash 必须被回收，不能留僵尸")
        with self.assertRaises(OSError, msg="后台子进程要随进程组一起杀掉"):
            os.kill(child, 0)

# ---------------------------------------------------------------- 备份可见性
class TestBackupDiscovery(unittest.TestCase):
    """回归：doctor 与 `backup list` 必须看得见 PG 备份。

    backup_runner 写出 .sql.gz / .dump.gz / .tar.gz 三类文件，
    旧 glob 只有 *.tar.gz 与 *.sql，PG 备份一种都命中不了：
    只跑数据库备份的机器会被判成"还没有备份"，刚跑完的备份也不算数。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_bk_")
        self.addCleanup(_rmtree, self.dir)
        os.makedirs(f"{self.dir}/manual", exist_ok=True)
        for name in ("bk1-2026-01-01-03-30.sql.gz", "bk2-2026-01-02-03-30.dump.gz",
                     "bk3-2026-01-03-03-30.tar.gz", "panel-manual-x.tar.gz", "notes.md"):
            Path(self.dir, name).write_text("x")
        self._old = config.BACKUP_DIR
        config.BACKUP_DIR = self.dir
        self.addCleanup(setattr, config, "BACKUP_DIR", self._old)

    def _keep_only(self, suffix: str) -> None:
        for f in os.listdir(self.dir):
            p = f"{self.dir}/{f}"
            if not f.endswith(suffix):
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)

    def test_doctor_counts_pg_backups(self):
        from app.routers import meta

        count, mtime = meta._recent_backups()
        self.assertEqual(count, 4, "三类备份 + manual 的 tar 都要计入，notes.md 不算")
        self.assertIsNotNone(mtime)

    def test_doctor_finds_pg_only_backups(self):
        from app.routers import meta

        self._keep_only(".sql.gz")
        count, _ = meta._recent_backups()
        self.assertEqual(count, 1)

        res = asyncio.run(meta._check_backup())
        self.assertNotIn("还没有备份", res["detail"], "只有 PG 备份时不能报没有备份")
        self.assertEqual(res["status"], "pass")

    def test_doctor_still_warns_when_truly_empty(self):
        from app.routers import meta

        self._keep_only(".nonexistent")
        res = asyncio.run(meta._check_backup())
        self.assertEqual(res["status"], "warn", "目录清空后仍要报 warn，别把检查写瞎")


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

    def test_epoch_bump_is_monotonic_within_same_second(self):
        """回归：旧实现用 int(time.time()) 作版本号，同一秒内连续 bump 值相同，
        导致改密后旧 token 仍然有效（安全漏洞 + 用例随机失败）。"""
        epochs = {dbm.bump_token_epoch() for _ in range(5)}
        self.assertEqual(len(epochs), 5, "连续 bump 必须每次都产生新版本号")
        user_epoch = {security.bump_user_epoch(self.user) for _ in range(5)}
        self.assertEqual(len(user_epoch), 5, "同一用户连续 bump 必须每次都产生新版本号")

    def test_revocation_takes_effect_immediately(self):
        tok = security.sign_token(self.user, "viewer")
        security.verify_token(tok)  # 基线：可用
        security.bump_user_epoch(self.user)
        with self.assertRaises(jwt.InvalidTokenError, msg="bump 之后旧 token 必须立即失效"):
            security.verify_token(tok)


# ---------------------------------------------------- 用户改角色/改密（HTTP 层）
class TestUserUpdateRevocation(unittest.TestCase):
    """PATCH /api/users/{uid} 的两条实测缺陷。

    1. 中间件读的是 **token 里的 role**（app/deps.py），不是库里的 role。旧实现
       改 role 后不吊销旧 token，于是把 admin 降成 viewer，对方手上的旧 token
       在 TTL 内（最长 72h）依然是 admin —— 降权完全无效，写操作照旧放行。
    2. 旧实现「先落库 role，再校验 password」：{"role":"viewer","password":"short"}
       返回 400，但 role 已经真的改了（部分写入），且这条分支不写审计。
    """

    BASE_PW = "Password123"      # setUp 里建用户用的初始密码
    NEW_PW = "Rotated99Pass"     # 通过接口改成的新密码

    def setUp(self):
        from starlette.testclient import TestClient

        from app import main as _main

        self.client = TestClient(_main.app)
        self.admin = _uid("uupd_admin_")
        self.target = _uid("uupd_target_")
        for name in (self.admin, self.target):
            dbm.execute(
                "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (name, security.hash_pass(self.BASE_PW), "admin"),
            )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.admin,))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.target,))
        self.admin_headers = {"Authorization": f"Bearer {security.sign_token(self.admin, 'admin')}"}
        self.tid = dbm.query_one("SELECT id FROM users WHERE username=?", (self.target,))["id"]

    def _role(self):
        return dbm.query_one("SELECT role FROM users WHERE id=?", (self.tid,))["role"]

    def test_demotion_revokes_old_admin_token(self):
        """核心回归：降权后旧 token 必须立刻不能再当 admin 用。"""
        victim_token = security.sign_token(self.target, "admin")
        # 基线：旧 token 能过中间件的写操作闸门（用一个非法 verb 的 4xx，
        # 只要不是 403「只读账号」就说明它仍被当成 admin）
        base = self.client.post(
            "/api/apps/999999/action", json={"verb": "stop"},
            headers={"Authorization": f"Bearer {victim_token}"},
        )
        self.assertNotIn("只读账号", base.text, "基线：未降权前该 token 应享有 admin 权限")

        r = self.client.patch(f"/api/users/{self.tid}", json={"role": "viewer"}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._role(), "viewer")

        with self.assertRaises(jwt.InvalidTokenError, msg="改 role 后旧 token 必须失效"):
            security.verify_token(victim_token)

        after = self.client.post(
            "/api/apps/999999/action", json={"verb": "stop"},
            headers={"Authorization": f"Bearer {victim_token}"},
        )
        self.assertIn("登录已过期", after.text, "降权后拿旧 token 必须被 401 拦下，而不是继续当 admin")

    def test_reissued_token_carries_new_role(self):
        """非空断言：降权后重新签发的 viewer token 确实被写操作闸门拦住
        （证明上一条不是因为 token 恰好不可用而“看起来”安全）。"""
        self.client.patch(f"/api/users/{self.tid}", json={"role": "viewer"}, headers=self.admin_headers)
        fresh = {"Authorization": f"Bearer {security.sign_token(self.target, 'viewer')}"}
        r = self.client.post("/api/apps/999999/action", json={"verb": "stop"}, headers=fresh)
        self.assertIn("只读账号", r.text)

    def test_invalid_password_does_not_change_role(self):
        """部分写入回归：整笔请求失败时，role 必须保持原值。"""
        before = self._role()
        r = self.client.patch(
            f"/api/users/{self.tid}", json={"role": "viewer", "password": "short"}, headers=self.admin_headers
        )
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("密码", r.text)
        self.assertEqual(self._role(), before, "400 的请求不能留下半个变更")

    def test_self_role_change_rejected_without_touching_db(self):
        """不能改自己角色；且被拒绝时不得写库、不得吊销自己的 token（否则管理员自锁）。"""
        aid = dbm.query_one("SELECT id FROM users WHERE username=?", (self.admin,))["id"]
        r = self.client.patch(f"/api/users/{aid}", json={"role": "viewer"}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(
            dbm.query_one("SELECT role FROM users WHERE id=?", (aid,))["role"], "admin", "拒绝后不能改库"
        )
        # 自己的 token 仍可用（没有被误吊销）
        self.assertEqual(security.verify_token(self.admin_headers["Authorization"][7:])["sub"], self.admin)

    def test_equal_role_does_not_lock_user_out(self):
        """幂等：role 传成当前值不算变更，不该吊销对方 token。"""
        tok = security.sign_token(self.target, "admin")
        r = self.client.patch(f"/api/users/{self.tid}", json={"role": "admin"}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(security.verify_token(tok)["sub"], self.target, "无实际变更时不应吊销 token")

    def test_password_change_still_revokes(self):
        """原有能力不回退：改密仍要吊销旧 token。"""
        tok = security.sign_token(self.target, "admin")
        r = self.client.patch(
            f"/api/users/{self.tid}", json={"password": "NewPassw0rd"}, headers=self.admin_headers
        )
        self.assertEqual(r.status_code, 200, r.text)
        with self.assertRaises(jwt.InvalidTokenError):
            security.verify_token(tok)

    def test_password_change_actually_sets_the_new_password(self):
        """非空断言：改密不是「只 bump epoch」——落库的必须是提交的那个密码。

        只验证吊销的话，把 pass_hash 写成任何别的值（甚至写死）都能过，
        而这个接口真正的用途是让用户能用新密码登录。
        """
        stored_old = dbm.query_one("SELECT pass_hash FROM users WHERE id=?", (self.tid,))["pass_hash"]
        r = self.client.patch(f"/api/users/{self.tid}", json={"password": self.NEW_PW}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 200, r.text)
        stored_new = dbm.query_one("SELECT pass_hash FROM users WHERE id=?", (self.tid,))["pass_hash"]
        self.assertNotEqual(stored_new, stored_old, "pass_hash 必须被更新")
        self.assertTrue(security.check_pass(self.NEW_PW, stored_new), "新密码必须能校验通过")
        self.assertFalse(security.check_pass(self.BASE_PW, stored_new), "旧密码必须失效")
        # 走真实登录接口确认端到端可用（而不只是哈希比对）
        login = self.client.post("/api/auth/login", json={"username": self.target, "password": self.NEW_PW})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn("token", login.json())
        dead = self.client.post("/api/auth/login", json={"username": self.target, "password": self.BASE_PW})
        self.assertEqual(dead.status_code, 401)

    def test_self_noop_role_patch_is_allowed(self):
        """自己传回当前角色（例如客户端把整个用户对象 PATCH 回来）不该被拒，
        也不该把自己踢下线 —— 只有「真的变更」才需要守卫与吊销。"""
        aid = dbm.query_one("SELECT id FROM users WHERE username=?", (self.admin,))["id"]
        tok = security.sign_token(self.admin, "admin")
        r = self.client.patch(f"/api/users/{aid}", json={"role": "admin"}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.query_one("SELECT role FROM users WHERE id=?", (aid,))["role"], "admin")
        self.assertEqual(security.verify_token(tok)["sub"], self.admin, "无变更不得吊销自己的 token")

    def test_empty_body_makes_no_change(self):
        """非空断言：空 body 不改 role/密码，也不吊销 —— 避免误伤在线会话。"""
        tok = security.sign_token(self.target, "admin")
        before = dbm.query_one("SELECT role,pass_hash FROM users WHERE id=?", (self.tid,))
        r = self.client.patch(f"/api/users/{self.tid}", json={}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.query_one("SELECT role,pass_hash FROM users WHERE id=?", (self.tid,)), before)
        self.assertEqual(security.verify_token(tok)["sub"], self.target)

    def test_missing_user_is_404(self):
        r = self.client.patch("/api/users/999999", json={"role": "viewer"}, headers=self.admin_headers)
        self.assertEqual(r.status_code, 404, r.text)


class TestUserCreateErrorHonesty(unittest.TestCase):
    """POST /api/users 只能把「唯一约束冲突」说成用户名已存在。

    旧写法 `except Exception`：磁盘满 / 库被锁 / WAL 写不进都返回
    {"error":"用户名已存在"}（实测），管理员换个名字重试仍然是这句，
    面板日志里也没有任何线索。
    """

    PW = "CreatePassw0rd"  # 建用户用的口令（≥8 位，过接口校验）

    def setUp(self):
        from starlette.testclient import TestClient

        from app import main as _main

        # raise_server_exceptions=False：我们要的是「应用把异常兜成 500 响应」这件事，
        # 默认 True 会让 TestClient 直接把异常抛回测试里，反而看不到状态码。
        self.client = TestClient(_main.app, raise_server_exceptions=False)
        self.admin = _uid("uce_admin_")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.admin, security.hash_pass("Password123"), "admin"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.admin,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.admin, 'admin')}"}

    def test_duplicate_name_still_reports_conflict(self):
        """非空断言（正向）：真冲突仍然是 400 + 用户名已存在，能力不回退。"""
        name = _uid("uce_dup_")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (name,))
        body = {"username": name, "password": self.PW}
        self.assertEqual(self.client.post("/api/users", json=body, headers=self.headers).status_code, 200)
        r = self.client.post("/api/users", json=body, headers=self.headers)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("已存在", r.text)

    def test_disk_full_is_not_reported_as_duplicate_name(self):
        def boom(sql, params=()):
            if "INSERT INTO users" in sql:
                raise sqlite3.OperationalError("database or disk is full")
            return None

        name = _uid("uce_disk_")
        with mock.patch.object(dbm, "execute", boom):
            r = self.client.post("/api/users", json={"username": name, "password": self.PW}, headers=self.headers)
        self.assertNotIn("已存在", r.text, f"存储故障不得伪装成用户名冲突，实到：{r.status_code} {r.text}")
        self.assertEqual(r.status_code, 500, r.text)

    def test_unrelated_crash_is_not_swallowed(self):
        name = _uid("uce_crash_")
        with mock.patch.object(dbm, "execute", side_effect=RuntimeError("boom")):
            r = self.client.post("/api/users", json={"username": name, "password": self.PW}, headers=self.headers)
        self.assertEqual(r.status_code, 500, r.text)


class TestDeletedUserNameReuse(unittest.TestCase):
    """删号 → 同名重建，旧 token 必须永久失效。

    users_delete 里那句 `security.bump_user_epoch(u["username"])` 看着多余
    （用户都删了，吊销谁的 token？），实际是唯一挡住这件事的东西：
    从没改过密的用户，token 里带的是**全局** epoch；删号时若不写一条
    `token_epoch:<name>`，把这个名字发给下一个人时，旧 token 的 ver 又会
    和 _user_epoch(name) 相等（双双回落全局 epoch）——离职管理员的 admin
    权限原地复活。实测（手工 DELETE FROM settings WHERE key LIKE 'token_epoch:%'
    模拟一次「清理孤儿配置」）旧 token 直接 verify 成功且 role=admin。

    所以这里锁两件事：删号必须留下 per-user epoch；epoch 行不得被当垃圾清掉。
    """

    def setUp(self):
        self.admin = _uid("durn_admin_")
        self.target = _uid("durn_target_")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.admin, security.hash_pass("Password123"), "admin"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.admin,))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.target,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.admin, 'admin')}"}

    def _client(self):
        from starlette.testclient import TestClient

        from app import main as _main

        return TestClient(_main.app, raise_server_exceptions=False)

    def test_delete_bumps_epoch_then_recreate_rejects_old_token(self):
        client = self._client()
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.target, security.hash_pass("Password123"), "admin"),
        )
        uid = dbm.query_one("SELECT id FROM users WHERE username=?", (self.target,))["id"]
        # 不经过改密，直接签发 —— token 带的是全局 epoch（最危险的那种）
        old_token = security.sign_token(self.target, "admin")
        self.assertEqual(security.verify_token(old_token)["role"], "admin", "基线：旧 token 是 admin")

        r = client.delete(f"/api/users/{uid}", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(
            dbm.get_setting(f"token_epoch:{self.target}"),
            "删号必须留下 per-user epoch，否则同名重建后旧 token 会重新有效",
        )
        with self.assertRaises(jwt.InvalidTokenError):
            security.verify_token(old_token)

        # 同名重建，交给下一个人
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.target, security.hash_pass("AnotherPass9"), "viewer"),
        )
        with self.assertRaises(jwt.InvalidTokenError, msg="同名重建后，前一个人的 token 必须仍然失效"):
            security.verify_token(old_token)

    def test_epoch_row_survives_a_settings_sweep(self):
        """守卫「别把 token_epoch:* 当孤儿配置清掉」这条不变量：
        产品代码里没有任何删 settings 的路径，将来若有人加，这条要红。"""
        joined = "\n".join(p.read_text(encoding="utf-8") for p in (config.BASE / "backend" / "app").rglob("*.py"))
        self.assertNotIn(
            "DELETE FROM settings",
            joined,
            "app/ 里出现了删除 settings 的语句：token_epoch:<user> 被清掉会让同名重建复活旧 token",
        )


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


# ---------------------------------------------------------------- 查询参数解析
class TestQueryParamParsing(unittest.TestCase):
    def test_parse_lines_never_raises(self):
        for bad in (None, "", "abc", "1.5", "[]", "1e3"):
            self.assertEqual(parse_lines(bad), 200, f"{bad!r} 应回落默认值而不是抛错")

    def test_parse_lines_clamps_both_ends(self):
        self.assertEqual(parse_lines("-5"), 1, "下界必须 clamp：负数会进 journalctl -n 造成诡异行为")
        self.assertEqual(parse_lines("999999"), 2000, "上界必须 clamp，否则一条请求拉爆日志")
        self.assertEqual(parse_lines("50"), 50)
        self.assertEqual(parse_lines("0"), 200, "0 视为未指定")


class TestAuditLimitClamp(unittest.TestCase):
    def test_negative_limit_does_not_dump_whole_table(self):
        """回归：SQLite 的 LIMIT 负数=不限制，只 clamp 上界会被 ?limit=-1 绕过。"""
        import sqlite3

        c = sqlite3.connect(":memory:")
        c.execute("create table t(x)")
        c.executemany("insert into t values(?)", [(i,) for i in range(300)])
        # 旧写法：min(limit, 500) —— limit=-1 原样进 SQL
        self.assertEqual(len(c.execute("select * from t limit ?", (min(-1, 500),)).fetchall()), 300,
                         "旧写法确实把全表吐出来了（这正是漏洞）")
        # 新写法：max(1, min(limit, 500))
        self.assertEqual(len(c.execute("select * from t limit ?", (max(1, min(-1, 500)),)).fetchall()), 1)


# ---------------------------------------------------------------- nginx 配置守卫
class TestNginxConfGuard(unittest.IsolatedAsyncioTestCase):
    """`nginx -t` 失败必须完全还原现场，且路径守卫不能被 `..` 绕过。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_ng_")
        self.path = f"{self.dir}/panel-new.conf"
        self._old_dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [self.dir]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self._old_dirs)

    async def _save_and_fail(self, content: str):
        async def fake_run(cmd, args, **kw):
            if cmd == "nginx":
                return {"code": 1, "out": "syntax error"}
            return {"code": 0, "out": ""}

        with mock.patch.object(nginx_ops, "run", fake_run), self.assertRaises(RuntimeError):
            await nginx_ops.save_app_config(self.path, content)

    async def test_new_bad_file_is_removed(self):
        # 文件原本不存在 => 本次是新建；坏配置留在 conf.d 会连带拖垮所有 vhost 操作
        await self._save_and_fail("server { broken")
        self.assertFalse(os.path.exists(self.path), "新建的坏配置必须删除")

    async def test_existing_file_restored(self):
        Path(self.path).write_text("# original\n")
        await self._save_and_fail("server { broken")
        self.assertEqual(Path(self.path).read_text(), "# original\n")

    async def test_dotdot_cannot_escape_conf_dir(self):
        """回归：旧守卫对未归一化字符串做前缀匹配，
        `/etc/nginx/conf.d/../../../../tmp/x.conf` 能一路放行并写到 /tmp。"""
        escape = f"{self.dir}/../../../../tmp/cp_escape_proof.conf"
        for fn in (nginx_ops.save_app_config, nginx_ops.quick_edit_config):
            with self.assertRaises(RuntimeError, msg=f"{fn.__name__} 必须拒绝越界路径"):
                await (fn(escape, "server {}") if fn is nginx_ops.save_app_config
                       else fn(escape, "ws"))
        self.assertFalse(os.path.exists("/tmp/cp_escape_proof.conf"), "越界文件不能被创建")

    async def test_non_nginx_path_rejected(self):
        with self.assertRaises(RuntimeError):
            await nginx_ops.save_app_config("/etc/crontab", "* * * * * rm -rf /")

    async def test_swapparent_file_rejected(self):
        with self.assertRaises(RuntimeError):
            await nginx_ops.save_app_config(f"{self.dir}/x.conf.swp", "server {}")

# ---------------------------------------------------------------- 应用回滚
class TestRollback(unittest.IsolatedAsyncioTestCase):
    """回归：回滚必须真的退到上一个版本，而不是 reset 到当前 commit 变成空操作。"""

    def setUp(self):
        import subprocess

        self.repo = tempfile.mkdtemp(prefix="cp_rb_")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t"}

        def commit(msg):
            Path(self.repo, "VERSION").write_text(msg)
            subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, env=env)
            subprocess.run(["git", "commit", "-m", msg], cwd=self.repo, check=True, env=env,
                           stdout=subprocess.DEVNULL)
            return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo, check=True,
                                  env=env, capture_output=True, text=True).stdout.strip()

        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True, env=env)
        self.sha_v1 = commit("v1")
        self.sha_v2 = commit("v2")
        self.app_id = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,'node',?,'main',?,'node x.js')",
            (f"rb{os.getpid()}_{id(self)}", "https://example.com/x.git", self.repo),
        )
        # 两次成功部署：当前运行 v2（最新成功记录也是 v2 —— 旧实现会 reset 到 v2，等于没回滚）
        for sha in (self.sha_v1, self.sha_v2):
            dbm.execute("INSERT INTO deployments(app_id,status,commit_sha) VALUES(?,'success',?)", (self.app_id, sha))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (self.app_id,))
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE app_id=?", (self.app_id,))
        self.addCleanup(_rmtree, self.repo)

    async def test_rolls_back_to_previous_version_not_current(self):
        with mock.patch.object(apps_service, "service_action", new=mock.AsyncMock()) as restart:
            res = await apps_service.rollback_app(self.app_id)
        self.assertEqual(res["commit"], self.sha_v1, "必须回到上一个成功版本")
        self.assertEqual(res["from"], self.sha_v2)
        restart.assert_awaited_once()
        head = await apps_service._current_sha(self.repo)
        self.assertEqual(head, self.sha_v1, "工作区必须真的切换过去")

    async def test_no_older_version_reports_clearly(self):
        # 部署历史只有当前 commit：应明确报错而不是静默"成功"
        dbm.execute("UPDATE deployments SET commit_sha=? WHERE app_id=?", (self.sha_v2, self.app_id))
        with self.assertRaises(RuntimeError) as ctx:
            await apps_service.rollback_app(self.app_id)
        self.assertIn("没有更早的成功版本", str(ctx.exception))



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

    def test_template_env_matches_env_writer_schema(self):
        """回归：模板 env 曾用 {"key","value"}，而 write_env_file 只认 {"k","v"}，
        模板声明的 PORT/NODE_ENV 被静默丢弃，.panel.env 写成空文件，
        应用拿不到端口却显示"部署成功"。这里把两个模块的契约钉死。"""
        d = tempfile.mkdtemp(prefix="cp_tpl_")
        for tpl in templates.list_templates():
            merged = templates.apply_template({"name": "t", "type": "node", "start_cmd": "npm start",
                                               "path": d, "template": tpl["key"]})
            if not merged.get("env"):
                continue
            for item in merged["env"]:
                self.assertIn("k", item, f"模板 {tpl['key']} 的 env 条目必须是 {{k,v}}：{item}")
                self.assertIn("v", item)
            apps_service.write_env_file({"name": "t", "path": d, "env": json.dumps(merged["env"])})
            body = Path(d, ".panel.env").read_text()
            self.assertIn("=", body, f"模板 {tpl['key']} 的 env 必须真的落进 .panel.env")

    def test_env_writer_silently_drops_mismatched_shape(self):
        """证明"静默丢弃"确实发生过：错误结构不报错，只写出空文件。"""
        d = tempfile.mkdtemp(prefix="cp_tpl2_")
        bad = [{"key": "PORT", "value": "3000"}]
        apps_service.write_env_file({"name": "b", "path": d, "env": json.dumps(bad)})
        self.assertEqual(Path(d, ".panel.env").read_text(), "\n", "旧结构会被完全忽略")

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


# ---------------------------------------------------------------- 限速来源 IP
class TestClientIp(unittest.TestCase):
    """回归：反代场景下限速用的 IP 不能取客户端可伪造的 XFF 最左值。"""

    def _req(self, headers: dict, peer: str = "127.0.0.1"):
        from types import SimpleNamespace

        from starlette.datastructures import Headers

        return SimpleNamespace(headers=Headers(raw=[(k.lower().encode(), v.encode()) for k, v in headers.items()]),
                               client=SimpleNamespace(host=peer))

    def test_real_ip_header_wins(self):
        with mock.patch.object(config, "TRUST_PROXY", True):
            r = self._req({"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
            self.assertEqual(main.client_ip(r), "203.0.113.9")

    def test_forged_leftmost_xff_is_ignored(self):
        # 攻击者伪造最左值，nginx 把真实对端追加到最右
        with mock.patch.object(config, "TRUST_PROXY", True):
            r = self._req({"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
            self.assertEqual(main.client_ip(r), "198.51.100.7")

    def test_empty_rightmost_falls_back(self):
        with mock.patch.object(config, "TRUST_PROXY", True):
            r = self._req({"X-Forwarded-For": "1.2.3.4, , 198.51.100.7, "})
            self.assertEqual(main.client_ip(r), "198.51.100.7")

    def test_no_proxy_uses_peer(self):
        with mock.patch.object(config, "TRUST_PROXY", False):
            r = self._req({"X-Forwarded-For": "1.2.3.4"}, peer="10.1.2.3")
            self.assertEqual(main.client_ip(r), "10.1.2.3")


# ---------------------------------------------------------------- 告警巡检解析
CERTBOT_OUT = """Saving debug log to /var/log/letsencrypt/letsencrypt.log
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
Found the following certs:
  Certificate Name: healthy.example.com
    Serial Number: aa11
    Key Type: ECDSA
    Identifiers: healthy.example.com
    Expiry Date: 2027-01-01 00:00:00+00:00 (VALID: 55 days)
    Certificate Path: /etc/letsencrypt/live/healthy.example.com/fullchain.pem
    Private Key Path: /etc/letsencrypt/live/healthy.example.com/privkey.pem
  Certificate Name: soon.example.com
    Expiry Date: 2026-10-12 00:00:00+00:00 (VALID: 5 days)
  Certificate Name: hours.example.com
    Expiry Date: 2026-10-07 20:00:00+00:00 (VALID: 6 hour(s))
  Certificate Name: oneday.example.com
    Expiry Date: 2026-10-08 00:00:00+00:00 (VALID: 1 day)
  Certificate Name: dead.example.com
    Expiry Date: 2026-01-01 00:00:00+00:00 (INVALID: EXPIRED)
  Certificate Name: revoked.example.com
    Expiry Date: 2026-12-01 00:00:00+00:00 (INVALID: REVOKED)
  Certificate Name: staging.example.com
    Expiry Date: 2026-12-01 00:00:00+00:00 (INVALID: TEST_CERT)
- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
"""


class TestAlertParsing(unittest.TestCase):
    """回归：证书巡检此前找的是 `Days Left:`——certbot 从不打印这个字段，
    所以 SSL 告警一直是静默空转，证书真的过期了也不响。
    解析规则必须贴着 certbot 真实输出（`(VALID: N days)` / `(INVALID: EXPIRED)`）。
    """

    def _by_name(self, out):
        return {c["name"]: c for c in alerts_service.parse_certbot(out)}

    def test_parses_every_cert_not_just_the_first(self):
        certs = self._by_name(CERTBOT_OUT)
        self.assertEqual(len(certs), 7, "每张证书都要独立成块，不能靠分隔线切")

    def test_days_extracted_from_real_format(self):
        certs = self._by_name(CERTBOT_OUT)
        self.assertEqual(certs["healthy.example.com"]["days"], 55)
        self.assertEqual(certs["soon.example.com"]["days"], 5)
        self.assertEqual(certs["oneday.example.com"]["days"], 1)
        self.assertFalse(certs["healthy.example.com"]["invalid"])

    def test_sub_day_expiry_counts_as_urgent(self):
        """`(VALID: 6 hour(s))` 里没有 day，必须算 0 天而不是漏掉。"""
        self.assertEqual(self._by_name(CERTBOT_OUT)["hours.example.com"]["days"], 0)

    def test_invalid_only_for_expiry_or_revocation(self):
        certs = self._by_name(CERTBOT_OUT)
        self.assertTrue(certs["dead.example.com"]["invalid"])
        self.assertTrue(certs["revoked.example.com"]["invalid"])
        # 测试证书不该按"过期"报警，否则 --staging 的机器天天误报
        self.assertFalse(certs["staging.example.com"]["invalid"])

    def test_no_certs_and_empty_output(self):
        self.assertEqual(alerts_service.parse_certbot("No certificates found."), [])
        self.assertEqual(alerts_service.parse_certbot(""), [])

    def test_low_threshold_triggers_alert_message(self):
        """整条链路：certbot 输出 → 阈值判定 → 推送文案。"""
        sent: list[tuple[str, str]] = []

        async def fake_notify(text):
            sent.append(("msg", text))
            return True

        with (mock.patch.object(alerts_service, "notify", fake_notify),
              mock.patch.object(dbm, "get_setting", lambda k: None),
              mock.patch.object(dbm, "set_setting", lambda k, v: None),
              mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 10),
              mock.patch.object(alerts_service, "run", _fake_run_ok)):
            asyncio.run(alerts_service.run_checks())
        self.assertTrue(any("soon.example.com" in t for _, t in sent), sent)
        self.assertTrue(any("dead.example.com" in t for _, t in sent), sent)
        self.assertFalse(any("healthy.example.com" in t for _, t in sent), "55 天不该报警")

    def test_ssl_check_skipped_when_threshold_zero(self):
        """阈值 0 = 关闭证书巡检，是既有语义，别改。"""
        called = []

        async def spy_run(cmd, args, **kw):
            called.append(cmd)
            return {"code": 0, "out": ""}

        with (mock.patch.object(alerts_service, "run", spy_run),
              mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 10),
              mock.patch.object(dbm, "get_setting", lambda k: "0" if k == "alert_ssl_days" else None),
              mock.patch.object(alerts_service, "notify", _noop_notify)):
            asyncio.run(alerts_service.run_checks())
        self.assertIn("systemctl", called, "其它巡检照常跑")
        self.assertNotIn("certbot", called, "关闭后不该调用 certbot")


async def _noop_notify(_text):
    return True


async def _fake_run_ok(cmd, args, **kw):
    if cmd == "certbot":
        return {"code": 0, "out": CERTBOT_OUT}
    if cmd == "systemctl":
        return {"code": 0, "out": ""}
    return {"code": 0, "out": ""}


# ---------------------------------------------------------------- 失败单元解析
class TestFailedUnitParsing(unittest.TestCase):
    """回归：systemd 不可用/无权限时，报错正文被拆成单元名，
    实测每 30 分钟推送一次「存在 failed 状态服务: System, Failed」。"""

    SYS_ERR = ("System has not been booted with systemd as init system (PID 1). "
               "Can't operate.\nFailed to connect to bus: Host is down")
    PERM_ERR = "Permission denied /run/systemd/private"

    def test_error_text_yields_no_units(self):
        for text in (self.SYS_ERR, self.PERM_ERR, "", "No units *are* failed."):
            self.assertEqual(alerts_service._failed_units(text), [], text[:40])

    def test_real_units_still_parsed(self):
        out = ("  nginx.service loaded failed failed The nginx HTTP and reverse proxy server\n"
               "  postgresql@16-main.service loaded failed failed PostgreSQL RDBMS\n"
               "  docker.socket loaded failed failed Docker Socket for the API\n")
        self.assertEqual(alerts_service._failed_units(out),
                         ["nginx.service", "postgresql@16-main.service", "docker.socket"])

    def test_duplicate_units_collapse(self):
        out = "  a.service loaded failed failed x\n  a.service loaded failed failed y\n"
        self.assertEqual(alerts_service._failed_units(out), ["a.service"])

    def test_nonzero_code_does_not_alert(self):
        """命令失败时必须整段跳过，不能只看输出文本。

        后缀过滤挡不住"正文里第一列本来就是合法单元名"的情况（下面是构造的
        部分失败输出），所以退出码这道防线必须独立生效。
        """
        sent = []

        async def fake_notify(text):
            sent.append(text)
            return True

        async def failing_run(cmd, args, **kw):
            return {"code": 1, "out": "  nginx.service loaded failed failed nginx\n"}

        with (mock.patch.object(alerts_service, "run", failing_run),
              mock.patch.object(alerts_service, "notify", fake_notify),
              mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 10),
              mock.patch.object(alerts_service, "parse_certbot", lambda o: []),
              mock.patch.object(dbm, "get_setting", lambda k: None)):
            asyncio.run(alerts_service.run_checks())
        self.assertEqual(sent, [], "systemctl 失败时不该发任何告警")

