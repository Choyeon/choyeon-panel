"""批次106：systemctl 没答上来时，定时器状态不许被写成「未挂表」，执行记录不许被写成「未执行」。

`_timer_states` / `_unit_results` 原本只看 `systemctl show` 的输出、完全丢掉退出码。没有 systemd、
dbus 连不上、非 root、探测超时——这些情况输出是一片空白，解析结果与「查过了，unit 存在但没挂」
一模一样：启用中的任务被渲染成「未挂表」，上次执行长期显示「未执行」，而定时器可能天天在跑。
面板替 systemd 说了一句它根本没说的话（批次㊷A 在防火墙那边修过同一类，批次101/105 立过规矩）。

修法：退出码非 0 时两份读表都不采信，把原因交给调用方收集，行上带 `systemd_unknown`；
界面用第三态「未读到」而不是二选一。unit 真的不存在时 systemctl 仍返回 0，那是一次成功的查询，
照旧按「没挂表」显示。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 只删自己建的那份目录（批次105 的教训：run_tests.py 的 CP_DATA_DIR 是整轮共用的）
_OWN_DATA_DIR = "CP_DATA_DIR" not in os.environ
if _OWN_DATA_DIR:
    os.environ["CP_DATA_DIR"] = tempfile.mkdtemp(prefix="cp_b106_data_")
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_b106_roots_"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as dbm  # noqa: E402
from app.errors import BadInput  # noqa: E402
from app.services import cron_service as C  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
CRON_PY = (SRC / "backend" / "app" / "services" / "cron_service.py").read_text(encoding="utf-8")
CRON_ROUTE = (SRC / "backend" / "app" / "routers" / "cron.py").read_text(encoding="utf-8")
UI_TS = (SRC / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
VUE = (SRC / "web" / "src" / "views" / "CronTasks.vue").read_text(encoding="utf-8")

_seq = iter(range(106000, 106999))
IDS: list[int] = []

BLIND_OUT = ("System has not been booted with systemd as init system (PID 1). Can't operate.\n"
             "Failed to connect to bus: Host is down")


def _dispatcher(states: str = "", results: str = "", journal: str = "",
                code: int = 0, journal_code: int = 0):
    """按 systemctl/journalctl 分派的 run() 替身，绝不在真实主机上执行。"""

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        if cmd == "journalctl":
            return {"code": journal_code, "out": journal}
        if cmd == "systemctl" and args and args[0] == "show":
            if any(str(a).endswith(".timer") for a in args):
                return {"code": code, "out": states if code == 0 else BLIND_OUT}
            return {"code": code, "out": results if code == 0 else BLIND_OUT}
        return {"code": code, "out": ""}

    return run


def _mk(name: str, last_run: str = "", last_status: str = "", log_tail: str = "旧输出") -> int:
    row = {"name": name, "expr": "0 3 * * *", "argv": json.dumps(["choyeonctl", "doctor"]),
           "enabled": 1, "last_run": last_run, "last_status": last_status,
           "log_tail": log_tail, "created_at": ""}
    cols = list(row)
    tid = dbm.execute(f"INSERT INTO cron_task({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                      tuple(row.values()))
    IDS.append(tid)
    return tid


def tearDownModule():
    for tid in IDS:
        dbm.execute("DELETE FROM cron_task WHERE id=?", (tid,))
    if _OWN_DATA_DIR:
        import shutil
        shutil.rmtree(os.environ["CP_DATA_DIR"], ignore_errors=True)


class TestExitCodeIsNotANegativeProof(unittest.TestCase):
    def test_timer_states_blind_returns_nothing_and_says_why(self):
        sink: list[str] = []
        with mock.patch.object(C, "run", _dispatcher(code=1)):
            got = asyncio.run(C._timer_states(["panel-cron-1.timer"], sink))
        self.assertEqual(got, {})
        self.assertEqual(len(sink), 1, "读不到要说一次，不能静默返回空表")
        self.assertIn("退出码 1", sink[0])
        self.assertIn("Failed to connect to bus", sink[0], "只说「读不到」等于没告诉人去哪查")

    def test_no_sink_still_works_for_one_arg_callers(self):
        # 老调用点/替身可以只传一个参数：不许 TypeError，也不许因此把「读不到」当成「查过了」
        # （漏掉的那一处恰好是静默跳过的那节，所以这一条必须断言返回值，不能只断言不报错）。
        with mock.patch.object(C, "run", _dispatcher(code=1)):
            self.assertEqual(asyncio.run(C._timer_states(["panel-cron-1.timer"])), {})
            self.assertEqual(asyncio.run(C._unit_results(["panel-cron-1"])), {})

    def test_readable_round_leaves_the_sink_alone(self):
        out = ("Id=panel-cron-1.timer\nActiveState=active\nUnitFileState=enabled\n"
               "NextElapseUSecRealtime=Sat 2026-10-03 03:30:00 CST\n\n")
        sink: list[str] = []
        with mock.patch.object(C, "run", _dispatcher(states=out)):
            got = asyncio.run(C._timer_states(["panel-cron-1.timer"], sink))
        self.assertTrue(got["panel-cron-1.timer"]["active"])
        self.assertEqual(sink, [], "读到了还记一笔「读不到」，第三态就成了新的谎")

    def test_blind_output_is_not_parsed_even_when_it_looks_like_a_block(self):
        # 超时（124）时子进程可能已经吐出半张表；半张表不能当成完整答案。
        half = "Id=panel-cron-1.timer\nActiveState=active\n"

        async def run(cmd, args, cwd=None, timeout=120, env=None):
            return {"code": 124, "out": half}

        sink: list[str] = []
        with mock.patch.object(C, "run", run):
            got = asyncio.run(C._timer_states(["panel-cron-1.timer"], sink))
        self.assertEqual(got, {})
        self.assertIn("退出码 124", sink[0])

    def test_unit_results_blind_same_shape(self):
        sink: list[str] = []
        with mock.patch.object(C, "run", _dispatcher(code=1)):
            got = asyncio.run(C._unit_results(["panel-cron-7"], sink))
        self.assertEqual(got, {})
        self.assertIn("退出码 1", sink[0])

    def test_nothing_asked_nothings_reported(self):
        # 一条任务都没有时不该冒出「读不到」：那压根没问 systemd。
        async def boom(*a, **k):
            raise AssertionError("units 为空不该调用 systemctl")

        sink: list[str] = []
        with mock.patch.object(C, "run", boom):
            self.assertEqual(asyncio.run(C._timer_states([], sink)), {})
            self.assertEqual(asyncio.run(C._unit_results([], sink)), {})
        self.assertEqual(sink, [])


class TestRowsCarryTheReason(unittest.TestCase):
    def test_blind_round_marks_every_row(self):
        tid = _mk(f"b106-blind-{os.getpid()}-{next(_seq)}")
        with mock.patch.object(C, "run", _dispatcher(code=1)):
            items = asyncio.run(C.list_tasks())
        hit = next(i for i in items if i["id"] == tid)
        self.assertIn("systemd_unknown", hit)
        self.assertIn("退出码 1", hit["systemd_unknown"])
        self.assertIn("systemctl", hit["systemd_unknown"])

    def test_readable_round_invents_no_unknown(self):
        tid = _mk(f"b106-ok-{os.getpid()}-{next(_seq)}")
        blocks = (f"Id=panel-cron-{tid}.timer\nActiveState=waiting\nUnitFileState=enabled\n"
                  "NextElapseUSecRealtime=n/a\n\n")
        with mock.patch.object(C, "run", _dispatcher(states=blocks)):
            items = asyncio.run(C.list_tasks())
        hit = next(i for i in items if i["id"] == tid)
        self.assertNotIn("systemd_unknown", hit, "读到了就别挂第三态，否则「未读到」永远擦不干净")
        self.assertFalse(hit["timer_active"])

    def test_two_helpers_failing_say_the_reason_once(self):
        tid = _mk(f"b106-dedupe-{os.getpid()}-{next(_seq)}")
        with mock.patch.object(C, "run", _dispatcher(code=1)):
            items = asyncio.run(C.list_tasks())
        hit = next(i for i in items if i["id"] == tid)
        self.assertEqual(hit["systemd_unknown"].count("退出码 1"), 1,
                         "同一个原因在定时器列与执行记录列各冒一次，会被读成两件事")


class TestRunLogAndRunNow(unittest.TestCase):
    def test_stored_fallback_says_which_side_was_not_read(self):
        tid = _mk(f"b106-log-{os.getpid()}-{next(_seq)}", last_run="2026-10-11 03:00:00", last_status="exit 1")
        with mock.patch.object(C, "run", _dispatcher(code=1)):
            got = asyncio.run(C.run_log(tid))
        self.assertEqual(got["source"], "stored")
        self.assertIn("退出码", got["systemd_unknown"],
                      "退回面板那一份是「退」，不说就成了「这台机器只跑过一次」")
        self.assertEqual(got["status"], "exit 1")

    def test_readable_run_log_keeps_the_key_absent(self):
        tid = _mk(f"b106-log2-{os.getpid()}-{next(_seq)}")
        blocks = (f"Id=panel-cron-{tid}.service\nActiveState=inactive\nResult=success\n"
                  f"ExecMainStatus=0\nExecMainStartTimestamp=Sun 2026-10-11 03:00:05 CST\n\n")
        with mock.patch.object(C, "run", _dispatcher(results=blocks, journal="-- 定时器那一轮\n")):
            got = asyncio.run(C.run_log(tid))
        self.assertNotIn("systemd_unknown", got)
        self.assertEqual(got["source"], "journal")

    def test_run_now_reports_the_overlap_gate_was_not_verified(self):
        tid = _mk(f"b106-run-{os.getpid()}-{next(_seq)}")

        async def fake_exec(t, row, argv, actor):
            return {"status": "ok", "log": "done"}

        with mock.patch.object(C, "run", _dispatcher(code=1)), \
                mock.patch.object(C, "_exec_task", fake_exec):
            out = asyncio.run(C.run_now(tid))
        self.assertEqual(out["status"], "ok")
        self.assertIn("退出码", out["systemd_unknown"],
                      "文件锁只挡面板自己发起的并发，timer 那一轮有没有在跑这句话没有证据")

    def test_run_now_still_blocks_a_visible_timer_round(self):
        tid = _mk(f"b106-block-{os.getpid()}-{next(_seq)}")
        blocks = (f"Id=panel-cron-{tid}.service\nActiveState=activating\nResult=success\n"
                  "ExecMainStatus=0\n\n")

        async def never(*a, **k):
            raise AssertionError("重叠时不该执行")

        with mock.patch.object(C, "run", _dispatcher(results=blocks)), \
                mock.patch.object(C, "_exec_task", never):
            with self.assertRaises(BadInput) as cm:
                asyncio.run(C.run_now(tid))
        self.assertIn("正在执行", str(cm.exception))


class TestNoSilentBranchesLeft(unittest.TestCase):
    def test_show_readers_check_the_exit_code(self):
        for fn in ("_unit_results", "_timer_states"):
            body = CRON_PY.split(f"async def {fn}", 1)[1].split("\nasync def ", 1)[0]
            self.assertIn('r["code"] != 0', body, f"{fn} 又把退出码丢了")

    def test_list_tasks_passes_both_sinks(self):
        body = CRON_PY.split("async def list_tasks", 1)[1].split("\n\n\n", 1)[0]
        self.assertEqual(body.count("unread"), 4, "两份读表都要交账，漏一处就是漏一个静默分支")
        self.assertIn("systemd_unknown", body)

    def test_route_does_not_decorate_the_rows(self):
        self.assertNotIn("systemd_unknown", CRON_ROUTE, "第三态由服务层给，路由只搬运")

    def test_stale_docstring_is_gone(self):
        self.assertNotIn("unit 不存在或本机没有 systemd 时按未知处理", CRON_PY,
                         "文档写着「按未知处理」而代码当成「没挂表」，是最难查的那种不一致")

    def test_unread_helper_reports_to_journal_and_sink(self):
        body = CRON_PY.split("def _show_unread", 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("warn(", body, "面板日志里也得留一句，界面只装得下短版本")
        self.assertIn("sink.append", body)


class TestFrontendContract(unittest.TestCase):
    def test_phrase_has_one_owner(self):
        self.assertEqual(UI_TS.count("export function unknownNote"), 1)
        self.assertEqual(UI_TS.count("export const UNKNOWN_LABEL"), 1)

    def test_timer_column_asks_unknown_first(self):
        cell = VUE.split("title: 'Timer'", 1)[1].split("{ title: '上次执行'", 1)[0]
        # 比的是渲染里那句三元，不是上面那段说明注释——注释里也写着「已挂表」。
        self.assertLess(cell.index("r.systemd_unknown"), cell.index("r.timer_active ? '已挂表'"),
                        "先判第三态，否则「未读到」永远走不到那三个分支")
        self.assertIn("UNKNOWN_LABEL", cell)
        self.assertIn("st warn", cell, "第三态要有自己的颜色档，和「没挂表」的灰点区分开")

    def test_last_run_cell_does_not_claim_never_ran(self):
        self.assertIn("? UNKNOWN_LABEL : '未执行'", VUE)

    def test_drawer_warns_about_the_side_it_could_not_read(self):
        self.assertIn('v-if="logData?.systemd_unknown"', VUE)
        head = VUE.split("label: '开始时间'", 1)[1][:240]
        self.assertIn("? UNKNOWN_LABEL : '未执行'", head,
                      "抽屉里的开始时间也要有第三态，不能停在「未执行」")

    def test_manual_run_toast_says_the_gate_was_not_checked(self):
        body = VUE.split("async function runNow", 1)[1].split("\nasync function openLog", 1)[0]
        self.assertIn("unknownNote(", body)
        self.assertIn("msg.warning", body, "重叠闸没验证过时不许只报成功")

    def test_short_label_is_not_hardcoded_in_the_page(self):
        self.assertNotIn("未读到", VUE, "短名从 UNKNOWN_LABEL 来，别在页面里另写一份")

    def test_task_type_declares_the_field(self):
        block = VUE.split("type Task", 1)[1].split("type RunLog", 1)[0]
        self.assertIn("systemd_unknown?: string", block)


if __name__ == "__main__":
    unittest.main()
