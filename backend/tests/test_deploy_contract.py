"""部署契约一致性守卫。

覆盖三类"文档说的和代码做的不一样"的回归：
1. scripts/*.sh 的部署参数取值（显式环境变量 > 既有 .env > 内置默认）与 .env 回写；
2. choyeonctl 的 schema、argparse 实际参数、AGENTS.md 命令表三者必须一致；
3. choyeonctl deploy/upgrade 真的把 --port/--prefix/--no-pull 用在了它声称的地方。

这一层的意义：AGENTS.md 是给人和 AI 智能体照抄执行的，任何一条写错的命令
都会让对方在"看起来成功"的结果上继续往下走。
"""

import argparse
import asyncio
import io
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

# 这些键可能来自外层 shell（甚至 systemd 注入），会让"读 .env"的分支被跳过
_NEUTRAL_KEYS = ("CP_PORT", "CP_HOST", "CP_DATA_DIR", "CP_BACKUP_DIR",
                 "CP_APP_ROOT", "CP_FILE_ROOTS", "CP_BRANCH", "CP_PREFIX")


def _bash(snippet, env_extra=None):
    """在 source 过 common.sh 的 bash 里执行片段；返回 CompletedProcess。"""
    env = {k: v for k, v in os.environ.items() if k not in _NEUTRAL_KEYS}
    env["NO_COLOR"] = "1"
    env.update({k: v for k, v in (env_extra or {}).items() if v is not None})
    full = f'source "{SCRIPTS}/common.sh" >/dev/null 2>&1\n{snippet}'
    return subprocess.run(["bash", "-c", full], capture_output=True, text=True,
                          env=env, timeout=60, stdin=subprocess.DEVNULL)


class TestEnvValueResolution(unittest.TestCase):
    """common.sh 必须把既有 .env 算进默认值，否则 .env 与 unit 会各说各话。"""

    def setUp(self):
        self.prefix = tempfile.mkdtemp(prefix="cp_env_")
        self.addCleanup(shutil.rmtree, self.prefix, ignore_errors=True)

    def _write_env(self, text):
        (Path(self.prefix) / ".env").write_text(text, encoding="utf-8")

    def _values(self, *names):
        r = _bash("printf '%s\\n' " + " ".join(f'"${n}"' for n in names),
                  {"CP_PREFIX": self.prefix})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_dotenv_supplies_port_and_paths(self):
        self._write_env("# 注释里也有一行 CP_PORT=9999，不能被当成值\n"
                        "CP_PORT=3300\nCP_HOST=0.0.0.0\n")
        port, host, data_dir = self._values("PORT", "HOST", "DATA_DIR")
        self.assertEqual(port, "3300")
        self.assertEqual(host, "0.0.0.0")
        self.assertEqual(data_dir, f"{self.prefix}/data")

    def test_explicit_environment_beats_dotenv(self):
        self._write_env("CP_PORT=3300\n")
        r = _bash('printf "%s" "$PORT"', {"CP_PREFIX": self.prefix, "CP_PORT": "3400"})
        self.assertEqual(r.stdout.strip(), "3400")

    def test_crlf_quotes_and_spaces_are_stripped(self):
        # Windows 编辑器改过的 .env 常见 CRLF；带引号也是合法写法（config.py 都支持）
        self._write_env('CP_PORT="3300" \r\nCP_BACKUP_DIR=/mnt/nas\r\n')
        port, backup = self._values("PORT", "BACKUP_DIR")
        self.assertEqual(port, "3300")
        self.assertEqual(backup, "/mnt/nas")

    def test_missing_dotenv_falls_back_to_defaults(self):
        port, data_dir = self._values("PORT", "DATA_DIR")
        self.assertEqual(port, "3210")
        self.assertEqual(data_dir, f"{self.prefix}/data")

    def test_rendered_unit_matches_dotenv(self):
        """改 .env → 重新渲染 unit 这条路必须真的生效（AGENTS.md §7.5 推荐的操作）。"""
        deploy_dir = Path(self.prefix) / "deploy"
        deploy_dir.mkdir()
        shutil.copy(REPO / "deploy" / "choyeon-panel.service", deploy_dir)
        self._write_env("CP_PORT=3300\nCP_APP_ROOT=/srv/apps\nCP_BACKUP_DIR=/nas/bk\n")
        out = f"{self.prefix}/rendered.service"
        r = _bash(f'UNIT_FILE="{out}"; render_unit >/dev/null 2>&1; cat "{out}"',
                  {"CP_PREFIX": self.prefix})
        body = r.stdout
        self.assertIn("Environment=CP_PORT=3300", body)
        self.assertIn(f"Environment=CP_DATA_DIR={self.prefix}/data", body)
        self.assertIn("Environment=CP_BACKUP_DIR=/nas/bk", body)
        self.assertIn("Environment=CP_APP_ROOT=/srv/apps", body)
        self.assertIn("Environment=CP_FILE_ROOTS=/srv/apps,/etc/nginx,/nas/bk", body)


class TestEnvSync(unittest.TestCase):
    """install.sh 渲染 unit 之前要把生效值写回 .env，两边不能留一份过期的。"""

    def setUp(self):
        self.prefix = tempfile.mkdtemp(prefix="cp_sync_")
        self.addCleanup(shutil.rmtree, self.prefix, ignore_errors=True)

    def _sync(self, dotenv, **extra):
        (Path(self.prefix) / ".env").write_text(dotenv, encoding="utf-8")
        env = {"CP_PREFIX": self.prefix}
        env.update(extra)
        r = _bash('env_sync_all >/dev/null 2>&1; cat "$PREFIX/.env"', env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_writes_effective_values_and_keeps_unrelated_keys(self):
        body = self._sync("CP_PORT=3210\nCP_JWT_SECRET=keepme\n# 说明行\n",
                          CP_PORT="3300")
        lines = dict(
            (k, v) for k, v in (
                ln.split("=", 1) for ln in body.splitlines() if "=" in ln and not ln.startswith("#")
            )
        )
        self.assertEqual(lines["CP_PORT"], "3300")
        self.assertEqual(lines["CP_JWT_SECRET"], "keepme")
        self.assertEqual(lines["CP_DATA_DIR"], f"{self.prefix}/data")
        self.assertIn("# 说明行", body)

    def test_appends_missing_keys(self):
        body = self._sync("CP_HOST=127.0.0.1\n", CP_BACKUP_DIR="/nas/bk")
        self.assertIn("CP_BACKUP_DIR=/nas/bk", body)
        self.assertIn("CP_FILE_ROOTS=", body)

    def test_missing_dotenv_is_seeded_and_aligned(self):
        """缺 .env 时 env_sync_all 必须建一份，并把六个部署参数写进去。

        旧契约是"没有 .env 就什么都不做"，代价是 AGENTS.md §5 的正规入口（编辑 .env
        → upgrade --no-pull）在一个不存在的文件上悬空，而面板照常健康——配置真正的出处
        变成内置默认 + unit 里那几行。夹具目录里没有 .env.example，走的正是"建空文件"
        这条支路：common.sh 是 set -euo pipefail，sync_env 读不到 .env 会让整条部署在
        第一次回写时就退出，所以这里同时钉 returncode。
        """
        r = _bash('env_sync_all', {"CP_PREFIX": self.prefix, "CP_PORT": "3300"})
        self.assertEqual(r.returncode, 0, r.stderr)
        p = Path(self.prefix) / ".env"
        self.assertTrue(p.exists(), "缺 .env 时没有生成，§5 的入口仍然悬空")
        body = p.read_text(encoding="utf-8")
        for key in ("CP_HOST", "CP_PORT", "CP_DATA_DIR", "CP_BACKUP_DIR",
                    "CP_APP_ROOT", "CP_FILE_ROOTS"):
            self.assertIn(f"{key}=", body)
        self.assertIn("CP_PORT=3300", body)
        # .env 里带密钥位（CP_JWT_SECRET、数据库口令），权限必须与 install.sh 一致
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_seeding_copies_the_whole_example_then_overwrites_deploy_keys(self):
        """播种的是整份示例：其余键（开关、限额、密钥占位）不能在这次生成里丢掉。"""
        (Path(self.prefix) / ".env.example").write_text(
            "CP_PORT=3210\nCP_JWT_SECRET=from-example\n# 说明行\n", encoding="utf-8")
        r = _bash('env_sync_all', {"CP_PREFIX": self.prefix, "CP_PORT": "3300"})
        self.assertEqual(r.returncode, 0, r.stderr)
        body = (Path(self.prefix) / ".env").read_text(encoding="utf-8")
        self.assertIn("CP_JWT_SECRET=from-example", body)
        self.assertIn("CP_PORT=3300", body, "示例里的端口盖住了本次生效值")
        self.assertIn("# 说明行", body)


class TestBranchNotSilentlyIgnored(unittest.TestCase):
    def test_existing_repo_warns_that_branch_is_ignored(self):
        prefix = tempfile.mkdtemp(prefix="cp_branch_")
        self.addCleanup(shutil.rmtree, prefix, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "-b", "main", prefix], check=True, timeout=30)
        r = _bash('clone_or_die 2>&1 || exit $?', {"CP_PREFIX": prefix, "CP_BRANCH": "v2"})
        self.assertIn("跳过克隆", r.stdout + r.stderr)
        self.assertIn("分支未生效", r.stdout + r.stderr)


class TestCliContract(unittest.TestCase):
    """schema ↔ argparse ↔ AGENTS.md 三处必须一致，谁漂移谁报错。"""

    def setUp(self):
        import cli
        self.cli = cli

    def _parser_options(self):
        root = self.cli.build_parser()
        action = next(a for a in root._actions
                      if isinstance(a, argparse._SubParsersAction))
        out = {}
        for name, sub in action.choices.items():
            opts = set()
            for a in sub._actions:
                for opt in a.option_strings:
                    # 只比长选项：-n 这类别名不进 schema，也不该进
                    if opt.startswith("--") and opt not in ("--json", "--help"):
                        opts.add(opt)
            out[name] = opts
        return out

    def _schema(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return asyncio.run(self.cli.cmd_schema(argparse.Namespace()))

    @staticmethod
    def _name_parts(name):
        """`app templates|list|...` → 主键 app + 别名列表；主键用于三方对齐。

        Markdown 表格里竖线要写成 `\\|` 才不会结束表格单元，这里先还原。
        """
        return [p for p in re.split(r"[\s|]+", name.replace("\\|", "|")) if p]

    def _schema_index(self):
        primary, alias = {}, {}
        for entry in self._schema()["commands"]:
            parts = self._name_parts(entry["name"])
            primary[parts[0]] = {a for a in entry["args"] if a != "<name>"}
            for p in parts[1:]:
                alias.setdefault(p, parts[0])
        return primary, alias

    def _lookup(self, primary, alias, name):
        return primary.get(name, primary.get(alias.get(name, "\0")))

    def test_schema_covers_every_parser_option(self):
        primary, alias = self._schema_index()
        for name, opts in self._parser_options().items():
            entry = self._lookup(primary, alias, name)
            self.assertIsNotNone(entry, f"schema 里没有 {name} 这条命令")
            missing = opts - entry
            self.assertFalse(missing, f"schema 的 {name} 少了参数：{sorted(missing)}")

    def test_schema_has_no_invented_option(self):
        parser_map = self._parser_options()
        primary, alias = self._schema_index()
        for name, args in primary.items():
            extra = args - parser_map.get(name, set())
            self.assertFalse(extra, f"schema 的 {name} 列了 parser 里不存在的参数：{sorted(extra)}")

    def test_upgrade_supports_no_pull(self):
        # AGENTS.md §7.5 把这条命令写成"禁止手工编辑 unit"的正规替代路径
        opts = self._parser_options()["upgrade"]
        self.assertIn("--no-pull", opts)

    def test_agents_md_table_matches_schema(self):
        primary, _alias = self._schema_index()
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        # 只取 §4 命令表：全文匹配会被"失败处置"表里那些反引号命令行污染
        section = text.split("## 4.", 1)[1].split("## 5.", 1)[0]
        rows = re.findall(r"^\| `([^`]+)` \| (`[^`]*`|—) \|", section, re.M)
        self.assertGreaterEqual(len(rows), 8, "AGENTS.md 命令表解析失败")
        table = {self._name_parts(name)[0]: cell for name, cell in rows}
        self.assertEqual(set(table), set(primary),
                         "AGENTS.md 的命令表与 schema 覆盖的命令不一致")
        for key, cell in table.items():
            listed = {x for x in cell.strip("`").split() if x != "<name>"} if cell != "—" else set()
            self.assertEqual(listed, primary[key],
                             f"AGENTS.md 里 `{key}` 的参数列表与 schema 不一致")

    def test_deploy_polls_the_port_it_deployed_to(self):
        """--port 必须决定健康探测的端口，否则成功的部署会在 healthy 上报 false。"""
        seen = {}

        async def fake_sh(cmd, timeout=60, env=None, stdin_devnull=False):
            seen["cmd"] = cmd
            seen["env"] = env or {}
            return {"code": 0, "out": "", "err": ""}

        async def fake_wait(timeout=30, port=None):
            seen["port"] = port
            return True

        args = self.cli.build_parser().parse_args(["deploy", "--port", "3299"])
        with mock.patch.object(self.cli.os, "geteuid", return_value=0), \
             mock.patch.object(self.cli, "sh", fake_sh), \
             mock.patch.object(self.cli, "wait_health", fake_wait), \
             mock.patch.object(self.cli, "_user_count", return_value=0), \
             mock.patch.object(self.cli.Out, "json_mode", True):
            data = asyncio.run(self.cli.cmd_deploy(args))
        self.assertEqual(seen["port"], 3299)
        self.assertEqual(seen["env"]["CP_PORT"], "3299")
        self.assertTrue(data["url"].endswith(":3299"))

    def test_deploy_reads_admin_count_from_target_prefix(self):
        """--prefix 指向别处时，端口/数据目录都按目标目录的 .env 解析（与 install.sh 同口径）。"""
        target = tempfile.mkdtemp(prefix="cp_dep_")
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        data_dir = Path(target) / "pgdata"
        db = data_dir / "panel.db"
        data_dir.mkdir(parents=True)
        (Path(target) / ".env").write_text(
            f"CP_DATA_DIR={data_dir}\nCP_PORT=3311\n", encoding="utf-8")
        import sqlite3
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE users(id INTEGER, username TEXT)")
        con.execute("INSERT INTO users VALUES (1, 'admin')")
        con.commit()
        con.close()

        async def fake_sh(cmd, timeout=60, env=None, stdin_devnull=False):
            return {"code": 0, "out": "", "err": ""}

        seen = {}

        async def fake_wait(timeout=30, port=None):
            seen["port"] = port
            return True

        clean_env = {k: v for k, v in os.environ.items() if k not in ("CP_DATA_DIR", "CP_PORT")}
        args = self.cli.build_parser().parse_args(["deploy", "--prefix", target])
        with mock.patch.object(self.cli.os, "geteuid", return_value=0), \
             mock.patch.dict(os.environ, clean_env, clear=True), \
             mock.patch.object(self.cli, "sh", fake_sh), \
             mock.patch.object(self.cli, "wait_health", fake_wait), \
             mock.patch.object(self.cli.Out, "json_mode", True):
            data = asyncio.run(self.cli.cmd_deploy(args))
        self.assertTrue(data["adminCreated"])
        self.assertEqual(data["dataDir"], str(data_dir))
        self.assertEqual(seen["port"], 3311)
        self.assertTrue(data["url"].endswith(":3311"))
        self.assertIn("cliHint", data)  # 装到了别的目录，后续命令得用那一份 CLI

    def test_upgrade_passes_no_pull_through(self):
        calls = []

        async def fake_sh(cmd, timeout=60, env=None, stdin_devnull=False):
            calls.append(cmd)
            return {"code": 0, "out": "", "err": ""}

        async def fake_wait(timeout=30, port=None):
            return True

        for flag, want in ((None, False), ("--no-pull", True)):
            calls.clear()
            args = self.cli.build_parser().parse_args(["upgrade"] + ([flag] if flag else []))
            with mock.patch.object(self.cli, "sh", fake_sh), \
                 mock.patch.object(self.cli, "wait_health", fake_wait), \
                 mock.patch.object(self.cli.Out, "json_mode", True):
                asyncio.run(self.cli.cmd_upgrade(args))
            self.assertTrue(any(str(c).endswith("update.sh") for c in calls[0]),
                            f"cmd_upgrade 没有调用 update.sh：{calls}")
            self.assertEqual("--no-pull" in calls[0], want,
                             f"upgrade {flag or '(无参数)'} 透传结果不符：{calls[0]}")


class TestCliUserCreate(unittest.TestCase):
    """choyeonctl user create 的落库与留痕——AGENTS.md 让智能体照抄这条命令建管理员。

    审计是面板唯一的追责线索：只写一句"已建号"而看不出建的是什么角色，
    "谁在这台机器上加了一个 admin"就没法回答。用户名那条链上还有个锚定的坑：
    末尾多一个换行的名字能过旧的正则，却过不了 token 主体的精确比对；这里连拒绝一起验。
    """

    NAME = f"utcli{os.getpid() % 100000}"

    def setUp(self):
        import cli
        from app import database as dbm
        self.cli, self.dbm = cli, dbm
        self.addCleanup(self.dbm.execute, "DELETE FROM users WHERE username=?", (self.NAME,))
        self.addCleanup(self.dbm.execute, "DELETE FROM audit WHERE username=?", (self.NAME,))

    def _create(self, name: str, role: str = "admin"):
        args = self.cli.build_parser().parse_args(
            ["user", "create", name, "--password-stdin", "--role", role]
        )
        with mock.patch.object(self.cli.sys, "stdin", io.StringIO("Password123\n")), \
             mock.patch.object(self.cli.Out, "json_mode", True):
            return asyncio.run(self.cli.cmd_user(args))

    def test_create_records_role_in_audit(self):
        self.assertTrue(self._create(self.NAME)["ok"])
        self.assertEqual(self.dbm.query_one("SELECT role FROM users WHERE username=?", (self.NAME,))["role"], "admin")
        row = self.dbm.query_one(
            "SELECT detail FROM audit WHERE username=? ORDER BY id DESC LIMIT 1", (self.NAME,)
        )
        self.assertIn("admin", str(row["detail"]), "审计要看得出建的是什么角色，光一条建号记录不够")

    def test_trailing_newline_name_is_rejected(self):
        """末尾多一个换行的名字既不该建得出来，也不该在审计里和真账号长得一样。"""
        bad = f"{self.NAME}\n"
        with self.assertRaises(SystemExit):
            self._create(bad)
        self.assertIsNone(self.dbm.query_one("SELECT 1 FROM users WHERE username=?", (bad,)))


if __name__ == "__main__":
    unittest.main()
