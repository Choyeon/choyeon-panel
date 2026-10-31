"""批次85：认证日志的行数上限不许冒充「过去 24 小时」。

`journalctl --since '24 hours ago' -n 50000` 限的是行数不是时间。窗口里日志超过
上限时（撞库高峰、sshd 调试日志开过一阵），读到的只是 24 小时的最后一段，而面板
过去把这几个数一律念成「过去 24 小时 N 次」——一次关于整窗口的测量，实际上只有
一段的下限。这里盯三件事：读满上限必须自报截断与起点、所有窗口说法只有一个出处、
读不到与没截断两种情形不许被这次修改说坏。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app.services import ban_service as BAN  # noqa: E402
from app.services import login_audit_service as LA  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _line(ts: str, msg: str) -> str:
    """`journalctl -o short-iso` 的一行：2026-10-30T07:57:09+0800 host sshd[1]: …"""
    return f"{ts} host sshd[1]: {msg}"


def _att(**kw) -> dict:
    base = {"ok": True, "failed": 0, "distinct_ips": 0, "top": [], "accepted": [],
            "accepted_ips": [], "note": "", "truncated": False, "covered_from": "",
            "line_limit": LA.JOURNAL_LINES}
    base.update(kw)
    base.setdefault("window_label", LA.window_label(base))
    base.setdefault("window_note", LA.window_note(base))
    return base


def _findings(attempts: dict) -> list[dict]:
    ssh = {"source": "sshd -T", "values": {"passwordauthentication": "yes",
                                           "permitrootlogin": "prohibit-password"},
           "notes": []}
    keys = {"files": [], "scanned": 0, "total_keys": 0}
    accounts = {"empty_password": [], "uid0_extra": []}
    guard = {"status": "unguarded", "phrase": "", "units": [{"name": "fail2ban.service",
                                                             "active": False, "unknown": False,
                                                             "busy": False, "desc": "没在跑"}]}
    return LA._findings(ssh, keys, accounts, attempts, guard, {"reachable": True})


class CapDetectionTests(unittest.TestCase):
    def _read(self, lines: list[str], limit: int = LA.JOURNAL_LINES):
        seen: list[list[str]] = []

        async def run(cmd, args, **_kw):
            seen.append(list(args))
            return {"code": 0, "out": "\n".join(lines)}

        with mock.patch.object(LA, "JOURNAL_LINES", limit), \
             mock.patch.object(LA, "run", run), \
             mock.patch.object(LA.shutil, "which", lambda _n: "/usr/bin/journalctl"):
            return asyncio.run(LA._attempts()), seen

    def test_reading_exactly_the_cap_is_declared_as_a_cut(self):
        """读满上限就是被切过：`-n` 取的是 24h 的最后 N 行，不是「24h 里只有这些」。"""
        lines = [_line(f"2026-10-30T07:5{i % 10}:00+0800", "Failed password for root from 1.2.3.4 port 1 ssh2")
                 for i in range(6)]
        got, _ = self._read(lines, limit=6)
        self.assertTrue(got["ok"])
        self.assertTrue(got["truncated"])
        self.assertEqual(got["line_limit"], 6)
        self.assertEqual(got["covered_from"], "2026-10-30 07:50")

    def test_one_line_under_the_cap_keeps_the_whole_day(self):
        lines = [_line("2026-10-30T07:57:09+0800", "Failed password for root from 1.2.3.4 port 1 ssh2")
                 for _ in range(5)]
        got, _ = self._read(lines, limit=6)
        self.assertFalse(got["truncated"])
        self.assertEqual(got["covered_from"], "")
        self.assertEqual(got["window_label"], "过去 24 小时")
        self.assertEqual(got["window_note"], "")

    def test_empty_window_is_not_truncated(self):
        got, _ = self._read([], limit=1)
        self.assertTrue(got["ok"])
        self.assertFalse(got["truncated"], "空输出不是读满上限")
        self.assertEqual(got["window_label"], "过去 24 小时")

    def test_timestamp_prefix_does_not_change_the_counts(self):
        """换成 short-iso 只该动时间前缀，判定行仍然按消息文本匹配。"""
        got, _ = self._read([
            _line("2026-10-30T07:57:09+0800", "Failed password for root from 1.2.3.4 port 1 ssh2"),
            _line("2026-10-30T07:57:10+0800", "Accepted password for bob from 8.8.8.8 port 1 ssh2"),
        ])
        self.assertEqual((got["failed"], got["distinct_ips"]), (1, 1))
        self.assertEqual(got["accepted"], [{"method": "password", "count": 1}])

    def test_journalctl_is_asked_for_iso_stamps_and_the_same_window(self):
        _, seen = self._read([])
        args = seen[0]
        self.assertIn("short-iso", args, "没有 ISO 时间前缀就翻不成「自 … 起」")
        self.assertEqual(args[args.index("--since") + 1], "24 hours ago")
        self.assertEqual(args[args.index("-n") + 1], str(LA.JOURNAL_LINES))


class WindowPhraseTests(unittest.TestCase):
    def test_truncated_label_names_the_cap_and_the_start(self):
        att = _att(truncated=True, covered_from="2026-10-30 07:57", line_limit=50000)
        self.assertIn("50000 行", att["window_label"])
        self.assertIn("2026-10-30 07:57", att["window_label"])
        self.assertNotIn("过去 24 小时", att["window_label"])

    def test_truncated_note_says_this_is_a_floor_and_where_the_rest_is(self):
        note = _att(truncated=True, covered_from="2026-10-30 07:57")["window_note"]
        self.assertIn("下限", note)
        self.assertIn("journalctl --since", note, "截断必须给出读到全量的办法")

    def test_unread_never_claims_a_window(self):
        """读不到时连「过去 24 小时」都不是一次测量：说法保持原样，由调用方说读不到。"""
        att = LA._no_attempts("journalctl 退出码 1，这段窗口没读到")
        self.assertFalse(att["truncated"])
        self.assertEqual(att["window_note"], "")
        self.assertEqual(att["window_label"], "过去 24 小时")


class FindingsHonestyTests(unittest.TestCase):
    def _brute(self, attempts: dict) -> dict:
        return next(f for f in _findings(attempts) if f["id"] == "brute-volume")

    def test_brute_finding_does_not_say_24h_when_cut(self):
        att = _att(failed=900, distinct_ips=40, truncated=True,
                   covered_from="2026-10-30 07:57",
                   top=[{"ip": "1.2.3.4", "count": 500}])
        f = self._brute(att)
        self.assertNotIn("过去 24 小时", f["title"])
        self.assertIn("50000 行", f["title"])
        self.assertIn("2026-10-30 07:57", f["detail"])
        self.assertIn("下限", f["detail"])

    def test_brute_finding_keeps_the_day_claim_when_whole(self):
        att = _att(failed=900, distinct_ips=40, top=[{"ip": "1.2.3.4", "count": 500}])
        f = self._brute(att)
        self.assertIn("过去 24 小时 900 次失败认证", f["title"])
        self.assertNotIn("下限", f["detail"])

    def test_accepted_password_uses_the_same_window(self):
        cut = _att(accepted=[{"method": "password", "count": 2}], truncated=True,
                   covered_from="2026-10-30 07:57")
        f = next(f for f in _findings(cut) if f["id"] == "accepted-password")
        self.assertNotIn("过去 24 小时", f["title"])
        self.assertIn("下限", f["detail"])


class DoctorAndCliTests(unittest.TestCase):
    def _data(self, attempts: dict) -> dict:
        return {"as_root": True,
                "sshd": {"source": "sshd -T", "port": 22, "reachable": True},
                "counts": {"risk": 0, "warn": 0, "note": 0}, "findings": [], "notes": [],
                "guard": {"status": "guarded", "phrase": "fail2ban.service 在跑", "units": []},
                "keys": {"files": [], "scanned": 0, "total_keys": 0},
                "accounts": {"empty_password": [], "uid0_extra": []},
                "attempts": attempts}

    def test_doctor_green_line_declares_the_floor_when_cut(self):
        got = LA.doctor_item(self._data(_att(failed=900, truncated=True,
                                             covered_from="2026-10-30 07:57")))
        self.assertEqual(got["status"], "pass")
        self.assertNotIn("过去 24 小时", got["detail"])
        self.assertIn("读取上限", got["detail"])
        self.assertIn("下限", got["detail"])

    def test_doctor_green_line_keeps_the_plain_sentence_when_whole(self):
        got = LA.doctor_item(self._data(_att(failed=11)))
        self.assertIn("过去 24 小时失败认证 11 次", got["detail"])

    def test_cli_overview_carries_label_and_note(self):
        text = cli._ssh_audit_text(self._data(_att(failed=900, distinct_ips=40, truncated=True,
                                                   covered_from="2026-10-30 07:57")))
        self.assertIn("50000 行", text)
        self.assertIn("说明：", text)
        self.assertIn("journalctl --since", text)
        self.assertNotIn("过去 24 小时失败认证", text)

    def test_cli_overview_unchanged_when_whole(self):
        text = cli._ssh_audit_text(self._data(_att(failed=7, distinct_ips=3)))
        self.assertIn("过去 24 小时失败认证 7 次（3 个来源地址）", text)
        self.assertNotIn("说明：", text)


class BanCommentTests(unittest.TestCase):
    def test_comment_window_follows_the_same_reading(self):
        self.assertIn("次/24h", BAN._comment(12))
        self.assertIn("次/窗口内", BAN._comment(12, "窗口内"))

    def test_ban_reminder_uses_the_services_window_label(self):
        att = _att(truncated=True, covered_from="2026-10-30 07:57",
                   accepted_ips=[{"ip": "203.0.113.9", "count": 2}])
        self.assertIn("50000 行", LA.window_label(att))
        self.assertEqual(BAN._accepted_from(att), {"203.0.113.9": 2})
        self.assertEqual(BAN._accepted_from(LA._no_attempts("读不到")), {})


class FrontendContractTests(unittest.TestCase):
    def test_page_reads_the_services_window_saying(self):
        src = (REPO / "web/src/views/Services.vue").read_text(encoding="utf-8")
        self.assertIn("a.window_label", src)
        self.assertIn("loginAttempts.value.window_label", src)
        self.assertIn("loginAttempts.truncated", src)
        self.assertIn("{{ attemptsCaption }}", src)
        self.assertIn("{{ loginAttempts.window_note }}", src)
        # 页面自己拼「过去 24 小时」就会与服务端说法分叉：只许在兜底里出现。
        self.assertNotIn("过去 24 小时失败认证", src)
        self.assertNotIn("过去 24 小时 ${", src)


if __name__ == "__main__":
    unittest.main()
