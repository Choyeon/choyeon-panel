"""批次90：清单进了告警、自检与拒绝理由，也不许只交前几个就闭嘴。

批次89 把自检页与判词里的清单接上了 `util.name_list`，这一批收尾剩下几处同类的账，
外加一处更隐蔽的：告警的取值列。

- `alerts_service._meta` 写的是 `json.dumps(kv)[:400]`。这一列在历史页是被 parse 之后
  展开给人看的，从中间剪一刀交出去的是半截 JSON——parse 失败，那一格退化成原样糊出来的
  一串引号，这条告警的取值等于全丢，而且没有任何地方说它丢了。改按字段收窄：清单留前三项
  并附真实长度，长字符串按预算截并留下未收窄时的字节数，怎么裁结果都是一行能 parse 的 JSON。
- failed-units 这一条把 `units[:20]` 写进 meta：第 21 个 failed 单元既不在 meta 里，也没人
  说过"还有几个"；正文却是 `', '.join(units)` 全量拼接，长机器上一屏刷不完。正文改用
  `name_list`（点名 + 总数），取值整份交给 `_meta` 收窄。
- `record` 的去重拿**未裁剪**的 message 和**已裁剪**的 last["message"] 比：超过 500 字的告警
  每轮都"看起来变了"，历史被同一条故障按 30 分钟一行刷满。改成比对真正落库那一份。
- 登录安全自检的 fail 判词写 `"、".join((空口令 + 多余 uid0)[:5])`：第 6 个账号既没点名
  也没计数，而这一条的语义是"这台机器可能已经被人进来了"。
- `cert_ops.renewal_hint` 只点前 3 张到期证书：顶栏那句"请尽快手动续期"于是永远只让人
  数得清 3 张，第四张在页面上不存在。
- 重启标记的包名单读出来就 `[:20]`，总数既不进载荷也不进终端；`cloud_backup.delete_target`
  的拒绝理由 `linked[:5]` 同一形状——被挡住删除的任务数说不全，管理员就不知道要先取消几条关联。

真实权限、真实读不到的三态在批次75/76/77 已经钉过；本批只管"读到了但只交一截"。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch90_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch90_bak_"))

import cli  # noqa: E402
from app import database as dbm  # noqa: E402
from app.services import alerts_service as A  # noqa: E402
from app.services import cert_ops as CERTS  # noqa: E402
from app.services import cloud_backup as CLOUD  # noqa: E402
from app.services import login_audit_service as LOGIN  # noqa: E402
from app.services import pending_service as PS  # noqa: E402

_seq = itertools.count()
ISO = "2026-10-11T22:49:04+08:00"


# ---- 告警取值列：收窄而不是剪断 ----

class MetaBudgetTests(unittest.TestCase):
    def test_small_payload_is_stored_verbatim(self):
        raw = A._meta(pct=40, threshold=85)
        self.assertEqual(json.loads(raw), {"pct": 40, "threshold": 85})
        self.assertNotIn("meta_full_chars", raw, "没超预算就不要多塞一句")

    def test_short_list_is_not_narrowed(self):
        units = ["a.service", "b.service"]
        self.assertEqual(json.loads(A._meta(units=units)), {"units": units})

    def test_long_list_keeps_three_names_and_the_real_length(self):
        units = [f"ut90-{i}-some-long-unit-name.service" for i in range(25)]
        raw = A._meta(units=units)
        self.assertLessEqual(len(raw), A.META_LIMIT)
        got = json.loads(raw)  # 半截 JSON 在这里就会抛，不靠肉眼
        self.assertEqual(got["units_total"], 25)
        self.assertEqual(len(got["units"]), 3)
        self.assertGreater(got["meta_full_chars"], A.META_LIMIT)

    def test_long_string_is_clipped_with_its_true_length(self):
        raw = A._meta(error="E" * 3000)
        self.assertLessEqual(len(raw), A.META_LIMIT)
        got = json.loads(raw)
        self.assertEqual(len(got["error"]), A.META_STR_CHARS)
        self.assertEqual(got["meta_full_chars"], len(json.dumps({"error": "E" * 3000},
                                                                ensure_ascii=False)))

    def test_many_keys_fall_back_to_key_names_not_half_json(self):
        kv = {f"k{i}": "V" * 60 for i in range(40)}
        raw = A._meta(**kv)
        self.assertLessEqual(len(raw), A.META_LIMIT)
        got = json.loads(raw)
        self.assertIn("k0", got["meta_keys"])
        self.assertTrue(got["meta_narrowed"])

    def test_every_shape_stays_parsable(self):
        """无论多大都必须是一行能 parse 的 JSON：历史页那一格靠它展开。"""
        shapes = [
            {"units": [f"u{i}.service" for i in range(300)]},
            {"units": [f"u{i}.service" for i in range(300)], "detail": "x" * 2000},
            {"note": "y" * 500, "backup": 7, "status": "exit 1"},
            {f"key{i}": f"value{i}" * 40 for i in range(60)},
            {"empty": []},
        ]
        for kv in shapes:
            raw = A._meta(**kv)
            with self.subTest(keys=sorted(kv)[:3]):
                self.assertLessEqual(len(raw), A.META_LIMIT)
                json.loads(raw)

    def test_unserialisable_still_returns_empty(self):
        self.assertEqual(A._meta(bad={1, 2}), "")


class RecordClipTests(unittest.TestCase):
    def setUp(self):
        self.code = f"ut90-{os.getpid()}-{next(_seq)}"
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (self.code,))

    def _rows(self):
        return dbm.query("SELECT * FROM alerts_log WHERE code=? ORDER BY id", (self.code,))

    def test_oversized_message_is_declared_and_deduped(self):
        """去重键必须是真正落库那一份文字。

        旧写法比的是未裁剪的 message：超过列预算的告警每轮都"变了"，同一条故障
        按巡检周期一路刷行，列表里再也看不到别的事。
        """
        msg = "Z" * (A.MESSAGE_LIMIT + 120)
        for _ in range(3):
            asyncio.run(A.record("warn", self.code, msg))
        rows = self._rows()
        self.assertEqual(len(rows), 1, f"同一条超限告警刷出了 {len(rows)} 行历史")
        self.assertIn(f"告警文字共 {len(msg)} 字符", rows[0]["message"])
        self.assertLess(len(rows[0]["message"]), len(msg))

    def test_short_message_stays_verbatim(self):
        asyncio.run(A.record("warn", self.code, "磁盘使用率 91%"))
        self.assertEqual(self._rows()[0]["message"], "磁盘使用率 91%")

    def test_meta_reaches_the_column_whole_and_parsable(self):
        meta = A._meta(units=[f"u{i}.service" for i in range(25)])
        asyncio.run(A.record("crit", self.code, "存在 failed 状态服务", meta=meta))
        row = self._rows()[0]
        self.assertEqual(row["meta"], meta, "落库的那份和算出来的那份必须同一个")
        # 25 个短名字塞得进预算：一份都不许少，也不许出现收窄键
        self.assertEqual(len(json.loads(row["meta"])["units"]), 25)
        self.assertNotIn("units_total", json.loads(row["meta"]))


# ---- failed-units 整轮：正文点名 + 总数，取值进 meta ----

_SNAP = {"cpu": 12.0, "memTotal": 1000, "memUsed": 200, "load": [0.4, 0.3, 0.2],
         "cpuCores": 4, "history": [{"t": 0, "cpu": 11.0}, {"t": 3000, "cpu": 12.0}]}


class FailedUnitsRoundTests(unittest.TestCase):
    UNITS = [f"ut90x{i}-a-somewhat-long-unit-name.service" for i in range(25)]

    def setUp(self):
        self.codes = ("failed-units", "disk", "cpu", "mem", "load1")
        marks = ",".join("?" for _ in self.codes)
        dbm.execute(f"DELETE FROM alerts_log WHERE code IN ({marks})", self.codes)
        self.addCleanup(dbm.execute, f"DELETE FROM alerts_log WHERE code IN ({marks})", self.codes)
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("sent:%",))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("streak:%",))

    async def _run(self, cmd, args, **kw):
        if "--state=failed" in args:
            out = "".join(f"{u} loaded failed failed ut90 probe\n" for u in self.UNITS)
            return {"code": 0, "out": out, "err": ""}
        return {"code": 0, "out": "", "err": ""}

    def _round(self):
        async def snap():
            return dict(_SNAP)

        async def no_probes():
            return []

        async def no_notify(_text):
            return False

        with (mock.patch.object(A, "run", self._run),
              mock.patch.object(A, "disk_usage_pct", lambda p: 40),
              mock.patch.object(A, "run_probes", no_probes),
              mock.patch.object(A, "notify", no_notify),
              mock.patch.object(A.system_metrics, "snapshot", snap)):
            asyncio.run(A._run_checks())

    def _row(self):
        return dbm.query_one("SELECT * FROM alerts_log WHERE code='failed-units'")

    def test_message_names_ten_and_declares_the_rest(self):
        self._round()
        msg = self._row()["message"]
        self.assertIn("存在 failed 状态服务", msg)
        self.assertIn(self.UNITS[0], msg)
        self.assertIn("共 25 个，另 15 个未列出", msg)
        self.assertNotIn(self.UNITS[10], msg, "正文只点名前十个")

    def test_meta_keeps_the_real_unit_count(self):
        self._round()
        got = json.loads(self._row()["meta"])
        self.assertEqual(got["units_total"], 25)
        self.assertEqual(len(got["units"]), 3)

    def test_two_rounds_write_one_row(self):
        """集合没变就不该再长一行——正文带总数也必须仍然是稳定文本。"""
        self._round()
        first = self._row()["message"]
        self._round()
        rows = dbm.query("SELECT id FROM alerts_log WHERE code='failed-units'")
        self.assertEqual(len(rows), 1, "同一批 failed 单元刷出了多行历史")
        self.assertEqual(self._row()["message"], first)


# ---- 登录安全自检的 fail 判词：六个账号不能说成五个 ----

def _login_data(empty, extra=()):
    return {
        "sshd": {"source": "sshd -T", "effective": True, "values": {}},
        "accounts": {"login_shells": [], "login_count": 0, "uid0_extra": list(extra),
                     "empty_password": list(empty), "shadow_unreadable": False,
                     "sudo_groups": {}, "notes": []},
        "keys": {"files": [], "scanned": 0, "total_keys": 0, "truncated": False},
        "counts": {"risk": 1, "warn": 0, "info": 0},
        "findings": [{"id": "root-password", "severity": "risk", "title": "root 还能用口令登录",
                      "detail": "", "fix": ""}],
        "notes": [],
    }


class LoginDoctorTotalTests(unittest.TestCase):
    def test_eight_bad_accounts_report_eight(self):
        got = LOGIN.doctor_item(_login_data([f"u{i}" for i in range(8)]))
        self.assertEqual(got["status"], "fail")
        self.assertIn("共 8 个，另 3 个未列出", got["detail"])
        self.assertIn("空口令或额外 uid=0", got["detail"])

    def test_four_bad_accounts_are_all_named_without_a_marker(self):
        got = LOGIN.doctor_item(_login_data(["a", "b", "c", "d"]))
        self.assertNotIn("未列出", got["detail"])
        self.assertIn("a、b、c、d", got["detail"])

    def test_empty_and_uid0_count_together(self):
        got = LOGIN.doctor_item(_login_data(["e1", "e2", "e3"], extra=["x1", "x2", "x3", "x4"]))
        self.assertIn("共 7 个，另 2 个未列出", got["detail"])


# ---- 证书续期提示：第四张到期证书也得被数进去 ----

def _certs(n):
    return [{"name": f"site{i}.example.com", "status": "expiring", "days": 3} for i in range(n)]


class RenewalHintTotalTests(unittest.TestCase):
    def test_five_expiring_certs_say_five(self):
        hint = CERTS.renewal_hint({"status": "missing"}, _certs(5))
        self.assertIn("5 张证书即将或已经到期", hint)
        self.assertIn("共 5 张，另 2 张未列出", hint)
        self.assertIn("site0.example.com", hint)

    def test_two_expiring_certs_are_all_named(self):
        hint = CERTS.renewal_hint({"status": "missing"}, _certs(2))
        self.assertIn("2 张证书即将或已经到期", hint)
        self.assertNotIn("未列出", hint)

    def test_timer_installed_stays_short_and_honest(self):
        self.assertIn("已安装续期定时器", CERTS.renewal_hint({"status": "installed"}, _certs(9)))

    def test_unknown_timer_still_says_cannot_confirm(self):
        self.assertIn("无法确认", CERTS.renewal_hint({"status": "unknown"}, []))


# ---- 重启标记的包名单：载荷给前 20 个并说总数 ----

class MarkerPkgTotalTests(unittest.TestCase):
    DPKG = "linux-image-6.12.1-1-amd64|6.12.1-1|ii \n"
    RELEASE = "6.12.1-1-amd64"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b90_marker_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.marker = Path(self.dir, "reboot-required")
        self.pkgs = Path(self.dir, "reboot-required.pkgs")

    def _kernel(self, lines, pkgs_path=None):
        self.marker.write_text("reboot required\n", encoding="utf-8")
        if pkgs_path is None:
            self.pkgs.write_text("".join(f"{p}\n" for p in lines), encoding="utf-8")
            pkgs_path = str(self.pkgs)

        def fake_run(cmd, **kw):
            return mock.Mock(returncode=0, stdout=self.DPKG, stderr="")

        with (mock.patch.object(PS, "REBOOT_MARKER", str(self.marker)),
              mock.patch.object(PS, "REBOOT_MARKER_PKGS", pkgs_path),
              mock.patch.object(PS.subprocess, "run", fake_run),
              mock.patch.object(PS.os, "uname", lambda: mock.Mock(release=self.RELEASE))):
            return PS._kernel_state(1790765667)

    def test_thirty_five_pkgs_report_thirty_five(self):
        out = self._kernel([f"pkg-{i}" for i in range(35)])
        self.assertTrue(out["marker"]["present"])
        self.assertEqual(out["marker"]["files_total"], 35)
        self.assertEqual(len(out["marker"]["files"]), PS.MARKER_PKGS_SHOWN)

    def test_few_pkgs_need_no_total_marker(self):
        out = self._kernel(["pkg-1", "pkg-2"])
        self.assertEqual(out["marker"]["files"], ["pkg-1", "pkg-2"])
        self.assertEqual(out["marker"]["files_total"], 2)

    def test_unreadable_pkg_list_is_zero_not_absent(self):
        """标记挂着但名单读不到（权限/被删）：present 仍为真，名单 0 个，不猜内容。"""
        out = self._kernel([], pkgs_path=str(Path(self.dir, "nope")))
        self.assertTrue(out["marker"]["present"])
        self.assertEqual(out["marker"]["files_total"], 0)
        self.assertEqual(out["marker"]["files"], [])

    def test_cli_says_the_count_and_the_omission(self):
        text = cli._pending_text(_pending_payload(self._kernel([f"pkg-{i}" for i in range(35)])))
        self.assertIn("35 个包在等重启", text)
        self.assertIn("只列前 20 个", text)

    def test_cli_does_not_invent_an_omission(self):
        text = cli._pending_text(_pending_payload(self._kernel(["pkg-1", "pkg-2"])))
        self.assertIn("2 个包在等重启", text)
        self.assertNotIn("只列前", text)


def _pending_payload(kernel):
    """`_collect()` 那种自洽形态：三节 reloads 都读到了，counts 与 findings 同源。"""
    return {"counts": {"risk": 0, "warn": 0, "info": 0}, "kernel": kernel,
            "stale": {"ok": True, "scanned": 10, "procs_total": 10, "elapsed_ms": 5,
                      "procs_stale": 0, "unit_total": 0, "orphan_total": 0, "units": [],
                      "budget_hit": False},
            "reloads": {"nginx": {"ok": True, "pending": False, "conf_files": 3,
                                  "newest_file": "/etc/nginx/sites-available/x"},
                        "sshd": {"ok": True, "pending": False, "files": 1, "pid": 671},
                        "systemd": {"ok": True, "checked": 20, "need_reload": []}},
            "apt": {"ok": True, "last_start_date": "2026-10-10", "transactions": 2},
            "findings": [], "notes": ["严格只读。"], "uptime_sec": 60,
            "as_of": "2026-10-11 22:49:04", "cached": False}


# ---- 删异地目标的拒绝理由：挂着几条要说全 ----

class CloudTargetLinkedTotalTests(unittest.TestCase):
    def setUp(self):
        self.tid = 900000 + next(_seq)
        self.ids = [int(dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES(?,?,'manual',0,0,7,1,?)",
            ("sqlite", f"db{i}", self.tid))) for i in range(7)]
        self.addCleanup(self._wipe)

    def _wipe(self):
        marks = ",".join("?" for _ in self.ids)
        dbm.execute(f"DELETE FROM backups WHERE id IN ({marks})", self.ids)

    def test_seven_linked_tasks_report_seven(self):
        with self.assertRaises(RuntimeError) as ctx:
            CLOUD.delete_target(self.tid)
        msg = str(ctx.exception)
        self.assertIn("还有备份任务挂在这个目标上", msg)
        self.assertIn(f"#{self.ids[0]}", msg)
        self.assertIn("共 7 条，另 2 条未列出", msg)

    def test_three_linked_tasks_are_all_named(self):
        self.ids = self.ids[:3]
        dbm.execute("DELETE FROM backups WHERE cloud_target_id=? AND id NOT IN (?,?,?)",
                    (self.tid, *self.ids))
        with self.assertRaises(RuntimeError) as ctx:
            CLOUD.delete_target(self.tid)
        self.assertNotIn("未列出", str(ctx.exception))
        self.assertIn(f"#{self.ids[2]}", str(ctx.exception))


# ---- 源码契约：这几处不许再退回硬编码切片 ----

class SourceContractTests(unittest.TestCase):
    def _src(self, rel: str) -> str:
        return (BACKEND / rel).read_text(encoding="utf-8")

    def test_alerts_has_no_call_site_slices(self):
        src = self._src("app/services/alerts_service.py")
        self.assertNotIn("units[:20]", src)
        self.assertNotIn("detail[:120]", src)
        self.assertNotIn('[:500]', src)
        self.assertIn("meta=_meta(units=units)", src)
        self.assertIn("clip_text(str(message or \"\"), MESSAGE_LIMIT", src)

    def test_meta_budget_is_defined_once(self):
        src = self._src("app/services/alerts_service.py")
        self.assertEqual(src.count("META_LIMIT = "), 1)
        self.assertIn("json.dumps(kv, ensure_ascii=False)", src)

    def test_marker_pkg_budget_is_defined_once(self):
        src = self._src("app/services/pending_service.py")
        self.assertNotIn("if ln.strip()][:20]", src)
        self.assertIn("MARKER_PKGS_SHOWN = 20", src)
        self.assertIn('out["marker"]["files_total"] = len(pkgs)', src)

    def test_the_four_new_call_sites_share_the_same_reader(self):
        pairs = {
            "app/services/login_audit_service.py": "name_list(listed, 5)",
            "app/services/cert_ops.py": 'name_list([c["name"] for c in expiring], 3, sep=", ", unit="张")',
            "app/services/cloud_backup.py": 'for x in linked], 5, unit="条")',
            "app/services/alerts_service.py": "name_list(units, 10, sep=', ')",
        }
        for rel, needle in pairs.items():
            src = self._src(rel)
            self.assertIn(needle, src, f"{rel} 没有走 name_list 这份出处")
            header = src.split("def ", 1)[0]
            self.assertIn("name_list", header, f"{rel} 忘了从 util 导入 name_list")


if __name__ == "__main__":
    unittest.main()
