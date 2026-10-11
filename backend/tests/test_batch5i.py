"""批次⑤I：定时任务"留痕 + 重叠 + 输出来源"与告警"恢复通知"。

覆盖的都是只有真 systemd 才会暴露、且线上已经发生过一次的坏法：
1. `systemctl show` 的属性顺序由 systemd 决定，实测 NextElapseUSecRealtime 排在 Id
   之前；按"看到 Id 才开始收属性"写就整列丢掉下次执行时间（网页上红过）。
2. 被 timer 唤醒的那一轮不写 cron_task.last_run：不读 systemd 的话界面长期显示
   "未执行"，而定时器其实天天在跑；输出也只在 journald 里。
3. 手动执行与定时器重叠会写出两份备份；两个管理员同时点也是。
4. 告警只有"出事"没有"已恢复"，收到通知的人只能自己登录面板确认。
5. 盘上残留没有主人的 unit = 一个既看不见也关不掉的定时器（本机残留过一对，
   每 5 分钟真的跑一次 certbot renew）。
6. Type=oneshot 执行中 ActiveState 是 **activating**（不是 active），而此时
   ExecMainStatus 已经是 0：只认 active 时重叠闸永不触发，照读退出码还会把
   上一次真正的失败盖成 ok。两条都是线上探针实测出来的。
7. 执行中就把任务删掉，unit 会以 failed 收尾，而 failed 记录不随文件删除消失，
   之后每轮告警都指着一个不存在的服务报 crit。
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b5i_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch5i_roots_"))

import unittest
from unittest import mock

from starlette.testclient import TestClient

from app import config, main, security
from app import database as dbm
from app.errors import BadInput, NotFound
from app.routers import meta as META
from app.services import alerts_service as A
from app.services import cron_service as C

_seq = iter(range(700000, 999999))


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _drop_task(tid: int):
    dbm.execute("DELETE FROM cron_task WHERE id=?", (tid,))


def _mk_task(name: str = "", expr: str = "0 3 * * *", argv=("choyeonctl", "doctor"),
             enabled=1, last_run=None, last_status="", log_tail="") -> dict:
    row = {"name": name or _uid("b5i"), "expr": expr, "argv": json.dumps(list(argv)),
           "enabled": enabled, "last_run": last_run, "last_status": last_status,
           "log_tail": log_tail, "created_at": ""}
    cols = list(row)
    tid = dbm.execute(f"INSERT INTO cron_task({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                      tuple(row.values()))
    return dbm.query_one("SELECT * FROM cron_task WHERE id=?", (tid,))


def _run_dispatcher(states: str = "", results: str = "", journal: str = "", journal_code: int = 0):
    """按 systemctl/journalctl 分派的 run() 替身，绝不在真实主机上执行。"""

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        if cmd == "journalctl":
            return {"code": journal_code, "out": journal}
        if cmd == "systemctl" and args and args[0] == "show":
            if any(str(a).endswith(".timer") for a in args):
                return {"code": 0, "out": states}
            return {"code": 0, "out": results}
        return {"code": 0, "out": ""}

    return run


# ------------------------------------------------------------------ show 解析
class TestShowParsing(unittest.TestCase):
    def test_properties_before_id_are_kept(self):
        # 真机上的顺序：NextElapseUSecRealtime / Result / ExecMainStartTimestamp 都在 Id 之前
        text = ("NextElapseUSecRealtime=Sun 2026-10-11 03:00:00 CST\n"
                "ActiveState=active\nId=panel-cron-7.timer\nUnitFileState=enabled\n"
                "\n"
                "NextElapseUSecRealtime=n/a\nActiveState=inactive\nId=panel-cron-8.timer\n")
        blocks = C._parse_show(text)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["NextElapseUSecRealtime"], "Sun 2026-10-11 03:00:00 CST")
        self.assertEqual(blocks[1]["Id"], "panel-cron-8.timer")

    def test_timer_states_next_elapse(self):
        states = ("NextElapseUSecRealtime=Sun 2026-10-11 03:00:00 CST\nActiveState=active\n"
                  "UnitFileState=enabled\nId=panel-cron-7.timer\n")
        with mock.patch.object(C, "run", _run_dispatcher(states=states)):
            got = asyncio.run(C._timer_states(["panel-cron-7.timer"]))
        st = got["panel-cron-7.timer"]
        self.assertTrue(st["active"])
        self.assertEqual(st["enabled"], "enabled")
        self.assertEqual(st["next"], "Sun 2026-10-11 03:00:00 CST")

    def test_n_a_and_infinity_are_not_next_times(self):
        states = ("NextElapseUSecRealtime=infinity\nId=panel-cron-1.timer\n"
                  "\nNextElapseUSecRealtime=n/a\nId=panel-cron-2.timer\n")
        with mock.patch.object(C, "run", _run_dispatcher(states=states)):
            got = asyncio.run(C._timer_states(["panel-cron-1.timer", "panel-cron-2.timer"]))
        self.assertEqual(got["panel-cron-1.timer"]["next"], "")
        self.assertEqual(got["panel-cron-2.timer"]["next"], "")

    def test_local_ts_drops_weekday_and_tz(self):
        self.assertEqual(C._local_ts("Sun 2026-10-11 03:30:10 CST"), "2026-10-11 03:30:10")
        self.assertEqual(C._local_ts("2026-10-11 03:30:10"), "2026-10-11 03:30:10")
        self.assertEqual(C._local_ts(""), "")
        self.assertEqual(C._local_ts("n/a"), "")

    def test_exec_result_vocabulary_matches_run_now(self):
        ok = {"ExecMainStartTimestamp": "Sun 2026-10-11 03:00:00 CST", "ExecMainStatus": "0", "Result": "success"}
        self.assertEqual(C._exec_result(ok), "ok")
        never = {"ExecMainStartTimestamp": "", "ExecMainStatus": "0", "Result": "success"}
        self.assertEqual(C._exec_result(never), "", "从没跑过不能报成 ok")
        to = {"ExecMainStartTimestamp": "Sun 2026-10-11 03:00:00 CST", "ExecMainStatus": "-", "Result": "timeout"}
        self.assertEqual(C._exec_result(to), "timeout")
        bad = {"ExecMainStartTimestamp": "Sun 2026-10-11 03:00:00 CST", "ExecMainStatus": "1", "Result": "exit-code"}
        self.assertEqual(C._exec_result(bad), "exit 1")

    def test_unit_results_maps_service_blocks(self):
        results = ("Result=success\nExecMainStatus=0\nActiveState=inactive\n"
                   "ExecMainStartTimestamp=Sun 2026-10-11 03:00:00 CST\nId=panel-cron-9.service\n"
                   "\nResult=success\nExecMainStatus=-\nActiveState=activating\n"
                   "ExecMainStartTimestamp=Sun 2026-10-11 04:00:00 CST\nId=panel-cron-10.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(results=results)):
            got = asyncio.run(C._unit_results(["panel-cron-9", "panel-cron-10"]))
        self.assertEqual(got["panel-cron-9"]["status"], "ok")
        self.assertFalse(got["panel-cron-9"]["running"])
        # 生产实测：Type=oneshot 执行中 ActiveState 是 activating（不是 active），
        # SubState=start。只认 active 时重叠闸永不触发，这条断言就是那件事的守门人。
        self.assertTrue(got["panel-cron-10"]["running"], "oneshot 正在跑时 ActiveState=activating")
        self.assertEqual(got["panel-cron-10"]["status"], "",
                         "还在跑的一轮没有结果，提前报 ok 会把上一次的失败盖成成功")

    def test_unit_results_treats_lingering_active_as_running(self):
        results = ("Result=success\nExecMainStatus=0\nActiveState=active\n"
                   "ExecMainStartTimestamp=Sun 2026-10-11 03:00:00 CST\nId=panel-cron-11.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(results=results)):
            got = asyncio.run(C._unit_results(["panel-cron-11"]))
        self.assertTrue(got["panel-cron-11"]["running"])
        self.assertEqual(got["panel-cron-11"]["status"], "")


# ------------------------------------------------------------------ 留痕合并
class TestMergeLastRun(unittest.TestCase):
    def test_timer_run_newer_than_stored_wins(self):
        item = {"last_run": "2026-09-01 03:00:00", "last_status": "ok"}
        C._merge_last_run(item, {"start": "2026-10-11 03:00:00", "status": "exit 1", "running": False})
        self.assertEqual(item["last_run"], "2026-10-11 03:00:00")
        self.assertEqual(item["last_status"], "exit 1")
        self.assertEqual(item["last_source"], "timer")

    def test_manual_run_stays_when_newer(self):
        item = {"last_run": "2026-10-11 09:00:00", "last_status": "ok"}
        C._merge_last_run(item, {"start": "2026-10-11 03:00:00", "status": "exit 1"})
        self.assertEqual(item["last_run"], "2026-10-11 09:00:00")
        self.assertEqual(item["last_source"], "manual")

    def test_never_run_by_systemd_leaves_row_alone(self):
        item = {"last_run": "", "last_status": ""}
        C._merge_last_run(item, {"start": "", "status": ""})
        self.assertEqual(item["last_run"], "")
        self.assertEqual(item["last_status"], "")
        self.assertFalse(item["running"])

    def test_running_flag_even_without_history(self):
        item = {"last_run": "", "last_status": ""}
        C._merge_last_run(item, {"start": "", "status": "", "running": True})
        self.assertTrue(item["running"])

    def test_running_round_does_not_masquerade_as_previous_result(self):
        # 上一次失败、这一轮正在跑：行上必须还是"执行中 + 上次 exit 1"，不能变成 ok
        item = {"last_run": "2026-10-10 03:00:00", "last_status": "exit 1"}
        C._merge_last_run(item, {"start": "2026-10-11 03:00:00", "status": "", "running": True})
        self.assertTrue(item["running"])
        self.assertEqual(item["last_run"], "2026-10-10 03:00:00")
        self.assertEqual(item["last_status"], "exit 1")


class TestListTaskProvenance(unittest.TestCase):
    def setUp(self):
        self.row = _mk_task(last_run="2026-09-01 03:00:00", last_status="ok")
        self.addCleanup(_drop_task, self.row["id"])

    def test_list_tasks_carries_timer_next_and_source(self):
        tid = self.row["id"]
        states = (f"NextElapseUSecRealtime=Sun 2026-10-11 03:00:00 CST\nActiveState=active\n"
                  f"UnitFileState=enabled\nId=panel-cron-{tid}.timer\n")
        results = (f"Result=exit-code\nExecMainStatus=2\nActiveState=inactive\n"
                   f"ExecMainStartTimestamp=Sun 2026-10-11 03:00:05 CST\nId=panel-cron-{tid}.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(states=states, results=results)):
            items = asyncio.run(C.list_tasks())
        me = next(i for i in items if i["id"] == tid)
        self.assertEqual(me["timer_next"], "Sun 2026-10-11 03:00:00 CST")
        self.assertEqual(me["last_run"], "2026-10-11 03:00:05")
        self.assertEqual(me["last_status"], "exit 2")
        self.assertEqual(me["last_source"], "timer")


# ------------------------------------------------------------------ 输出来源
class TestRunLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b5i_unit_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.object(config, "UNIT_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_task_is_404(self):
        with self.assertRaises(NotFound):
            asyncio.run(C.run_log(999999))

    def test_stored_when_never_run_by_timer(self):
        row = _mk_task(log_tail="克隆 https://u:S3cr3tT0ken@github.com/x.git 失败",
                       last_run=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.addCleanup(_drop_task, row["id"])
        with mock.patch.object(C, "run", _run_dispatcher(results="")):
            got = asyncio.run(C.run_log(row["id"]))
        self.assertEqual(got["source"], "stored")
        self.assertIn("克隆", got["log"])
        self.assertNotIn("S3cr3tT0ken", got["log"], "库里那份也要脱敏后才出去")

    def test_journal_when_timer_ran_after_manual(self):
        row = _mk_task(log_tail="旧的手动输出", last_run="2026-09-01 03:00:00")
        self.addCleanup(_drop_task, row["id"])
        results = ("Result=success\nExecMainStatus=0\nActiveState=inactive\n"
                   f"ExecMainStartTimestamp=Sun 2026-10-11 03:00:00 CST\nId=panel-cron-{row['id']}.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(
                results=results,
                journal="timer 这一轮\n推送 https://u:J0urnalSecret@github.com/x.git ok\n")):
            got = asyncio.run(C.run_log(row["id"]))
        self.assertEqual(got["source"], "journal")
        self.assertEqual(got["start"], "2026-10-11 03:00:00")
        self.assertIn("timer 这一轮", got["log"])
        self.assertNotIn("J0urnalSecret", got["log"])

    def test_manual_wins_over_older_journal(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        row = _mk_task(log_tail="刚刚手动跑的这份", last_run=now)
        self.addCleanup(_drop_task, row["id"])
        results = ("Result=success\nExecMainStatus=0\nActiveState=inactive\n"
                   "ExecMainStartTimestamp=Sun 2000-01-01 03:00:00 CST\n"
                   f"Id=panel-cron-{row['id']}.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(results=results, journal="二十年前的 timer\n")):
            got = asyncio.run(C.run_log(row["id"]))
        self.assertEqual(got["source"], "stored")
        self.assertIn("刚刚手动跑的这份", got["log"])

    def test_running_round_reads_journald_and_marks_running(self):
        row = _mk_task(log_tail="上一次的手动输出", last_run="2026-09-01 03:00:00", last_status="exit 1")
        self.addCleanup(_drop_task, row["id"])
        results = ("ExecMainStatus=-\nResult=success\nActiveState=activating\n"
                   f"ExecMainStartTimestamp=Sun 2026-10-11 03:00:00 CST\nId=panel-cron-{row['id']}.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(results=results, journal="这一轮还在跑\n")):
            got = asyncio.run(C.run_log(row["id"]))
        self.assertEqual(got["source"], "journal")
        self.assertTrue(got["running"])
        self.assertEqual(got["status"], "", "没跑完就没有状态可报")
        self.assertIn("这一轮还在跑", got["log"])

    def test_stored_branch_reports_not_running(self):
        row = _mk_task(log_tail="输出", last_run=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.addCleanup(_drop_task, row["id"])
        with mock.patch.object(C, "run", _run_dispatcher(results="")):
            got = asyncio.run(C.run_log(row["id"]))
        self.assertFalse(got["running"])

    def test_journal_failure_falls_back_to_stored(self):
        row = _mk_task(log_tail="库里的一份", last_run="2026-09-01 03:00:00")
        self.addCleanup(_drop_task, row["id"])
        results = ("ExecMainStatus=0\nResult=success\nActiveState=inactive\n"
                   "ExecMainStartTimestamp=Sun 2026-10-11 03:00:00 CST\n"
                   f"Id=panel-cron-{row['id']}.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(results=results, journal="", journal_code=1)):
            got = asyncio.run(C.run_log(row["id"]))
        self.assertEqual(got["source"], "stored")
        self.assertEqual(got["log"], "库里的一份")


# ------------------------------------------------------------------ 重叠执行
class TestRunNowOverlap(unittest.TestCase):
    def setUp(self):
        self.row = _mk_task()
        self.addCleanup(_drop_task, self.row["id"])
        self.lock = C._lock_path(self.row["id"])
        self.lock.parent.mkdir(parents=True, exist_ok=True)

    def test_refuses_while_timer_round_is_active(self):
        results = ("ExecMainStatus=-\nActiveState=activating\n"
                   f"ExecMainStartTimestamp=Sun 2026-10-11 03:00:00 CST\nId=panel-cron-{self.row['id']}.service\n")
        with mock.patch.object(C, "run", _run_dispatcher(results=results)), \
                mock.patch.object(C, "_resolve_binary", lambda n: "/usr/bin/true"), \
                mock.patch.object(asyncio, "create_subprocess_exec") as sub, self.assertRaises(BadInput) as ctx:
            asyncio.run(C.run_now(self.row["id"]))
        self.assertIn("定时器正在执行", str(ctx.exception))
        sub.assert_not_called()

    def test_refuses_when_lock_is_held(self):
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(C, "run", _run_dispatcher(results="")), \
                mock.patch.object(C, "_resolve_binary", lambda n: "/usr/bin/true"), self.assertRaises(BadInput) as ctx:
            asyncio.run(C.run_now(self.row["id"]))
        self.assertIn("正在执行中", str(ctx.exception))

    def test_lock_released_after_run(self):
        import fcntl

        async def fake_exec(tid, row, argv, actor):
            return {"code": 0, "status": "ok", "log": ""}

        with mock.patch.object(C, "run", _run_dispatcher(results="")), \
                mock.patch.object(C, "_exec_task", fake_exec):
            asyncio.run(C.run_now(self.row["id"]))
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fail("执行结束后锁没释放，之后每次手动执行都会被自己挡住")


class TestCleanupLeavesNoFailedUnit(unittest.TestCase):
    """停用/删除要停掉正在跑的那一轮，并在 systemd 里清干净。

    线上出现过：任务执行中就被删除，unit 文件在 ExecStart 期间消失，systemd 把该轮
    记成 failed，而 failed 记录不随文件删除而消失——之后每轮告警都报
    "存在 failed 状态服务: panel-cron-N.service"，指向一个已经不存在的任务。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b5i_clean_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.object(config, "UNIT_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

        async def run(cmd, args, cwd=None, timeout=120, env=None):
            self.calls.append([cmd, *args])
            return {"code": 0, "out": ""}

        p2 = mock.patch.object(C, "run", run)
        p2.start()
        self.addCleanup(p2.stop)

    def test_stop_and_reset_failed_are_issued(self):
        tid = 4242
        for ext in ("service", "timer"):
            Path(f"{self.dir}/panel-cron-{tid}.{ext}").write_text("[Unit]\n")
        asyncio.run(C._cleanup_units({"id": tid, "enabled": 1}))
        base = f"panel-cron-{tid}.service"
        self.assertIn(["systemctl", "stop", base], self.calls, "执行中的那轮必须先停")
        self.assertIn(["systemctl", "reset-failed", base], self.calls, "否则 failed 记录长期挂在 list-units 里")
        self.assertEqual(os.listdir(self.dir), [], "两份 unit 文件都要清掉")


class TestCronLogRoute(unittest.TestCase):
    """GET /api/cron/{id}/log 的鉴权与错误契约。"""

    def setUp(self):
        from fastapi import FastAPI, Request

        from app.routers.cron import router as cron_router

        api = FastAPI()
        self.role = {"v": "admin"}

        @api.middleware("http")
        async def _state(req: Request, call_next):
            req.state.cp_role = self.role["v"]
            req.state.cp_sub = "ut-b5i"
            return await call_next(req)

        api.include_router(cron_router)
        self.client = TestClient(api)

    def test_admin_gets_log(self):
        row = _mk_task(log_tail="输出内容")
        self.addCleanup(_drop_task, row["id"])
        with mock.patch.object(C, "run", _run_dispatcher(results="")):
            r = self.client.get(f"/api/cron/{row['id']}/log")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["log"], "输出内容")

    def test_viewer_forbidden(self):
        self.role["v"] = "viewer"
        row = _mk_task()
        self.addCleanup(_drop_task, row["id"])
        r = self.client.get(f"/api/cron/{row['id']}/log")
        self.assertEqual(r.status_code, 403, r.text)

    def test_unknown_id_is_404_and_non_numeric_400(self):
        r = self.client.get("/api/cron/987654/log")
        self.assertEqual(r.status_code, 404, r.text)
        r = self.client.get("/api/cron/abc/log")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertTrue(any("一" <= ch <= "鿿" for ch in r.json()["error"]))


# ------------------------------------------------------------------ doctor 对账
class TestTimerUnitsCheck(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b5i_units_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = mock.patch.object(config, "UNIT_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.task = _mk_task()
        self.addCleanup(_drop_task, self.task["id"])
        self.bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) VALUES('sqlite',1,'daily',3,0,7,1)",
            ())
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (self.bid,))
        # 默认两边都有 unit：对账通过
        self._write_unit(f"panel-cron-{self.task['id']}")
        self._write_unit(f"panel-backup-{self.bid}")

    def _write_unit(self, base: str, timer: bool = True, enable: bool = False):
        Path(self.dir, f"{base}.service").write_text("[Service]\n")
        if timer:
            Path(self.dir, f"{base}.timer").write_text("[Timer]\n")
        if enable:
            wants = Path(self.dir, "timers.target.wants")
            wants.mkdir(exist_ok=True)
            Path(wants, f"{base}.timer").write_text("")

    def test_aligned_is_pass(self):
        got = asyncio.run(META._check_timer_units())
        self.assertEqual(got["status"], "pass", got)

    def test_disabled_task_and_manual_backup_need_no_units(self):
        """禁用任务与 manual 备份本来就不该有 unit，不能被算成"单元缺失"。"""
        dbm.execute("UPDATE cron_task SET enabled=0 WHERE id=?", (self.task["id"],))
        dbm.execute("UPDATE backups SET schedule='manual' WHERE id=?", (self.bid,))
        Path(self.dir, f"panel-cron-{self.task['id']}.timer").unlink()
        Path(self.dir, f"panel-cron-{self.task['id']}.service").unlink()
        Path(self.dir, f"panel-backup-{self.bid}.timer").unlink()
        Path(self.dir, f"panel-backup-{self.bid}.service").unlink()
        got = asyncio.run(META._check_timer_units())
        self.assertEqual(got["status"], "pass", got["detail"])

    def test_orphan_unit_is_warn_with_removal_command(self):
        self._write_unit("panel-cron-424242", enable=True)
        got = asyncio.run(META._check_timer_units())
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("panel-cron-424242.timer", got["detail"])
        self.assertIn("仍在按计划执行", got["detail"])
        self.assertIn("systemctl disable --now panel-cron-424242.timer", got["fix"])

    def test_disabled_orphan_still_warns(self):
        """没 enable 的残留 unit 也要报：daemon-reload 之后它就等着被启用，且没人知道它是谁。"""
        self._write_unit("panel-cron-424243")
        got = asyncio.run(META._check_timer_units())
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("panel-cron-424243.timer", got["detail"])
        self.assertNotIn("仍在按计划执行", got["detail"])

    def test_missing_unit_is_warn(self):
        Path(self.dir, f"panel-cron-{self.task['id']}.timer").unlink()
        got = asyncio.run(META._check_timer_units())
        self.assertEqual(got["status"], "warn", got)
        self.assertIn(f"panel-cron-{self.task['id']}", got["detail"])

    def test_no_unit_dir_is_pass_not_crash(self):
        with mock.patch.object(config, "UNIT_DIR", str(Path(self.dir, "nope"))):
            got = asyncio.run(META._check_timer_units())
        self.assertEqual(got["status"], "pass", got)

    def test_registered_and_not_one_click_fixable(self):
        ids = [cid for cid, _, _ in META._CHECKS]
        self.assertIn("timer-units", ids)
        self.assertNotIn("timer-units", META.FIXABLE, "删 unit 要人确认归属，不能给按钮")


# ------------------------------------------------------------------ 恢复通知
class TestRecoveryNotify(unittest.TestCase):
    def setUp(self):
        self.code = _uid("rec")
        self.sent: list = []

        async def notify(text):
            self.sent.append(text)
            return True

        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (self.code,))
        self.addCleanup(lambda: [dbm.set_setting(k, "") for k in (f"notified:{self.code}", f"sent:{self.code}")])
        self.notify = notify

    def _check(self, trigger: bool, message: str, ok_message: str = ""):
        with mock.patch.object(A, "notify", self.notify):
            asyncio.run(A._check(self.code, trigger, message, code=self.code, level="crit",
                                 ok_message=ok_message))

    def test_recovery_announced_once_and_marker_cleared(self):
        self._check(True, "磁盘满了")
        self.assertEqual(self.sent, ["磁盘满了"])
        self.assertEqual(dbm.get_setting(f"notified:{self.code}"), A._today())
        self._check(False, "", ok_message="磁盘恢复正常")
        self.assertEqual(self.sent[-1], "已恢复：磁盘恢复正常")
        self.assertFalse(dbm.get_setting(f"notified:{self.code}"), "标记必须清掉，否则下一轮告警无法再通知恢复")
        before = len(self.sent)
        self._check(False, "", ok_message="磁盘恢复正常")
        self.assertEqual(len(self.sent), before, "同一条恢复不能重复推送")

    def test_recovery_without_alert_is_silent(self):
        """没发过告警的对象（一切正常）不该收到"已恢复"。"""
        self._check(False, "", ok_message="一切正常")
        self.assertEqual(self.sent, [])
        got = dbm.query_one("SELECT level FROM alerts_log WHERE code=? ORDER BY id DESC LIMIT 1", (self.code,))
        self.assertEqual(got["level"], "ok", "历史仍然要留下一行")

    def test_delivery_failure_does_not_claim_notified(self):
        async def failing(_text):
            return False

        self.notify = failing
        self._check(True, "证书过期")
        self.assertFalse(dbm.get_setting(f"notified:{self.code}"))
        self._check(False, "", ok_message="证书已续期")
        self.assertFalse(dbm.get_setting(f"notified:{self.code}"))

    def test_debounced_alert_sets_marker_only_when_pushed(self):
        sent: list = []

        async def notify(text):
            sent.append(text)
            return True

        code = _uid("deb")
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))
        self.addCleanup(lambda: [dbm.set_setting(k, "") for k in (f"notified:{code}", f"sent:{code}")])
        with mock.patch.object(A, "notify", notify):
            asyncio.run(A._check(code, True, "CPU 91%", code=code, ok_message="CPU 正常", debounce=2))
            self.assertEqual(sent, [], "第一轮只落历史，不推送")
            asyncio.run(A._check(code, True, "CPU 93%", code=code, ok_message="CPU 正常", debounce=2))
            self.assertEqual(len(sent), 1)
            self.assertEqual(dbm.get_setting(f"notified:{code}"), A._today())


# ------------------------------------------------------------------ 手动巡检留痕
class TestRunChecksAudit(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        user = _uid("auditu").replace("-", "_")
        self.token = security.sign_token(user, "admin")
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))

    def test_manual_check_run_is_audited(self):
        with mock.patch.object(A, "run_checks", mock.AsyncMock()) as ran:
            r = self.client.post("/api/settings/alerts/run-checks",
                                 headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200, r.text)
        ran.assert_awaited_once()
        row = dbm.query_one("SELECT username, action FROM audit WHERE action='settings:alert-checks' ORDER BY id DESC LIMIT 1")
        self.assertIsNotNone(row, "手动巡检会真的发通知，必须查得到是谁点的")


if __name__ == "__main__":
    unittest.main()
