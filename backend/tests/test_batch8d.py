"""批次⑧D：`choyeonctl firewall` 的命令行契约。

CLI 与面板防火墙页共用 `firewall_service`，所以这里锁的是"终端里也一样绕不过去"：
- 退出码按 AGENTS.md 的承诺分档：参数写歪 2（用法错误）、被自锁守卫拦下或防火墙自身报错 1、
  这台机器根本没装防火墙 3。脚本靠这三个档决定是改参数重试还是停下来，全并成 1 就等于没有契约；
- `off` 必须带 `--yes`，`remove` 必须带 `--expect`（编号会随任何一次删除前移）；
- 守卫不提供绕过参数，所以这条命令的选项集合里不该出现 `--force`；
- `list` 的人类可读形态必须把 sig 印出来——否则照着终端提示删规则还得先跑一次 `--json`，
  而中间那一次列表变化正是签名比对要防的。
"""

import argparse
import asyncio
import contextlib
import io
import os
import tempfile
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch8d_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch8d_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app.errors import BadInput, Forbidden  # noqa: E402

RULES = [
    {"num": 1, "token": "22/tcp", "port": 22, "port_to": None, "proto": "tcp", "action": "allow",
     "direction": "in", "source": "Anywhere", "family": "ipv4", "comment": "",
     "sig": "22/tcp|allow|any", "protected": True},
    {"num": 2, "token": "80/tcp", "port": 80, "port_to": None, "proto": "tcp", "action": "allow",
     "direction": "in", "source": "Anywhere", "family": "ipv6", "comment": "web",
     "sig": "80/tcp|allow|any", "protected": False},
]
STATE = {
    "tool": "ufw", "active": True, "rules": RULES, "protected": [22],
    "protected_reasons": {"22": "sshd 监听端口"}, "output": "Status: active",
    "default_incoming": "deny", "logging": "on (low)",
}


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


def _run(coro):
    """跑一次 CLI 命令，返回 (结果, stdout, stderr)；SystemExit 的 code 一并带出。"""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            data = asyncio.run(coro)
        except SystemExit as e:
            data, code = None, e.code or 0
    return data, out.getvalue(), err.getvalue(), code


def _state(**kw):
    base = dict(STATE)
    base.update(kw)
    return base


class CliFirewallTests(unittest.TestCase):
    def setUp(self):
        cli.Out.json_mode = False

    def tearDown(self):
        cli.Out.json_mode = False

    def _no_tool(self):
        return mock.patch.object(cli.firewall_service, "detect", new=mock.AsyncMock(return_value="ufw"))

    def test_parser_offers_exactly_the_documented_verbs(self):
        # AGENTS.md 与 schema 写的是这六个动作；少一个就是文档指向的出口本身是坏的
        action = next(a for a in cli.build_parser()._actions
                      if isinstance(a, argparse._SubParsersAction))
        fw = action.choices["firewall"]
        self.assertEqual(set(fw._option_string_actions) - {"--help", "-h", "--json"},
                         {"--port", "--port-to", "--proto", "--source", "--comment", "--rate",
                          "--num", "--expect", "--yes"})
        self.assertEqual(set(fw._get_positional_actions()[0].choices),
                         {"list", "allow", "deny", "remove", "on", "off"})

    def test_there_is_no_way_to_bypass_the_lockout_guard(self):
        action = next(a for a in cli.build_parser()._actions
                      if isinstance(a, argparse._SubParsersAction))
        opts = set(action.choices["firewall"]._option_string_actions)
        for banned in ("--force", "--yes-i-know", "--ignore-lockout"):
            self.assertNotIn(banned, opts)

    def test_list_prints_sig_and_lock_marks(self):
        rd = mock.AsyncMock(return_value=STATE)
        with mock.patch.object(cli.firewall_service, "read", new=rd):
            data, text, _err, code = _run(cli.cmd_firewall(_args("firewall", "list")))
        self.assertEqual(code, 0)
        self.assertIs(data, STATE)
        self.assertIsNone(rd.call_args.kwargs.get("request"), "CLI 没有 Request，不该带进前端那套端口推断")
        self.assertIn("ufw · 已启用 · 默认入站 deny", text)
        self.assertIn("22（sshd 监听端口）", text)
        self.assertIn("sig=22/tcp|allow|any", text)
        self.assertIn("受保护", text)
        self.assertIn("共 2 条", text)
        # v4/v6 是两条独立规则，读数的人必须分得开（孪生规则不互相顶岗）
        self.assertIn("IPv6", text)
        self.assertIn("IPv4", text)

    def test_list_json_mode_returns_the_whole_state(self):
        cli.Out.json_mode = True
        with mock.patch.object(cli.firewall_service, "read", new=mock.AsyncMock(return_value=STATE)):
            _data, text, _err, code = _run(cli.cmd_firewall(_args("firewall", "list", "--json")))
        self.assertEqual(code, 0)
        self.assertIn('"tool": "ufw"', text)
        self.assertIn("sig", text)

    def test_allow_maps_args_onto_the_service_body(self):
        add = mock.AsyncMock(return_value={"ok": True, "detail": "规则已添加"})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "add_rule", new=add):
            _d, text, _e, code = _run(cli.cmd_firewall(_args(
                "firewall", "allow", "--port", "8000", "--port-to", "8100", "--proto", "udp",
                "--source", "10.0.0.0/8", "--comment", "office vpn", "--rate")))
        self.assertEqual(code, 0)
        self.assertEqual(add.call_args.args[0], {
            "port": 8000, "action": "allow", "proto": "udp", "port_to": 8100,
            "source": "10.0.0.0/8", "comment": "office vpn", "rate": True})
        self.assertEqual(add.call_args.args[1], "ufw")
        self.assertIn("规则已添加", text)

    def test_allow_without_optional_args_sends_a_minimal_body(self):
        add = mock.AsyncMock(return_value={"ok": True})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "add_rule", new=add):
            _run(cli.cmd_firewall(_args("firewall", "allow", "--port", "23306")))
        self.assertEqual(add.call_args.args[0], {"port": 23306, "action": "allow", "proto": "tcp"})

    def test_deny_is_the_same_call_with_action_deny(self):
        add = mock.AsyncMock(return_value={"ok": True})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "add_rule", new=add):
            _run(cli.cmd_firewall(_args("firewall", "deny", "--port", "23")))
        self.assertEqual(add.call_args.args[0]["action"], "deny")

    def test_missing_port_is_a_usage_error_and_reaches_the_service(self):
        # 少了 --port 绝不能退化成"放行任意端口"这种一条敞口的规则
        add = mock.AsyncMock(return_value={"ok": True})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "add_rule", new=add):
            _d, _t, err, code = _run(cli.cmd_firewall(_args("firewall", "allow", "--proto", "tcp")))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--port", err)
        add.assert_not_called()

    def test_remove_requires_the_signature(self):
        rm = mock.AsyncMock(return_value={"ok": True})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "remove_rule", new=rm):
            _d, _t, err, code = _run(cli.cmd_firewall(_args("firewall", "remove", "--num", "2")))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--expect", err)
        rm.assert_not_called()

    def test_remove_passes_num_and_expect_through(self):
        rm = mock.AsyncMock(return_value={"ok": True, "removed": "80/tcp", "reloaded": True})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "remove_rule", new=rm):
            _d, text, _e, code = _run(cli.cmd_firewall(_args("firewall", "remove", "--num", "2", "--expect", "S")))
        self.assertEqual(code, 0)
        self.assertEqual(rm.call_args.args, (2, "S", "ufw"))
        self.assertIn("80/tcp", text)

    def test_remove_names_the_twin_rule_that_still_opens_the_port(self):
        rm = mock.AsyncMock(return_value={"ok": True, "removed": "3399/tcp", "still_open": ["3399/tcp (v6)"]})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "remove_rule", new=rm):
            _d, text, _e, code = _run(cli.cmd_firewall(_args("firewall", "remove", "--num", "4", "--expect", "S")))
        self.assertEqual(code, 0)
        self.assertIn("已删除 3399/tcp", text)
        self.assertIn("3399/tcp (v6) 仍在放行同一端口", text)

    def test_off_needs_an_explicit_yes(self):
        se = mock.AsyncMock(return_value={"ok": True})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "set_enabled", new=se):
            _d, _t, err, code = _run(cli.cmd_firewall(_args("firewall", "off")))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--yes", err)
        se.assert_not_called()

    def test_off_with_yes_and_on_reach_the_service(self):
        se = mock.AsyncMock(return_value={"ok": True, "active": False, "detail": "防火墙已停用"})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "set_enabled", new=se):
            _d, text, _e, code = _run(cli.cmd_firewall(_args("firewall", "off", "--yes")))
        self.assertEqual(code, 0)
        self.assertEqual(se.call_args.args, (False, "ufw"))
        self.assertIn("已停用", text)
        se2 = mock.AsyncMock(return_value={"ok": True, "active": True, "detail": "已启用"})
        with self._no_tool(), mock.patch.object(cli.firewall_service, "set_enabled", new=se2):
            _run(cli.cmd_firewall(_args("firewall", "on")))
        self.assertEqual(se2.call_args.args, (True, "ufw"))

    def test_bad_input_is_exit_two_and_forbidden_is_exit_one(self):
        # AGENTS.md 的退出码表：2 = 参数写错（改对再重试），1 = 执行失败（先弄清为什么被拒）
        cases = [(BadInput("端口范围是 1-65535"), cli.EXIT_USAGE, "1-65535"),
                 (Forbidden("规则 22/tcp 是 22 目前唯一的放行来源，删除后这些端口对本机不再开放。"),
                  cli.EXIT_FAIL, "唯一的放行来源")]
        for exc, want_code, want_text in cases:
            with self.subTest(exc=type(exc).__name__):
                add = mock.AsyncMock(side_effect=exc)
                with self._no_tool(), mock.patch.object(cli.firewall_service, "add_rule", new=add):
                    _d, _t, err, code = _run(cli.cmd_firewall(_args("firewall", "allow", "--port", "22")))
                self.assertEqual(code, want_code)
                self.assertIn(want_text, err)

    def test_no_firewall_installed_is_a_precondition(self):
        add = mock.AsyncMock(return_value={"ok": True})
        with mock.patch.object(cli.firewall_service, "detect", new=mock.AsyncMock(return_value=None)), \
             mock.patch.object(cli.firewall_service, "add_rule", new=add):
            _d, _t, err, code = _run(cli.cmd_firewall(_args("firewall", "allow", "--port", "8080")))
        self.assertEqual(code, cli.EXIT_PRECONDITION)
        self.assertIn("既没有 ufw 也没有 firewalld", err)
        add.assert_not_called()

    def test_list_still_answers_when_there_is_no_firewall(self):
        """只读那条路要能在"还没装防火墙"的机器上给出答案——部署脚本正是这时候来查的。"""
        none_state = _state(tool="none", rules=[], active=False, output="未安装 ufw 或 firewalld",
                            protected=[])
        with mock.patch.object(cli.firewall_service, "read", new=mock.AsyncMock(return_value=none_state)):
            data, text, _e, code = _run(cli.cmd_firewall(_args("firewall", "list")))
        self.assertEqual(code, 0)
        self.assertEqual(data["tool"], "none")
        self.assertIn("共 0 条", text)

    def test_firewalld_list_shows_zone(self):
        st = _state(tool="firewalld", default_incoming="", zone="public", logging="")
        with mock.patch.object(cli.firewall_service, "read", new=mock.AsyncMock(return_value=st)):
            _d, text, _e, code = _run(cli.cmd_firewall(_args("firewall", "list")))
        self.assertEqual(code, 0)
        self.assertIn("默认区域 public", text)


if __name__ == "__main__":
    unittest.main()
