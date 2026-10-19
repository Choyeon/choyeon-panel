"""批次㊹：nginx 访问日志的来源发现与流量统计。

定这一批的动机是两个都能在页面上看见的洞，不是"想加个功能"：

1. **全新安装的「访问/错误日志」页签永远是空的。** 来源发现只扫 `CP_FILE_ROOTS`，而线上
   nginx 的日志在 `/var/log/nginx`——它不在默认白名单里，也不该为了看日志被塞进文件管理页
   的白名单（那是"人能打开哪些目录"）。更要命的是发现只认 `access.log`/`error.log` 两个裸名字，
   按站点分开的 `rosetta.access.log` 一个都认不出来，而这才是这台机器上真正有人看的日志。
2. **没有任何流量可见性。** 1Panel/宝塔都有网站监控；面板能看 CPU、看磁盘、看端口，
   却答不出"这个站点今天被访问了多少次、哪个地址在被刷、5xx 从什么时候开始涨"。

因为读日志的代码跑在 **root** 进程里，测试按危险面排，不按功能排：

- **白名单只有一个出处**：`web_logs` 是发现与尾读的唯一实现，路由不许再抄一份字节上限；
- **越界一律挡在外面**：符号链接逃出的目录、非普通文件（fifo 会把线程池的 read 挂死）、
  轮替压缩件（gzip 按字节 tail 只会吐乱码）；
- **上限是真的生效**：尾读字节、行数、聚合键数、曲线桶数，每一项都用打桩的上限验一遍，
  并且超出部分要在结果里如实报数，而不是静默少算；
- **统计只说窗口内的事**：尾读必然混着比窗口更早的行，窗口外的行数单独报 `outsideWindow`；
- **不外泄**：查询串（token 常在这里）与原始行都不出现在响应里，绝对路径也不贴给浏览器。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch44_data_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

import cli  # noqa: E402
from app import (  # noqa: E402
    config,  # noqa: E402
    main,
    security,
)
from app import database as dbm  # noqa: E402
from app.services import access_stats as A  # noqa: E402
from app.services import web_logs as WL  # noqa: E402

ROOT = Path(config.BASE)
_seq = iter(range(1000, 999999))
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BASE_TS = datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC).timestamp()


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _stamp(ts: float, offset: str = "+0000") -> str:
    """nginx 时间戳，手工拼月份名。

    不用 `strftime("%d/%b/%Y…")`：`%b` 跟着 C 库的当前 locale，测试机上换成中文 locale
    就会写出解析不出来的时间戳，表现为"统计里全是坏行"——和被测代码的错一模一样难查。
    """
    d = datetime.fromtimestamp(ts, UTC)
    return f"{d.day:02d}/{_MONTHS[d.month - 1]}/{d.year}:{d.hour:02d}:{d.minute:02d}:{d.second:02d} {offset}"


def _line(minutes_ago: int = 0, client: str = "203.0.113.9", method: str = "GET",
          path: str = "/posts", status: int = 200, sent: int = 512, query: str = "",
          referer: bool = True) -> str:
    head = (f'{client} - - [{_stamp(BASE_TS - minutes_ago * 60)}] '
            f'"{method} {path}{query} HTTP/1.1" {status} {sent}')
    return head + (' "-" "Mozilla/5.0 (batch44)"' if referer else "")


def _write(path: str, lines: list[str]) -> None:
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestNameMatching(unittest.TestCase):
    """文件名规则：认出按站点分开的日志，但不要压缩件与别的后缀。"""

    def test_accepted_names(self):
        for name, kind, site in (("access.log", "access", None),
                                 ("error.log", "error", None),
                                 ("rosetta.access.log", "access", "rosetta"),
                                 ("vl-choyeon.error.log", "error", "vl-choyeon"),
                                 ("rosetta.access.log.1", "access", "rosetta")):
            m = WL.LOG_NAME_RE.match(name)
            self.assertIsNotNone(m, f"认不出 {name}：这一条就是「日志页签是空的」的根因")
            self.assertEqual(m.group("kind"), kind)
            self.assertEqual(m.group("site"), site)

    def test_rejected_names(self):
        # .gz 不收：tail 与统计都按字节读末尾再解码，gzip 内容只会变成坏行
        for name in ("access.log.gz", "access.log.2", "notalog.log", "access.txt",
                     "rosetta.access.log.gz", "my.access.log.bak", ""):
            self.assertIsNone(WL.LOG_NAME_RE.match(name), f"{name} 不该被当成日志")

    def test_long_site_name_is_not_matched(self):
        self.assertIsNone(WL.LOG_NAME_RE.match("a" * 90 + ".access.log"))


class TestRootsUnion(unittest.TestCase):
    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="cp44_logs_")
        self.files = tempfile.mkdtemp(prefix="cp44_files_")
        self.outside = tempfile.mkdtemp(prefix="cp44_out_")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.addCleanup(shutil.rmtree, self.files, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        self.patch = mock.patch.object(config, "LOG_ROOTS", [self.logs])
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.p2 = mock.patch.object(config, "FILE_ROOTS", [self.files])
        self.p2.start()
        self.addCleanup(self.p2.stop)

    def _labels(self) -> set[str]:
        return {f["label"] for f in WL.nginx_log_files()}

    def test_log_root_is_scanned_without_widening_file_root(self):
        """CP_LOG_ROOTS 让 nginx 的默认位置可读，但文件管理页的白名单一个字节都没变大。"""
        _write(f"{self.logs}/rosetta.access.log", [_line()])
        self.assertIn(f"{os.path.basename(self.logs)}/rosetta.access.log", self._labels())
        self.assertEqual(config.FILE_ROOTS, [self.files])

    def test_file_root_still_works(self):
        os.makedirs(f"{self.files}/myapp")
        _write(f"{self.files}/myapp/access.log", [_line()])
        self.assertIn("myapp/access.log", self._labels())

    def test_roots_are_deduped_in_order(self):
        same = self.logs
        with mock.patch.object(config, "FILE_ROOTS", [same, self.files]):
            self.assertEqual(WL.log_roots(), [same, self.files])

    def test_outside_files_are_invisible(self):
        _write(f"{self.outside}/access.log", ["SECRET-OUTSIDE-LINE\n"])
        paths = [f["path"] for f in WL.nginx_log_files()]
        self.assertNotIn(f"{self.outside}/access.log", paths)

    def test_symlink_escape_is_skipped(self):
        _write(f"{self.logs}/access.log", [_line()])
        _write(f"{self.outside}/error.log", ["SECRET-OUTSIDE-LINE\n"])
        try:
            os.symlink(f"{self.outside}/error.log", f"{self.logs}/site.error.log")
        except (OSError, NotImplementedError):
            self.skipTest("平台不支持符号链接")
        paths = [f["path"] for f in WL.nginx_log_files()]
        self.assertNotIn(f"{self.outside}/error.log", paths)
        self.assertTrue(all("site.error.log" not in p for p in paths))

    def test_directory_named_like_a_log_is_not_a_source(self):
        """目录名撞上规则时不能被列出：tail 一个目录是 IsADirectoryError，
        统计一个目录是 500，都不是"这个来源没法看"的正确答案。"""
        os.makedirs(f"{self.logs}/access.log")
        self.assertEqual(self._labels(), set())

    def test_non_regular_file_is_skipped(self):
        """fifo 的 read() 会一直等：tail 跑在线程池里，等住的就是进程的并发额度。"""
        if not hasattr(os, "mkfifo"):
            self.skipTest("平台不支持 fifo")
        os.mkfifo(f"{self.logs}/access.log")
        self.addCleanup(os.unlink, f"{self.logs}/access.log")
        self.assertEqual(WL.nginx_log_files(), [])

    def test_indexes_are_stable_and_shared(self):
        _write(f"{self.logs}/b.access.log", [_line()])
        _write(f"{self.logs}/a.access.log", [_line()])
        files = WL.nginx_log_files()
        self.assertEqual([f["label"] for f in files], sorted(f["label"] for f in files))
        # 第二次发现必须给同样的顺序，否则下拉框序号与统计序号会错位
        self.assertEqual([f["path"] for f in WL.nginx_log_files()],
                         [f["path"] for f in files])

    def test_max_files_cap(self):
        for i in range(6):
            _write(f"{self.logs}/s{i}.access.log", [_line()])
        with mock.patch.object(WL, "MAX_LOG_FILES", 3):
            self.assertEqual(len(WL.nginx_log_files()), 3)
        self.assertEqual(len(WL.nginx_log_files()), 6)

    def test_hint_names_the_keys_but_not_the_paths(self):
        hint = WL.discovery_hint()
        for key in ("CP_LOG_ROOTS", "CP_FILE_ROOTS", "upgrade --no-pull"):
            self.assertIn(key, hint)
        self.assertNotIn(self.logs, hint)
        self.assertNotIn(self.files, hint)


class TestTailFile(unittest.TestCase):
    def test_tail_is_bounded_and_redacted(self):
        d = tempfile.mkdtemp(prefix="cp44_tail_")
        self.addCleanup(shutil.rmtree, d, True)
        p = f"{d}/access.log"
        _write(p, [f"line{i} " + _line(client="10.0.0.1", path=f"/x{i}") for i in range(1, 101)])
        out = WL.tail_file(p, 5, "", 1500)
        # 批次61 起返回四个数分开的 dict：items/matched/dropped/window_truncated
        self.assertEqual(len(out["items"]), 5)
        self.assertIn("line100", out["items"][-1])
        self.assertEqual((out["matched"], out["dropped"]), (100, 95))
        self.assertFalse(out["window_truncated"])
        # 上游脱敏口径不变：日志行里的凭据要遮住
        _write(p, ["x - - [16/Oct/2026:09:00:00 +0000] \"GET https://bob:S3cr3tT0ken@h/ HTTP/1.1\" 200 1"])
        self.assertNotIn("S3cr3tT0ken", WL.tail_file(p, 5, "", 1500)["items"][0])

    def test_filter_and_char_cap(self):
        d = tempfile.mkdtemp(prefix="cp44_tail2_")
        self.addCleanup(shutil.rmtree, d, True)
        p = f"{d}/access.log"
        _write(p, [_line(client="1.1.1.1"), _line(client="2.2.2.2"), _line(client="1.1.1.1")])
        self.assertEqual(len(WL.tail_file(p, 50, "1.1.1.1", 1500)["items"]), 2)
        self.assertTrue(all(len(x) <= 40 for x in WL.tail_file(p, 50, "", 40)["items"]))


class TestTimeAndRequestParsing(unittest.TestCase):
    def test_known_timestamp(self):
        self.assertEqual(A._parse_time(_stamp(BASE_TS)), BASE_TS)

    def test_month_is_not_locale_dependent_and_case_tolerant(self):
        self.assertEqual(A._parse_time("16/oct/2026:09:00:00 +0000"), BASE_TS)
        self.assertIsNone(A._parse_time("16/Oxt/2026:09:00:00 +0000"))
        self.assertIsNone(A._parse_time("2026-10-16 09:00:00"))

    def test_offset_is_applied_not_ignored(self):
        east = A._parse_time("16/Oct/2026:09:00:00 +0800")
        utc = A._parse_time("16/Oct/2026:09:00:00 +0000")
        self.assertAlmostEqual(east + 8 * 3600, utc, delta=1)

    def test_query_and_fragment_are_dropped(self):
        method, path = A._split_request("GET /posts?token=abc123#frag HTTP/1.1")
        self.assertEqual((method, path), ("GET", "/posts"))

    def test_proxy_form_and_star_and_connect(self):
        self.assertEqual(A._split_request('GET http://example.com/a/b?x=1 HTTP/1.1'),
                         ("GET", "/a/b"))
        self.assertEqual(A._split_request("OPTIONS * HTTP/1.1"), ("OPTIONS", "*"))
        self.assertEqual(A._split_request("CONNECT example.com:443 HTTP/1.1"),
                         ("CONNECT", "example.com:443"))

    def test_broken_request_line_does_not_raise(self):
        self.assertEqual(A._split_request("GET"), ("GET", ""))
        self.assertEqual(A._split_request(""), ("-", ""))

    def test_path_is_capped(self):
        _, path = A._split_request("GET /" + "a" * 400 + " HTTP/1.1")
        self.assertEqual(len(path), A.PATH_CHARS)

    def test_combined_and_common_both_parse(self):
        for line in (_line(referer=True), _line(referer=False)):
            m = A._LINE_RE.match(line)
            self.assertIsNotNone(m, "两种 log_format 都要认，否则整份日志都是坏行")


class TestReadTail(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp44_read_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = f"{self.d}/access.log"

    def test_small_file_is_not_reported_as_truncated(self):
        """回归：判据写成 `size > start` 时，每个非空小文件都自称"只看了尾部"。"""
        _write(self.p, [_line(i) for i in range(3)])
        lines, read_bytes, truncated = A.read_tail(self.p)
        self.assertEqual(len(lines), 3)
        self.assertEqual(read_bytes, os.path.getsize(self.p))
        self.assertFalse(truncated)

    def test_large_file_is_truncated_and_partial_line_dropped(self):
        _write(self.p, ["x" * 40 for _ in range(4)])
        with mock.patch.object(A, "MAX_BYTES", 45):
            lines, read_bytes, truncated = A.read_tail(self.p)
        self.assertTrue(truncated)
        self.assertLessEqual(read_bytes, 45)
        self.assertTrue(all(len(x) in (0, 40) for x in lines), f"半行没丢掉：{[len(x) for x in lines]}")


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp44_collect_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = f"{self.d}/access.log"

    def test_window_is_a_real_window(self):
        _write(self.p, [_line(m) for m in (0, 30, 59, 60, 61, 120)])
        d = A.collect(self.p, 60)
        self.assertEqual(d["parsed"], 4, "最新一行往回 60 分钟：整 60 分钟那条踩在边界上算进来，"
                                        "61 与 120 分钟那两条不算")
        self.assertEqual(d["outsideWindow"], 2)
        self.assertEqual(d["windowMinutes"], 60)
        self.assertTrue(d["from"] and d["to"])

    def test_counts_only_include_window(self):
        _write(self.p, [_line(0, status=500), _line(120, status=200)])
        d = A.collect(self.p, 60)
        self.assertEqual(d["status"]["5xx"], 1)
        self.assertEqual(d["status"]["2xx"], 0, "窗口外的 200 混进窗口就等于报了一个不存在的数")
        self.assertEqual(sum(d["status"].values()), d["parsed"])

    def test_empty_file_reports_zeroes_instead_of_crashing(self):
        _write(self.p, [])
        d = A.collect(self.p, 60)
        self.assertEqual(d["parsed"], 0)
        self.assertEqual(d["series"], [])
        self.assertEqual(d["topPaths"], [])
        self.assertEqual(d["from"], "")

    def test_malformed_lines_are_counted_not_silently_dropped(self):
        _write(self.p, [_line(0), "这是中文说明行", "", "bad - - [ nope ] \"GET / HTTP/1.1\" x y"])
        d = A.collect(self.p, 60)
        self.assertEqual(d["parsed"], 1)
        self.assertEqual(d["malformed"], 2, "空行不算坏行，其余解析不出来的必须数出来")

    def test_bytes_out_counts_every_line_in_window(self):
        _write(self.p, [_line(i, sent=1000, path=f"/p{i}") for i in range(4)])
        d = A.collect(self.p, 60)
        self.assertEqual(d["bytesOut"], 4000)

    def test_key_overflow_is_reported(self):
        _write(self.p, [_line(i, path=f"/u{i}", client=f"10.0.0.{i}") for i in range(5)])
        with mock.patch.object(A, "MAX_KEYS", 2):
            d = A.collect(self.p, 60)
        self.assertEqual(d["clients"], 2)
        self.assertGreater(d["clientsOverflow"], 0)
        self.assertGreater(d["pathsOverflow"], 0)
        self.assertEqual(d["topClients"][-1]["key"], "…其他")
        self.assertEqual(d["topPaths"][-1]["path"], "…其他")

    def test_line_cap_marks_truncated_and_limits_scanned(self):
        _write(self.p, [_line(i) for i in range(50)])
        with mock.patch.object(A, "MAX_LINES", 10):
            d = A.collect(self.p, 60)
        self.assertEqual(d["scannedLines"], 10)
        self.assertTrue(d["truncatedRead"])

    def test_series_is_bounded_by_window(self):
        _write(self.p, [_line(i * 3) for i in range(20)])
        d = A.collect(self.p, 5)
        self.assertLessEqual(len(d["series"]), 5)
        self.assertTrue(all(b["count"] >= 1 for b in d["series"]))

    def test_minutes_is_clamped(self):
        _write(self.p, [_line()])
        self.assertEqual(A.collect(self.p, 99999)["windowMinutes"], A.SERIES_MAX)
        self.assertEqual(A.collect(self.p, 0)["windowMinutes"], 1)

    def test_control_chars_never_reach_an_aggregation_key(self):
        """扫描器把 TLS 记录打到 80 端口时，nginx 会原样记一行；榜单里不能是乱码。

        线上复现过：`\\x16\\x03\\x01\\x05` 被当成"请求方法"画进方法榜，
        375px 上是一串看不见却占位的字符。抹掉控制字符后，读不出就标 "-"。
        """
        self.assertEqual(A._split_request("\x16\x03\x01\x05\x00\x01\x00\x00\x11"), ("-", ""))
        self.assertEqual(A._split_request("GET /a\tb HTTP/1.1"), ("GET", "/ab"))
        tls_probe = "\x16\x03\x01\x05\x00\x01"
        _write(self.p, [_line(0),
                        f'10.0.0.1 - - [{_stamp(BASE_TS)}] "{tls_probe}" 400 0 "-" "-"'])
        d = A.collect(self.p, 60)
        self.assertEqual(sorted(k for k in (m["key"] for m in d["methods"])), ["-", "GET"])
        self.assertNotIn("\x16", json.dumps(d))

    def test_nginx_hex_escapes_are_unescaped_not_kept(self):
        r"""nginx 落盘的是 `\x16` 这六个字符，不是 0x16 这一个字节——线上日志的真形态。

        上一版只抹裸字节，于是方法榜里出现字面 `\x16\x03\x01\x05`（375px 实测）。
        现在先按 `\xHH` 还原：不可打印档删掉，可打印档（引号 `\x22`）回到原字符。
        """
        esc = r"\x16\x03\x00\x00\xE8\x01\x00\x00\xE4\x03\x00"
        self.assertEqual(A._split_request(esc), ("-", ""))
        self.assertEqual(A._unescape_nginx(r"a\x22b\x1fc\xe8d"), 'a"bcd')
        _write(self.p, [_line(0),
                        f'10.0.0.9 - - [{_stamp(BASE_TS)}] "{esc}" 400 150 "-" "-"',
                        _line(1, method="OPTIONS", path="*")])
        d = A.collect(self.p, 60)
        self.assertEqual(sorted(m["key"] for m in d["methods"]), ["-", "GET", "OPTIONS"])
        self.assertNotIn("\\x", json.dumps(d))

    def test_method_key_needs_a_real_method_name(self):
        """方法位只收 3-16 个字母：数字、标点、单两个字母都是扫描残片，一律读不出。"""
        for tok in ("G", "PO", "1.1", "GET1", "PROPFIND", "REPORT", ""):
            want = tok if len(tok) >= 3 and tok.isalpha() else "-"
            self.assertEqual(A._method_key(tok), want, tok)

    def test_top_codes_and_methods_shape(self):
        _write(self.p, [_line(0, status=200), _line(1, status=200), _line(2, status=404),
                        _line(3, status=200, method="POST")])
        d = A.collect(self.p, 60)
        self.assertEqual(d["topCodes"][0]["code"], "200")
        self.assertEqual({m["key"] for m in d["methods"]}, {"GET", "POST"})

    def test_no_raw_line_or_query_leaks_into_result(self):
        _write(self.p, [_line(0, path="/pay", query="?token=S3cr3tT0ken&phone=13800000000")])
        d = A.collect(self.p, 60)
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("S3cr3tT0ken", blob)
        self.assertNotIn("13800000000", blob)
        self.assertNotIn("Mozilla", blob, "统计不返回原始行：榜单要的是路径，不是整行日志")


class TestLogOptionsStillHasOneSource(unittest.TestCase):
    def test_router_does_not_copy_the_byte_cap(self):
        """尾读上限只能在 web_logs 里有一份。

        路由里再写一个 `TAIL_BYTES = 512 * 1024`，页面文案与真实截断长度就会在
        某次改动后各说一套——批次㉑ 收口的正是这类"五处副本"。
        """
        src = (ROOT / "backend" / "app" / "routers" / "system.py").read_text(encoding="utf-8")
        self.assertNotIn("TAIL_BYTES = ", src)
        self.assertNotIn("NGINX_LOG_NAMES", src)
        self.assertIn("web_logs.TAIL_BYTES", src)


class TestStatsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.admin = _uid("b44a")
        cls.viewer = _uid("b44v")
        for name, role in ((cls.admin, "admin"), (cls.viewer, "viewer")):
            dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                        (name, security.hash_pass("Password123"), role))
        cls.hdr = {"Authorization": f"Bearer {security.sign_token(cls.admin, 'admin')}"}
        cls.vhdr = {"Authorization": f"Bearer {security.sign_token(cls.viewer, 'viewer')}"}

    @classmethod
    def tearDownClass(cls):
        for name in (cls.admin, cls.viewer):
            dbm.execute("DELETE FROM users WHERE username=?", (name,))

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="cp44_api_logs_")
        self.outside = tempfile.mkdtemp(prefix="cp44_api_out_")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.addCleanup(shutil.rmtree, self.outside, True)
        for p in (mock.patch.object(config, "LOG_ROOTS", [self.logs]),
                  mock.patch.object(config, "FILE_ROOTS", [])):
            p.start()
            self.addCleanup(p.stop)
        _write(f"{self.logs}/rosetta.access.log",
               [_line(m, path="/pay", query="?token=S3cr3tT0ken") for m in (0, 5, 30)]
               + [_line(10, status=500), "不是日志的一行"])
        _write(f"{self.logs}/rosetta.error.log", ["[error] boom"])
        _write(f"{self.outside}/access.log", ["SECRET-OUTSIDE-LINE"])

    def _files(self) -> list[dict]:
        r = self.client.get("/api/system/nginx-log?lines=1", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["files"]

    def test_viewer_and_anonymous_are_refused(self):
        self.assertEqual(self.client.get("/api/system/nginx-stats", headers=self.vhdr).status_code, 403)
        self.assertEqual(self.client.get("/api/system/nginx-stats").status_code, 401)

    def test_index_space_is_shared_with_the_tail_endpoint(self):
        files = self._files()
        self.assertEqual([f["label"] for f in files],
                         [f["label"] for f in self.client.get(
                             "/api/system/nginx-stats?file=0", headers=self.hdr).json()["files"]])

    def test_stats_shape_and_numbers(self):
        r = self.client.get("/api/system/nginx-stats?file=0&minutes=60", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["parsed"], 4)
        self.assertEqual(d["malformed"], 1)
        self.assertEqual(sum(d["status"].values()), 4)
        self.assertTrue(d["source"].endswith("/rosetta.access.log"))
        self.assertEqual(d["site"], "rosetta")
        self.assertTrue(d["series"])

    def test_absent_file_picks_an_access_log_and_says_which(self):
        """页面第一枪不带 file：要落在"能统计"的来源上，并回写实际序号。

        否则下拉还没清单时选中 error.log，用户看到的就是"点了统计却报 400"。
        """
        _write(f"{self.logs}/a-first.error.log", ["[error] sorts before access logs"])
        r = self.client.get("/api/system/nginx-stats", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["files"][d["file"]]["kind"], "access")
        self.assertTrue(d["source"].endswith("/rosetta.access.log"))
        self.assertGreater(d["parsed"], 0)

    def test_only_error_logs_is_a_state_not_a_crash(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        os.makedirs(self.logs, exist_ok=True)
        _write(f"{self.logs}/site.error.log", ["[error] 只有错误日志"])
        d = self.client.get("/api/system/nginx-stats", headers=self.hdr).json()
        self.assertEqual(d["source"], "")
        self.assertIn("访问日志", d["hint"])
        self.assertEqual([f["kind"] for f in d["files"]], ["error"])

    def test_no_absolute_path_and_no_raw_line_reaches_the_browser(self):
        r = self.client.get("/api/system/nginx-stats?file=0", headers=self.hdr)
        self.assertNotIn(self.logs, r.text)
        self.assertNotIn("S3cr3tT0ken", r.text)
        self.assertNotIn("Mozilla", r.text)

    def test_error_log_is_a_client_error_naming_the_alternatives(self):
        files = self._files()
        err_idx = next(i for i, f in enumerate(files) if f["kind"] == "error")
        r = self.client.get(f"/api/system/nginx-stats?file={err_idx}", headers=self.hdr)
        self.assertEqual(r.status_code, 400)
        self.assertIn("错误日志", r.json()["error"])
        self.assertIn("访问日志", r.json()["error"])

    def test_bad_arguments(self):
        for q in ("file=x", "minutes=x", "minutes=0", "minutes=1441", "file=99"):
            r = self.client.get(f"/api/system/nginx-stats?{q}", headers=self.hdr)
            self.assertEqual(r.status_code, 400, f"{q} 应该被拒：{r.text}")

    def test_no_sources_returns_hint_not_an_error(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        r = self.client.get("/api/system/nginx-stats", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["files"], [])
        self.assertIn("CP_LOG_ROOTS", d["hint"])
        self.assertNotIn(self.outside, r.text)

    def test_tail_endpoint_uses_the_same_discovery(self):
        r = self.client.get("/api/system/nginx-log?lines=10", headers=self.hdr)
        d = r.json()
        self.assertEqual(d["source"], os.path.basename(self.logs) + "/rosetta.access.log")
        self.assertNotIn("SECRET-OUTSIDE-LINE", r.text)

    def test_window_ladder_comes_from_the_same_place_as_the_cap(self):
        """「统计窗口」下拉的档位必须由服务端下发，且落在 SERIES_MAX 之内。

        前端自己抄一份 15/30/60…，SERIES_MAX 一改就会出现「选了 24 小时、服务端按 720 截」
        这类安静失真（批次㉑/㉒B 收口过同一种病）。
        """
        ladder = A.WINDOW_LADDER
        self.assertEqual(ladder, sorted(set(ladder)))
        self.assertTrue(all(1 <= n <= A.SERIES_MAX for n in ladder), ladder)
        self.assertIn(A.SERIES_MAX, ladder, "上限本身必须可选，否则用户永远够不到")
        self.assertIn(60, ladder, "默认 60 分钟必须能在下拉里被选中并回显")
        body = self.client.get("/api/system/log-options", headers=self.hdr).json()
        self.assertEqual(body["stats_minutes"], ladder)


class TestCliWebstats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = _uid("b44c")
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (cls.admin, security.hash_pass("Password123"), "admin"))

    @classmethod
    def tearDownClass(cls):
        dbm.execute("DELETE FROM users WHERE username=?", (cls.admin,))

    def setUp(self):
        self.logs = tempfile.mkdtemp(prefix="cp44_cli_logs_")
        self.addCleanup(shutil.rmtree, self.logs, True)
        for p in (mock.patch.object(config, "LOG_ROOTS", [self.logs]),
                  mock.patch.object(config, "FILE_ROOTS", [])):
            p.start()
            self.addCleanup(p.stop)
        _write(f"{self.logs}/site.access.log", [_line(m) for m in (0, 10, 40)])
        _write(f"{self.logs}/site.error.log", ["[error] boom"])
        self.cli = cli
        self._json_mode = cli.Out.json_mode

    def tearDown(self):
        self.cli.Out.json_mode = self._json_mode

    def _run(self, argv: list[str]):
        """跑一次只读命令，返回 (退出码, 解析出的 JSON)。"""
        self.cli.Out.json_mode = True
        buf = io.StringIO()
        code = 0
        try:
            args = self.cli.build_parser().parse_args(argv)
            with contextlib.redirect_stdout(buf):
                asyncio.run(args.fn(args))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            self.cli.Out.json_mode = self._json_mode
        out = buf.getvalue()
        return code, (json.loads(out) if out.strip().startswith("{") else {"raw": out})

    def test_parser_defaults_and_readonly_help(self):
        args = self.cli.build_parser().parse_args(["webstats"])
        self.assertIs(args.fn, self.cli.cmd_webstats)
        self.assertEqual((args.file, args.minutes), ("0", "60"))
        self.assertFalse(args.list)
        row = [ln for ln in self.cli.build_parser().format_help().splitlines()
               if ln.strip().startswith("webstats")]
        self.assertEqual(len(row), 1)
        self.assertIn("只读", row[0])

    def test_list_reports_the_shared_index_and_paths(self):
        code, d = self._run(["webstats", "--list"])
        self.assertEqual(code, 0)
        self.assertEqual([f["label"] for f in d["files"]],
                         [f["label"] for f in WL.nginx_log_files()])
        self.assertIn(self.logs, json.dumps(d))  # root 终端里给绝对路径是有用的

    def test_stats_and_window(self):
        code, d = self._run(["webstats", "--file", "0", "--minutes", "30"])
        self.assertEqual(code, 0, d)
        self.assertEqual(d["parsed"], 2, "30 分钟窗口只该有 0 分钟与 10 分钟那两行")
        self.assertEqual(d["outsideWindow"], 1)

    def test_error_log_is_usage_error(self):
        code, d = self._run(["webstats", "--file", "1"])
        self.assertEqual(code, self.cli.EXIT_USAGE, d)
        self.assertIn("错误日志", d["error"])

    def test_bad_arguments_are_usage_errors(self):
        for argv in (["webstats", "--file", "x"], ["webstats", "--minutes", "abc"],
                     ["webstats", "--minutes", "0"], ["webstats", "--minutes", "99999"],
                     ["webstats", "--file", "77"]):
            code, d = self._run(argv)
            self.assertEqual(code, self.cli.EXIT_USAGE, f"{argv} → {d}")

    def test_no_sources_is_a_precondition_failure_with_the_hint(self):
        shutil.rmtree(self.logs, ignore_errors=True)
        code, d = self._run(["webstats"])
        self.assertEqual(code, self.cli.EXIT_PRECONDITION)
        self.assertIn("CP_LOG_ROOTS", d["error"])
        code2, _ = self._run(["webstats", "--list"])
        self.assertEqual(code2, self.cli.EXIT_PRECONDITION)

    def test_human_text_is_honest_about_the_window(self):
        d = A.collect(f"{self.logs}/site.access.log", 30)
        text = self.cli._webstats_text({**d, "source": "site.access.log"})
        self.assertIn("窗口 30 分钟", text)
        self.assertIn("另有 1 行未计入", text)
        empty = self.cli._webstats_text({**A.collect(f"{self.logs}/site.error.log", 30),
                                         "source": "x"})
        self.assertIn("没有可解析的请求行", empty)

    def test_schema_promises_the_same_limits_as_the_code(self):
        """契约里写的上限必须等于代码里的常量，否则智能体按文档算的预算是假的。"""
        entry = next(c for c in asyncio.run(self.cli.cmd_schema(
            self.cli.build_parser().parse_args(["schema"])))["commands"] if c["name"] == "webstats")
        blob = entry["desc"] + " ".join(entry["notes"])
        self.assertIn(str(A.MAX_BYTES // 1024 // 1024), blob)
        self.assertIn(str(A.MAX_LINES), blob)
        self.assertIn(str(A.MAX_KEYS), blob)
        for phrase in ("只读", "CP_LOG_ROOTS", "truncatedRead", "退出码"):
            self.assertIn(phrase, blob)


if __name__ == "__main__":
    unittest.main()
