"""批次㊺A：网站流量按天与环比（世代发现 + 逐日聚合 + 接口 + CLI + 契约同源）。

要守的五件事，每件都有对应用例：
- **回看历史不是第二条读取路径**：`.1`/`.2.gz` 这些世代从没进过 tail 的白名单，
  `log_series()` 必须逐份重新过"在白名单里 + 是普通文件"，符号链接逃出去的那一份照样不可见；
- **环比宁缺毋假**：上一周期依据不足（日志没那么长、世代被读量上限截断、某份正被 logrotate
  改名读不到）就不给百分比，并把原因写进 `previous.reason`，而不是算出一个看着很像的涨跌；
- **按天分桶跟着日志自己写的那天**：`01:30 +0800` 属于日志上的那一天，按 UTC 归桶会把昨晚的
  流量挪进前一天，趋势图上就是"昨天凭空多了一截"；
- **挑选来源一个出处**：tail、分钟窗口、按天趋势必须共用同一份列表与同一条挑选规则，
  否则"3 号文件"在两个页签里指两个文件；
- **档位与上限一个出处**：`DAY_LADDER` 只在服务里写一次，接口下发、CLI 引用、AGENTS.md 复述，
  三处必须同一个数（与批次16A 的 argparse↔schema↔AGENTS 同构）。
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch45_data_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

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
# 全部用固定墙上日期：用例不依赖"今天是几号"，在任何机器上跑都是同一张表。
LOCAL = datetime(2026, 10, 16, 10, 0, 0)


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _wall(back: int = 0, hh: int = 10, mm: int = 0, tz: str = "+0800", client: str = "203.0.113.9",
          path: str = "/posts", status: int = 200, sent: int = 512, query: str = "") -> str:
    """一条 combined 行，时间戳就是"日志自己写的那个墙上时刻"。

    手工拼月份名，不用 `strftime("%b")`：`%b` 跟 C 库 locale 走，中文 locale 下会写出解析
    不出来的时间戳，症状和被测代码出错一模一样，查起来浪费人。
    """
    d = (LOCAL - timedelta(days=back)).replace(hour=hh, minute=mm, second=0)
    stamp = (f"{d.day:02d}/{_MONTHS[d.month - 1]}/{d.year}:"
             f"{d.hour:02d}:{d.minute:02d}:{d.second:02d} {tz}")
    return (f'{client} - - [{stamp}] "GET {path}{query} HTTP/1.1" {status} {sent}'
            f' "-" "Mozilla/5.0 (batch45)"')


def _date(back: int = 0) -> str:
    return (LOCAL - timedelta(days=back)).date().isoformat()


class _LogDirMixin:
    """把日志放进一个只属于本用例的白名单目录。"""

    def _make_logs(self):
        self.logs = tempfile.mkdtemp(prefix="cp45_logs_")
        self.outside = tempfile.mkdtemp(prefix="cp45_out_")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        for p in (mock.patch.object(config, "LOG_ROOTS", [self.logs]),
                  mock.patch.object(config, "FILE_ROOTS", [])):
            p.start()
            self.addCleanup(p.stop)

    def _plain(self, name: str, lines: list[str]) -> str:
        p = f"{self.logs}/{name}"
        Path(p).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def _gz(self, name: str, lines: list[str]) -> str:
        p = f"{self.logs}/{name}"
        with gzip.open(p, "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return p

    def _six_days(self):
        """6 天流量摊在三个世代里：当前件（第 0/1 天）、`.1`（第 2/3 天）、`.2.gz`（第 4/5 天）。"""
        self._plain("rosetta.access.log", [_wall(0), _wall(0, 23, 30), _wall(1), _wall(1, 1, 30)])
        self._plain("rosetta.access.log.1", [_wall(2), _wall(3)])
        self._gz("rosetta.access.log.2.gz", [_wall(4, query="?token=S3cr3tT0ken"), _wall(5)])
        self._plain("rosetta.error.log", ["[error] 不算流量"])

    def _entries(self):
        return WL.log_series(f"{self.logs}/rosetta.access.log")


class TestSeriesDiscovery(_LogDirMixin, unittest.TestCase):
    """`log_series()`：只在同一目录里找同名世代，每份重新过白名单。"""

    def setUp(self):
        self._make_logs()

    def test_current_file_and_generations_come_back_in_order(self):
        self._six_days()
        series = self._entries()
        self.assertEqual([s["gen"] for s in series], [0, 1, 2])
        self.assertEqual([s["name"] for s in series],
                         ["rosetta.access.log", "rosetta.access.log.1", "rosetta.access.log.2.gz"])
        self.assertTrue(series[2]["gz"])
        self.assertFalse(series[0]["gz"])
        self.assertEqual([s["bytes"] for s in series],
                         [os.path.getsize(s["path"]) for s in series])

    def test_generation_ten_is_not_sorted_by_string(self):
        for back in (1, 2, 10):
            self._plain(f"site.access.log.{back}", [_wall(back)])
        self._plain("site.access.log", [_wall(0)])
        gens = [s["gen"] for s in WL.log_series(f"{self.logs}/site.access.log")]
        self.assertEqual(gens, [0, 1, 2, 10], ".10 必须排在 .2 之后：按字符串排会把老日志当新的")

    def test_unrelated_names_in_the_same_directory_are_not_a_generation(self):
        self._plain("access.log", [_wall(0)])
        for junk in ("access.log.bak", "access.log.old", "other.access.log", "access.log.1.gz.1",
                     "access.log.1234", "access.log.0.gz"):
            self._plain(junk, ["噪声"])
        self.assertEqual([s["name"] for s in WL.log_series(f"{self.logs}/access.log")],
                         ["access.log"])

    def test_rotated_source_itself_still_sees_the_whole_series(self):
        """下拉里选中 `.1` 时，世代名要归到同一个 stem，否则"看历史"会只看这一份。"""
        self._six_days()
        self.assertEqual([s["gen"] for s in WL.log_series(f"{self.logs}/rosetta.access.log.1")],
                         [0, 1, 2])

    def test_symlink_escape_and_directory_are_skipped(self):
        self._plain("access.log", [_wall(0)])
        Path(f"{self.outside}/leak.access.log.1").write_text(_wall(1) + "\n", encoding="utf-8")
        os.symlink(f"{self.outside}/leak.access.log.1", f"{self.logs}/access.log.1")
        os.makedirs(f"{self.logs}/access.log.2", exist_ok=True)
        names = [s["name"] for s in WL.log_series(f"{self.logs}/access.log")]
        self.assertEqual(names, ["access.log"], "符号链接逃出去的和目录都不是可读的世代")

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(WL.log_series(f"{self.logs}/gone.access.log"), [])

    def test_generation_count_is_capped(self):
        self._plain("access.log", [_wall(0)])
        for back in range(1, 46):
            self._plain(f"access.log.{back}", [_wall(back)])
        series = WL.log_series(f"{self.logs}/access.log")
        self.assertEqual(len(series), WL.MAX_SERIES_FILES)
        self.assertEqual(series[0]["gen"], 0)
        self.assertEqual(series[-1]["gen"], WL.MAX_SERIES_FILES - 1)

    def test_tail_whitelist_still_refuses_gzip(self):
        """tail 那张列表不收 `.gz`：它按字节读末尾再解码，gzip 内容只会变成坏行。"""
        self._six_days()
        labels = [f["label"] for f in WL.nginx_log_files()]
        self.assertFalse([x for x in labels if x.endswith(".gz")], labels)
        self.assertTrue([s for s in self._entries() if s["gz"]], "按天趋势要能看见 gz 世代")


class TestStemAndGen(unittest.TestCase):
    def test_names(self):
        self.assertEqual(WL._stem_and_gen("access.log.7.gz"), ("access.log", 7))
        self.assertEqual(WL._stem_and_gen("access.log"), ("access.log", 0))
        self.assertEqual(WL._stem_and_gen("rosetta.error.log.1"), ("rosetta.error.log", 1))


class TestCollectDays(_LogDirMixin, unittest.TestCase):
    def setUp(self):
        self._make_logs()

    def test_three_day_window_and_previous_period(self):
        self._six_days()
        d = A.collect_days(self._entries(), 3)
        self.assertEqual([r["date"] for r in d["rows"]], [_date(2), _date(1), _date(0)])
        self.assertEqual([r["pv"] for r in d["rows"]], [1, 2, 2])
        self.assertEqual(d["current"]["pv"], 5)
        self.assertEqual(d["previous"]["pv"], 3, "上一周期 = 第 3/4/5 天")
        self.assertEqual(d["previous"]["days"], 3)
        self.assertFalse(d["previous"]["partial"])
        self.assertEqual(d["previous"]["reason"], "full")
        self.assertTrue(d["comparable"])
        self.assertEqual(d["deltaPvPct"], 66.7, "5 比 3 涨 66.7%，不是拿半份日志凑出来的数")
        self.assertEqual(d["parsed"], 8)
        self.assertEqual(d["totalFiles"], 3)
        self.assertEqual(d["historyFrom"], _date(5))

    def test_anchor_is_the_newest_day_in_the_log_not_today(self):
        """日志停在九天前，就不该出现"今天"这一列 0：锚点是日志里最新那天。"""
        self._plain("quiet.access.log", [_wall(9)])
        d = A.collect_days(WL.log_series(f"{self.logs}/quiet.access.log"), 3)
        self.assertEqual(d["anchor"], _date(9))
        self.assertEqual(d["rows"][2]["pv"], 1)
        self.assertEqual([r["pv"] for r in d["rows"][:2]], [0, 0])

    def test_day_follows_the_offset_written_in_the_log(self):
        """`01:30 +0800` 是墙上那天的凌晨，按 UTC 归桶会掉进前一天。"""
        self._plain("tz.access.log", [_wall(0, 1, 30, tz="+0800"), _wall(0, 23, 30, tz="-0500")])
        d = A.collect_days(WL.log_series(f"{self.logs}/tz.access.log"), 3)
        self.assertEqual(d["anchor"], _date(0))
        self.assertEqual(d["rows"][2]["pv"], 2, "两条都算在日志自己写的那一天")
        stamp = _wall(0, 1, 30, tz="+0800").split("[", 1)[1].split("]", 1)[0]
        as_utc = datetime.fromtimestamp(A._parse_time(stamp), UTC).date()
        self.assertNotEqual(as_utc.isoformat(), d["anchor"],
                            "按 UTC 分桶会把它算成前一天——这条就是把那个 bug 钉住的")

    def test_status_and_bytes_land_in_the_right_day(self):
        self._plain("codes.access.log", [_wall(0, status=404), _wall(0, status=503),
                                         _wall(0, status=200, sent=1000), _wall(1, status=500)])
        d = A.collect_days(WL.log_series(f"{self.logs}/codes.access.log"), 2)
        self.assertEqual(d["rows"][1], {"date": _date(0), "pv": 3, "uv": 1, "uvFloored": False,
                                       "bytes": 2024, "e4xx": 1, "e5xx": 1})
        self.assertEqual(d["rows"][0]["e5xx"], 1, "第 1 天那条 500 不能混进第 0 天的账")

    def test_uv_is_deduped_across_days_within_the_window(self):
        self._plain("uv.access.log", [_wall(0, client="198.51.100.1"), _wall(0, client="198.51.100.2"),
                                      _wall(1, client="198.51.100.1"), _wall(1, client="198.51.100.3")])
        d = A.collect_days(WL.log_series(f"{self.logs}/uv.access.log"), 2)
        self.assertEqual([r["uv"] for r in d["rows"]], [2, 2], "逐日各 2 个人")
        self.assertEqual(d["current"]["uv"], 3, "窗口里是三个不同客户端，不是 2+2")

    def test_short_history_is_not_compared(self):
        """只有 4 天日志却按 7 天问环比：上一周期凑不满，百分比一律不给。"""
        self._plain("few.access.log", [_wall(0), _wall(1)])
        self._plain("few.access.log.1", [_wall(2), _wall(3)])
        d = A.collect_days(WL.log_series(f"{self.logs}/few.access.log"), 7)
        self.assertEqual(len(d["rows"]), 7)
        self.assertTrue(d["previous"]["partial"])
        self.assertEqual(d["previous"]["reason"], "history")
        self.assertFalse(d["comparable"])
        self.assertIsNone(d["deltaPvPct"])

    def test_zero_traffic_day_is_not_the_same_as_missing_log(self):
        """中间那天空无一人，但日志确实覆盖到了那天：环比照给，不把真空档当成没历史。"""
        self._plain("gap.access.log", [_wall(0), _wall(1)])
        self._plain("gap.access.log.1", [_wall(3), _wall(4)])
        d = A.collect_days(WL.log_series(f"{self.logs}/gap.access.log"), 2)
        self.assertEqual(d["previous"]["days"], 1, "上一窗口只有第 3 天有请求")
        self.assertFalse(d["previous"]["partial"])
        self.assertTrue(d["comparable"])

    def test_clipped_scan_is_not_compared(self):
        """单份读量上限被撞到：两边口径不一致，不给假的百分比。"""
        self._six_days()
        real = A.read_lines
        self.addCleanup(setattr, A, "read_lines", real)
        A.read_lines = lambda entry, cap=None: (real(entry, 120)[0], True)
        d = A.collect_days(self._entries(), 3)
        self.assertEqual(d["truncatedFiles"], 3, "每份都被点名")
        self.assertTrue(d["previous"]["partial"])
        self.assertEqual(d["previous"]["reason"], "clipped")
        self.assertFalse(d["comparable"])

    def test_a_generation_that_vanishes_mid_scan_is_named_not_a_crash(self):
        """发现世代之后、读它之前正好撞上 logrotate：这份要报"读不到"，接口不能变 500。"""
        self._six_days()
        entries = self._entries()
        os.unlink(f"{self.logs}/rosetta.access.log.1")
        d = A.collect_days(entries, 3)
        self.assertEqual(d["unreadableFiles"], 1)
        self.assertEqual(d["scannedFiles"], 2)
        self.assertTrue(d["previous"]["partial"])
        self.assertEqual(d["previous"]["reason"], "clipped")
        self.assertFalse(d["comparable"])

    def test_line_cap_is_honest_and_stops(self):
        self._plain("big.access.log", [_wall(0)] * 50)
        real = A.MAX_LINES
        self.addCleanup(setattr, A, "MAX_LINES", real)
        A.MAX_LINES = 10
        d = A.collect_days(WL.log_series(f"{self.logs}/big.access.log"), 3)
        self.assertEqual(d["scannedLines"], 10)
        self.assertEqual(d["parsed"], 10, "封顶就是封顶，多出来的行不许被算进请求数")
        self.assertTrue(d["previous"]["partial"], "扫描被行数截断 → 不给环比")

    def test_buckets_are_pruned_and_the_drop_is_counted(self):
        """一份横跨 121 天的破 logrotate 不会让逐日桶无界增长，被丢的如实报在 outside。"""
        self._plain("spread.access.log", [_wall(back) for back in range(121)])
        d = A.collect_days(WL.log_series(f"{self.logs}/spread.access.log"), 3)
        self.assertEqual(d["parsed"], 121)
        self.assertEqual(d["outside"], 121 - 6, "只留住 2*天数 那一圈，其余点名报数")
        self.assertEqual(sum(r["pv"] for r in d["rows"]), 3)

    def test_malformed_and_hex_escaped_lines_are_not_traffic(self):
        esc = r'\x16\x03\x00\x00\xE8\x01\x00\x00\xE4\x03\x00'
        self._plain("junk.access.log", [_wall(0), esc, "不是日志的一行", ""])
        d = A.collect_days(WL.log_series(f"{self.logs}/junk.access.log"), 2)
        self.assertEqual(d["parsed"], 1)
        self.assertEqual(d["malformed"], 2, "扫描器的 TLS 噪声与真坏行都要点名")
        self.assertNotIn("\\x", json.dumps(d, ensure_ascii=False))

    def test_empty_series_is_a_plain_answer(self):
        d = A.collect_days([], 7)
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["anchor"], "")
        self.assertFalse(d["comparable"])
        self.assertEqual(d["previous"]["reason"], "no-data")

    def test_days_argument_is_clamped(self):
        self._six_days()
        self.assertEqual(A.collect_days(self._entries(), 0)["days"], A.DAY_LADDER[1])
        self.assertEqual(A.collect_days(self._entries(), 999)["days"], A.MAX_DAYS)
        self.assertEqual(A.collect_days(self._entries(), -5)["days"], 1)

    def test_no_absolute_path_leaves_the_service(self):
        self._six_days()
        blob = json.dumps(A.collect_days(self._entries(), 3), ensure_ascii=False)
        self.assertNotIn(self.logs, blob)
        self.assertNotIn("S3cr3tT0ken", blob, "查询串里的 token 不该出现在按天结果里")
        self.assertNotIn("Mozilla", blob)

    def test_stops_reading_once_two_periods_are_covered(self):
        self._six_days()
        for back in range(6, 40):
            self._plain(f"rosetta.access.log.{back}", [_wall(back)])
        d = A.collect_days(self._entries(), 3)
        self.assertLess(d["scannedFiles"], d["totalFiles"],
                        "已经够两个周期还继续翻，就是把「看趋势」变成整盘扫描")
        self.assertEqual(d["historyFrom"], _date(5))


class TestParseTimeParts(unittest.TestCase):
    def test_returns_epoch_and_offset(self):
        epoch, offset = A._parse_time_parts("16/Oct/2026:01:30:00 +0800")
        self.assertEqual(offset, 8 * 3600)
        self.assertEqual(epoch + offset, datetime(2026, 10, 16, 1, 30, tzinfo=UTC).timestamp())
        self.assertEqual(A._parse_time("16/Oct/2026:01:30:00 +0800"), epoch)

    def test_negative_offset_is_kept_as_a_negative_number(self):
        _epoch, offset = A._parse_time_parts("16/Oct/2026:23:30:00 -0500")
        self.assertEqual(offset, -5 * 3600)

    def test_bad_stamps(self):
        for raw in ("", "16/Oct/2026:01:30:00", "32/Oct/2026:01:30:00 +0800",
                    "16/Xyz/2026:01:30:00 +0800", "16/Oct/2026:25:30:00 +0800"):
            self.assertIsNone(A._parse_time_parts(raw), raw)


class TestReadLines(_LogDirMixin, unittest.TestCase):
    def setUp(self):
        self._make_logs()

    def test_plain_and_gzip_both_come_back_as_lines(self):
        p = self._plain("a.log", [_wall(0), _wall(1)])
        g = self._gz("b.log.1.gz", [_wall(2)])
        self.assertEqual(len(A.read_lines({"path": p, "gz": False})[0]), 2)
        lines, capped = A.read_lines({"path": g, "gz": True})
        self.assertEqual(len(lines), 1)
        self.assertFalse(capped)

    def test_cap_drops_the_partial_tail_line(self):
        first, tail = _wall(0), "y" * 200
        p = self._plain("c.log", [first, tail])
        lines, capped = A.read_lines({"path": p, "gz": False}, cap=len(first) + 30)
        self.assertTrue(capped)
        self.assertEqual(lines, [first], "被截断的半行不是完整记录，不能当一条请求计入")

    def test_missing_file_raises_for_the_caller_to_report(self):
        with self.assertRaises(OSError):
            A.read_lines({"path": f"{self.logs}/gone.log", "gz": False})


class _AuthMixin:
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.admin = _uid("b45a")
        cls.viewer = _uid("b45v")
        for name, role in ((cls.admin, "admin"), (cls.viewer, "viewer")):
            dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                        (name, security.hash_pass("Password123"), role))
        cls.hdr = {"Authorization": f"Bearer {security.sign_token(cls.admin, 'admin')}"}
        cls.vhdr = {"Authorization": f"Bearer {security.sign_token(cls.viewer, 'viewer')}"}

    @classmethod
    def tearDownClass(cls):
        for name in (cls.admin, cls.viewer):
            dbm.execute("DELETE FROM users WHERE username=?", (name,))


class TestDailyApi(_AuthMixin, _LogDirMixin, unittest.TestCase):
    def setUp(self):
        self._make_logs()
        self._six_days()

    def test_viewer_and_anonymous_are_refused(self):
        self.assertEqual(self.client.get("/api/system/nginx-daily", headers=self.vhdr).status_code, 403)
        self.assertEqual(self.client.get("/api/system/nginx-daily").status_code, 401)

    def test_numbers_and_the_shared_index_space(self):
        r = self.client.get("/api/system/nginx-daily?file=0&days=3", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["days"], 3)
        self.assertEqual([x["date"] for x in d["rows"]], [_date(2), _date(1), _date(0)])
        self.assertEqual(d["current"]["pv"], 5)
        self.assertEqual(d["deltaPvPct"], 66.7)
        self.assertTrue(d["source"].endswith("/rosetta.access.log"))
        self.assertEqual(d["site"], "rosetta")
        tail = self.client.get("/api/system/nginx-log?lines=1", headers=self.hdr).json()["files"]
        self.assertEqual([f["label"] for f in tail], [f["label"] for f in d["files"]],
                         "tail、分钟统计、按天趋势必须是同一个 0 号")

    def test_series_names_only_never_paths(self):
        r = self.client.get("/api/system/nginx-daily?days=3", headers=self.hdr)
        d = r.json()
        self.assertEqual([s["name"] for s in d["series"]],
                         ["rosetta.access.log", "rosetta.access.log.1", "rosetta.access.log.2.gz"])
        self.assertNotIn(self.logs, r.text)
        self.assertNotIn("S3cr3tT0ken", r.text)
        self.assertNotIn("Mozilla", r.text)

    def test_default_days_comes_from_the_ladder(self):
        d = self.client.get("/api/system/nginx-daily", headers=self.hdr).json()
        self.assertEqual(d["days"], A.DAY_LADDER[1])
        self.assertEqual(len(d["rows"]), A.DAY_LADDER[1])

    def test_error_log_source_is_a_client_error_naming_the_alternatives(self):
        files = self.client.get("/api/system/nginx-log?lines=1", headers=self.hdr).json()["files"]
        err_idx = next(i for i, f in enumerate(files) if f["kind"] == "error")
        r = self.client.get(f"/api/system/nginx-daily?file={err_idx}", headers=self.hdr)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("错误日志", r.json()["error"])

    def test_bad_arguments(self):
        for q in ("file=x", "days=x", "days=0", "days=31", "file=99"):
            r = self.client.get(f"/api/system/nginx-daily?{q}", headers=self.hdr)
            self.assertEqual(r.status_code, 400, f"{q} 应该被拒：{r.text}")

    def test_no_sources_returns_hint_not_an_error(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        r = self.client.get("/api/system/nginx-daily", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["files"], [])
        self.assertIn("CP_LOG_ROOTS", d["hint"])

    def test_only_error_logs_is_a_state_not_a_crash(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        os.makedirs(self.logs, exist_ok=True)
        self._plain("site.error.log", ["[error] 只有错误日志"])
        d = self.client.get("/api/system/nginx-daily", headers=self.hdr).json()
        self.assertEqual(d["source"], "")
        self.assertIn("访问日志", d["hint"])

    def test_log_options_publishes_the_day_ladder(self):
        d = self.client.get("/api/system/log-options", headers=self.hdr).json()
        self.assertEqual(d["stats_days"], A.DAY_LADDER)
        self.assertEqual(d["stats_minutes"], A.WINDOW_LADDER)


class TestOneSourceOnly(unittest.TestCase):
    """按天趋势不许抄第二份挑选逻辑或第二份档位表。"""

    def test_router_picks_the_source_in_exactly_one_place(self):
        src = (ROOT / "backend" / "app" / "routers" / "system.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("def _pick_access_source"), 1)
        self.assertEqual(src.count("_pick_access_source(files, want)"), 2,
                         "分钟窗口与按天趋势要共用同一个挑选函数")
        self.assertEqual(src.count('kind"] != "access'), 1, "「哪个来源能统计」的判断只能有一处")
        self.assertNotIn("DAY_LADDER = ", src)
        self.assertIn("access_stats.DAY_LADDER", src)

    def test_service_caps_are_written_once(self):
        s = (ROOT / "backend" / "app" / "services" / "access_stats.py").read_text(encoding="utf-8")
        w = (ROOT / "backend" / "app" / "services" / "web_logs.py").read_text(encoding="utf-8")
        self.assertEqual(s.count("DAY_LADDER = "), 1)
        self.assertEqual(w.count("MAX_SERIES_FILES = "), 1)
        self.assertEqual(w.count("SERIES_NAME_RE = "), 1)

    def test_gzip_is_only_read_by_the_daily_reader(self):
        """tail 与分钟统计不许偷偷解压 gzip：那会把"看昨天"变成读随机中段。"""
        s = (ROOT / "backend" / "app" / "services" / "access_stats.py").read_text(encoding="utf-8")
        read_tail = s[s.index("def read_tail"):s.index("def collect(")]
        self.assertNotIn("gzip", read_tail)


class TestCliDaily(_LogDirMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._json_mode = cli.Out.json_mode

    def setUp(self):
        self._make_logs()
        self._six_days()

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        buf = io.StringIO()
        code = 0
        cli.Out.json_mode = True
        args = cli.build_parser().parse_args(argv)
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(args.fn(args))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            cli.Out.json_mode = self._json_mode
        out = buf.getvalue()
        return code, (json.loads(out) if out.strip().startswith("{") else {"raw": out})

    def test_parser_defaults(self):
        args = cli.build_parser().parse_args(["webstats"])
        self.assertFalse(args.daily)
        self.assertIsNone(args.days)
        self.assertTrue(cli.build_parser().parse_args(["webstats", "--daily"]).daily)

    def test_daily_run(self):
        code, d = self._run(["webstats", "--daily", "--file", "0", "--days", "3"])
        self.assertEqual(code, 0, d)
        self.assertEqual([x["date"] for x in d["rows"]], [_date(2), _date(1), _date(0)])
        self.assertEqual(d["deltaPvPct"], 66.7)
        self.assertEqual(d["series"][2]["name"], "rosetta.access.log.2.gz")

    def test_days_without_daily_is_a_usage_error(self):
        """写了 `--days` 却没 `--daily`：不能悄悄按分钟窗口跑完还不生效。"""
        code, d = self._run(["webstats", "--days", "5"])
        self.assertEqual(code, cli.EXIT_USAGE, d)
        self.assertIn("--daily", d["error"])

    def test_bad_daily_arguments(self):
        for argv in (["webstats", "--daily", "--days", "x"], ["webstats", "--daily", "--days", "0"],
                     ["webstats", "--daily", "--days", "31"], ["webstats", "--daily", "--file", "99"],
                     ["webstats", "--daily", "--file", "0", "--minutes", "abc"]):
            code, d = self._run(argv)
            self.assertEqual(code, cli.EXIT_USAGE, f"{argv} → {d}")

    def test_error_log_source_is_a_usage_error(self):
        files = WL.nginx_log_files()
        err_idx = next(i for i, f in enumerate(files) if f["kind"] == "error")
        code, d = self._run(["webstats", "--daily", "--file", str(err_idx)])
        self.assertEqual(code, cli.EXIT_USAGE, d)
        self.assertIn("错误日志", d["error"])

    def test_no_sources_is_a_precondition_failure(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        code, d = self._run(["webstats", "--daily"])
        self.assertEqual(code, cli.EXIT_PRECONDITION, d)
        self.assertIn("CP_LOG_ROOTS", d["error"])

    def test_human_text_gives_the_percentage_only_when_comparable(self):
        d = self._run(["webstats", "--daily", "--file", "0", "--days", "3"])[1]
        text = cli._webdaily_text({**d, "source": "rosetta.access.log"})
        self.assertIn("网站按天流量", text)
        self.assertIn(_date(0), text)
        self.assertIn("上升 66.7%", text)
        self.assertIn("回看：读了 3/3 份世代", text)
        nd = self._run(["webstats", "--daily", "--file", "0", "--days", "7"])[1]
        t2 = cli._webdaily_text({**nd, "source": "rosetta.access.log"})
        self.assertIn("不做环比", t2)
        self.assertIn("往前凑不满一个周期", t2)

    def test_human_text_names_a_vanished_generation(self):
        d = self._run(["webstats", "--daily", "--file", "0", "--days", "3"])[1]
        d = {**d, "source": "rosetta.access.log", "unreadableFiles": 2}
        self.assertIn("2 份世代这次没读到", cli._webdaily_text(d))

    def test_human_text_for_a_log_with_nothing_parseable(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        os.makedirs(self.logs, exist_ok=True)
        self._plain("empty.access.log", ["什么都不是"])
        code, d = self._run(["webstats", "--daily", "--file", "0"])
        self.assertEqual(code, 0, d)
        text = cli._webdaily_text({**d, "source": "empty.access.log"})
        self.assertIn("没读到可解析的请求行", text)

    def test_schema_promises_the_same_numbers_as_the_code(self):
        entry = next(c for c in asyncio.run(cli.cmd_schema(
            cli.build_parser().parse_args(["schema"])))["commands"] if c["name"] == "webstats")
        self.assertEqual(entry["args"], ["--file", "--minutes", "--list", "--daily", "--days"])
        blob = entry["desc"] + " ".join(entry["notes"])
        for needle in (str(A.MAX_DAYS), str(WL.MAX_SERIES_FILES),
                       str(A.DAILY_FILE_BYTES // 1024 // 1024), str(A.MAX_KEYS),
                       str(A.MAX_UV_PER_DAY), "环比", "uvFloored"):
            self.assertIn(needle, blob, f"契约里没写 {needle}，智能体按文档算的预算是假的")

    def test_schema_args_are_options_argparse_really_accepts(self):
        """契约里写的参数必须真能解析出来：写一个不存在的参数比漏写更坏。"""
        entry = next(c for c in asyncio.run(cli.cmd_schema(
            cli.build_parser().parse_args(["schema"])))["commands"] if c["name"] == "webstats")
        for flag in entry["args"]:
            argv = ["webstats", flag] + ([] if flag in ("--daily", "--list") else ["1"])
            if flag == "--days":
                argv += ["--daily"]
            args = cli.build_parser().parse_args(argv)
            self.assertTrue(hasattr(args, flag.lstrip("-").replace("-", "_")), flag)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["webstats", "--weekly"])


class TestAgentsDocRow(unittest.TestCase):
    def test_webstats_row_documents_daily(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        row = [ln for ln in text.splitlines() if ln.startswith("| `webstats`")]
        self.assertEqual(len(row), 1)
        for needle in ("--file", "--minutes", "--list", "--daily", "--days", "环比", "退出码"):
            self.assertIn(needle, row[0], f"AGENTS.md 的 webstats 行缺 {needle}")
        self.assertIn("logrotate", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
