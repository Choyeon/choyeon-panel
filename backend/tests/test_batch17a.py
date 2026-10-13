"""批次⑰A：口径统一——探测失败与判不出，都不许渲染成绿灯。

这一批不改任何功能，只收一件事：**同一个事实，两处必须在说同一句话**。

- `CollectHonestyTests`：`apt list --upgradable` 因为 dpkg 锁、超时（124）、命令不存在（127）
  而失败时，旧代码只往 notes 里塞一句就照常 `security_known = True`，于是 `pending_total=0`
  一路走到自检项变成 pass「没有待装安全更新」。这台机器最需要的信号（还没打的安全更新）
  被探测失败伪装成干净，是这一批的主案。现在读不到就是 None + `probe_error`，doctor 只能
  出 warn，且 fix 给的是刷新索引那条命令。
- `_probe_error` 的判据只看一件事：非零退出码 **且** 一行都没解析出来。非零但有行，数量
  就是真的（此时不可能假绿），退出码留在 notes 里说明；`dnf -q check-update` 的 100 是约定。
- `RebootVerdictTests`：`/run/reboot-required` 由 update-notifier-common 在 apt 事件里写，
  服务器镜像默认不装它（生产机上 `ls` 直接 No such file）。旧代码 `REBOOT_FLAG.exists()`
  因此恒为 False：补丁页签说「不用重启」，同一屏的 `pending` 自检项却说「装了新内核，进程
  还踩着旧映射」。现在两处共用 `pending_service.kernel_reboot_verdict()`，判不出返回 None。
- `GuestTimeTests`：`guest`/`guest_nice` 内核已计在 `user`/`nice` 里，整行求和加了两遍，
  分母偏大 → 占用率偏低。仪表盘与 `choyeonctl load` 在同一台机器上给过两个 CPU%，就是
  这里和 load_service 各算各的。生产机当前 guest=0，所以这是**潜伏**缺陷，不是本机的故障。
- `LoadWindowTests`：ctxt/forks 的增量是 `stat1→stat2` 之间攒的，中间夹着配额扫描和整轮
  /proc 扫描；旧分母写的是标称 1 秒，页面同时写着「窗口 1 秒」，每秒速率被抬高两三倍。
  现在 `window_sec` 就是单调时钟实测跨度；进程表自己的窗口仍是 `procs.window_sec`。
- `ExposureOrderTests`：5353/5355 判成链路本地是因为 avahi 在那儿监听；面板既然把一个配了
  域名的应用登记在这个端口上，那就是真的源站暴露，白名单不许把它盖成红灯涂绿。
- `StaleIndexTests` / `PatchTabHonestyTests`：`apt list` 读的是 /var/lib/apt/lists 里的本地索引，
  索引是 30 天前的时候，退出码 0、清单干净、结论早已作废——这一条与探测失败同源。阈值
  `STALE_TRUST_HOURS` 只写在服务层，CLI 引用同一个常量、页签只认 `index_stale`；页签里那句
  `Number(pending_total) || 0` 会把后端新给的 null 重新写成「待装 0 个」，所以渲染口径一起钉住。

夹具里的 apt/dnf 输出、/proc/stat 行、dpkg-query 结果都按真实格式手写，不依赖跑测试的机器。
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch17a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch17a_roots_"))

import unittest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import sys_helper  # noqa: E402

import cli  # noqa: E402
from app.services import exposure_service as ex  # noqa: E402
from app.services import load_service as ls  # noqa: E402
from app.services import pending_service as ps  # noqa: E402
from app.services import system_metrics as sm  # noqa: E402
from app.services import updates_service as us  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SV = ROOT / "web" / "src" / "views" / "Services.vue"

# `apt list --upgradable` 的真实尾行格式（N 行 + 一行 Listing...）。
APT_UPGRADABLE = """Listing...
libsqlite3-0/noble-security 3.45.1-1ubuntu2.3 amd64 [upgradable from: 3.45.1-1ubuntu2.2]
openssl/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]
"""


def _aio(coro):
    import asyncio
    return asyncio.run(coro)


class ProbeErrorTests(unittest.TestCase):
    def test_zero_code_is_never_an_error(self):
        self.assertIsNone(us._probe_error(us.DEBIAN, 0, []))
        self.assertIsNone(us._probe_error(us.DEBIAN, 0, [{"name": "x"}]))

    def test_rhel_and_suse_use_100_for_pending(self):
        # dnf -q check-update / zypper lp 有待装时就是回 100，这不是失败。
        self.assertIsNone(us._probe_error(us.RHEL, 100, [{"name": "x"}]))
        self.assertIsNone(us._probe_error(us.SUSE, 100, [{"name": "x"}]))

    def test_timeout_and_missing_command_are_unreadable(self):
        for code in (124, 127, 1, 2):
            with self.subTest(code=code):
                self.assertIsNotNone(us._probe_error(us.DEBIAN, code, []))

    def test_rows_present_means_the_number_is_real(self):
        # 非零但解析出了行：数量 > 0 时不可能假绿，照报。
        self.assertIsNone(us._probe_error(us.DEBIAN, 1, [{"name": "openssl"}]))


def _collect_updates(code, out, family=us.DEBIAN, reboot=(False, None), age=None):
    """跑一次 `updates_service._collect`，把外部世界全部换成夹具。

    只 mock 到 `run` 这一层：解析器、阈值、doctor 判词都走真实代码，
    否则用例通过也证明不了线上那段不会假绿。
    """
    async def fake_run(cmd, args, **kw):
        return {"code": code, "out": out}

    patches = [
        mock.patch.object(us, "run", fake_run),
        mock.patch.object(us, "family_of", lambda m, a: family),
        mock.patch.object(us, "_apt_cache_age", lambda: age),
        mock.patch.object(us, "_auto_updates", lambda f: _const({"state": "enabled", "unit": "u"})),
        mock.patch.object(us, "_reboot_verdict", lambda f: _const(reboot)),
        mock.patch.object(us.shutil, "which", lambda c: f"/usr/bin/{c}"),
    ]
    for p in patches:
        p.start()
    try:
        return _aio(us._collect())
    finally:
        for p in patches:
            p.stop()


class CollectHonestyTests(unittest.TestCase):
    def test_failed_probe_does_not_claim_a_clean_machine(self):
        d = _collect_updates(124, "timeout after 120s")
        self.assertIsNone(d["pending_total"], "读不到时必须是 None，不是 0")
        self.assertIsNone(d["security_count"])
        self.assertTrue(d["probe_error"])
        item = us.doctor_item(d)
        self.assertNotEqual(item["status"], "pass", "探测失败绝不能出绿灯")
        self.assertIn("不等于", item["detail"])
        self.assertEqual(item["fix"], d["commands"]["refresh_index"])

    def test_missing_apt_is_reported_as_unreadable_too(self):
        d = _collect_updates(127, "")
        self.assertIsNone(d["pending_total"])
        self.assertEqual(us.doctor_item(d)["status"], "warn")

    def test_successful_empty_probe_is_a_real_pass(self):
        d = _collect_updates(0, "Listing...")
        self.assertEqual(d["pending_total"], 0)
        self.assertEqual(d["security_count"], 0)
        self.assertIsNone(d["probe_error"])
        self.assertEqual(us.doctor_item(d)["status"], "pass")

    def test_successful_probe_counts_security(self):
        d = _collect_updates(0, APT_UPGRADABLE)
        self.assertEqual(d["pending_total"], 2)
        self.assertGreaterEqual(d["security_count"], 1)
        self.assertEqual(us.doctor_item(d)["status"], "warn")

    def test_notes_say_the_number_is_unknown(self):
        d = _collect_updates(1, "")
        self.assertTrue(any("不是 0" in n or "读不到" in n for n in d["notes"]))

    def test_refresh_index_exists_for_every_supported_family(self):
        for fam in (us.DEBIAN, us.RHEL, us.ARCH, us.SUSE, us.ALPINE):
            with self.subTest(fam=fam):
                self.assertTrue(us.commands_for(fam)["refresh_index"], f"{fam} 要给得出刷新索引的命令")


class StaleIndexTests(unittest.TestCase):
    """探测成功但索引是 30 天前的：exit 0、清单干净、结论其实早已作废。

    `apt list --upgradable` 读的是 /var/lib/apt/lists 里的本地索引，而面板不代跑
    `apt update`。索引旧于 `STALE_TRUST_HOURS` 时，"没有待装安全更新"这句绿灯必须降级，
    且 pass 的文案里要带上索引年龄——否则页签上那个「已全部装上」说的其实是上个月。
    """

    def test_stale_index_cannot_be_a_pass(self):
        d = _collect_updates(0, "Listing...", age=720.0)   # 30 天
        self.assertEqual(d["pending_total"], 0, "数量本身是真的，问题在时效")
        self.assertTrue(d["index_stale"])
        item = us.doctor_item(d)
        self.assertEqual(item["status"], "warn")
        self.assertIn("本地索引", item["detail"])
        self.assertIn("不能当结论用", item["detail"])
        self.assertEqual(item["fix"], d["commands"]["refresh_index"])

    def test_fresh_index_pass_names_the_age(self):
        d = _collect_updates(0, "Listing...", age=6.0)
        self.assertFalse(d["index_stale"])
        item = us.doctor_item(d)
        self.assertEqual(item["status"], "pass")
        self.assertIn("索引 6 小时前刷新", item["detail"])

    def test_families_without_an_apt_index_are_not_stale(self):
        # arch 根本没有 /var/lib/apt/lists，不能因为读不到年龄就判它过期。
        d = _collect_updates(0, "", family=us.ARCH, age=None)
        self.assertIsNone(d["index_age_hours"])
        self.assertFalse(d["index_stale"])
        self.assertEqual(us.doctor_item(d)["status"], "pass")

    def test_cli_prints_the_stale_index_and_the_refresh_command(self):
        d = _collect_updates(0, "Listing...", age=720.0)
        text = cli._updates_text({**d, "distro": "Debian", "auto_updates": {"state": "enabled", "unit": "u"},
                                  "packages": [], "checked_at": "x"})
        self.assertIn("小时前的本地索引", text)
        self.assertIn("apt update", text)

    def test_the_threshold_is_stated_once(self):
        # 阈值只许写在服务层：CLI 与网页引用同一个常量，不许各自抄一份 168。
        self.assertEqual(us.STALE_TRUST_HOURS, 168.0)
        cli_src = (ROOT / "backend" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("updates_service.STALE_TRUST_HOURS", cli_src)
        self.assertNotIn("> 168", cli_src)
        vue_src = SV.read_text(encoding="utf-8")
        self.assertIn("patches.index_stale", vue_src)
        self.assertNotIn("168", vue_src)


async def _const(v):
    return v


class _Flag:
    """`/run/reboot-required` 的替身：PosixPath.exists 是只读描述符，patch.object 打不进去。"""

    def __init__(self, present):
        self.present = present

    def exists(self):
        return self.present


class RebootVerdictTests(unittest.TestCase):
    def test_debian_follows_the_kernel_package(self):
        with mock.patch.object(ps, "kernel_reboot_verdict", lambda: {"needs_reboot": True, "reason": None}):
            req, why = _aio(us._reboot_verdict(us.DEBIAN))
        self.assertTrue(req)
        with mock.patch.object(ps, "kernel_reboot_verdict", lambda: {"needs_reboot": False, "reason": None}):
            req, _why = _aio(us._reboot_verdict(us.DEBIAN))
        self.assertFalse(req)

    def test_debian_does_not_need_the_update_notifier_marker(self):
        # 生产机上 /run/reboot-required 不存在，但装了新内核 —— 旧代码在这里返回 False。
        newer = {"needs_reboot": True, "reason": None, "newer": [{"pkg": "linux-image-6.12.9", "version": "1"}]}
        with mock.patch.object(us, "REBOOT_FLAG", _Flag(False)), \
             mock.patch.object(ps, "kernel_reboot_verdict", lambda: newer):
            req, _why = _aio(us._reboot_verdict(us.DEBIAN))
        self.assertTrue(req, "两个页签必须同时说「要重启」")

    def test_undecidable_is_none_not_false(self):
        with mock.patch.object(ps, "kernel_reboot_verdict",
                               lambda: {"needs_reboot": None, "reason": "自定义内核，无法比对"}):
            req, why = _aio(us._reboot_verdict(us.DEBIAN))
        self.assertIsNone(req)
        self.assertIn("无法比对", why)

    def test_rhel_uses_its_own_flag(self):
        with mock.patch.object(us, "REBOOT_FLAG", _Flag(True)):
            self.assertTrue(_aio(us._reboot_verdict(us.RHEL))[0])
        with mock.patch.object(us, "REBOOT_FLAG", _Flag(False)):
            self.assertFalse(_aio(us._reboot_verdict(us.RHEL))[0])

    def test_other_families_are_not_judged(self):
        req, _why = _aio(us._reboot_verdict(us.ARCH))
        self.assertIsNone(req)


class KernelVerdictApiTests(unittest.TestCase):
    def test_wrapper_shells_out_to_the_same_state_the_tab_uses(self):
        seen = {}

        def fake_state(btime):
            seen["btime"] = btime
            return {"needs_reboot": True, "reason": None}

        with mock.patch.object(ps, "_kernel_state", fake_state), \
             mock.patch.object(ps, "_boot_epoch", lambda: 1234):
            got = ps.kernel_reboot_verdict()
        self.assertEqual(seen["btime"], 1234)
        self.assertTrue(got["needs_reboot"])

    def test_source_is_shared_not_copied(self):
        # 补丁页签不许再自己算一遍重启判定：它必须调用 pending_service 的那个入口。
        text = (ROOT / "backend" / "app" / "services" / "updates_service.py").read_text(encoding="utf-8")
        self.assertIn("pending_service.kernel_reboot_verdict", text)


class _Times:
    """psutil cpu_times 的替身：只关心 _fields 与逐字段取值。"""

    def __init__(self, **kw):
        self._fields = tuple(kw)
        for k, v in kw.items():
            setattr(self, k, v)


class GuestTimeTests(unittest.TestCase):
    STAT_WITH_GUEST = "cpu  100 10 50 800 20 5 15 30 40 8\n"
    # 前八项之和 = 1030；把 guest(40)/guest_nice(8) 再加一遍会得到 1078。

    def test_sys_helper_excludes_guest_from_total(self):
        got = sys_helper.parse_stat(self.STAT_WITH_GUEST)
        self.assertEqual(got["total"], 1030)
        self.assertEqual(got["idle"], 820)

    def test_sys_helper_short_line_unchanged(self):
        self.assertEqual(sys_helper.parse_stat("cpu  1 2 3 4\n")["total"], 10)

    def test_metrics_cpu_total_skips_guest_fields(self):
        t = _Times(user=100, nice=10, system=50, idle=800, iowait=20,
                   irq=5, softirq=15, steal=30, guest=40, guest_nice=8)
        self.assertEqual(sm._cpu_total(t), 1030)

    def test_metrics_proc_raw_uses_first_eight_fields(self):
        files = {
            "/proc/stat": "cpu  100 10 50 800 20 5 15 30 40 8\ncpu0 50 5 25 400 10 2 8 15 20 4\n",
            "/proc/meminfo": "MemTotal: 1000000 kB\nMemAvailable: 500000 kB\n",
            "/proc/net/dev": "face | bytes\nlo:1 1\neth0: 100 0 0 0 0 0 0 0 200\n",
            "/proc/diskstats": "8 0 sda 1 2 3 4 5 6 7 8 9 10 11\n",
            "/proc/uptime": "10 20\n",
        }
        with mock.patch.object(sm, "_read", lambda p: files.get(p, "")), \
             mock.patch.object(sm.shutil, "disk_usage", lambda p: type("D", (), {"total": 1, "used": 1})()), \
             mock.patch.object(os, "listdir", lambda p: ["1", "2"]):
            got = sm._proc_raw()
        self.assertEqual(got["total"], 1030, "兜底路径也不许把 guest 算两遍")
        self.assertEqual(got["perCore"][0][1], 515)

    def test_psutil_path_goes_through_cpu_total(self):
        src = inspect.getsource(sm._psutil_raw)
        self.assertIn("_cpu_total(", src)
        self.assertNotIn("sum(getattr(ct", src, "别在 psutil 路径上自己求和")


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


class LoadWindowTests(unittest.TestCase):
    """ctxt/forks 的分母必须是真实跨度，进程表自己的窗口另算。"""

    def _collect(self, span=2.5):
        clock = _FakeClock()
        ticks1 = {k: 0 for k in ls.CPU_FIELDS}
        ticks1.update({"user": 100, "idle": 800})
        ticks2 = {k: 0 for k in ls.CPU_FIELDS}
        ticks2.update({"user": 200, "idle": 800})
        states = [{"ok": True, "ticks": ticks1, "ctxt": 1000, "forks": 10,
                   "procs_running": 1, "procs_blocked": 0},
                  {"ok": True, "ticks": ticks2, "ctxt": 3500, "forks": 35,
                   "procs_running": 1, "procs_blocked": 0}]
        calls = {"n": 0}

        def fake_stat_cpu():
            got = states[min(calls["n"], len(states) - 1)]
            calls["n"] += 1
            return got

        def fake_scan_procs(hz, deadline):
            clock.now += span   # 扫描 + 1 秒采样窗，真实跨度由它决定
            return {"ok": True, "reason": None, "scanned": 3, "budget_hit": False,
                    "states": {}, "busy": [], "busy_total": 0,
                    "blocked": [], "blocked_total": 0, "window_sec": ls.SAMPLE_WINDOW_SEC}

        with mock.patch.object(ls, "time", clock), \
             mock.patch.object(ls, "_stat_cpu", fake_stat_cpu), \
             mock.patch.object(ls, "_scan_procs", fake_scan_procs), \
             mock.patch.object(ls, "_quota_scan", lambda u, d: {"ok": True, "first": {}}), \
             mock.patch.object(ls, "_quota_second_pass", lambda a, u: []), \
             mock.patch.object(ls, "_psi", lambda: {"ok": True, "cpu": {}, "io": {}}), \
             mock.patch.object(ls, "_loadavg", lambda: {"ok": True, "reason": None}), \
             mock.patch.object(ls, "_findings", lambda *a: []):
            return ls._collect()

    def test_window_sec_is_the_measured_span(self):
        d = self._collect(span=2.5)
        self.assertAlmostEqual(d["window_sec"], 2.5, places=1)

    def test_rates_use_that_same_span(self):
        d = self._collect(span=2.5)
        # 2500 次切换摊在 2.5 秒上是 1000/秒；摊在标称 1 秒上会说成 2500/秒。
        self.assertEqual(d["cpu"]["ctxt_per_sec"], 1000)
        self.assertEqual(d["cpu"]["forks_per_sec"], 10)

    def test_proc_table_keeps_its_own_window(self):
        d = self._collect(span=2.5)
        self.assertEqual(d["procs"]["window_sec"], ls.SAMPLE_WINDOW_SEC)


class ExposureOrderTests(unittest.TestCase):
    ROW = {"port": 5353, "external": True, "addrs": ["0.0.0.0"], "processes": ["avahi-daemon"]}

    def _v(self, app_by_port, row=None):
        return ex._verdict(row or self.ROW, True, "", {}, app_by_port)

    def test_panel_app_with_domain_beats_the_link_local_allowlist(self):
        verdict, reason, _fix = self._v({5353: {"name": "cast", "domain": "cast.example.com"}})
        self.assertEqual(verdict, ex.EXPOSED)
        self.assertIn("直连源站", reason)

    def test_link_local_allowlist_still_covers_the_system_daemon(self):
        verdict, _reason, _fix = self._v({})
        self.assertEqual(verdict, ex.INTERNAL)

    def test_app_without_domain_does_not_become_exposed(self):
        verdict, _reason, _fix = self._v({5353: {"name": "cast", "domain": ""}})
        self.assertEqual(verdict, ex.INTERNAL)

    def test_allowlist_member_is_the_registered_port_family(self):
        self.assertIn(5353, ex.LINK_LOCAL_PORTS)


class FrontendWindowWiringTests(unittest.TestCase):
    """页签上那两句说的是进程表，必须跟着 procs 的窗口，不能借用总窗口。"""

    @classmethod
    def setUpClass(cls):
        cls.text = SV.read_text(encoding="utf-8")

    def test_proc_sentences_use_the_proc_window(self):
        self.assertIn("窗口 ${procInfo.window_sec} 秒", self.text)
        self.assertIn("每个数字都是这 {{ procInfo.window_sec }} 秒里的增量", self.text)

    def test_total_span_is_still_shown_once(self):
        self.assertIn("{{ ldData.window_sec }} 秒", self.text)

    def test_no_proc_sentence_borrows_the_top_level_window(self):
        for line in self.text.splitlines():
            if "每个数字都是这" in line or "个任务，窗口" in line:
                self.assertNotIn("ldData.window_sec", line, line.strip()[:60])


class PatchTabHonestyTests(unittest.TestCase):
    """页签也不许把 null 渲染成 0——它和自检项的假绿是同一个缺陷的另一半。

    后端改成 `pending_total=None` 之后，前端那句 `Number(x) || 0` 会把「不知道」重新
    写成「待装 0 个」，绿灯照旧。所以这里钉住渲染口径。
    """

    @classmethod
    def setUpClass(cls):
        cls.text = SV.read_text(encoding="utf-8")

    def test_pending_number_is_not_coerced_to_zero(self):
        self.assertNotIn("Number(patches.value?.pending_total) || 0", self.text)
        self.assertIn("<i>待装</i>{{ pendingText }}", self.text)
        self.assertIn("totalPending.value === null ? '读不到'", self.text)

    def test_probe_failure_is_said_in_the_alert_and_the_empty_table(self):
        self.assertIn("这一轮读不到待装清单", self.text)
        self.assertIn("空表不代表没有更新", self.text)

    def test_the_tab_dot_lights_when_the_page_cannot_certify_clean(self):
        self.assertIn("!!patches.value?.probe_error", self.text)
        self.assertIn("!!patches.value?.index_stale", self.text)

    def test_undecidable_reboot_gets_its_own_chip(self):
        self.assertIn("patches.reboot_required === null", self.text)

    def test_refresh_command_is_offered_from_the_backend_field(self):
        self.assertIn("patches.commands?.refresh_index", self.text)


class CliUpdatesTextTests(unittest.TestCase):
    def _text(self, d):
        return cli._updates_text({**d, "distro": "Debian", "family": "debian",
                                  "auto_updates": {"state": "enabled", "unit": "u"},
                                  "commands": us.commands_for("debian"),
                                  "packages": [], "notes": [], "checked_at": "x"})

    def test_unreadable_probe_never_prints_zero_pending(self):
        t = self._text({"probe_error": "退出码 124", "pending_total": None,
                        "security_count": None, "reboot_required": False})
        self.assertIn("读不到", t)
        self.assertNotIn("待装 0 个", t)

    def test_real_zero_still_prints_zero(self):
        t = self._text({"probe_error": None, "pending_total": 0,
                        "security_count": 0, "reboot_required": False})
        self.assertIn("待装 0 个", t)

    def test_undecidable_reboot_says_so_on_debian(self):
        t = self._text({"probe_error": None, "pending_total": 0, "security_count": 0,
                        "reboot_required": None, "reboot_reason": "自定义内核，无法比对"})
        self.assertIn("判不出", t)
        self.assertNotIn("上次更新要求重启", t)

    def test_reboot_required_still_prints(self):
        t = self._text({"probe_error": None, "pending_total": 3, "security_count": 1,
                        "reboot_required": True})
        self.assertIn("上次更新要求重启才生效", t)


if __name__ == "__main__":
    unittest.main()
