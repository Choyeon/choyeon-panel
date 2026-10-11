"""批次⑤M：应用**改名**之后，那些按名字编的状态没人跟着走。

⑤L 管的是"对象删了，名字的残留态要带走"，这一批是另一半：对象还在，名字换了。
`UPDATE apps SET name=?` 只改这一行，而下面这些状态是拿应用名当键的，实测全部留在旧名字上：

1. `probe_expect:<旧名>`：管理员给这个应用单独设过的预期状态码就此不生效，探活悄悄回落到
   全局 `alert_probe_expect`——而 Settings 里那条覆写还挂在那儿，看着是生效的。
2. `notified:probe:<旧名>`：改名前正处于"已发过告警"的状态，改名后新 code 是
   `probe:<新名>`，没有任何一条路径会再读旧标记，于是这场故障永远没有「已恢复」（约定 5）。
3. `sent:probe-<旧名>`：当天已发过的去重标记留在旧名上，新名字从"今天没发过"重新开始，
   改名当天同一次故障会被推两遍。
4. `backups` 里 `kind='app'` 的 target 存的就是应用名（`backup_runner` 按
   `WHERE name=?` 找目录），改名后那一晚 timer 跑到就 SystemExit 一次，面板上这条任务
   仍显示"已启用"——和⑤L 的"删应用后 target 悬空"同一类，只是触发条件换成了改名。

还有一处设计判断写在这里，免得以后有人把它"修"回去：**旧名字的历史行不改写**。
`alerts_log` 里 `probe:<旧名>` 是改名那一刻已经发生的事实，搬走等于篡改历史；
改名后新 code 从空历史开始，第一次判定该记的照样记。
"""

import asyncio
import itertools
import os
import tempfile
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch5m_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch5m_roots_"))

import unittest  # noqa: E402

from app import database as dbm  # noqa: E402
from app.services import alerts_service, apps_service, backup_service  # noqa: E402

_seq = itertools.count()


def _aname(prefix: str) -> str:
    """应用名口径：小写字母开头，只允许小写字母/数字/连字符（NAME_RE，≤39 字符）。"""
    return f"{prefix}-{os.getpid()}-{next(_seq)}"[:39]


def _free_ports(n: int = 2) -> list:
    """整轮 pytest 共用一个库，端口要按"当前库里没人用"来挑，不能凭印象写死。"""
    used = {r["port"] for r in dbm.query("SELECT port FROM apps WHERE port IS NOT NULL")}
    out, p = [], 38100 + next(_seq) * 50
    while len(out) < n:
        if p not in used:
            out.append(p)
        p += 1
    return out


def _mk_app(name: str, **kw) -> int:
    row = {"name": name, "type": "node", "repo_url": "", "branch": "main",
           "path": f"{tempfile.gettempdir()}/{name}", "start_cmd": "node x.js"}
    row.update(kw)
    cols = ",".join(row)
    marks = ",".join("?" * len(row))
    return dbm.execute(f"INSERT INTO apps({cols}) VALUES({marks})", tuple(row.values()))


def _mk_backup(kind: str, target: str) -> int:
    return dbm.execute(
        "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES(?,?,?,?,?,?,1)",
        (kind, target, "daily", 3, 30, 7),
    )


def _survivors(name: str) -> list:
    """这个名字在 settings 里还留着哪几个键（行存在即为残留，空串也算）。"""
    return [k for k in alerts_service.probe_marker_keys(name) if dbm._read_setting(k) is not None]


def _refused(*_a, **_kw):
    raise ConnectionRefusedError(111, "Connection refused")


def _streams(status_line: bytes):
    class _Reader:
        async def readline(self):
            return status_line

    class _Writer:
        def write(self, data):
            self.sent = data

        async def drain(self):
            pass

        def close(self):
            pass

    return _Reader(), _Writer()


def _responds(status_line: bytes):
    async def fake(*_a, **_kw):
        return _streams(status_line)

    return fake


def _apps_named(app_id: int, name: str, port: int):
    async def fake(sql, params=()):
        if "FROM apps" in sql:
            return [{"id": app_id, "name": name, "port": port}]
        return []

    return fake


class _Case(unittest.TestCase):
    """公共夹具：一对唯一名字，收尾把两边的标记与历史都清干净。"""

    def setUp(self):
        self.old = _aname("old")
        self.new = _aname("new")
        self.app_id = None
        self.backup_ids = []
        for name in (self.old, self.new):
            for key in alerts_service.probe_marker_keys(name):
                self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", (key,))
            self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (f"probe:{name}",))

    def tearDown(self):
        if self.app_id is not None:
            dbm.execute("DELETE FROM apps WHERE id=?", (self.app_id,))
            dbm.execute("DELETE FROM deployments WHERE app_id=?", (self.app_id,))
        for bid in self.backup_ids:
            dbm.execute("DELETE FROM backups WHERE id=?", (bid,))

    def mk_backup(self, kind: str, target: str) -> int:
        bid = _mk_backup(kind, target)
        self.backup_ids.append(bid)
        return bid

    def target_of(self, bid: int) -> str:
        return dbm.query_one("SELECT target FROM backups WHERE id=?", (bid,))["target"]


class TestRenameMarkerMigration(_Case):
    """`alerts_service.rename_app`：四个键整体搬走，旧名字一行都不留。"""

    def test_all_four_markers_move(self):
        vals = ["404", "2026-10-11", "3", "2026-10-11"]
        for key, v in zip(alerts_service.probe_marker_keys(self.old), vals, strict=False):
            dbm.set_setting(key, v)
        alerts_service.rename_app(self.old, self.new)
        self.assertEqual([dbm.get_setting(k) for k in alerts_service.probe_marker_keys(self.new)], vals)
        self.assertEqual(
            _survivors(self.old),
            [],
            "旧名字的标记必须一行都不留，否则 Settings 的覆写列表里多一条指向不存在应用的幽灵行",
        )

    def test_empty_old_value_never_clobbers_the_new_one(self):
        """恢复时 `notified:` 是被清成空串的；空值不该把新名字上已有的覆写擦掉。"""
        dbm.set_setting(f"probe_expect:{self.new}", "500")
        dbm.set_setting(f"notified:probe:{self.old}", "")
        alerts_service.rename_app(self.old, self.new)
        self.assertEqual(dbm.get_setting(f"probe_expect:{self.new}"), "500")
        self.assertIsNone(dbm._read_setting(f"notified:probe:{self.old}"), "空行仍然要删掉")

    def test_noop_when_the_effective_key_is_the_same(self):
        dbm.set_setting(f"probe_expect:{self.old}", "404")
        alerts_service.rename_app(self.old, self.old)
        self.assertEqual(
            dbm.get_setting(f"probe_expect:{self.old}"),
            "404",
            "改名改回同一个键（或截断后同键）时不得把值搬没了",
        )

    def test_history_rows_are_not_rewritten(self):
        dbm.execute(
            "INSERT INTO alerts_log(ts,level,code,message) VALUES(datetime('now'),?,?,?)",
            ("crit", f"probe:{self.old}", "应用无响应"),
        )
        alerts_service.rename_app(self.old, self.new)
        self.assertEqual(len(dbm.query("SELECT 1 FROM alerts_log WHERE code=?", (f"probe:{self.old}",))), 1)
        self.assertEqual(dbm.query("SELECT 1 FROM alerts_log WHERE code=?", (f"probe:{self.new}",)), [])


class TestUpdateAppRenameSideEffects(_Case):
    """产品路径：`apps_service.update_app` 改名时把两类名字派生态一起带走。"""

    def _prepare(self):
        self.port = _free_ports(2)[0]
        self.app_id = _mk_app(self.old, port=self.port)
        self.app_backup = self.mk_backup("app", self.old)
        self.legacy_sqlite = self.mk_backup("sqlite", self.old)
        self.id_sqlite = self.mk_backup("sqlite", str(self.app_id))
        self.pg_backup = self.mk_backup("pg", "somedb")
        dbm.set_setting(f"probe_expect:{self.old}", "404")
        dbm.set_setting(f"notified:probe:{self.old}", "2026-10-11")
        return apps_service.update_app(self.app_id, {"name": self.new})

    def test_markers_and_targets_follow_the_rename(self):
        row = self._prepare()
        self.assertEqual(row["name"], self.new)
        self.assertEqual(dbm.get_setting(f"probe_expect:{self.new}"), "404")
        self.assertEqual(dbm.get_setting(f"notified:probe:{self.new}"), "2026-10-11")
        self.assertEqual(_survivors(self.old), [], f"改名后 {self.old} 的标记一个都不该剩")
        self.assertEqual(self.target_of(self.app_backup), self.new)
        self.assertEqual(
            self.target_of(self.legacy_sqlite),
            self.new,
            "历史按名配置的 sqlite 任务同样会悬空，必须一起改",
        )

    def test_renamed_app_backup_task_still_resolves_its_target(self):
        """`backup_runner` 就是拿 target 当名字查目录的，这一条等价于"那一晚不再 SystemExit"。"""
        self._prepare()
        bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (self.app_backup,))
        self.assertIsNotNone(
            dbm.query_one("SELECT path FROM apps WHERE name=?", (bk["target"],)),
            f"备份任务 #{bk['id']} 的 target={bk['target']} 查不到应用，timer 会当场 SystemExit",
        )
        shown = [b for b in backup_service.list_backups() if b["id"] == self.app_backup][0]
        self.assertFalse(shown["target_missing"], "改名后这条任务在备份列表里必须回到正常状态")

    def test_id_and_pg_targets_are_left_alone(self):
        self._prepare()
        self.assertEqual(
            self.target_of(self.id_sqlite),
            str(self.app_id),
            "按 id 存的 sqlite 任务本来就不受改名影响，不该被动过",
        )
        self.assertEqual(self.target_of(self.pg_backup), "somedb")

    def test_edit_without_rename_touches_nothing(self):
        """只改端口时不许有任何搬迁：否则一次普通保存就会挪走别人的告警状态。"""
        self.port, second = _free_ports(2)
        self.app_id = _mk_app(self.old, port=self.port)
        bid = self.mk_backup("app", self.old)
        dbm.set_setting(f"probe_expect:{self.old}", "404")
        apps_service.update_app(self.app_id, {"port": second})
        self.assertEqual(dbm.get_setting(f"probe_expect:{self.old}"), "404")
        self.assertEqual(self.target_of(bid), self.old)
        self.assertEqual(_survivors(self.new), [])

    def test_external_unit_rename_still_migrates(self):
        """挂在 update_app 而不是 apply_edited 的理由：外部 unit 那条路径会提前返回。"""
        self.port = _free_ports(1)[0]
        self.app_id = _mk_app(self.old, port=self.port)
        dbm.set_setting(f"probe_expect:{self.old}", "404")
        apps_service.update_app(self.app_id, {"name": self.new, "unit_override": "external.service"})
        self.assertEqual(dbm.get_setting(f"probe_expect:{self.new}"), "404")
        self.assertEqual(_survivors(self.old), [])


class TestProbeContinuityAcrossRename(_Case):
    """端到端：改一次名，探活的两条线（恢复通知 / 当天去重）都不断。"""

    def setUp(self):
        super().setUp()
        self.port = _free_ports(1)[0]
        self.app_id = _mk_app(self.old, port=self.port)
        dbm.set_setting(f"probe_expect:{self.old}", "200")
        self.sent = []

        async def spy(text):
            self.sent.append(text)
            return True

        self.spy = spy

    def _probe(self, name, opener):
        with (
            mock.patch.object(alerts_service.aio_db, "query", _apps_named(self.app_id, name, self.port)),
            mock.patch("asyncio.open_connection", opener),
            mock.patch.object(alerts_service, "notify", self.spy),
        ):
            asyncio.run(alerts_service.run_probes())

    def _rows(self, name):
        return dbm.query("SELECT level, message FROM alerts_log WHERE code=? ORDER BY id", (f"probe:{name}",))

    def test_recovery_notice_survives_the_rename(self):
        self._probe(self.old, _refused)
        self.assertEqual(len(self.sent), 1, f"基线：连不上要推送，实到 {self.sent}")
        self.assertEqual(dbm.get_setting(f"notified:probe:{self.old}"), alerts_service._today())

        apps_service.update_app(self.app_id, {"name": self.new})
        self.sent.clear()
        self._probe(self.new, _responds(b"HTTP/1.1 200 OK\r\n"))

        self.assertEqual(
            len(self.sent),
            1,
            f"改名后恢复必须补一条「已恢复」（notified: 要跟着搬到新名字），实到 {self.sent}",
        )
        self.assertIn("已恢复", self.sent[0])
        self.assertFalse(dbm.get_setting(f"notified:probe:{self.new}"), "补发恢复后标记要清掉")
        self.assertEqual([r["level"] for r in self._rows(self.new)], ["ok"])

    def test_same_outage_is_not_notified_twice_after_rename(self):
        """`sent:` 也要跟着搬：否则改名当天同一次无响应会被推两遍。"""
        self._probe(self.old, _refused)
        self.assertEqual(len(self.sent), 1)
        apps_service.update_app(self.app_id, {"name": self.new})
        self.assertEqual(dbm.get_setting(f"sent:probe-{self.new}"), alerts_service._today())
        self.sent.clear()
        self._probe(self.new, _refused)
        self.assertEqual(self.sent, [], f"故障没变、同一天、同一个应用，不得因为改名再推一条：{self.sent}")

    def test_per_app_expectation_override_still_applies(self):
        """预期状态码的覆写是"每应用"配置：改名后必须继续按搬到新名字的那份判定。"""
        apps_service.update_app(self.app_id, {"name": self.new})
        self.assertEqual(dbm.get_setting(f"probe_expect:{self.new}"), "200", "覆写要跟着应用走")
        self.sent.clear()
        self._probe(self.new, _responds(b"HTTP/1.1 200 OK\r\n"))
        self.assertEqual(
            [r["level"] for r in self._rows(self.new)],
            ["ok"],
            "200 在预期内就该判存活；判成 crit 说明覆写没跟过来（回落到了全局默认）",
        )

    def test_a_rename_never_overwrites_the_target_apps_own_override(self):
        """反向不变量：另一个应用改名撞进来时，以它自己的值为准，但本次应用的键不该多出死行。"""
        other = _aname("oth")
        for key in alerts_service.probe_marker_keys(other):
            self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", (key,))
        dbm.set_setting(f"probe_expect:{self.new}", "500")
        dbm.set_setting(f"probe_expect:{other}", "404")
        alerts_service.rename_app(other, self.new)
        self.assertEqual(dbm.get_setting(f"probe_expect:{self.new}"), "404", "后来的改名以本应用的新值为准")
        self.assertEqual(_survivors(other), [])


if __name__ == "__main__":
    unittest.main()
