"""批次91：榜单截了一截要说"剩下的算在哪"，账号表没扫完不许说"这台机器没有"。

两处同一个形状——只交出前几名 / 只扫前几千行本身没问题，问题是**少掉的那部分既不在榜上、
也没人说它不在**，于是读的人把它当成全量：

- `access_stats._top_counts` 的文档与 CLI schema 都写着"超出并进「…其他」并计数"，实际只有
  `topClients` 真带了溢出请求数：`methods` 恒传 `extra=0`，第 16 个方法名之后的请求既不上榜
  也不在"其他"里；`topCodes` 更直接，`[:10]` 一刀，第 11 个状态码的请求数从表里蒸发。扫描器
  在 80 端口上刷出来的方法名轻松超过 15 个，这不是假想场景。
- `_top_paths` 的「…其他」那一行字节写死 0，等于在流量列上说"这些路径没发过东西"；键表封顶
  时那份字节在循环里被丢掉了，没人捡。
- `paths`（唯一路径数）压根不在载荷里，前端只能显示客户端数；`clientsFloored` 只在 CLI 说了，
  网页那一栏读起来像精确数。
- `login_audit_service` 读 `/etc/passwd`/`shadow`/`group` 时 `[:4000]` 一刀切完就走：排在第
  4001 行的空口令账号或多余 uid=0 看不见，而自检绿灯与 CLI 的「空口令 0 个 · 额外 uid=0 0 个」
  说的恰恰是"这台机器没有那种账号"。读不到（`None`）在批次75 已经钉过不许当"没有"，
  这里是**读到了但只读了一段**，同一条纪律的另一半。

`ACCOUNTS_MAX_LINES` 只在服务层定义一次：CLI 的 schema 说明、doctor 判词、前端换掉的否定词
都读同一个来源，不各自抄一份 4000。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch91_data_"))

import cli  # noqa: E402
from app.services import access_stats as A  # noqa: E402
from app.services import login_audit_service as L  # noqa: E402

ROOT = BACKEND.parent
BASE_TS = datetime(2026, 10, 30, 9, 0, 0, tzinfo=UTC).timestamp()
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _stamp(ts: float) -> str:
    d = datetime.fromtimestamp(ts, UTC)
    return (f"{d.day:02d}/{_MONTHS[d.month - 1]}/{d.year}:{d.hour:02d}:{d.minute:02d}:{d.second:02d} +0000")


def _line(minutes_ago: int = 0, client: str = "203.0.113.9", method: str = "GET",
          path: str = "/posts", status: int = 200, sent: int = 512) -> str:
    return (f'{client} - - [{_stamp(BASE_TS - minutes_ago * 60)}] '
            f'"{method} {path} HTTP/1.1" {status} {sent} "-" "Mozilla/5.0 (batch91)"')


class _TmpLog(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp91_log_")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = f"{self.d}/access.log"

    def write(self, lines: list[str]) -> None:
        Path(self.p).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TopCountsMergeTests(_TmpLog):
    def test_methods_beyond_the_cap_are_merged_not_lost(self):
        """原来 methods 榜永远不带「…其他」：第 16 名之后的请求既不上榜也没人计数。"""
        methods = ["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH", "TRACE",
                   "PROPFIND", "REPORT", "MKCOL", "COPY", "MOVE", "LOCK", "UNLOCK",
                   "SEARCH", "NOTIFY", "BROWSE"]
        self.write([_line(i, method=m, path=f"/m{i}") for i, m in enumerate(methods)])
        d = A.collect(self.p, 60)
        rows = d["methods"]
        self.assertEqual(rows[-1]["key"], "…其他")
        self.assertEqual(sum(r["count"] for r in rows), d["parsed"], "落榜的请求数必须有一个去处")
        self.assertLessEqual(len(rows) - 1, A.TOP_N)

    def test_codes_beyond_the_shorter_cap_are_merged(self):
        """状态码榜比路径榜短一截，但"短的"不等于"没的"。"""
        codes = [200 + i for i in range(0, 19)]
        self.write([_line(i, status=c, path=f"/c{i}") for i, c in enumerate(codes)])
        d = A.collect(self.p, 60)
        rows = d["topCodes"]
        self.assertLessEqual(len(rows), A.TOP_CODES + 1)
        self.assertEqual(rows[-1]["code"], "…其他")
        self.assertEqual(sum(r["count"] for r in rows), d["parsed"])
        self.assertEqual(sum(d["status"].values()), d["parsed"], "分布表与榜单得是同一批请求")

    def test_no_extra_row_when_everything_fits(self):
        self.write([_line(i, path=f"/a{i}") for i in range(3)])
        d = A.collect(self.p, 60)
        self.assertNotIn("…其他", [r["key"] for r in d["methods"]])
        self.assertNotIn("…其他", [r["path"] for r in d["topPaths"]])
        self.assertNotIn("…其他", [r["code"] for r in d["topCodes"]])


class TopPathsBytesTests(_TmpLog):
    def test_other_row_carries_the_overflow_bytes(self):
        """「…其他」的字节原来写死 0，等于说这些路径没发过东西。"""
        self.write([_line(i, path=f"/p{i}", sent=1000) for i in range(6)])
        with mock.patch.object(A, "TOP_N", 3):
            d = A.collect(self.p, 60)
        rows = d["topPaths"]
        self.assertEqual(rows[-1]["path"], "…其他")
        self.assertEqual(rows[-1]["count"], 3)
        self.assertEqual(rows[-1]["bytes"], 3000)
        self.assertEqual(sum(r["bytes"] for r in rows), d["bytesOut"])

    def test_key_cap_overflow_counts_requests_and_bytes(self):
        """键表封顶后新路径的请求与字节都不该凭空消失。"""
        self.write([_line(i, path=f"/u{i}", client=f"10.0.0.{i}", sent=700) for i in range(6)])
        with mock.patch.object(A, "MAX_KEYS", 2):
            d = A.collect(self.p, 60)
        self.assertEqual(d["paths"], 2)
        self.assertTrue(d["pathsFloored"])
        self.assertTrue(d["clientsFloored"])
        self.assertGreater(d["pathsOverflow"], 0)
        other = d["topPaths"][-1]
        self.assertEqual(other["path"], "…其他")
        self.assertGreater(other["bytes"], 0)
        self.assertEqual(sum(r["count"] for r in d["topPaths"]), d["parsed"],
                         "封顶掉的那份要并进「…其他」，否则榜单加起来比请求总数小")


class PayloadShapeTests(_TmpLog):
    def test_unreadable_payload_has_the_same_keys(self):
        """前端按键渲染：读不动那一份少一个键就是 undefined。"""
        self.write([_line(0)])
        ok_keys = set(A.collect(self.p, 60))
        bad_keys = set(A._unreadable(60))
        self.assertEqual(ok_keys - bad_keys, set(), f"正常载荷多了 {ok_keys - bad_keys} 个键")
        self.assertEqual(bad_keys - ok_keys, {"readError"}, "只有 readError 是读不动那一份特有的")

    def test_new_fields_survive_json(self):
        self.write([_line(0)])
        d = A.collect(self.p, 60)
        blob = json.dumps(d, ensure_ascii=False)
        for key in ("paths", "clientsFloored", "pathsFloored", "pathsOverflow", "clientsOverflow"):
            self.assertIn(key, blob)


class WebstatsTextTests(_TmpLog):
    def test_cli_prints_every_row_it_was_given(self):
        """终端不再自己二次截断：接口给 15 名 + 「…其他」，这里切 10 名就又少一截。"""
        src = Path(cli.__file__).read_text(encoding="utf-8")
        body = src.split("def _webstats_text", 1)[1].split("def cmd_schema", 1)[0]
        self.assertNotIn('[:10]', body, "CLI 又自己截了一遍")
        self.assertIn("clientsFloored", body)

    def test_cli_says_the_client_count_is_a_floor(self):
        self.write([_line(i, client=f"10.0.0.{i}", path=f"/x{i}") for i in range(5)])
        with mock.patch.object(A, "MAX_KEYS", 2):
            d = A.collect(self.p, 60)
        text = cli._webstats_text(d)
        self.assertIn("已达统计键数上限", text)
        self.assertIn("…其他", text)

    def test_schema_promises_the_cap_the_service_uses(self):
        """schema 里那句"超出并进…其他"是承诺，代码没做到就该红，而不是等人发现。"""
        src = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("落榜的请求数并进「…其他」", src)
        self.assertIn("access_stats.TOP_CODES", src, "schema 里的状态码榜长要读服务层的常量，不抄数字")


# ---- 账号表扫描的完整性 ----

USERS = [
    {"name": "root", "uid": 0, "home": "/root", "shell": "/bin/bash", "login": True},
    {"name": "bob", "uid": 1001, "home": "/home/bob", "shell": "/bin/bash", "login": True},
]


def _reader(files: dict[str, str]):
    def fake(path, limit=200000):
        return files.get(path)

    return mock.patch.object(L, "_read_text", fake)


class ReadLinesTests(unittest.TestCase):
    def test_unreadable_is_none_not_empty(self):
        """读不到与读到零行是两件事：后者会说"这台机器没有"。"""
        with _reader({"/etc/passwd": None}):
            self.assertEqual(L._read_lines("/etc/passwd"), (None, False))

    def test_short_file_is_not_partial(self):
        with _reader({"/etc/shadow": "root:x:1:0:::\nbob:x:1:0:::\n"}):
            lines, partial = L._read_lines("/etc/shadow")
        self.assertEqual(len(lines), 2)
        self.assertFalse(partial)

    def test_cap_marks_the_scan_incomplete(self):
        text = "".join(f"u{i}:x:{i}:1::/home/u{i}:/bin/bash\n" for i in range(L.ACCOUNTS_MAX_LINES + 3))
        with _reader({"/etc/group": text}):
            lines, partial = L._read_lines("/etc/group")
        self.assertEqual(len(lines), L.ACCOUNTS_MAX_LINES)
        self.assertTrue(partial)

    def test_char_budget_ending_is_also_an_unfinished_scan(self):
        """`read(limit)` 停在字符上时行数还没触顶，只看行数会把这种截断说成"扫全了"。"""
        text = "".join(f"u{i}:x:{i}:1::/home/u{i}:/bin/bash\n" for i in range(50))
        with mock.patch.object(L, "ACCOUNTS_MAX_CHARS", len(text) - 10), _reader({"/etc/group": text}):
            lines, partial = L._read_lines("/etc/group")
        self.assertTrue(partial, f"字符先触顶却没被认成截断：{len(lines)} 行")


class AccountsPartialTests(unittest.TestCase):
    def test_partial_passwd_is_named_and_complete_is_false(self):
        with _reader({"/etc/shadow": "root:x:1:0:::\n", "/etc/group": "sudo:x:27:bob\n"}):
            acc = L._accounts(USERS, True)
        self.assertEqual(acc["scan_partial"], ["/etc/passwd"])
        self.assertFalse(acc["complete"])
        self.assertTrue(any("/etc/passwd" in n for n in acc["notes"]))
        self.assertIn(f"{L.ACCOUNTS_MAX_LINES} 行", " ".join(acc["notes"]))

    def test_complete_scan_says_so_without_a_note(self):
        with _reader({"/etc/shadow": "root:x:1:0:::\n", "/etc/group": "sudo:x:27:bob\n"}):
            acc = L._accounts(USERS)
        self.assertTrue(acc["complete"])
        self.assertEqual(acc["scan_partial"], [])
        self.assertEqual(acc["notes"], [])

    def test_every_truncated_file_is_named(self):
        """三份表各自可能更长，只点一个就等于把另外两份说成扫全了。"""
        long_group = "u:x:1:\n" * (L.ACCOUNTS_MAX_LINES + 1)
        with _reader({"/etc/shadow": "root:x:1:0:::\n", "/etc/group": long_group}):
            acc = L._accounts(USERS, True)
        self.assertEqual(acc["scan_partial"], ["/etc/passwd", "/etc/group"])

    def test_negative_conclusion_is_scoped_to_what_was_read(self):
        with _reader({"/etc/shadow": "root:x:1:0:::\n", "/etc/group": "sudo:x:27:\n"}):
            acc = L._accounts(USERS, True)
        self.assertEqual(acc["empty_password"], [])
        sentence = " ".join(acc["notes"])
        self.assertIn("「没有空口令账号 / 没有多余 uid=0」只对已扫过的这一段成立", sentence)


class DoctorItemTests(unittest.TestCase):
    def data(self, **kw):
        acc = {"login_shells": ["root", "bob"], "login_count": 2, "uid0_extra": [],
               "empty_password": [], "shadow_unreadable": False, "sudo_groups": {"sudo": ["bob"]},
               "scan_partial": kw.get("partial", []), "complete": not kw.get("partial"),
               "notes": []}
        return {
            "sshd": {"source": "sshd -T", "port": 22, "reachable": False},
            "counts": {"risk": 0, "warn": 0, "note": 0},
            "findings": [],
            "accounts": acc,
            "guard": {"status": "guarded", "phrase": "在跑"},
            "attempts": {"ok": True, "failed": 3, "truncated": False},
            "notes": [],
            "as_root": True,
        }

    def test_incomplete_scan_is_not_a_green_light(self):
        got = L.doctor_item(self.data(partial=["/etc/shadow"]))
        self.assertEqual(got["status"], "warn")
        self.assertIn(f"{L.ACCOUNTS_MAX_LINES} 行", got["detail"])
        self.assertIn("/etc/shadow", got["detail"])

    def test_complete_scan_is_still_green(self):
        self.assertEqual(L.doctor_item(self.data())["status"], "pass")

    def test_fixture_without_the_new_key_is_not_downgraded(self):
        """老判词里没有 complete 这一键（批次89 那批替身就是），不能被当成"没扫完"。"""
        d = self.data()
        del d["accounts"]["complete"]
        del d["accounts"]["scan_partial"]
        self.assertEqual(L.doctor_item(d)["status"], "pass")

    def test_breaking_in_is_still_fail_before_the_partial_note(self):
        d = self.data(partial=["/etc/passwd"])
        d["accounts"]["uid0_extra"] = ["ghost"]
        self.assertEqual(L.doctor_item(d)["status"], "fail")


class CollectWiringTests(unittest.TestCase):
    def test_passwd_partial_reaches_the_accounts_block(self):
        """读出来的一路传到写出去的那一份：中间丢掉就等于整页仍然说"扫全了"。"""
        ssh = {"source": "sshd -T", "effective": True, "values": {}, "notes": []}
        guard = {"status": "guarded", "phrase": "在跑", "units": [], "note": None}
        att = {"ok": True, "failed": 0, "distinct_ips": 0, "top": [], "accepted": [],
               "accepted_ips": [], "truncated": False, "note": None}
        keys = {"files": [], "scanned": 0, "total_keys": 0, "truncated": False}
        panel = {"ok": True, "accounts": 0, "with_totp": 0, "recent_failed": 0, "note": None}

        async def run_ssh():
            return ssh

        async def run_guard():
            return guard

        async def run_att():
            return att

        async def ports():
            return {"ports": []}

        with mock.patch.object(L, "_sshd_settings", run_ssh), \
            mock.patch.object(L, "_passwd_users", lambda: ([], True)), \
            mock.patch.object(L, "_guard_status", run_guard), \
            mock.patch.object(L, "_attempts", run_att), \
            mock.patch.object(L, "_panel_2fa", lambda: panel), \
            mock.patch.object(L, "_authorized_keys", lambda users, as_root: keys), \
            mock.patch.object(L.exposure_service, "audit", ports):
            got = asyncio.run(L._collect())
        self.assertFalse(got["accounts"]["complete"])
        self.assertEqual(got["accounts"]["scan_partial"], ["/etc/passwd"])
        self.assertTrue(any("账号表" in n for n in got["notes"]), "说明要进整页的 notes")


class SshAuditTextTests(unittest.TestCase):
    def test_account_line_declares_an_unfinished_scan(self):
        src = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("账号表没扫完", src)
        self.assertIn('acc.get("scan_partial")', src)


class FrontendContractTests(unittest.TestCase):
    """后端测试把前端源码当契约输入：这几行改坏了，页面就又把下限念成精确数。"""

    def test_log_center_uses_the_new_flags(self):
        src = (ROOT / "web" / "src" / "views" / "LogCenter.vue").read_text(encoding="utf-8")
        self.assertIn("d.pathsFloored", src)
        self.assertIn("d.clientsFloored", src)
        self.assertIn("d.paths", src)
        self.assertIn("次请求的客户端没单独计数", src, "溢出数的是请求次数，不是人数")

    def test_ssh_audit_page_does_not_claim_a_clean_read_of_an_unscanned_range(self):
        src = (ROOT / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")
        self.assertIn("scan_partial", src)
        self.assertIn("accNeg('/etc/shadow')", src)
        self.assertIn("accNeg('/etc/passwd')", src)
        self.assertNotIn("uid0_extra.join('、') : '没有'", src, "扫不完时这句是否定不了整台机器的")


if __name__ == "__main__":
    unittest.main()
