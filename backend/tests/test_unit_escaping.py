"""面板生成的 systemd unit 行转义回归测试。

覆盖的都是沙箱真 systemd（PID 1 可用）实测出来的坏法，不是风格问题：
1. `%X` 说明符在 Environment= / ExecStart= / WorkingDirectory= 里都会被展开，
   引号挡不住：`WorkingDirectory=/a%b` 目录不存在 → 单元起不来；
   `Environment="V=/a%b"` → V 变成 `/a<启动 ID>`；
   `ExecStart=/bin/bash -lc "printf tag=%b"` → 实际执行的是 `printf tag=/bin/sh`
   （Shell 说明符），面板显示的命令和真正跑起来的不是同一件事。
2. Environment= 的赋值不加引号时，值在第一个空格处截断
   （`CP_DATA_DIR=/tmp/sp dir/sub` → 进程只拿到 `/tmp/sp`），备份写到别的目录、
   面板毫无提示。整条赋值必须用双引号包住。
3. ExecStart 参数位里的 `${VAR}` 会被 systemd 用**它自己的环境**（含
   EnvironmentFile 读进来的 .panel.env）提前替换；没定义的变量替换成空串，
   命令照样 rc=0。后果有两层：start_cmd 里写 `${DB_PASSWORD}` 会把密钥放进
   argv，而 /proc/<pid>/cmdline 是全局可读的；`${PORT:-3000}` 这类 bash 写法
   被吞成空串，服务"启动成功"但参数没了。
4. 反方向同样有坑：`$` 只在 ExecStart 的**参数位**要翻倍。二进制路径和
   WorkingDirectory 里 systemd 不做变量展开，翻倍后反而变成找不到文件/目录
   （实测 ExecStart 二进制写 `dd$$dlr/e.sh` → 203，WorkingDirectory 写
   `dd$$dlr` → 200）；Environment= 的值同样不展开 `$`（实测 `V=${ZZ}` 原样
   到达），所以那两处只处理 `%`。
"""

import asyncio
import inspect
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import time

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_unit_esc_ut_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from app import config  # noqa: E402
from app import database as dbm  # noqa: E402
from app.services import apps_service as APPS  # noqa: E402
from app.services import backup_service as B  # noqa: E402
from app.util import unit_env_value, unit_exec_arg_escape, unit_pct_escape  # noqa: E402

_seq = itertools.count()


def _exec_line(unit: str) -> str:
    for line in unit.splitlines():
        if line.startswith("ExecStart="):
            return line
    raise AssertionError(f"unit 里没有 ExecStart：{unit!r}")


def _fake_run(calls):
    """顶掉 systemctl：记录调用，返回成功（与 test_backup_service 同款）。"""

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        calls.append([cmd, *args])
        return {"code": 0, "out": ""}

    return run


# ---------------------------------------------------------------- 纯字符串层
class TestPctEscape(unittest.TestCase):
    def test_percent_doubled(self):
        self.assertEqual(unit_pct_escape("a%b"), "a%%b")
        self.assertEqual(unit_pct_escape("100%%"), "100%%%%")

    def test_only_percent_touched(self):
        """$ 与空格各有位置相关的处理，这里一律不许动。"""
        self.assertEqual(unit_pct_escape("/tmp/sp ace/$X"), "/tmp/sp ace/$X")

    def test_none_and_non_str(self):
        self.assertEqual(unit_pct_escape(None), "")
        self.assertEqual(unit_pct_escape(8000), "8000")


class TestEnvValue(unittest.TestCase):
    """`Environment=KEY="..."` 里的值：转义 `\\` 与 `"`、压平换行、`%` 翻倍。"""

    def test_backslash_then_quote_then_percent(self):
        self.assertEqual(unit_env_value('a\\b"c%d'), 'a\\\\b\\"c%%d')

    def test_newlines_flattened(self):
        self.assertEqual(unit_env_value("a\nb\rc"), "a b c")

    def test_dollar_left_alone(self):
        """实测 systemd 不在 Environment= 里展开 `${ZZ}`（值原样到进程），
        翻倍只会让用户真的需要 `$X` 时拿到 `$$X`。"""
        self.assertEqual(unit_env_value("cost $HOME ${X}"), "cost $HOME ${X}")

    def test_none(self):
        self.assertEqual(unit_env_value(None), "")


class TestExecArgEscape(unittest.TestCase):
    """ExecStart 参数位：`%` 与 `$` 各自翻倍，互不干扰。"""

    def test_percent_and_dollar_doubled(self):
        self.assertEqual(unit_exec_arg_escape("printf %b"), "printf %%b")
        self.assertEqual(unit_exec_arg_escape("${PORT:-3000}"), "$${PORT:-3000}")

    def test_combo(self):
        self.assertEqual(unit_exec_arg_escape("a$%b"), "a$$%%b")
        self.assertEqual(unit_exec_arg_escape("$%$%"), "$$%%$$%%")

    def test_other_chars_untouched(self):
        self.assertEqual(unit_exec_arg_escape('node "a b" \\ c'), 'node "a b" \\ c')

    def test_none(self):
        self.assertEqual(unit_exec_arg_escape(None), "")


# ---------------------------------------------------------------- 渲染层
class TestRenderUnitEscaping(unittest.TestCase):
    def _app(self, start_cmd, **kw):
        return {
            "name": "demo-app",
            "path": "/root/www/demo-app",
            "start_cmd": start_cmd,
            "port": 3000,
            "unit_template": None,
            "unit_override": None,
            **kw,
        }

    def test_percent_in_start_cmd_is_doubled(self):
        self.assertEqual(
            _exec_line(APPS.render_unit(self._app("printf tag=%b"))),
            'ExecStart=/bin/bash -lc "printf tag=%%b"',
        )

    def test_dollar_brace_in_start_cmd_is_doubled(self):
        self.assertEqual(
            _exec_line(APPS.render_unit(self._app("node server.js ${DB_PASSWORD}"))),
            'ExecStart=/bin/bash -lc "node server.js $${DB_PASSWORD}"',
        )

    def test_plain_dollar_is_doubled_too(self):
        """`$ZZ` 单独不被 systemd 展开，但翻倍是唯一能把 `$` 写成字面量的写法，
        统一翻倍后 systemd 逐字下发，展开交回 bash。"""
        self.assertEqual(
            _exec_line(APPS.render_unit(self._app("echo $HOME $x"))),
            'ExecStart=/bin/bash -lc "echo $$HOME $$x"',
        )

    def test_paths_not_escaped(self):
        """路径不许加戏：WorkingDirectory 里出现 `$$` 会变成"目录不存在"（实测 200）。"""
        unit = APPS.render_unit(self._app("node x.js"))
        self.assertIn("WorkingDirectory=/root/www/demo-app\n", unit)
        self.assertIn("EnvironmentFile=-/root/www/demo-app/.panel.env\n", unit)

    def test_custom_template_start_cmd_also_escaped(self):
        tpl = "[Service]\nExecStart=/bin/bash -lc {{start_cmd}}\n"
        out = APPS.render_unit_from(tpl, self._app("printf %b ${ZZ}"))
        self.assertEqual(out, '[Service]\nExecStart=/bin/bash -lc "printf %%b $${ZZ}"\n')

    def test_guard_matrix(self):
        """两处渲染都必须过 unit_exec_arg_escape；漏一处，上面的断言就成了假绿。"""
        for fn in (APPS.render_unit, APPS.render_unit_from):
            self.assertIn("unit_exec_arg_escape", inspect.getsource(fn), fn.__name__)


class TestBackupUnitLines(unittest.TestCase):
    """备份 timer 的 service 单元：三处写法各钉一条。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_ue_unit_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.calls = []
        p1 = mock.patch.object(config, "UNIT_DIR", self.dir)
        p2 = mock.patch.object(B, "run", _fake_run(self.calls))
        for p in (p1, p2):
            self.addCleanup(p.stop)
            p.start()

    def _write(self, data_dir=None, backend_dir=None, python_bin=None):
        bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) "
            "VALUES('pg','appdb','daily',3,30,7,1)"
        )
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (bid,))
        patches = []
        if data_dir is not None:
            patches.append(mock.patch.object(config, "DATA_DIR", data_dir))
        if backend_dir is not None:
            patches.append(mock.patch.object(B, "BACKEND_DIR", backend_dir))
        if python_bin is not None:
            patches.append(mock.patch.object(B, "PYTHON_BIN", python_bin))
        for p in patches:
            self.addCleanup(p.stop)
            p.start()
        row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
        asyncio.run(B._sync_timer(row))
        with open(f"{self.dir}/panel-backup-{bid}.service") as f:
            return f.read()

    def test_environment_line_is_quoted_and_escaped(self):
        unit = self._write(data_dir="/tmp/sp dir/a%b")
        self.assertIn('Environment="CP_DATA_DIR=/tmp/sp dir/a%%b"', unit)

    def test_execstart_binary_is_quoted_and_escaped(self):
        unit = self._write(python_bin="/opt/py thon/bin/python%b")
        self.assertIn('ExecStart="/opt/py thon/bin/python%%b" -m app.backup_runner', unit)

    def test_working_directory_escaped_and_unquoted(self):
        unit = self._write(backend_dir="/srv/back end/a%b")
        self.assertIn("WorkingDirectory=/srv/back end/a%%b\n", unit)
        # 这条上加引号是致命错误（实测 "path is not absolute"），必须没有引号
        self.assertNotIn('WorkingDirectory="', unit)

    def test_plain_paths_unchanged(self):
        """正常安装路径不该被改动，否则单元测试可能全靠"什么都改"通过。"""
        unit = self._write(
            data_dir="/var/lib/panel",
            backend_dir="/srv/panel/backend",
            python_bin="/srv/panel/backend/.venv/bin/python",
        )
        self.assertIn("WorkingDirectory=/srv/panel/backend\n", unit)
        self.assertIn('ExecStart="/srv/panel/backend/.venv/bin/python" -m app.backup_runner', unit)
        self.assertIn('Environment="CP_DATA_DIR=/var/lib/panel"', unit)

    def test_guard_matrix(self):
        src = inspect.getsource(B._sync_timer)
        self.assertIn("unit_pct_escape(", src)
        self.assertIn("unit_env_value(", src)
        self.assertIn('f\'Environment="CP_DATA_DIR={_data}"', src)
        self.assertIn('f\'ExecStart="{_py}"', src)


# ---------------------------------------------------------------- 真 systemd
class TestRealSystemdFidelity(unittest.TestCase):
    """把字符串断言钉在真实 systemd 上：装 unit → 起服务 → 读回进程真正拿到的值。

    这一类不测"我们写出的字符串长什么样"，测"systemd 把它解释成什么"。
    需要 root：unit 必须落在 /etc/systemd/system 才会被 PID 1 看到，非 root
    （GitHub Actions 的 runner 就是普通用户）连文件都创建不了；那属于
    "没权限用它"，不是"环境没有 systemd"，同样只能整类跳过。
    """

    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest(f"需要 root 才能写 /etc/systemd/system（当前 euid={os.geteuid()}）")
        r = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True)
        if r.returncode not in (0, 1, 2) or r.stdout.strip() == "offline":
            raise unittest.SkipTest(f"本环境没有可用的 systemd：{r.stdout.strip() or r.stderr.strip()}")
        cls.root = tempfile.mkdtemp(prefix="cp_real_unit_")
        cls.installed = []

    @classmethod
    def tearDownClass(cls):
        for name in cls.installed:
            subprocess.run(["systemctl", "stop", f"{name}.service"], capture_output=True)
            subprocess.run(["systemctl", "reset-failed", name], capture_output=True)
            p = f"/etc/systemd/system/{name}.service"
            if os.path.exists(p):
                os.unlink(p)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        shutil.rmtree(cls.root, ignore_errors=True)

    def _install(self, unit_text: str) -> str:
        """写到真 unit 目录（systemd 只认这里），名字唯一、退出时清掉。"""
        name = f"cpue{next(_seq)}-{os.getpid()}"
        with open(f"/etc/systemd/system/{name}.service", "w") as f:
            f.write(unit_text)
        self.installed.append(name)
        return name

    def _start_and_read(self, name: str, out: str) -> str:
        for args in (["daemon-reload"], ["start", "--no-block", f"{name}.service"]):
            r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self.fail(f"systemctl {args} 失败：{r.stderr.strip()[:200]}")
        for _ in range(100):
            if os.path.exists(out) and os.path.getsize(out) > 0:
                with open(out) as f:
                    text = f.read()
                os.unlink(out)
                return text
            time.sleep(0.05)
        res = subprocess.run(
            ["systemctl", "show", f"{name}.service", "-p", "Result", "-p", "ExecMainStatus"],
            capture_output=True, text=True,
        ).stdout.split()
        self.fail(f"服务没有写出 {out}（{'/'.join(res)}）")

    # ---- 备份单元：路径与环境变量必须逐字到达 ----

    def _backup_unit(self, data_dir: str, backend_dir: str, python_bin: str) -> tuple[str, str]:
        """unit 文本 100% 由 backup_service._sync_timer 生成，不手写。

        返回 (unit 原文, 脚本落盘路径)：真 runner 会跑 pg_dump/tar，这里换成
        把环境与参数写盘的脚本，但 ExecStart 的路径写法与 unit 其余部分
        都还是被测代码产出的原文。
        """
        d = tempfile.mkdtemp(prefix="cp_ue_", dir=self.root)
        bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) "
            "VALUES('pg','appdb','daily',3,30,7,1)"
        )
        self.addCleanup(dbm.execute, "DELETE FROM backups WHERE id=?", (bid,))
        calls = []
        out = f"{d}/out"
        with (
            mock.patch.object(config, "UNIT_DIR", d),
            mock.patch.object(config, "DATA_DIR", data_dir),
            mock.patch.object(B, "BACKEND_DIR", backend_dir),
            mock.patch.object(B, "PYTHON_BIN", python_bin),
            mock.patch.object(B, "run", _fake_run(calls)),
        ):
            row = dbm.query_one("SELECT * FROM backups WHERE id=?", (bid,))
            asyncio.run(B._sync_timer(row))
        with open(python_bin, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'{{ echo "BIN=$0"; echo "A1=$1"; echo "WD=$PWD"; '
                f'echo "DATA=[$CP_DATA_DIR]"; }} > "{out}"\n'
            )
        os.chmod(python_bin, 0o755)
        with open(f"{d}/panel-backup-{bid}.service") as f:
            return f.read(), out

    def test_backup_unit_values_arrive_verbatim(self):
        """带空格、`%`、`$`、引号的 CP_DATA_DIR 必须逐字进进程环境。"""
        bin_dir = f"{self.root}/sp ace/a%b"
        os.makedirs(bin_dir, exist_ok=True)
        py = f"{bin_dir}/python"
        data = '/tmp/sp dir/a%b"q$X'
        backend = f"{self.root}/back end/pct%b"
        os.makedirs(backend, exist_ok=True)
        unit, out = self._backup_unit(data, backend, py)
        name = self._install(unit)
        text = self._start_and_read(name, out)
        self.assertIn(f'DATA=[{data}]', text, f"CP_DATA_DIR 被改写：\n{text}\n--- unit ---\n{unit}")
        self.assertIn(f"WD={backend}", text, f"WorkingDirectory 被改写：\n{text}")
        self.assertIn(f"BIN={bin_dir}", text, f"二进制路径被改写：\n{text}")

    def test_old_backup_form_truncates(self):
        """非空验证：旧写法（Environment= 不加引号、% 不转义）会被 systemd
        在空格处截断——证明上面那条不是恰好成立。"""
        bin_dir = f"{self.root}/sp2"
        os.makedirs(bin_dir, exist_ok=True)
        py = f"{bin_dir}/python"
        out = f"{bin_dir}/out"
        data = "/tmp/sp dir/a%b"
        with open(py, "w") as f:
            f.write('#!/bin/sh\n' f'{{ echo "DATA=[$CP_DATA_DIR]"; }} > "{out}"\n')
        os.chmod(py, 0o755)
        unit = (
            "[Unit]\nDescription=old form\n[Service]\nType=oneshot\n"
            f"Environment=CP_DATA_DIR={data}\n"
            f'ExecStart="{py}" -m app.backup_runner 1\n'
        )
        name = self._install(unit)
        text = self._start_and_read(name, out)
        self.assertIn("DATA=[/tmp/sp]", text, f"旧写法应当截断成 /tmp/sp，实际：\n{text}")

    # ---- 应用单元：systemd 交给 bash 的命令必须与面板显示的一致 ----

    def _delivered(self, start_cmd: str, extra_env: str = "") -> str:
        """渲染出 unit、把 ExecStart 换成"把 argv[2] 原样落盘"的形式，读回交付内容。"""
        d = tempfile.mkdtemp(prefix="cp_ue_app_", dir=self.root)
        out = f"{d}/cmdline.bin"
        dump = f"{d}/dump.sh"
        with open(dump, "w") as f:
            f.write("#!/bin/sh\ncat /proc/$PPID/cmdline > \"$1\"\n")
        os.chmod(dump, 0o755)
        # 结尾的 true 阻止 bash 对最后一条命令做 exec 优化，$PPID 才是那个 bash
        tail = f"; {dump} {out}; true"
        app = {"name": "demo-app", "path": d, "start_cmd": start_cmd + tail, "port": 3000,
               "unit_template": None, "unit_override": None, "env": None}
        unit = APPS.render_unit(app)
        if extra_env:
            unit = unit.replace("[Install]", f"{extra_env}\n[Install]")
        name = self._install(unit)
        # 先建空文件：下面的轮询直接 getsize，服务还没跑起来时不该报错
        with open(out, "wb") as f:
            pass
        for args in (["daemon-reload"], ["start", "--no-block", f"{name}.service"]):
            r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self.fail(f"systemctl {args} 失败：{r.stderr.strip()[:200]}")
        for _ in range(100):
            if os.path.getsize(out) > 0:
                break
            time.sleep(0.05)
        else:
            self.fail("服务没有写出 argv")
        # 用二进制读：cmdline 用 NUL 分隔，按行读会被值里的内容骗过
        with open(out, "rb") as f:
            fields = f.read().split(b"\0")
        os.unlink(out)
        self.assertGreater(len(fields), 2, f"cmdline 里没有 argv[2]：{fields}")
        argv2 = fields[2].decode()
        self.assertTrue(argv2.endswith(tail), f"argv[2]={argv2!r} 与注入的尾巴不匹配")
        return argv2[: -len(tail)]

    def test_start_cmd_reaches_bash_unchanged(self):
        for cmd in (
            "node server.js",
            'npm run "build"',
            "node a\\b --x",
            "node ünits --x",  # 非 ASCII：json.dumps 出 \\uXXXX，systemd 会解回 ü
            "printf tag=%b",
            "echo ${HOME} ${PORT:-3000}",
            "echo $HOME $x",
            "systemctl status %i",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._delivered(cmd), cmd)

    def test_start_cmd_secret_not_leaked_into_argv(self):
        """unit 环境里的密钥不许被 systemd 塞进 argv（cmdline 全局可读）。"""
        secret = "s3cr3t-db-password"
        got = self._delivered(
            "echo ${DB_PASSWORD}",
            extra_env=f"Environment=DB_PASSWORD={secret}\nEnvironment=ZZ={secret}\n",
        )
        self.assertNotIn(secret, got, f"密钥被 systemd 展开进命令行：{got!r}")
        self.assertEqual(got, "echo ${DB_PASSWORD}")

    def test_old_app_form_expands_dollar_and_percent(self):
        """非空验证：旧写法（json.dumps 原样、不做转义）会被 systemd 抢先展开。"""
        d = tempfile.mkdtemp(prefix="cp_ue_old_", dir=self.root)
        out = f"{d}/cmdline.bin"
        dump = f"{d}/dump.sh"
        with open(dump, "w") as f:
            f.write("#!/bin/sh\ncat /proc/$PPID/cmdline > \"$1\"\n")
        os.chmod(dump, 0o755)
        cmd = "echo ${ZZ} %b"
        tail = f"; {dump} {out}; true"
        unit = (
            "[Service]\nType=simple\n"
            f"WorkingDirectory={d}\n"
            "Environment=ZZ=from-unit-env\n"
            f'ExecStart=/bin/bash -lc {json.dumps(cmd + tail)}\n'
        )
        name = self._install(unit)
        for args in (["daemon-reload"], ["start", "--no-block", f"{name}.service"]):
            r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                self.fail(f"systemctl {args} 失败：{r.stderr.strip()[:200]}")
        for _ in range(100):
            if os.path.exists(out) and os.path.getsize(out) > 0:
                break
            time.sleep(0.05)
        else:
            self.fail("旧写法服务没有写出 argv")
        with open(out, "rb") as f:
            argv2 = f.read().split(b"\0")[2].decode()
        os.unlink(out)
        delivered = argv2[: -len(tail)]
        # 期望的坏结果：${ZZ} 被 unit 环境替换、%b 变成 /bin/sh。
        # 哪天 systemd 改了行为，这条会失败并提醒上面那些前提已经不成立。
        self.assertNotEqual(delivered, cmd, f"旧写法本该被展开，实际逐字：{delivered!r}")
        self.assertIn("from-unit-env", delivered, f"旧写法结果与实测展开行为不符：{delivered!r}")


if __name__ == "__main__":
    unittest.main()
