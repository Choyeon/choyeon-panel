"""批次⑤O：备份任务的 target 必须指着真实存在的应用，但不能因此锁死老任务。

线上探针（一次性本机 git 仓库 + 真部署 + 真备份/恢复）实测到的坏法：
`POST /api/backups {"kind":"app","target":"p5o-nope-6ca80f","schedule":"manual"}`
返回 **200**，任务建起来了、列表里"已启用"，跑一次才拿到
`{"code":1,"log":"应用 p5o-nope-6ca80f 不存在"}`；换成 daily 计划就是每晚
一次 SystemExit，而面板上看不出区别。`kind='app'` 过去只校验名字格式，
`kind='sqlite'` 却校验存在性——同一份 `_validate_target` 里的不对称。

修的时候有第二个坑，方向和上面相反：存在性校验若无条件套在 update 上，
⑤L 特意留出来的 `target_missing` 行（目标应用已被删除，任务还要能收尾）就会
连"改备份时间""停用"都做不了——`merged target` 永远是非空的历史值，
每次 PATCH 都被"应用不存在"挡回。备份页的编辑弹窗又是整行回传的
（`form.target` 一直带着原值），所以判据必须是**target 有没有被换掉**，
而不是请求体里有没有 target。
"""

import asyncio
import os
import tempfile
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5o_"))

import unittest  # noqa: E402

from app import database as dbm  # noqa: E402
from app.services import backup_service as B  # noqa: E402

_seq = [0]


def _name(prefix="p5oapp"):
    _seq[0] += 1
    return f"{prefix}-{os.getpid()}-{_seq[0]}"


def _mk_app(**kw):
    """插一行应用（不建目录：这里只验存在性判断，不跑真实备份）。"""
    row = {
        "name": kw.get("name") or _name(),
        "type": "node",
        "repo_url": "",
        "branch": "main",
        "path": kw.get("path") or tempfile.mkdtemp(prefix="cp_5o_app_"),
        "start_cmd": "node x",
    }
    cols = list(row)
    aid = dbm.execute(
        f"INSERT INTO apps({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row.values())
    )
    return dbm.query_one("SELECT * FROM apps WHERE id=?", (aid,))


def _mk_backup(**kw):
    row = {
        "kind": "pg",
        "target": "appdb",
        "schedule": "daily",
        "hour": 3,
        "minute": 30,
        "keep": 7,
        "enabled": 1,
    }
    row.update(kw)
    cols = list(row)
    bid = dbm.execute(
        f"INSERT INTO backups({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row.values())
    )
    return bid


async def _no_timer(b):
    return None


class _BackupCase(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(B, "_sync_timer", _no_timer)
        p.start()
        self.addCleanup(p.stop)
        self.apps = []
        self.bids = []

    def app(self, **kw):
        a = _mk_app(**kw)
        self.apps.append(a["id"])
        return a

    def backup(self, **kw):
        bid = _mk_backup(**kw)
        self.bids.append(bid)
        return bid

    def tearDown(self):
        if self.bids:
            dbm.execute(f"DELETE FROM backups WHERE id IN ({','.join('?' * len(self.bids))})", tuple(self.bids))
        if self.apps:
            dbm.execute(f"DELETE FROM apps WHERE id IN ({','.join('?' * len(self.apps))})", tuple(self.apps))


class TestAppTargetMustExist(_BackupCase):
    """create：app / sqlite 都必须指向存在的应用，pg 只查格式。"""

    def test_create_app_target_missing_is_rejected(self):
        """探针实测这一步返回 200，任务建起来后每晚失败。"""
        ghost = _name("p5o-nope")
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.create_backup({"kind": "app", "target": ghost}))
        self.assertIn("不存在", str(cm.exception))
        self.assertEqual(dbm.query("SELECT id FROM backups WHERE target=?", (ghost,)), [])

    def test_create_app_target_existing_ok(self):
        a = self.app()
        row = asyncio.run(B.create_backup({"kind": "app", "target": a["name"], "schedule": "manual"}))
        self.bids.append(row["id"])
        self.assertEqual((row["kind"], row["target"]), ("app", a["name"]))

    def test_create_sqlite_target_still_checked(self):
        """sqlite 的存在性校验本来就有，改完不能把它弄丢。"""
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.create_backup({"kind": "sqlite", "target": "99999999"}))
        self.assertIn("应用不存在", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm2:
            asyncio.run(B.create_backup({"kind": "sqlite", "target": _name("p5o-nope")}))
        self.assertIn("不存在", str(cm2.exception))

    def test_create_sqlite_target_existing_ok(self):
        a = self.app()
        row = asyncio.run(B.create_backup({"kind": "sqlite", "target": str(a["id"]), "schedule": "manual"}))
        self.bids.append(row["id"])
        self.assertEqual(row["target"], str(a["id"]))

    def test_create_pg_does_not_require_existing_database(self):
        """pg 库名只查格式：面板自己能建库，"先配计划再建库"是合理路径。"""
        row = asyncio.run(B.create_backup({"kind": "pg", "target": "p5o_not_created_yet", "schedule": "manual"}))
        self.bids.append(row["id"])
        self.assertEqual(row["target"], "p5o_not_created_yet")


class TestStaleTargetStaysEditable(_BackupCase):
    """target 没被换掉的 PATCH 一律放行——⑤L 的 target_missing 行还要能收尾。"""

    def _stale(self):
        a = self.app()
        bid = self.backup(kind="app", target=a["name"])
        dbm.execute("DELETE FROM apps WHERE id=?", (a["id"],))
        self.apps.remove(a["id"])
        return bid, a["name"]

    def test_update_schedule_on_stale_row_ok(self):
        bid, _ = self._stale()
        out = asyncio.run(B.update_backup(bid, {"schedule": "weekly", "hour": 5}))
        self.assertEqual((out["schedule"], out["hour"]), ("weekly", 5))

    def test_update_disable_on_stale_row_ok(self):
        """停用是唯一能立刻止住每晚失败的操作，绝不能被校验挡住。"""
        bid, _ = self._stale()
        out = asyncio.run(B.update_backup(bid, {"enabled": False}))
        self.assertEqual(out["enabled"], 0)

    def test_edit_dialog_resends_unchanged_stale_target_ok(self):
        """备份页编辑弹窗是整行回传的：target 原样带回来也必须算"没改"。"""
        bid, target = self._stale()
        out = asyncio.run(
            B.update_backup(bid, {"kind": "app", "target": target, "schedule": "manual", "keep": 3})
        )
        self.assertEqual((out["target"], out["schedule"], out["keep"]), (target, "manual", 3))

    def test_sqlite_stale_row_editable_by_id(self):
        a = self.app()
        bid = self.backup(kind="sqlite", target=str(a["id"]))
        dbm.execute("DELETE FROM apps WHERE id=?", (a["id"],))
        self.apps.remove(a["id"])
        out = asyncio.run(B.update_backup(bid, {"keep": 2}))
        self.assertEqual(out["keep"], 2)

    def test_pointing_at_another_missing_target_is_rejected(self):
        """换目标就要指对东西；拒绝时原行原样保留。"""
        bid, target = self._stale()
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.update_backup(bid, {"target": _name("p5o-nope2")}))
        self.assertIn("不存在", str(cm.exception))
        self.assertEqual(dbm.query_one("SELECT target FROM backups WHERE id=?", (bid,))["target"], target)

    def test_repointing_at_existing_app_ok(self):
        bid, _ = self._stale()
        a = self.app()
        out = asyncio.run(B.update_backup(bid, {"target": a["name"]}))
        self.assertEqual(out["target"], a["name"])

    def test_format_check_still_applies_without_target_change(self):
        """存在性可以因为"没改目标"而跳过，格式校验不行——名字还得是合法名字。"""
        bid = self.backup(kind="app", target="bad;name")
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.update_backup(bid, {"keep": 2}))
        self.assertIn("应用名不合法", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
