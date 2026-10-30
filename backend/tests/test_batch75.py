"""批次75：三态门禁——「读不动」不许再被自检说成「确认没有」，也不许把页面打成 500。

`Path.exists()` / `is_dir()` 只有两个答案，现实里有三个：在、确认不在、读不动所以确认不了。
它们把 PermissionError 折成 False，而本项目里 False 一路传下去的意思从来是"确认没有"。
这一批修的都是同一条链上的谎：

- `updates_service._reboot_verdict` 的 RHEL 分支写的是 `REBOOT_FLAG.exists()`：/run 被收紧过
  权限（或面板降权运行）的机器上，补丁页签与 `choyeonctl updates` 永远说"不用重启"，
  而 Debian 那一支早在批次17 就改成三态了——同一个事实，两个家族两个口径。
- `meta._check_cert_renewal` 的 `live.is_dir()`：/etc/letsencrypt 读不动时 glob 只回一张空表，
  于是自检 pass「certbot 已装但暂无证书」——"有证书、没人续期、而面板连查都查不了"这条
  最要紧的事在任何界面上都不再出现。
- `meta._check_timer_units` 的 `unit_dir.is_dir()`：读不到 /etc/systemd/system 时 pass
  「本机无 systemd 单元目录，跳过定时器对账」。批次72 给 `env_drift` 的闸门改成三态时
  没管到这一处镜像。
- `pending_service._sshd_state` 裸 `os.listdir("/proc")`：同文件另外两处同样的遍历都带
  guard，只有这里没有——hidepid/降权的机器上 `GET /api/pending` 直接 500，页面从
  "这一节没查成"变成"这一页打不开"。
- 顺带三处同源：`_collect` 里 `/etc/os-release` 的裸 `read_text`（exists 过了不等于读得动，
  抛出去就是 `GET /api/updates` 500），CLI 里 `family == "debian"` 的白名单（RHEL 现在
  也返回 None，硬编码家族等于让终端把这件事整个吞掉，而网页那一页照样显示"判不出"），
  以及 `pending_service` 里 nginx 那一节的 guard 只 `return` 不写原因 +
  `doctor_item` 硬编码的「nginx/sshd/systemd 都没有待重载」——三节里有一节没做成时，
  自检同一条 detail 里前半句宣称全过、后半句承认读不到。

真实权限用例沿用批次72/73 的形状：非 root 机器上 `chmod 000` 当场实测；root（生产机）有
DAC_OVERRIDE 拦不住，就 fork 降权到 nobody 在同进程里跑同一条读路径。两条都不满足才 skip。
"""

import ast
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b75_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app import config, util  # noqa: E402
from app.routers import meta as META  # noqa: E402
from app.services import pending_service as ps  # noqa: E402
from app.services import updates_service as us  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
UTIL_PY = (SRC / "backend" / "app" / "util.py").read_text(encoding="utf-8")
META_PY = (SRC / "backend" / "app" / "routers" / "meta.py").read_text(encoding="utf-8")
UPDATES_PY = (SRC / "backend" / "app" / "services" / "updates_service.py").read_text(encoding="utf-8")
PENDING_PY = (SRC / "backend" / "app" / "services" / "pending_service.py").read_text(encoding="utf-8")
CLI_PY = (SRC / "backend" / "cli.py").read_text(encoding="utf-8")

_DENY = PermissionError(13, "Permission denied")


def _code_lines(text: str) -> str:
    """只剩可执行行：这一批把旧写法留在注释与 docstring 里当证据，守卫不许报说明文字。"""
    tree = ast.parse(text)
    drop: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [line for no, line in enumerate(text.splitlines(), 1)
            if no not in drop and not line.strip().startswith("#")]
    return "\n".join(kept)


def _aio(coro):
    return asyncio.run(coro)


async def _const(v):
    return v


def _unprivileged(call):
    """在非 root 视角跑一次 call()（返回值必须是字符串，用管道送回父进程断言）。"""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        try:
            return call()
        except Exception as e:  # noqa: BLE001 — 崩了也要把异常名交回断言
            return "RAISED=" + type(e).__name__
    try:
        import pwd
        pw = pwd.getpwnam("nobody")
    except (ImportError, KeyError):
        return ""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(w, 1)
            os.dup2(w, 2)
            os.close(r)
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            try:
                res = call()
            except Exception as e:  # noqa: BLE001
                res = "RAISED=" + type(e).__name__
            os.write(w, res.encode("utf-8") + b"\n")
        except BaseException:  # noqa: BLE001 — fork 子进程兜底：失败带 traceback 进管道
            import traceback
            traceback.print_exc()
        finally:
            os._exit(9)
    os.close(w)
    with os.fdopen(r, "r", encoding="utf-8", errors="replace") as f:
        out = f.read().strip()
    os.waitpid(pid, 0)
    return out


class GateTriStateTests(unittest.TestCase):
    """门禁本身：三态各走各的路，且原因里不拼绝对路径。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="cp_b75_gate_")
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_path_state_present_and_absent(self):
        p = Path(self.base, "reboot-required")
        self.assertEqual(util.path_state(p), (False, ""))
        p.write_text("reboot\n", encoding="utf-8")
        self.assertEqual(util.path_state(p), (True, ""))

    def test_path_state_denied_is_none(self):
        with mock.patch.object(util.os, "stat", side_effect=_DENY):
            state, why = util.path_state("/etc/shadow")
        self.assertIsNone(state, "Path.exists() 在这里回 False，而 False 的意思是「确认没有」")
        self.assertEqual(why, "PermissionError")

    def test_dir_state_readable_returns_names(self):
        Path(self.base, "panel-cron-1.timer").write_text("", encoding="utf-8")
        state, names, why = util.dir_state(self.base)
        self.assertTrue(state)
        self.assertEqual(names, ["panel-cron-1.timer"])
        self.assertEqual(why, "")

    def test_dir_state_missing_is_confirmed_absent(self):
        self.assertEqual(util.dir_state(Path(self.base, "nope")), (False, [], ""))

    def test_dir_state_on_a_regular_file_is_absent_not_unknown(self):
        p = Path(self.base, "plain")
        p.write_text("x", encoding="utf-8")
        self.assertEqual(util.dir_state(p), (False, [], ""))

    def test_dir_state_denied_is_none(self):
        with mock.patch.object(util.os, "scandir", side_effect=_DENY):
            state, names, why = util.dir_state("/etc/systemd/system")
        self.assertIsNone(state)
        self.assertEqual(names, [])
        self.assertEqual(why, "PermissionError")

    def test_reason_never_leaks_the_path(self):
        # 这些字符串会进接口响应：只带异常类型，errno 的文本里那句绝对路径不能跟出来
        with mock.patch.object(util.os, "scandir",
                               side_effect=PermissionError(13, "Permission denied: '/etc/letsencrypt'")):
            why = util.dir_state("/etc/letsencrypt")[2]
        self.assertEqual(why, "PermissionError")


class RebootVerdictTriStateTests(unittest.TestCase):
    """RHEL 那一支从此和 Debian 一样是三态：要重启 / 确认不用 / 判不出。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="cp_b75_run_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.flag = Path(self.base, "reboot-required")

    def test_absent_flag_is_a_real_no(self):
        with mock.patch.object(us, "REBOOT_FLAG", self.flag):
            self.assertEqual(_aio(us._reboot_verdict(us.RHEL)), (False, None))

    def test_present_flag_is_a_real_yes(self):
        self.flag.write_text("reboot\n", encoding="utf-8")
        with mock.patch.object(us, "REBOOT_FLAG", self.flag):
            self.assertEqual(_aio(us._reboot_verdict(us.RHEL)), (True, None))

    def test_unreadable_run_dir_is_none_not_false(self):
        with mock.patch.object(us, "REBOOT_FLAG", self.flag), \
                mock.patch.object(util.os, "stat", side_effect=_DENY):
            req, why = _aio(us._reboot_verdict(us.RHEL))
        self.assertIsNone(req, "读不动时返回 False 就是「确认不用重启」，本批修的正是这个")
        self.assertIn("确认不了", why)

    def test_unsupported_family_still_silent(self):
        req, why = _aio(us._reboot_verdict(us.ARCH))
        self.assertIsNone(req)
        self.assertIn("不由面板负责", why)


class CertRenewalGateTests(unittest.TestCase):
    """`cert-renew` 自检项：读不动 ≠ 没有证书，两个答案必须分开。"""

    def _run(self, gate):
        with mock.patch.object(META.shutil, "which", lambda c: "/usr/bin/certbot"), \
                mock.patch.object(META, "dir_state", lambda p: gate):
            return _aio(META._check_cert_renewal())

    def test_unreadable_dir_is_warn_not_pass(self):
        out = self._run((None, [], "PermissionError"))
        self.assertEqual(out["status"], "warn", "证书在、读不动、面板闭眼报绿灯——本批主案")
        self.assertIn("确认不了", out["detail"])
        self.assertNotIn("暂无证书", out["detail"])

    def test_missing_dir_is_still_a_confirmed_pass(self):
        out = self._run((False, [], ""))
        self.assertEqual(out["status"], "pass")
        self.assertIn("暂无证书", out["detail"])

    def _tls_dir(self, entries):
        base = tempfile.mkdtemp(prefix="cp_b75_tls_")
        self.addCleanup(shutil.rmtree, base, True)
        live = Path(base, "live")
        live.mkdir()
        for name, kind in entries:
            p = Path(live, name)
            if kind == "dir":
                p.mkdir()
            else:
                p.write_text("x", encoding="utf-8")
        return live

    def _run_real(self, live):
        with mock.patch.object(META.shutil, "which", lambda c: "/usr/bin/certbot"), \
                mock.patch.object(config, "TLS_DIR", str(live)), \
                mock.patch.object(META, "dir_state", util.dir_state):
            return _aio(META._check_cert_renewal())

    def test_lineage_without_readable_fullchain_is_warn(self):
        live = self._tls_dir([("demo.conf", "dir"), ("README", "file")])
        out = self._run_real(live)
        self.assertEqual(out["status"], "warn", "看得见 lineage、读不出证书，不是「没有证书」")
        self.assertIn("1 个 lineage", out["detail"])

    def test_only_readme_is_confirmed_no_certs(self):
        # README 不是证书：无证书的机器不许被上面那条误报成 warn
        live = self._tls_dir([("README", "file")])
        out = self._run_real(live)
        self.assertEqual(out["status"], "pass")
        self.assertIn("暂无证书", out["detail"])

    def test_readable_certs_still_go_the_timer_route(self):
        # 回归护栏：读得动、有证书时，后面那套定时器判词一个字都不变
        live = self._tls_dir([("demo.conf", "dir")])
        Path(live, "demo.conf", "fullchain.pem").write_text("x", encoding="utf-8")
        t = {"status": "installed", "checked": "systemd-timer", "detail": "每 12 小时"}
        with mock.patch.object(META.cert_ops, "timer_status", lambda: _const(t)):
            out = self._run_real(live)
        self.assertEqual(out["status"], "pass")
        self.assertIn("1 张证书有自动续期", out["detail"])


class TimerUnitsGateTests(unittest.TestCase):
    """`timer-units` 自检项：闸门三态，读不动这一档从 pass 变成"没做成"。"""

    def test_unreadable_unit_dir_is_warn_not_pass(self):
        with mock.patch.object(META, "dir_state", lambda p: (None, [], "PermissionError")):
            out = _aio(META._check_timer_units())
        self.assertEqual(out["status"], "warn")
        self.assertIn("没有做成", out["detail"])
        self.assertNotIn("跳过定时器对账", out["detail"])

    def test_missing_unit_dir_is_still_a_pass(self):
        # 真的没有 systemd（容器/非 systemd 机器）：这一档继续 pass，别把无法检查伪装成失败
        with mock.patch.object(META, "dir_state", lambda p: (False, [], "")):
            out = _aio(META._check_timer_units())
        self.assertEqual(out["status"], "pass")
        self.assertIn("本机无 systemd 单元目录", out["detail"])

    def _run_real(self, files, cron_ids, backup_ids):
        base = tempfile.mkdtemp(prefix="cp_b75_units_")
        self.addCleanup(shutil.rmtree, base, True)
        for f in files:
            Path(base, f).write_text("[Timer]\n", encoding="utf-8")

        async def fake_query(sql, params=()):
            return [{"id": i} for i in (cron_ids if "cron_task" in sql else backup_ids)]

        with mock.patch.object(config, "UNIT_DIR", base), \
                mock.patch.object(META, "dir_state", util.dir_state), \
                mock.patch.object(META.aio_db, "query", fake_query):
            return _aio(META._check_timer_units())

    def test_aligned_units_on_a_readable_dir_still_pass(self):
        out = self._run_real(["panel-cron-7.timer"], [7], [])
        self.assertEqual(out["status"], "pass", out["detail"])
        self.assertIn("一一对应", out["detail"])

    def test_orphan_still_warns_when_dir_is_readable(self):
        out = self._run_real(["panel-cron-99.timer"], [], [])
        self.assertEqual(out["status"], "warn")
        self.assertIn("panel-cron-99.timer", out["detail"])

    def test_missing_unit_still_warns_when_dir_is_readable(self):
        out = self._run_real([], [7], [])
        self.assertEqual(out["status"], "warn")
        self.assertIn("panel-cron-7", out["detail"])


class SshdProcScanTests(unittest.TestCase):
    """`_sshd_state` 的 /proc 遍历：读不到就标未确认，不许把整页打成 500。"""

    def test_denied_proc_returns_unconfirmed_not_raise(self):
        with mock.patch.object(ps.os, "listdir", side_effect=_DENY):
            out = ps._sshd_state(1790765667, 250)
        self.assertFalse(out["ok"])
        self.assertIn("读不到 /proc", out["reason"])

    def test_pending_audit_degrades_instead_of_500(self):
        # 整条读路径（含 nginx 与 sshd 两处 /proc 遍历）读不动时仍要回一份能渲染的数据
        with mock.patch.object(ps.os, "listdir", side_effect=_DENY), \
                mock.patch.object(ps, "_boot_epoch", lambda: 1790765667):
            data = ps._collect("choyeon-panel.service")
        for sec in ("nginx", "sshd"):
            self.assertFalse(data["reloads"][sec]["ok"])
            self.assertIn("读不到 /proc", data["reloads"][sec]["reason"], f"{sec} 读不到却不写原因")
        self.assertIn("nginx", data["unreadable"])
        self.assertIn("sshd", data["unreadable"])
        item = ps.doctor_item(data)
        # 这两节没做成，自检就不许宣称它们「没有待重载」——三节的句子是硬编码时就是这样打的架
        self.assertNotIn("nginx/sshd/systemd 都没有待重载", item["detail"])
        self.assertNotIn("sshd 都没有待重载", item["detail"])
        self.assertIn("读不到的节", item["detail"])

    def test_readable_sections_are_named_in_the_pass_sentence(self):
        # 反向护栏：读得动的节照样点名，不因为改成"按实际读到的拼"就整体沉默
        data = {"kernel": {"ok": True, "running": "6.12.111"}, "stale": {"ok": True},
                "findings": [], "counts": {}, "unreadable": [],
                "reloads": {"nginx": {"ok": True}, "sshd": {"ok": True}, "systemd": {"ok": True}}}
        self.assertIn("nginx/sshd/systemd 都没有待重载", ps.doctor_item(data)["detail"])

    def test_readable_proc_still_walks(self):
        # 回归护栏：读得动的机器上这一段照常给出该给的东西，不因为加了 guard 就少说话。
        # 有没有 sshd 是机器事实（CI 上就没有），所以只锁形态：要么查到东西，要么如实给原因。
        out = ps._sshd_state(0, 250)
        if out["ok"]:
            self.assertGreaterEqual(out["files"], 1, "files 是参与比对的文件数，不是路径列表")
            self.assertIsInstance(out["newest_file"], str)
        else:
            self.assertIsInstance(out["reason"], str)
            self.assertTrue(out["reason"], "没查成必须留一句原因，不能空着")


class OsReleaseProbeTests(unittest.TestCase):
    """`/etc/os-release` 读不动时：不 500，且 notes 要说明发行版名为什么是空的。

    PosixPath 的方法是只读描述符（`p.read_text = ...` 直接 AttributeError），所以这里
    换成临时目录里的**真路径**，只 mock `path_state` 这一层门禁。
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="cp_b75_osr_")
        self.addCleanup(shutil.rmtree, self.base, True)

    def _collect(self, target, state=(True, "")):
        async def fake_run(cmd, args, **kw):
            return {"code": 0, "out": "Listing..."}

        patches = [
            mock.patch.object(us, "run", fake_run),
            mock.patch.object(us, "family_of", lambda m, a: us.DEBIAN),
            mock.patch.object(us, "_apt_cache_age", lambda: None),
            mock.patch.object(us, "_auto_updates", lambda f: _const({"state": "enabled", "unit": "u"})),
            mock.patch.object(us, "_reboot_verdict", lambda f: _const((False, None))),
            mock.patch.object(us.shutil, "which", lambda c: f"/usr/bin/{c}"),
            mock.patch.object(us, "OS_RELEASE", target),
            mock.patch.object(us, "path_state", lambda p: state),
        ]
        for p in patches:
            p.start()
        try:
            return _aio(us._collect())
        finally:
            for p in reversed(patches):
                p.stop()

    def test_readable_file_unchanged(self):
        good = Path(self.base, "os-release")
        good.write_text('PRETTY_NAME="Ubuntu 24.04"\nID=ubuntu\n', encoding="utf-8")
        got = self._collect(good)
        self.assertEqual(got["distro"], "Ubuntu 24.04")
        self.assertFalse(any("os-release" in n for n in got["notes"]), "读得动时一个字都不许多说")

    def test_stat_denied_is_a_note_not_a_500(self):
        got = self._collect(Path(self.base, "os-release"), state=(None, "PermissionError"))
        self.assertTrue(got["ok"])
        self.assertEqual(got["distro"], "")
        self.assertTrue(any("读不到" in n and "os-release" in n for n in got["notes"]), got["notes"])

    def test_confirmed_absent_says_nothing_extra(self):
        # 真的没有 /etc/os-release（某些容器）：那是"确认没有"，不是"读不到"，别多嘴
        got = self._collect(Path(self.base, "os-release"), state=(False, ""))
        self.assertFalse(any("os-release" in n for n in got["notes"]), got["notes"])

    def test_stat_ok_but_denied_open_still_speaks(self):
        # stat 过了不等于读得动：这里用同名目录造一个真实的 OSError（IsADirectoryError），
        # 旧写法是裸 read_text，抛出去就是 GET /api/updates 直接 500
        as_dir = Path(self.base, "os-release")
        as_dir.mkdir()
        got = self._collect(as_dir, state=(True, ""))
        self.assertTrue(got["ok"], "读不动不许把整条探测打成 500")
        self.assertTrue(any("os-release" in n for n in got["notes"]), got["notes"])


class CliRebootLineTests(unittest.TestCase):
    """`choyeonctl updates` 那行判词不再按家族白名单吞掉「判不出」。"""

    def _text(self, **over):
        d = {"ok": True, "distro": "Red Hat", "family": "rhel", "supported": True,
             "pending_total": 0, "security_count": 0, "security_known": True,
             "probe_error": None, "index_age_hours": None, "packages": [], "advisories": [],
             "reboot_required": None, "reboot_reason": "确认不了 /run/reboot-required（PermissionError）",
             "auto_updates": {"state": "unknown"}, "notes": [], "commands": {}}
        d.update(over)
        return cli._updates_text(d)

    def test_rhel_undecidable_is_printed(self):
        self.assertIn("要不要重启判不出", self._text())

    def test_debian_undecidable_still_printed(self):
        self.assertIn("要不要重启判不出", self._text(family="debian"))

    def test_confirmed_no_reboot_says_nothing(self):
        self.assertNotIn("重启", self._text(reboot_required=False, reboot_reason=None))

    def test_unsupported_family_not_nagged(self):
        # 「该发行版的重启判定不由面板负责」不是缺陷，别在终端里冒充成一条待办
        self.assertNotIn("判不出", self._text(family="arch", supported=False))


class SourceContractTests(unittest.TestCase):
    def test_gate_defined_once_in_util_and_imported(self):
        self.assertIn("def path_state(", UTIL_PY)
        self.assertIn("def dir_state(", UTIL_PY)
        self.assertIn("from ..util import PROBE_TIMEOUT, path_state, run", UPDATES_PY)
        self.assertIn("from ..util import CTRL_TIMEOUT, dir_state, disk_usage_pct, run", META_PY)

    def test_boolean_gates_are_gone(self):
        # 只查可执行行：这一批把旧写法留在注释与 docstring 里当证据（和批次50A 同一口径），
        # 拿整份源码做子串断言的守卫会永远在报一段说明文字。
        up, mt = _code_lines(UPDATES_PY), _code_lines(META_PY)
        self.assertNotIn("REBOOT_FLAG.exists()", up)
        self.assertNotIn("live.is_dir()", mt)
        self.assertNotIn("unit_dir.is_dir()", mt)
        self.assertNotIn("OS_RELEASE.exists()", up)
        self.assertIn("path_state(OS_RELEASE)", up)
        self.assertIn("osr_unreadable = type(e).__name__", up)
        self.assertIn("path_state(REBOOT_FLAG)", up)
        self.assertIn("dir_state(unit_dir)", mt)
        self.assertIn("dir_state(live)", mt)

    def test_every_proc_walk_is_guarded(self):
        lines = PENDING_PY.splitlines()
        for i, line in enumerate(lines):
            if 'os.listdir("/proc")' in line:
                window = "\n".join(lines[max(0, i - 4):i])
                self.assertIn("try:", window, f"第 {i + 1} 行的 /proc 遍历没有 guard")
        self.assertGreaterEqual(sum('os.listdir("/proc")' in row for row in lines), 3)

    def test_cli_no_longer_hardcodes_the_family(self):
        self.assertNotIn('d.get("family") == "debian" and d.get("reboot_required")', CLI_PY)
        self.assertIn('d.get("supported") and d.get("reboot_required") is None', CLI_PY)

    def test_proc_guards_all_leave_a_reason(self):
        code = _code_lines(PENDING_PY)
        self.assertIn('out["reason"] = "读不到 /proc，nginx', code)
        self.assertIn('out["reason"] = "读不到 /proc，sshd', code)
        # 覆盖三节的结论不再硬编码：句子按"实际读到的节"拼
        self.assertNotIn("nginx/sshd/systemd 都没有待重载", code)
        self.assertIn('detail += f"，{\'/\'.join(read)} 都没有待重载"', code)

    def test_web_null_branch_still_there(self):
        sv = (SRC / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")
        self.assertIn("patches.reboot_required === null", sv)


class RealPermissionTests(unittest.TestCase):
    """门禁那条 None 分支用真权限造现场，不拿 mock 冒充实测。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="cp_b75_real_")
        os.chmod(self.base, 0o755)
        self.addCleanup(shutil.rmtree, self.base, True)

    def _closed(self, name, seed=None):
        """建一个目录、先放好内容、最后才关掉权限（非 root 时后面写不进去）。"""
        d = Path(self.base, name)
        d.mkdir()
        for rel in seed or ():
            p = Path(d, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        os.chmod(str(d), 0o000)
        self.addCleanup(os.chmod, str(d), 0o755)
        return d

    def test_dir_state_on_a_closed_directory(self):
        d = self._closed("units", ["panel-cron-1.timer"])
        out = _unprivileged(lambda: str(util.dir_state(d)))
        self.assertTrue(out, "root 且查不到 nobody，复现不了非 root 现场")
        self.assertNotIn("RAISED", out)
        self.assertIn("None", out, "读不动的目录必须回 None，实测：" + out[:200])
        self.assertIn("PermissionError", out)

    def test_path_state_on_a_flag_behind_a_closed_directory(self):
        d = self._closed("run", ["reboot-required"])
        out = _unprivileged(lambda: str(util.path_state(Path(d, "reboot-required"))))
        self.assertNotIn("RAISED", out)
        self.assertIn("None", out, "标记在盘上，但读不动就等于判不出：" + out[:200])

    def test_rhel_reboot_verdict_is_none_under_real_deny(self):
        d = self._closed("run2", ["reboot-required"])
        flag = Path(d, "reboot-required")

        def probe():
            with mock.patch.object(us, "REBOOT_FLAG", flag):
                req, why = _aio(us._reboot_verdict(us.RHEL))
            return f"REQ={req} WHY={why}"

        out = _unprivileged(probe)
        self.assertIn("REQ=None", out, "盘上有标记而面板读不动 → 必须判不出，不是「要重启」也不是「不用」：" + out[:200])

    def test_cert_item_is_not_green_when_tls_dir_is_closed(self):
        root = self._closed("letsencrypt", ["live/demo.conf/fullchain.pem", "live/README"])
        live = Path(root, "live")
        with mock.patch.object(META.shutil, "which", lambda c: "/usr/bin/certbot"), \
                mock.patch.object(config, "TLS_DIR", str(live)):
            out = _unprivileged(lambda: str(_aio(META._check_cert_renewal())["status"]))
        self.assertTrue(out)
        self.assertNotIn("RAISED", out)
        self.assertIn("warn", out, "真权限下这一项必须从绿灯变成没查成：" + out[:200])


if __name__ == "__main__":
    unittest.main()
