"""批次89：清单与日志的静默截断必须自报真实总量。

自检详情、登录安全判词、改动未生效判词里都有「只列前 N 个」的写法。位置有限需要截，
但截断这件事本身从来不说：12 个空口令账号只列 10 个、8 把弱钥匙只列 6 把、
7 张证书只列 5 张——界面读起来像「就这些」，漏掉的那几条于是没人去处理。
certbot 输出同理：`log[:4000]` 在 4000 字符处一刀切，出错那一段若在后面就永远看不到。

这里钉住：
- `util.name_list` / `util.clip_text` 的截断说法（总数 + 未列出数）；
- 登录安全、改动未生效、定时器单元、证书续期四处真的用上了它；
- 续期日志两条出口不再裸切片。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b89_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, util  # noqa: E402
from app.routers import meta as META  # noqa: E402
from app.services import login_audit_service as LOGIN  # noqa: E402
from app.services import pending_service as PENDING  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ISO = "2026-10-01T00:00:00+00:00"


def _aio(coro):
    return asyncio.run(coro)


class NameListTests(unittest.TestCase):
    def test_short_list_is_unchanged(self):
        self.assertEqual(util.name_list(["a", "b"], 3), "a、b")

    def test_exact_fit_is_unchanged(self):
        self.assertEqual(util.name_list(["a", "b", "c"], 3), "a、b、c")

    def test_truncation_reports_total_and_omitted(self):
        got = util.name_list([f"n{i}" for i in range(12)], 6)
        self.assertTrue(got.startswith("n0、n1、n2、n3、n4、n5"))
        self.assertIn("共 12 个", got)
        self.assertIn("另 6 个未列出", got)

    def test_custom_separator_and_unit(self):
        got = util.name_list(["a", "b", "c"], 2, sep=", ", unit="张")
        self.assertIn("a, b…", got)
        self.assertIn("共 3 张，另 1 张未列出", got)

    def test_generator_is_materialised(self):
        got = util.name_list((str(i) for i in range(5)), 4)
        self.assertIn("共 5 个，另 1 个未列出", got)


class ClipTextTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(util.clip_text("abc", 10), "abc")

    def test_none_becomes_empty(self):
        self.assertEqual(util.clip_text(None, 10), "")

    def test_truncation_reports_true_length(self):
        got = util.clip_text("x" * 25, 10, what="命令输出")
        self.assertTrue(got.startswith("x" * 10 + "…"))
        self.assertIn("命令输出共 25 字符", got)
        self.assertIn("只显示了前 10 字符", got)

    def test_exact_fit_is_unchanged(self):
        self.assertEqual(util.clip_text("x" * 10, 10), "x" * 10)


# ---- 登录安全：四份清单 ----

def _key(comment="laptop", fp="SHA256:x", weak=False, unparsed=False):
    return {"comment": comment, "fp": fp, "weak": weak, "unparsed": unparsed}


def _keyfile(user, keys=None, flags=None):
    return {"user": user, "flags": flags or [], "symlink": False, "keys": keys or []}


def _ssh():
    return {
        "source": "sshd -T",
        "values": {
            "passwordauthentication": ["no"],
            "permitrootlogin": ["prohibit-password"],
            "kbdinteractiveauthentication": ["no"],
            "permitemptypasswords": ["no"],
        },
        "notes": [],
    }


def _accounts(**kw):
    base = {"empty_password": [], "uid0_extra": []}
    base.update(kw)
    return base


def _attempts():
    return {"ok": True, "failed": 0, "distinct_ips": 0, "truncated": False}


def _findings(accounts, keys):
    return LOGIN._findings(_ssh(), keys, accounts, _attempts(),
                           {"status": "guarded"}, {"reachable": True})


def _by_id(findings, fid):
    return next(f for f in findings if f["id"] == fid)


class LoginAuditTotalTests(unittest.TestCase):
    def test_empty_password_list_reports_twelve(self):
        acc = _accounts(empty_password=[f"u{i}" for i in range(12)])
        f = _by_id(_findings(acc, {"files": []}), "empty-password")
        self.assertIn("共 12 个，另 2 个未列出", f["detail"])
        self.assertIn("u9", f["detail"])
        self.assertNotIn("u10", f["detail"])

    def test_weak_keys_report_eight(self):
        files = [
            _keyfile("bob", [_key(comment="old", weak=True) for _ in range(5)]),
            _keyfile("amy", [_key(comment="old", weak=True) for _ in range(3)]),
        ]
        f = _by_id(_findings(_accounts(), {"files": files}), "weak-key")
        self.assertIn("共 8 把，另 2 把未列出", f["detail"])

    def test_bad_key_files_report_six(self):
        files = [_keyfile(f"u{i}", [_key()], flags=["可写"]) for i in range(6)]
        f = _by_id(_findings(_accounts(), {"files": files}), "ak-file")
        self.assertIn("共 6 个，另 2 个未列出", f["detail"])

    def test_keys_without_comment_report_seven(self):
        files = [_keyfile("quiet", [_key(comment="") for _ in range(7)])]
        f = _by_id(_findings(_accounts(), {"files": files}), "no-comment")
        self.assertIn("共 7 把，另 2 把未列出", f["detail"])


# ---- 改动未生效：三份清单 ----

def _stale_unit(pkgs, security_all=False):
    files = [{"path": f"/usr/lib/{p}.so", "pkg": p, "pkg_version": "1",
              "security": security_all or i == 0, "gone": True, "ctime_iso": ISO}
             for i, p in enumerate(pkgs)]
    return {"unit": "nginx.service", "files": files, "procs": 2, "pids": [10, 11],
            "started_iso": ISO, "age_human": "3 天"}


def _pending(**reloads):
    return PENDING._findings({"needs_reboot": False}, {}, reloads)


class PendingTotalTests(unittest.TestCase):
    def test_newer_kernels_report_five(self):
        kernel = {"needs_reboot": True, "running": "6.1.0-1",
                  "newer": [{"pkg": f"linux-image-{i}"} for i in range(5)]}
        f = _by_id(PENDING._findings(kernel, {}, {}), "reboot-kernel")
        self.assertIn("共 5 个，另 2 个未列出", f["detail"])
        self.assertIn("linux-image-0", f["detail"])
        self.assertNotIn("linux-image-4", f["detail"])

    def test_stale_file_packages_report_six(self):
        # 安全相关文件优先入册；全标 security 才能一次点到 6 个包名。
        stale = {"units": [_stale_unit([f"lib{i}" for i in range(6)], security_all=True)]}
        f = _by_id(PENDING._findings({"needs_reboot": False}, stale, {}), "stale-lib:nginx.service")
        self.assertIn("共 6 个，另 2 个未列出", f["detail"])

    def test_need_daemon_reload_reports_eight(self):
        f = _by_id(_pending(systemd={"need_reload": [f"u{i}.service" for i in range(8)]}),
                   "daemon-reload")
        self.assertIn("共 8 个，另 3 个未列出", f["detail"])


# ---- 自检：定时器残留与证书清单 ----

def _timer_units(files, cron_ids=(), backup_ids=()):
    base = tempfile.mkdtemp(prefix="cp_b89_units_")
    for name in files:
        Path(base, name).write_text("[Timer]\n", encoding="utf-8")

    async def fake_query(sql, params=()):
        return [{"id": i} for i in (cron_ids if "cron_task" in sql else backup_ids)]

    try:
        with mock.patch.object(config, "UNIT_DIR", base), \
                mock.patch.object(META.aio_db, "query", fake_query):
            return _aio(META._check_timer_units())
    finally:
        shutil.rmtree(base, ignore_errors=True)


class TimerUnitTotalTests(unittest.TestCase):
    def test_orphans_report_total_and_remaining_fixes(self):
        out = _timer_units([f"panel-cron-90{i}.timer" for i in range(8)])
        self.assertEqual(out["status"], "warn")
        self.assertIn("共 8 个，另 2 个未列出", out["detail"])
        self.assertIn("另有 2 个残留单元未列出", out["fix"])

    def test_missing_units_report_total(self):
        out = _timer_units([], cron_ids=[901, 902, 903, 904, 905, 906, 907])
        self.assertEqual(out["status"], "warn")
        self.assertIn("共 7 个，另 1 个未列出", out["detail"])


class CertNamesTotalTests(unittest.TestCase):
    def test_cert_list_reports_seven(self):
        base = tempfile.mkdtemp(prefix="cp_b89_tls_")
        try:
            for i in range(7):
                d = Path(base, f"site{i}")
                d.mkdir()
                (d / "fullchain.pem").write_text("x", encoding="utf-8")
            timer = {"status": "installed", "checked": "systemd", "detail": "每 12 小时"}
            with mock.patch.object(config, "TLS_DIR", base), \
                    mock.patch.object(META.shutil, "which", lambda _: "/usr/bin/certbot"), \
                    mock.patch.object(META.cert_ops, "timer_status", lambda: _const(timer)):
                out = _aio(META._check_cert_renewal())
        finally:
            shutil.rmtree(base, ignore_errors=True)
        self.assertEqual(out["status"], "pass", out)
        self.assertIn("共 7 张，另 2 张未列出", out["detail"])


async def _const(value):
    return value


# ---- 续期日志出口：不再裸切片 ----

class RenewLogContractTests(unittest.TestCase):
    def test_extras_clips_with_true_length(self):
        src = (ROOT / "backend/app/routers/extras.py").read_text(encoding="utf-8")
        self.assertIn('clip_text(redact_creds(res["log"]), 4000, what="certbot 输出")', src)
        self.assertNotIn('redact_creds(res["log"])[:4000]', src)

    def test_sites_clips_with_true_length(self):
        src = (ROOT / "backend/app/routers/sites.py").read_text(encoding="utf-8")
        self.assertIn('clip_text(_mask(redact_creds(res["log"])), 4000, what="certbot 输出")', src)
        self.assertNotIn('_mask(redact_creds(res["log"]))[:4000]', src)

    def test_sites_error_detail_clips_with_true_length(self):
        src = (ROOT / "backend/app/routers/sites.py").read_text(encoding="utf-8")
        self.assertIn('clip_text(_mask(redact_creds(raw)), 400, what="错误信息")', src)


if __name__ == "__main__":
    unittest.main()
