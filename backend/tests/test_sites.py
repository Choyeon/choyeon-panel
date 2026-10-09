"""网站/证书总览（cert_ops + routers/sites）的回归测试。

锁定的都是"不能假装成功"这类行为：
1. certbot 文本要正确分桶成 ok / expiring / expired，天数按真实到期时间算。
2. 机器上没有 certbot 时必须改用 openssl 读证书文件，而不是返回空列表冒充"没有证书"。
3. 两个来源都拿不到 → 空列表 + reason，绝不编数据。
4. 续期定时器实测没有就报 missing，检查不了就报 unknown（UI 不能再宣称"续期已托管"）。
5. nginx 配置目录不存在 → /api/sites 返回空 + reason。
6. 写接口仅管理员可用，错误正文里不出现本机绝对路径。
"""

import asyncio
import itertools
import os
import re
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_sites_ut_"))

from app import config  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import sites  # noqa: E402
from app.services import cert_ops  # noqa: E402

_seq = itertools.count()
NOW = datetime(2026, 10, 10, tzinfo=UTC)
_TMP_DIRS = []

CERTBOT_OUT = """Saving debug log to /var/log/letsencrypt/letsencrypt.log

- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
Found 3 certificates

Certificate Name: a.example.com
  Serial Number: 1234567
  Key Type: ECDSA
  Domains: a.example.com www.a.example.com
  Expiry Date: 2026-12-01 00:00:00+00:00 (VALID: 52 days)
  Certificate Path: /etc/letsencrypt/live/a.example.com/fullchain.pem
  Private Key Path: /etc/letsencrypt/live/a.example.com/privkey.pem

Certificate Name: b.example.com
  Serial Number: 7654321
  Domains: b.example.com
  Expiry Date: 2026-10-20 00:00:00+00:00 (VALID: 10 days)
  Certificate Path: /etc/letsencrypt/live/b.example.com/fullchain.pem

Certificate Name: c.example.com
  Domains: c.example.com
  Expiry Date: 2026-09-01 00:00:00+00:00 (INVALID: EXPIRED)
  Certificate Path: /etc/letsencrypt/live/c.example.com/fullchain.pem

- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
"""

OPENSSL_OUT = """notAfter=Dec  1 00:00:00 2026 GMT
issuer=C = US, O = Let's Encrypt, CN = R3
subject=CN = a.example.com
X509v3 Subject Alternative Name:
    DNS:a.example.com, DNS:www.a.example.com"""

ABS_PATH_RE = re.compile(r"/(?:etc|root|usr|var|home|tmp)/")


def _fake_run(table):
    """按命令名分发假子进程结果；未列出的命令一律当作"没装"（127）。"""

    async def _run(cmd, args, **kw):
        value = table.get(cmd)
        if value is None:
            return {"code": 127, "out": f"{cmd}: command not found"}
        return value if not callable(value) else value(args)

    return _run


def _req(role="admin", sub="sites_tester"):
    return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub=sub))


def _mkdtemp(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return d


def _aname(prefix="sites-app"):
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _mk_app(domain, port=3000):
    """插一行应用并返回 (name, id)；调用方自己 addCleanup 删掉，避免污染同库的其它用例。"""
    name = _aname()
    app_id = dbm.execute(
        "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,start_cmd) VALUES(?,?,?,?,?,?,?,?)",
        (name, "node", "", "main", f"/root/www/{name}", port, domain, "node x.js"),
    )
    return name, app_id


# nginx 配置里满是 `{}`，用 str.format 会和占位符打架，这里用显式的 __TOKEN__ 替换。
PANEL_CONF = """server {
    listen 80;
    listen [::]:80;
    server_name __DOMAIN__;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name __DOMAIN__;
    ssl_certificate /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    location / {
        proxy_pass http://127.0.0.1:__PORT__;
        proxy_set_header Upgrade $http_upgrade;
    }
}
"""

MANUAL_CONF = ("server {\n    listen 80;\n    server_name __DOMAIN__;\n"
               "    location / { proxy_pass http://127.0.0.1:__PORT__; }\n}\n")


def _conf_text(tpl: str, domain: str, port: int) -> str:
    return tpl.replace("__DOMAIN__", domain).replace("__PORT__", str(port))


class TestCertbotParsing(unittest.TestCase):
    def test_days_and_status_buckets(self):
        table = {"certbot": {"code": 0, "out": CERTBOT_OUT}, "openssl": {"code": 0, "out": OPENSSL_OUT}}
        with mock.patch.object(cert_ops, "run", _fake_run(table)):
            data = asyncio.run(cert_ops.list_certs(NOW))
        self.assertEqual(data["source"], "certbot")
        self.assertIsNone(data["reason"])
        by_name = {c["name"]: c for c in data["certs"]}
        self.assertEqual(sorted(by_name), ["a.example.com", "b.example.com", "c.example.com"])

        a, b, c = by_name["a.example.com"], by_name["b.example.com"], by_name["c.example.com"]
        self.assertEqual(a["status"], "ok")
        self.assertEqual(a["days_left"], 52)
        self.assertEqual(a["domains"], ["a.example.com", "www.a.example.com"])
        # certbot 的文本里没有签发者，签发者是从证书文件本身读出来的
        self.assertEqual(a["issuer"], "Let's Encrypt")
        self.assertEqual(a["certbot_id"], "a.example.com")
        self.assertTrue(a["expiry"].startswith("2026-12-01"))

        self.assertEqual(b["status"], "expiring")
        self.assertEqual(b["days_left"], 10)
        self.assertEqual(c["status"], "expired")
        self.assertLess(c["days_left"], 0)
        self.assertTrue(c["invalid"])

    def test_expiring_boundary_is_21_days(self):
        out = ("Certificate Name: edge.example.com\n  Domains: edge.example.com\n"
               "  Expiry Date: 2026-10-31 00:00:00+00:00 (VALID: 21 days)\n")
        with mock.patch.object(cert_ops, "run", _fake_run({"certbot": {"code": 0, "out": out}})):
            data = asyncio.run(cert_ops.list_certs(NOW))
        self.assertEqual(data["certs"][0]["days_left"], 21)
        self.assertEqual(data["certs"][0]["status"], "expiring")

    def test_unparseable_expiry_is_unknown_not_ok(self):
        """拿不到天数时不能默认"没事"——那正是证书静默过期的路径。"""
        out = ("Certificate Name: weird.example.com\n  Domains: weird.example.com\n"
               "  Expiry Date: not-a-date\n")
        with mock.patch.object(cert_ops, "run", _fake_run({"certbot": {"code": 0, "out": out}})):
            data = asyncio.run(cert_ops.list_certs(NOW))
        self.assertIsNone(data["certs"][0]["days_left"])
        self.assertEqual(data["certs"][0]["status"], "unknown")


class TestOpensslFallback(unittest.TestCase):
    def test_used_when_certbot_missing(self):
        live = _mkdtemp("cp_live_")
        for n in ("a.example.com", "b.example.com"):
            p = Path(live, n)
            p.mkdir()
            (p / "fullchain.pem").write_text("PEM")
        Path(live, "README").write_text("keep")
        table = {"openssl": {"code": 0, "out": OPENSSL_OUT}}
        with mock.patch.object(cert_ops, "run", _fake_run(table)), mock.patch.object(config, "TLS_DIR", live):
            data = asyncio.run(cert_ops.list_certs(NOW))
        self.assertEqual(data["source"], "openssl")
        self.assertIn("certbot", data["reason"] or "", "回退口径必须说明为什么不是 certbot")
        self.assertEqual({c["name"] for c in data["certs"]}, {"a.example.com", "b.example.com"})
        first = data["certs"][0]
        self.assertEqual(first["domains"], ["a.example.com", "www.a.example.com"])
        self.assertEqual(first["issuer"], "Let's Encrypt")
        self.assertEqual(first["days_left"], 52)
        self.assertEqual(first["certbot_id"], "a.example.com")

    def test_both_sources_unavailable_returns_reason_not_fake_rows(self):
        with mock.patch.object(cert_ops, "run", _fake_run({})), \
             mock.patch.object(config, "TLS_DIR", "/nonexistent-letsencrypt-dir"):
            data = asyncio.run(cert_ops.list_certs(NOW))
        self.assertEqual(data["certs"], [])
        self.assertEqual(data["source"], "none")
        self.assertTrue(data["reason"])
        # 目录不可读时只报异常类型：str(OSError) 会把本机绝对路径带进接口
        self.assertIsNone(ABS_PATH_RE.search(data["reason"]))

    def test_certbot_present_but_lists_nothing(self):
        with mock.patch.object(cert_ops, "run", _fake_run({"certbot": {"code": 0, "out": "No certificates found."}})), \
             mock.patch.object(config, "TLS_DIR", "/nonexistent-letsencrypt-dir"):
            data = asyncio.run(cert_ops.list_certs(NOW))
        self.assertEqual(data["certs"], [])
        self.assertEqual(data["source"], "certbot")


class TestTimerStatus(unittest.TestCase):
    def test_installed_via_systemd(self):
        out = "Tue 2026-10-13 03:14:00 UTC 12h left certbot.timer certbot.service\n"
        with mock.patch.object(cert_ops, "run", _fake_run({"systemctl": {"code": 0, "out": out}})):
            self.assertEqual(asyncio.run(cert_ops.timer_status())["status"], "installed")

    def test_missing_reported_honestly_without_unit(self):
        """本机没装 certbot.timer、也没有 cron 任务 → missing，而不是"已托管"。"""
        table = {
            "systemctl": {"code": 0, "out": "Mon 2026-10-13 00:00:00 UTC 1h left logrotate.timer x\n"},
            "crontab": {"code": 1, "out": "no crontab for root"},
        }
        with mock.patch.object(cert_ops, "run", _fake_run(table)), \
             mock.patch.object(cert_ops, "_CRON_DIRS", []), \
             mock.patch.object(cert_ops, "_TIMER_UNIT_DIRS", []):
            r = asyncio.run(cert_ops.timer_status())
        self.assertEqual(r["status"], "missing")
        self.assertIn("certbot", r["detail"])

    def test_unknown_when_nothing_can_be_checked(self):
        """无 systemd 又没有 cron 的机器：如实 unknown，不能把"查不了"说成"没配置"。"""
        with mock.patch.object(cert_ops, "run", _fake_run({})), \
             mock.patch.object(cert_ops, "_CRON_DIRS", ["/nonexistent-cron-dir"]), \
             mock.patch.object(cert_ops, "_TIMER_UNIT_DIRS", []):
            r = asyncio.run(cert_ops.timer_status())
        self.assertEqual(r["status"], "unknown")

    def test_hint_does_not_claim_managed_renewal_when_missing(self):
        certs = [{"name": "a.example.com", "status": "expiring", "days_left": 3}]
        hint = cert_ops.renewal_hint({"status": "missing"}, certs)
        self.assertIn("a.example.com", hint)
        self.assertNotIn("已安装", hint)
        self.assertIn("已安装", cert_ops.renewal_hint({"status": "installed"}, certs))


class TestSitesListing(unittest.TestCase):
    def test_joins_domain_to_app(self):
        conf = _mkdtemp("cp_conf_")
        domain = f"demo{os.getpid()}.choyeon.cc"
        orphan = f"orphan{os.getpid()}.choyeon.cc"
        Path(conf, "panel-demo.conf").write_text(_conf_text(PANEL_CONF, domain, 3000))
        Path(conf, "manual.conf").write_text(_conf_text(MANUAL_CONF, orphan, 9099))
        name, app_id = _mk_app(domain)
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))

        async def _states(units):
            return {u: {"active": True, "mainPid": 42} for u in units}

        with mock.patch.object(config, "NGINX_CONF_DIRS", [conf]), \
             mock.patch.object(sites.systemd_ops, "unit_states", _states):
            r = asyncio.run(sites.sites_list())
        rows = {x["domain"]: x for x in r["sites"]}
        self.assertIsNone(r["reason"])
        hit = rows[domain]
        self.assertEqual(hit["app"], name)
        self.assertEqual(hit["app_id"], app_id)
        self.assertEqual(hit["port"], 3000)
        self.assertTrue(hit["https"])
        self.assertTrue(hit["redirect"])
        self.assertTrue(hit["websocket"])
        self.assertEqual(hit["file"], "panel-demo.conf")
        self.assertTrue(hit["active"])
        # 未纳管的 nginx 配置也要露出来，但归属信息必须留空，而不是错挂到别的应用
        self.assertIsNone(rows[orphan]["app"])
        self.assertIsNone(rows[orphan]["active"])
        self.assertEqual(rows[orphan]["port"], 9099)

    def test_returns_empty_with_reason_when_nginx_missing(self):
        with mock.patch.object(config, "NGINX_CONF_DIRS", ["/nonexistent-nginx-conf-dir"]):
            r = asyncio.run(sites.sites_list())
        self.assertEqual(r["sites"], [])
        self.assertTrue(r["reason"])
        self.assertIn("nginx", r["reason"])

    def test_existing_dir_without_files_is_a_plain_empty_list(self):
        """目录在、里面没配置 → 是"确实没有站点"，不该报 nginx 缺失。"""
        conf = _mkdtemp("cp_conf_")
        with mock.patch.object(config, "NGINX_CONF_DIRS", [conf]), \
             mock.patch.object(sites.systemd_ops, "unit_states", _empty_states):
            r = asyncio.run(sites.sites_list())
        self.assertEqual(r["sites"], [])
        self.assertIsNone(r["reason"])


async def _empty_states(units):
    return {}


class TestRenewEndpoint(unittest.TestCase):
    def test_viewer_is_forbidden(self):
        r = asyncio.run(sites.ssl_renew("a.example.com", _req(role="viewer")))
        self.assertEqual(r.status_code, 403)

    def test_apply_and_remove_are_admin_only(self):
        for fn in (sites.site_apply, sites.site_remove):
            r = asyncio.run(fn(1, _req(role="viewer")))
            self.assertEqual(r.status_code, 403, fn.__name__)

    def test_config_read_is_admin_only(self):
        r = asyncio.run(sites.site_config("a.example.com", _req(role="viewer")))
        self.assertEqual(r.status_code, 403)

    def test_bad_cert_name_rejected(self):
        r = asyncio.run(sites.ssl_renew("../../etc/passwd", _req()))
        self.assertEqual(r.status_code, 400)

    def test_error_body_has_no_absolute_paths(self):
        """certbot/nginx 的报错原文满是 /etc/... 全路径，接口不能原样回显。"""

        async def _boom():
            raise RuntimeError('nginx: [emerg] open() "/etc/nginx/conf.d/panel-demo.conf" failed '
                               "(2: No such file or directory) in /etc/nginx/nginx.conf:20")

        with mock.patch.object(sites.nginx_ops, "renew_certs", _boom):
            r = asyncio.run(sites.ssl_renew("a.example.com", _req()))
        self.assertEqual(r.status_code, 400)
        body = r.body.decode()
        self.assertIsNone(ABS_PATH_RE.search(body), body)
        # 错误仍然可诊断：文件名与 emerg 原因留着
        self.assertIn("panel-demo.conf", body)
        self.assertIn("校验", body)

    def test_success_masks_paths_and_audits_only_the_name(self):
        async def _ok():
            return "Certificate not yet due for renewal; log at /var/log/letsencrypt/letsencrypt.log"

        with mock.patch.object(sites.nginx_ops, "renew_certs", _ok), \
             mock.patch.object(sites.aio_db, "audit") as audit_stub:
            r = asyncio.run(sites.ssl_renew("a.example.com", _req(sub="renew_tester")))
        self.assertEqual(r["ok"], True)
        self.assertIsNone(ABS_PATH_RE.search(r["log"]))
        audit_stub.assert_called_once_with("renew_tester", "site:renew", "a.example.com")


class TestVhostDelegation(unittest.TestCase):
    def test_apply_delegates_and_invalidates_cache(self):
        domain = f"apply{os.getpid()}.choyeon.cc"
        name, app_id = _mk_app(domain)
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        seen = {}

        async def _apply(spec):
            seen.update(spec)

        with mock.patch.object(sites.nginx_ops, "apply_vhost", _apply), \
             mock.patch.object(sites.nginx_ops, "invalidate_domain_cache") as inv:
            r = asyncio.run(sites.site_apply(app_id, _req()))
        self.assertEqual(r["ok"], True, r)
        self.assertEqual(seen, {"name": name, "domain": domain, "port": 3000})
        inv.assert_called()

    def test_apply_reports_nginx_failure_without_path(self):
        _, app_id = _mk_app(f"bad{os.getpid()}.choyeon.cc")
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))

        async def _fail(spec):
            raise RuntimeError("nginx config test failed, rolled back: /etc/nginx/conf.d/panel-bad.conf:5 bad")

        with mock.patch.object(sites.nginx_ops, "apply_vhost", _fail), \
             mock.patch.object(sites.nginx_ops, "invalidate_domain_cache"):
            r = asyncio.run(sites.site_apply(app_id, _req()))
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(ABS_PATH_RE.search(r.body.decode()), r.body.decode())

    def test_apply_without_domain_is_refused_before_touching_nginx(self):
        _, app_id = _mk_app(None)
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        with mock.patch.object(sites.nginx_ops, "apply_vhost") as ap:
            r = asyncio.run(sites.site_apply(app_id, _req()))
        self.assertEqual(r.status_code, 400)
        ap.assert_not_called()

    def test_remove_reports_whether_a_vhost_was_there(self):
        _, app_id = _mk_app(f"rm{os.getpid()}.choyeon.cc")
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))

        async def _rm(name):
            pass

        with mock.patch.object(sites.nginx_ops, "remove_vhost", _rm), \
             mock.patch.object(sites.nginx_ops, "vhost_path", lambda name: "/nonexistent-dir/x.conf"), \
             mock.patch.object(sites.nginx_ops, "invalidate_domain_cache"):
            r = asyncio.run(sites.site_remove(app_id, _req()))
        self.assertEqual(r["ok"], True)
        self.assertFalse(r["removed"])


class TestSiteConfigRead(unittest.TestCase):
    def test_returns_basename_and_content(self):
        conf = _mkdtemp("cp_cfg_")
        domain = f"cfg{os.getpid()}.choyeon.cc"
        Path(conf, "panel-cfg.conf").write_text(_conf_text(MANUAL_CONF, domain, 4000))
        with mock.patch.object(config, "NGINX_CONF_DIRS", [conf]):
            r = asyncio.run(sites.site_config(domain, _req()))
        self.assertEqual(r["file"], "panel-cfg.conf")
        self.assertNotIn("/", r["file"])
        self.assertNotIn(conf, str(r))

    def test_unknown_domain_404(self):
        conf = _mkdtemp("cp_cfg_")
        with mock.patch.object(config, "NGINX_CONF_DIRS", [conf]):
            r = asyncio.run(sites.site_config("nothing-here.example.com", _req()))
        self.assertEqual(r.status_code, 404)


class TestPathMasking(unittest.TestCase):
    def test_basename_only_and_credentials(self):
        raw = 'cloning https://user:tok@example.com/x.git into /root/www/app failed'
        out = sites._cmd_error(RuntimeError(raw))
        self.assertIsNone(ABS_PATH_RE.search(out))
        self.assertNotIn("tok", out)
        self.assertIn("example.com", out)


def tearDownModule():
    for d in _TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
