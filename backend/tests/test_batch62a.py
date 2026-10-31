"""批次 62A：读不到的事不能说成"没问题"，提示不能指一条不存在的命令。

这三件事来自技术债盘点的"诚实性 / 一处读者"两类，共同点都是同一句话在三个入口上
说的是三件事，或者一句好消息其实来自一次探测失败：

- **24h 失败认证**：journal 读不到时 `attempts` 那份占位的 `failed` 就是 0，
  自检那句绿灯照原样念出来成了"24 小时失败认证 0 次"，CLI 概览行也一样，
  网页那三格芯片也一样——而这一格想说的恰恰是"有没有人在撞库"。
  判据用服务端已经给的 `attempts.ok`：读不到就写读不到，与那条横幅同一个说法。
- **证书续期看的是哪个目录**：站点页那个读者读 `config.TLS_DIR`，自检这项硬编码
  `/etc/letsencrypt/live`。改过 CP_TLS_DIR 的机器上自检永远看见"零张证书"直接 pass，
  「有证书但没人续期」这件事在任何界面上都不再出现。
- **失败留痕要遮凭据**：备份失败正文里最常出现的就是带口令的连接串，而审计表跟着
  panel.db 一起推去异地。同文件另外四处早就 redact_creds 了，漏的是 restore/run 两处
  与 dbops 的行写失败。
- **异地拉回之后往哪走**：CLI 成功后提示「现在可以用 backup restore 恢复」，
  而 backup 的子命令表里没有 restore，照这句话敲得到 exit 2。
- **应用表读不到不等于没有应用**：端口对账把 apps 表读失败折成空表，载荷仍 ok:true，
  于是应用端口认不出来（可能误判对外暴露），"登记的应用"空着又像这台机器没登记过。
- **权限 stat 失败不算核对过**：数据目录那一项把所有 OSError 都当"不存在"跳过，
  读不到也写"权限正常"。
- **CI 自己得走文档唯一那条入口**：冒烟用 `--password '<字面量>'`，违反 AGENTS.md 第 7
  条（argv 里的口令同机可见、还进日志），而 `--password-stdin` 这条正规入口在 CI 从没跑过。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch62a_data_"))

from fastapi.testclient import TestClient  # noqa: E402

import cli  # noqa: E402
from app import config, main, security  # noqa: E402
from app import database as dbm
from app.routers import meta  # noqa: E402
from app.services import backup_service, cert_ops, pg_service  # noqa: E402
from app.services import exposure_service as ex  # noqa: E402
from app.services import login_audit_service as LA  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TMP_DIRS: list[str] = []


def tearDownModule():
    for d in TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def _tmp(prefix: str) -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    TMP_DIRS.append(d)
    return d


def _guard(status="guarded", phrase="fail2ban.service active"):
    return {"status": status, "phrase": phrase, "units": []}


def _att(**kw):
    base = {"ok": True, "window": "24h", "failed": 0, "distinct_ips": 0, "top": [],
            "accepted": [], "accepted_ips": [], "note": ""}
    base.update(kw)
    return base


def _login_data(**kw):
    base = {"as_root": True,
            "sshd": {"source": "sshd -T", "port": 22, "reachable": True},
            "counts": {"risk": 0, "warn": 0, "note": 0}, "findings": [], "notes": ["说明"],
            "guard": _guard(), "settings": [], "keys": {"files": [], "scanned": 0, "total_keys": 0},
            "accounts": {"login_shells": [], "login_count": 0, "uid0_extra": [],
                         "empty_password": [], "shadow_unreadable": False, "sudo_groups": {}},
            "attempts": _att(failed=3, distinct_ips=2)}
    base.update(kw)
    return base


# ------------------------------------------------ 1. 读不到的认证日志不说成 0 次

class AttemptsUnreadTests(unittest.TestCase):
    def test_doctor_does_not_read_the_missing_window_as_zero(self):
        got = LA.doctor_item(_login_data(attempts=_att(ok=False, failed=0, distinct_ips=0,
                                                       note="没有 journalctl，统计不到登录尝试")))
        self.assertEqual(got["status"], "warn", "探测失败换来的不能是一条好消息")
        self.assertNotIn("0 次", got["detail"])
        self.assertIn("读不到", got["detail"])
        self.assertIn("journalctl", got["detail"], "判词要带上为什么读不到，不然只能再查一次")

    def test_doctor_still_counts_a_readable_window(self):
        """对照：日志读得到时那句照旧，别把修诚实做成把好消息也删掉。"""
        got = LA.doctor_item(_login_data(attempts=_att(failed=11, distinct_ips=4)))
        self.assertEqual(got["status"], "pass")
        self.assertIn("过去 24 小时失败认证 11 次", got["detail"])

    def test_cli_overview_line_says_unread(self):
        text = cli._ssh_audit_text(_login_data(attempts=_att(ok=False, note="journalctl 退出码 1，这段窗口没读到")))
        self.assertNotIn("失败认证 0 次", text)
        self.assertIn("认证日志读不到", text)
        self.assertIn("journalctl 退出码 1", text)

    def test_cli_overview_line_still_prints_the_count(self):
        text = cli._ssh_audit_text(_login_data(attempts=_att(failed=7, distinct_ips=3)))
        self.assertIn("失败认证 7 次（3 个来源地址）", text)

    def test_frontend_chips_gate_on_the_same_flag(self):
        """网页那三格芯片与横幅必须同一个判据：不许一处写"读不到"、另一处写"0 次"。"""
        src = (REPO / "web/src/views/Services.vue").read_text(encoding="utf-8")
        for line in re.findall(r"<span class=\"ls-chip[^\n]*?(?:24h 失败认证|来源地址|成功登录)[^\n]*", src):
            self.assertNotRegex(line, r"loginAttempts\.\w+ \?\? '—'",
                                "读不到的占位 0 会照样落进这一格：要用 attempts.ok 折成说法")
        self.assertIn("const attemptsLine = computed(() => (loginAttempts.value.ok", src)
        self.assertIn("const sourcesLine = computed(() => (loginAttempts.value.ok", src)
        self.assertIn("if (!loginAttempts.value.ok) return '读不到';", src)


# ------------------------------------------------ 2. 证书续期只有一个目录读者

class CertRenewalSingleReaderTests(unittest.TestCase):
    def test_the_check_reads_the_configured_tls_dir(self):
        live = Path(_tmp("cp62a_tls_")) / "live"
        (live / "panel.example.com").mkdir(parents=True)
        (live / "panel.example.com" / "fullchain.pem").write_text("x", encoding="utf-8")
        timer = mock.AsyncMock(return_value={"status": "missing", "detail": "没有定时器"})
        with mock.patch.object(config, "TLS_DIR", str(live)), \
             mock.patch.object(meta.shutil, "which", lambda n: "/usr/bin/certbot"), \
             mock.patch.object(cert_ops, "timer_status", timer):
            got = asyncio.run(meta._check_cert_renewal())
        self.assertEqual(got["status"], "warn", "换了目录的机器上这张证书也得被看见")
        self.assertIn("1 张证书", got["detail"])

    def test_the_source_keeps_one_path_reader(self):
        """对照：硬编码那份是这次要修的病，别再长回来。"""
        src = (REPO / "backend/app/routers/meta.py").read_text(encoding="utf-8")
        self.assertNotIn('Path("/etc/letsencrypt', src)
        self.assertIn("Path(config.TLS_DIR)", src)


# ------------------------------------------------ 3. 失败留痕遮凭据

class AuditRedactionTests(unittest.TestCase):
    SK = "Sup3rSecretDsnPw"

    def setUp(self):
        self.user = "u62a_redact_admin"
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.client = TestClient(main.app)
        self.h = {"Authorization": f"Bearer {security.sign_token(self.user, 'admin')}"}

    def _last(self, action):
        return dbm.query_one("SELECT detail FROM audit WHERE action=? ORDER BY id DESC", (action,))

    def test_backup_run_failure_is_redacted(self):
        boom = mock.AsyncMock(side_effect=RuntimeError(f"pg_dump 失败：could not connect "
                                                      f"postgres://panel:{self.SK}@127.0.0.1/db"))
        with mock.patch.object(backup_service, "run_backup_now", boom):
            r = self.client.post("/api/backups/999/run", headers=self.h, json={})
        self.assertEqual(r.status_code, 500)
        row = self._last("backup:run")
        self.assertIsNotNone(row)
        self.assertNotIn(self.SK, row["detail"])
        self.assertIn("999", row["detail"], "任务号要留着，不然看不出是哪条任务失败")

    def test_backup_restore_failure_is_redacted(self):
        boom = mock.AsyncMock(side_effect=RuntimeError(f"restore 用了 password={self.SK} 的连接"))
        with mock.patch.object(backup_service, "restore_backup", boom):
            r = self.client.post("/api/backups/999/restore", headers=self.h,
                                 json={"file": "bk999-app.sqlite"})
        self.assertIn(r.status_code, (400, 500))
        row = self._last("backup:restore")
        self.assertIsNotNone(row)
        self.assertNotIn(self.SK, row["detail"])
        self.assertIn("***", row["detail"])

    def test_pg_row_write_failure_is_redacted(self):
        boom = mock.AsyncMock(side_effect=RuntimeError(f"OperationalError: connection "
                                                       f"require password={self.SK} failed"))
        with mock.patch.object(pg_service, "write_row", boom):
            self.client.post("/api/db/pg/appdb/row", headers=self.h,
                             json={"action": "update", "schema": "public", "table": "t"})
        row = self._last("db:row-update")
        self.assertIsNotNone(row)
        self.assertNotIn(self.SK, row["detail"])

    def tearDown(self):
        for action in ("backup:run", "backup:restore", "db:row-update"):
            dbm.execute("DELETE FROM audit WHERE action=?", (action,))


# ------------------------------------------------ 4. 提示不指一条不存在的命令

class PullHintTests(unittest.TestCase):
    def _pull_text(self, restoreable: bool) -> str:
        args = cli.build_parser().parse_args(
            ["backup", "cloud-pull", "--id", "7", "--target", "3", "--name", "bk7-x.tar.gz"])
        res = {"ok": True, "target": "minio", "name": "bk7-x.tar.gz", "bytes": 4096,
               "restoreable": restoreable}
        out = io.StringIO()
        with mock.patch.object(cli.cloud_backup, "pull_one", return_value=res), \
             mock.patch.object(cli.Out, "json_mode", False), \
             mock.patch.object(sys, "stdout", out):
            asyncio.run(cli.cmd_backup(args))
        return out.getvalue()

    def test_hint_points_at_the_recovery_entry_that_exists(self):
        text = self._pull_text(True)
        self.assertNotIn("backup restore", text)
        self.assertIn("备份文件", text)

    def test_non_restoreable_artifact_still_says_so(self):
        self.assertIn("不支持自动恢复", self._pull_text(False))

    def test_the_cli_never_advertises_a_backup_restore_subcommand(self):
        src = (REPO / "backend/cli.py").read_text(encoding="utf-8")
        self.assertNotIn("backup restore", src)


# ------------------------------------------------ 5. 应用表读不到不等于没有应用

class AppPortsUnreadTests(unittest.TestCase):
    SS = ('LISTEN 0 511 0.0.0.0:3100 0.0.0.0:* users:(("next-server",pid=15,fd=3))\n')

    def _collect(self, query):
        async def fake_run(cmd, args, cwd=None, timeout=120, env=None):
            return {"code": 0, "out": self.SS, "err": ""}

        ex._cache["data"] = None
        ex._cache["at"] = 0.0
        with mock.patch.object(ex, "run", fake_run), \
             mock.patch.object(ex.firewall_service, "read",
                               mock.AsyncMock(return_value={"tool": "ufw", "active": True,
                                                             "default_incoming": "deny", "rules": [],
                                                             "state_ok": True, "rules_ok": True})), \
             mock.patch.object(ex.firewall_service, "protected_ports",
                               lambda request=None: ({22: "x"}, {22: "sshd 监听端口"})), \
             mock.patch.object(ex.dbm, "query", query), \
             mock.patch.object(ex.shutil, "which", lambda n: "/usr/bin/ss"):
            return asyncio.run(ex.audit(True))

    def test_unread_apps_table_says_so_in_notes(self):
        def boom(sql, params=()):
            raise RuntimeError("no such table: apps")

        data = self._collect(boom)
        self.assertEqual(data["apps"], [])
        self.assertTrue(any("应用表没读到" in n for n in data["notes"]), data["notes"])
        self.assertIn("应用表没读到", cli._ports_text(data))

    def test_readable_apps_table_adds_no_complaint(self):
        data = self._collect(lambda sql, params=(): [{"id": 1, "name": "web", "port": 3100,
                                                      "domain": ""}])
        self.assertEqual([a["name"] for a in data["apps"]], ["web"])
        self.assertFalse(any("应用表" in n for n in data["notes"]), data["notes"])


# ------------------------------------------------ 6. 权限读不到不算核对过

class PermissionsUnreadTests(unittest.TestCase):
    def test_missing_env_is_still_not_judged(self):
        with mock.patch.object(config, "ENV_FILE", _tmp("cp62a_env_") + "/absent.env"), \
             mock.patch.object(config, "DATA_DIR", _tmp("cp62a_data_")):
            got = asyncio.run(meta._check_permissions())
        self.assertEqual(got["status"], "pass")

    def test_stat_failure_is_a_warn_not_the_green_line(self):
        blocker = Path(_tmp("cp62a_block_")) / "file"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.object(config, "ENV_FILE", str(blocker.parent / "absent.env")), \
             mock.patch.object(config, "DATA_DIR", str(blocker / "sub")):
            got = asyncio.run(meta._check_permissions())
        self.assertEqual(got["status"], "warn")
        self.assertIn("读不到", got["detail"])
        self.assertNotIn("权限正常", got["detail"])


# ------------------------------------------------ 7. CI 冒烟走文档那条入口

class CiCredentialTests(unittest.TestCase):
    """AGENTS.md 第 7 条禁的是 argv 里的口令；注释里提到这个写法是被禁的说明，不算违规。
    所以断言只看真正会被执行的那些行。"""

    @staticmethod
    def _executable(text: str) -> str:
        return "\n".join(
            ln for ln in text.splitlines()
            if not ln.lstrip().startswith("#")
        )

    def test_ci_never_puts_a_password_in_argv(self):
        text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        code = self._executable(text)
        self.assertNotIn("--password ", code)
        self.assertNotRegex(code, r"--password\s*=\s*['\"]")
        self.assertIn("--password-stdin", text)

    def test_ci_password_is_generated_not_a_literal(self):
        text = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("secrets.choice", text)
        self.assertIn('printf \'%s\' "$PW" |', text)


if __name__ == "__main__":
    unittest.main()
