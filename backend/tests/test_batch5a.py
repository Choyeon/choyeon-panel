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
from app import security  # noqa: E402
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
    """配置文件删了但 nginx 没通过校验：不能静默返回 ok。"""

    def setUp(self):
        # CONF_DIR 在 import 时绑成了 /etc/nginx/conf.d，测试必须指到临时目录，
        # 否则写进去的半截 server 块会被真实 nginx 读到。
        self.dir = tempfile.mkdtemp(prefix="cp_5a_conf_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.patcher = mock.patch.object(nginx_ops, "CONF_DIR", self.dir)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _vhost(self, name):
        path = Path(nginx_ops.vhost_path(name))
        path.write_text("server {};\n")
        return path

    def test_raises_when_nginx_test_fails(self):
        name = f"roll{os.getpid()}"
        path = self._vhost(name)

        async def fake_run(cmd, args, **_kw):
            code = 1 if (cmd == "nginx" or args[:1] == ["reload"]) else 0
            return {"code": code, "out": 'nginx: [emerg] unknown directive in /etc/nginx/nginx.conf:20', "err": ""}

        with mock.patch.object(nginx_ops, "run", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(nginx_ops.remove_vhost(name))
        self.assertIn("nginx 校验未通过", str(ctx.exception))
        self.assertFalse(path.exists(), "报错时文件确实已经删除，调用方要按此说明状态")

    def test_reloads_when_config_is_valid(self):
        name = f"good{os.getpid()}"
        self._vhost(name)
        calls = []

        async def fake_run(cmd, args, **_kw):
            calls.append(" ".join([cmd, *args]))
            return {"code": 0, "out": "syntax is ok", "err": ""}

        with mock.patch.object(nginx_ops, "run", fake_run):
            asyncio.run(nginx_ops.remove_vhost(name))
        self.assertIn("systemctl reload nginx", calls)


class TestSiteRemoveReportsFailedReload(unittest.TestCase):
    """删除站点时 nginx 校验失败必须回 400，不能让界面显示"已移除"。"""

    def test_returns_400_with_honest_wording(self):
        # 生产里 remove_vhost 抛的就是这句：文件已删、面板没 reload、附上 nginx 原文
        msg = ('配置已删除，但 nginx 校验未通过，面板没有重载（该域名可能仍在按旧配置提供服务）：'
               'nginx: [emerg] open() "/etc/nginx/conf.d/panel-x.conf" failed (2: No such file or directory)')

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
        self.assertNotIn("/etc/nginx", body)
        self.assertIn("配置已删除", body)
        # 删除场景没有回滚：说"已回滚该文件"会让人以为旧 vhost 还在，实际文件已经没了
        self.assertNotIn("已回滚", body)


if __name__ == "__main__":
    unittest.main()
