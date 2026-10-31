"""批次97：删号时头像清理把「读不动/删不掉」折成「已删除」。

`avatars.purge` 用 `os.path.isdir` 探目录——读不动（缺权限、父路径不是目录）和目录
确实不存在都被吞成同一个 False，直接 `return 0`；单个文件 `os.unlink` 失败又被
`except OSError: pass` 咽掉，计数照常往上加。返回一个 int「删了几个」，两处调用方
（网页 `users_delete`、CLI `user delete`）连返回值都不看，照报 `ok:True` / 「已删除」。

于是那张被删用户的 `<id>.png` 还挂在免登录的 `/api/avatars/<id>.png` 公开路由上，
面板里再没有任何地方能看见它、清它——而 purge 存在的唯一理由就是别留下这张脸
（批次⑤C 写下的判据）。读不到不是"没有"，删不掉不是"删完了"：截断可以，冒充总量不行；
同理，"没核对成"可以，冒充"已清理"不行。

这里让 purge 走 `util.dir_state` 交出三态，返回 `{purged, kept, unread}`，并把"哪里没
删干净"的措辞收在 `residue_note` 一处，删号的路由与 CLI 都引用它，网页据此提示残留。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch97_data_"))

from app import avatars, config, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import extras  # noqa: E402

_seq = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid() % 100000}{next(_seq)}"


def _req(body=None, role="admin", sub="tester"):
    async def _json():
        return body

    return SimpleNamespace(
        state=SimpleNamespace(cp_role=role, cp_sub=sub),
        json=_json,
        headers={},
        client=SimpleNamespace(host="unittest"),
    )


async def _async_noop(*a, **k):
    return None


class PurgeHonestyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b97_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(config, "AVATAR_DIR", self.dir)
        self.addCleanup(p.stop)
        p.start()

    def test_unreadable_dir_is_not_reported_as_clean(self):
        # 读不动目录 != 目录里没有头像：必须带出 unread，不能被调用方当成"已清理"。
        with mock.patch.object(avatars.util, "dir_state", return_value=(None, [], "PermissionError")):
            res = avatars.purge(3)
        self.assertEqual(res, {"purged": 0, "kept": [], "unread": "PermissionError"})
        self.assertIn("读不动", avatars.residue_note(res))

    def test_absent_dir_is_genuinely_nothing_to_clean(self):
        # 目录确实不存在（False 那一档）才是"没东西可删"，不该被误标成读不动。
        with mock.patch.object(avatars.util, "dir_state", return_value=(False, [], "")):
            res = avatars.purge(3)
        self.assertEqual(res, {"purged": 0, "kept": [], "unread": None})
        self.assertEqual(avatars.residue_note(res), "")

    def test_file_that_will_not_unlink_is_listed_as_kept(self):
        for name in ("9.png", "9.jpg"):
            Path(self.dir, name).write_bytes(b"x")
        real = os.unlink

        def fake(path, *a, **k):
            if str(path).endswith(".jpg"):
                raise OSError("read-only file system")
            real(path)

        with mock.patch.object(avatars.os, "unlink", side_effect=fake):
            res = avatars.purge(9)
        self.assertEqual(res["purged"], 1)
        self.assertEqual(res["kept"], ["9.jpg"], "删不掉的那张脸要能被点名")
        self.assertIsNone(res["unread"])
        note = avatars.residue_note(res)
        self.assertIn("9.jpg", note)
        self.assertIn("/api/avatars/", note)


class RouteContractTests(unittest.TestCase):
    def test_route_surfaces_residue_instead_of_a_clean_delete(self):
        victim = _uid("b97v")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (victim, security.hash_pass("Password123"), "viewer"),
        )
        vid = dbm.query_one("SELECT id FROM users WHERE username=?", (victim,))["id"]
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE id=?", (vid,))
        residue = {"purged": 0, "kept": [f"{vid}.png"], "unread": None}
        with (mock.patch.object(extras.avatars, "purge", return_value=residue),
              mock.patch.object(extras.aio_db, "audit", _async_noop)):
            r = asyncio.run(extras.users_delete(str(vid), _req({}, role="admin", sub="theadmin")))
        self.assertTrue(r["ok"])
        self.assertIn(f"{vid}.png", r["warn"])
        self.assertIn("/api/avatars/", r["warn"])

    def test_route_still_reports_a_plain_ok_when_clean(self):
        victim = _uid("b97c")
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (victim, security.hash_pass("Password123"), "viewer"),
        )
        vid = dbm.query_one("SELECT id FROM users WHERE username=?", (victim,))["id"]
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE id=?", (vid,))
        clean = {"purged": 1, "kept": [], "unread": None}
        with (mock.patch.object(extras.avatars, "purge", return_value=clean),
              mock.patch.object(extras.aio_db, "audit", _async_noop)):
            r = asyncio.run(extras.users_delete(str(vid), _req({}, role="admin", sub="theadmin")))
        self.assertEqual(r, {"ok": True}, "干净删除不应多出 warn 键")


class SourceContractTests(unittest.TestCase):
    def test_cli_weaves_residue_into_the_human_note(self):
        src = (BACKEND / "cli.py").read_text(encoding="utf-8")
        self.assertIn("avatars.residue_note", src)
        self.assertIn("avatar_warn", src)
        self.assertIn('"avatar": res', src, "JSON 里要交出结构化清理结果")

    def test_users_table_surfaces_the_warn(self):
        src = (BACKEND.parent / "web" / "src" / "views" / "Users.vue").read_text(encoding="utf-8")
        self.assertIn("res?.warn", src)
        self.assertIn("msg.warning", src)

    def test_residue_wording_lives_only_in_avatars(self):
        # 同一句"哪里没删干净"只有一个出处：调用方引用 residue_note，不各抄一遍产出串。
        av = (BACKEND / "app" / "avatars.py").read_text(encoding="utf-8")
        self.assertEqual(av.count("def residue_note"), 1)
        self.assertIn("免登录访问", av)
        for rel in ("app/routers/extras.py", "cli.py"):
            src = (BACKEND / rel).read_text(encoding="utf-8")
            self.assertIn("residue_note", src, f"{rel} 应引用共用的措辞而非自己复述")
            self.assertNotIn("免登录访问", src, f"{rel} 不该自己复述头像措辞")


if __name__ == "__main__":
    unittest.main()
