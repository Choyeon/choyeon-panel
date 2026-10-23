"""批次65：纳管 unit 对既有应用可达，且「单元不存在」不再是不完整的判词。

线上现场（2026-10-24）：`choyeon-book`（app id 40）在应用列表里是红色「单元不存在」，
而 book.choyeon.cc 是 200、3400 端口有人 listen、`choyeon-book.service` enabled+active。
差的是那一条记录没有 `unit_override`，于是面板按 `panel-choyeon-book.service` 去问 systemd，
问到一个从来不存在的名字：状态、运行日志、unit 页签、启停全部打在错的名字上。

两半缺陷：
1. **写入口不在界面上**。`unit_override` 只有「新建应用」表单能填；详情页保存载荷带着这个键
   （`AppDetail.vue` 的 `unit_override: edit.value.unit_override`），配置页却没有对应的输入框——
   `edit` 是从 app 整份展开的，所以它永远是原值。CLI 的 `app create` 同样没有这个参数。
   结果：登记错了以后，支持的工具链里没有任何一处能改回来。
2. **判词只说了一半，还把用户推向更坏的动作**。原话是「本机 systemd 里没有这个单元……请重新
   立即部署」。照做会由 `render_unit` 写出第二个 unit（`panel-choyeon-book.service`），
   ExecStart 指向同一个目录、同一个 3400，带 `Restart=always` 和正在服务的那个外部 unit 抢端口。

修法：后端在"面板的 unit 不存在 + 没登记纳管 + 本机有同名 unit 文件"时给一个 `suggestUnit`
（与 `ensure_env_link` 判 no-unit 用同一个读者：UNIT_DIR 下的文件在不在），列表与详情共用；
详情页补上「已有 unit」输入框与一键填入，判词改成指向纳管并明确别点部署；列表的状态列
从红色「单元不存在」改成黄色「未纳管」。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b65_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import apps as apps_router  # noqa: E402
from app.services import apps_service, systemd_ops  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
UI_TS = (SRC / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
APPS_VUE = (SRC / "web" / "src" / "views" / "Apps.vue").read_text(encoding="utf-8")
DETAIL_VUE = (SRC / "web" / "src" / "views" / "AppDetail.vue").read_text(encoding="utf-8")


def _props(active="inactive", load="loaded", pid="0"):
    return {"Id": "panel-demo.service", "ActiveState": active, "LoadState": load,
            "SubState": "dead", "MainPID": pid}


def _app(**kw):
    row = {"id": 1, "name": "demo", "unit_override": None, "port": 3400, "domain": None,
           "path": "/root/www/demo", "env": "[]", "type": "node", "start_cmd": "node server.js"}
    row.update(kw)
    return row


class SuggestUnitRuleTests(unittest.TestCase):
    """规则本身：只在"面板找不到 unit、没登记纳管、本机恰好有同名 unit 文件"三件事同时成立时给建议。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b65_units_")
        self.p = mock.patch.object(apps_service, "UNIT_DIR", self.dir)
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_same_named_unit_file_is_suggested(self):
        open(os.path.join(self.dir, "demo.service"), "w").close()
        self.assertEqual(apps_service.suggest_unit(_app(), True), "demo.service")

    def test_no_file_no_suggestion(self):
        # A/B 反证：同名 unit 文件不在时绝不凭空建议，红字照旧是「单元不存在」
        self.assertIsNone(apps_service.suggest_unit(_app(), True))

    def test_present_unit_is_not_a_problem_to_explain(self):
        open(os.path.join(self.dir, "demo.service"), "w").close()
        self.assertIsNone(apps_service.suggest_unit(_app(), False),
                          "unit 查到了就没有「未纳管」这回事，判词不该多出来")

    def test_already_externally_managed_row_gets_no_suggestion(self):
        open(os.path.join(self.dir, "demo.service"), "w").close()
        got = apps_service.suggest_unit(_app(unit_override="other.service"), True)
        self.assertIsNone(got, "已经登记过纳管却读不到 unit，是那个 unit 真的没了，不能建议改成同名")

    def test_suggestion_is_the_name_without_panel_prefix(self):
        # 建议值必须能直接填进「已有 unit」并通过 _validate 的 unit 名校验
        open(os.path.join(self.dir, "demo.service"), "w").close()
        got = apps_service.suggest_unit(_app(), True)
        self.assertTrue(got.endswith(".service"))
        self.assertNotIn("panel-", got)
        self.assertEqual(got, apps_service.unit_name(_app(unit_override="demo.service")))


class ListAppsCarriesSuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def _list(self, files, missing):
        rows = [_app()]
        state = systemd_ops.derive_state(_props(
            "inactive", load="not-found" if missing else "loaded"))

        async def q(sql, params=None):
            return rows if "FROM apps" in sql else []

        with mock.patch.object(apps_service, "UNIT_DIR", files), \
             mock.patch.object(apps_service.aio_db, "query", q), \
             mock.patch.object(apps_service.systemd_ops, "unit_states",
                               mock.AsyncMock(return_value={"panel-demo.service": state})), \
             mock.patch.object(apps_service.nginx_ops, "domains_by_port", lambda: {}), \
             mock.patch.object(apps_service, "_listen_lines", mock.AsyncMock(return_value="")):
            return await apps_service.list_apps()

    async def test_list_emits_the_suggested_unit(self):
        d = tempfile.mkdtemp(prefix="cp_b65_units_")
        open(os.path.join(d, "demo.service"), "w").close()
        out = await self._list(d, missing=True)
        self.assertTrue(out[0]["unitMissing"])
        self.assertEqual(out[0]["suggestUnit"], "demo.service")

    async def test_list_emits_none_when_nothing_shares_the_name(self):
        # A/B 反证：光有 not-found 不足以说"未纳管"，否则每个从没部署成功的应用都会被指成纳管漏登记
        d = tempfile.mkdtemp(prefix="cp_b65_units_")
        out = await self._list(d, missing=True)
        self.assertIsNone(out[0]["suggestUnit"])


class AppGetRouteCarriesSuggestionTests(unittest.IsolatedAsyncioTestCase):
    def _req(self, role="admin"):
        return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub="t", cp_exp=float("inf")),
                               query_params={})

    async def _get(self, files, missing):
        app = _app()
        state = systemd_ops.derive_state(_props(
            "inactive", load="not-found" if missing else "loaded"))

        async def unit_state(_unit):
            return state

        with mock.patch.object(apps_service, "UNIT_DIR", files), \
             mock.patch.object(apps_service, "get_app", lambda _id: app), \
             mock.patch.object(apps_router.systemd_ops, "unit_state", unit_state), \
             mock.patch.object(apps_router.dbm, "query_one", lambda *a: None), \
             mock.patch.object(apps_router.dbm, "query", lambda *a: []):
            return await apps_router.app_get(1, self._req())

    async def test_detail_agrees_with_the_list(self):
        d = tempfile.mkdtemp(prefix="cp_b65_units_")
        open(os.path.join(d, "demo.service"), "w").close()
        out = await self._get(d, missing=True)
        self.assertEqual(out["suggestUnit"], "demo.service",
                         "详情页与列表页在同一个事实上必须同一个出处，否则两个页面说法不一（批次㉜ 同类）")

    async def test_detail_has_the_key_even_without_a_suggestion(self):
        d = tempfile.mkdtemp(prefix="cp_b65_units_")
        out = await self._get(d, missing=True)
        self.assertIn("suggestUnit", out)
        self.assertIsNone(out["suggestUnit"])


class FrontendContractTests(unittest.TestCase):
    def test_unit_tag_says_unmanaged_when_there_is_a_suggestion(self):
        self.assertIn("未纳管", UI_TS)
        self.assertRegex(UI_TS.replace(" ", ""), r"(?s)if\(r\.unitMissing\).*suggestUnit",
                         "分支必须挂在 unitMissing 上并读 suggestUnit，不能另起一份状态判断")

    def test_plain_missing_unit_keeps_the_old_wording(self):
        # A/B 反证：判不出建议值时（比如从没部署成功）原话「单元不存在」必须留着——
        # 那种情况下「未纳管」是错的，会让人去填一个并不存在的外部 unit。
        self.assertIn("单元不存在", UI_TS)

    def test_both_readers_use_the_shared_tag(self):
        self.assertIn("unitTag(r)", APPS_VUE)
        self.assertIn("suggestUnit", APPS_VUE)
        self.assertIn("app.suggestUnit", DETAIL_VUE)

    def test_detail_page_can_write_unit_override(self):
        self.assertRegex(DETAIL_VUE.replace(" ", ""), r'v-model:value="edit.unit_override"',
                         "保存载荷早就带着 unit_override，配置页必须有对应输入框，否则既有应用改不动")
        self.assertIn('label="已有 unit"', DETAIL_VUE, "与新建表单同一栏名，别在两个页面用两种说法")

    def test_detail_page_warns_against_deploying_over_a_live_unit(self):
        self.assertIn("不要点「立即部署」", DETAIL_VUE)
        self.assertIn("抢同一个端口", DETAIL_VUE)

    def test_original_wording_survives_when_no_unit_shares_the_name(self):
        self.assertIn("服务从未部署成功或已被改名", DETAIL_VUE)


if __name__ == "__main__":
    unittest.main()
