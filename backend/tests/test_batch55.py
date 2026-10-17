"""批次55A：备份任务的执行留痕、失败点名与告警。

备份任务是整机上唯一"出事还能重来"的那条线，而它跑在面板之外：systemd timer 的
`ExecStart=` 起一个独立进程，没有 API 调用方，也没有人替它记账。改动之前这张表只有
「计划是什么」，一条每晚都在 SystemExit 的任务在面板上照样显示"已启用"，只有翻
journalctl 才发现早就不产出了。这一批按危险面逐条钉住：

1. **留痕必须由 runner 自己写，且四路结果各归各位。** 成功=ok、`_fail()` 的
   SystemExit=fail+原因、子进程非零=exit N、其它异常=fail。SystemExit 是 BaseException，
   `except Exception` 接不住——漏掉它就漏掉了最常态的那一类失败。
2. **抢不到锁的跳过不是结论。** 它必须返回 0 **并且不写留痕**，否则一次跳过会把上一次
   真失败的原因盖成"刚刚也失败过"，或把成功盖成失败。
3. **原因文本走 redact_creds 并截到 RUN_NOTE_MAX。** 留痕会进告警正文、自检判词和
   `choyeonctl backup jobs`，一条 pg_dump 报错里的 `password=…` 就这样出接口了。
4. **从没跑过 ≠ 失败。** 空串是"没有结论"，既不能粉饰成绿灯，也不能报成故障。
5. **告警的去重与恢复由同一条留痕驱动。** 持续失败每轮重插会把历史表刷满；修好后不补
   一条「已恢复」，人就得登录面板确认现在到底有没有事（约定 5）。任务被删时它那两个
   标记跟着清，历史行不动。
6. **自检先点名再数份数，CLI 与网页读同一个读者。** 只要有一个读者各查各的列，
   「有 3 条失败」和点名的 2 条就会长期对不上。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import io
import itertools
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch55_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch55_bak_"))

import unittest

from app import backup_runner as R
from app import config, locks
from app import database as dbm
from app.routers import meta as meta_mod
from app.services import alerts_service as A
from app.services import backup_service as B
from cli import Out, build_parser, cmd_backup, cmd_schema

ROOT = Path(config.BASE)
_seq = itertools.count(1)
_CLEAN: list[int] = []


def _mkjob(kind: str = "app", target: str | None = None, **cols) -> int:
    """插一条备份任务并返回 id；用例结束统一删掉，不给下一轮留幽灵任务。"""
    fields = {"kind": kind, "target": target or f"ut{os.getpid()}-{next(_seq)}",
              "schedule": "daily", "enabled": 1}
    fields.update(cols)
    marks = ", ".join("?" for _ in fields)
    bid = dbm.execute(
        f"INSERT INTO backups({', '.join(fields)}) VALUES({marks})", tuple(fields.values()))
    _CLEAN.append(int(bid))
    return int(bid)


def _job(bid: int) -> dict:
    return dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))


def _rows(code: str) -> list[dict]:
    return dbm.query("SELECT * FROM alerts_log WHERE code=? ORDER BY id", (code,))


def _wipe_job(bid: int) -> None:
    code = f"backup:{bid}"
    dbm.execute("DELETE FROM alerts_log WHERE code=?", (code,))
    dbm.execute("DELETE FROM settings WHERE key=?", (f"sent:backup-{bid}",))
    dbm.execute("DELETE FROM settings WHERE key=?", (f"notified:{code}",))
    dbm.execute("DELETE FROM backups WHERE id=?", (bid,))


class _JobCase(unittest.TestCase):
    def setUp(self):
        del _CLEAN[:]

    def tearDown(self):
        for bid in _CLEAN:
            _wipe_job(bid)


# ------------------------------------------------ 1. runner 写留痕

class TestRunnerTrace(_JobCase):
    """`run_job` 的四路结果 + 跳过 + 留痕写失败。

    `main()` 与 `_push_offsite()` 都被替身接管：这里验的是"这一次执行怎么记账"，
    真的 tar/pg_dump 是 ㊸A、㊱A 那几批的事。
    """

    def _run(self, bid: int, main_impl, push_impl=lambda _row: None) -> int:
        with (mock.patch.object(R, "main", main_impl),
              mock.patch.object(R, "_push_offsite", push_impl),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            return R.run_job(bid)

    def test_success_writes_ok_and_clears_the_old_reason(self):
        bid = _mkjob(last_status="fail", last_note="上一轮的失败原因")
        self.assertEqual(self._run(bid, lambda _row: None), 0)
        row = _job(bid)
        self.assertEqual(row["last_status"], "ok")
        self.assertEqual(row["last_note"], "", "成功后必须清掉旧原因，否则界面还在说失败")
        self.assertRegex(str(row["last_run"]), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_fail_via_systemexit_records_the_reason(self):
        """`_fail()` 抛的是 SystemExit——以前它穿出 `__main__`，留痕正漏在最常态的失败上。"""
        bid = _mkjob()
        self.assertEqual(self._run(bid, lambda _row: R._fail("pg_dump", 1)), 1)
        row = _job(bid)
        self.assertEqual(row["last_status"], "fail")
        self.assertIn("pg_dump", row["last_note"])

    def test_systemexit_with_int_code_keeps_the_exit_code(self):
        bid = _mkjob()

        def boom(_row):
            raise SystemExit(3)

        self.assertEqual(self._run(bid, boom), 3)
        self.assertEqual(_job(bid)["last_status"], "fail")

    def test_called_process_error_records_exit_n(self):
        bid = _mkjob()

        def boom(_row):
            raise subprocess.CalledProcessError(124, ["pg_dump"])

        self.assertEqual(self._run(bid, boom), 1)
        row = _job(bid)
        self.assertEqual(row["last_status"], "exit 124", "超时和崩了要在状态里分得出来")

    def test_unexpected_exception_records_fail(self):
        bid = _mkjob()

        def boom(_row):
            raise OSError(28, "No space left on device")

        self.assertEqual(self._run(bid, boom), 1)
        row = _job(bid)
        self.assertEqual(row["last_status"], "fail")
        self.assertIn("No space left", row["last_note"])

    def test_lock_skip_returns_zero_and_leaves_the_trace_alone(self):
        """跳过不是这一轮的结论：写留痕会把上一次真失败的原因盖掉。"""
        bid = _mkjob(last_status="exit 1", last_note="pg_dump 退出 1")
        guard = locks.acquire(locks.key_backup(bid), "备份任务", "system")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(R.run_job(bid), 0)
        finally:
            guard.release()
        row = _job(bid)
        self.assertEqual(row["last_status"], "exit 1")
        self.assertEqual(row["last_note"], "pg_dump 退出 1")

    def test_note_is_redacted_and_capped(self):
        bid = _mkjob()

        def boom(_row):
            raise RuntimeError("连接被拒 password=hunter2secret " + "详情" * 400)

        self._run(bid, boom)
        note = _job(bid)["last_note"]
        self.assertNotIn("hunter2secret", note)
        self.assertIn("***", note)
        self.assertLessEqual(len(note), R.RUN_NOTE_MAX)

    def test_trace_write_failure_does_not_rejudge_a_good_backup(self):
        """留痕写不进去只是记不成账，不能把一次成功的备份改判成失败。"""
        bid = _mkjob()
        with (mock.patch.object(R, "main", lambda _row: None),
              mock.patch.object(R, "_push_offsite", lambda _row: None),
              mock.patch.object(R.dbm, "execute", side_effect=RuntimeError("库锁")),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(R.run_job(bid), 0)

    def test_missing_job_is_still_exit_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.run_job(999999), 2)


# ------------------------------------------------ 2. 一个读者：巡检/自检/CLI 共用

class TestRunReaders(_JobCase):
    def test_run_label_reads_like_the_web_page(self):
        """类型名只有一个出处：同一条任务在邮件里和页面上必须叫同一个名字。"""
        self.assertEqual(B.run_label({"kind": "pg", "target": "all"}), "PG 备份 all")
        self.assertEqual(B.run_label({"kind": "app", "target": "rosetta"}), "应用备份 rosetta")
        # sqlite 的 target 存的是 apps.id，判词里必须换成应用名——邮件里写 "#3" 没人对得上号
        self.assertEqual(B.run_label({"kind": "sqlite", "target": "424242"}),
                         f"SQLite 备份 {B._sqlite_target_label('424242')}")
        self.assertEqual(B.run_label({"kind": "", "target": "x"}), "? x")

    def test_is_failed_run_needs_an_attempt(self):
        self.assertFalse(B.is_failed_run({"last_status": ""}), "从没跑过不是故障")
        self.assertFalse(B.is_failed_run({"last_status": "ok"}))
        self.assertTrue(B.is_failed_run({"last_status": "fail"}))
        self.assertTrue(B.is_failed_run({"last_status": "exit 2"}))

    def test_scheduled_runs_skips_disabled_and_manual(self):
        listed = _mkjob(last_status="exit 1")
        disabled = _mkjob(enabled=0, last_status="fail")
        manual = _mkjob(schedule="manual", last_status="fail")
        rows = B.scheduled_runs()
        ids = {r["id"] for r in rows}
        self.assertIn(listed, ids)
        self.assertNotIn(disabled, ids, "停用的任务没有「这一轮该跑没跑」")
        self.assertNotIn(manual, ids, "手动任务由点它的人负责")
        self.assertEqual({r["label"] for r in rows if r["id"] == listed},
                         {B.run_label(_job(listed))})
        # 整轮 pytest 共用一个库，只验"我的这几条在不在"，不验集合相等
        fails = {t["id"] for t in B.failing_tasks()}
        self.assertIn(listed, fails)
        self.assertNotIn(disabled, fails)
        self.assertNotIn(manual, fails)


# ------------------------------------------------ 3. 告警：去重、点名、恢复

class TestBackupAlerts(_JobCase):
    def _fail_row(self, bid: int) -> dict:
        return {"id": bid, "kind": "app", "target": "ut-app", "schedule": "daily",
                "enabled": 1, "last_run": "2026-10-17 03:30:00",
                "last_status": "exit 1", "last_note": "tar 退出 1",
                "label": B.run_label({"kind": "app", "target": "ut-app"})}

    def _run_checks(self, rows, sent):
        async def spy_notify(text):
            sent.append(text)
            return True

        with (mock.patch.object(A, "scheduled_runs", lambda: rows),
              mock.patch.object(A, "notify", spy_notify)):
            return asyncio.run(A.run_backup_checks())

    def test_failure_records_crit_once_and_recovers_after(self):
        bid = _mkjob()
        code = f"backup:{bid}"
        sent: list[str] = []
        results = self._run_checks([self._fail_row(bid)], sent)
        rows = _rows(code)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["level"], "crit")
        self.assertEqual(results, [{"id": bid, "label": "应用备份 ut-app", "status": "exit 1"}])
        self.assertEqual(len(sent), 1)
        self.assertIn("exit 1", sent[0])
        self.assertIn("tar 退出 1", sent[0], "推送里要带上原因，不然还得登面板翻日志")

        self._run_checks([self._fail_row(bid)], sent)
        self.assertEqual(len(_rows(code)), 1, "持续中的告警不得每轮重插")
        self.assertEqual(len(sent), 1, "sent: 标记当天有效")

        good = {**self._fail_row(bid), "last_status": "ok", "last_note": ""}
        self._run_checks([good], sent)
        latest = _rows(code)
        self.assertEqual(latest[-1]["level"], "ok")
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[-1].startswith("已恢复"), sent[-1])
        self.assertEqual(dbm.get_setting("notified:" + code), "", "恢复后标记要被清掉")

    def test_history_message_is_stable_and_numbers_live_in_meta(self):
        """历史行是去重键：带上每轮都在变的时间就永远去不了重（约定 4）。"""
        bid = _mkjob()
        code = f"backup:{bid}"
        self._run_checks([self._fail_row(bid)], [])
        row = _rows(code)[0]
        self.assertEqual(row["message"], "备份任务 应用备份 ut-app 执行失败（状态 exit 1）")
        self.assertNotIn("2026-10-17", row["message"], "时间只进 meta 和推送，不进历史正文")
        self.assertIn("2026-10-17", row["meta"])

    def test_never_run_task_stays_silent(self):
        bid = _mkjob()
        row = {**self._fail_row(bid), "last_status": "", "last_note": ""}
        sent: list[str] = []
        self.assertEqual(self._run_checks([row], sent), [])
        self.assertEqual(_rows(f"backup:{bid}"), [], "刚建好的计划不是故障")
        self.assertEqual(sent, [])

    def test_reader_failure_does_not_look_like_a_green_light(self):
        """读不到留痕就跳过巡检，不能报"一切正常"（批次⑰A）。"""
        with mock.patch.object(A, "scheduled_runs", side_effect=RuntimeError("库坏了")):
            self.assertEqual(asyncio.run(A.run_backup_checks()), [])

    def test_deleting_a_job_clears_its_alert_markers(self):
        """任务删掉后 sent:/notified: 不能再留在 settings 里（与 forget_app 同一口径）。

        编号是 AUTOINCREMENT，新任务不会复用 `backup:<id>`，所以这里没有"同名继承"那一档
        后果，攒下来的只是没人读的死行——但探活标记当年也正是这么攒出来的。
        """
        bid = _mkjob()
        code = f"backup:{bid}"
        self._run_checks([self._fail_row(bid)], [])
        self.assertTrue(dbm.get_setting(f"sent:backup-{bid}"))
        self.assertTrue(dbm.get_setting(f"notified:{code}"))
        A.forget_backup(bid)
        self.assertFalse(dbm.get_setting(f"sent:backup-{bid}"))
        self.assertFalse(dbm.get_setting(f"notified:{code}"))
        self.assertEqual(len(_rows(code)), 1, "历史行不跟着删（约定 4）")
        # 光测函数本身不够：service 里漏掉这一句，上面全绿而线上照样攒死行
        self.assertIn("alerts_service.forget_backup", inspect.getsource(B.delete_backup),
                      "delete_backup 不再清理告警标记了")


# ------------------------------------------------ 4. 自检项：先点名再数份数

class TestDoctorItem(_JobCase):
    def _check(self, failing, recent=(3, None)):
        with (mock.patch.object(B, "failing_tasks", lambda: failing),
              mock.patch.object(meta_mod, "_recent_backups", lambda: recent)):
            return asyncio.run(meta_mod._check_backup())

    def test_failing_task_is_a_fail_even_when_archives_exist(self):
        """别的任务照样产出归档，只看"最新一份几天前"就会报 pass。"""
        out = self._check([{"id": 1, "label": "应用备份 rosetta", "last_status": "fail",
                            "last_run": "2026-10-17 03:30:00", "last_note": "磁盘满"}],
                          recent=(9, time.time()))
        self.assertEqual(out["status"], "fail")
        self.assertIn("1 条定时备份任务最近一次执行失败", out["detail"])
        self.assertIn("应用备份 rosetta（fail，2026-10-17 03:30:00）", out["detail"])
        self.assertIn("choyeonctl backup jobs", out["fix"], "fix 必须指向真存在的子命令")

    def test_list_is_capped_but_still_counts_every_task(self):
        """判词「数与点名」同源（批次㉔）：点数与点名的条目要能对上。"""
        failing = [{"id": i, "label": f"应用备份 a{i}", "last_status": "fail",
                    "last_run": "", "last_note": ""} for i in range(1, 8)]
        out = self._check(failing)
        self.assertIn("7 条", out["detail"])
        self.assertIn("另有 2 条", out["detail"])
        self.assertEqual(out["detail"].count("应用备份"), 5)

    def test_no_failure_falls_through_to_the_old_checks(self):
        self.assertEqual(self._check([], recent=(0, None))["status"], "warn")
        self.assertEqual(self._check([], recent=(3, time.time()))["status"], "pass")


# ------------------------------------------------ 5. CLI 与契约

class TestCliJobs(unittest.TestCase):
    def setUp(self):
        del _CLEAN[:]

    def tearDown(self):
        for bid in _CLEAN:
            dbm.execute("DELETE FROM backups WHERE id=?", (bid,))

    def _run(self, argv, json_mode=True):
        args = build_parser().parse_args(argv)
        buf = io.StringIO()
        with mock.patch.object(Out, "json_mode", json_mode), \
             contextlib.redirect_stdout(buf):
            return asyncio.run(cmd_backup(args)), buf.getvalue()

    def test_jobs_shows_the_same_trace_the_web_page_reads(self):
        bid = _mkjob(kind="app", target="ut-cli", last_status="exit 1",
                     last_run="2026-10-17 03:30:00", last_note="tar 退出 1")
        out, _ = self._run(["backup", "jobs"])
        job = next(j for j in out["jobs"] if j["id"] == bid)
        self.assertEqual(job["last_status"], "exit 1")
        self.assertEqual(job["label"], "应用备份 ut-cli")
        self.assertIn("tar 退出 1", job["last_note"])
        self.assertIn(bid, [j["id"] for j in out["failed"]])

    def test_never_run_task_is_listed_as_unexecuted_not_failed(self):
        bid = _mkjob(kind="app", target="ut-cli2")
        out, _ = self._run(["backup", "jobs"])
        job = next(j for j in out["jobs"] if j["id"] == bid)
        self.assertEqual(job["last_status"], "未执行")

    def test_human_output_names_the_reason(self):
        bid = _mkjob(kind="app", target="ut-cli3", last_status="fail",
                     last_run="2026-10-17 03:30:00", last_note="目标应用目录不存在")
        out, text = self._run(["backup", "jobs"], json_mode=False)
        self.assertIn("应用备份 ut-cli3", text)
        self.assertIn("目标应用目录不存在", text)
        self.assertIn(str(bid), text)

    def test_schema_and_argparse_and_agents_doc_agree_on_jobs(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cmds = asyncio.run(cmd_schema(argparse.Namespace()))["commands"]
        entry = next(c for c in cmds if c["name"].startswith("backup "))
        self.assertIn("jobs", entry["name"].split("|"))
        build_parser().parse_args(["backup", "jobs"])
        # AGENTS.md 里备份那一行的命令名是 `backup run|list|jobs|…`，不是单独的 `backup`
        row = [ln for ln in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
               if ln.startswith("| `backup ")]
        self.assertEqual(len(row), 1, row)
        self.assertIn("jobs", row[0], "AGENTS.md 的备份行必须写上新子命令")
        for col in ("last_run", "last_status", "last_note"):
            self.assertIn(col, row[0], f"契约里要点名留给智能体的列：{col}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
