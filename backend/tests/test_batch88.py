"""批次88：部署记录把「最近 20 条」当成全部历史。

`GET /api/apps/{id}/deployments` 原来直接返回裸数组、SQL 里写死 `LIMIT 20`：
每次部署 / 同步 / 回滚都会写一行，第 21 条之前的历史既翻不到、也没有一个字
说它存在。页面把 `deps.length` 当成了「一共部署过几次」。

这里钉住补上来的契约：分页数组 + 真实总数 + before 游标（严格小于，页间不重叠），
以及前端顺着游标往回翻的入口。批次80/82 在审计与告警历史上做的是同一件事。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_88_"))

from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402

APP = 880001
OTHER = 880002
VIEWER = "b88-viewer"
PWD = "Password123"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (VIEWER, security.hash_pass(PWD), "viewer"))

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM deployments WHERE app_id IN (?,?)", (APP, OTHER))
        dbm.execute("DELETE FROM users WHERE username=?", (VIEWER,))

    def setUp(self):
        dbm.execute("DELETE FROM deployments WHERE app_id IN (?,?)", (APP, OTHER))

    def _insert(self, app_id: int, i: int, status: str = "success") -> int:
        return dbm.execute(
            "INSERT INTO deployments(app_id,status,commit_sha,log) VALUES(?,?,?,?)",
            (app_id, status, f"sha{i:03d}", f"日志 #{i}"),
        )

    def _get(self, query: str, name: str = VIEWER, role: str = "viewer", status: int = 200):
        token = security.sign_token(name, role)
        r = self.client.get(f"/api/apps/{APP}/deployments{query}",
                            headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, status, r.text)
        return r.json()


class PagingTests(_Base):
    def test_first_page_declares_the_true_total(self):
        ids = [self._insert(APP, i) for i in range(25)]
        page = self._get("")
        self.assertEqual(page["total"], 25, "总数必须是整张表，不是这一页的条数")
        self.assertEqual(len(page["rows"]), 20)
        self.assertTrue(page["has_more"])
        self.assertEqual([r["id"] for r in page["rows"]], ids[::-1][:20], "新到旧")

    def test_before_cursor_returns_the_remainder_without_overlap(self):
        ids = [self._insert(APP, i) for i in range(25)]
        first = self._get("?limit=10")
        second = self._get(f"?limit=10&before={first['rows'][-1]['id']}")
        third = self._get(f"?limit=10&before={second['rows'][-1]['id']}")
        seen = [r["id"] for r in first["rows"] + second["rows"] + third["rows"]]
        self.assertEqual(seen, ids[::-1], "三页拼起来必须正好是全集，不重不漏")
        self.assertEqual([first["total"], second["total"], third["total"]], [25, 25, 25])
        self.assertEqual([first["has_more"], second["has_more"], third["has_more"]],
                         [True, True, False])

    def test_limit_uses_the_shared_parser(self):
        for i in range(25):
            self._insert(APP, i)
        self.assertEqual(len(self._get("?limit=2")["rows"]), 2)
        # 0 回落到这一页的默认条数（与审计/告警历史的 parse_limit 同口径）；
        # 负数在 SQLite 里表示「不限制」，必须夹成 1，不能把整张表吐出来。
        self.assertEqual(self._get("?limit=0")["limit"], 20)
        self.assertEqual(len(self._get("?limit=-5")["rows"]), 1)
        self.assertEqual(self._get("?limit=1000")["limit"], 200, "上限 200")

    def test_unknown_app_is_empty_not_an_error(self):
        page = self._get("?before=999999", status=200)
        self.assertEqual((page["rows"], page["total"], page["has_more"]), ([], 0, False))

    def test_other_apps_rows_are_not_counted(self):
        self._insert(APP, 1)
        self._insert(OTHER, 2)
        page = self._get("")
        self.assertEqual(page["total"], 1)
        self.assertEqual([r["app_id"] for r in page["rows"]], [APP])

    def test_rows_stay_lite(self):
        self._insert(APP, 1)
        row = self._get("")["rows"][0]
        self.assertNotIn("log", row, "列表只带日志长度，全文走单条详情")
        self.assertIn("log_len", row)


class FrontendContractTests(unittest.TestCase):
    @staticmethod
    def _src(*parts: str) -> str:
        root = Path(__file__).resolve().parents[2] / "web" / "src"
        return root.joinpath(*parts).read_text(encoding="utf-8")

    def test_api_takes_a_cursor(self):
        src = self._src("api.ts")
        self.assertIn("deployments: (id: number, before?: number)", src)
        self.assertIn("before=${before}", src)

    def test_detail_page_owns_the_total_and_can_page_back(self):
        src = self._src("views", "AppDetail.vue")
        for token in ("depsTotal", "depsHasMore", "loadMoreDeps", "加载更早", "已列"):
            self.assertIn(token, src, f"部署记录页缺「{token}」")
        self.assertIn("page.rows", src, "列表必须读分页数组，不能再把响应当数组")
        self.assertNotIn("deps.value = list", src, "旧的裸数组写法不能再回来")


if __name__ == "__main__":
    unittest.main()
