"""批次82：告警历史的「加载更早的记录」。

改之前这一页只能看到最新的一档，更早的记录没有入口；页面写了「还有更早的记录未加载」
却什么也做不了。这里钉住补上来的契约：

- before 是严格小于：分页之间不重叠、不遗漏；
- 游标只收窄这一页，matched 仍是整表（过滤条件下）的真实条数，翻页时不缩水；
- 游标写坏（0 / 负数 / 非数字）是 400，不静默当成「从头开始」；
- 游标与 q / level 叠加；只读账号也能翻。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_82_"))

from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402

PREFIX = "b82:"
USER = "b82-viewer"
PWD_USER = "b82-admin"
PWD = "Password123"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (USER, security.hash_pass(PWD), "viewer"))
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (PWD_USER, security.hash_pass(PWD), "admin"))

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM alerts_log WHERE code LIKE ?", (PREFIX + "%",))
        dbm.execute("DELETE FROM users WHERE username IN (?,?)", (USER, PWD_USER))

    def setUp(self):
        dbm.execute("DELETE FROM alerts_log WHERE code LIKE ?", (PREFIX + "%",))

    def _get(self, path: str, name: str = PWD_USER, role: str = "admin", status: int = 200):
        token = security.sign_token(name, role)
        r = self.client.get(path, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    @staticmethod
    def _insert(level: str, code: str, message: str) -> int:
        return dbm.execute(
            "INSERT INTO alerts_log(ts,level,code,message,meta) VALUES(datetime('now'),?,?,?,?)",
            (level, code, message, "{}"),
        )


class TestCursorWalk(_Base):
    def test_pages_do_not_overlap_or_skip(self):
        ids = [self._insert("crit", PREFIX + "walk", f"m{i}") for i in range(7)]
        # 别的记录也在表里：游标必须接着「这一条件下」的最旧一条走
        self._insert("ok", PREFIX + "noise", "noise")

        first = self._get(f"/api/alerts/history?q={PREFIX}walk&limit=3")
        self.assertEqual([r["id"] for r in first["rows"]], ids[::-1][:3])
        self.assertEqual(first["matched"], 7)
        self.assertTrue(first["hasMore"])

        second = self._get(f"/api/alerts/history?q={PREFIX}walk&limit=3&before={first['rows'][-1]['id']}")
        self.assertEqual([r["id"] for r in second["rows"]], ids[::-1][3:6])
        self.assertEqual(second["matched"], 7, "游标只收窄这一页，匹配数不能缩水")
        self.assertTrue(second["hasMore"])

        third = self._get(f"/api/alerts/history?q={PREFIX}walk&limit=3&before={second['rows'][-1]['id']}")
        self.assertEqual([r["id"] for r in third["rows"]], ids[::-1][6:])
        self.assertEqual(third["matched"], 7)
        self.assertFalse(third["hasMore"])

        seen = [r["id"] for r in first["rows"] + second["rows"] + third["rows"]]
        self.assertEqual(seen, ids[::-1], "三页拼起来应正好是完整时间线，不重不漏")

    def test_bad_cursor_is_400(self):
        self._insert("crit", PREFIX + "bad", "x")
        for raw in ("0", "-3", "abc", "1.5"):
            r = self.client.get(f"/api/alerts/history?before={raw}",
                                headers={"Authorization": f"Bearer {security.sign_token(PWD_USER, 'admin')}"})
            self.assertEqual(r.status_code, 400, f"before={raw} 应被拒绝，实际 {r.status_code}")
            self.assertEqual(r.json().get("code"), "bad_cursor", r.text)

    def test_cursor_combines_with_level_and_q(self):
        crit = [self._insert("crit", PREFIX + "mix", f"c{i}") for i in range(3)]
        self._insert("warn", PREFIX + "mix", "w0")

        body = self._get(f"/api/alerts/history?q={PREFIX}mix&level=crit&limit=2")
        self.assertEqual(len(body["rows"]), 2)
        self.assertEqual(body["matched"], 3, "匹配数按 q+level 的整表条数算")
        body = self._get(
            f"/api/alerts/history?q={PREFIX}mix&level=crit&limit=2&before={body['rows'][-1]['id']}"
        )
        self.assertEqual([r["id"] for r in body["rows"]], crit[:1])
        self.assertEqual(body["matched"], 3)
        self.assertFalse(body["hasMore"])

    def test_viewer_can_page(self):
        ids = [self._insert("warn", PREFIX + "ro", f"m{i}") for i in range(3)]
        first = self._get(f"/api/alerts/history?q={PREFIX}ro&limit=2", USER, "viewer")
        second = self._get(
            f"/api/alerts/history?q={PREFIX}ro&limit=2&before={first['rows'][-1]['id']}",
            USER, "viewer",
        )
        self.assertEqual([r["id"] for r in second["rows"]], ids[:1])
        self.assertEqual(second["matched"], 3)


if __name__ == "__main__":
    unittest.main()
