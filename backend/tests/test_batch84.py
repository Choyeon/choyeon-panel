"""批次84：接口自己切过的清单，必须把真实总数一起交出来。

`MAX_PKGS = 300` / `MAX_ADVISORIES = 60` 是响应体大小的**传输上限**，不是测量结果。可此前
它们切完就不留痕，于是每个读者只能去数被切过的数组：

- `security_count = len(advisories)`：87 条安全通告的机器一律显示「60 个」，而这个 60 恰好
  等于面板自己的上限——管理员以为是数出来的，其实是限出来的。
- `packages` 只剩前 300 条，头行用的却是未切的 `pending_total`：`choyeonctl updates` 同一屏
  里头行写「待装 412 个」、末行写「共 300 个」，而且那句「完整清单用 --json」成了假话——
  --json 里同样只有那 300 条。
- 前端批次83 的「安全通告：… 等 N 条」也数的是切过的数组，上限一到就永远说「等 60 条」，
  表格里看不到的那部分则一个字都不提。

这一批把总数与上限写进响应（`advisory_total` / `list_limits`），网页、CLI、自检全部改读它们；
上限仍只有服务层一份，页面与终端不再抄 300/60 的字面量。
"""

import asyncio
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b84_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app.services import updates_service as us  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
SERVICES_VUE = (SRC / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")
UI_TS = (SRC / "web" / "src" / "ui.ts").read_text(encoding="utf-8")
UPDATES_PY = (SRC / "backend" / "app" / "services" / "updates_service.py").read_text(encoding="utf-8")
CLI_PY = (SRC / "backend" / "cli.py").read_text(encoding="utf-8")


def _aio(coro):
    return asyncio.run(coro)


async def _const(v):
    return v


def _apt_text(n: int) -> str:
    """n 行 apt list（都落在非 security 的 suite 上，安全子集另算）。"""
    return "\n".join(
        f"pkg{i}/stable 1.0-{i} amd64 [upgradable from: 0.9-{i}]" for i in range(n)
    )


def _dnf_text(n: int) -> str:
    return "\n".join(f"pkg{i}.x86_64  1.0-{i}.el8  baseos" for i in range(n))


def _adv_text(n: int) -> str:
    return "\n".join(f"RHSA-2024:{1000 + i} Moderate/ Security: kernel-{i}.el8.x86_64" for i in range(n))


def _collect(family: str, *, apt=None, dnf=None, adv=None, code: int = 0):
    """跑一次真 `_collect`，只把外部世界（子进程、systemd、时间）换成夹具。

    解析器、上限、security 判定与判词都走真实代码，否则用例绿了也证明不了线上那段不假报。
    """
    async def fake_run(cmd, args, **kw):
        if "updateinfo" in args:
            return {"code": 0, "out": adv or ""}
        if "check-update" in args:
            return {"code": code, "out": dnf or ""}
        return {"code": code, "out": apt or ""}

    patches = [
        mock.patch.object(us, "run", fake_run),
        mock.patch.object(us, "family_of", lambda m, a: family),
        mock.patch.object(us, "_apt_cache_age", lambda: None),
        mock.patch.object(us, "_auto_updates", lambda f: _const({"state": "enabled", "unit": "u"})),
        mock.patch.object(us, "_reboot_verdict", lambda f: _const((False, None))),
        mock.patch.object(us.shutil, "which", lambda c: f"/usr/bin/{c}"),
    ]
    for p in patches:
        p.start()
    try:
        return _aio(us._collect())
    finally:
        for p in patches:
            p.stop()


class AdvisoryTotalsTests(unittest.TestCase):
    def test_advisory_count_is_the_real_total_not_the_cap(self):
        d = _collect(us.RHEL, dnf=_dnf_text(5), adv=_adv_text(87), code=100)
        self.assertEqual(d["advisory_total"], 87, "真实通告数必须原样交出去")
        self.assertEqual(len(d["advisories"]), us.MAX_ADVISORIES, "响应体仍然只带前 60 条")
        self.assertEqual(d["list_limits"]["advisories"], us.MAX_ADVISORIES, "上限由接口自己说，别让读者抄")

    def test_security_count_uses_the_uncapped_total(self):
        d = _collect(us.RHEL, dnf=_dnf_text(5), adv=_adv_text(87), code=100)
        self.assertEqual(d["security_count"], 87)
        self.assertNotEqual(d["security_count"], len(d["advisories"]),
                            "又拿切过的数组当安全更新数——上限会伪装成测量结果")

    def test_below_the_cap_reports_no_phantom_truncation(self):
        d = _collect(us.RHEL, dnf=_dnf_text(5), adv=_adv_text(12), code=100)
        self.assertEqual(d["advisory_total"], 12)
        self.assertEqual(len(d["advisories"]), 12)
        self.assertEqual(d["security_count"], 12)

    def test_debian_has_no_advisory_channel_and_says_zero(self):
        d = _collect(us.DEBIAN, apt=_apt_text(3))
        self.assertEqual(d["advisory_total"], 0, "没有这一路就是 0，不是 None/缺失")
        self.assertEqual(d["security_count"], 0)


class PackagesTotalsTests(unittest.TestCase):
    def test_pending_total_survives_the_package_cap(self):
        d = _collect(us.DEBIAN, apt=_apt_text(412))
        self.assertEqual(d["pending_total"], 412, "总数用切之前的长度")
        self.assertEqual(len(d["packages"]), us.MAX_PKGS, "响应体仍受 MAX_PKGS 约束")
        self.assertEqual(d["list_limits"]["packages"], us.MAX_PKGS)

    def test_limits_are_the_service_constants(self):
        d = _collect(us.DEBIAN, apt="")
        self.assertEqual(d["list_limits"], {"packages": us.MAX_PKGS, "advisories": us.MAX_ADVISORIES})

    def test_failed_probe_still_says_unknown(self):
        d = _collect(us.DEBIAN, apt="", code=124)
        self.assertIsNone(d["pending_total"], "读不到时是 None——这一条批次17 就钉住了，不许回退")
        self.assertTrue(d["probe_error"])


class CliTextTests(unittest.TestCase):
    def _text(self, **kw):
        base = {
            "supported": True, "family": us.DEBIAN, "distro": "Debian 13",
            "probe_error": None, "index_age_hours": None, "index_stale": False,
            "auto_updates": {"state": "enabled", "unit": "unattended-upgrades.service"},
            "reboot_required": False, "reboot_reason": None, "notes": [],
            "commands": us.commands_for(us.DEBIAN), "checked_at": "2026-01-01T00:00:00+00:00",
        }
        base.update(kw)
        return cli._updates_text(base)

    def test_capped_list_does_not_send_the_reader_to_json(self):
        d = us.MAX_PKGS
        text = self._text(pending_total=412, security_count=0,
                          packages=[{"name": f"p{i}", "current": "1", "candidate": "2", "security": False}
                                    for i in range(d)],
                          list_limits={"packages": d, "advisories": us.MAX_ADVISORIES})
        self.assertIn("待装 412 个", text)
        self.assertIn("只读出前 300 个", text)
        self.assertNotIn("完整清单用 --json", text, "--json 里同样只有 300 条，这句是假话")
        self.assertNotIn("共 300 个", text, "末行不许再拿被切的数组长度当总数")

    def test_uncapped_tail_uses_the_true_total(self):
        text = self._text(pending_total=900, security_count=0,
                          packages=[{"name": f"p{i}", "current": "1", "candidate": "2", "security": False}
                                    for i in range(300)],
                          list_limits={"packages": 1000, "advisories": us.MAX_ADVISORIES})
        self.assertIn("...共 900 个", text, "以前这里印 len(packages)，等于把数组长度说成待装总数")
        self.assertIn("完整清单用 --json", text)

    def test_old_payload_without_limits_still_prints(self):
        """没有 list_limits 的旧 fixture（自检缓存、测试夹具）不该让 CLI 直接抛。"""
        text = self._text(pending_total=25, security_count=1,
                          packages=[{"name": f"p{i}", "current": "1", "candidate": "2", "security": False}
                                    for i in range(25)])
        self.assertIn("...共 25 个", text)


class FrontendContractTests(unittest.TestCase):
    """前端读的是接口给的真总数，不是自己数数组、也不是抄常量。"""

    def test_advisory_line_passes_the_server_total(self):
        self.assertRegex(
            SERVICES_VUE,
            re.compile(r"cutCounted\(\s*patches\.value\?\.advisories\s*\|\|.*advisory_total", re.S))

    def test_package_cut_note_reads_the_limit_from_the_api(self):
        self.assertIn("list_limits?.packages", SERVICES_VUE)
        self.assertIn("patchListCut", SERVICES_VUE)
        # 页面里出现裸的 300/60 当上限，就是第二份抄本：接口改了它不会跟着改
        self.assertNotIn("pending_total > 300", SERVICES_VUE)
        self.assertNotIn("length > 300", SERVICES_VUE)

    def test_cut_counted_accepts_a_real_total(self):
        self.assertRegex(
            UI_TS,
            r"export function cutCounted<T>\(items: T\[\], max: number, show: \(item: T\) => string, "
            r"realTotal\?: number\)")
        self.assertIn("等 ${total} 条", UI_TS)

    def test_service_keeps_a_single_source_for_the_limits(self):
        # 上限只在服务层写一次；页面/CLI 只读 list_limits
        for blob, name in ((SERVICES_VUE, "Services.vue"), (CLI_PY, "cli.py")):
            self.assertNotIn("MAX_ADVISORIES", blob, f"{name} 不该自己引用服务层常量")


if __name__ == "__main__":
    unittest.main()
