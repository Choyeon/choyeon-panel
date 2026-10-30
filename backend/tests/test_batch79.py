"""批次79：面板入口域名的读法（三态）与守卫式切换。

这里盯的是几条「改坏了就从公网进不去面板」的边界：

- 只改 server_name / certbot live 证书路径 / `if ($host = …)`，proxy_pass 与手工指令原样保留；
- 新域名没证书、证书不是 certbot 管的、域名已被别的文件占用，一律拒绝并给出可复制的处置命令；
- 写入后 nginx -t / reload / 回环 HTTPS 复核任一步不过，盘上那份必须回到改动前，
  而不是留下「配置指向新域名、证书还是旧的」半死入口；
- 读不到 / 盘上已没有那份文件时不猜，直接拒绝。
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_79_ut_"))

from app import config  # noqa: E402
from app.services import nginx_ops, panel_entry_service  # noqa: E402

OLD = "admin.example.com"
NEW = "panel.example.com"

CONF_TEXT = f"""\
server {{
    listen 443 ssl;
    server_name {OLD};
    ssl_certificate {config.TLS_DIR}/{OLD}/fullchain.pem;
    ssl_certificate_key {config.TLS_DIR}/{OLD}/privkey.pem;
    client_max_body_size 50m;
    location / {{
        proxy_pass http://127.0.0.1:{config.PORT};
        proxy_set_header Host $host;
    }}
    # 老域名留档：server_name {OLD};
}}
server {{
    listen 80;
    if ($host = {OLD}) {{
        return 301 https://$host$request_uri;
    }}
    server_name {OLD};
    return 404;
}}
"""


class _Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_79_conf_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "admin.example.com")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(CONF_TEXT)
        self.entry = {
            "file": self.path,
            "name": "admin.example.com",
            "analysis": nginx_ops.analyze_config(CONF_TEXT),
            "blocks": [],
        }
        self._patch("loaded_configs", lambda: [self.entry])
        self._patch_async("loaded_conf_paths", [self.path])
        self._patch("config_dirs_present", lambda: True)
        self._patch("invalidate_domain_cache", lambda: None)
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", config.NGINX_CONF_DIRS)

    def _patch(self, name, value):
        p = mock.patch.object(nginx_ops, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _patch_own(self, name, value):
        p = mock.patch.object(panel_entry_service, name, value)
        p.start()
        self.addCleanup(p.stop)

    def _patch_async(self, name, value):
        async def _fn(*a, **kw):
            return value

        self._patch(name, _fn)

    def _fake_run(self, curl_code=0, reload_code=0, test_code=0):
        calls = []

        async def _run(cmd, args, cwd=None, timeout=None, env=None):
            calls.append((cmd, list(args)))
            if cmd == "nginx":
                return {"code": test_code, "out": "nginx: configuration file test failed" if test_code else ""}
            if cmd == "systemctl":
                return {"code": reload_code, "out": "Job failed" if reload_code else ""}
            return {"code": curl_code, "out": "200" if curl_code == 0 else ""}

        p = mock.patch.object(panel_entry_service, "run", _run)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def _patch_cert(self, lineage):
        async def _fn(domain):
            return lineage

        self._patch("cert_lineage", _fn)

    def _patch_owner(self, owner):
        async def _fn(domain, target):
            return owner

        self._patch("domain_owner", _fn)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    # ---- 改写只碰三样 ----

    def test_rewrite_touches_only_names_cert_paths_and_host_guard(self):
        out = panel_entry_service.rewrite_names(CONF_TEXT, NEW)
        self.assertIn(f"server_name {NEW};", out)
        self.assertIn(f"{config.TLS_DIR}/{NEW}/fullchain.pem", out)
        self.assertIn(f"{config.TLS_DIR}/{NEW}/privkey.pem", out)
        self.assertIn(f"if ($host = {NEW})", out)
        # 不该动的东西一个都没动
        self.assertIn(f"proxy_pass http://127.0.0.1:{config.PORT};", out)
        self.assertIn("client_max_body_size 50m;", out)
        self.assertIn("proxy_set_header Host $host;", out)
        self.assertIn(f"# 老域名留档：server_name {OLD};", out)
        self.assertNotIn(OLD, out.replace(f"# 老域名留档：server_name {OLD};", ""))

    def test_ssl_state_refuses_non_certbot_and_ambiguous(self):
        has, lineage, why = panel_entry_service._ssl_state(CONF_TEXT)
        self.assertTrue(has)
        self.assertEqual(lineage, OLD)
        self.assertEqual(why, "")

        manual = "server {\n listen 443 ssl;\n ssl_certificate /etc/ssl/certs/x.pem;\n}\n"
        has, lineage, why = panel_entry_service._ssl_state(manual)
        self.assertTrue(has)
        self.assertIsNone(lineage)
        self.assertIn("live 目录", why)

        two = (f"server {{\n listen 443 ssl;\n ssl_certificate {config.TLS_DIR}/a/fullchain.pem;\n"
               f" ssl_certificate {config.TLS_DIR}/b/fullchain.pem;\n}}\n")
        has, lineage, why = panel_entry_service._ssl_state(two)
        self.assertTrue(has)
        self.assertIsNone(lineage)
        self.assertIn("不止一张证书", why)

    # ---- 读现状：读不到不许说成「没有入口」 ----

    def test_detail_says_unreadable_when_no_conf_dirs(self):
        self._patch("config_dirs_present", lambda: False)
        d = asyncio.run(panel_entry_service.detail())
        self.assertEqual(d["unreadable"], True)
        self.assertIn("nginx 配置目录", d["reason"])
        self.assertEqual(d["entries"], [])

    def test_detail_says_unreadable_when_nginx_dash_t_fails(self):
        self._patch_async("loaded_conf_paths", None)
        d = asyncio.run(panel_entry_service.detail())
        self.assertEqual(d["unreadable"], True)
        self.assertIn("nginx -T", d["reason"])

    def test_detail_reads_domain_and_notes(self):
        d = asyncio.run(panel_entry_service.detail())
        self.assertFalse(d["unreadable"])
        self.assertEqual(d["domain"], OLD)
        self.assertTrue(d["changeable"])
        self.assertEqual(d["entries"][0]["lineage"], OLD)
        self.assertEqual(d["notes"], [])

    def test_detail_marks_missing_entry_file_not_changeable(self):
        os.unlink(self.path)
        d = asyncio.run(panel_entry_service.detail())
        self.assertEqual(d["entries"][0]["state"], "missing")
        self.assertFalse(d["changeable"])
        self.assertTrue(any("读不动" in n for n in d["notes"]))

    def test_detail_without_panel_entry_is_not_unreadable(self):
        self._patch("loaded_configs", lambda: [])
        d = asyncio.run(panel_entry_service.detail())
        self.assertFalse(d["unreadable"])
        self.assertEqual(d["domain"], "")
        self.assertEqual(d["entries"], [])
        self.assertIn("反代到面板端口", d["reason"])

    # ---- 切换：拒绝路径 ----

    def test_change_refuses_invalid_domain(self):
        with self.assertRaises(panel_entry_service.PanelEntryError):
            asyncio.run(panel_entry_service.change_domain("not a domain"))

    def test_change_refuses_without_panel_entry(self):
        self._patch("loaded_configs", lambda: [])
        with self.assertRaises(panel_entry_service.PanelEntryError) as e:
            asyncio.run(panel_entry_service.change_domain(NEW))
        self.assertIn("反代到面板端口", str(e.exception))

    def test_change_refuses_when_entry_file_missing(self):
        os.unlink(self.path)
        with self.assertRaises(panel_entry_service.PanelEntryError) as e:
            asyncio.run(panel_entry_service.change_domain(NEW))
        self.assertIn("读不动", str(e.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_change_refuses_when_cert_missing(self):
        self._patch_cert(None)
        async def _mail():
            return "ops@example.com"

        self._patch_own("_cert_email", _mail)
        with self.assertRaises(panel_entry_service.PanelEntryError) as e:
            asyncio.run(panel_entry_service.change_domain(NEW))
        msg = str(e.exception)
        self.assertIn("还没有证书", msg)
        self.assertIn(f"certbot --nginx -d {NEW} --redirect -m ops@example.com", msg)

    def test_change_refuses_when_domain_owned_elsewhere(self):
        self._patch_cert(NEW)
        self._patch_owner("/etc/nginx/conf.d/other.conf")
        with self.assertRaises(panel_entry_service.PanelEntryError) as e:
            asyncio.run(panel_entry_service.change_domain(NEW))
        self.assertIn("other.conf", str(e.exception))
        self.assertEqual(self._read(), CONF_TEXT)

    def test_change_is_noop_for_same_domain(self):
        calls = self._fake_run()
        res = asyncio.run(panel_entry_service.change_domain(OLD))
        self.assertFalse(res["changed"])
        self.assertEqual(calls, [])
        self.assertEqual(self._read(), CONF_TEXT)

    # ---- 切换：成功与回滚 ----

    def test_change_writes_then_probes(self):
        self._patch_cert(NEW)
        self._patch_owner(None)
        calls = self._fake_run()
        res = asyncio.run(panel_entry_service.change_domain(NEW.upper()))
        self.assertTrue(res["changed"])
        self.assertEqual(res["domain"], NEW)
        self.assertEqual(res["oldDomains"], [OLD])
        self.assertTrue(res["verified"]["probe"]["ok"])
        self.assertIn(NEW, self._read())
        # 注释里留档的老域名原样保留，只有真正生效的 server_name 被换掉
        self.assertEqual(self._read().count(f"server_name {OLD};"), 1)
        self.assertEqual([c[0] for c in calls], ["nginx", "systemctl", "curl"])
        self.assertTrue(any("回环复核绕过了 DNS" in n for n in res["notes"]))

    def test_change_rolls_back_when_reload_fails(self):
        self._patch_cert(NEW)
        self._patch_owner(None)
        calls = self._fake_run(reload_code=1)
        with self.assertRaises(panel_entry_service.PanelEntryError) as e:
            asyncio.run(panel_entry_service.change_domain(NEW))
        self.assertIn("已回滚", str(e.exception))
        self.assertEqual(self._read(), CONF_TEXT)
        self.assertEqual([c for c in calls if c[0] == "systemctl"], [("systemctl", ["reload", "nginx"])] * 2)

    def test_change_rolls_back_when_probe_fails(self):
        self._patch_cert(NEW)
        self._patch_owner(None)
        self._fake_run(curl_code=60)
        with self.assertRaises(panel_entry_service.PanelEntryError) as e:
            asyncio.run(panel_entry_service.change_domain(NEW))
        self.assertIn("已回滚", str(e.exception))
        self.assertEqual(self._read(), CONF_TEXT)


if __name__ == "__main__":
    unittest.main()
