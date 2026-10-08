"""backup_service / backup_runner 的回归测试。

覆盖的都是实测出来的坏法：
1. 校验只做一半：create 查 kind/target，update 什么都不查——
   `PATCH {"target": "all;DROP DATABASE x;--"}` 一路写进库，
   `PATCH {"kind": "sqlite"}` 抛的是 sqlite3.IntegrityError（不是业务错误）。
2. 备份文件名只到分钟：实测同一分钟跑两次，第二次把第一次原地覆盖，目录里只剩一份。
3. unit 目录写死 /etc/systemd/system，和 apps_service 的 CP_UNIT_DIR 开关分家。
4. create 的 enabled 用 `is False` 判定：传数字 0 会被记成 1，PATCH 同样 payload 记成 0。
5. delete_backup 对不存在的 id 静默成功，对外照样 {"ok": true}。
"""

import asyncio
import gzip
import inspect
import itertools
import os
import shutil
import tempfile

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_bk_ut_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from app import backup_runner as R  # noqa: E402
from app import config, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.services import backup_service as B  # noqa: E402

_seq = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _drop(bid: int):
    dbm.execute("DELETE FROM backups WHERE id=?", (bid,))


async def _no_timer(b):
    return None


def _fake_run(calls):
    """顶掉 systemctl：记录调用，返回成功。"""

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        calls.append([cmd, *args])
        return {"code": 0, "out": ""}

    return run


class TestTargetPairValidation(unittest.TestCase):
    """create 与 update 必须共用同一份 kind/target 校验。"""

    def setUp(self):
        self._p = mock.patch.object(B, "_sync_timer", _no_timer)
        self._p.start()
        self.addCleanup(self._p.stop)

    def _mk(self, **kw) -> int:
        row = {"kind": "pg", "target": "appdb", "schedule": "daily", "hour": 3, "minute": 30, "keep": 7, "enabled": 1}
        row.update(kw)
        cols = list(row)
        bid = dbm.execute(
            f"INSERT INTO backups({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row.values())
        )
        self.addCleanup(_drop, bid)
        return bid

    def test_create_rejects_injection_target(self):
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.create_backup({"kind": "pg", "target": "all;DROP DATABASE x;--"}))
        self.assertIn("PG 库名不合法", str(cm.exception))

    def test_update_rejects_injection_target(self):
        """旧写法在这里放行，坏值直接进库。"""
        bid = self._mk()
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.update_backup(bid, {"target": "all;DROP DATABASE x;--"}))
        self.assertIn("PG 库名不合法", str(cm.exception))
        self.assertEqual(dbm.query_one("SELECT target FROM backups WHERE id=?", (bid,))["target"], "appdb")

    def test_update_rejects_bad_kind_as_business_error(self):
        """必须是 RuntimeError，不是未捕获的 sqlite3.IntegrityError。"""
        bid = self._mk()
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.update_backup(bid, {"kind": "sqlite"}))
        self.assertIn("kind 必须是 pg 或 app", str(cm.exception))
        self.assertEqual(dbm.query_one("SELECT kind FROM backups WHERE id=?", (bid,))["kind"], "pg")

    def test_update_kind_switch_uses_merged_pair(self):
        """只改 kind 也要按 (新 kind, 旧 target) 这一对来校验。

        target=app_db 对 pg 合法（IDENT_RE 允许下划线），对 app 不合法（is_name 禁止）。
        """
        bid = self._mk(target="app_db")
        self.assertTrue(B.is_ident("app_db"))
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.update_backup(bid, {"kind": "app"}))
        self.assertIn("应用名不合法", str(cm.exception))

    # ---- 合法路径必须放行，否则"永远拒绝"也能通过以上所有测试 ----

    def test_create_accepts_legal_values(self):
        self.addCleanup(_drop, asyncio.run(B.create_backup({"kind": "pg", "target": "app_db"}))["id"])
        self.addCleanup(_drop, asyncio.run(B.create_backup({"kind": "pg", "target": "all"}))["id"])
        self.addCleanup(_drop, asyncio.run(B.create_backup({"kind": "app", "target": "my-app"}))["id"])

    def test_update_accepts_legal_target_and_pair(self):
        bid = self._mk(target="app_db")
        out = asyncio.run(B.update_backup(bid, {"target": "other_db", "kind": "pg", "keep": 3}))
        self.assertEqual((out["target"], out["kind"], out["keep"]), ("other_db", "pg", 3))
        out2 = asyncio.run(B.update_backup(bid, {"kind": "app", "target": "my-app"}))
        self.assertEqual((out2["kind"], out2["target"]), ("app", "my-app"))

    def test_update_without_kind_or_target_keeps_stored_pair(self):
        """只改 schedule 不应因为"合并出的 pair 缺字段"被误杀。"""
        bid = self._mk(kind="app", target="my-app")
        out = asyncio.run(B.update_backup(bid, {"schedule": "weekly", "hour": 5}))
        self.assertEqual((out["kind"], out["target"], out["schedule"], out["hour"]), ("app", "my-app", "weekly", 5))


class TestEnabledSemantics(unittest.TestCase):
    """POST 与 PATCH 对同一个 enabled 值要有同一个结果。"""

    def setUp(self):
        self._p = mock.patch.object(B, "_sync_timer", _no_timer)
        self._p.start()
        self.addCleanup(self._p.stop)

    def _both(self, value):
        create_row = asyncio.run(B.create_backup({"kind": "pg", "target": "appdb", "enabled": value}))
        self.addCleanup(_drop, create_row["id"])
        patch_row = asyncio.run(B.update_backup(create_row["id"], {"enabled": value}))
        return create_row["enabled"], patch_row["enabled"]

    def test_falsy_values_disable_on_both_paths(self):
        for value in (False, 0, ""):
            with self.subTest(value=value):
                created, updated = self._both(value)
                self.assertEqual((created, updated), (0, 0))

    def test_truthy_and_missing_enable_on_both_paths(self):
        for value in (True, 1, "1"):
            with self.subTest(value=value):
                created, updated = self._both(value)
                self.assertEqual((created, updated), (1, 1))
        row = asyncio.run(B.create_backup({"kind": "pg", "target": "appdb"}))
        self.addCleanup(_drop, row["id"])
        self.assertEqual(row["enabled"], 1)


class TestUnitDir(unittest.TestCase):
    """unit 目录跟 CP_UNIT_DIR 走，不再写死 /etc/systemd/system。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_bk_unit_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls = []
        p1 = mock.patch.object(config, "UNIT_DIR", self.dir)
        p2 = mock.patch.object(B, "run", _fake_run(self.calls))
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        p1.start()
        p2.start()

    def test_create_and_delete_use_configured_dir(self):
        row = asyncio.run(B.create_backup({"kind": "pg", "target": "appdb", "schedule": "daily", "enabled": 1}))
        self.addCleanup(_drop, row["id"])
        u = f"panel-backup-{row['id']}"
        svc = f"{self.dir}/{u}.service"
        tmr = f"{self.dir}/{u}.timer"
        self.assertTrue(os.path.exists(svc), svc)
        self.assertTrue(os.path.exists(tmr), tmr)
        self.assertFalse(os.path.exists(f"/etc/systemd/system/{u}.service"), "真 unit 目录被写脏了")
        # 禁用的任务：unit 文件应当被清掉
        asyncio.run(B.update_backup(row["id"], {"schedule": "manual"}))
        self.assertFalse(os.path.exists(svc))
        self.assertFalse(os.path.exists(tmr))
        asyncio.run(B.delete_backup(row["id"]))
        self.assertIsNone(dbm.query_one("SELECT id FROM backups WHERE id=?", (row["id"],)))

    def test_delete_removes_units_in_configured_dir(self):
        row = asyncio.run(B.create_backup({"kind": "pg", "target": "appdb", "schedule": "daily", "enabled": 1}))
        self.addCleanup(_drop, row["id"])
        u = f"panel-backup-{row['id']}"
        self.assertTrue(os.path.exists(f"{self.dir}/{u}.timer"))
        asyncio.run(B.delete_backup(row["id"]))
        self.assertFalse(os.path.exists(f"{self.dir}/{u}.service"))
        self.assertFalse(os.path.exists(f"{self.dir}/{u}.timer"))


class TestDeleteMissingTask(unittest.TestCase):
    def test_unknown_id_is_business_error(self):
        calls = []
        with (
            mock.patch.object(B, "run", _fake_run(calls)),
            mock.patch.object(B, "_sync_timer", _no_timer),
            self.assertRaises(RuntimeError) as cm,
        ):
            asyncio.run(B.delete_backup(999999))
        self.assertIn("备份任务不存在", str(cm.exception))
        # 报错要在 systemctl 之前：不存在就别去动系统
        self.assertEqual(calls, [])


class TestBackupFilename(unittest.TestCase):
    """秒级时间戳 + 撞名补序号：一份备份不许吃掉另一份。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_bk_files_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(R, "BACKUP_DIR", self.dir)
        self.addCleanup(p.stop)
        p.start()
        self.app = tempfile.mkdtemp(prefix="cp_bk_app_")
        self.addCleanup(shutil.rmtree, self.app, True)
        with open(f"{self.app}/a.txt", "w") as f:
            f.write("V1")
        name = _uid("bkapp")
        self.aid = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,'node','','main',?,'node x')",
            (name, self.app),
        )
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (self.aid,))
        self.bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES('app',?,'manual',3,30,7,1)",
            (name,),
        )
        self.addCleanup(_drop, self.bid)

    def test_stamp_has_seconds(self):
        stamp = R._stamp()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
        self.assertNotIn(":", stamp)

    def test_free_path_appends_sequence(self):
        first = R._free_path(7, "2026-01-01-00-00-00", ".tar.gz", self.dir)
        self.assertEqual(first, f"{self.dir}/bk7-2026-01-01-00-00-00.tar.gz")
        with open(first, "w") as f:
            f.write("x")
        second = R._free_path(7, "2026-01-01-00-00-00", ".tar.gz", self.dir)
        self.assertEqual(second, f"{self.dir}/bk7-2026-01-01-00-00-00-2.tar.gz")
        with open(second, "w") as f:
            f.write("x")
        third = R._free_path(7, "2026-01-01-00-00-00", ".dump.gz", self.dir)
        self.assertEqual(third, f"{self.dir}/bk7-2026-01-01-00-00-00.dump.gz")
        # 不同后缀互不占用名字
        with open(third, "w") as f:
            f.write("x")
        self.assertEqual(R._free_path(7, "2026-01-01-00-00-00", ".dump.gz", self.dir),
                         f"{self.dir}/bk7-2026-01-01-00-00-00-2.dump.gz")

    def test_two_runs_same_second_keep_both_files(self):
        """把时间戳钉死，模拟同一秒内跑两次（连点两次"立即备份"）。"""
        bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,))
        with mock.patch.object(R, "_stamp", lambda: "2026-01-01-00-00-00"):
            R.main(bk)
            with open(f"{self.app}/a.txt", "w") as f:
                f.write("V2")
            R.main(bk)
        files = sorted(os.listdir(self.dir))
        self.assertEqual(len(files), 2, files)
        base = f"bk{self.bid}-2026-01-01-00-00-00.tar.gz"
        variant = f"bk{self.bid}-2026-01-01-00-00-00-2.tar.gz"
        self.assertEqual(set(files), {base, variant})
        # 前一份必须还是前一份：内容可解、且仍是 V1（按名字取，别按排序位置——
        # `-2` 里的 `-`(0x2D) 比 `.`(0x2E) 小，字典序里补序号的那份排在前面）
        with gzip.open(f"{self.dir}/{base}", "rb") as g:
            payload = g.read()
        self.assertIn(b"V1", payload)
        self.assertNotIn(b"V2", payload)
        with gzip.open(f"{self.dir}/{variant}", "rb") as g:
            payload2 = g.read()
        self.assertIn(b"V2", payload2)

    def test_every_producer_uses_free_path(self):
        """三类产物（pg all / pg 单库 / app）都得走 _free_path，漏一个就还是会被覆盖。

        守卫矩阵用源码断言，和 test_apps_service 里部署守卫同一套写法。
        """
        src = inspect.getsource(R.main)
        self.assertEqual(src.count("_free_path("), 3, src)
        self.assertNotIn('f"{BACKUP_DIR}/bk', src, "还有分支在手工拼备份文件名")


class TestHttpContract(unittest.TestCase):
    """坏值对外是 4xx + error，不许 500、也不许静默成功。"""

    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from app import main

        cls.client = TestClient(main.app)
        cls.user = _uid("bk_ep_")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (cls.user, security.hash_pass("Password123"), "admin"),
        )
        cls.headers = {"Authorization": f"Bearer {security.sign_token(cls.user, 'admin')}"}

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username=?", (cls.user,))

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_bk_ep_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p1 = mock.patch.object(config, "UNIT_DIR", self.dir)
        self.calls = []
        p2 = mock.patch.object(B, "run", _fake_run(self.calls))
        p3 = mock.patch.object(B, "BACKUP_DIR", self.dir)
        for p in (p1, p2, p3):
            self.addCleanup(p.stop)
            p.start()

    def test_bad_target_on_patch_is_400(self):
        r = self.client.post("/api/backups", json={"kind": "pg", "target": "appdb"}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        bid = r.json()["id"]
        self.addCleanup(_drop, bid)
        r2 = self.client.patch(f"/api/backups/{bid}", json={"target": "all;DROP DATABASE x;--"}, headers=self.headers)
        self.assertEqual(r2.status_code, 400, r2.text)
        self.assertIn("error", r2.json())

    def test_bad_kind_on_create_is_400(self):
        r = self.client.post("/api/backups", json={"kind": "sqlite", "target": "appdb"}, headers=self.headers)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("kind 必须是 pg 或 app", r.json()["error"])

    def test_enabled_zero_roundtrip(self):
        r = self.client.post("/api/backups", json={"kind": "pg", "target": "appdb", "enabled": 0}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.addCleanup(_drop, r.json()["id"])
        self.assertEqual(r.json()["enabled"], 0)

    def test_delete_unknown_id_is_400_not_ok(self):
        r = self.client.delete("/api/backups/999999", headers=self.headers)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("备份任务不存在", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
