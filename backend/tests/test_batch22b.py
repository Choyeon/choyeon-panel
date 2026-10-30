"""批次㉒B：一次读取的边界只有一个出处。

一、Redis 的 key 详情。同一个「这次最多读多少」在三条命令里各写一遍
（GETRANGE 的 `65535`、HSCAN/SSCAN 的 `COUNT 200`、LRANGE/ZRANGE 的 `199`），
两条实现路线（RESP 流水线与 redis-cli 回退）又各自把边界抄成一次比较
（`slen > 65536`、`n > 200`）——五个地方、两种写法，改一处其余四处不会跟着动。
页面那句说明是第六处，而且它不分成句：`RedisKeyPanel.vue` 对 hash/list/set/zset
也写着「仅读取前 64KB（截断）；截断状态下不开放编辑」，可那四类是按条目数截断的，
且「不开放编辑」只对 string 成立（集合本来就没有整值覆盖这条路，条目增删照常可用）。

现在边界只有 `REDIS_VALUE_BYTES` / `REDIS_RANGE_ITEMS` 两个常数：命令参数由它们派生
（闭区间上界是 `N-1`、`COUNT` 是 `N`），两条路线比较的是同一个数，详情响应再把
`read_limits` 带下去，页面按类型用它收到的数字成句。

二、列表的条数。默认值与上限各写两遍（`alerts.py` 的 100/500、`/api/audit` 的 100/500），
档位与默认再各写两遍（`Alerts.vue`、`Audit.vue` 的 `ref(200)` + `[100,200,500]`），
`api.ts` 里还有第三个 100。其中三条互相矛盾：UI 默认 200、接口默认 100，
同一个 `GET /api/audit` 从页面进是 200 条、从 curl 进是 100 条。
告警页还靠 `?limit=500` 绕开接口那个 100 —— 抄本一旦被绕过就不会有人去修它。

现在默认/上限/档位收进 `app/util.py`（`LIST_DEFAULT` / `LIST_CAP` / `parse_limit()` /
`list_ladder()`），两个列表接口把实际生效值与档位随结果一起下发，页面只回显收到的。
今天派生档位仍是 100/200/500，看得见的东西不变，变的是它只有一个出处。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import tempfile

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_22b_"))

import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app import util as app_util  # noqa: E402
from app.services import pg_service  # noqa: E402
from app.util import LIST_CAP, LIST_DEFAULT, list_ladder, parse_limit  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
REDIS_SRC = (ROOT / "backend/app/services/pg_service.py").read_text(encoding="utf-8")
PANEL_SRC = (ROOT / "web/src/components/RedisKeyPanel.vue").read_text(encoding="utf-8")
ALERTS_SRC = (ROOT / "web/src/views/Alerts.vue").read_text(encoding="utf-8")
AUDIT_SRC = (ROOT / "web/src/views/Audit.vue").read_text(encoding="utf-8")
API_SRC = (ROOT / "web/src/api.ts").read_text(encoding="utf-8")
ROUTER_ALERTS = (ROOT / "backend/app/routers/alerts.py").read_text(encoding="utf-8")
ROUTER_EXTRAS = (ROOT / "backend/app/routers/extras.py").read_text(encoding="utf-8")

# 旧抄本里的那些字面量。它们只允许出现在常数定义与注释里，不许再出现在读命令和比较里。
OLD_LITERAL_TOKENS = ('"65535"', '"65536"', "65535", "65536", '"199"', '"200"', "> 200")


def code_of(sfc: str) -> str:
    """剥掉注释后的源码：注释里可以叙述刚被删掉的那份副本，代码和模板里不行。"""
    no_html = re.sub(r"<!--.*?-->", "", sfc, flags=re.S)
    no_slash = re.sub(r"^[ \t]*//.*$", "", no_html, flags=re.M)
    return re.sub(r"^[ \t]*#.*$", "", no_slash, flags=re.M)


class TestRedisBoundariesAreDerived(unittest.TestCase):
    """读命令的参数与截断比较都由那两个常数算出来，不是换了个地方抄的一组数。"""

    def test_pipeline_argv_is_built_from_the_constants(self):
        cmds, kinds = pg_service._detail_commands("k")
        v, n = pg_service.REDIS_VALUE_BYTES, pg_service.REDIS_RANGE_ITEMS
        self.assertIn(["GETRANGE", "k", "0", str(v - 1)], cmds)
        self.assertIn(["HSCAN", "k", "0", "COUNT", str(n)], cmds)
        self.assertIn(["LRANGE", "k", "0", str(n - 1)], cmds)
        self.assertIn(["SSCAN", "k", "0", "COUNT", str(n)], cmds)
        self.assertIn(["ZRANGE", "k", "0", str(n - 1), "WITHSCORES"], cmds)
        self.assertEqual(len(cmds), len(kinds))

    def test_moving_the_constants_moves_the_commands(self):
        """把边界改小，命令必须跟着改小——证明参数是算出来的而不是恰好相等。"""
        with (mock.patch.object(pg_service, "REDIS_VALUE_BYTES", 1024),
              mock.patch.object(pg_service, "REDIS_RANGE_ITEMS", 50)):
            cmds, _ = pg_service._detail_commands("k")
        self.assertIn(["GETRANGE", "k", "0", "1023"], cmds)
        self.assertIn(["LRANGE", "k", "0", "49"], cmds)
        self.assertIn(["HSCAN", "k", "0", "COUNT", "50"], cmds)

    def test_no_literal_boundaries_left_in_the_readers(self):
        for fn in ("_detail_commands", "_redis_key_cli", "redis_key"):
            src = inspect.getsource(getattr(pg_service, fn))
            for token in OLD_LITERAL_TOKENS:
                self.assertNotIn(token, src, f"{fn} 里还留着副本 {token}")

    def test_string_truncation_follows_the_constant(self):
        """边界改成 10 字节时，11 字节的值就该判成截断；写死 65536 的旧实现不会。"""
        async def fake_pipeline(cmds, **kw):
            return ["string", 120, None, 11, 0, 0, 0, 0, "x" * 10, [], [], [], []]

        with (mock.patch.object(pg_service, "REDIS_VALUE_BYTES", 10),
              mock.patch.object(pg_service.redis_client, "pipeline", fake_pipeline)):
            out = asyncio.run(pg_service.redis_key("k"))
        self.assertTrue(out["truncated"], "长度 11 > 边界 10 必须报截断")
        self.assertEqual(out["read_limits"], {"value_bytes": 10,
                                             "range_items": pg_service.REDIS_RANGE_ITEMS})

    def test_at_the_boundary_it_is_not_truncated(self):
        async def fake_pipeline(cmds, **kw):
            return ["string", 120, None, 10, 0, 0, 0, 0, "x" * 10, [], [], [], []]

        with (mock.patch.object(pg_service, "REDIS_VALUE_BYTES", 10),
              mock.patch.object(pg_service.redis_client, "pipeline", fake_pipeline)):
            out = asyncio.run(pg_service.redis_key("k"))
        self.assertFalse(out["truncated"], "恰好等于边界是全值，不是截断")

    def test_member_types_truncate_on_the_item_boundary(self):
        async def fake_pipeline(cmds, **kw):
            return ["list", 120, None, 0, 0, 11, 0, 0, "", [], ["m0", "m1"], [], []]

        with (mock.patch.object(pg_service, "REDIS_RANGE_ITEMS", 10),
              mock.patch.object(pg_service.redis_client, "pipeline", fake_pipeline)):
            out = asyncio.run(pg_service.redis_key("l"))
        self.assertTrue(out["truncated"], "11 条 > 边界 10 必须报截断")
        self.assertEqual(out["read_limits"]["range_items"], 10)

    def test_cli_fallback_agrees_with_the_pipeline(self):
        """回退路线（redis-cli）必须用同一对边界，否则连不上 TCP 时结论就换了。"""
        seen = []

        async def fake_rcli(args):
            seen.append(list(args))
            cmd = args[0]
            if cmd == "TYPE":
                return "list"
            if cmd == "TTL":
                return "120"
            if cmd == "MEMORY":
                return "70000"
            if cmd == "LLEN":
                return "11"
            if cmd == "LRANGE":
                return "m0\nm1"
            return ""

        with (mock.patch.object(pg_service, "REDIS_RANGE_ITEMS", 10),
              mock.patch.object(pg_service, "_rcli", fake_rcli)):
            out = asyncio.run(pg_service._redis_key_cli("l"))
        self.assertTrue(out["truncated"])
        self.assertEqual(out["read_limits"], {"value_bytes": pg_service.REDIS_VALUE_BYTES,
                                             "range_items": 10})
        self.assertIn(["LRANGE", "l", "0", "9"], seen, "末位下标必须是 N-1")

    def test_the_limits_ship_with_every_detail(self):
        """两条路线都下发 read_limits，页面才不必自己知道今天是多少。"""
        async def fake_pipeline(cmds, **kw):
            return ["none", -2, None, 0, 0, 0, 0, 0, "", [], [], [], []]

        with mock.patch.object(pg_service.redis_client, "pipeline", fake_pipeline):
            out = asyncio.run(pg_service.redis_key("ghost"))
        self.assertEqual(out["read_limits"], {"value_bytes": pg_service.REDIS_VALUE_BYTES,
                                             "range_items": pg_service.REDIS_RANGE_ITEMS})


class TestListLimitsAreDerived(unittest.TestCase):
    def test_clamps_and_fallbacks(self):
        self.assertEqual(parse_limit(None), LIST_DEFAULT)
        self.assertEqual(parse_limit(""), LIST_DEFAULT)
        self.assertEqual(parse_limit("abc"), LIST_DEFAULT, "非数字回落默认值，不抛 ValueError")
        self.assertEqual(parse_limit("0"), LIST_DEFAULT, "0 当'没填'，而不是给一个空列表")
        self.assertEqual(parse_limit("99999"), LIST_CAP)
        self.assertEqual(parse_limit("-1"), 1, "SQLite 里 LIMIT 负数＝不限制，必须夹下界")
        self.assertEqual(parse_limit("6", default=LIST_CAP), 6, "调用方可以指定自己的默认档")

    def test_ladder_is_derived_and_covers_default_and_ceiling(self):
        ladder = list_ladder()
        self.assertEqual(ladder, sorted(set(ladder)))
        self.assertIn(LIST_DEFAULT, ladder, "默认值必须能在下拉里被选中并回显")
        self.assertIn(LIST_CAP, ladder, "上限本身必须可选")
        self.assertTrue(all(1 <= n <= LIST_CAP for n in ladder), ladder)
        self.assertEqual(parse_limit(str(ladder[-1])), ladder[-1])

    def test_ladder_recomputes_when_the_constants_move(self):
        with (mock.patch.object(app_util, "LIST_DEFAULT", 150),
              mock.patch.object(app_util, "LIST_CAP", 600)):
            self.assertEqual(app_util.list_ladder(), [75, 150, 600])

    def test_default_above_ceiling_is_dropped_not_offered(self):
        with (mock.patch.object(app_util, "LIST_DEFAULT", 900),
              mock.patch.object(app_util, "LIST_CAP", 500)):
            ladder = app_util.list_ladder()
        self.assertNotIn(900, ladder, "不能给一个服务端一定给不出的档位")
        self.assertTrue(all(n <= 500 for n in ladder), ladder)

    def test_today_ladder_is_unchanged(self):
        self.assertEqual(list_ladder(), [100, 200, 500], "这批只统一出处，看得见的那几档不该变")

    def test_routers_do_not_write_their_own_numbers(self):
        src = code_of(ROUTER_ALERTS)
        self.assertNotIn("HISTORY_DEFAULT", src)
        self.assertNotIn("HISTORY_CAP", src)
        self.assertIn("parse_limit", src)
        self.assertIn("default=LIST_CAP", src, "未确认列表不传 limit 时要读到上限")
        head = code_of(ROUTER_EXTRAS).split('@router.get("/api/audit")')[-1]
        audit_src = head.split("\n@router.")[0]
        self.assertIn("parse_limit", audit_src)
        for token in ("100", "500"):
            self.assertNotIn(token, audit_src, f"/api/audit 里不该再出现字面量 {token}")


class TestEndpointsShipTheirOwnNumbers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.users = {"b22b-admin": "admin", "b22b-viewer": "viewer"}
        for name, role in cls.users.items():
            dbm.execute(
                "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (name, security.hash_pass("Password123"), role),
            )

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username IN (?,?)", tuple(cls.users))

    def _get(self, path: str, name: str = "b22b-admin"):
        token = security.sign_token(name, self.users[name])
        return self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    def test_audit_returns_an_envelope_with_its_own_limits(self):
        for i in range(3):
            dbm.audit("b22b-admin", f"b22b:probe:{i}", "ok")
        body = self._get("/api/audit").json()
        self.assertIsInstance(body, dict, "裸数组没地方说「截断到多少」")
        self.assertEqual(body["limit"], LIST_DEFAULT)
        self.assertEqual(body["ladder"], list_ladder())
        self.assertGreaterEqual(len(body["rows"]), 3)

    def test_audit_honours_an_explicit_limit_and_the_clamp(self):
        self.assertEqual(self._get("/api/audit?limit=2").json()["limit"], 2)
        self.assertEqual(self._get("/api/audit?limit=99999").json()["limit"], LIST_CAP)
        self.assertEqual(self._get("/api/audit?limit=-1").json()["limit"], 1)
        self.assertEqual(self._get("/api/audit?limit=abc").json()["limit"], LIST_DEFAULT)

    def test_audit_is_still_admin_only(self):
        self.assertEqual(self._get("/api/audit", "b22b-viewer").status_code, 403)
        self.assertEqual(self.client.get("/api/audit").status_code, 401)

    def test_history_carries_the_ladder(self):
        body = self._get("/api/alerts/history").json()
        self.assertEqual(body["limit"], LIST_DEFAULT)
        self.assertEqual(body["ladder"], list_ladder())

    def test_open_reads_up_to_the_cap_without_a_client_copied_limit(self):
        """告警页过去靠 ?limit=500 绕开接口自己的 100；绕过抄本没人会去修它。"""
        code = "b22b:bulk"
        for i in range(LIST_DEFAULT + 5):
            dbm.execute(
                "INSERT INTO alerts_log(ts,level,code,message,meta) VALUES(datetime('now'),?,?,?,?)",
                ("crit", code, f"bulk {i}", "{}"),
            )
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))
        body = self._get("/api/alerts/open").json()
        self.assertGreater(len(body["rows"]), LIST_DEFAULT, "默认仍按 100 截断就说明没收到上限那一档")
        self.assertTrue(any(r["code"] == code for r in body["rows"]))


class TestWebHasNoCopies(unittest.TestCase):
    def test_alerts_page_reads_the_numbers_from_the_response(self):
        src = code_of(ALERTS_SRC)
        for needle in ("最近 100 条", "最近 200 条", "最近 500 条", "ref(200)", "limit=500"):
            self.assertNotIn(needle, src, f"告警页不该再写死 {needle}")
        self.assertIn("data.value?.ladder", src)
        self.assertIn("shownLimit", src)
        self.assertIn("req('/alerts/open')", src)

    def test_audit_page_reads_the_numbers_from_the_response(self):
        src = code_of(AUDIT_SRC)
        for needle in ("最近 100 条", "最近 200 条", "最近 500 条", "ref(200)", "clamp 在 500"):
            self.assertNotIn(needle, src, f"审计页不该再写死 {needle}")
        self.assertIn("data.value?.ladder", src)
        self.assertIn("data.value?.rows", src)

    def test_api_helper_does_not_default_the_limit(self):
        src = code_of(API_SRC)
        self.assertNotIn("audit: (limit = 100)", src)
        # 批次80 把过滤与游标也收进选项对象；关键仍是 limit 只透传、不自带默认值
        self.assertRegex(src, r"audit: \(opts: \{ limit\?: number")

    def test_redis_panel_sentences_itself_from_the_payload(self):
        src = code_of(PANEL_SRC)
        self.assertNotIn("64KB", src, "文案里的字节数必须是接口给的")
        self.assertIn("read_limits", src)
        self.assertIn("truncNote", src)
        # 两种截断分别成句：string 谈字节与"不开放编辑"，集合谈条目数
        self.assertIn("仅读取前 ${items} 条", src)
        self.assertIn("仅读取前 ${size}", src)


if __name__ == "__main__":
    unittest.main()
