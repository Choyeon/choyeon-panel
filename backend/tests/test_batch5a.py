"""批次⑤A（数据与运维接口正确性）的回归测试。

锁住这一轮在生产实测里抓到的四类错误：
接口参数被吞掉后"看起来成功了但做错了地方"（SQL 控制台跑在默认库、
角色笔误被静默升成 admin），以及"检查项说没事而机器上其实没人干活"
（管理员判定按账号总数、vhost 删除后 nginx 校验失败被静默吞掉）。
"""

import asyncio
import itertools
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5a_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_5a_roots_"))

from app import database as dbm  # noqa: E402
from app.routers import dbops, extras, meta, sites  # noqa: E402
from app.services import nginx_ops, pg_service  # noqa: E402


def _req(body=None, role="admin", sub="tester"):
    async def _json():
        return body

    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub), json=_json)


_seq = itertools.count()


def _uid(prefix: str) -> str:
    """唯一且 ≤32 位的用户名（走 USERNAME_RE 的那个正则）。"""
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


class TestSqlConsoleTargetDb(unittest.TestCase):
    """SQL 控制台必须跟着界面选中的库走。"""

    def test_routes_to_requested_db_and_echoes_it(self):
        seen = {}

        async def _sql(sql, db=None):
            seen["args"] = (sql, db)
            return "1 row"

        with mock.patch.object(pg_service, "query_sql", _sql), \
             mock.patch.object(extras.aio_db, "audit"), mock.patch.object(dbops.aio_db, "audit"):
            r = asyncio.run(dbops.pg_query(_req({"sql": "SELECT 1", "db": "rosetta"})))
        self.assertEqual(seen["args"], ("SELECT 1", "rosetta"))
        # 回带 db：界面据此写明执行位置，用户才知道语句到底落在哪个库
        self.assertEqual(r["db"], "rosetta")

    def test_defaults_to_postgres_when_omitted(self):
        called = {}

        async def _sql(sql, db=None):
            called["db"] = db
            return ""

        with mock.patch.object(pg_service, "query_sql", _sql), mock.patch.object(dbops.aio_db, "audit"):
            r = asyncio.run(dbops.pg_query(_req({"sql": "SELECT 1"})))
        self.assertEqual(called["db"], "postgres")
        self.assertEqual(r["db"], "postgres")

    def test_rejects_illegal_db_name(self):
        """库名要进 psql 命令行，非法名直接 400，不能拼进去。"""
        with mock.patch.object(pg_service, "query_sql") as never:
            r = asyncio.run(dbops.pg_query(_req({"sql": "SELECT 1", "db": "pg; drop table users;--"})))
        self.assertEqual(r.status_code, 400)
        never.assert_not_called()

    def test_empty_sql_is_rejected(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(pg_service.query_sql("   "))


class TestRoleValidation(unittest.TestCase):
    """角色只认 security.ROLES：写错的值必须 400，不能被"else admin"吞掉。"""

    def _user(self, role: str = "viewer") -> tuple[int, str]:
        name = _uid("r")
        dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)", (name, "x", role))
        uid = dbm.query_one("SELECT id FROM users WHERE username=?", (name,))["id"]
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE id=?", (uid,))
        return uid, name

    def test_create_rejects_unknown_role(self):
        r = asyncio.run(extras.users_create(_req({
            "username": _uid("x"), "password": "aaaaaaaa1", "role": "readonly"})))
        self.assertEqual(r.status_code, 400, "把 role 打错不能静默变成 admin")
        self.assertIn("角色只能是", r.body.decode())

    def test_create_does_not_persist_bad_role(self):
        name = _uid("x")
        asyncio.run(extras.users_create(_req({"username": name, "password": "aaaaaaaa1", "role": "BOGUS"})))
        self.assertIsNone(dbm.query_one("SELECT id FROM users WHERE username=?", (name,)))

    def test_create_accepts_viewer(self):
        name = _uid("v")
        r = asyncio.run(extras.users_create(_req({"username": name, "password": "aaaaaaaa1", "role": "viewer"})))
        self.assertEqual(r, {"ok": True})
        u = dbm.query_one("SELECT id,role FROM users WHERE username=?", (name,))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE id=?", (u["id"],))
        self.assertEqual(u["role"], "viewer")

    def test_patch_rejects_unknown_role(self):
        """role 笔误在 PATCH 上同样要 400：旧写法把人悄悄升成 admin 还返回 200。"""
        uid, _ = self._user("viewer")
        r = asyncio.run(extras.users_update(str(uid), _req({"role": "supadmin"})))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(dbm.query_one("SELECT role FROM users WHERE id=?", (uid,))["role"], "viewer")
        self.assertEqual(dbm.query_one("SELECT pass_hash FROM users WHERE id=?", (uid,))["pass_hash"], "x")


class TestAdminCheckCountsAdmins(unittest.TestCase):
    """doctor 的「管理员账号」要数 admin，不是数账号总数。

    直接喂计数而不是往共享临时库里塞用户：同一 pytest 进程里所有测试模块用的是
    同一个 CP_DATA_DIR，别处残留的账号会让"有几个管理员"变成随机数。
    """

    def _run(self, total: int, admins: int) -> dict:
        def fake(sql, params=()):
            n = admins if "role='admin'" in sql else total
            return {"c": n}

        with mock.patch.object(dbm, "query_one", fake):
            return asyncio.run(meta._check_admin())

    def test_only_viewers_is_fail(self):
        """只剩 viewer 的机器：旧实现按 COUNT(*)>0 报 pass，而没人能进面板改配置。"""
        res = self._run(total=2, admins=0)
        self.assertEqual(res["status"], "fail")
        self.assertIn("没有管理员", res["detail"])
        self.assertIn("user create", res["fix"])

    def test_no_users_at_all_is_fail(self):
        res = self._run(total=0, admins=0)
        self.assertEqual(res["status"], "fail")
        self.assertIn("抢注", res["detail"])

    def test_admin_present_is_pass(self):
        res = self._run(total=3, admins=1)
        self.assertEqual(res["status"], "pass")
        self.assertIn("1 个管理员", res["detail"])


class TestRemoveVhostFailsLoudly(unittest.TestCase):
    """配置文件删了但 nginx 没通过校验：不能静默返回 ok，也不能把用户的站点删没。"""

    def setUp(self):
        # CONF_DIR 在 import 时绑成了 /etc/nginx/conf.d，测试必须指到临时目录，
        # 否则写进去的半截 server 块会被真实 nginx 读到。
        self.dir = tempfile.mkdtemp(prefix="cp_5a_conf_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.patcher = mock.patch.object(nginx_ops, "CONF_DIR", self.dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _vhost(self, name, content="server {};\n"):
        path = Path(nginx_ops.vhost_path(name))
        path.write_text(content)
        return path

    def test_restores_file_when_nginx_test_fails(self):
        # 校验不过的原因通常在别处（另一个文件引用了本文件的 upstream，或机器上本来
        # 就有坏配置）。旧实现删了就删了：调用方收到 400，界面显示"移除失败"，
        # 而那份保住域名的配置已经没了，下一次 reload 站点直接下线。
        name = f"roll{os.getpid()}"
        path = self._vhost(name, "# 保留我\nserver {};\n")

        async def fake_run(cmd, args, **_kw):
            code = 1 if cmd == "nginx" else 0
            return {"code": code, "out": 'nginx: [emerg] unknown directive in /etc/nginx/nginx.conf:20'}

        with mock.patch.object(nginx_ops, "run", fake_run), self.assertRaises(RuntimeError) as ctx:
            asyncio.run(nginx_ops.remove_vhost(name))
        self.assertIn("恢复", str(ctx.exception))
        self.assertTrue(path.exists(), "校验不过必须把文件放回去，域名照旧由它服务")
        self.assertEqual(path.read_text(), "# 保留我\nserver {};\n")

    def test_reloads_when_config_is_valid(self):
        name = f"good{os.getpid()}"
        self._vhost(name)
        calls = []

        async def fake_run(cmd, args, **_kw):
            calls.append(" ".join([cmd, *args]))
            return {"code": 0, "out": "syntax is ok"}

        with mock.patch.object(nginx_ops, "run", fake_run):
            asyncio.run(nginx_ops.remove_vhost(name))
        self.assertIn("systemctl reload nginx", calls)

    def test_reload_failure_is_reported_not_swallowed(self):
        # 删除已经落盘、nginx -t 也过了，但 reload 失败：nginx 仍按内存里的旧配置服务。
        # 旧写法 `await run(...)` 直接丢掉返回值，接口回 ok，界面显示"已移除"，
        # 域名照旧可访问，直到下一次成功 reload 才暴露。
        name = f"badreload{os.getpid()}"
        path = self._vhost(name)

        async def fake_run(cmd, args, **_kw):
            return {"code": 1, "out": "nginx is not running"} if cmd == "systemctl" else {"code": 0, "out": "ok"}

        with mock.patch.object(nginx_ops, "run", fake_run), self.assertRaises(RuntimeError) as ctx:
            asyncio.run(nginx_ops.remove_vhost(name))
        self.assertIn("重载失败", str(ctx.exception))
        self.assertFalse(path.exists(), "这一步不回收文件：配置是对的，nginx 一起来就按新配置走")


class TestSiteRemoveReportsFailedReload(unittest.TestCase):
    """删除站点这一步失败必须回 400，不能让界面显示"已移除"。"""

    def _remove_with(self, msg: str):
        async def _boom(*_a, **_kw):
            raise RuntimeError(msg)

        name = _uid("rmv")
        dbm.execute("INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,?,?,?,?,?)",
                    (name, "node", "https://x/y.git", "main", "/tmp/x", "node i.js"))
        app = dbm.query_one("SELECT id FROM apps WHERE name=?", (name,))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app["id"],))

        with mock.patch.object(sites.nginx_ops, "remove_vhost", _boom):
            r = asyncio.run(sites.site_remove(app["id"], _req()))
        self.assertEqual(r.status_code, 400)
        body = r.body.decode()
        self.assertNotIn("/etc/nginx", body, "本机绝对路径不出接口")
        return body

    def test_restored_file_is_described_as_restored(self):
        # nginx -t 因别的原因不过时，remove_vhost 会把配置放回去：文案必须说"恢复了"，
        # 说成"已移除"或"删了没恢复"都让人无法判断站点现在到底是谁在服务。
        body = self._remove_with(
            'nginx 校验未通过，已把该域名的配置恢复回去（面板没有重载，站点仍按原配置提供服务）：'
            'nginx: [emerg] host not found in upstream "x" in /etc/nginx/conf.d/other.conf:3')
        self.assertIn("恢复", body)

    def test_deleted_but_not_reloaded_is_not_reported_as_rollback(self):
        # 校验通过、reload 失败：文件确实没了，这时说"已回滚该文件"就是反话
        body = self._remove_with(
            '配置已删除，但 nginx 校验通过、重载失败（该域名可能仍在按旧配置提供服务）：'
            'Failed to reload nginx.service: Unit nginx.service is not active')
        self.assertIn("配置已删除", body)
        self.assertNotIn("已回滚", body)


if __name__ == "__main__":
    unittest.main()
