"""批次94：pending 的 systemd need_reload 名单是载荷预算截过的，总数必须单独成项。

`_systemd_state` 把 NeedDaemonReload=yes 的 unit 收齐后按 MAX_STALE_UNITS 截到 12 条放进
载荷，用来画界面。若判词与 CLI 拿 `len(need_reload)` 当总数念，一台改过几十条 unit 的机器会被
说成"12 个待重载"——这是一个下限冒充精确值。修法是把真实总数放进 `need_reload_total`，判词、
CLI、前端都读它，截断措辞仍走同一份 `name_list`。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch94_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch94_bak_"))

import cli  # noqa: E402
from app.services import pending_service as PS  # noqa: E402
from app.util import name_list  # noqa: E402


def _by_id(findings, fid):
    return next(f for f in findings if f["id"] == fid)


# ---- name_list 的 total 覆写：名单是下限时也能说出真实总数 ----

class NameListTotalOverrideTests(unittest.TestCase):
    def test_real_total_is_named_even_though_list_is_shorter(self):
        got = name_list([f"u{i}.service" for i in range(12)], 5, sep=", ", total=40)
        self.assertIn("共 40 个", got)
        self.assertIn("另 35 个未列出", got)

    def test_default_total_still_follows_len(self):
        self.assertNotIn("未列出", name_list(["a.service", "b.service"], 5))


# ---- _findings：判词里的数读 need_reload_total，不是被截过的名单长度 ----

class FindingsReloadTotalTests(unittest.TestCase):
    RELOADS = {
        "nginx": {"ok": True, "pending": False},
        "sshd": {"ok": True, "pending": False},
        "systemd": {"ok": True, "checked": 130, "need_reload_total": 40,
                    "need_reload": [f"u{i}.service" for i in range(12)]},
    }

    def test_title_uses_the_true_total_not_the_capped_list(self):
        f = _by_id(PS._findings({"needs_reboot": False}, {}, self.RELOADS), "daemon-reload")
        self.assertEqual(f["title"], "40 个 unit 文件改过但没 daemon-reload")

    def test_detail_declares_the_unlisted_omission(self):
        f = _by_id(PS._findings({"needs_reboot": False}, {}, self.RELOADS), "daemon-reload")
        self.assertIn("共 40 个", f["detail"])
        self.assertIn("另 35 个未列出", f["detail"])

    def test_short_list_is_not_padded_with_an_invented_total(self):
        rl = {"systemd": {"ok": True, "checked": 3, "need_reload_total": 2,
                          "need_reload": ["a.service", "b.service"]},
              "nginx": {"ok": True, "pending": False}, "sshd": {"ok": True, "pending": False}}
        f = _by_id(PS._findings({"needs_reboot": False}, {}, rl), "daemon-reload")
        self.assertNotIn("未列出", f["detail"])
        self.assertIn("a.service, b.service", f["detail"])

    def test_missing_total_falls_back_to_list_length(self):
        rl = {"systemd": {"ok": True, "checked": 8,
                          "need_reload": [f"u{i}.service" for i in range(8)]},
              "nginx": {"ok": True, "pending": False}, "sshd": {"ok": True, "pending": False}}
        f = _by_id(PS._findings({"needs_reboot": False}, {}, rl), "daemon-reload")
        self.assertEqual(f["title"], "8 个 unit 文件改过但没 daemon-reload")


# ---- CLI：待重载总数与未列出的差额都念出来 ----

def _payload(systemd):
    return {"counts": {"risk": 0, "warn": 0, "info": 0}, "kernel": {"ok": True, "running": "6.1"},
            "stale": {"ok": True, "scanned": 1, "procs_total": 1, "procs_stale": 0, "units": [],
                      "unit_total": 0, "orphan_total": 0, "elapsed_ms": 5, "budget_hit": False},
            "reloads": {"nginx": {"ok": True, "pending": False, "conf_files": 2},
                        "sshd": {"ok": True, "pending": False, "files": 1, "pid": 671},
                        "systemd": systemd},
            "apt": {"ok": True}, "findings": [], "notes": [], "uptime_sec": 60,
            "as_of": "2026-10-11 22:49:04", "cached": False}


class CliPendingTextReloadTotalTests(unittest.TestCase):
    def test_reports_the_true_total_and_the_omission(self):
        text = cli._pending_text(_payload(
            {"ok": True, "checked": 200, "need_reload_total": 40,
             "need_reload": [f"u{i}.service" for i in range(12)]}))
        self.assertIn("40 个待 daemon-reload", text)
        self.assertIn("另有 36 个未列出", text)

    def test_small_reload_set_is_not_said_to_be_truncated(self):
        text = cli._pending_text(_payload(
            {"ok": True, "checked": 20, "need_reload_total": 2,
             "need_reload": ["a.service", "b.service"]}))
        self.assertIn("2 个待 daemon-reload", text)
        self.assertNotIn("未列出", text)


# ---- 源码契约：总数在截断之前捕获，前端读的是 total 不是名单长度 ----

class SourceContractTests(unittest.TestCase):
    def _src(self, rel: str) -> str:
        return (BACKEND / rel).read_text(encoding="utf-8")

    def test_total_is_captured_before_the_payload_cap(self):
        src = self._src("app/services/pending_service.py")
        cap = src.index("out[\"need_reload\"] = out[\"need_reload\"][:MAX_STALE_UNITS]")
        assign = src.index("out[\"need_reload_total\"] = len(out[\"need_reload\"])")
        self.assertLess(assign, cap, "总数必须在截到 12 条之前捕获，否则只剩下限")

    def test_finding_routes_through_name_list(self):
        src = self._src("app/services/pending_service.py")
        self.assertIn("name_list(listed, 5, sep=', ', total=total)", src)

    def test_vue_reads_need_reload_total_not_the_list_length(self):
        src = (BACKEND.parent / "web" / "src" / "views" / "Services.vue").read_text(encoding="utf-8")
        # 两处"多少个待重载"（表格单元 + 说明芯片）都改读 need_reload_total。
        self.assertGreaterEqual(src.count("need_reload_total"), 2)
        # 表格单元里给用户看的总数不能再直接从截过的 need_reload.length 数出来。
        self.assertNotIn("${r.need_reload.length} 个", src)


if __name__ == "__main__":
    unittest.main()
