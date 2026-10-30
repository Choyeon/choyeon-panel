"""批次⑨：待装补丁探测（updates_service）+ 自检项 + `choyeonctl updates`。

锁定的都是"报错方向"上的语义，因为这一层的价值全在于能不能信它给的数字：
- 判不出安全更新子集必须报 None，不能报 0：把"不知道"说成"没有"，管理员就不再去看
  第二处，而这一行恰好是唯一会提醒他去装补丁的地方；
- `apt list` 读的是本地索引，面板不代跑 `apt update`（写索引、抢 dpkg 锁），所以索引
  过旧必须写进 notes，而不是给一个看起来很干净的结论；
- 只读：这一层不允许出现任何会改系统状态的调用；
- CLI 退出码按 AGENTS.md 分档，发行版判不出来是 3（前置条件不满足），不是 1。
"""

import asyncio
import contextlib
import io
import os
import tempfile
import types
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch9a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch9a_roots_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app.services import updates_service as up  # noqa: E402

# 线上真表形态：suite 可能是逗号并列的多仓库，多架构包行尾还会并列多个旧版本。
APT_OUT = """Listing...
libssl3/stable-security 3.0.18-1~deb13u2 amd64 [upgradable from: 3.0.18-1~deb13u1]
curl/stable 8.11.0-1 amd64 [upgradable from: 8.10.1-1]
libc6/stable,stable-security 2.41-12 amd64 [upgradable from: 2.41-11]
"""

UBUNTU_OUT = """Listing...
openssl/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]
"""

DNF_OUT = """Last metadata expiration check: 1:00:00 ago on Mon.

kernel.x86_64           0:5.14.0-503.14.1.el9_5     baseos
vim-minimal.x86_64      2:9.0.2081-3.el9_5          appstream
shim-x64.x86_64         15.8-2.el9                  security-extra
"""

UPDATEINFO_OUT = """Update Information:
RHSA-2025:1234 Moderate: kernel security update
  RHSA-2025:1234   kernel-5.14.0-503.14.1.el9_5
FEDORA-2025-abcdef   firefox
"""

PACMAN_OUT = """linux 6.12.9.arch1-1 -> 6.12.10.arch1-1
pacman 7.0.0-4 -> 7.0.0-6
no arrow line here
"""

ZYPPER_OUT = """SUSE-SU-2025:0001 | important | security | openSUSE-Tumbleweed
openSUSE-SU-2025:0002 | moderate | recommended | repo
name | category | severity | status
---- | -------- | -------- | ----
"""

APK_OUT = """alpine-baselayout 3.6.5-r0 < 3.7.0-r0
zlib 1.3.1-r1  < 1.3.2-r0
Installed versions:
"""


def _fake_run(table):
    calls = []

    async def run(cmd, args, cwd=None, timeout=120, env=None):
        key = " ".join([cmd, *[str(x) for x in args]])
        calls.append(key)
        for prefix, (code, out) in table.items():
            if key.startswith(prefix):
                return {"code": code, "out": out}
        return {"code": 127, "out": f"命令不存在: {cmd}"}

    run.calls = calls
    return run


class _Path:
    """够用的 Path 替身：exists / read_text / glob。

    批次75 起生产代码不再问 `exists()`，改用 `os.stat` 做三态门禁（在 / 确认不在 / 读不动），
    而 `os.stat` 只认真路径。所以替身把 exists_flag 落到临时目录里一个真文件上：
    True 就建出来（stat 得到"在"），False 就不建（stat 得到 FileNotFoundError = 确认不在），
    两种情况下 `read_text` 都仍回本次用例钉住的那份文本。
    """

    _dir = tempfile.mkdtemp(prefix="cp_b9a_paths_")

    def __init__(self, text="", exists_flag=True, files=None):
        self.text = text
        self.exists_flag = exists_flag
        self.files = files or []
        self._real = os.path.join(self._dir, f"p{id(self)}")
        if exists_flag:
            with open(self._real, "w", encoding="utf-8") as fh:
                fh.write(text)

    def __fspath__(self):
        return self._real

    def exists(self):
        return self.exists_flag

    def read_text(self, encoding=None, errors=None):
        return self.text

    def glob(self, pattern):
        return list(self.files)


def _os_release(id_, id_like="", pretty="Test"):
    lines = [f'ID={id_}', f'PRETTY_NAME={pretty}']
    if id_like:
        lines.append(f'ID_LIKE="{id_like}"')
    return "\n".join(lines) + "\n"


class ParseTests(unittest.TestCase):
    def test_apt_security_comes_from_the_suite(self):
        pkgs = up.parse_apt_list(APT_OUT)
        self.assertEqual([p["name"] for p in pkgs], ["libssl3", "curl", "libc6"])
        self.assertEqual([p["security"] for p in pkgs], [True, False, True])
        self.assertEqual(pkgs[0]["current"], "3.0.18-1~deb13u1")
        self.assertEqual(pkgs[0]["candidate"], "3.0.18-1~deb13u2")

    def test_apt_handles_ubuntu_parallel_suites(self):
        pkgs = up.parse_apt_list(UBUNTU_OUT)
        self.assertEqual(len(pkgs), 1)
        self.assertTrue(pkgs[0]["security"], "noble-updates,noble-security 里有 security 就算安全更新")
        self.assertEqual(pkgs[0]["suite"], "noble-updates,noble-security")

    def test_apt_ignores_the_header_line(self):
        self.assertEqual(up.parse_apt_list("Listing...\n"), [])
        self.assertEqual(up.parse_apt_list(""), [])

    def test_dnf_three_column_rows_only(self):
        pkgs = up.parse_dnf_check_update(DNF_OUT)
        self.assertEqual([p["name"] for p in pkgs], ["kernel.x86_64", "vim-minimal.x86_64", "shim-x64.x86_64"])
        self.assertEqual([p["security"] for p in pkgs], [False, False, True])

    def test_dnf_advisories_are_deduped_and_format_tolerant(self):
        adv = up.parse_dnf_updateinfo(UPDATEINFO_OUT)
        self.assertEqual([a["id"] for a in adv], ["RHSA-2025:1234", "FEDORA-2025-abcdef"])
        self.assertEqual(up.parse_dnf_updateinfo("no advisory here\n"), [])

    def test_pacman_and_apk_parsers(self):
        pk = up.parse_pacman_qu(PACMAN_OUT)
        self.assertEqual([p["name"] for p in pk], ["linux", "pacman"])
        self.assertEqual(pk[0]["candidate"], "6.12.10.arch1-1")
        ap = up.parse_apk_version(APK_OUT)
        self.assertEqual([p["name"] for p in ap], ["alpine-baselayout", "zlib"])
        self.assertEqual(ap[1]["candidate"], "1.3.2-r0")

    def test_zypper_matches_security_in_any_column(self):
        pk = up.parse_zypper_lp(ZYPPER_OUT)
        self.assertTrue(pk[0]["security"])
        self.assertFalse(pk[1]["security"])
        self.assertNotEqual(pk[-1]["name"], "name")

    def test_family_from_id_like(self):
        self.assertEqual(up.family_of(_meta(_os_release("ubuntu", "debian")), set()), up.DEBIAN)
        self.assertEqual(up.family_of(_meta(_os_release("rocky", "rhel fedora")), set()), up.RHEL)
        self.assertEqual(up.family_of(_meta(_os_release("debian")), {"apt"}), up.DEBIAN)

    def test_family_falls_back_to_package_manager_then_unknown(self):
        # ID 认不出但机器上有 dnf：按包管理器判，别报"不支持"。
        self.assertEqual(up.family_of(_meta(_os_release("vagueos")), {"dnf"}), up.RHEL)
        self.assertEqual(up.family_of(_meta(_os_release("vagueos")), set()), up.UNKNOWN)

    def test_commands_per_family(self):
        self.assertIn("unattended-upgrade", up.commands_for(up.DEBIAN)["apply_security"])
        self.assertEqual(up.commands_for(up.RHEL)["apply_security"], "dnf upgrade --security -y")
        # refresh_index 是探测失败/索引过旧时唯一能给管理员的那条命令，未知族回空串而不是 None。
        self.assertEqual(up.commands_for(up.UNKNOWN), {"apply_all": "", "apply_security": "", "refresh_index": ""})


def _meta(text):
    return up.parse_os_release(text)


class DetectTests(unittest.TestCase):
    def setUp(self):
        up._cache["data"] = None
        up._cache["at"] = 0.0

    def _patch(self, *, os_text, table, which=("apt",), reboot=False, kernel=(False, None)):
        return (
            mock.patch.object(up, "OS_RELEASE", _Path(os_text)),
            mock.patch.object(up, "REBOOT_FLAG", _Path("", reboot)),
            mock.patch.object(up, "shutil", types.SimpleNamespace(which=lambda c: c in which)),
            mock.patch.object(up, "run", _fake_run(table)),
            mock.patch.object(up, "_apt_cache_age", lambda: None),
            # ⑰A 起 Debian 的重启判定改走 pending_service 的内核包比对。不把它钉住，用例
            # 断言的就是"跑测试这台机器装了什么内核"，而不是被测逻辑。
            mock.patch.object(up.pending_service, "kernel_reboot_verdict",
                              lambda: {"needs_reboot": kernel[0], "reason": kernel[1]}),
        )

    def _detect(self, os_text, table, **kw):
        # 一个测试里连续两次 _detect 时必须回源：否则第二次拿到的是第一次的缓存，
        # 断言的其实是上一行的发行版（线上第一次跑就是这么骗过 reboot_required 的）。
        up._cache["data"] = None
        up._cache["at"] = 0.0
        patches = self._patch(os_text=os_text, table=table, **kw)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            return asyncio.run(up.detect()), patches[3].new

    def test_debian_counts_pending_and_security(self):
        table = {"apt list": (0, APT_OUT), "systemctl is-enabled unattended": (0, "enabled\n")}
        data, run = self._detect(_os_release("debian", pretty="Debian"), table)
        self.assertEqual(data["family"], up.DEBIAN)
        self.assertEqual(data["pending_total"], 3)
        self.assertEqual(data["security_count"], 2)
        self.assertTrue(data["security_known"])
        self.assertEqual(data["auto_updates"], {"state": "enabled", "unit": "unattended-upgrades.service"})
        self.assertIn("apt list --upgradable", run.calls)

    def test_detect_is_read_only(self):
        """这一层不许出现任何写操作：apt update / upgrade / install 都不该被调用。"""
        table = {"apt list": (0, APT_OUT)}
        _, run = self._detect(_os_release("debian"), table)
        for key in run.calls:
            self.assertNotIn(key.split()[1:3], [["update"], ["upgrade"], ["install"], ["-y"]], key)
            self.assertFalse(key.startswith("apt update"), key)

    def test_result_is_cached_until_refresh(self):
        table = {"apt list": (0, APT_OUT)}
        patches = self._patch(os_text=_os_release("debian"), table=table)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            first = asyncio.run(up.detect())
            second = asyncio.run(up.detect())
            third = asyncio.run(up.detect(refresh=True))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"], "30 分钟内的第二次调用必须命中缓存")
        self.assertFalse(third["cached"])
        self.assertEqual(table and len([1 for k in table]), 1)

    def test_stale_index_is_reported_not_hidden(self):
        table = {"apt list": (0, APT_OUT)}
        patches = self._patch(os_text=_os_release("debian"), table=table)
        with patches[0], patches[1], patches[2], patches[3], patches[5], \
             mock.patch.object(up, "_apt_cache_age", lambda: 72.0):
            data = asyncio.run(up.detect())
        self.assertTrue(any("索引" in n for n in data["notes"]), data["notes"])
        self.assertEqual(data["index_age_hours"], 72.0)
        self.assertFalse(data["index_stale"], "3 天的索引还当得了结论")

    def test_unknown_security_subset_is_none_not_zero(self):
        """RHEL 两边都给不出安全子集时报 None：报 0 等于劝管理员别管这行。"""
        table = {
            "dnf -q check-update": (100, "kernel.x86_64  0:5.14.0-1.el9  baseos\n"),
            "dnf -q updateinfo": (0, "Loading mirror speeds\n"),
        }
        data, _ = self._detect(_os_release("rocky", "rhel fedora"), table, which=("dnf",))
        self.assertEqual(data["family"], up.RHEL)
        self.assertEqual(data["pending_total"], 1)
        self.assertIsNone(data["security_count"])
        self.assertFalse(data["security_known"])
        self.assertTrue(any("安全更新子集" in n for n in data["notes"]))

    def test_rhel_prefers_advisory_count(self):
        table = {
            "dnf -q check-update": (100, "kernel.x86_64  0:5.14.0-1.el9  baseos\n"),
            "dnf -q updateinfo": (0, UPDATEINFO_OUT),
        }
        data, _ = self._detect(_os_release("rhel"), table, which=("dnf",))
        self.assertEqual(data["security_count"], 2)
        self.assertTrue(data["security_known"])

    def test_unsupported_distro_reports_no_numbers(self):
        data, run = self._detect(_os_release("plan9"), {}, which=())
        self.assertEqual(data["family"], up.UNKNOWN)
        self.assertFalse(data["supported"])
        self.assertEqual(run.calls[:1], [], "判不出发行版就不该去猜包管理器")

    def test_reboot_verdict_source_per_family(self):
        """⑰A：Debian 跟内核包走，RHEL 跟它自己的标记走，其它族不判。

        旧代码在 Debian 上只读 /run/reboot-required，而那个标记由 update-notifier-common
        在 apt 事件里写、服务器镜像默认不装它——补丁页签因此恒报「不用重启」，同一屏的
        `pending` 自检项却说「装了新内核，进程还踩着旧映射」。现在两处共用一个来源。
        """
        data, _ = self._detect(_os_release("debian"), {"apt list": (0, APT_OUT)},
                               reboot=False, kernel=(True, None))
        self.assertTrue(data["reboot_required"], "标记缺席也要跟着内核包说「要重启」")
        dnf_table = {"dnf -q check-update": (100, "kernel.x86_64  0:5.14.0-1.el9  baseos\n"),
                     "dnf -q updateinfo": (0, UPDATEINFO_OUT)}
        data2, _ = self._detect(_os_release("rocky", "rhel fedora"), dnf_table, which=("dnf",), reboot=True)
        self.assertTrue(data2["reboot_required"], "RHEL 仍按它自己的标记判")
        table3 = {"pacman -Qu": (0, PACMAN_OUT)}
        data3, _ = self._detect(_os_release("arch", pretty="Arch"), table3, which=("pacman",))
        self.assertIsNone(data3["reboot_required"], "Arch 不在这套标记的适用面上，不猜")

    def test_families_without_a_known_unit_say_unknown(self):
        """Arch/Alpine 上根本没有 unattended-upgrades，报"未开启"是假警报。

        判不出就等于判不出：状态给 unknown，一个 systemctl 都不许探。
        """
        table = {"pacman -Qu": (0, PACMAN_OUT)}
        data, run = self._detect(_os_release("arch", pretty="Arch"), table, which=("pacman",))
        self.assertEqual(data["auto_updates"], {"state": "unknown", "unit": None})
        self.assertEqual([c for c in run.calls if c.startswith("systemctl")], [])

    def test_unsupported_family_probes_nothing(self):
        data, run = self._detect(_os_release("plan9"), {}, which=())
        self.assertEqual(run.calls, [])
        self.assertEqual(data["auto_updates"]["state"], "unknown")


class DoctorItemTests(unittest.TestCase):
    def _data(self, **kw):
        base = {
            "supported": True, "family": up.DEBIAN, "pending_total": 0, "security_count": 0,
            "security_known": True, "reboot_required": False,
            "auto_updates": {"state": "enabled", "unit": "unattended-upgrades.service"},
            "commands": {"apply_all": "apt update && apt upgrade -y", "apply_security": "unattended-upgrade -d"},
        }
        base.update(kw)
        return base

    def test_clean_is_pass(self):
        item = up.doctor_item(self._data())
        self.assertEqual(item["status"], "pass")
        self.assertIn("没有待装安全更新", item["detail"])

    def test_security_updates_are_warn_with_the_command(self):
        item = up.doctor_item(self._data(security_count=3, pending_total=9))
        self.assertEqual(item["status"], "warn")
        self.assertIn("3 个待装安全更新", item["detail"])
        self.assertEqual(item["fix"], "unattended-upgrade -d")

    def test_feature_only_updates_stay_pass(self):
        item = up.doctor_item(self._data(security_count=0, pending_total=4))
        self.assertEqual(item["status"], "pass")
        self.assertIn("另有 4 个功能更新", item["detail"])

    def test_unknown_subset_with_pending_is_warn(self):
        item = up.doctor_item(self._data(security_count=None, security_known=False, pending_total=2))
        self.assertEqual(item["status"], "warn")
        self.assertIn("判不出安全更新子集", item["detail"])

    def test_reboot_requirement_surfaces(self):
        item = up.doctor_item(self._data(reboot_required=True))
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["fix"], "systemctl reboot")

    def test_auto_updates_off_is_said_out_loud(self):
        item = up.doctor_item(self._data(auto_updates={"state": "disabled", "unit": None}))
        self.assertIn("自动安全更新未开启", item["detail"])

    def test_unknown_auto_state_is_not_claimed_off(self):
        item = up.doctor_item(self._data(auto_updates={"state": "unknown", "unit": None}))
        self.assertNotIn("未开启", item["detail"])
        self.assertEqual(item["status"], "pass")

    def test_unsupported_is_warn_not_pass(self):
        item = up.doctor_item(self._data(supported=False))
        self.assertEqual(item["status"], "warn")


class RegistrationTests(unittest.TestCase):
    def test_updates_is_a_doctor_item(self):
        from app.routers import meta
        ids = [c[0] for c in meta._CHECKS]
        self.assertIn("updates", ids)
        self.assertEqual(len(ids), len(set(ids)), "自检项 id 不能重复")

    def test_agents_doc_claims_the_real_item_count(self):
        """AGENTS.md 写的项数就是自动化判断 fail==0 时的预期规模，写歪了会误导排障。"""
        from app.routers import meta
        with open(os.path.join(cli.ROOT, "AGENTS.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f"当前 {len(meta._CHECKS)} 项", text)


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


def _run(coro):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            data = asyncio.run(coro)
        except SystemExit as e:
            data, code = None, e.code or 0
    return data, out.getvalue(), err.getvalue(), code


DEBIAN_DATA = {
    "ok": True, "cached": False, "family": "debian", "distro": "Debian 13", "supported": True,
    "pending_total": 3, "security_count": 2, "security_known": True,
    "packages": [
        {"name": "libssl3", "suite": "stable-security", "candidate": "b", "current": "a", "security": True},
        {"name": "curl", "suite": "stable", "candidate": "y", "current": "x", "security": False},
        {"name": "libc6", "suite": "stable-security", "candidate": "2", "current": "1", "security": True},
    ],
    "advisories": [], "reboot_required": False,
    "auto_updates": {"state": "disabled", "unit": None},
    "checked_at": "2026-10-12T12:00:00+00:00",
    "commands": {"apply_all": "apt update && apt upgrade -y", "apply_security": "unattended-upgrade -d"},
    "notes": [],
}


class CliUpdatesTests(unittest.TestCase):
    def setUp(self):
        cli.Out.json_mode = False

    def tearDown(self):
        cli.Out.json_mode = False

    def test_updates_takes_only_refresh(self):
        action_strings = set()
        for action in cli.build_parser()._subparsers._group_actions[0].choices["updates"]._actions:
            if action.option_strings and action.option_strings[0] != "-h":
                action_strings.add(action.option_strings[0])
        # --json 是所有子命令共有的父解析器选项，不算 updates 自己的参数。
        self.assertEqual(action_strings, {"--refresh", "--json"})

    def test_detect_gets_the_refresh_flag(self):
        with mock.patch.object(up, "detect", mock.AsyncMock(return_value=DEBIAN_DATA)) as d:
            _run(cli.cmd_updates(_args("updates")))
            self.assertFalse(d.await_args.kwargs["refresh"])
            _run(cli.cmd_updates(_args("updates", "--refresh")))
            self.assertTrue(d.await_args.kwargs["refresh"])

    def test_text_output_names_the_security_packages_and_the_command(self):
        with mock.patch.object(up, "detect", mock.AsyncMock(return_value=DEBIAN_DATA)):
            _data, out, _err, code = _run(cli.cmd_updates(_args("updates")))
        self.assertEqual(code, 0)
        self.assertIn("待装 3 个，其中安全更新 2 个", out)
        self.assertIn("libssl3", out)
        self.assertIn("[security]", out)
        self.assertIn("只装安全更新：unattended-upgrade -d", out)
        self.assertIn("自动安全更新未开启", out)

    def test_json_mode_prints_the_payload(self):
        cli.Out.json_mode = True
        with mock.patch.object(up, "detect", mock.AsyncMock(return_value=DEBIAN_DATA)):
            _data, out, _err, code = _run(cli.cmd_updates(_args("updates", "--json")))
        self.assertEqual(code, 0)
        self.assertIn('"security_count": 2', out)

    def test_unsupported_distro_is_exit_precondition(self):
        payload = dict(DEBIAN_DATA, supported=False, family="unknown", pending_total=0,
                       security_count=None, packages=[], notes=["未识别"])
        with mock.patch.object(up, "detect", mock.AsyncMock(return_value=payload)):
            _data, _out, err, code = _run(cli.cmd_updates(_args("updates")))
        self.assertEqual(code, 3, "判不出发行版属于前置条件不满足，不是执行失败")
        self.assertIn("未识别", err)

    def test_schema_advertises_updates(self):
        text = io.StringIO()
        with mock.patch.object(up, "detect", mock.AsyncMock(return_value=DEBIAN_DATA)):
            with contextlib.redirect_stdout(text):
                asyncio.run(cli.cmd_schema(_args("schema")))
        self.assertIn('"updates"', text.getvalue())


if __name__ == "__main__":
    unittest.main()
