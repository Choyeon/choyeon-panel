"""批次㊶：本机子进程的超时，一个出处。

`util.run()` 的默认值是 120 秒，而 app/ 下 109 个调用点里 25 个根本没传超时。这 25 个不是
远程命令，全是本机一秒内回来的东西：`systemctl daemon-reload` 11 处（生产机实测 1.17s）、
`nginx -t` 3 处（0.088s）、`systemctl reload nginx` 4 处、`nginx -v`、`systemctl list-units`
（0.052s）、failed 单位表、`redis-cli INFO`/`DBSIZE`、`ln -sf`、`chmod`。剩下 84 个各自写了数，
同一条命令在不同地方拿到不同答案：`nginx -t` 自检那处写 10、nginx_ops 那三处不传（于是按 120 等），
`nginx -T` 一处 30、一处 20；`firewall-cmd --state` 一处 20、一处 10；
`systemctl show MainPID` 一处 5、一处 10；`ss -tlnp` 一处 4、一处 20——后者那个 20 还不是
裸数字，而是 exposure_service 自家立的 `SS_TIMEOUT = 20`，值跟共享常量一模一样，名字却是
私有的，所以两个模块各自"看着都对"。`journalctl` 更三份：`--disk-usage`（只 stat 目录）
自己配 15，`-k -b -n 3000` 配 8，`--since "24 hours ago"` 配 25——后两个真要扫日志、
预算该在调用点自己声明，第一个不该。于是**同一件事有两个答案**，
而默认那个答案是最糟的一个——它把"这条本机
命令两分钟没回来"当成正常等待。面板以 root 跑在客户机上，`nginx -t`/`reload nginx` 在
`_nginx_section()` 的全局锁里，`list-units` 在服务页加载路径上，卡住一个就钉住一串。
默认值因此按"最慢的常态本机命令"定：`CTRL_TIMEOUT` 给那条 1.17s 的命令留十倍余量，
只看一眼状态的探针另立 `PROBE_TIMEOUT`。远程命令（certbot、git、npm）不受影响——它们在
调用点本来就必须自己传更大的数。

第二件事更难看：`backup_runner.run_sh` 压根没有超时。夜间备份是 systemd timer 直接
`ExecStart=… -m app.backup_runner <id>` 起的，生产机 `systemctl show panel-backup-1.service
-p TimeoutStartUSec --value` 回的是 **infinity**（oneshot 没有起停超时），面板 API 那个
1800 秒的预算在这条路上根本不参与。于是一条卡死的 pg_dump（等一个长事务的锁、写一块卡住的
盘）就永远以 root 身份挂着，并按 flock 的语义一直攥着 `locks.key_backup(bid)`——flock 只在
进程死亡时由内核释放。之后每次 timer 到点都打印一句 "backup skipped" 返回 0，journal 和面板
一片祥和，直到真要恢复那天才发现磁盘上最近一份归档是三周前的。

超时之后还剩两个坑，一起收：`dump_cmd` 里那道 `|| { rm -f …; }` 在 SIGKILL 下没机会运行，
截断的归档必须我们自己删（`list_files()` 会把半份 gz 当可恢复备份端上来，还占 keep 名额
一格）；只 kill bash 会留下 `pg_dump`/`gzip` 两个孩子继续往没人认领的文件里写字，所以按
整个进程组杀。

顺带修掉三处"把命令失败当数据读"：`systemd_ops.list_services()` 直接按下标解析 `run()` 的
正文，拿到超时那句短正文就 IndexError；`_check_firewall()` 在 `ufw status` 失败时正文里自然
没有 "inactive"，于是把"没查到"报成"ufw 已启用"——安全项上的假绿灯；
`updates_service._auto_updates()` 在循环外落一份 "disabled"，超时那句正文因此变成"这台机器
没开自动安全更新"，而 `is-enabled` 对真的 disabled 单元本来就退 1，所以判据只能看正文、
只有读不到时才说 unknown。CLI 的安装/升级脚本超时判词也改成走 `timeout_note`，
"已终止整个进程组"这句话从此只有一个地方在写。
"""

from __future__ import annotations

import ast
import inspect
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch41_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch41_roots_"))

from app import backup_runner as BR
from app.services import firewall_service, systemd_ops
from app.util import CTRL_TIMEOUT, PROBE_TIMEOUT, run, timeout_note

BACKEND = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND / "app"

# 这些命令的产出只是"本机现在的一个事实"，或本机一秒内完成的控制动作（daemon-reload、
# nginx reload）：读不到就报，绝不该按分钟等。
# 值是允许出现的参数词（None = 这条命令带任何参数都算瞬时本地操作）。
LOCAL_READS = {
    "systemctl": {"list-units", "list-unit-files", "list-timers", "show", "is-active",
                  "is-enabled", "status", "daemon-reload", "reload"},
    "nginx": {"-v", "-t", "-T"},
    "redis-cli": {"INFO", "DBSIZE"},
    "ufw": {"status"},
    "firewall-cmd": {"--state", "--get-default-zone", "--list-all"},
    "ln": None,
    "chmod": None,
    "which": None,
    "ss": None,
    # `journalctl --disk-usage` 只 stat 日志目录；同一程序的另两种用法（`-k -b -n 3000`、
    # `--since "24 hours ago"`）要扫日志，预算必须在调用点自己声明，不归这条管。
    "journalctl": {"--disk-usage"},
}
# AST 里读到这些名字时按它们的值判，其余名字（变量传参）跳过而不是猜。
_NAMED = {"CTRL_TIMEOUT": CTRL_TIMEOUT, "PROBE_TIMEOUT": PROBE_TIMEOUT, "SUBPROCESS_TIMEOUT": 6}


def _strs(node) -> list:
    """一个 AST 节点里的字符串字面量；不是列表（变量传参）就给空表。"""
    if isinstance(node, ast.List):
        return [n.value for n in node.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return []


def _cmd_and_args(call: ast.Call):
    """取 (命令名, 参数字面量列表)：`run("systemctl", ["show", …])` 与 `subprocess.run(["nginx", "-T"], …)`。

    参数是变量（`run("systemctl", args, …)`）时只回命令名、参数表为空——这种情况判不出
    它读的是哪条子命令，宁可当成"不是本机读"放过，也不要猜一个词把它算进白名单。
    """
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        if " " in first.value:
            # run_shell("ss -tlnp")：整条命令行写在一个字符串里，第一个词才是程序名。
            head, *rest = first.value.split()
            return head, rest
        return first.value, _strs(call.args[1] if len(call.args) > 1 else None)
    if isinstance(first, ast.List) and first.elts and isinstance(first.elts[0], ast.Constant) \
            and isinstance(first.elts[0].value, str):
        return first.elts[0].value, _strs(ast.List(elts=first.elts[1:]))
    return None, []


def _timeout_of(call: ast.Call, positional_index: int):
    """(秒数, 是否显式传参)。解析不了返回 (None, …)。没传就是默认值 CTRL_TIMEOUT。"""
    for kw in call.keywords:
        if kw.arg != "timeout":
            continue
        if isinstance(kw.value, ast.Name):
            return _NAMED.get(kw.value.id), True
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, (int, float)):
            return kw.value.value, True
        return None, True
    if len(call.args) > positional_index:
        node = call.args[positional_index]
        if isinstance(node, ast.Name):
            return _NAMED.get(node.id), True
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value, True
        return None, True
    return CTRL_TIMEOUT, False


def _iter_calls():
    """产出 (文件, 调用节点, timeout 的位置下标, 不传参数时的预算是不是 CTRL_TIMEOUT)。"""
    for py in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func
            if isinstance(f, ast.Name):
                if f.id == "run":
                    yield py, node, 3, True          # run(cmd, args, cwd, timeout)
                elif f.id in ("run_shell", "run_shell_async"):
                    yield py, node, 1, True          # run_shell(cmd, timeout)
            elif isinstance(f, ast.Attribute) and f.attr == "run" \
                    and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
                yield py, node, 1, False             # subprocess.run([...], input, timeout, …)


def _is_read(cmd: str, toks: list) -> bool:
    allowed = LOCAL_READS.get(cmd)
    if allowed is None:
        return cmd in LOCAL_READS
    return any(t in allowed for t in toks)


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


class TestTimeoutOneSource(unittest.TestCase):
    """本机命令的超时只能来自那两个常量。"""

    def test_run_default_is_ctrl_timeout(self):
        tree = ast.parse((APP_DIR / "util.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
        defaults = fn.args.defaults
        names = [a.arg for a in fn.args.args[len(fn.args.args) - len(defaults):]]
        node = dict(zip(names, defaults, strict=True))["timeout"]
        self.assertIsInstance(node, ast.Name, f"默认值必须是常量名，实际是 {ast.dump(node)}")
        self.assertEqual(node.id, "CTRL_TIMEOUT", "默认值就是「没人传参数时的答案」，不许再写裸数字")

    def test_constants_are_defensible(self):
        # 下限：盖住生产机实测最慢的常态本机命令（daemon-reload 1.17s）十倍余量；
        # 上限：再长就不再是"本机命令"的口径，那种等待该由调用点自己声明。
        self.assertGreaterEqual(CTRL_TIMEOUT, 10)
        self.assertLessEqual(CTRL_TIMEOUT, 60)
        self.assertLess(PROBE_TIMEOUT, CTRL_TIMEOUT)

    def test_no_local_read_waits_long(self):
        bad = []
        for py, call, idx, _default in _iter_calls():
            cmd, toks = _cmd_and_args(call)
            if cmd not in LOCAL_READS or not _is_read(cmd, toks):
                continue
            secs, _ = _timeout_of(call, idx)
            if secs is None or secs <= CTRL_TIMEOUT:
                continue
            bad.append(f"{py.relative_to(BACKEND)}:{call.lineno} {cmd} {toks[:1]} 等 {secs}s")
        self.assertEqual(bad, [], "本机读命令的超时不能超过 CTRL_TIMEOUT：\n" + "\n".join(bad))

    def test_default_budget_only_covers_local_reads(self):
        """不传超时的本机调用点（`run()` 与 `run_shell*()`）必须全在本机读白名单里。

        把默认值从 120 秒压到 20 秒的唯一前提是：没传参数的那些调用全是本机命令。
        这条用例守的就是这个前提——以后谁给 git/certbot/npm 少写一个 timeout，它直接红，
        而不是让那条远程命令悄悄按 20 秒死掉（那种失败在页面上表现为"克隆突然不行了"）。
        """
        offenders = []
        for py, call, idx, default_is_ctrl in _iter_calls():
            if not default_is_ctrl or _timeout_of(call, idx)[1]:
                continue  # subprocess.run 不吃 CTRL_TIMEOUT 这个默认值，不归这条管
            cmd, toks = _cmd_and_args(call)
            if cmd not in LOCAL_READS or not _is_read(cmd, toks):
                offenders.append(f"{py.relative_to(BACKEND)}:{call.lineno} {cmd}")
        self.assertEqual(offenders, [], "这些调用点吃的是 CTRL_TIMEOUT，得是本机读命令：\n"
                                        + "\n".join(offenders))

    def test_probe_timeout_name_means_one_thing(self):
        """`PROBE_TIMEOUT` 这个名字在后端只指本机命令探针。

        alerts_service 原来自己也有一份 `PROBE_TIMEOUT = 3.0`（一次 HTTP 探活的网络预算）。
        从 util 导入同名常量之后，那个 3.0 会被静静地换成 5：HTTP 探活、TCP 探活和
        `systemctl list-units` 全按另一个预算等，而两份代码读起来都"没错"。
        现在它叫 HTTP_PROBE_TIMEOUT。
        """
        src = (APP_DIR / "services" / "alerts_service.py").read_text(encoding="utf-8")
        assigns = [ln.strip() for ln in src.splitlines() if "PROBE_TIMEOUT = " in ln]
        self.assertEqual(assigns, ["HTTP_PROBE_TIMEOUT = 3.0"],
                         f"3.0 那份必须带 HTTP_ 前缀，别占用常量名：{assigns}")
        from app.services import alerts_service as al
        self.assertEqual(al.PROBE_TIMEOUT, PROBE_TIMEOUT)
        self.assertEqual(al.HTTP_PROBE_TIMEOUT, 3.0)

    def test_local_reads_do_not_invent_a_number(self):
        """同一族命令不许出现第三个数：超时要么不传（走默认），要么用那两个常量名。"""
        stray = []
        for py, call, _idx, _default in _iter_calls():
            cmd, toks = _cmd_and_args(call)
            if cmd not in LOCAL_READS or not _is_read(cmd, toks):
                continue
            for kw in call.keywords:
                if kw.arg != "timeout" or not isinstance(kw.value, ast.Constant):
                    continue
                if kw.value.value not in (CTRL_TIMEOUT, PROBE_TIMEOUT, 6):
                    stray.append(f"{py.relative_to(BACKEND)}:{call.lineno} {cmd} 写了裸数字 {kw.value.value}")
        self.assertEqual(stray, [], "请改用 CTRL_TIMEOUT / PROBE_TIMEOUT：\n" + "\n".join(stray))

    def test_probe_sites_use_probe_timeout(self):
        """这些「只显示一行状态 / 只取一个数」的探针必须按 PROBE_TIMEOUT 记账。"""
        pairs = [
            (
                APP_DIR / "services" / "nginx_ops.py",
                'run("nginx", ["-v"], timeout=PROBE_TIMEOUT',
                "nginx -v 是版本探针，不该按控制命令的预算等",
            ),
            (
                APP_DIR / "services" / "systemd_ops.py",
                '["list-units", "--type=service", "--all", "--no-legend", "--plain", "--no-pager"], '
                "timeout=PROBE_TIMEOUT",
                "服务列表页每 15s 刷一次，探针不能钉住请求",
            ),
            (
                APP_DIR / "services" / "alerts_service.py",
                '"--state=failed", "--no-legend", "--plain"], timeout=PROBE_TIMEOUT',
                "告警轮询里的 failed 检查是状态读取",
            ),
            (
                APP_DIR / "services" / "apps_service.py",
                'run_shell_async("ss -tlnp", timeout=PROBE_TIMEOUT)',
                "这一处 ss 只为应用详情补一个端口列",
            ),
            (
                APP_DIR / "services" / "apps_service.py",
                '["show", unit, "-p", "MainPID", "--value"], timeout=PROBE_TIMEOUT',
                "MainPID 是看一眼就走的读取，和 systemd_ops 里那条 show 用同一个预算",
            ),
            (
                APP_DIR / "services" / "storage_service.py",
                'run("journalctl", ["--disk-usage"], timeout=PROBE_TIMEOUT',
                "--disk-usage 只 stat 目录，不该占一份自己的 15 秒",
            ),
        ]
        for path, needle, why in pairs:
            self.assertIn(needle, _flat(path), f"{path.name}：{why}")

    def test_no_module_keeps_a_private_copy(self):
        """别在模块里再立一份和共享常量同值的私有超时名。

        exposure_service 原来写着 `SS_TIMEOUT = 20`：值就是 CTRL_TIMEOUT，名字却是自家的，
        于是同一条 `ss -tlnp` 在两个模块里各有出处（apps_service 那份写 4）。
        要 20 秒就导入 CTRL_TIMEOUT，要 5 秒就导入 PROBE_TIMEOUT。
        """
        dupes = []
        for py in sorted(APP_DIR.rglob("*.py")):
            if py.name == "util.py":
                continue
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for n in tree.body:
                if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Constant):
                    continue
                v = n.value.value
                if isinstance(v, (int, float)) and v in (CTRL_TIMEOUT, PROBE_TIMEOUT) \
                        and any(isinstance(t, ast.Name) and "TIMEOUT" in t.id for t in n.targets):
                    dupes.append(f"{py.relative_to(BACKEND)}:{n.lineno} {n.targets[0].id} = {v}")
        self.assertEqual(dupes, [], "这些私有常量是共享预算的副本：\n" + "\n".join(dupes))

    def test_timeout_note_single_wording(self):
        hits = [str(py.relative_to(BACKEND.parent))
                for py in list(APP_DIR.rglob("*.py")) + [BACKEND / "cli.py"]
                if py.name != "util.py" and "已终止整个进程组" in py.read_text(encoding="utf-8")]
        self.assertEqual(hits, [], f"措辞应从 util.timeout_note 来，别自己再拼一句：{hits}")

    def test_backup_budget_referenced_not_recopied(self):
        """一次备份的时间预算只有一个出处：service 侧两处引用常量，不再各抄一个 1800。"""
        src = (APP_DIR / "services" / "backup_service.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"timeout: int = backup_runner\.JOB_TIMEOUT_SEC")
        self.assertRegex(src, r"timeout=backup_runner\.JOB_TIMEOUT_SEC")
        self.assertNotIn("timeout=1800", src)
        self.assertNotIn("timeout: int = 1800", src)

    def test_cmd_budget_smaller_than_job(self):
        self.assertLess(BR.CMD_TIMEOUT_SEC, BR.JOB_TIMEOUT_SEC)
        self.assertEqual(BR.JOB_TIMEOUT_SEC - BR.CMD_TIMEOUT_SEC, 120)


class TestTimeoutNote(unittest.TestCase):
    def test_names_the_program(self):
        note = timeout_note("nginx", 20)
        self.assertIn("nginx", note)
        self.assertIn("超时", note)
        self.assertIn("20", note)

    def test_int_and_float_rendered(self):
        self.assertIn("（20s", timeout_note("x", 20))
        self.assertIn("（0.5s", timeout_note("x", 0.5))


class TestRunBehaviour(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_message_names_the_program(self):
        r = await run("sleep", ["30"], timeout=1)
        self.assertEqual(r["code"], 124)
        self.assertIn("sleep", r["out"], "判词要指出是哪条命令卡住，而不是一句 timeout")
        self.assertIn("超时", r["out"])
        self.assertNotIn("30", r["out"], "argv 不进判词：那里面可能带口令与连接串")

    async def test_default_applies_when_not_passed(self):
        with mock.patch("app.util.asyncio.wait_for", side_effect=TimeoutError) as wf:
            r = await run("true", [])
        self.assertEqual(r["code"], 124)
        self.assertEqual(wf.call_args_list[0].kwargs["timeout"], CTRL_TIMEOUT,
                         "第一条 wait_for 就是 communicate 的等待窗口，它必须等于默认预算")


class TestRunShTimeout(unittest.TestCase):
    """runner 的超时：杀掉整组、删掉半成品、报 124。"""

    def test_exit_code_passthrough(self):
        self.assertEqual(BR.run_sh("true", timeout=10), 0)
        self.assertEqual(BR.run_sh("exit 3", timeout=10), 3)
        self.assertEqual(BR.run_sh("exit 1", timeout=10), 1,
                         "tar 的 1 要原样传出，否则活跃目录的备份会被当致命错误删掉")

    def test_timeout_kills_group_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as td:
            out = f"{td}/bk9-2026-10-15.dump.gz"
            pidf = f"{td}/pids"
            with open(out, "w"):
                pass  # _free_path 原子占名留下的 0 字节文件
            producer = f"sh -c 'echo $$ > {pidf}; sleep 600 & echo $! >> {pidf}; wait'"
            started = time.monotonic()
            code = BR.run_sh(BR.dump_cmd(producer, out), out, timeout=1)
            self.assertEqual(code, 124, "超时必须报 124，让 main() 的 code != 0 / > 1 分支判失败")
            self.assertLess(time.monotonic() - started, 15, "等满 600 秒说明进程组没杀干净")
            self.assertFalse(os.path.exists(out), "截断的归档不能留给 list_files() 当「可恢复的备份」")
            pids = [int(x) for x in Path(pidf).read_text().split()]
            self.assertEqual(len(pids), 2, "外层 sh 与它后面的 sleep 都要有下落")
            for pid in pids:
                deadline = time.monotonic() + 5
                alive = True
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    time.sleep(0.05)
                self.assertFalse(alive, f"pid {pid} 还在：只 kill 直接子进程会留下人继续写字节")

    def test_timeout_without_out_file(self):
        self.assertEqual(BR.run_sh("sleep 30", timeout=1), 124,
                         "没有产物路径的调用不能因为删文件而炸")

    def test_signature_defaults(self):
        sig = inspect.signature(BR.run_sh)
        self.assertEqual(sig.parameters["timeout"].default, BR.CMD_TIMEOUT_SEC)
        self.assertIsNone(sig.parameters["out_file"].default)


class TestFailureIsNotData(unittest.IsolatedAsyncioTestCase):
    """命令没跑成的那句正文，不能当表格数据往下读。"""

    async def test_list_services_raises_on_timeout(self):
        async def fake_run(cmd, args, **kw):
            return {"code": 124, "out": timeout_note(cmd, PROBE_TIMEOUT)}
        with mock.patch.object(systemd_ops, "run", fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                await systemd_ops.list_services()
        self.assertIn("list-units", str(ctx.exception))
        self.assertIn("超时", str(ctx.exception), "要把真实原因带出去，不能只回一个空列表装成「没有服务」")

    async def test_list_services_skips_short_lines(self):
        async def fake_run(cmd, args, **kw):
            return {"code": 0, "out": "a.service loaded active running 说明\n只有一行一个词"}
        with mock.patch.object(systemd_ops, "run", fake_run):
            rows = await systemd_ops.list_services()
        self.assertEqual([r["unit"] for r in rows], ["a.service"])

    async def test_list_services_happy_path(self):
        async def fake_run(cmd, args, **kw):
            return {"code": 0, "out": "nginx.service loaded active running A high performance web server"}
        with mock.patch.object(systemd_ops, "run", fake_run):
            rows = await systemd_ops.list_services()
        self.assertEqual(rows[0]["desc"], "A high performance web server")

    async def test_auto_updates_timeout_is_not_disabled(self):
        """`is-enabled` 没跑成，不能报成"这台机器没开自动安全更新"。"""
        from app.services import updates_service as us

        async def fake_run(cmd, args, **kw):
            return {"code": 124, "out": timeout_note(cmd, PROBE_TIMEOUT)}
        with mock.patch.object(us, "run", fake_run):
            got = await us._auto_updates(us.DEBIAN)
        self.assertEqual(got["state"], "unknown")

    async def test_auto_disabled_still_reported(self):
        """反过来也别把真的没开读成 unknown：`is-enabled` 对 disabled 单元就是退 1。"""
        from app.services import updates_service as us

        async def fake_run(cmd, args, **kw):
            return {"code": 1, "out": "disabled"}
        with mock.patch.object(us, "run", fake_run):
            got = await us._auto_updates(us.DEBIAN)
        self.assertEqual(got["state"], "disabled")
        self.assertIn(got["unit"], us.AUTO_UNITS_BY_FAMILY[us.DEBIAN])


class TestDoctorHonesty(unittest.IsolatedAsyncioTestCase):
    """自检项：查不到就是查不到，尤其防火墙那一项。

    批次㊷之后 `_check_firewall()` 不再自己起子进程，只渲染 `firewall_service.read()` 那一份
    事实，所以这里喂的是 `read()` 的产出形态（三态 `active` + `state_ok`/`rules_ok`/`reason`），
    判词本身由 service 层的用例盯。
    """

    async def _firewall(self, **over):
        from app.routers import meta

        base = {
            "tool": "ufw", "active": True, "state_ok": True, "rules_ok": True, "reason": "",
            "rules": [], "protected": [22], "protected_reasons": {"22": "sshd 监听端口"},
        }
        base.update(over)

        async def fake_read(*a, **kw):
            return dict(base)

        with mock.patch.object(firewall_service, "read", fake_read):
            return await meta._check_firewall()

    @staticmethod
    def _why(label: str, code: int, text: str = "") -> str:
        """reason 串由 service 层生成，这里只借用同一句，免得测试里的措辞和线上对不上。"""
        return firewall_service._not_read(label, code, text)

    async def _nginx(self, ret):
        from app.routers import meta

        async def fake_run(cmd, args, **kw):
            return ret
        with mock.patch.object(meta, "run", fake_run), \
                mock.patch.object(meta.shutil, "which", lambda x: "/usr/sbin/nginx"):
            return await meta._check_nginx()

    async def test_ufw_timeout_is_not_enabled(self):
        item = await self._firewall(active=None, state_ok=False, rules_ok=False,
                                    reason=self._why("ufw status verbose", 124, timeout_note("ufw", CTRL_TIMEOUT)))
        self.assertEqual(item["status"], "warn", "超时报成 pass 就是安全项上的假绿灯")
        self.assertIn("无法确认", item["detail"])

    async def test_ufw_query_failure_carries_exit_code(self):
        item = await self._firewall(active=None, state_ok=False, rules_ok=False,
                                    reason=self._why("ufw status verbose", 2))
        self.assertEqual(item["status"], "warn")
        self.assertIn("退出码 2", item["detail"])

    async def test_ufw_inactive_still_warn(self):
        item = await self._firewall(active=False)
        self.assertEqual(item["status"], "warn")
        self.assertIn("未启用", item["detail"])

    async def test_rules_table_not_read_is_not_zero_rules(self):
        """状态读到了、规则表没读到：这一项不能说"已启用，0 条入站规则"。"""
        item = await self._firewall(rules_ok=False, reason=self._why("ufw status numbered", 6))
        self.assertEqual(item["status"], "warn")
        self.assertNotIn("0 条", item["detail"])

    async def test_ufw_active_pass(self):
        item = await self._firewall(rules=[{"num": 1}, {"num": 2}])
        self.assertEqual(item["status"], "pass")
        self.assertIn("2 条入站规则", item["detail"])

    async def test_firewalld_query_failure_is_not_running(self):
        item = await self._firewall(tool="firewalld", active=None, state_ok=False, rules_ok=False,
                                    reason=self._why("firewall-cmd --state", 124, timeout_note("firewall-cmd", CTRL_TIMEOUT)))
        self.assertEqual(item["status"], "warn")
        self.assertIn("无法确认", item["detail"], "查失败和「确实没在运行」要分得开")

    async def test_firewalld_confirmed_stopped_names_the_unit(self):
        item = await self._firewall(tool="firewalld", active=False, rules_ok=False)
        self.assertEqual(item["status"], "warn")
        self.assertIn("未在运行", item["detail"])

    async def test_no_firewall_tool_is_its_own_answer(self):
        item = await self._firewall(tool="none", active=False)
        self.assertEqual(item["status"], "warn")
        self.assertIn("未检测到", item["detail"])

    async def test_nginx_timeout_is_not_config_error(self):
        item = await self._nginx({"code": 124, "out": timeout_note("nginx", CTRL_TIMEOUT)})
        self.assertEqual(item["status"], "warn")
        self.assertNotIn("配置有误", item["detail"], "这句指控会把人引去翻一份本来正确的配置")

    async def test_nginx_real_failure_is_fail(self):
        item = await self._nginx({"code": 1, "out": "[emerg] unknown directive"})
        self.assertEqual(item["status"], "fail")


if __name__ == "__main__":
    unittest.main()
