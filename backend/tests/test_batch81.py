"""批次81：告警历史的关键字过滤挪到服务端。

改之前 Alerts.vue 只在已加载的那一档（≤500 行）里 filter：三天前那个 code 的命中
根本不在这一屏，页面却显示「没有符合筛选条件的告警记录」——把「这一屏没有」说成「没有」。
这里钉住替换后的契约：

- q 在服务端过滤 code 与 message 两列；
- LIKE 里的 % _ \\ 当字面量；
- matched 是整张表在这个过滤条件下的真实条数，不是本页行数；
- hasMore 由多取一行判断，窗口边界能被页面标注出来；
- level 过滤与 q 叠加；空 q 等于不过滤；超长 q 按 SEARCH_Q_MAX 截断。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_81_"))

from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.util import SEARCH_Q_MAX  # noqa: E402

PREFIX = "b81:"
USER = "b81-viewer"
PWD_USER = "b81-admin"
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


class TestServerSideFilter(_Base):
    def test_matches_code_and_message(self):
        self._insert("crit", PREFIX + "disk", "disk 87% used")
        self._insert("warn", PREFIX + "cpu", "load high")
        body = self._get(f"/api/alerts/history?q={PREFIX}disk")
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["rows"][0]["code"], PREFIX + "disk")
        # message 命中：页面搜索框的文案是「过滤来源/内容」，两列都要算
        body = self._get("/api/alerts/history?q=load+high")
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["rows"][0]["code"], PREFIX + "cpu")

    def test_like_wildcards_are_literal(self):
        self._insert("crit", PREFIX + "pct", "disk 100%done")
        self._insert("crit", PREFIX + "pct2", "disk 100Xdone")
        body = self._get("/api/alerts/history?q=100%25done")
        self.assertEqual([r["code"] for r in body["rows"]], [PREFIX + "pct"])
        self._insert("crit", PREFIX + "us", "foo_bar")
        self._insert("crit", PREFIX + "us2", "fooxbar")
        body = self._get("/api/alerts/history?q=foo_bar")
        self.assertEqual([r["code"] for r in body["rows"]], [PREFIX + "us"])

    def test_blank_q_does_not_filter_and_long_q_is_capped(self):
        self._insert("crit", PREFIX + "one", "a")
        self._insert("ok", PREFIX + "two", "b")
        body = self._get("/api/alerts/history?q=+")
        self.assertEqual(body["q"], "")
        self.assertGreaterEqual(body["matched"], 2)
        body = self._get("/api/alerts/history?q=" + "z" * (SEARCH_Q_MAX + 50))
        self.assertEqual(len(body["q"]), SEARCH_Q_MAX)
        self.assertEqual(body["matched"], 0)

    def test_level_and_q_combine(self):
        self._insert("crit", PREFIX + "disk", "disk full")
        self._insert("warn", PREFIX + "disk", "disk warm")
        body = self._get(f"/api/alerts/history?q={PREFIX}disk&level=crit")
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["rows"][0]["level"], "crit")

    def test_viewer_can_read_history(self):
        self._insert("crit", PREFIX + "ro", "readable")
        body = self._get(f"/api/alerts/history?q={PREFIX}ro", USER, "viewer")
        self.assertEqual(body["matched"], 1)
        # 未登录仍然 401：这条读权限只给登录账号
        self.assertEqual(self.client.get("/api/alerts/history").status_code, 401)


class TestMatchedAndWindow(_Base):
    def test_matched_is_table_wide_and_window_is_flagged(self):
        for i in range(5):
            self._insert("crit", PREFIX + "spam", f"hit {i}")
        self._insert("ok", PREFIX + "other", "noise")
        body = self._get(f"/api/alerts/history?q={PREFIX}spam&limit=2")
        self.assertEqual(len(body["rows"]), 2)
        self.assertEqual(body["matched"], 5, "匹配数是整表条数，不是本页行数")
        self.assertTrue(body["hasMore"])
        # 刚好装满一档时没有更多：多取的那一行才是判据
        body = self._get(f"/api/alerts/history?q={PREFIX}spam&limit=5")
        self.assertEqual(len(body["rows"]), 5)
        self.assertFalse(body["hasMore"])

    def test_rows_are_newest_first_under_filter(self):
        ids = [self._insert("crit", PREFIX + "seq", f"m{i}") for i in range(3)]
        body = self._get(f"/api/alerts/history?q={PREFIX}seq")
        self.assertEqual([r["id"] for r in body["rows"]], ids[::-1])

    def test_bad_level_still_rejected(self):
        r = self.client.get("/api/alerts/history?level=bogus",
                            headers={"Authorization": f"Bearer {security.sign_token(PWD_USER, 'admin')}"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
