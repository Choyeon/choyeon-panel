"""批次⑤L：对象删掉之后，按**名字**编码的残留状态。

覆盖的都是实测坏法：
1. `delete_app` 清了 apps/deployments/unit/nginx，却不清它按应用名编的四个 settings 键
   （`probe_expect:<名>` / `sent:probe-<名>` / `streak:probe:<名>` / `notified:probe:<名>`）。
   后果不是"多几行垃圾"：删掉应用再建一个同名的，新应用第一天就继承了旧状态——
   `sent:` 等于今天，于是它第一次无响应**一条通知都发不出去**（⑤K 约定 2 的同一类失明）。
2. 要清这些键就得能删 settings，而 `token_epoch:<用户>` 也是 settings 里的键，
   且是**删不得**的吊销墓碑（同名重建后旧 token 会原地复活）。所以删除必须走一个
   硬挡墓碑的入口，而不是靠每个调用方记住。
3. 数据库页可以删掉任意库，包括应用详情里"关联数据库"正指着的那个：应用记录留下一个
   指向不存在库的关联，服务连不上库，而面板里没有任何地方说明是谁删的。
"""

import asyncio
import itertools
import os
import tempfile
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch5l_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch5l_roots_"))

import unittest  # noqa: E402

from app import database as dbm  # noqa: E402
from app.services import alerts_service, apps_service, pg_service  # noqa: E402

_seq = itertools.count()


def _aname(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _dbn(prefix: str) -> str:
    """PG 库/角色名走 IDENT_RE：只允许字母数字下划线，连字符会被拒（实测）。"""
    return f"{prefix}{os.getpid()}_{next(_seq)}"


def _mk_app(name: str, **kw) -> int:
    row = {"name": name, "type": "node", "repo_url": "", "branch": "main",
           "path": f"{tempfile.gettempdir()}/{name}", "start_cmd": "node x.js"}
    row.update(kw)
    cols = ",".join(row)
    marks = ",".join("?" * len(row))
    return dbm.execute(f"INSERT INTO apps({cols}) VALUES({marks})", tuple(row.values()))


def _keys_like(fragment: str) -> list:
    return [r["key"] for r in dbm.query("SELECT key FROM settings WHERE key LIKE ?", (f"%{fragment}%",))]


class TestDeleteSettingGuard(unittest.TestCase):
    """删除 settings 只能走 dbm.delete_setting，而它硬挡吊销墓碑。"""

    def test_tombstones_are_refused_and_untouched(self):
        # 不写也不删全局 jwt_secret / token_epoch：本进程其它用例都在拿它们签验 token，
        # 这里只断"删不动"，值必须原样在那儿。
        dbm.set_setting("token_epoch:someone", "v1")
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", ("token_epoch:someone",))
        dbm.token_epoch()
        dbm.jwt_secret()
        for key in ("token_epoch:someone", "token_epoch", "jwt_secret"):
            before = dbm._read_setting(key)
            with self.assertRaises(ValueError, msg=key):
                dbm.delete_setting(key)
            self.assertEqual(dbm._read_setting(key), before, f"{key} 被拒之后必须一行都没删")

    def test_ordinary_key_is_removed(self):
        dbm.set_setting("sent:probe-demo", "2026-01-01")
        dbm.delete_setting("sent:probe-demo")
        self.assertIsNone(dbm.get_setting("sent:probe-demo"))

    def test_deleting_absent_key_is_a_noop(self):
        """forget_app 对每个键都要能重复调用：删一个从没覆写过探活的应用是常态。"""
        dbm.delete_setting("probe_expect:never-existed")
        self.assertEqual(_keys_like("probe_expect:never-existed"), [])


class TestProbeMarkerKeys(unittest.TestCase):
    """键名必须和真实写标记的那几处一模一样——否则 forget_app 清的是另一份键。"""

    def test_keys_are_the_ones_the_service_actually_writes(self):
        name = _aname("mk1")
        today = alerts_service._today()
        with mock.patch.object(alerts_service, "notify", mock.AsyncMock(return_value=True)):
            asyncio.run(alerts_service._check(f"probe-{name}", True, f"应用 {name} 无响应",
                                              code=f"probe:{name}", level="crit"))
        dbm.set_setting(f"probe_expect:{name}", "200")
        dbm.set_setting(f"streak:probe:{name}", "2")
        written = set(_keys_like(name))
        self.assertEqual(
            written, set(alerts_service.probe_marker_keys(name)),
            f"实际写下的键与 probe_marker_keys 不一致，实到：{sorted(written)}",
        )
        self.assertIn(f"sent:probe-{name}", written, f"基线：sent 标记应等于今天，收到 {dbm.get_setting('sent:probe-' + name)!r}")
        self.assertEqual(dbm.get_setting(f"sent:probe-{name}"), today)

    def test_long_name_uses_the_same_truncation_as_run_probes(self):
        """`run_probes` 把应用名截到 48 字符才编键；清理必须跟着截，否则长名字漏清。"""
        long = "a" * 30 + "-" + "b" * 30  # 61 字符
        keys = alerts_service.probe_marker_keys(long)
        self.assertTrue(all(long[:48] in k for k in keys), keys)
        self.assertNotIn(f"probe_expect:{long}", keys)

    def test_forget_app_leaves_other_apps_and_global_markers_alone(self):
        mine, other = _aname("mk2"), _aname("mk2-other")
        for n in (mine, other):
            dbm.set_setting(f"sent:probe-{n}", "2026-01-01")
            dbm.set_setting(f"notified:probe:{n}", "2026-01-01")
        dbm.set_setting("sent:disk", "2026-01-01")
        dbm.set_setting("notified:mem", "2026-01-01")
        alerts_service.forget_app(mine)
        self.assertEqual(_keys_like(mine), [])
        self.assertEqual(len(_keys_like(other)), 2, "别人的标记不能被牵连")
        self.assertEqual(dbm.get_setting("sent:disk"), "2026-01-01", "全局标记与某个应用无关")
        self.assertEqual(dbm.get_setting("notified:mem"), "2026-01-01")


class TestDeleteAppPurgesMarkers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_root = apps_service.APP_ROOT
        apps_service.APP_ROOT = tempfile.mkdtemp(prefix="cp5l_root_")
        self.name = _aname("del1")
        self.app_id = _mk_app(self.name, port=38599)

    def tearDown(self):
        apps_service.APP_ROOT = self._old_root

    async def _delete(self):
        with mock.patch.object(apps_service, "remove_unit", mock.AsyncMock()):
            await apps_service.delete_app(self.app_id, False)

    async def test_markers_gone_but_history_kept(self):
        today = alerts_service._today()
        for key, val in ((f"probe_expect:{self.name}", "200"),
                         (f"streak:probe:{self.name}", "3"),
                         (f"notified:probe:{self.name}", today)):
            dbm.set_setting(key, val)
        dbm.set_setting(f"sent:probe-{self.name}", today)
        # 历史行是另一条线：由 _checker_loop 按 alert_keep_days/rows 裁剪，删除应用不许动它
        dbm.execute("INSERT INTO alerts_log(ts,level,code,message) VALUES(datetime('now'),?,?,?)",
                    ("crit", f"probe:{self.name}", f"应用 {self.name} 无响应"))
        await self._delete()
        self.assertEqual(_keys_like(self.name), [], "删掉应用后它按名字编的标记必须全部消失")
        rows = dbm.query("SELECT id FROM alerts_log WHERE code=?", (f"probe:{self.name}",))
        self.assertEqual(len(rows), 1, "告警历史不是删除应用的附赠品（⑤K 约定 4）")

    async def test_recreated_same_name_gets_its_first_notification(self):
        """⑤L 的实际后果：旧 sent: 让同名新应用当天第一次故障静默。"""
        dbm.set_setting(f"sent:probe-{self.name}", alerts_service._today())
        await self._delete()
        _mk_app(self.name, port=38599)
        notify = mock.AsyncMock(return_value=True)
        with mock.patch.object(alerts_service, "notify", notify):
            await alerts_service._check(f"probe-{self.name}", True, f"应用 {self.name} 无响应",
                                        code=f"probe:{self.name}", level="crit")
        self.assertEqual(notify.await_count, 1, f"同名重建后第一次故障必须真投递，实到 {notify.await_count} 次")

    async def test_delete_without_any_override_still_works(self):
        """绝大多数应用从没覆写过探活：forget_app 不能因此报错。"""
        await self._delete()
        self.assertIsNone(dbm.query_one("SELECT id FROM apps WHERE id=?", (self.app_id,)))


class TestDropDbOwnership(unittest.IsolatedAsyncioTestCase):
    async def test_owned_db_is_refused_before_sql(self):
        name, other = _aname("pg1"), _aname("pg1-neighbor")
        _mk_app(name, db_names="rosetta,shared_db")
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE name=?", (name,))
        _mk_app(other, db_names="xrosettay")  # 只是包含子串，不是同一个库
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE name=?", (other,))
        psql = mock.AsyncMock()
        with mock.patch.object(pg_service, "psql", psql):
            with self.assertRaises(RuntimeError) as ctx:
                await pg_service.drop_db("rosetta")
            self.assertIn(name, str(ctx.exception))
            self.assertIn("取消关联", str(ctx.exception))
            self.assertEqual(psql.await_count, 0, "拒绝必须在任何 SQL 之前")
            # 子串同名不算关联，仍然放行
            await pg_service.drop_db("rosetta" + "x")
            self.assertEqual(psql.await_count, 1)

    async def test_unowned_db_still_drops(self):
        psql = mock.AsyncMock()
        with mock.patch.object(pg_service, "psql", psql):
            await pg_service.drop_db(_dbn("pgfree"))
        self.assertEqual(psql.await_count, 1)

    async def test_app_delete_unblocks_the_db(self):
        """关联是应用记录的属性：应用没了，这条守卫就不该再挡住删库。"""
        name = _aname("pg2")
        app_id = _mk_app(name, db_names="gone_db")
        with mock.patch.object(apps_service, "remove_unit", mock.AsyncMock()):
            await apps_service.delete_app(app_id, False)
        psql = mock.AsyncMock()
        with mock.patch.object(pg_service, "psql", psql):
            await pg_service.drop_db("gone_db")
        self.assertEqual(psql.await_count, 1)


if __name__ == "__main__":
    unittest.main()
