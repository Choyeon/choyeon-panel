"""批次⑲B：两件事——资源告警要真的分出轻重，「尾读多少行」这几个数字要只有一个出处。

一、要紧档落 crit。CPU/内存/load1 三项超标过去一律 `level="warn"`，注释的理由是
「资源阈值是降级不是停机，crit 留给连不上/单元 failed/证书过期」。问题是磁盘同一件
事早就按两档走了（越过 `disk_thresholds()` 的 crit 记 crit），于是：
- 「CPU 82%」和「CPU 98%」在告警列表里长得一模一样，按 level 过滤的通知渠道分不出
  轻重，要么两条都推、要么把要紧的那条一起滤掉；
- 概览仪表按同一对数（配置线 + `CRIT_STEP`）早就变红了，页面说"要出事"、列表说
  "值得处理"，同一台机器两套话。
要紧档不另发明数字：CPU/内存读 `resource_thresholds()`，负载读自检用的
`load_service.LOAD_RISK_PER_CORE`（每核排队 2 个）。

二、行数上限。同一个 `--lines` 旗标，`cli._tail()` 夹到 5000，`cron_service` 的正则只
放行 1..1000（注释却写着"与 CLI 的 _tail() 同口径"），HTTP 侧另有一档 2000，前端再把
这三个数抄成第三份。坏法是定时任务把终端里能用的值当非法参数 400 拒掉，而拒它的理由
那句根本不成立。现在三个数字住在 `app/util.py`，白名单校验、CLI、HTTP 解析共用，前端
从 `/api/cron/options` 读。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_19b_"))

import unittest  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

import cli  # noqa: E402
from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.errors import BadInput  # noqa: E402
from app.services import alerts_service as als  # noqa: E402
from app.services import cron_service as cr  # noqa: E402
from app.services import load_service as ls  # noqa: E402
from app.util import CLI_LOG_LINES_MAX, HTTP_LOG_LINES_MAX, LOG_LINES_DEFAULT, parse_lines  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CLI_SRC = (ROOT / "backend/cli.py").read_text(encoding="utf-8")
CRON_SRC = (ROOT / "backend/app/services/cron_service.py").read_text(encoding="utf-8")
ALERT_SRC = (ROOT / "backend/app/services/alerts_service.py").read_text(encoding="utf-8")
WEB_SRC = (ROOT / "web/src/views/CronTasks.vue").read_text(encoding="utf-8")


class TestLineLimitsHaveOneSource(unittest.TestCase):
    """`--lines` 在终端里能用，登记成定时任务就不能被拒。"""

    def test_whitelist_accepts_every_value_the_cli_accepts(self):
        for n in (1, 200, 1000, 2000, CLI_LOG_LINES_MAX):
            with self.subTest(n=n):
                cr._validate_choyeonctl(["logs", "--lines", str(n)])
                cr._validate_choyeonctl(["logs", f"--lines={n}"])
                self.assertEqual(cli._tail(n), n, "CLI 与白名单要对同一个 n 给同一个答案")

    def test_out_of_range_and_non_numeric_are_rejected(self):
        for bad in ("0", "-1", str(CLI_LOG_LINES_MAX + 1), "abc", "1e3", "007abc", " 5", ""):
            with self.subTest(bad=bad):
                for form in (["logs", "--lines", bad], ["logs", f"--lines={bad}"]):
                    with self.assertRaises(BadInput, msg=str(form)):
                        cr._validate_choyeonctl(form)

    def test_zero_lines_would_dump_the_whole_log(self):
        """下限 1 不是装饰：`splitlines()[-0:]` 是整份日志。"""
        self.assertEqual(cli._tail(0), 1)
        with self.assertRaises(BadInput):
            cr._validate_choyeonctl(["logs", "--lines", "0"])

    def test_no_second_line_count_left_in_cron_or_cli(self):
        self.assertNotIn("_LINES_RE", CRON_SRC, "白名单不许再养一条自己的正则")
        self.assertNotIn("min(int(raw or 1), 5000)", CLI_SRC)
        self.assertIn("min(int(raw or 1), CLI_LOG_LINES_MAX)", CLI_SRC)

    def test_http_tier_is_a_separate_smaller_cap(self):
        """接口那一档保护的是响应体，跟命令行不同数，也别互相引用。"""
        self.assertLess(HTTP_LOG_LINES_MAX, CLI_LOG_LINES_MAX)
        self.assertEqual(parse_lines(str(HTTP_LOG_LINES_MAX + 1)), HTTP_LOG_LINES_MAX)
        self.assertEqual(cli._tail(HTTP_LOG_LINES_MAX + 1), HTTP_LOG_LINES_MAX + 1)

    def test_command_options_publishes_the_numbers(self):
        opts = cr.command_options()
        self.assertEqual(opts["lines_max"], CLI_LOG_LINES_MAX)
        self.assertEqual(opts["lines_default"], LOG_LINES_DEFAULT)
        self.assertEqual(opts["lines_min"], 1)
        self.assertEqual(opts["output_keep_bytes"], cr.LOG_TAIL_BYTES)

    def test_frontend_reads_them_instead_of_copying_them(self):
        for key in ("lines_max", "lines_default", "lines_min", "output_keep_bytes"):
            self.assertIn(key, WEB_SRC, f"前端应读 /cron/options 的 {key}")
        self.assertNotIn("LOG_LINES_MAX", WEB_SRC, "前端那份常量副本要删干净")
        self.assertNotIn("LOG_LINES_DEFAULT", WEB_SRC)
        self.assertNotIn("KB", WEB_SRC, "留存量由服务端下发，帮助文案不能各写各的")


class TestCronOptionsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.users = {"b19b-admin": "admin", "b19b-viewer": "viewer"}
        for name, role in cls.users.items():
            dbm.execute(
                "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (name, security.hash_pass("Password123"), role),
            )

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username IN (?,?)", tuple(cls.users))

    def _get(self, name: str):
        role = self.users[name]
        token = security.sign_token(name, role)
        return self.client.get("/api/cron/options", headers={"Authorization": f"Bearer {token}"})

    def test_numbers_come_from_the_backend(self):
        body = self._get("b19b-admin").json()
        self.assertEqual(body["lines_max"], CLI_LOG_LINES_MAX)
        self.assertEqual(body["lines_default"], LOG_LINES_DEFAULT)
        self.assertEqual(body["output_keep_bytes"], cr.LOG_TAIL_BYTES)

    def test_viewer_cannot_read_the_whitelist(self):
        self.assertEqual(self._get("b19b-viewer").status_code, 403)


class TestResourceAlertTiers(unittest.TestCase):
    """`_check_resources()` 的判档：配置线之上是 warn，要紧档之上才是 crit。"""

    SNAP = {"cpu": 0.0, "memTotal": 1000, "memUsed": 0, "load": [0.0, 0.0, 0.0], "cpuCores": 4,
            "history": [{"t": 0, "cpu": 1.0}, {"t": 3000, "cpu": 2.0}]}

    def _run(self, **over) -> dict[str, tuple[str, str]]:
        """跑一轮资源巡检，抓 `_check` 收到的 (level, 文案)；`alert_*` 走配置桩不进数据库。"""
        seen: dict[str, tuple[str, str]] = {}
        limits = {k: over.pop(k) for k in ("alert_cpu_pct", "alert_mem_pct", "alert_load1_x") if k in over}
        defaults = {"alert_cpu_pct": 80, "alert_mem_pct": 90, "alert_load1_x": 2}
        snap_over = dict(self.SNAP, **over)

        async def fake_check(key, trigger, message, *, code=None, level="warn", **_kw):
            if trigger:
                seen[code or key] = (level, message)

        async def fake_snap():
            return snap_over

        with (mock.patch.object(als, "_check", fake_check),
              mock.patch.object(als, "setting_int", lambda k: limits.get(k, defaults[k])),
              mock.patch.object(als.system_metrics, "snapshot", fake_snap)):
            asyncio.run(als._check_resources())
        return seen

    def test_warn_band_and_crit_band(self):
        self.assertEqual(self._run(cpu=85.0)["cpu"][0], "warn", "配置线 80、要紧档 90 之间该是 warn")
        self.assertEqual(self._run(cpu=90.0)["cpu"][0], "crit", "越过要紧档要落 crit，和磁盘同一口径")
        self.assertEqual(self._run(memUsed=820, alert_mem_pct=70)["mem"][0], "crit", "内存读的是那条配置线，不是 90")
        self.assertEqual(self._run(memUsed=910)["mem"][0], "warn")
        # 要紧档封顶 99：默认内存线 90 加一档就贴到顶，99% 才算要紧
        self.assertEqual(self._run(memUsed=990)["mem"][0], "crit")

    def test_message_names_both_lines(self):
        level, msg = self._run(cpu=95.0)["cpu"]
        self.assertEqual(level, "crit")
        self.assertIn("阈值 80", msg)
        self.assertIn("要紧档 90", msg, "历史里要看得出越过的到底是哪一档")

    def test_load_uses_the_doctors_risk_number(self):
        self.assertEqual(als.LOAD_RISK_PER_CORE, ls.LOAD_RISK_PER_CORE,
                         "负载要紧档不许在服务层另写一份")
        # 默认配置线 2×核数 == 要紧档（每核排队 2 个），所以一超标即 crit
        self.assertEqual(self._run(load=[8.0, 3.0, 1.0])["load1"][0], "crit")
        # 把配置线调早到 1×核数（饱和就报），1.5×核数正好落在两档之间
        self.assertEqual(self._run(load=[6.0, 3.0, 1.0], alert_load1_x=1)["load1"][0], "warn")

    def test_no_hardcoded_warn_for_resource_rows_left(self):
        for chunk in ALERT_SRC.split("await _check(")[1:]:
            key = chunk.split(",", 1)[0].strip()
            if key in ('"cpu"', '"mem"', '"load1"'):
                self.assertNotIn('level="warn"', chunk, f"{key} 的档位要由数值决定")


if __name__ == "__main__":
    unittest.main()
