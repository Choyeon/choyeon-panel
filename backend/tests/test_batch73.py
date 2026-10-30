"""批次73：日志「在清单里」不等于「读得动」——两条 GET 不再变 500，也不再画空表。

现场（生产机实测复现）：面板服务降权成非 root 后，nginx 的 `access.log`（0640 root:adm）
仍然出现在来源清单里——清单只要求目录可 traversal 且是普通文件——于是
`GET /api/system/nginx-log` 与 `GET /api/system/nginx-stats` 各自踩在一处裸读上
（`os.path.getsize` + `open()` / `read_tail()`），一个 PermissionError 出去就是 500。
更糟的是「如果只把异常吞掉」：尾读回一张空表、统计回一张全零表，页面念出来分别是
"这段日志没有命中行"和"这个窗口没人访问"，把权限问题说成了内容问题。

修法分三层，同一句判词只有 `web_logs.readable_hint()` 一个出处：
- 服务层给第三种事实：`tail_file` 带 `readable: False`，`collect` 回一份键完全一致的零值表
  加 `readError`（缺键的前端就是 undefined 渲染，所以键一个都不能少）；
- 路由层把 `readError` 原样带出去，并把 `truncated` 判成 True——"读不动"不是"没裁掉所以完整"；
- CLI 的 `webstats` 走前置条件失败（退出码 3），而不是把全零表打到终端。

真实权限用例沿用批次72 的形状：非 root 机器上 `chmod 000` 当场实测；root（生产机）有
DAC_OVERRIDE 拦不住，就 fork 降权到 nobody 在同进程里跑同一条读路径。两条都不满足才 skip。
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b73_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import system as sys_router  # noqa: E402
from app.services import access_stats, web_logs  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
LOG_CENTER = (SRC / "web" / "src" / "views" / "LogCenter.vue").read_text(encoding="utf-8")
CLI_PY = (SRC / "backend" / "cli.py").read_text(encoding="utf-8")
WEB_LOGS_PY = (SRC / "backend" / "app" / "services" / "web_logs.py").read_text(encoding="utf-8")
SYSTEM_PY = (SRC / "backend" / "app" / "routers" / "system.py").read_text(encoding="utf-8")
ACCESS_PY = (SRC / "backend" / "app" / "services" / "access_stats.py").read_text(encoding="utf-8")

_DENY = PermissionError(13, "Permission denied")


def _line(ip: str, hh: int, mm: int, code: str = "200", path: str = "/") -> str:
    return (f'{ip} - - [30/Oct/2026:{hh:02d}:{mm:02d}:07 +0800] '
            f'"GET http://demo.cp73{path} HTTP/1.1" {code} 512 "-" "Mozilla/5.0 (batch73)"')


def _req(**qp) -> SimpleNamespace:
    """只带 query_params 与 admin 角色的最小 Request 替身（路由只用这两样）。"""
    return SimpleNamespace(state=SimpleNamespace(cp_role="admin"), query_params=dict(qp))


def _write_log(path: str) -> None:
    Path(path).write_text("\n".join([_line("10.0.0.1", 9, 1), _line("10.0.0.2", 9, 2, "404", "/x")])
                          + "\n", encoding="utf-8")


def _stats_keys() -> set:
    readable = tempfile.mkdtemp(prefix="cp_b73_ok_")
    try:
        p = f"{readable}/access.log"
        _write_log(p)
        return set(access_stats.collect(p, 60))
    finally:
        shutil.rmtree(readable, True)


class ReadableBaselineTests(unittest.TestCase):
    """回归护栏：读得动时四个数的口径一个字都不变。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b73_tail_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.p = f"{self.dir}/access.log"
        _write_log(self.p)

    def test_tail_file_reports_readable(self):
        out = web_logs.tail_file(self.p, 5, "", 1500)
        self.assertTrue(out["readable"])
        self.assertEqual(len(out["items"]), 2)

    def test_collect_on_real_log_has_no_read_error(self):
        got = access_stats.collect(self.p, 60)
        self.assertNotIn("readError", got)
        self.assertEqual(got["parsed"], 2)

    def test_permission_error_specifically_is_the_third_state(self):
        """0640 root:adm 撞非 root 抛的是 PermissionError，不是 FileNotFoundError。"""
        with mock.patch.object(web_logs.os.path, "getsize", side_effect=_DENY):
            out = web_logs.tail_file(self.p, 5, "", 1500)
        self.assertFalse(out["readable"])
        self.assertEqual(out["items"], [])
        self.assertEqual(out["matched"], 0)
        self.assertFalse(out["window_truncated"], "没读进来就不该声称「读到过窗口头」")
        with mock.patch.object(access_stats, "read_tail", side_effect=_DENY):
            got = access_stats.collect(self.p, 60)
        self.assertTrue(got["readError"])
        self.assertEqual(got["bytesRead"], 0)


class UnreadableShapeTests(unittest.TestCase):
    """零值表必须和正常表同键：前端每个键各占一个位置，缺一个就是 undefined。"""

    def test_unreadable_has_exactly_the_same_keys(self):
        want = _stats_keys()
        got = access_stats.collect("/proc/1/nope/access.log", 60)
        self.assertEqual(set(got) - want, {"readError"})
        self.assertEqual(want - set(got), set(), "正常表有的键一个都不能少，前端会渲染出 undefined")
        self.assertEqual(set(got["status"]), {"0xx", "1xx", "2xx", "3xx", "4xx", "5xx"})

    def test_unreadable_is_not_mistakable_for_a_quiet_window(self):
        got = access_stats.collect("/proc/1/nope/access.log", 60)
        self.assertTrue(got["readError"], "零值必须配一句读不到，否则它念起来就是这个窗口没人访问")
        self.assertEqual(got["parsed"], 0)
        self.assertEqual(got["topPaths"], [])
        self.assertEqual(got["windowMinutes"], 60)

    def test_clamps_window_like_the_normal_path(self):
        self.assertEqual(access_stats.collect("/proc/1/nope", 0)["windowMinutes"], 1)
        self.assertEqual(access_stats.collect("/proc/1/nope", 999999)["windowMinutes"],
                         access_stats.SERIES_MAX)

    def test_hint_does_not_leak_the_path(self):
        hint = web_logs.readable_hint()
        self.assertIn("0640", hint)
        self.assertNotIn("cp_b73", hint)
        self.assertNotIn(str(SRC), hint, "这个字符串会进浏览器：不拼绝对路径")


class RouterHonestyTests(unittest.TestCase):
    """两条 GET：读不动回 200 + readError，不回 500，也不回一张看起来完整的表。

    这里用「清单里有、这一秒开不了」的路径（logrotate 改名那一瞬也是这个形状）；
    真权限的 0640 现场由 RealPermissionTests 覆盖。
    """

    def setUp(self):
        self.files = [{"path": "/proc/1/nope/access.log", "label": "access.log",
                       "kind": "access", "site": ""}]

    def _files(self):
        return mock.patch.object(web_logs, "nginx_log_files", lambda: list(self.files))

    def test_tail_route_says_readerror_instead_of_500(self):
        with self._files():
            out = asyncio.run(sys_router.nginx_log(_req(file="0", lines="200")))
        self.assertIsInstance(out, dict)
        self.assertIn("readError", out)
        self.assertTrue(out["readError"])
        self.assertEqual(out["items"], [])
        self.assertTrue(out["truncated"], "读不动是第三种事实，不是「没裁掉所以是完整答案」")

    def test_stats_route_says_readerror_instead_of_500(self):
        with self._files():
            out = asyncio.run(sys_router.nginx_stats(_req(file="0", minutes="60")))
        self.assertIsInstance(out, dict)
        self.assertTrue(out["readError"])
        self.assertEqual(out["parsed"], 0)
        self.assertEqual(out["file"], 0)

    def test_readable_tail_keeps_empty_read_error(self):
        p = tempfile.mkdtemp(prefix="cp_b73_rt_")
        self.addCleanup(shutil.rmtree, p, True)
        log = f"{p}/access.log"
        _write_log(log)
        self.files = [{"path": log, "label": "access.log", "kind": "access", "site": ""}]
        with self._files():
            out = asyncio.run(sys_router.nginx_log(_req(file="0", lines="200")))
        self.assertEqual(out["readError"], "")
        self.assertEqual(out["count"], 2)
        self.assertFalse(out["truncated"])


class RealPermissionTests(unittest.TestCase):
    """真权限现场：000 的日志文件，非 root 读它 —— tail 与 collect 都不许抛。"""

    def test_unreadable_file_is_reported_not_raised(self):
        base = tempfile.mkdtemp(prefix="cp_b73_perm_")
        log = f"{base}/access.log"
        _write_log(log)
        os.chmod(base, 0o755)
        os.chmod(log, 0o000)

        def probe() -> str:
            try:
                t = web_logs.tail_file(log, 5, "", 1500)
                c = access_stats.collect(log, 60)
                return (f"READABLE={t['readable']} ITEMS={len(t['items'])} "
                        f"ERR={bool(c.get('readError'))} PARSED={c['parsed']}")
            except Exception as e:  # noqa: BLE001 — 崩了也要把异常名送回父进程断言
                return "RAISED=" + type(e).__name__

        want = "READABLE=False ITEMS=0 ERR=True PARSED=0"
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            try:
                import pwd
                pw = pwd.getpwnam("nobody")
                uid, gid = pw.pw_uid, pw.pw_gid
            except (ImportError, KeyError):
                shutil.rmtree(base, True)
                self.skipTest("root 但查不到 nobody，复现不了非 root 现场")
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.dup2(w, 1)
                    os.dup2(w, 2)
                    os.close(r)
                    os.setgid(gid)
                    os.setuid(uid)
                    os.write(w, probe().encode("utf-8") + b"\n")
                except BaseException:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                finally:
                    os._exit(9)
            os.close(w)
            with os.fdopen(r, "r", encoding="utf-8", errors="replace") as f:
                out = f.read()
            os.waitpid(pid, 0)
        else:
            out = probe()
        shutil.rmtree(base, True)
        self.assertIn(want, out, f"000 的日志该被说成「读不到」，实测：{out[:600]}")
        self.assertNotIn("RAISED=", out)


class SourceContractTests(unittest.TestCase):
    """同一条判词只有一个出处；服务层真的兜住了，前端与 CLI 各就三位。"""

    def test_service_layer_catches_instead_of_escaping(self):
        tail_fn = WEB_LOGS_PY.split("def tail_file")[1]
        self.assertIn("except OSError:", tail_fn)
        self.assertIn('"readable": False', tail_fn)
        self.assertIn('"readable": True', tail_fn)
        self.assertNotIn("size = os.path.getsize(path)\n    start", tail_fn,
                         "裸读必须回到 try 里，否则非 root 读 0640 日志就是 500")
        self.assertIn("def readable_hint", WEB_LOGS_PY)
        collect_fn = ACCESS_PY.split("def collect(")[1].split("\ndef ")[0]
        self.assertIn("except OSError:", collect_fn)
        self.assertIn("_unreadable(minutes)", collect_fn)

    def test_router_uses_the_shared_hint_and_counts_read_error_as_truncated(self):
        route = SYSTEM_PY.split("async def nginx_log")[1].split("@router")[0]
        self.assertIn('tail.get("readable", True)', route)
        self.assertIn("web_logs.readable_hint()", route)
        self.assertIn('bool(read_error) or tail["dropped"] > 0', route)

    def test_stats_route_does_not_rewrap_the_service_hint(self):
        # 统计的判词来自 access_stats._unreadable()，路由原样带出：两处各写一句就会分叉
        route = SYSTEM_PY.split("async def nginx_stats")[1].split("@router")[0]
        self.assertNotIn("readable_hint()", route)

    def test_webstats_cli_fails_the_premise_instead_of_printing_zeros(self):
        fn = CLI_PY.split("async def cmd_webstats")[1].split("\ndef ")[0]
        self.assertIn('data.get("readError")', fn)
        self.assertIn("EXIT_PRECONDITION", fn)

    def test_tail_tab_separates_unreadable_from_no_match(self):
        self.assertIn("nResult.value?.readError", LOG_CENTER)
        self.assertIn("这个来源读不到", LOG_CENTER)
        self.assertNotIn(':text="nResult ? ', LOG_CENTER,
                         "空表文案不能再用「有没有结果」二选一")

    def test_stats_tab_shows_the_read_error_before_the_zero_table(self):
        self.assertIn("sResult?.readError", LOG_CENTER)
        self.assertIn("readError?: string", LOG_CENTER)
        self.assertIn("readError?: string", LOG_CENTER.split("type SResp")[1])


if __name__ == "__main__":
    unittest.main()
