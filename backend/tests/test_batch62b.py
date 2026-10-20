"""批次 62B：CLI 与部署脚本在"没做成 / 没核对成"时必须说实话。

这一批是同一句话的三个入口里，最靠外那两层（shell 脚本与 choyeonctl）把失败
或者"没核对"念成了好消息：

- **wait_health 没有探测工具时 return 0**：install.sh 于是打印「服务已启动并通过健康检查」，
  update.sh 打印「健康检查通过」并宣布升级完成。唯一能发现"服务其实没起来"的自动环节，
  被它自己的前置条件缺失（机器上没装 curl/wget）关掉了——AGENTS.md §7.8 禁止伪造自检结果。
  改成三档返回码（0 探到 / 1 探到但不 ok / 2 根本没探），并且 rc=2 **不触发自动回滚**：
  没证据就把仓库 reset --hard 回上一个 commit，等于用一次猜测去修一次猜测。
- **deploy/upgrade/backup 的失败只给退出码**：--json 模式下脚本自身的输出被完整丢弃，
  智能体按 AGENTS.md 去读 JSON 的 error 字段，读到的是「安装脚本退出码 1」。
  apt 的锁、npm 的报错、vite 的堆栈全在 stdout/stderr 里，不报错就没人会去手工跑第二遍。
  现在带末段输出，并且先过 redact_creds（安装日志里有仓库 URL 与 .env 的键值）。
- **backup run 说「备份完成：None」**：脚本退 0 不等于产物落地，判据换成"目录里多出一份新文件"。
- **firewall on/off 只认退出码**：`ufw enable` 在 iptables 缺失时照样退 0；写完必须用同一个
  读者 `read()` 复核，没生效就报失败，别让面板在最要命的开关上粉饰绿灯。
- **RUNBOOK 与 CLI 用法行推荐 --password**：AGENTS.md §7.2 明令禁止的写法出现在排障入口上。
- **认不出的授权密钥打印成「None位 … None」**：None 是"没解析出来"，念出来像真有个位数。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCRIPTS = REPO / "scripts"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch62b_data_"))

import cli  # noqa: E402
from app.services import apps_service  # noqa: E402
from app.services import firewall_service as fw  # noqa: E402

NEUTRAL_KEYS = ("CP_PORT", "CP_HOST", "CP_DATA_DIR", "CP_BACKUP_DIR",
                "CP_APP_ROOT", "CP_FILE_ROOTS", "CP_BRANCH", "CP_PREFIX")


def _bash(snippet: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """在 source 过 common.sh 的 bash 里执行片段（stdin 断开，避免 tty 分支随机翻脸）。"""
    env = {k: v for k, v in os.environ.items() if k not in NEUTRAL_KEYS}
    env["NO_COLOR"] = "1"
    env.update(env_extra or {})
    full = f'source "{SCRIPTS}/common.sh" >/dev/null 2>&1\n{snippet}'
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                          env=env, timeout=60, stdin=subprocess.DEVNULL)


def _json_of(text: str) -> dict:
    start = text.find("{")
    return json.loads(text[start:]) if start >= 0 else {}


@unittest.skipIf(not shutil.which("bash"), "需要 bash")
class WaitHealthTriState(unittest.TestCase):
    """没有探测工具时必须回 2，而不是 0（=绿灯）。"""

    def test_no_probe_tool_is_not_a_pass(self):
        # have_cmd 就是"探测工具在不在"这个判据本身，替身只改这一件事，
        # 于是装了 curl 的机器也测得到那个分支。
        # 用 `|| rc=$?` 而不是裸调用：common.sh 顶部是 set -euo pipefail，
        # 非 0 返回会让片段当场中止，拿不到 echo 的结果。
        r = _bash('have_cmd() { return 1; }; rc=0; wait_health 1 >/dev/null 2>&1 || rc=$?; echo "rc=$rc"')
        self.assertEqual(r.stdout.strip(), "rc=2",
                         "无 curl/wget 时旧实现 return 0，install.sh 据此宣布「通过健康检查」")

    def test_no_probe_tool_says_not_checked(self):
        r = _bash('have_cmd() { return 1; }; wait_health 1')
        self.assertIn("未核对", r.stdout + r.stderr, "要说清这一项没核对成，而不是静默跳过")

    def test_probe_present_but_endpoint_dead_is_a_fail(self):
        """有 curl、端口没人应答：这才是真的"没起来"，必须是 1（脚本据此回滚）。"""
        if not shutil.which("curl"):
            self.skipTest("需要 curl")
        r = _bash("PORT=1; rc=0; wait_health 1 >/dev/null 2>&1 || rc=$?; echo rc=$rc")
        self.assertEqual(r.stdout.strip(), "rc=1")

    def test_a_real_ok_true_is_zero(self):
        """修诚实不能修成误报：正文给了 "ok":true 就得是 0。"""
        shim = tempfile.mkdtemp(prefix="cp_wh_shim_")
        self.addCleanup(shutil.rmtree, shim, ignore_errors=True)
        with open(os.path.join(shim, "curl"), "w", encoding="utf8") as fh:
            fh.write('#!/bin/sh\nprintf \'{"ok":true}\\n\'\n')
        os.chmod(os.path.join(shim, "curl"), 0o755)
        r = _bash("wait_health 1 >/dev/null 2>&1; echo rc=$?",
                  {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"})
        self.assertEqual(r.stdout.strip(), "rc=0")


class DeployScriptsReadTheThreeStates(unittest.TestCase):
    """调用方不许再把 wait_health 当布尔用：rc=2 既不能说「通过」，也不能触发回滚。"""

    def setUp(self):
        self.install = (SCRIPTS / "install.sh").read_text(encoding="utf-8")
        self.update = (SCRIPTS / "update.sh").read_text(encoding="utf-8")

    def test_no_bare_boolean_use(self):
        for name, text in (("install.sh", self.install), ("update.sh", self.update)):
            self.assertIsNone(re.search(r"if\s+wait_health", text),
                              f"{name} 还在用 `if wait_health`，第二档（根本没探）会被念成通过")

    def test_rollback_only_on_real_failure(self):
        self.assertIn('if [ "$rc" -eq 1 ]', self.update,
                      "只有真失败才回滚；rc=2 回滚等于用一次 git reset --hard 去猜一个没证据的结论")

    def test_unprobed_run_says_not_checked(self):
        self.assertIn("没核对成", self.update)
        self.assertIn("没核对成", self.install)


class ScriptTailHelper(unittest.TestCase):
    def test_redacts_credentials(self):
        t = cli.script_tail({"code": 1, "err": "", "out": "clone https://user:secrettoken@gh/x/y.git failed"})
        self.assertIn("clone", t)
        self.assertNotIn("secrettoken", t, "末段输出会进 --json 的 error，也常被贴进工单")

    def test_keeps_the_end_and_caps_length(self):
        t = cli.script_tail({"code": 1, "err": "", "out": "\n".join(f"line{i}" for i in range(400))},
                            lines=5, limit=40)
        self.assertIn("line399", t)
        self.assertLessEqual(len(t), 40)

    def test_empty_output_is_empty(self):
        self.assertEqual(cli.script_tail({"code": 1, "err": "", "out": ""}), "")


class DeployFailureCarriesReason(unittest.TestCase):
    async def _deploy(self, r: dict) -> tuple[int, str]:
        async def fake_sh(cmd, timeout=60, env=None, stdin_devnull=False):
            return r

        async def fake_wait(timeout=30, port=None):
            return True

        args = cli.build_parser().parse_args(["deploy", "--port", "3299"])
        out = io.StringIO()
        code = 0
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
             mock.patch.object(cli, "sh", fake_sh), \
             mock.patch.object(cli, "wait_health", fake_wait), \
             mock.patch.object(cli, "_user_count", return_value=0), \
             mock.patch.object(cli.Out, "json_mode", True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                await cli.cmd_deploy(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_failure_includes_exit_code_and_tail(self):
        """--json 下脚本正文一个字都不打印，所以 error 必须自己带原因。"""
        code, text = asyncio.run(self._deploy(
            {"code": 1, "err": "", "out": "npm error code ETARGET\nnpm error notarget 1.2.3"}))
        self.assertEqual(code, 1)
        data = _json_of(text)
        self.assertIn("退出码 1", data["error"])
        self.assertIn("npm error code ETARGET", data["error"], "AGENTS.md 让智能体读 error 字段判失败")

    def test_tail_survives_a_stdout_only_failure(self):
        """apt 与 npm 的报错常在 stdout：只看 err 的那版实现会交出一句空白原因。"""
        _code, text = asyncio.run(self._deploy(
            {"code": 1, "err": "", "out": "E: Could not get lock /var/lib/dpkg/lock-frontend"}))
        self.assertIn("dpkg/lock-frontend", _json_of(text)["error"])

    def test_credential_in_script_output_is_redacted(self):
        _code, text = asyncio.run(self._deploy(
            {"code": 1, "err": "", "out": "fetch https://git:ghp_SECRETSECRET@github.com/a/b.git"}))
        self.assertNotIn("ghp_SECRETSECRET", text)


class UpgradeFailureCarriesReason(unittest.TestCase):
    async def _upgrade(self, r: dict) -> tuple[int, str]:
        async def fake_sh(cmd, timeout=60, env=None, stdin_devnull=False):
            return r

        args = cli.build_parser().parse_args(["upgrade", "--no-pull"])
        out = io.StringIO()
        code = 0
        with mock.patch.object(cli, "sh", fake_sh), \
             mock.patch.object(cli.Out, "json_mode", True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                await cli.cmd_upgrade(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_stdout_only_failure_still_gives_a_reason(self):
        code, text = asyncio.run(self._upgrade(
            {"code": 1, "err": "", "out": "vite build failed: Cannot find module 'x'"}))
        self.assertEqual(code, 1)
        self.assertIn("Cannot find module", text, "只读 err 时这句会整段丢掉")
        self.assertIn("退出码 1", text)


class BackupRunMustProduceAnArtifact(unittest.TestCase):
    """备份是唯一一个"假成功"就足以造成不可逆损失的环节。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_batch62b_bk_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.manual = Path(self.dir, "manual")
        self.manual.mkdir(parents=True)

    async def _run(self, makes_file: bool, before: bool = False) -> tuple[int, str]:
        if before:
            old = self.manual / "panel-manual-20260101-000000.tar.gz"
            old.write_bytes(b"x")
            os.utime(old, (1_700_000_000, 1_700_000_000))

        async def fake_sh(cmd, timeout=60, env=None, stdin_devnull=False):
            if makes_file:
                (self.manual / "panel-manual-20261020-030000.tar.gz").write_bytes(b"y")
            return {"code": 0, "err": "", "out": "备份完成"}

        args = cli.build_parser().parse_args(["backup", "run"])
        out = io.StringIO()
        code = 0
        with mock.patch.object(cli.config, "BACKUP_DIR", self.dir), \
             mock.patch.object(cli, "sh", fake_sh), \
             mock.patch.object(cli.Out, "json_mode", True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                await cli.cmd_backup(args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_no_file_at_all_is_not_a_success(self):
        code, text = asyncio.run(self._run(makes_file=False))
        self.assertEqual(code, 1, "脚本退 0 但一份产物都没有，旧实现回 ok:true + 「备份完成：None」")
        self.assertNotIn("None", text)
        self.assertFalse(_json_of(text)["ok"])
        self.assertIn("没有新增", _json_of(text)["error"])

    def test_an_old_file_alone_is_not_a_success(self):
        """目录里有旧的那一份更容易骗过人：判据是"多出来一份"，不是"有东西"。"""
        code, text = asyncio.run(self._run(makes_file=False, before=True))
        self.assertEqual(code, 1)
        self.assertIn("最新的仍是", _json_of(text)["error"])

    def test_a_new_file_is_reported(self):
        code, text = asyncio.run(self._run(makes_file=True, before=True))
        self.assertEqual(code, 0)
        self.assertIn("panel-manual-20261020-030000.tar.gz", text)


UFW_NUM_EMPTY = ("Status: active\n\nTo                         Action      From\n"
                 "--                         ------      ----\n")


def _ufw_run(status_text: str):
    """按给定正文回 `ufw status verbose`，写操作一律退 0（这正是缺陷本体）。"""
    async def run(cmd, args, cwd=None, timeout=120, env=None):
        if cmd == "ufw" and list(args[:2]) == ["status", "verbose"]:
            return {"code": 0, "err": "", "out": status_text}
        if cmd == "ufw" and list(args[:2]) == ["status", "numbered"]:
            return {"code": 0, "err": "", "out": UFW_NUM_EMPTY}
        return {"code": 0, "err": "", "out": "Command completed\n"}
    return run


class FirewallToggleVerifiesItsOwnEffect(unittest.TestCase):
    """写完必须用同一个读者复核：退出码不是"防火墙开了"的证据。"""

    def _set(self, status_text: str, active: bool):
        # 保护端口钉成空表：这一条只测写完的复核，自锁守卫由批次㊷/㊻ 各自覆盖
        with mock.patch.object(fw, "protected_ports", lambda request=None: ([], {})), \
             mock.patch.object(fw, "run", _ufw_run(status_text)):
            try:
                return asyncio.run(fw.set_enabled(active, fw.UFW))
            except fw.ExecFailed as e:
                return e

    def test_a_silent_no_op_is_a_failure(self):
        """`ufw enable` 退 0 而状态仍 inactive（iptables 缺失、ufw-init 报错时实测会这样）。"""
        got = self._set("Status: inactive\nDefault: deny (incoming)\n", True)
        self.assertIsInstance(got, fw.ExecFailed, "旧实现把这次开关报成「已启用」")
        self.assertIn("没有生效", str(got))

    def test_unread_state_is_a_failure_not_a_pass(self):
        got = self._set("ERROR: problem running ufw-init\n", False)
        self.assertIsInstance(got, fw.ExecFailed)
        self.assertIn("读不到", str(got))

    def test_a_real_toggle_reports_the_observed_state(self):
        got = self._set("Status: active\nDefault: deny (incoming)\n", True)
        self.assertIsInstance(got, dict, got)
        self.assertTrue(got["ok"])


def _audit_data(key: dict) -> dict:
    return {"as_root": True,
            "sshd": {"source": "sshd -T", "port": 22, "reachable": True},
            "counts": {"risk": 0, "warn": 0, "note": 0}, "findings": [], "settings": [],
            "attempts": {"ok": True, "failed": 0, "distinct_ips": 0, "top": [], "accepted": []},
            "accounts": {"login_shells": [], "login_count": 0, "uid0_extra": [],
                         "empty_password": [], "shadow_unreadable": False, "sudo_groups": {}},
            "guard": {"status": "unknown", "phrase": "", "units": []},
            "keys": {"total_keys": 1, "scanned": 1,
                     "files": [{"user": "root", "path": "/root/.ssh/authorized_keys",
                                "mode": "600", "owner": "root", "keys": [key]}]}}


class UnparsedKeyLineHasNoNone(unittest.TestCase):
    """认不出的那把钥匙要说"读不出来"，不是把 None 念成位数和指纹。"""

    def test_unparsed_key_line(self):
        t = cli._ssh_audit_text(_audit_data(
            {"algo": "?", "bits": None, "comment": "ssh-rsa AAAAB3NzaC1q", "fp": None,
             "weak": False, "unparsed": True}))
        self.assertNotIn("None", t)
        self.assertIn("位数未知", t)
        self.assertIn("指纹读不到", t)

    def test_parsed_key_line_still_shows_bits_and_fp(self):
        t = cli._ssh_audit_text(_audit_data(
            {"algo": "ed25519", "bits": 256, "comment": "laptop", "fp": "SHA256:abc",
             "weak": False, "unparsed": False}))
        self.assertIn("ed25519 256位", t)
        self.assertIn("SHA256:abc", t)


class DocsNeverRecommendArgvPassword(unittest.TestCase):
    """AGENTS.md §7.2 禁止的写法不能出现在排障入口与用法行上。"""

    def test_runbook_quick_table(self):
        text = (REPO / "RUNBOOK.md").read_text(encoding="utf-8")
        self.assertNotIn("--password ", text, "RUNBOOK 的速查表还在推荐 argv 传口令")
        self.assertIn("--password-stdin", text)

    def test_cli_epilog(self):
        src = (BACKEND / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("user create alice --password 'xxx'", src)
        self.assertIn("user create alice --password-stdin", src)

    def test_key_fingerprint_cell_has_no_bare_null(self):
        """网页那一栏和 CLI 那一行必须说同一句话：读不到就说读不到。

        `k.fp` 为空时旧写法渲染出空白单元格，看上去像"这把钥匙没有指纹"；
        而它实际是"这一行没解析出来"——正是批次62B 给 CLI 修掉的同一个毛病。
        """
        vue = (REPO / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")
        self.assertIn("k.fp || '指纹读不到'", vue)


class AppCreateErrorIsRedacted(unittest.TestCase):
    """建应用时仓库 URL 里带过 token，异常正文原样打终端就会把它留在屏幕与工单里。"""

    def test_token_never_reaches_the_terminal(self):
        def boom(_payload):
            raise RuntimeError("clone https://git:ghp_SECRETSECRET@github.com/a/b.git: 403")

        args = cli.build_parser().parse_args(
            ["app", "create", "--name", "x", "--start-cmd", "node i.js", "--repo", "https://a/b.git"])
        out = io.StringIO()
        code = 0
        with mock.patch.object(apps_service, "create_app", boom), \
             mock.patch.object(cli.Out, "json_mode", True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                asyncio.run(cli.cmd_app(args))
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 1)
        self.assertNotIn("ghp_SECRETSECRET", out.getvalue())
        self.assertIn("403", out.getvalue(), "遮凭据不等于把真实原因也遮掉")


if __name__ == "__main__":
    unittest.main()
