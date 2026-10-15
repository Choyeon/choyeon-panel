"""批次㊱：子进程跟着这一次调用死。

`util.run()` 是面板里绝大多数 root 命令的执行入口，`pg_service.psql()`、`_rcli_raw()`、
`cron_service._exec_task()`、`system.py` 的日志 SSE 流是另外四处自己 spawn 的地方。
它们共同的缺陷：命令按 `start_new_session=True` 起在独立进程组里，而进程组只由
`kill_group()` 收 —— 超时和"输出过大"都收，被取消时不收。asyncio 的 CancelledError
自 3.8 起继承 BaseException 而不是 Exception，所以原来那层 `except Exception` 的清理
分支根本看不见取消：客户端断开 SSE、nginx 的 proxy_read_timeout 掐断一条长请求、
面板优雅停机撤销在飞任务，都会把一个 root 身份的 certbot / git fetch / psql /
journalctl -f 留在身后继续跑，管道没人读、退出码没人收，下一轮再来一个。
面板以 root 运行，这些遗孤就是别人接管这台机器的入口。

守卫分两类：行为守卫（真起一个慢子进程、取消这次调用，再去 /proc 确认它没了）和
静态形状守卫（这几处代码必须还是这个形状，否则修复会被下一次改动静默抹掉）。

同一批里还有前端的两个方向：一是"每个请求都要有自己的失败说法"（一处取数失败不许让
隔壁区块显示成空数据，也不许永远停在「加载中」），二是 375px 逐页签走查量到的说明句裁切
——`.mono-dim` / `.cell-sub` 是为表格单元格设计的（nowrap + ellipsis，全文另有触发器），
被抄进 `.cp-help` 说明句和窄屏卡片之后，路径与命令行这种没有空格可断的长串就整条没了。
"""
import asyncio
import contextlib
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from app.util import run

BACKEND = Path(__file__).resolve().parents[1]
WEB = BACKEND.parent / "web" / "src"


def _body(rel: str, header: str) -> str:
    """取一个顶层函数/异步函数的函数体（到下一个顶格 def/class 为止）。"""
    src = (BACKEND / rel).read_text(encoding="utf-8")
    start = src.index(header) + len(header)
    rest = src[start:]
    m = re.search(r"\n(?=(async\s+def |def |class ))", rest)
    return rest[: m.start()] if m else rest


def _pid_alive(pid: int) -> bool:
    """进程还在（或成了没人收的僵尸）就算活着。僵尸也要算：它占着 pid 和表项。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if os.name != "posix":
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b") ", 1)[1].split()[0]
    except OSError:
        return False
    return state != b"Z"


class RunDiesWithTheCall(unittest.IsolatedAsyncioTestCase):
    """util.run：正常返回、超时、被取消，三条路出去时孩子都必须没了。"""

    def setUp(self):
        self._pids = []
        self._tasks = []

    async def asyncTearDown(self):
        # 断言失败时也不能把 sleep 300 留给下一轮测试（或这台机器）
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for pid in self._pids:
            if _pid_alive(pid):
                with contextlib.suppress(OSError):
                    os.killpg(os.getpgid(pid), 9)

    async def _start_slow(self, pidfile: Path):
        """起一个会把自身 pid 落盘的长命令，返回 (pid, 那次 run 的 task)。

        用 `exec sleep 300` 是让 sleep 顶掉 sh 而不是当它的儿子：这样落盘的 pid 就是
        进程组组长，也是 kill_group 唯一的靶子，不会把"儿子死了父亲还在"读成通过。
        """
        task = asyncio.create_task(
            run("sh", ["-c", f"echo $$ > '{pidfile}'; exec sleep 300"], timeout=120)
        )
        self._tasks.append(task)
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip().isdigit():
                pid = int(pidfile.read_text().strip())
                self._pids.append(pid)
                return pid, task
            await asyncio.sleep(0.05)
        task.cancel()
        self.fail("子进程没起来，本测试无法覆盖取消/超时路径")
        raise AssertionError

    @staticmethod
    async def _wait_gone(pid: int, seconds: float = 6.0) -> bool:
        deadline = time.time() + seconds
        while _pid_alive(pid) and time.time() < deadline:
            await asyncio.sleep(0.05)
        return not _pid_alive(pid)

    async def test_cancel_kills_the_child(self):
        with tempfile.TemporaryDirectory() as td:
            pid, task = await self._start_slow(Path(td) / "pid")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(
                await self._wait_gone(pid),
                f"取消后 pid={pid} 还活着或成了僵尸：root 身份的 sleep 300 留下来了",
            )

    async def test_timeout_kills_and_reaps(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "pid"
            task = asyncio.create_task(
                run("sh", ["-c", f"echo $$ > '{pidfile}'; exec sleep 300"], timeout=1)
            )
            self._tasks.append(task)
            r = await task
            self.assertEqual(r["code"], 124)
            self.assertIn("timeout", r["out"])
            pid = int(pidfile.read_text().strip())
            self._pids.append(pid)
            # reap 的意义就在这：不 await proc.wait() 的话这里会是个 Z
            self.assertTrue(await self._wait_gone(pid), f"超时后 pid={pid} 还活着或成了僵尸")

    async def test_normal_paths_still_work(self):
        self.assertEqual((await run("sh", ["-c", "echo ok"]))["out"], "ok")
        self.assertEqual((await run("definitely-not-a-command-zz", []))["code"], 127)


class SpawnSitesStayGuarded(unittest.TestCase):
    """静态形状：五处 spawn 的清理分支不许被改回"只有超时那一条"。"""

    def test_util_run_has_a_baseexception_branch(self):
        body = _body("app/util.py", "async def run(")
        self.assertIn("except BaseException:", body)
        branch = body.split("except BaseException:", 1)[1]
        self.assertIn("kill_group(proc)", branch)
        self.assertIn("raise", branch, "取消必须原样往上抛，不能吞成 code=1")
        # 超时支要 reap：不 wait 的 communicate 会留下僵尸
        timeout_branch = body.split("except TimeoutError:", 1)[1].split("return {", 1)[0]
        self.assertIn("await reap(proc)", timeout_branch)

    def test_psql_cannot_leave_the_su_group(self):
        body = _body("app/services/pg_service.py", "async def psql(")
        self.assertIn("finally:", body)
        final = body.split("finally:", 1)[1]
        self.assertIn("if proc.returncode is None:", final)
        self.assertIn("kill_group(proc)", final)

    def test_rcli_raw_cannot_leave_redis_cli(self):
        body = _body("app/services/pg_service.py", "async def _rcli_raw(")
        final = body.split("finally:", 1)[1]
        self.assertIn("if proc.returncode is None:", final)
        self.assertIn("kill_group(proc)", final)

    def test_cron_exec_task_kills_on_cancel(self):
        body = _body("app/services/cron_service.py", "async def _exec_task(")
        self.assertIn("except BaseException:", body)
        branch = body.split("except BaseException:", 1)[1].split("except FileNotFoundError", 1)[0]
        self.assertIn("kill_group(proc)", branch)
        self.assertIn("raise", branch)
        self.assertIn("await reap(proc)", body.split("except TimeoutError:", 1)[1])

    def test_journal_sse_sets_up_before_it_spawns(self):
        body = _body("app/routers/system.py", "async def _sse_journal(")
        spawn = body.index("create_subprocess_exec(")
        baseline = body.index("base = await asyncio.to_thread(security.session_identity, user)")
        self.assertLess(baseline, spawn, "身份基线必须赶在起进程之前取")
        self.assertLess(body.index("until = time.monotonic() + _RECHECK_SEC"), spawn)
        # 起进程与 try 之间同样不允许任何 await：它一抛，journalctl -f 就成了没人认领的
        # 长命进程，finally 根本轮不到执行，而浏览器端 EventSource 断了自动重连，
        # 每一次重连多留一个，直到把面板的 fd 吃光。
        gap = body[spawn: body.index("try:", spawn)]
        self.assertNotIn("await", gap, "spawn 到 try 之间不允许 await")

    def test_certbot_certificates_timeout_is_one_number(self):
        alerts = (BACKEND / "app" / "services" / "alerts_service.py").read_text(encoding="utf-8")
        self.assertIn("CERTBOT_TIMEOUT", alerts)
        self.assertIn('await run("certbot", ["certificates"], timeout=CERTBOT_TIMEOUT)', alerts)
        ops = _body("app/services/cert_ops.py", "def _from_certbot")
        self.assertIn("timeout=CERTBOT_TIMEOUT", ops)


class EveryFetchHasAFailureStory(unittest.TestCase):
    """静态形状：前端每个取数入口的失败都得有说法，而且不许连坐到别的卡片上。"""

    def _web(self, rel: str) -> str:
        return (WEB / rel).read_text(encoding="utf-8")

    def test_logcenter_and_logstream_bind_their_error(self):
        lc = self._web("views/LogCenter.vue")
        self.assertIn("error: svcError", lc)
        self.assertIn("error: logOptsError", lc)
        self.assertIn("const degraded = computed(", lc)
        self.assertIn('v-if="degraded"', lc)
        ls = self._web("components/LogStream.vue")
        self.assertIn("error: optsError", ls)

    def test_services_reports_the_override_file_fetch_failure(self):
        self.assertIn("error: unitFilesError", self._web("views/Services.vue"))

    def test_cron_and_backup_report_their_list_fetch(self):
        self.assertIn("error: listError", self._web("views/CronTasks.vue"))
        self.assertIn("error: appsError", self._web("views/Backups.vue"))

    def test_description_key_column_wraps_instead_of_clipping(self):
        dsc = self._web("components/DescriptionsCard.vue")
        rule = dsc.split(".dsc-k {", 1)[1].split("}", 1)[0]
        self.assertNotIn("white-space: nowrap", rule)
        self.assertNotIn("text-overflow: ellipsis", rule)
        self.assertIn("white-space: normal", rule)

    def test_dashboard_no_longer_lets_one_request_blame_another(self):
        db = self._web("views/Dashboard.vue")
        # history 与 metrics-status、apps 与 services 各自落账
        self.assertIn("const [h, s] = await Promise.allSettled([", db)
        self.assertIn("const [a, sv] = await Promise.allSettled([", db)
        self.assertIn("appsError", db)
        self.assertIn("mstatError", db)
        self.assertIn("samplerTag", db)
        # api.info() 那个空 catch 是这一轮的靶子：失败要说出来，也不许永远停在「加载中」
        self.assertNotIn("api.info().then(", db)
        self.assertIn("async function loadInfo()", db)
        self.assertIn("if (infoError.value && n % 5 === 0) loadInfo();", db)


class ProseAndCardsWrapTheirLongTokens(unittest.TestCase):
    """`.mono-dim` / `.cell-sub` / `.mono-1l` 都自带 nowrap + ellipsis —— 那是为表格单元格
    设计的（列宽所限，全文另有触发器）。批次㊱A 的 375px 逐页签走查量到的是同一批类被用在
    说明句和窄屏卡片里：`.cp-help` 里那个 unit 文件路径实测 323px 内容压进 301px 的盒子，
    尾巴直接没了；手机上没有 hover，也就没有 title 可补。"""

    def _web(self, rel: str) -> str:
        return (WEB / rel).read_text(encoding="utf-8")

    def test_named_prose_sites_carry_the_wrap_trigger(self):
        sites = [
            ("views/AppDetail.vue", '文件：<span class="mono-dim cp-break">{{ unitInfo?.path }}</span>'),
            ("views/CronTasks.vue", '将执行：<span class="mono-dim cp-break">{{ buildArgv()'),
            ("views/Settings.vue", '<span class="mono-dim cp-break">choyeonctl user 2fa-disable'),
            ("views/Firewall.vue", '<div class="cell-sub cp-break">{{ r.source'),
        ]
        for rel, needle in sites:
            with self.subTest(site=rel):
                self.assertIn(needle, self._web(rel))

    def test_logcenter_result_lines_use_the_help_form_not_a_nowrap_cell(self):
        lc = self._web("views/LogCenter.vue")
        self.assertIn('<div v-if="jResult" class="cp-help"', lc)
        self.assertIn('<div v-else-if="nResult" class="cp-help"', lc)
        # 回到 .cell-sub 就是回到 nowrap：这两条是整句说明，不是一格数据
        self.assertNotIn('class="cell-sub" style="display: block', lc)

    def test_no_new_unwrapped_nowrap_class_in_template_markup(self):
        """模板标记（不是 columns.render 里的 h()）出现这三个类时，必须同时挂共享折行触发器。
        例外是这几条量过的短标签：最长一条「跟随（每 5 秒按同样条件重查）」在 301px 里也放不下
        第二行，但它们没有裁切风险，写死在白名单里，新增一处不挂触发器就会红。"""
        short_labels = {
            "views/CronTasks.vue": ["等待有效的计划表达式"],
            "views/Firewall.vue": ["受保护"],
            "views/LogCenter.vue": ["只读查询，不写入任何数据", "秒按同样条件重查", "快捷入口："],
        }
        offenders = []
        for path in sorted(WEB.rglob("*.vue")):
            rel = str(path.relative_to(WEB)).replace("\\", "/")
            for line in path.read_text(encoding="utf-8").splitlines():
                for cls_attr in re.findall(r'class="([^"]*)"', line):
                    if not re.search(r"\b(mono-dim|cell-sub|mono-1l)\b", cls_attr):
                        continue
                    if re.search(r"\b(cp-break|cp-prose)\b", cls_attr):
                        continue
                    if any(s in line for s in short_labels.get(rel, [])):
                        continue
                    offenders.append(f"{rel}: {line.strip()[:70]}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
