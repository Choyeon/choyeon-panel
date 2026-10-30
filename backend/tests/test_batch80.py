"""批次80：操作审计的过滤与翻页挪到服务端。

改之前页面只在已加载的那一屏（≤ LIST_CAP 行）里 filter：更早的记录永远搜不到，
而且会被如实显示成「没有匹配的记录」——把「这一屏里没有」说成「没有」。
这里盯的是替换后的几条边界：

- q 在服务端过滤，四个字段（登录名/昵称/操作/详情）都算命中；
- LIKE 里的 % _ \\ 当字面量：搜「100%」不能变成「以 100 开头」；
- matched 是整张表在**这个过滤条件**下的真实条数，不是本页行数；
- before 是上一页最旧记录的 id（严格小于），往回翻不重不漏，bad cursor 明确 400；
- 空/纯空白 q 等于不过滤；q 超长按 AUDIT_Q_MAX 截断（响应里带回实际用的词）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_80_"))

from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.routers.extras import AUDIT_Q_MAX  # noqa: E402

PREFIX = "b80:"
USER = "b80-filter-user"
NICK = "批次80昵称"
PWD_USER = "b80-admin"
PWD = "Password123"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role,nickname) VALUES(?,?,?,?)",
                    (USER, security.hash_pass(PWD), "viewer", NICK))
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (PWD_USER, security.hash_pass(PWD), "admin"))

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM audit WHERE action LIKE ?", (PREFIX + "%",))
        dbm.execute("DELETE FROM users WHERE username IN (?,?)", (USER, PWD_USER))

    def setUp(self):
        dbm.execute("DELETE FROM audit WHERE action LIKE ?", (PREFIX + "%",))

    def _get(self, path: str, name: str = PWD_USER, status: int = 200):
        token = security.sign_token(name, "admin" if name == PWD_USER else "viewer")
        r = self.client.get(path, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def _seed(self, rows):
        for action, detail in rows:
            dbm.execute("INSERT INTO audit(username,action,detail) VALUES(?,?,?)",
                        (USER, action, detail))


class TestServerSideFilter(_Base):
    def test_matches_username_nickname_action_and_detail(self):
        self._seed([
            (PREFIX + "app:deploy", "应用 1"),
            (PREFIX + "backup:run", "nightly"),
        ])
        # 昵称单独命中：页面以前也匹配昵称，挪到服务端后这条不能丢
        body = self._get(f"/api/audit?q={NICK}")
        self.assertGreaterEqual(body["matched"], 2)
        self.assertTrue(all(r["username"] == USER for r in body["rows"]))
        # 详情命中
        body = self._get(f"/api/audit?q={PREFIX}backup")
        self.assertEqual(body["matched"], 1)
        self.assertEqual(body["rows"][0]["action"], PREFIX + "backup:run")

    def test_like_wildcards_are_literal(self):
        self._seed([
            (PREFIX + "a:100%done", "x"),
            (PREFIX + "b:100Xdone", "x"),
            (PREFIX + "c:foo_bar", "x"),
            (PREFIX + "d:fooxbar", "x"),
        ])
        body = self._get("/api/audit?q=100%25done")  # %25 == '%'
        self.assertEqual(body["matched"], 1, "% 被当成通配符就会多算 100Xdone")
        self.assertEqual(body["rows"][0]["action"], PREFIX + "a:100%done")
        body = self._get("/api/audit?q=foo_bar")
        self.assertEqual(body["matched"], 1, "_ 被当成单字符通配就会多算 fooxbar")

    def test_blank_query_is_no_filter(self):
        self._seed([(PREFIX + "one", "d")])
        body = self._get("/api/audit?q=%20%20&limit=1")
        self.assertEqual(body["q"], "", "纯空白不该变成一个会过滤掉一切的词")
        self.assertGreaterEqual(body["matched"], 1)

    def test_long_query_is_capped_and_echoed(self):
        body = self._get("/api/audit?q=" + "z" * (AUDIT_Q_MAX + 50))
        self.assertEqual(len(body["q"]), AUDIT_Q_MAX)


class TestMatchedAndCursor(_Base):
    def test_matched_counts_the_whole_table_not_the_page(self):
        self._seed([(f"{PREFIX}n{i}", "same") for i in range(5)])
        body = self._get(f"/api/audit?q={PREFIX}n&limit=2")
        self.assertEqual(body["matched"], 5)
        self.assertEqual(len(body["rows"]), 2)
        self.assertTrue(body["hasMore"])
        body = self._get(f"/api/audit?q={PREFIX}n&limit=5")
        self.assertEqual(len(body["rows"]), 5)
        self.assertFalse(body["hasMore"])

    def test_before_walks_back_without_gaps_or_repeats(self):
        self._seed([(f"{PREFIX}page{i}", "d") for i in range(5)])
        seen, before, guard = [], None, 0
        while guard < 10:
            guard += 1
            url = f"/api/audit?q={PREFIX}page&limit=2" + (f"&before={before}" if before else "")
            body = self._get(url)
            seen += [r["id"] for r in body["rows"]]
            if not body["hasMore"]:
                break
            before = body["rows"][-1]["id"]
        self.assertEqual(len(seen), 5, "翻到底应正好覆盖 5 条")
        self.assertEqual(len(set(seen)), 5, "游标翻页不许重复")
        self.assertEqual(seen, sorted(seen, reverse=True), "仍按 id 倒序")

    def test_bad_cursor_is_a_400_not_an_empty_page(self):
        for raw in ("0", "-3", "abc"):
            body = self._get(f"/api/audit?before={raw}", status=400)
            self.assertEqual(body["code"], "bad_cursor")

    def test_viewer_cannot_read_audit(self):
        self._get("/api/audit?q=x", name=USER, status=403)


if __name__ == "__main__":
    unittest.main()
