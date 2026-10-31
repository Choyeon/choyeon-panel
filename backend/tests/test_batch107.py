"""批次107：日志来源清单与轮替世代的静默封顶不再冒充「全部」。

两处封顶原本都只体现在「少了几条」上，界面与 CLI 读上去和真实情况一模一样：

- `nginx_log_files()` 扫到 `MAX_LOG_FILES` 就 break（省下那次走盘是有意的），但返回一个 60 条
  的列表和「这台机器只有 60 个日志」完全同形——下拉框、`--file` 序号、统计与按天趋势全用这份
  序号，谁都不知道 60 号以后还有文件；
- `log_series()` 先收集全部世代再 `[:MAX_SERIES_FILES]`，于是调用方拿到的 `len()` 恒等于上限，
  按天趋势因此能写出「读了 31/31 份世代」这句恰好最像读完了的话，而环比比的其实是被削短的一段；
- 世代所在目录连列都列不出来时（目录没权限、正好被 logrotate 改名），旧写法交出空列表——
  和「这个站点确实没有历史」同形，于是界面可以说「没人访问」。

要守的三件事，每件都有对应用例：
1. **列表可以截断，账不能**：`log_source_scan()` 给 `listed/limit/capped`（真实总数要重做那次
   省掉的扫描，给不出就不编），`series_scan()` 给 `total/capped/unreadable`（份数在已经拿到手
   的目录名里就能数清，不给才是漏报）；
2. **只报一次**：旧名字 `nginx_log_files()` / `log_series()` 收成同一份扫描的投影，任何调用方
   都不可能拿到没有触顶信息的那一份；
3. **三个出口都带账**：接口（tail / 分钟统计 / 按天趋势）、CLI（`webstats --list` / `--daily`）
   与前端措辞各自只有一个出处，读不到的那个来源不许被画成一张完整的表。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch107_data_"))

from starlette.testclient import TestClient  # noqa: E402

import cli  # noqa: E402
from app import (  # noqa: E402
    config,
    main,
    security,
)
from app import database as dbm  # noqa: E402
from app.services import access_stats as A  # noqa: E402
from app.services import web_logs as WL  # noqa: E402

ROOT = Path(config.BASE)
_seq = iter(range(1000, 999999))
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LOCAL = datetime(2026, 10, 16, 10, 0, 0)


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _wall(back: int = 0, client: str = "203.0.113.9", path: str = "/posts",
          status: int = 200, sent: int = 512) -> str:
    """一条 combined 行，时间戳写死成墙上日期：用例不依赖「今天是几号」。"""
    d = (LOCAL - timedelta(days=back)).replace(hour=10, minute=0, second=0)
    stamp = (f"{d.day:02d}/{_MONTHS[d.month - 1]}/{d.year}:"
             f"{d.hour:02d}:{d.minute:02d}:{d.second:02d} +0800")
    return (f'{client} - - [{stamp}] "GET {path} HTTP/1.1" {status} {sent}'
            f' "-" "Mozilla/5.0 (batch107)"')


def _date(back: int = 0) -> str:
    return (LOCAL - timedelta(days=back)).date().isoformat()


def _row(back: int = 0, pv: int = 1) -> dict:
    return {"date": _date(back), "pv": pv, "uv": pv, "uvFloored": False,
            "bytes": pv * 512, "e4xx": 0, "e5xx": 0}


class _LogDirMixin:
    """把日志放进一个只属于本用例的白名单目录。"""

    def _make_logs(self):
        self.logs = tempfile.mkdtemp(prefix="cp107_logs_")
        self.addCleanup(shutil.rmtree, self.logs, True)
        for p in (mock.patch.object(config, "LOG_ROOTS", [self.logs]),
                  mock.patch.object(config, "FILE_ROOTS", [])):
            p.start()
            self.addCleanup(p.stop)
        self.dir_label = os.path.basename(os.path.realpath(self.logs))

    def _write(self, name: str, lines: list[str]) -> str:
        p = f"{self.logs}/{name}"
        Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def _gz(self, name: str, lines: list[str]) -> str:
        p = f"{self.logs}/{name}"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def _many_sources(self, n: int) -> None:
        for i in range(n):
            self._write(f"s{i}.access.log", [_wall(0)])

    def _four_generations(self) -> None:
        """四个世代摊六天：当前件、`.1`、`.2`、`.3.gz`。"""
        self._write("rosetta.access.log", [_wall(0), _wall(1)])
        self._write("rosetta.access.log.1", [_wall(2), _wall(3)])
        self._write("rosetta.access.log.2", [_wall(4)])
        self._gz("rosetta.access.log.3.gz", [_wall(5)])


class TestSourceScan(_LogDirMixin, unittest.TestCase):
    """`log_source_scan()`：列表截得掉，「截过」这件事截不掉。"""

    def setUp(self):
        self._make_logs()

    def test_projection_is_the_same_scan_not_a_second_reader(self):
        self._many_sources(2)
        scan = WL.log_source_scan()
        self.assertEqual(WL.nginx_log_files(), scan["files"],
                         "旧名字必须读同一份扫描，否则调用方还能拿到不带账的那一份")
        self.assertEqual(scan["listed"], 2)
        self.assertEqual(scan["limit"], WL.MAX_LOG_FILES)
        self.assertFalse(scan["capped"], "没到上限时不许凭空喊触顶")

    def test_cap_is_reported_even_though_the_walk_stopped(self):
        self._many_sources(6)
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            scan = WL.log_source_scan()
        self.assertEqual(len(scan["files"]), 3, "省掉那次走盘是对的")
        self.assertEqual(scan["listed"], 3)
        self.assertTrue(scan["capped"], "但省下扫描不等于可以把「一共三个来源」当成事实")
        self.assertEqual(scan["limit"], 3, "上限值要跟着出去：判词里的数字不能由调用方抄")

    def test_order_is_still_the_shared_index_space(self):
        self._write("b.access.log", [_wall(0)])
        self._write("a.access.log", [_wall(0)])
        files = WL.log_source_scan()["files"]
        self.assertEqual([f["label"] for f in files],
                         [f"{self.dir_label}/a.access.log", f"{self.dir_label}/b.access.log"])

    def test_hint_gives_the_limit_and_the_way_out_but_no_paths(self):
        hint = WL.source_cap_hint()
        self.assertIn(str(WL.MAX_LOG_FILES), hint)
        for key in ("CP_LOG_ROOTS", "choyeonctl upgrade --no-pull"):
            self.assertIn(key, hint)
        self.assertNotIn(self.logs, hint, "这句会进浏览器：白名单里有哪些目录不是页面能读的信息")


class TestSeriesScan(_LogDirMixin, unittest.TestCase):
    """`series_scan()`：份数在已经拿到手的目录名里，凭什么只报截过的那一段。"""

    def setUp(self):
        self._make_logs()
        self._four_generations()

    def test_total_is_the_real_count_not_the_length_of_the_slice(self):
        with mock.patch.object(WL, "MAX_SERIES_FILES", 2):
            scan = WL.series_scan(f"{self.logs}/rosetta.access.log")
        self.assertEqual([e["gen"] for e in scan["entries"]], [0, 1])
        self.assertEqual(scan["total"], 4, "盘上就是四份，切片长度不是份数")
        self.assertTrue(scan["capped"])
        self.assertEqual(scan["limit"], 2)
        self.assertFalse(scan["unreadable"])

    def test_no_cap_when_every_generation_fit(self):
        scan = WL.series_scan(f"{self.logs}/rosetta.access.log")
        self.assertEqual(scan["total"], 4)
        self.assertEqual(len(scan["entries"]), 4)
        self.assertFalse(scan["capped"])

    def test_projection_matches_the_scan(self):
        with mock.patch.object(WL, "MAX_SERIES_FILES", 3):
            self.assertEqual(WL.log_series(f"{self.logs}/rosetta.access.log"),
                             WL.series_scan(f"{self.logs}/rosetta.access.log")["entries"])

    def test_a_directory_that_cannot_be_listed_is_its_own_fact(self):
        """旧写法在这里 `return out` 交出空表：和「盘上只有当前这一份」完全同形。"""
        with mock.patch.object(WL.os, "scandir", side_effect=OSError(13, "Permission denied")):
            scan = WL.series_scan(f"{self.logs}/rosetta.access.log")
        self.assertTrue(scan["unreadable"])
        self.assertEqual(scan["entries"], [])
        self.assertFalse(scan["capped"], "一份都没看到，谈不上触顶")
        self.assertFalse(WL.series_scan(f"{self.logs}/rosetta.access.log")["unreadable"],
                         "同一个函数在能列目录时必须给 False，否则前端分不出两态")

    def test_the_two_sentences_are_separate_facts(self):
        """「更早的还在盘上」与「连清单都没列出来」的处置相反，不能合成一句。"""
        cap, disc = WL.series_cap_hint(), WL.series_discovery_hint()
        self.assertIn(str(WL.MAX_SERIES_FILES), cap)
        self.assertIn("盘上", cap)
        self.assertIn("logrotate", disc)
        self.assertIn("不等于", disc)
        self.assertNotIn(self.logs, disc)
        self.assertNotIn(str(WL.MAX_SERIES_FILES), disc, "触顶的数字不该出现在「列不出清单」那句里")

    def test_a_generation_that_escapes_the_whitelist_is_not_counted(self):
        """回看不构成第二条任意文件读取的路径：每一份世代仍要重新落在白名单里。"""
        outside = tempfile.mkdtemp(prefix="cp107_out_")
        self.addCleanup(shutil.rmtree, outside, True)
        Path(f"{outside}/old.log").write_text(_wall(30), encoding="utf-8")
        link = f"{self.logs}/rosetta.access.log.7"
        try:
            os.symlink(f"{outside}/old.log", link)
        except OSError:
            self.skipTest("平台不允许创建符号链接")
        self.addCleanup(os.unlink, link)
        scan = WL.series_scan(f"{self.logs}/rosetta.access.log")
        self.assertEqual(scan["total"], 4, "逃出去的那一份不能算进历史")


class TestCollectDaysFlags(_LogDirMixin, unittest.TestCase):
    """`collect_days()` 只做一件事：把发现层给的三个数如实折进环比的置信度。"""

    def setUp(self):
        self._make_logs()
        self._four_generations()

    def _entries(self):
        return WL.log_series(f"{self.logs}/rosetta.access.log")

    def test_series_total_replaces_the_length_of_the_slice(self):
        with mock.patch.object(WL, "MAX_SERIES_FILES", 2):
            series = WL.series_scan(f"{self.logs}/rosetta.access.log")
        d = A.collect_days(series["entries"], 3, series_total=series["total"],
                           series_capped=series["capped"])
        self.assertEqual(d["totalFiles"], 4)
        self.assertEqual(d["seriesTotal"], 4)
        self.assertTrue(d["seriesCapped"])
        self.assertFalse(d["seriesUnreadable"])
        self.assertFalse(d["comparable"])
        self.assertIsNone(d["deltaPvPct"])

    def test_a_shortened_lookback_is_clipped_not_history(self):
        """日志本身没那么长（history）与这一轮被削短（clipped）是两种处置：后者能改。"""
        with mock.patch.object(WL, "MAX_SERIES_FILES", 2):
            series = WL.series_scan(f"{self.logs}/rosetta.access.log")
        d = A.collect_days(series["entries"], 3, series_total=series["total"],
                           series_capped=series["capped"])
        self.assertTrue(d["previous"]["partial"])
        self.assertEqual(d["previous"]["reason"], "clipped")

    def test_callers_that_pass_nothing_keep_the_old_reading(self):
        """老签名仍然可用：不传就是「份数等于列表长度」，但绝不能把 31 说成全部。"""
        d = A.collect_days(self._entries(), 3)
        self.assertEqual(d["totalFiles"], 4)
        self.assertEqual(d["seriesTotal"], 4)
        self.assertFalse(d["seriesCapped"])
        self.assertFalse(d["seriesUnreadable"])

    def test_unreadable_is_not_no_data(self):
        d = A.collect_days([], 3, series_unreadable=True)
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["previous"]["reason"], "unreadable")
        self.assertTrue(d["seriesUnreadable"])
        self.assertFalse(d["comparable"])

    def test_empty_list_without_the_flag_still_means_no_traffic(self):
        d = A.collect_days([], 3)
        self.assertEqual(d["previous"]["reason"], "no-data")
        self.assertFalse(d["seriesUnreadable"])

    def test_unreadable_outranks_the_other_two_reasons(self):
        d = A.collect_days([], 3, series_total=0, series_capped=True, series_unreadable=True)
        self.assertEqual(d["previous"]["reason"], "unreadable", "先修权限与目录，再谈被削短的一段")


class _AuthMixin:
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.admin = _uid("b107a")
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (cls.admin, security.hash_pass("Password123"), "admin"))
        cls.hdr = {"Authorization": f"Bearer {security.sign_token(cls.admin, 'admin')}"}

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username=?", (cls.admin,))


class TestApiCarriesTheFlags(_AuthMixin, _LogDirMixin, unittest.TestCase):
    """三个 GET：触顶与列不出要跟着结果一起出去，没有来源时也要带上（否则界面渲染 undefined）。"""

    def setUp(self):
        self._make_logs()

    def _get(self, url: str) -> dict:
        r = self.client.get(url, headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_tail_carries_the_source_ledger(self):
        self._write("rosetta.access.log", [_wall(0)])
        d = self._get("/api/system/nginx-log?lines=10")
        self.assertFalse(d["filesCapped"])
        self.assertEqual(d["filesListed"], 1)
        self.assertEqual(d["filesLimit"], WL.MAX_LOG_FILES)

    def test_tail_says_capped_and_still_lists_the_bounded_set(self):
        self._many_sources(6)
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            d = self._get("/api/system/nginx-log?lines=10")
        self.assertTrue(d["filesCapped"])
        self.assertEqual(d["filesListed"], 3)
        self.assertEqual(len(d["files"]), 3)

    def test_out_of_range_names_the_cap(self):
        """「可用 0-2」在触顶时本身就会误导人——用户以为这就是全部清单。"""
        self._many_sources(6)
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            r = self.client.get("/api/system/nginx-log?file=99", headers=self.hdr)
            hint = WL.source_cap_hint()
        self.assertEqual(r.status_code, 400)
        self.assertIn("可用 0-2", r.json()["error"])
        self.assertIn(hint, r.json()["error"], "越界判词用的必须是服务层那一句，不是路由自己复述")

    def test_no_sources_still_answers_with_the_ledger(self):
        for url in ("/api/system/nginx-log?lines=10", "/api/system/nginx-stats?minutes=60",
                    "/api/system/nginx-daily?days=3"):
            d = self._get(url)
            for k in ("filesCapped", "filesLimit", "filesListed"):
                self.assertIn(k, d, f"{url} 少了 {k}，界面会渲染出 undefined")
            self.assertEqual(d["filesListed"], 0)
            self.assertFalse(d["filesCapped"])

    def test_stats_route_reports_the_source_cap(self):
        self._many_sources(6)
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            d = self._get("/api/system/nginx-stats?minutes=60")
        self.assertTrue(d["filesCapped"])
        self.assertEqual(len(d["files"]), 3)

    def test_daily_forwards_what_the_discovery_layer_saw(self):
        self._four_generations()
        seen: dict = {}
        real = A.collect_days

        def spy(entries, days, **kw):
            seen.update(kw)
            return real(entries, days, **kw)

        with mock.patch.object(WL, "MAX_SERIES_FILES", 2), mock.patch.object(A, "collect_days", spy):
            d = self._get("/api/system/nginx-daily?days=3")
        self.assertEqual(seen, {"series_total": 4, "series_capped": True, "series_unreadable": False},
                         "路由不许自己数 entries 当份数")
        self.assertEqual(d["seriesTotal"], 4)
        self.assertEqual(d["seriesListed"], 2)
        self.assertEqual(d["seriesLimit"], 2)
        self.assertTrue(d["seriesCapped"])
        self.assertFalse(d["comparable"], "被削短的上一段不能给出涨跌")

    def test_daily_keeps_the_unreadable_state_all_the_way_out(self):
        self._write("rosetta.access.log", [_wall(0)])
        unreadable = {"entries": [], "total": 0, "limit": WL.MAX_SERIES_FILES,
                      "capped": False, "unreadable": True}
        with mock.patch.object(WL, "series_scan", lambda _p: unreadable):
            d = self._get("/api/system/nginx-daily?days=3")
        self.assertTrue(d["seriesUnreadable"])
        self.assertEqual(d["previous"]["reason"], "unreadable")
        self.assertEqual(d["seriesListed"], 0)
        self.assertFalse(d["filesCapped"], "这一秒列不出世代清单，不代表来源清单也触顶")

    def test_series_entries_never_leak_paths(self):
        self._four_generations()
        d = self._get("/api/system/nginx-daily?days=3")
        for e in d["series"]:
            self.assertEqual(set(e), {"name", "gen", "gz", "bytes"})
        self.assertNotIn(self.logs, json.dumps(d, ensure_ascii=False))


class _CliRunner:
    """同一份 CLI 入口，json 与人类文本各跑一遍。"""

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buf = io.StringIO()
        code = 0
        prev = cli.Out.json_mode
        cli.Out.json_mode = True
        args = cli.build_parser().parse_args(argv)
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(args.fn(args))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            cli.Out.json_mode = prev
        out = buf.getvalue()
        return code, (json.loads(out) if out.strip().startswith("{") else {"raw": out})

    def _text(self, argv: list[str]) -> str:
        buf = io.StringIO()
        prev = cli.Out.json_mode
        cli.Out.json_mode = False
        args = cli.build_parser().parse_args(argv)
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(args.fn(args))
        except SystemExit:
            pass
        finally:
            cli.Out.json_mode = prev
        return buf.getvalue()


class TestCliHonesty(_CliRunner, _LogDirMixin, unittest.TestCase):
    def setUp(self):
        self._make_logs()

    def test_list_json_carries_the_cap_and_text_says_it(self):
        self._many_sources(6)
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            code, d = self._run(["webstats", "--list"])
            text = self._text(["webstats", "--list"])
            hint = WL.source_cap_hint()
        self.assertEqual(code, 0, d)
        self.assertTrue(d["capped"])
        self.assertEqual(d["listed"], 3)
        self.assertEqual(d["limit"], 3)
        self.assertIn(hint, text, "终端形态必须原样引用服务层那句，数字要跟着上限走")

    def test_list_without_a_cap_does_not_cry_capped(self):
        self._many_sources(2)
        _, d = self._run(["webstats", "--list"])
        self.assertFalse(d["capped"])
        self.assertNotIn("扫描上限", self._text(["webstats", "--list"]))

    def test_out_of_range_names_the_cap(self):
        self._many_sources(6)
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            code, d = self._run(["webstats", "--file", "99"])
            hint = WL.source_cap_hint()
        self.assertEqual(code, cli.EXIT_USAGE, d)
        self.assertIn(hint, d["error"])

    def test_daily_reports_listed_versus_total(self):
        self._four_generations()
        with mock.patch.object(WL, "MAX_SERIES_FILES", 2):
            code, d = self._run(["webstats", "--daily", "--days", "3"])
            text = self._text(["webstats", "--daily", "--days", "3"])
            hint = WL.series_cap_hint()
        self.assertEqual(code, 0, d)
        self.assertEqual(d["seriesTotal"], 4)
        self.assertEqual(d["seriesListed"], 2)
        self.assertIn(hint, text)
        self.assertIn("本次统计 2/4 份", text)
        self.assertNotIn("4/4", text, "翻到 2 份却报成翻了 4 份，是这一批要修掉的那句话")

    def test_clipped_reason_names_what_was_clipped(self):
        """只写「被截断或读不到」会把触顶念成文件坏了；这一轮撞上哪一种，得说得出数。"""
        base = {"source": "rosetta.access.log", "days": 3, "rows": [_row(0)],
                "current": {"pv": 1}, "scannedFiles": 31, "totalFiles": 45,
                "seriesListed": 31, "seriesTotal": 45, "seriesCapped": True,
                "truncatedFiles": 0, "unreadableFiles": 0, "comparable": False,
                "previous": {"pv": 0, "days": 0, "partial": True, "reason": "clipped"}}
        text = cli._webdaily_text(base)
        self.assertIn("世代总数已到本次回看上限", text)
        self.assertIn("有 0 份被单份读量上限截断", text)
        self.assertNotIn("撞上 logrotate", text, "这一轮没有读不到的世代，别替它编一个原因")
        capped_off = cli._webdaily_text({**base, "seriesCapped": False})
        self.assertNotIn("世代总数已到本次回看上限", capped_off)

    def test_unreadable_lookback_is_not_announced_as_no_traffic(self):
        self._write("rosetta.access.log", [_wall(0)])
        unreadable = {"entries": [], "total": 0, "limit": WL.MAX_SERIES_FILES,
                      "capped": False, "unreadable": True}
        with mock.patch.object(WL, "series_scan", lambda _p: unreadable):
            code, d = self._run(["webstats", "--daily", "--days", "3"])
            text = self._text(["webstats", "--daily", "--days", "3"])
        self.assertEqual(code, 0, d)
        self.assertTrue(d["seriesUnreadable"])
        self.assertIn(WL.series_discovery_hint(), text)
        for lie in ("没人访问", "没读到可解析的请求行"):
            self.assertNotIn(lie, text, f"列不出清单被说成「{lie}」，管理员会去查流量而不是查权限")

    def test_no_sources_is_still_a_precondition_failure_not_a_cap(self):
        code, d = self._run(["webstats", "--list"])
        self.assertEqual(code, cli.EXIT_PRECONDITION, d)
        self.assertIn("CP_LOG_ROOTS", d["error"])


class TestOneSourceContract(unittest.TestCase):
    """每个事实只有一个出处：这句话写在哪个文件里，别处只能引用它。"""

    @staticmethod
    def _read(*parts: str) -> str:
        return (ROOT / Path(*parts)).read_text(encoding="utf-8")

    def test_the_cap_sentences_live_in_web_logs_only(self):
        w = self._read("backend", "app", "services", "web_logs.py")
        for name in ("def source_cap_hint", "def series_cap_hint", "def series_discovery_hint"):
            self.assertEqual(w.count(name), 1, name)
        for needle in ("扫描上限", "数量上限是", "列不出这个来源"):
            self.assertEqual(w.count(needle), 1, f"{needle} 在 web_logs 里就该写一次")
            for rel in (("backend", "app", "routers", "system.py"), ("backend", "cli.py"),
                        ("backend", "app", "services", "access_stats.py")):
                self.assertNotIn(needle, self._read(*rel),
                                 f"{rel} 里再写一遍，两句迟早只剩一处转述")

    def test_the_honest_readers_are_the_only_scans(self):
        w = self._read("backend", "app", "services", "web_logs.py")
        self.assertEqual(w.count("def log_source_scan"), 1)
        self.assertEqual(w.count("def series_scan"), 1)
        self.assertIn('return log_source_scan()["files"]', w, "旧名字收成投影，别留第二份扫描")
        self.assertIn('return series_scan(path)["entries"]', w)
        self.assertEqual(w.count("MAX_LOG_FILES = "), 1)
        self.assertEqual(w.count("MAX_SERIES_FILES = "), 1)

    def test_every_api_exit_carries_the_source_ledger(self):
        s = self._read("backend", "app", "routers", "system.py")
        self.assertEqual(s.count("def _source_meta"), 1)
        self.assertEqual(s.count("_source_meta(scan)"), 5,
                         "tail 两个出口、挑选来源、分钟统计、按天趋势都要带账")
        self.assertEqual(s.count("def _range_hint"), 1)
        self.assertEqual(s.count("_range_hint(files, scan)"), 2)
        self.assertEqual(s.count("web_logs.log_source_scan"), 3,
                         "三个 GET 共用一份来源账，否则同一个 3 号在两个页签里还是两个文件")
        self.assertEqual(s.count("web_logs.nginx_log_files"), 0)
        self.assertEqual(s.count("web_logs.log_series"), 0)

    def test_cli_reads_the_same_helpers(self):
        c = self._read("backend", "cli.py")
        self.assertEqual(c.count("web_logs.log_source_scan()"), 1)
        self.assertEqual(c.count("web_logs.series_scan("), 1)
        self.assertEqual(c.count("web_logs.source_cap_hint()"), 2)
        self.assertEqual(c.count("web_logs.series_cap_hint()"), 1)
        self.assertEqual(c.count("web_logs.series_discovery_hint()"), 1)
        self.assertEqual(c.count("def _webdaily_text"), 1)

    def test_frontend_wording_has_one_home_too(self):
        ui = self._read("web", "src", "ui.ts")
        for name in ("export function sourcesCappedNote", "export function seriesCappedNote",
                     "export const SERIES_UNREADABLE_NOTE"):
            self.assertEqual(ui.count(name), 1, name)
        view = self._read("web", "src", "views", "LogCenter.vue")
        for name in ("sourcesCappedNote", "seriesCappedNote", "SERIES_UNREADABLE_NOTE"):
            self.assertGreaterEqual(view.count(name), 2, f"{name} 不能只 import 不用")
            self.assertIn(name, view.split("</script>")[0], "引用要落在取数与判词那一段")
        self.assertEqual(view.count("} & SrcMeta;"), 3, "三个页签的响应类型都挂同一份来源账")
        self.assertIn('v-if="tailCap"', view)
        self.assertIn("世代清单", view)
        self.assertIn("来源清单", view)

    def test_seeded_tests_still_pin_the_shared_picking_rule(self):
        """批次㊺ 的挑选函数守卫必须继续盯住真实调用点，不能被这次改名悄悄放宽。"""
        t = self._read("backend", "tests", "test_batch45.py")
        self.assertIn("WL.log_series(", t, "投影名字仍要有用例在用，不许悄悄删掉")
        self.assertIn('src.count("_pick_access_source(scan, want)")', t,
                      "挑选来源仍要是一个出处（tail 之外的两个页签共用）")
