"""cron_service / 定时任务路由 / 日志检索接口的测试。

覆盖的都是这套功能"出错就会很难看"的地方：
1. 表达式宽容解析（@daily、6 段、英文名）会被渲染成一个**错误的时间**，任务照跑、
   面板显示"已启用"——所以必须拒收，且拒绝理由要说人话。
2. cron 的「几号 or 星期几」是"或"语义，systemd OnCalendar 是"且"语义。
   照搬会把任务排到错误的日子，这里必须显式报错而不是猜。
3. 命令白名单：argv 会进 ExecStart 与 create_subprocess_exec。shell 元字符必须在
   **字符集**层面就挡掉，而不是拼好之后再检查。
4. 删除/禁用必须清掉 .service 与 .timer 两份文件并 daemon-reload。
5. run_now 的输出要截断（4KB）并脱敏——任务日志会进数据库、还会在网页上显示。

全程不碰真实 systemd：unit 目录指向 tmp，systemctl 与 subprocess 全部 mock。
"""

import asyncio
import json
import os
import shutil
import tempfile
from urllib.parse import urlencode

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_cron_ut_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from app import config, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.errors import BadInput, NotFound  # noqa: E402
from app.routers import system as SYS  # noqa: E402
from app.services import cron_service as C  # noqa: E402
from app.util import CLI_LOG_LINES_MAX, HTTP_LOG_LINES_MAX, LOG_LINES_DEFAULT  # noqa: E402

_seq = iter(range(1000, 999999))


def _uid(prefix: str) -> str:
    # 任务名的规则是 ^[a-z][a-z0-9-]{1,38}$（不能有下划线），用户名才允许 _
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _drop(tid: int):
    dbm.execute("DELETE FROM cron_task WHERE id=?", (tid,))


def _fake_run(calls: list):
    """顶掉 systemctl：只记录 argv，绝不在真实主机上执行。"""

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        calls.append([cmd, *args])
        return {"code": 0, "out": ""}

    return run


def _has_chinese(msg: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in msg)


class _Case(unittest.TestCase):
    """公共夹具：unit 目录指向 tmp，systemctl 与二进制解析全用假的。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_cron_unit_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls: list = []
        patches = [
            mock.patch.object(config, "UNIT_DIR", self.dir),
            mock.patch.object(C, "run", _fake_run(self.calls)),
            mock.patch.object(C, "_resolve_binary", lambda name: f"/usr/local/bin/{name}"),
        ]
        for p in patches:
            self.addCleanup(p.stop)
            p.start()

    def _mk(self, name=None, expr="0 3 * * *", argv=("choyeonctl", "backup", "run"), enabled=1) -> dict:
        """直接写库造一行（绕过校验），用来模拟脏数据/历史数据。"""
        row = {
            "name": name or _uid("mk"),
            "expr": expr,
            "argv": json.dumps(list(argv)),
            "enabled": enabled,
            "last_run": None,
            "last_status": "",
            "log_tail": "",
            "created_at": "",
        }
        cols = list(row)
        tid = dbm.execute(
            f"INSERT INTO cron_task({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row.values())
        )
        self.addCleanup(_drop, tid)
        return dbm.query_one("SELECT * FROM cron_task WHERE id=?", (tid,))


# ---------------------------------------------------------------- 表达式解析
class TestParseExpr(unittest.TestCase):
    ACCEPT = [
        "* * * * *",
        "0 3 * * *",
        "30 3 * * *",
        "*/15 * * * *",
        "0 9-18 * * 1-5",
        "0 0 1 1 *",
        "5/15 * * * *",
        "0 0 * * 0",
        "0 0 * * 7",
        "0 6,18 1,15 * *",
        "0-10/2 * * * *",
        "  0   3  *  *  *  ",
    ]
    REJECT = [
        "@daily",
        "@reboot",
        "@yearly",
        "* * * * * *",
        "0 3 * * * 2026",
        "* * * * mon",
        "0 3 * jan *",
        "0 60 * * *",
        "0 24 * * *",
        "0 3 0 * *",
        "0 3 32 * *",
        "0 3 * 13 *",
        "0 3 * * 8",
        "30-10 * * * *",
        "*/0 * * * *",
        "0 3 * * 1,",
        "",
        "   ",
        "* * * *",
        "0 3 * * a",
        "0 3 * * 1;5",
        "0 3 * * 1 5",
        "0 3 * * -5",
    ]

    def test_accept_table(self):
        for expr in self.ACCEPT:
            with self.subTest(expr=expr):
                spec = C.parse_expr(expr)
                for key, lo, hi, _label in C.FIELDS:
                    self.assertTrue(lo <= min(spec[key]) <= max(spec[key]) <= (6 if key == "dow" else hi), key)
                self.assertTrue(_has_chinese(spec["human"]))

    def test_reject_table_is_valueerror_with_chinese(self):
        for expr in self.REJECT:
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError) as cm:
                    C.parse_expr(expr)
                msg = str(cm.exception)
                self.assertTrue(_has_chinese(msg), f"报错不是中文：{msg}")
                self.assertNotIn("Traceback", msg)

    def test_dow_7_normalizes_to_sunday(self):
        self.assertEqual(C.parse_expr("0 0 * * 7")["dow"], [0])
        self.assertEqual(C.parse_expr("0 0 * * 0")["dow"], [0])

    def test_step_from_nonzero_start(self):
        self.assertEqual(C.parse_expr("5/15 * * * *")["minute"], [5, 20, 35, 50])

    def test_range_with_step(self):
        self.assertEqual(C.parse_expr("0-10/3 * * * *")["minute"], [0, 3, 6, 9])

    def test_comma_list_with_ranges(self):
        self.assertEqual(C.parse_expr("0,30 8-10 1-7 * *")["dom"], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(C.parse_expr("0,30 8-10 1-7 * *")["hour"], [8, 9, 10])

    def test_human_is_readable_chinese(self):
        cases = {
            "0 3 * * *": "每天 03:00",
            "*/15 * * * *": "每 15 分钟",
            "0 0 * * 0": "每周日 00:00",
            "0 9-18 * * 1-5": "每周一至周五 09-18 点整点",
            "0 0 1 1 *": "每年 01 月 01 日 00:00",
        }
        for expr, want in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(C.parse_expr(expr)["human"], want)


class TestOnCalendar(unittest.TestCase):
    RENDER = {
        "0 3 * * *": "*-*-* 03:00:00",
        "*/15 * * * *": "*-*-* *:00,15,30,45:00",
        "0 9-18 * * 1-5": "Mon..Fri *-*-* 09..18:00:00",
        "0 0 1 1 *": "*-01-01 00:00:00",
        "30 3 1,15 * *": "*-*-01,15 03:30:00",
        "0 6 * * 0": "Sun *-*-* 06:00:00",
        "0 6 * * 6": "Sat *-*-* 06:00:00",
        "0 0 1,15 * *": "*-*-01,15 00:00:00",
    }

    def test_render_table(self):
        for expr, want in self.RENDER.items():
            with self.subTest(expr=expr):
                self.assertEqual(C.to_oncalendar(C.parse_expr(expr)), want)

    def test_dow_names_map_sunday_zero(self):
        # cron 0=周日；systemd 用名字，错位一天就是"周五的活周三干"
        for cron_dow, name in ((0, "Sun"), (1, "Mon"), (3, "Wed"), (5, "Fri"), (6, "Sat")):
            with self.subTest(cron_dow=cron_dow):
                cal = C.to_oncalendar(C.parse_expr(f"0 0 * * {cron_dow}"))
                self.assertTrue(cal.startswith(f"{name} "), cal)

    def test_dom_and_dow_both_restricted_is_refused(self):
        spec = C.parse_expr("0 0 12 * 3")
        with self.assertRaises(ValueError) as cm:
            C.to_oncalendar(spec)
        self.assertIn("同时限定", str(cm.exception))
        # 中文说明要如实写出"或"语义，不能假装没问题
        self.assertIn("满足其一", spec["human"])

    def test_dom_step_keeps_star_semantics(self):
        # `*/2` 在 dom 上仍是星号形式（Vixie 的 DOM_STAR），不该被当成"或"语义拒掉
        self.assertEqual(
            C.to_oncalendar(C.parse_expr("0 3 */2 * *")),
            "*-*-01,03,05,07,09,11,13,15,17,19,21,23,25,27,29,31 03:00:00",
        )

    def test_preview_has_no_side_effects(self):
        calls = []
        with mock.patch.object(C, "run", _fake_run(calls)):
            out = C.preview("0 3 * * *")
            bad = C.preview("0 0 12 * 3")
            self.assertEqual(calls, [])
        self.assertEqual(out["oncalendar"], "*-*-* 03:00:00")
        self.assertIsNone(out["error"])
        self.assertIn("同时限定", bad["error"])
        self.assertIn("满足其一", bad["human"])
        with self.assertRaises(ValueError):
            C.preview("@daily")


# ---------------------------------------------------------------- 命令白名单
class TestArgvWhitelist(unittest.TestCase):
    def test_accepts_documented_commands(self):
        for argv in (
            ["choyeonctl", "backup", "run"],
            ["choyeonctl", "doctor"],
            ["choyeonctl", "status"],
            ["choyeonctl", "backup", "list"],
            ["choyeonctl", "app", "list"],
            ["choyeonctl", "logs"],
            ["choyeonctl", "logs", "--lines=200"],
            ["choyeonctl", "logs", "--lines", "200"],
            ["choyeonctl", "app", "deploy", "--id", "3"],
            ["choyeonctl", "app", "rollback", "--id", "12"],
            ["certbot", "renew"],
            ["journalctl", "--vacuum-size=200M"],
            ["journalctl", "--vacuum-time=7d"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(C._validate_argv(argv), argv)

    def test_presets_are_valid(self):
        for p in C.PRESET_CMDS:
            if p.get("needs_unit"):
                continue
            with self.subTest(preset=p["argv"]):
                C._validate_argv(list(p["argv"]))

    def test_logs_preset_pins_no_line_count(self):
        """logs 预设只到子命令为止，行数交给前端参数框拼 `--lines=N`。

        预设里写死 `--lines 200` 时，界面上再填任何参数都会变成 logs 的第二个参数，
        服务端必然 400，用户无论怎么改都保存不了。
        """
        p = next(x for x in C.PRESET_CMDS if x["argv"][:2] == ["choyeonctl", "logs"])
        self.assertEqual(p["argv"], ["choyeonctl", "logs"])

    def test_logs_accepts_one_lines_flag_only(self):
        for argv in (["choyeonctl", "logs"], ["choyeonctl", "logs", "--lines=500"],
                     ["choyeonctl", "logs", "--lines", "500"]):
            with self.subTest(argv=argv):
                self.assertEqual(C._validate_argv(list(argv)), argv)
        with self.assertRaises(BadInput):
            C._validate_argv(["choyeonctl", "logs", "--lines", "200", "--lines=500"])

    def test_rejects_arbitrary_command(self):
        with self.assertRaises(BadInput) as cm:
            C._validate_argv(["rm", "-rf", "/"])
        self.assertIn("不在白名单", str(cm.exception))

    def test_rejects_shell_metacharacters(self):
        for argv in (
            ["choyeonctl", "backup", "run;", "echo", "pwned"],
            ["choyeonctl", "backup", "run", "&&", "rm"],
            ["choyeonctl", "backup", "run", "|", "tee"],
            ["choyeonctl", "backup", "run`id`"],
            ["choyeonctl", "backup", "$(id)"],
            ["choyeonctl", "backup", "run > /tmp/x"],
            ["sh", "-c", "choyeonctl backup run"],
            ["choyeonctl", "backup run"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(BadInput) as cm:
                    C._validate_argv(argv)
                self.assertTrue(_has_chinese(str(cm.exception)))

    def test_rejects_path_form_and_non_list(self):
        with self.assertRaises(BadInput):
            C._validate_argv(["/usr/local/bin/choyeonctl", "backup", "run"])
        with self.assertRaises(BadInput):
            C._validate_argv(["choyeonctl", 3])
        with self.assertRaises(BadInput):
            C._validate_argv("choyeonctl backup run")
        with self.assertRaises(BadInput):
            C._validate_argv([])

    def test_rejects_unknown_choyeonctl_subcommand(self):
        for argv in (
            ["choyeonctl", "user", "create", "admin"],
            ["choyeonctl", "restart"],
            ["choyeonctl", "deploy"],
            ["choyeonctl", "backup", "run", "--force"],
            ["choyeonctl", "app", "deploy", "--id", "3", "--extra", "1"],
            ["choyeonctl", "logs", "--lines=99999"],
            ["choyeonctl", "logs", "--lines", "0"],
        ):
            with self.subTest(argv=argv), self.assertRaises(BadInput):
                C._validate_argv(argv)

    def test_certbot_and_journalctl_are_narrow(self):
        with self.assertRaises(BadInput):
            C._validate_argv(["certbot", "certonly", "--nginx"])
        with self.assertRaises(BadInput):
            C._validate_argv(["journalctl", "--rotate"])
        with self.assertRaises(BadInput):
            C._validate_argv(["journalctl", "-u", "nginx", "-f"])
        with self.assertRaises(BadInput):
            C._validate_argv(["journalctl", "--vacuum-size=200T"])

    def test_systemctl_only_managed_units(self):
        with self.assertRaises(BadInput) as cm:
            C._validate_argv(["systemctl", "restart", "ssh.service"])
        self.assertIn("纳管", str(cm.exception))
        with self.assertRaises(BadInput):
            C._validate_argv(["systemctl", "stop", "choyeon-panel.service"])
        self.assertEqual(
            C._validate_argv(["systemctl", "restart", "choyeon-panel.service"]),
            ["systemctl", "restart", "choyeon-panel.service"],
        )

    def test_app_unit_is_managed(self):
        path = tempfile.mkdtemp(prefix="cp_cron_app_")
        self.addCleanup(shutil.rmtree, path, True)
        name = _uid("app")
        aid = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,start_cmd) VALUES(?,'node','','main',?,'node x')",
            (name, path),
        )
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (aid,))
        C._validate_argv(["systemctl", "restart", f"panel-{name}.service"])


# ---------------------------------------------------------------- unit 落地
class TestUnitFiles(_Case):
    def test_create_writes_both_units_without_shell(self):
        row = asyncio.run(
            C.create_task({"name": _uid("bk"), "expr": "0 3 * * *", "argv": ["choyeonctl", "backup", "run"]})
        )
        self.addCleanup(_drop, row["id"])
        base = f"{self.dir}/panel-cron-{row['id']}"
        svc = open(f"{base}.service").read()
        tmr = open(f"{base}.timer").read()
        self.assertIn("Type=oneshot", svc)
        line = next(ln for ln in svc.splitlines() if ln.startswith("ExecStart="))
        # 命令本体必须原样是 argv（结尾就是可执行文件 + 参数），不是 `sh -c "…"` 的字符串。
        # 批次㉝ 之后它前面可以带一把 flock 重叠闸，那把闸同样以 argv 形态出现，
        # `--` 之后还是原来那几个参数——所以断言只看结尾，中间的前缀由 TestExecStart 负责。
        self.assertTrue(line.endswith("/usr/local/bin/choyeonctl backup run"), line)
        if "flock" in line:
            self.assertIn("-- /usr/local/bin/choyeonctl backup run", line)
        self.assertNotIn("sh -c", svc)
        self.assertNotIn("shell", svc)
        self.assertIn("OnCalendar=*-*-* 03:00:00", tmr)
        self.assertIn("Persistent=true", tmr)
        self.assertIn("WantedBy=timers.target", tmr)
        # 环境只带路径类变量，不含口令/token
        self.assertNotIn("passw", svc.lower())
        self.assertNotIn("token", svc.lower())
        self.assertIn(["systemctl", "daemon-reload"], self.calls)
        self.assertIn(["systemctl", "enable", "--now", f"panel-cron-{row['id']}.timer"], self.calls)

    def test_delete_removes_both_unit_files(self):
        row = asyncio.run(C.create_task({"name": _uid("del"), "expr": "*/10 * * * *", "argv": ["certbot", "renew"]}))
        base = f"{self.dir}/panel-cron-{row['id']}"
        self.assertTrue(os.path.exists(f"{base}.service"))
        self.assertTrue(os.path.exists(f"{base}.timer"))
        name = asyncio.run(C.delete_task(row["id"]))
        self.assertEqual(name, row["name"])
        self.assertFalse(os.path.exists(f"{base}.service"), ".service 残留会继续被 timer 唤醒")
        self.assertFalse(os.path.exists(f"{base}.timer"), ".timer 残留会继续按计划跑")
        self.assertIsNone(dbm.query_one("SELECT id FROM cron_task WHERE id=?", (row["id"],)))
        self.assertIn(["systemctl", "disable", "--now", f"panel-cron-{row['id']}.timer"], self.calls)
        self.assertIn(["systemctl", "daemon-reload"], self.calls)

    def test_disable_removes_units(self):
        row = asyncio.run(C.create_task({"name": _uid("off"), "expr": "0 3 * * *", "argv": ["choyeonctl", "doctor"]}))
        self.addCleanup(_drop, row["id"])
        out = asyncio.run(C.update_task(row["id"], {"enabled": False}))
        self.assertEqual(out["enabled"], 0)
        base = f"{self.dir}/panel-cron-{row['id']}"
        self.assertFalse(os.path.exists(f"{base}.service"))
        self.assertFalse(os.path.exists(f"{base}.timer"))

    def test_reenable_rewrites_units(self):
        row = asyncio.run(C.create_task({"name": _uid("re"), "expr": "0 3 * * *", "argv": ["choyeonctl", "doctor"]}))
        self.addCleanup(_drop, row["id"])
        asyncio.run(C.update_task(row["id"], {"enabled": False}))
        out = asyncio.run(C.update_task(row["id"], {"enabled": True, "expr": "30 4 * * *"}))
        self.assertEqual(out["expr"], "30 4 * * *")
        self.assertIn("OnCalendar=*-*-* 04:30:00", open(f"{self.dir}/panel-cron-{row['id']}.timer").read())

    def test_unexpressible_and_bad_cmd_are_not_stored(self):
        n0 = dbm.query_one("SELECT COUNT(*) c FROM cron_task")["c"]
        for body in (
            {"name": _uid("or"), "expr": "0 0 12 * 3", "argv": ["certbot", "renew"]},
            {"name": _uid("rm"), "expr": "0 3 * * *", "argv": ["rm", "-rf", "/"]},
            {"name": _uid("ex"), "expr": "@daily", "argv": ["certbot", "renew"]},
            {"name": "Bad Name", "expr": "0 3 * * *", "argv": ["certbot", "renew"]},
        ):
            with self.subTest(name=body["name"]), self.assertRaises(BadInput):
                asyncio.run(C.create_task(body))
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM cron_task")["c"], n0)

    def test_sync_units_rejects_dirty_stored_argv(self):
        row = self._mk(argv=("rm", "-rf", "/"))
        with self.assertRaises(BadInput):
            asyncio.run(C._sync_units(row))

    def test_enable_failure_rolls_back_the_row(self):
        async def failing(cmd, args, cwd=None, timeout=120, env=None):
            self.calls.append([cmd, *args])
            return {"code": 1, "out": "Failed to enable unit"}

        with mock.patch.object(C, "run", failing), self.assertRaises(BadInput) as cm:
            asyncio.run(C.create_task({"name": _uid("rf"), "expr": "0 3 * * *", "argv": ["certbot", "renew"]}))
        self.assertIn("已撤销", str(cm.exception))
        self.assertIsNone(dbm.query_one("SELECT id FROM cron_task WHERE name=?", (_uid("rf"),)))

    def test_list_tasks_enriches_without_systemd(self):
        row = asyncio.run(C.create_task({"name": _uid("list"), "expr": "0 3 * * *", "argv": ["choyeonctl", "status"]}))
        self.addCleanup(_drop, row["id"])
        items = asyncio.run(C.list_tasks())
        hit = next(i for i in items if i["id"] == row["id"])
        self.assertEqual(hit["cmd"], "choyeonctl status")
        self.assertEqual(hit["human"], "每天 03:00")
        self.assertFalse(hit["expr_error"])
        self.assertEqual(hit["timer"], f"panel-cron-{row['id']}.timer")
        self.assertFalse(hit["timer_active"])
        self.assertEqual(hit["timer_next"], "")

    def test_list_tasks_reads_live_timer_state(self):
        row = asyncio.run(C.create_task({"name": _uid("nx"), "expr": "0 3 * * *", "argv": ["choyeonctl", "status"]}))
        self.addCleanup(_drop, row["id"])
        show_out = (
            f"Id=panel-cron-{row['id']}.timer\nActiveState=waiting\nUnitFileState=enabled\n"
            "NextElapseUSecRealtime=Sat 2026-10-03 03:30:00 CST\n\n"
            "Id=panel-cron-99999.timer\n"
        )

        async def show(cmd, args, cwd=None, timeout=120, env=None):
            return {"code": 0, "out": show_out}

        with mock.patch.object(C, "run", show):
            items = asyncio.run(C.list_tasks())
        hit = next(i for i in items if i["id"] == row["id"])
        self.assertFalse(hit["timer_active"])  # waiting 不是 active，芯片要如实显示
        self.assertEqual(hit["timer_next"], "Sat 2026-10-03 03:30:00 CST")

    def test_legacy_row_with_garbage_expr_still_lists(self):
        row = self._mk(expr="@daily")
        items = asyncio.run(C.list_tasks())
        hit = next(i for i in items if i["id"] == row["id"])
        self.assertEqual(hit["human"], "")
        self.assertTrue(hit["expr_error"])

    def test_unknown_id_is_not_found(self):
        # delete_task 只要 id，update_task 还要 body——两个入口都必须认不出就 404
        for fn, args in ((C.delete_task, (999999,)), (C.update_task, (999999, {}))):
            with self.subTest(fn=fn.__name__), self.assertRaises(NotFound):
                asyncio.run(fn(*args))


# ---------------------------------------------------------------- 立即执行
class _FakeProc:
    def __init__(self, payload: bytes, code: int = 0):
        self.payload = payload
        self.returncode = code

    async def communicate(self):
        return self.payload, b""

    def kill(self):
        pass


async def _exec_ok(*_argv, **_kw):
    """create_subprocess_exec 的替身：零退出、一行输出，绝不真的起进程。"""
    return _FakeProc(b"hi\n", 0)


class TestRunNow(_Case):
    def _run_with(self, payload: bytes, code: int = 0):
        seen: list = []

        async def fake_exec(*argv, **kw):
            seen.append([*argv])
            self.kwargs = kw
            return _FakeProc(payload, code)

        row = asyncio.run(
            C.create_task({"name": _uid("run"), "expr": "0 3 * * *", "argv": ["choyeonctl", "backup", "run"]})
        )
        self.addCleanup(_drop, row["id"])
        with mock.patch.object(C.asyncio, "create_subprocess_exec", fake_exec):
            out = asyncio.run(C.run_now(row["id"], actor="ut"))
        return row, out, seen

    def test_exec_uses_argv_not_shell(self):
        _, _, seen = self._run_with(b"done\n")
        self.assertEqual(seen[0], ["/usr/local/bin/choyeonctl", "backup", "run"])
        self.assertNotIn("-c", seen[0])
        self.assertTrue(self.kwargs.get("start_new_session"))

    def test_output_capped_at_4kb(self):
        _, out, _ = self._run_with(b"x" * 40000)
        self.assertLessEqual(len(out["log"].encode()), 4096)
        stored = dbm.query_one("SELECT log_tail FROM cron_task ORDER BY id DESC LIMIT 1")["log_tail"]
        self.assertLessEqual(len(stored.encode()), 4096)

    def test_credentials_redacted_not_merely_truncated(self):
        # 凭据行藏在 4KB 尾巴里：必须先脱敏再截断，否则截断边界一动就漏
        payload = b"y" * 3000 + b"\nclone https://bob:S3cr3tT0ken@github.com/u/r.git ok\n" + b"z" * 2000
        _, out, _ = self._run_with(payload)
        self.assertNotIn("S3cr3tT0ken", out["log"])
        self.assertIn("bob:***@", out["log"])
        self.assertLessEqual(len(out["log"].encode()), 4096)

    def test_status_and_tail_persisted(self):
        row, out, _ = self._run_with(b"boom\n", code=2)
        self.assertEqual(out["code"], 2)
        self.assertEqual(out["status"], "exit 2")
        db_row = dbm.query_one("SELECT * FROM cron_task WHERE id=?", (row["id"],))
        self.assertEqual(db_row["last_status"], "exit 2")
        self.assertTrue(db_row["last_run"])
        self.assertEqual(db_row["log_tail"], "boom\n")
        audit = dbm.query("SELECT * FROM audit WHERE action='cron:run' ORDER BY id DESC LIMIT 1")[0]
        self.assertIn(row["name"], audit["detail"])
        self.assertNotIn("choyeonctl", audit["detail"])

    def test_zero_exit_is_ok(self):
        _, out, _ = self._run_with(b"ok\n")
        self.assertEqual(out["status"], "ok")

    def test_run_rejects_dirty_stored_argv(self):
        row = self._mk(argv=("echo", "hi"))
        with self.assertRaises(BadInput):
            asyncio.run(C.run_now(row["id"]))


# ---------------------------------------------------------------- HTTP 契约
class TestCronRouter(_Case):
    """路由单独挂一个小 app：main.py 的注册由总控负责，模块必须能独立导入。"""

    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI, Request
        from starlette.testclient import TestClient

        from app.routers.cron import router as cron_router

        api = FastAPI()

        @api.middleware("http")
        async def _state(req: Request, call_next):
            req.state.cp_role = cls.role
            req.state.cp_sub = cls.actor
            return await call_next(req)

        api.include_router(cron_router)
        cls.client = TestClient(api)

    def setUp(self):
        _Case.setUp(self)  # tmp unit 目录 + systemctl 替身，路由测试同样不能碰真 systemd
        TestCronRouter.role = "admin"
        TestCronRouter.actor = "ut-cron"


class TestCronHttp(TestCronRouter):
    def test_preview_returns_chinese_and_oncalendar(self):
        r = self.client.get("/api/cron/preview?expr=0%203%20*%20*%20*")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["human"], "每天 03:00")
        self.assertEqual(r.json()["oncalendar"], "*-*-* 03:00:00")

    def test_preview_rejects_macro(self):
        r = self.client.get("/api/cron/preview?expr=@daily")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertTrue(_has_chinese(r.json()["error"]))

    def test_create_list_run_delete(self):
        name = _uid("http")
        r = self.client.post("/api/cron", json={"name": name, "expr": "*/5 * * * *", "argv": ["certbot", "renew"]})
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["id"]
        self.addCleanup(_drop, tid)
        self.assertTrue(os.path.exists(f"{self.dir}/panel-cron-{tid}.timer"))

        lst = self.client.get("/api/cron").json()["items"]
        hit = next(i for i in lst if i["id"] == tid)
        self.assertEqual(hit["human"], "每 5 分钟")
        self.assertEqual(hit["cmd"], "certbot renew")

        with mock.patch.object(C.asyncio, "create_subprocess_exec", _exec_ok):
            run = self.client.post(f"/api/cron/{tid}/run")
        self.assertEqual(run.status_code, 200, run.text)

        self.assertEqual(self.client.patch(f"/api/cron/{tid}", json={"enabled": False}).status_code, 200)
        self.assertFalse(os.path.exists(f"{self.dir}/panel-cron-{tid}.timer"))

        self.assertEqual(self.client.delete(f"/api/cron/{tid}").status_code, 200)
        self.assertIsNone(dbm.query_one("SELECT id FROM cron_task WHERE id=?", (tid,)))
        # 审计按任务名过滤：测试库是共享的，别的用例也会写 cron:run
        actions = [
            a["action"]
            for a in dbm.query("SELECT * FROM audit WHERE action LIKE 'cron:%' AND detail LIKE ? ORDER BY id", (f"%{name}%",))
        ]
        self.assertEqual(actions, ["cron:create", "cron:run", "cron:update", "cron:delete"])

    def test_bad_command_is_400_not_stored(self):
        r = self.client.post("/api/cron", json={"name": _uid("inj"), "expr": "0 3 * * *", "argv": ["rm", "-rf", "/"]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("白名单", r.json()["error"])
        self.assertIsNone(dbm.query_one("SELECT id FROM cron_task WHERE name LIKE ?", ("inj-%",)))

    def test_unknown_id_is_404(self):
        # 用 request() 统一发：httpx 版的 TestClient 不允许在 get/delete 上直接传 json
        for method, path in (("delete", "/api/cron/999999"), ("patch", "/api/cron/999999"), ("post", "/api/cron/999999/run")):
            with self.subTest(method=method):
                r = self.client.request(method, path, json={})
                self.assertEqual(r.status_code, 404, r.text)
                self.assertTrue(_has_chinese(r.json()["error"]))

    def test_non_numeric_id_is_400(self):
        r = self.client.patch("/api/cron/abc", json={"enabled": 0})
        self.assertEqual(r.status_code, 400, r.text)

    def test_viewer_is_refused(self):
        TestCronRouter.role = "viewer"
        self.addCleanup(setattr, TestCronRouter, "role", "admin")
        for method, path in (("get", "/api/cron"), ("post", "/api/cron"), ("delete", "/api/cron/1")):
            with self.subTest(method=method):
                r = self.client.request(method, path, json={})
                self.assertEqual(r.status_code, 403, r.text)
                self.assertTrue(_has_chinese(r.json()["error"]))

    def test_duplicate_name_is_400(self):
        name = _uid("dup")
        body = {"name": name, "expr": "0 3 * * *", "argv": ["choyeonctl", "doctor"]}
        self.assertEqual(self.client.post("/api/cron", json=body).status_code, 200)
        r = self.client.post("/api/cron", json=body)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("已存在", r.json()["error"])
        tid = dbm.query_one("SELECT id FROM cron_task WHERE name=?", (name,))["id"]
        self.addCleanup(_drop, tid)


# ---------------------------------------------------------------- 日志检索接口
def _journal_entry(msg: str) -> str:
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": "1767225600000000",
            "_SYSTEMD_UNIT": "choyeon-panel.service",
            "PRIORITY": "3",
            "MESSAGE": msg,
        }
    )


class TestLogsEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from app import main

        cls.client = TestClient(main.app)
        cls.user = _uid("lgu")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (cls.user, security.hash_pass("Password123"), "admin"),
        )
        cls.headers = {"Authorization": f"Bearer {security.sign_token(cls.user, 'admin')}"}

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username=?", (cls.user,))

    def setUp(self):
        self.calls: list = []

        async def fake_run(cmd, args, cwd=None, timeout=120, env=None):
            self.calls.append([cmd, *args])
            return {"code": 0, "out": _journal_entry("clone https://bob:S3cr3tT0ken@github.com/u/r.git")}

        p = mock.patch.object(SYS, "run", fake_run)
        self.addCleanup(p.stop)
        p.start()

    def _get(self, path, **qp):
        return self.client.get(f"{path}?{urlencode(qp)}", headers=self.headers)

    def test_malformed_unit_is_400_and_reaches_no_process(self):
        for bad in ("nginx.service; rm -rf /", "-u", "$(id)", "a" * 80, "../../etc/passwd"):
            with self.subTest(bad=bad):
                r = self._get("/api/system/logs", unit=bad)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("unit", r.json()["error"])
        self.assertEqual(self.calls, [])

    def test_overlong_grep_is_400(self):
        r = self._get("/api/system/logs", grep="k" * (SYS.MAX_GREP + 1))
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("关键词", r.json()["error"])
        self.assertEqual(self.calls, [])

    def test_newline_in_grep_is_400(self):
        self.assertEqual(self._get("/api/system/logs", grep="a\nb").status_code, 400)

    def test_bad_level_and_time_are_400(self):
        # 5h 是合法的相对写法（放在这里当反例容易误伤），非法量级/单位要挡下来
        for qp in (
            {"level": "all"},
            {"since": "yesterday"},
            {"since": "31 Dec 2026"},
            {"until": "5hours"},
            {"since": "0h"},
            {"since": "400d"},
        ):
            with self.subTest(qp=qp):
                self.assertEqual(self._get("/api/system/logs", **qp).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_argv_shape_is_journalctl_only(self):
        r = self._get(
            "/api/system/logs", unit="choyeon-panel.service", since="30min", level="err", grep="Timeout", limit="50"
        )
        self.assertEqual(r.status_code, 200, r.text)
        argv = self.calls[0]
        self.assertEqual(argv[0], "journalctl")
        self.assertNotIn("-c", argv)
        self.assertNotIn("sh", argv)
        self.assertIn("choyeon-panel.service", argv)
        self.assertIn("err", argv)
        self.assertIn("Timeout", argv)  # 关键词是独立 argv 元素，不做任何字符串拼接
        i = argv.index("-g")
        self.assertEqual(argv[i + 1], "Timeout")
        s = argv.index("--since")
        self.assertRegex(argv[s + 1], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")  # 相对量已换算成绝对时间
        self.assertNotIn("30min", argv)

    def test_abs_time_and_levels_are_passed_through(self):
        self._get("/api/system/logs", since="2026-01-02 03:04", until="2026-01-02 03:59:59", level="warning")
        argv = self.calls[0]
        self.assertEqual(argv[argv.index("--since") + 1], "2026-01-02 03:04")
        self.assertEqual(argv[argv.index("--until") + 1], "2026-01-02 03:59:59")
        self.assertEqual(argv[argv.index("-p") + 1], "warning")

    def test_since_after_until_is_400(self):
        self.assertEqual(self._get("/api/system/logs", since="2026-01-02 10:00", until="2026-01-02 09:00").status_code, 400)
        self.assertEqual(self.calls, [])

    def test_limit_is_capped_and_defaults(self):
        self._get("/api/system/logs", limit="999999")
        argv = self.calls[0]
        # HTTP 侧的档位独立于 CLI 的 `logs --lines`（`CLI_LOG_LINES_MAX`）：
        # 接口一次能拉的行数受响应体大小约束，命令行的那次是人在终端里看着输出的。
        self.assertLess(HTTP_LOG_LINES_MAX, CLI_LOG_LINES_MAX)
        self.assertEqual(argv[argv.index("-n") + 1], str(HTTP_LOG_LINES_MAX))
        self._get("/api/system/logs", limit="abc")
        argv = self.calls[1]
        self.assertEqual(argv[argv.index("-n") + 1], str(LOG_LINES_DEFAULT), "非数字视为未指定")

    def test_items_parsed_and_redacted(self):
        body = self._get("/api/system/logs", unit="choyeon-panel").json()
        self.assertEqual(body["count"], 1)
        self.assertNotIn("S3cr3tT0ken", body["items"][0]["text"])
        self.assertIn("bob:***@", body["items"][0]["text"])
        self.assertEqual(body["items"][0]["prio"], 3)
        self.assertTrue(body["items"][0]["ts"])

    def test_viewer_cannot_read_logs(self):
        viewer = _uid("vwu")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (viewer, security.hash_pass("Password123"), "viewer"),
        )
        try:
            h = {"Authorization": f"Bearer {security.sign_token(viewer, 'viewer')}"}
            q = self.client.get("/api/system/logs?unit=nginx", headers=h)
            n = self.client.get("/api/system/nginx-log", headers=h)
        finally:
            dbm.execute("DELETE FROM users WHERE username=?", (viewer,))
        self.assertEqual(q.status_code, 403, q.text)
        self.assertEqual(n.status_code, 403, n.text)

    def test_journalctl_regex_failure_is_client_error(self):
        async def failing(cmd, args, cwd=None, timeout=120, env=None):
            return {"code": 1, "out": "Failed to parse regex"}

        with mock.patch.object(SYS, "run", failing):
            r = self._get("/api/system/logs", grep="([unclosed")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("正则", r.json()["error"])

    def test_journalctl_missing_is_502_not_traceback(self):
        async def missing(cmd, args, cwd=None, timeout=120, env=None):
            return {"code": 127, "out": "FileNotFoundError: journalctl"}

        with mock.patch.object(SYS, "run", missing):
            r = self._get("/api/system/logs")
        self.assertEqual(r.status_code, 502, r.text)
        self.assertNotIn("Traceback", r.text)


class TestNginxLog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from app import main

        cls.client = TestClient(main.app)
        cls.user = _uid("nlu")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (cls.user, security.hash_pass("Password123"), "admin"),
        )
        cls.headers = {"Authorization": f"Bearer {security.sign_token(cls.user, 'admin')}"}

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username=?", (cls.user,))

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cp_nlx_")
        self.outside = tempfile.mkdtemp(prefix="cp_nly_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        os.makedirs(f"{self.root}/conf.d")
        with open(f"{self.root}/access.log", "w") as f:
            f.write("\n".join(f"line{i} GET https://bob:S3cr3tT0ken@host/x?k=v" for i in range(1, 301)) + "\n")
        with open(f"{self.outside}/access.log", "w") as f:
            f.write("SECRET-OUTSIDE-LINE\n")
        p = mock.patch.object(config, "FILE_ROOTS", [self.root])
        self.addCleanup(p.stop)
        p.start()

    def test_both_roots_list_their_files(self):
        with mock.patch.object(config, "FILE_ROOTS", [self.root, self.outside]):
            paths = sorted(f["path"] for f in SYS.nginx_log_files())
        self.assertEqual(paths, sorted([f"{self.root}/access.log", f"{self.outside}/access.log"]))

    def test_only_files_under_roots_are_visible(self):
        paths = [f["path"] for f in SYS.nginx_log_files()]
        self.assertEqual(paths, [f"{self.root}/access.log"])
        self.assertNotIn(f"{self.outside}/access.log", paths)

    def test_symlink_escape_is_skipped(self):
        try:
            os.symlink(self.outside, f"{self.root}/link")
        except (OSError, NotImplementedError):
            self.skipTest("平台不支持符号链接")
        paths = [f["path"] for f in SYS.nginx_log_files()]
        self.assertEqual(paths, [f"{self.root}/access.log"])

    def test_tail_filter_and_redaction(self):
        r = self.client.get("/api/system/nginx-log?lines=5", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body["items"]), 5)
        self.assertIn("line300", body["items"][-1])
        self.assertNotIn("S3cr3tT0ken", r.text)
        self.assertNotIn(self.root, r.text)  # 不把绝对路径贴给浏览器
        self.assertTrue(body["source"].endswith("/access.log"))
        r2 = self.client.get("/api/system/nginx-log?lines=100&filter=line29", headers=self.headers)
        items = r2.json()["items"]
        self.assertTrue(items and all("line29" in x for x in items))

    def test_bad_file_index_and_long_filter(self):
        for q in ("file=99", "file=abc", f"filter={'k' * 300}"):
            with self.subTest(q=q):
                self.assertEqual(self.client.get(f"/api/system/nginx-log?{q}", headers=self.headers).status_code, 400)

    def test_no_candidates_explains_itself(self):
        with mock.patch.object(config, "FILE_ROOTS", [self.outside + "-none"]):
            r = self.client.get("/api/system/nginx-log", headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("CP_FILE_ROOTS", r.json()["hint"])


if __name__ == "__main__":
    unittest.main()
