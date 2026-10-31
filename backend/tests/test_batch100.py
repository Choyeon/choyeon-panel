"""批次100：一次保存的多个字段必须同一事务，中途失败不许留下「半套新半套旧」。

`dbm.execute()` 每条语句自己 commit，于是这几处写路径都是"多条独立写"：
- 管理员改账号（role + pass_hash + nickname + avatar）逐条写；
- 告警设置一次保存 N 个键，逐条 `set_setting`；
- 自己改资料（nickname + avatar）逐条写；
- 删应用 / 删备份任务 / 删异地目标的「主行 + 台账行」逐条写。

任意一条撞上车锁超时、磁盘满或约束冲突，前面那些已经落库：请求是 500、审计一条不记，
管理员以为没改成，而库里那个人真的已经被升成 admin、SMTP 地址已经换了而口令还是旧的。
删对象那一半更隐蔽——主行删掉而台账没删，剩下的孤儿行没有任何入口能读到，也没有入口能
再删它们。这里补 `execute_many` / `set_settings`（一把锁、一次 commit、失败 rollback），
写路径全部改成一次调用，并把事务边界钉进断言。
"""

import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp100_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp100_roots_"))

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import aio_db, database as dbm, main  # noqa: E402
from app.routers import extras  # noqa: E402
from app.services import cloud_backup  # noqa: E402


def _req(body: dict, sub: str = "boss", role: str = "admin"):
    async def _json():
        return body

    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub),
                           json=_json, headers={}, client=SimpleNamespace(host="ut"))


def _mk_user(name: str, role: str = "viewer") -> int:
    dbm.execute("DELETE FROM users WHERE username=?", (name,))
    return dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)", (name, "oldhash", role))


class _FlakyConn:
    """包一层真连接：语句或参数里含 needle 的那条写直接失败，其余原样转发。

    `sqlite3.Connection.execute` 是只读属性，patch.object 换不掉方法，只能把整个连接对象
    换掉；commit/rollback 等一律转发，所以回滚走的还是真连接。
    """

    def __init__(self, real, needle: str):
        self._real, self._needle = real, needle

    def execute(self, sql, params=()):
        if self._needle in sql or self._needle in str(params):
            raise sqlite3.OperationalError("模拟：database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _flaky(needle: str):
    return mock.patch.object(dbm, "conn", _FlakyConn(dbm.conn, needle))


class DbLayerAtomicTests(unittest.TestCase):
    """底层先证明：第二条失败时第一条回滚，这是上面所有端点的前提。"""

    def test_第二条失败时第一条回滚(self):
        uid = _mk_user("cp100_rollback")
        with _flaky("nickname"), self.assertRaises(sqlite3.OperationalError):
            dbm.execute_many([
                ("UPDATE users SET role=? WHERE id=?", ("admin", uid)),
                ("UPDATE users SET nickname=? WHERE id=?", ("新名字", uid)),
            ])
        row = dbm.query_one("SELECT role,nickname FROM users WHERE id=?", (uid,))
        self.assertEqual(row["role"], "viewer", "回滚没兜住：角色已经写进去了")
        self.assertEqual(row["nickname"], "", "昵称那一列的默认值是空串，不是 NULL")

    def test_空批次不动库(self):
        dbm.execute_many([])
        dbm.set_settings({})

    def test_set_settings全批落库并失效缓存(self):
        dbm.set_settings({"cp100_a": "1", "cp100_b": "2"})
        self.assertEqual(dbm.get_setting("cp100_a"), "1")
        self.assertEqual(dbm.get_setting("cp100_b"), "2")
        # 认证类键写完必须掉缓存，否则改完 jwt_secret 还在用旧值签票
        dbm._auth_cache["cp100_a"] = ("stale", 0.0)
        dbm.set_setting("cp100_a", "3")
        self.assertNotIn("cp100_a", dbm._auth_cache)
        self.assertEqual(dbm.get_setting("cp100_a"), "3")

    def test_set_settings失败时一个键都没写(self):
        with _flaky("cp100_bad"), self.assertRaises(sqlite3.OperationalError):
            dbm.set_settings({"cp100_ok": "1", "cp100_bad": "2"})
        self.assertIsNone(dbm._read_setting("cp100_ok"))
        self.assertIsNone(dbm._read_setting("cp100_bad"))


class UserUpdateAtomicTests(unittest.TestCase):
    def test_改口令失败时角色没被升上去(self):
        uid = _mk_user("cp100_perm")
        with _flaky("pass_hash"), self.assertRaises(sqlite3.OperationalError):
            asyncio.run(extras.users_update(str(uid), _req({"role": "admin", "password": "S3cretPass-wd"})))
        self.assertEqual(dbm.query_one("SELECT role FROM users WHERE id=?", (uid,))["role"], "viewer",
                         "半套写入：口令没改，角色却已经升成 admin")

    def test_多字段一次调用落库(self):
        uid = _mk_user("cp100_all")
        calls = []
        orig = aio_db.execute_many

        async def spy(stmts):
            calls.append(stmts)
            return await orig(stmts)

        with mock.patch.object(aio_db, "execute_many", spy):
            r = asyncio.run(extras.users_update(
                str(uid), _req({"role": "admin", "nickname": "小张", "avatar": "/a.png"})))
        self.assertEqual(r["ok"], True)
        self.assertEqual(len(calls), 1, "多个字段分多次写就不是一个事务")
        self.assertEqual(len(calls[0]), 3)
        row = dbm.query_one("SELECT role,nickname,avatar FROM users WHERE id=?", (uid,))
        self.assertEqual((row["role"], row["nickname"], row["avatar"]), ("admin", "小张", "/a.png"))

    def test_路由不再逐条写库(self):
        src = (BACKEND / "app" / "routers" / "extras.py").read_text(encoding="utf-8")
        body = src.split("async def users_update")[1].split("@router")[0]
        self.assertIn("await aio_db.execute_many(", body)
        self.assertNotIn('dbm.execute("UPDATE users', body)


class AlertsSaveAtomicTests(unittest.TestCase):
    def test_一批键走一次set_settings(self):
        calls = []
        orig = aio_db.set_settings

        async def spy(pairs):
            calls.append(pairs)
            return await orig(pairs)

        body = {"alert_disk_pct": "80", "alert_ssl_days": "7", "alert_keep_rows": "500"}
        with mock.patch.object(aio_db, "set_settings", spy):
            r = asyncio.run(extras.alerts_save(_req(body)))
        self.assertEqual(r["ok"], True)
        self.assertEqual(len(calls), 1, "一次保存拆成多条独立写：第二条失败就留下一半新一半旧")
        self.assertEqual(calls[0], body)
        self.assertEqual(dbm.get_setting("alert_ssl_days"), "7")

    def test_路由里不再有逐条set_setting循环(self):
        src = (BACKEND / "app" / "routers" / "extras.py").read_text(encoding="utf-8")
        body = src.split("async def alerts_save")[1].split("@router")[0]
        self.assertIn("await aio_db.set_settings(accepted)", body)
        self.assertNotIn("dbm.set_setting(k, val)", body)


class ProfileAtomicTests(unittest.TestCase):
    def test_昵称与头像一次事务(self):
        uid = _mk_user("cp100_prof", role="admin")
        calls = []
        orig = aio_db.execute_many

        async def spy(stmts):
            calls.append(stmts)
            return await orig(stmts)

        with mock.patch.object(aio_db, "execute_many", spy):
            r = asyncio.run(main.update_profile(_req({"nickname": "花名", "avatar": "/b.png"}, sub="cp100_prof")))
        self.assertEqual(r["ok"], True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 2)
        row = dbm.query_one("SELECT nickname,avatar FROM users WHERE id=?", (uid,))
        self.assertEqual((row["nickname"], row["avatar"]), ("花名", "/b.png"))

    def test_改一半失败时一个字段都没改(self):
        _mk_user("cp100_prof2", role="admin")
        dbm.execute("UPDATE users SET nickname=? WHERE username=?", ("旧名", "cp100_prof2"))
        with _flaky("avatar"), self.assertRaises(sqlite3.OperationalError):
            asyncio.run(main.update_profile(
                _req({"nickname": "新名", "avatar": "/c.png"}, sub="cp100_prof2")))
        row = dbm.query_one("SELECT nickname,avatar FROM users WHERE username=?", ("cp100_prof2",))
        self.assertEqual(row["nickname"], "旧名", "头像没写成，昵称却已经换了——页面看到的是半套")


class DeletePathsAtomicTests(unittest.TestCase):
    """「主行 + 它的台账」必须一起消失：只删一半留下的是谁都读不到也删不掉的孤儿行。"""

    def _src(self, rel):
        return (BACKEND / rel).read_text(encoding="utf-8")

    def _pair(self, rel, pattern):
        m = re.search(pattern, self._src(rel), re.S)
        self.assertIsNotNone(m, f"{rel} 没有把两条 DELETE 收进同一个 execute_many")
        return m.group(1)

    def test_删应用的两条delete在一个调用里(self):
        seg = self._pair("app/services/apps_service.py", r"execute_many\(\[(.*?)\]\)")
        self.assertIn("DELETE FROM apps", seg)
        self.assertIn("DELETE FROM deployments", seg)

    def test_删备份任务的两条delete在一个调用里(self):
        seg = self._pair("app/services/backup_service.py", r"execute_many\(\[(.*?)\]\)")
        self.assertIn("DELETE FROM backups", seg)
        self.assertIn("DELETE FROM backup_pushes", seg)

    def test_删异地目标的两条delete在一个调用里(self):
        seg = self._pair("app/services/cloud_backup.py", r"dbm\.execute_many\(\[(.*?)\]\)")
        self.assertIn("DELETE FROM backup_pushes", seg)
        self.assertIn("DELETE FROM cloud_targets", seg)

    def test_真删目标时两张表一起清空(self):
        tid = dbm.execute(
            "INSERT INTO cloud_targets(name,endpoint,bucket,access_key) VALUES(?,?,?,?)",
            ("cp100-t", "https://s3.example.com", "b", "ak"))
        dbm.execute("INSERT INTO backup_pushes(target_id,backup_id,name,key,bytes) VALUES(?,?,?,?,?)",
                    (tid, 1, "x.gz", "pre/x.gz", 10))
        out = cloud_backup.delete_target(tid)
        self.assertEqual(out["records_dropped"], 1)
        self.assertIsNone(dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (tid,)))
        self.assertEqual(dbm.query("SELECT id FROM backup_pushes WHERE target_id=?", (tid,)), [])


class BatchApiShapeTests(unittest.TestCase):
    """批量写的线程池版必须存在：这几处都在 async 处理函数里，不能拿全局 RLock 直接写。"""

    def test_aio_db暴露两条批量写(self):
        self.assertTrue(callable(aio_db.execute_many))
        self.assertTrue(callable(aio_db.set_settings))

    def test_单键set_setting仍是同一份upsert(self):
        dbm.set_setting("cp100_single", "v1")
        self.assertEqual(dbm.get_setting("cp100_single"), "v1")
        dbm.set_setting("cp100_single", "v2")
        self.assertEqual(dbm.get_setting("cp100_single"), "v2")
        dbm.delete_setting("cp100_single")
        self.assertIsNone(dbm._read_setting("cp100_single"))


if __name__ == "__main__":
    unittest.main()
