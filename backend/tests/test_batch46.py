"""批次㊻A：SSH 爆破来源的一键封禁（ban_service + /api/bans + choyeonctl ban）。

要守的五件事，每件都有对应用例：
- **不重新发明防火墙**：ufw 那条规则仍由 `firewall_service` 写删，删除仍走编号 + 签名与自锁
  判定；ban_service 只回答"封谁、这条是谁封的、封完生效没有"，所以测试盯的是它有没有把
  删除权留在防火墙那一层，而不是自己拼一条 `ufw delete`；
- **成功只能是"列回来的表里有这个地址"**：ufw 打印 Aborted 却返回 0 是这台机器实测过的行为，
  退出码不能当证据——写完之后那一次重新列表才是证据，缺席就判失败；
- **空列表与"没读到"是两种答复**：`ok=false` 必须带原因并锁住写操作，firewalld 的
  "集合还不存在"则是真的没有任何封禁，不能报成读不到；
- **只封单个公网地址**：CIDR、域名、内网/回环/组播一律拒绝。封一片是人在终端上想清楚的事，
  让它变成一个顺手参数等于给一次误操作发通行证；`--hits` 只进备注，不参与任何判定；
- **数字只有一个出处**：`MAX_BANS` 与标记在服务里写一次，CLI 的人类文本、`cmd_schema` 契约、
  AGENTS.md 的表格行必须引用同一个数（与批次㉖A/㊺A 同构）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch46_data_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

import cli  # noqa: E402
from app import database as dbm  # noqa: E402
from app import (  # noqa: E402
    main,
    security,
)
from app.errors import BadInput, ExecFailed, Forbidden  # noqa: E402
from app.services import ban_service as B  # noqa: E402
from app.services import firewall_service as fw  # noqa: E402

# 测试用地址必须是"真公网"：203.0.113.0/24 这类文档段在 Python 里算 is_private，
# 会被面板自己的守卫拒掉——那正是被测行为，不能拿它当"要封的爆破来源"。
IP = "45.12.34.56"
_seq = iter(range(1000, 999999))


def _uid(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _rule(num: int, ip: str, comment: str = f"{B.MARKER} ssh 爆破") -> dict:
    return {"num": num, "port": None, "port_to": None, "token": "Anywhere",
            "action": "deny", "proto": "any", "source": ip, "family": "ipv4",
            "comment": comment, "sig": f"deny|{ip}", "protected": False}


def _read(rules, rules_ok=True, reason=""):
    return {"tool": fw.UFW, "active": True, "state_ok": True, "rules_ok": rules_ok,
            "reason": reason, "rules": rules, "protected": [], "protected_reasons": {},
            "default_incoming": "DROP", "marker": B.MARKER, "maxBans": B.MAX_BANS}


class _Runner:
    """假的外部命令执行器：按"命令行里出现哪个子串"给返回码与原文。"""

    def __init__(self, replies=None):
        self.calls: list[str] = []
        self.replies = replies or {}

    async def __call__(self, cmd, args=None, **kw):
        joined = " ".join([cmd] + [str(x) for x in (args or [])])
        self.calls.append(joined)
        for key, resp in self.replies.items():
            if key in joined:
                return {"code": resp.get("code", 0), "out": resp.get("out", "")}
        return {"code": 0, "out": ""}

    def ran(self, needle: str) -> list[str]:
        return [c for c in self.calls if needle in c]


class _FakeIpset:
    """够用的 firewalld 替身：运行期与 permanent 两份成员表，加上那条绑定 rich rule。"""

    def __init__(self, runtime=(), permanent=(), exists=True, fail_runtime=False):
        self.runtime = list(runtime)
        self.permanent = list(permanent)
        self.exists = exists
        self.fail_runtime = fail_runtime
        self.rich = True
        self.calls: list[str] = []

    async def run(self, cmd, args=None, **kw):
        argv = [str(x) for x in (args or [])]
        joined = " ".join([cmd] + argv)
        self.calls.append(joined)
        perm = "--permanent" in argv
        table = self.permanent if perm else self.runtime
        if "--get-entries" in joined:
            if not self.exists:
                return {"code": 1, "out": "Error: CURRENT_FIREWALLD: ipset 'choyeon-ban' not existing"}
            return {"code": 0, "out": "\n".join(table)}
        if "--query-rich-rule" in joined:
            return {"code": 0 if self.rich else 1, "out": "yes" if self.rich else "no"}
        if "--add-rich-rule=" in joined or "--new-ipset=" in joined:
            self.rich = True
            self.exists = True
            return {"code": 0, "out": "success"}
        op = "add" if "--add-entry=" in joined else "remove" if "--remove-entry=" in joined else ""
        if op:
            ip = next(a.split("=", 1)[1] for a in argv if a.startswith(f"--{op}-entry="))
            if self.fail_runtime and not perm:
                return {"code": 1, "out": "COMMAND_FAILED: ipset: Argument wrong"}
            if op == "add" and ip not in table:
                table.append(ip)
            if op == "remove" and ip in table:
                table.remove(ip)
            return {"code": 0, "out": "success"}
        return {"code": 0, "out": ""}


def _ufw(reads: list) -> mock._patch:
    """把 detect + read 钉成 ufw，`reads` 是每次读取依次返回的结果（最后一次会复用）。"""
    state = {"i": 0}

    async def fake_read(tool=None, request=None):
        idx = min(state["i"], len(reads) - 1)
        state["i"] += 1
        return reads[idx]

    return mock.patch.object(fw, "read", fake_read)


class TestAddressGuard(unittest.TestCase):
    """只接受单个公网地址：其余写法一律拒绝，且一条命令都不许发出去。"""

    def test_non_public_or_non_address_inputs_are_refused(self):
        bad = ["", "   ", "10.0.0.5", "127.0.0.1", "192.168.1.7", "169.254.0.1",
               "224.0.0.5", "0.0.0.0", "::1", "fe80::1", "10.0.0.0/8", "0.0.0.0/0",
               "example.com", "http://example.com", "203.0.113.9:22", "203.0.113.999"]
        runner = _Runner()
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             mock.patch.object(B, "run", runner):
            for raw in bad:
                with self.assertRaises((BadInput, Forbidden), msg=raw):
                    asyncio.run(B.ban(raw, None, 12))
        self.assertEqual(runner.calls, [], "判定还没过就发了写命令，等于把守卫写在命令之后")

    def test_the_bad_input_message_does_not_echo_paths_or_credentials(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)):
            with self.assertRaises(BadInput) as cm:
                asyncio.run(B.ban("10.0.0.0/8", None, 0))
        self.assertIn("CIDR", str(cm.exception.message))


class TestUfwPath(unittest.TestCase):
    def test_ban_writes_a_marked_rule_at_position_one(self):
        runner = _Runner()
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([]), _read([_rule(1, IP, f"{B.MARKER} ssh 爆破 12 次/24h")])]), \
             mock.patch.object(B, "run", runner), \
             mock.patch.object(B, "_attempts", mock.AsyncMock(return_value={})):
            out = asyncio.run(B.ban(IP, None, 12))
        self.assertTrue(out["ok"])
        self.assertEqual(len(runner.ran("ufw insert 1 deny from")), 1)
        self.assertIn(f"{B.MARKER} ssh 爆破 12 次/24h", runner.calls[0])
        self.assertIn(f"deny from {IP} comment {B.MARKER}", runner.calls[0])
        self.assertEqual([b["ip"] for b in out["bans"]], [IP])

    def test_exit_code_zero_is_not_success(self):
        """ufw 打印 Aborted 却返回 0：写完重新列回来，地址不在表里就不能说成功。"""
        for out in ("ERROR: 'insert' Aborted.", ""):
            runner = _Runner({"ufw insert": {"code": 0, "out": out}})
            with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
                 _ufw([_read([])]), \
                 mock.patch.object(B, "run", runner), \
                 mock.patch.object(B, "_attempts", mock.AsyncMock(return_value={})):
                with self.assertRaises(ExecFailed, msg=out):
                    asyncio.run(B.ban(IP, None, 30))

    def test_unread_rule_table_locks_the_write(self):
        runner = _Runner()
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([], rules_ok=False, reason="ufw status 退出码 1")]), \
             mock.patch.object(B, "run", runner):
            with self.assertRaises(Forbidden) as cm:
                asyncio.run(B.ban(IP, None, 30))
        self.assertIn("没读到", cm.exception.message)
        self.assertEqual(runner.calls, [], "现状读不到还继续写，等于在没有证据的情况下动防火墙")

    def test_already_banned_writes_no_second_rule(self):
        runner = _Runner()
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([_rule(2, IP)])]), \
             mock.patch.object(B, "run", runner):
            out = asyncio.run(B.ban(IP, None, 5))
        self.assertTrue(out.get("already"))
        self.assertEqual(runner.calls, [])

    def test_list_is_capped(self):
        rules = [_rule(i, f"203.0.113.{i}") for i in range(1, B.MAX_BANS + 1)]
        runner = _Runner()
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read(rules)]), mock.patch.object(B, "run", runner):
            with self.assertRaises(BadInput) as cm:
                asyncio.run(B.ban("45.12.34.99", None, 9))
        self.assertIn(str(B.MAX_BANS), cm.exception.message)
        self.assertEqual(runner.calls, [])

    def test_hits_only_names_the_comment_and_never_decides(self):
        # hits=0 与 hits=10**9 必须走同一条判定路径：备注里有几个数字与封不封无关。
        for hits in (0, -5, 10 ** 9):
            runner = _Runner()
            after = _read([_rule(1, IP, B._comment(hits))])
            with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
                 _ufw([_read([]), after]), \
                 mock.patch.object(B, "run", runner), \
                 mock.patch.object(B, "_attempts", mock.AsyncMock(return_value={})):
                out = asyncio.run(B.ban(IP, None, hits))
            self.assertTrue(out["ok"], hits)
            self.assertIn(B.MARKER, runner.calls[0])
            if not hits:
                self.assertNotIn("次/24h", runner.calls[0])

    def test_accepted_session_is_warned_but_still_banned(self):
        """口令被猜中过一次也要封，但必须说清那会断开一个正在工作的会话。"""
        runner = _Runner()
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([]), _read([_rule(1, IP)])]), \
             mock.patch.object(B, "run", runner), \
             mock.patch.object(B, "_attempts", mock.AsyncMock(return_value={
                 "ok": True, "accepted_ips": [{"ip": IP, "count": 2}]})):
            out = asyncio.run(B.ban(IP, None, 40))
        self.assertTrue(out["ok"])
        self.assertIn("成功登录过 2 次", out["detail"])
        self.assertEqual(len(runner.ran("ufw insert")), 1)

    def test_unban_delegates_to_firewall_remove_rule(self):
        """删除权留在防火墙那一层：编号 + 签名比对与自锁守卫一次都不能绕。"""
        seen = {}

        async def fake_remove(num, expect, tool, request=None):
            seen.update({"num": num, "expect": expect, "tool": tool, "request": request})
            return {"removed": f"deny {IP}", "detail": "", "still_open": []}

        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             mock.patch.object(fw, "remove_rule", fake_remove), \
             _ufw([_read([])]):
            out = asyncio.run(B.unban(IP, 7, f"deny|{IP}", "REQ"))
        self.assertEqual(seen, {"num": 7, "expect": f"deny|{IP}", "tool": fw.UFW, "request": "REQ"})
        self.assertIn("已解封", out["detail"])

    def test_ufw_unban_without_num_is_a_usage_error(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)):
            with self.assertRaises(BadInput):
                asyncio.run(B.unban(IP, 0, ""))

    def test_list_separates_empty_from_unreadable(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([])]):
            ok = asyncio.run(B.list_bans())
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["bans"], [])
        self.assertEqual(ok["reason"], "")

        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([], rules_ok=False, reason="命令超时")]):
            bad = asyncio.run(B.list_bans())
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["bans"], [])
        self.assertIn("命令超时", bad["reason"])

    def test_foreign_rules_are_not_claimed_as_bans(self):
        """只认自家标记：别人手工加的 deny 规则不该出现在封禁列表里，也不该被 rm 误删。"""
        rules = [_rule(1, IP, "临时封一下"), _rule(2, "198.51.100.9", ""),
                 _rule(3, "198.51.100.10", f"{B.MARKER} ssh 爆破 4 次/24h"),
                 _rule(5, "198.51.100.12", f"{B.MARKER}nana 撞前缀的人工备注"),
                 {"num": 4, "action": "allow", "source": "198.51.100.11",
                  "comment": B.MARKER, "sig": "allow", "family": "ipv4"}]
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read(rules)]):
            out = asyncio.run(B.list_bans())
        self.assertEqual([b["ip"] for b in out["bans"]], ["198.51.100.10"])
        self.assertTrue(B._is_mine(f"{B.MARKER} ssh 爆破"))
        self.assertTrue(B._is_mine(B.MARKER))
        self.assertFalse(B._is_mine(f"{B.MARKER}nana"))


class TestFirewalldPath(unittest.TestCase):
    def _ban(self, fake: _FakeIpset, accepted=None):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.FIREWALLD)), \
             mock.patch.object(B, "run", fake.run), \
             mock.patch.object(B, "_attempts", mock.AsyncMock(return_value={
                 "ok": True, "accepted_ips": [{"ip": k, "count": v} for k, v in (accepted or {}).items()]})):
            return asyncio.run(B.list_bans()), asyncio.run(B.ban(IP, None, 8))

    def test_ban_lands_in_both_scopes(self):
        fake = _FakeIpset()
        before, out = self._ban(fake)
        self.assertTrue(before["ok"] and before["bans"] == [])
        self.assertTrue(out["ok"])
        row = next(b for b in out["bans"] if b["ip"] == IP)
        self.assertTrue(row["runtime"] and row["permanent"],
                        "只写运行期的话，别人一次 --reload 就把封禁静默清空")

    def test_half_written_is_reported_not_hidden(self):
        fake = _FakeIpset(fail_runtime=True)
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.FIREWALLD)), \
             mock.patch.object(B, "run", fake.run), \
             mock.patch.object(B, "_attempts", mock.AsyncMock(return_value={})):
            with self.assertRaises(ExecFailed):
                asyncio.run(B.ban(IP, None, 8))

    def test_missing_ipset_is_empty_not_unreadable(self):
        fake = _FakeIpset(exists=False)
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.FIREWALLD)), \
             mock.patch.object(B, "run", fake.run):
            out = asyncio.run(B.list_bans())
        self.assertTrue(out["ok"], "集合不存在是真的没有封禁，报成读不到会让人去修一条好着的探测")
        self.assertEqual(out["bans"], [])

    def test_unban_rechecks_the_collection(self):
        fake = _FakeIpset(runtime=[IP], permanent=[IP])
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.FIREWALLD)), \
             mock.patch.object(B, "run", fake.run):
            out = asyncio.run(B.unban(IP))
        self.assertEqual(out["bans"], [])
        self.assertTrue([c for c in fake.calls if f"--remove-entry={IP}" in c])

    def test_unban_refuses_when_the_address_is_still_there(self):
        """firewalld 说删了不算，两份集合里都列不到了才算。"""
        class _Stubborn(_FakeIpset):
            """说"删好了"但集合没动——面板不能被这句话骗过去。"""

            async def run(self, cmd, args=None, **kw):
                joined = " ".join(map(str, args or []))
                if "--remove-entry=" in joined:
                    self.calls.append(f"{cmd} {joined}")
                    return {"code": 0, "out": "success"}
                return await super().run(cmd, args, **kw)

        fake = _Stubborn(runtime=[IP], permanent=[IP])
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.FIREWALLD)), \
             mock.patch.object(B, "run", fake.run):
            with self.assertRaises(ExecFailed):
                asyncio.run(B.unban(IP))

    def test_no_firewall_tool_refuses_instead_of_pretending(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value="none")):
            with self.assertRaises(Forbidden) as cm:
                asyncio.run(B.ban(IP, None, 8))
            out = asyncio.run(B.list_bans())
        self.assertIn("fail2ban", cm.exception.message)
        self.assertFalse(out["ok"])
        self.assertIn("没有 ufw", out["reason"])


class TestLoginAuditAcceptedIps(unittest.TestCase):
    """`accepted_ips` 是封禁那一层判断"这个地址有没有成功登录过"的唯一出处。"""

    LOG = (
        "Oct 17 10:00:01 host sshd[1]: Failed password for root from 203.0.113.9 port 22 ssh2\n"
        "Oct 17 10:00:02 host sshd[1]: Failed password for root from 203.0.113.9 port 22 ssh2\n"
        "Oct 17 10:00:03 host sshd[1]: Failed password for invalid user admin from 198.51.100.4 port 22 ssh2\n"
        "Oct 17 10:00:04 host sshd[1]: Accepted publickey for root from 203.0.113.9 port 22 ssh2\n"
        "Oct 17 10:00:05 host sshd[1]: Accepted password for deploy from 192.0.2.10 port 22 ssh2\n"
    )

    def _attempts(self, out: str, code: int = 0) -> dict:
        from app.services import login_audit_service as LA

        async def fake_run(cmd, args=None, **kw):
            return {"code": code, "out": out}

        with mock.patch.object(LA.shutil, "which", lambda p: "/usr/bin/journalctl"), \
             mock.patch.object(LA, "run", fake_run):
            return asyncio.run(LA.attempts())

    def test_accepted_sources_are_counted_per_ip(self):
        d = self._attempts(self.LOG)
        self.assertTrue(d["ok"])
        self.assertEqual(d["failed"], 3)
        self.assertEqual(d["accepted_ips"], [
            {"ip": "192.0.2.10", "count": 1},
            {"ip": "203.0.113.9", "count": 1},
        ])

    def test_unreadable_log_yields_no_rows_rather_than_a_clean_list(self):
        d = self._attempts("", code=1)
        self.assertFalse(d["ok"])
        self.assertEqual(d["accepted_ips"], [])
        self.assertEqual(self._attempts("", code=7)["accepted_ips"], [])

    def test_comment_survives_the_firewall_comment_charset(self):
        """备注要能过 `firewall_service` 的字符集与长度校验，否则 ufw 会把整条规则判成非法。"""
        for hits in (0, 1, 12345, -9, "abc", None, 10 ** 30):
            text = B._comment(hits)
            self.assertTrue(fw.COMMENT_RE.fullmatch(text), text)
            self.assertLessEqual(len(text), fw.MAX_COMMENT)
        self.assertIn(str(B.MAX_HITS), B._comment(10 ** 30), "hits 必须夹在服务说的那个上限里")


_JSON_MODE = cli.Out.json_mode


def _cli(argv: list[str]):
    """跑一条 CLI（按 `--json` 模式），返回 (退出码, stdout 原文)。"""
    buf = io.StringIO()
    code = 0
    args = cli.build_parser().parse_args(argv + ["--json"])
    cli.Out.json_mode = True
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            asyncio.run(args.fn(args))
    except SystemExit as e:  # Out.error
        code = e.code if isinstance(e.code, int) else 1
    finally:
        cli.Out.json_mode = _JSON_MODE
    return code, buf.getvalue()


class TestCli(unittest.TestCase):
    def test_list_is_the_same_answer_as_the_page(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             _ufw([_read([_rule(4, IP, f"{B.MARKER} ssh 爆破 20 次/24h")])]):
            code, out = _cli(["ban", "list"])
        self.assertEqual(code, 0, out)
        d = json.loads(out)
        self.assertEqual([b["num"] for b in d["bans"]], [4])
        self.assertEqual(d["maxBans"], B.MAX_BANS)

    def test_rm_without_num_and_expect_is_usage_error(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             mock.patch.object(B, "unban", mock.AsyncMock(return_value={"ok": True, "bans": []})):
            code, out = _cli(["ban", "rm", IP])
        self.assertEqual(code, cli.EXIT_USAGE, out)
        self.assertIn("--expect", out)

    def test_list_without_firewall_is_precondition(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value="none")):
            code, out = _cli(["ban", "add", IP])
        self.assertEqual(code, cli.EXIT_PRECONDITION, out)

    def test_guard_message_maps_to_usage_error(self):
        with mock.patch.object(fw, "detect", mock.AsyncMock(return_value=fw.UFW)), \
             mock.patch.object(B, "ban", mock.AsyncMock(side_effect=BadInput("只能封单个 IP 地址"))):
            code, out = _cli(["ban", "add", "example.com"])
        self.assertEqual(code, cli.EXIT_USAGE, out)

    def test_human_table_names_the_cap_marker_and_the_third_state(self):
        table = cli._ban_table({"tool": fw.UFW, "ok": True, "marker": B.MARKER,
                                "maxBans": B.MAX_BANS,
                                "bans": [{"ip": IP, "num": 3, "sig": "deny|x", "comment": B.MARKER,
                                          "runtime": True, "permanent": False}]})
        self.assertIn(f"上限 {B.MAX_BANS}", table)
        self.assertIn(B.MARKER, table)
        self.assertIn("重启后失效", table)
        self.assertIn("choyeonctl ban rm", table)
        bad = cli._ban_table({"tool": fw.UFW, "ok": False, "reason": "ufw 没返回", "bans": []})
        self.assertIn("不等于「没有封禁」", bad)
        self.assertNotIn("封禁 0 条", bad)


class TestContract(unittest.TestCase):
    def _entry(self) -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            cmds = asyncio.run(cli.cmd_schema(cli.build_parser().parse_args(["schema"])))["commands"]
        return next(c for c in cmds if c["name"].startswith("ban"))

    def test_schema_args_are_options_argparse_really_accepts(self):
        """契约里写的参数必须真能解析出来：写一个不存在的参数比漏写更坏。"""
        entry = self._entry()
        self.assertEqual(entry["args"], ["--hits", "--num", "--expect"])
        for flag in entry["args"]:
            value = "deny|x" if flag == "--expect" else "12"
            parsed = cli.build_parser().parse_args(["ban", "add", IP, flag, value])
            self.assertEqual(getattr(parsed, flag.lstrip("-").replace("-", "_")),
                             value if flag == "--expect" else 12, flag)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(["ban", "purge"])

    def test_schema_cap_and_marker_match_the_service(self):
        entry = self._entry()
        blob = entry["desc"] + " ".join(entry["notes"])
        self.assertIn(str(B.MAX_BANS), blob, "契约里的上限必须是服务里那个数")
        self.assertIn(B.MARKER, blob)

    def test_agents_md_row_exists(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        # 表格里的竖线是转义写的（`ban list\|add\|rm`），断言必须按实际字形来
        self.assertIn("| `ban list\\|add\\|rm`", text,
                      "AGENTS.md §4 缺 ban 这一行，智能体按文档找不到命令")
        for needle in (B.MARKER, "--expect", str(B.MAX_BANS)):
            self.assertIn(needle, text.split("| `ban list")[1].split("\n")[0], needle)

    def test_openapi_has_the_three_verbs(self):
        paths = main.app.openapi()["paths"]
        self.assertEqual(sorted(k for k in paths["/api/bans"]), ["delete", "get", "post"])


class _AuthMixin:
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.admin = _uid("b46a")
        cls.viewer = _uid("b46v")
        for name, role in ((cls.admin, "admin"), (cls.viewer, "viewer")):
            dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                        (name, security.hash_pass("Password123"), role))
        cls.hdr = {"Authorization": f"Bearer {security.sign_token(cls.admin, 'admin')}"}
        cls.vhdr = {"Authorization": f"Bearer {security.sign_token(cls.viewer, 'viewer')}"}

    @classmethod
    def tearDownClass(cls):
        for name in (cls.admin, cls.viewer):
            dbm.execute("DELETE FROM users WHERE username=?", (name,))


class TestBansApi(_AuthMixin, unittest.TestCase):
    def test_read_and_write_are_admin_only(self):
        self.assertEqual(self.client.get("/api/bans", headers=self.vhdr).status_code, 403)
        self.assertEqual(self.client.post("/api/bans", json={"ip": IP}, headers=self.vhdr).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/bans?ip={IP}&num=1", headers=self.vhdr).status_code, 403)
        self.assertEqual(self.client.get("/api/bans").status_code, 401)

    def test_list_is_the_service_answer_verbatim(self):
        with mock.patch.object(B, "list_bans", mock.AsyncMock(return_value=_read([]))):
            r = self.client.get("/api/bans", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["marker"], B.MARKER)

    def test_refusal_is_audited_too(self):
        """封禁挡的是别人，留痕比一般写操作更要紧：拒绝也要进审计。"""
        with mock.patch.object(B, "ban", mock.AsyncMock(side_effect=Forbidden("封不了"))):
            r = self.client.post("/api/bans", json={"ip": IP, "hits": 40}, headers=self.hdr)
        self.assertEqual(r.status_code, 403, r.text)
        row = dbm.query_one("SELECT detail FROM audit WHERE username=? AND action='ban:add' "
                            "ORDER BY id DESC LIMIT 1", (self.admin,))
        self.assertIn("拒绝", row["detail"])

    def test_hits_is_a_number_or_nothing(self):
        """hits 只进备注：非数字、对象、超大值都不能变成判定，也不能把请求打歪。"""
        seen = []

        async def fake_ban(ip, request=None, hits=0):
            seen.append(hits)
            return {"ok": True, "added": True, "tool": fw.UFW, "bans": [], "detail": "已封禁"}

        for body in ({"ip": IP, "hits": "abc"}, {"ip": IP, "hits": {"n": 1}},
                     {"ip": IP, "hits": 10 ** 30}, {"ip": IP}):
            with mock.patch.object(B, "ban", fake_ban):
                r = self.client.post("/api/bans", json=body, headers=self.hdr)
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [0, 0, B.MAX_HITS, 0], "hits 必须被夹进 0..MAX_HITS，且永远不进判定")

    def test_remove_passes_signature_through(self):
        seen = {}

        async def fake_unban(ip, num=0, expect="", request=None):
            seen.update({"ip": ip, "num": num, "expect": expect})
            return {"ok": True, "tool": fw.UFW, "bans": [], "detail": "已解封"}

        with mock.patch.object(B, "unban", fake_unban):
            r = self.client.delete(f"/api/bans?ip={IP}&num=9&expect=deny%7Cx", headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, {"ip": IP, "num": 9, "expect": "deny|x"})


if __name__ == "__main__":
    unittest.main()
