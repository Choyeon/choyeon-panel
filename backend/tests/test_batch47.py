"""批次㊼A：站点链路对账（site_audit_service + /api/site-audit + choyeonctl site-audit + 自检项）。

这一层的价值全在「join 的判定不许说谎」上，用例就盯着四件事：

- **只有本机看得见的目标才判得死**：`proxy_pass` 指向别的机器、或者监听表这一轮根本没读到
  （没有 ss、ss 失败、条目封顶）时，判词必须是「未确认」，不能折成「没人听」。把一次探测失败
  说成一个不存在的故障比漏报更糟——人会去查一个本来好好的地址。
- **要紧的才点红**：只有「反代目标没人听」（现在访问就是 502）让自检项 fail；证书不覆盖、
  明文不跳转、配置读不到都只到 warn。面板不替你 reload nginx，也不替你改 vhost。
- **一个事实一个出处**：配置走 `nginx_ops.loaded_configs()`、端口走 `exposure_service`、
  证书清单走 `cert_ops`、域名归属走 `site_claim_service.attribution()`，这里只判覆盖关系、
  不算到期天数，也不再自己按端口认第二次应用；数与点名同源（`tallied`）。
- **载荷只出文件名**：`/etc/nginx` 下的绝对路径不进浏览器——本机目录结构本身就是攻击面信息。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch47_data_"))

import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

import cli  # noqa: E402
from app import database as dbm  # noqa: E402
from app import main, security  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import nginx_ops  # noqa: E402
from app.services import site_audit_service as SA  # noqa: E402
from app.severity import SEV_INFO, SEV_RISK, SEV_WARN  # noqa: E402

ROOT = Path(cli.ROOT)


def _exp(ports=(80, 443, 3000), ss_ok=True, capped=False):
    return {"ports": [{"port": p} for p in ports], "ss_available": ss_ok, "ports_capped": capped}


def _certs(domains=("a.com",), source="certbot", reason=""):
    return {"certs": [{"domains": list(domains)}] if source != "none" else [],
            "source": source, "reason": reason}


def _cfg(name, *, doms=(), listen=(80, 443), ssl=True, targets=(), redirect=True):
    """一份「配置项 + 解析结果」。绝对路径故意留着，用来验证它不会进载荷。

    `blocks` 与 `analysis` 都给，与 `loaded_configs()` 的真实形状一致：链路判定看文件级聚合，
    纳管归属看 server 块的根路径反代。只给一份的话，另一层会静默拿到空数据。
    """
    proxy_ports = [t["port"] for t in targets if t.get("port")]
    analysis = {"serverNames": list(doms), "listenPorts": list(listen), "ssl": ssl,
                "proxyTargets": [dict(t) for t in targets], "httpsRedirect": redirect,
                "proxyPorts": proxy_ports}
    blocks = [{"serverNames": list(doms), "targets": [dict(t) for t in targets],
               "ports": proxy_ports, "redirectTarget": "",
               "defaultTarget": dict(targets[0]) if targets else None,
               "listenPorts": list(listen), "ssl": ssl, "websocket": False,
               "httpsRedirect": redirect}]
    return {"name": name, "file": f"/etc/nginx/conf.d/{name}",
            "analysis": analysis, "blocks": blocks}


def _dflt(ports=(), ok=True, reason=""):
    """默认档那一档的入参形状（由 `nginx_ops.default_servers()` 给出，见批次 60A）。

    这里默认「读到了，但这个端口集合没东西可判」，因为本模块的用例盯着原来的四段账；
    默认档自己的判定在 test_batch60a.py 里测，包括 ok=False 那条"读不到就说读不到"。
    """
    return {"ok": ok, "reason": reason, "ports": list(ports)}


def _build(cfgs, exp=None, certs=None, apps=(), present=True, reg=None, dflt=None):
    return SA._build(cfgs, _exp() if exp is None else exp,
                     _certs() if certs is None else certs, list(apps), present, reg,
                     _dflt() if dflt is None else dflt)


def _sev(payload, sev):
    return [f for f in payload["findings"] if f["severity"] == sev]


def _ids(payload, sev=None):
    src = _sev(payload, sev) if sev is not None else payload["findings"]
    return [f["id"] for f in src]


class DeadTargetIsTheOnlyRisk(unittest.TestCase):
    def test_loopback_port_without_listener_is_named_with_its_port(self):
        p = _build([_cfg("a.conf", doms=["shop.com"], targets=[{"host": "127.0.0.1", "port": 3100}])],
                   apps=[{"name": "shop", "port": 3100, "domain": "shop.com"}])
        risky = _sev(p, SEV_RISK)
        self.assertEqual(len(risky), 1, risky)
        self.assertIn("shop.com", risky[0]["title"])
        self.assertIn("3100", risky[0]["detail"])
        self.assertIn("3100", risky[0]["fix"])
        self.assertIn("nginx -t", risky[0]["detail"], "要说清为什么这三页都是绿的")
        self.assertEqual(p["links"][0]["dead"], [3100])
        self.assertIs(p["links"][0]["targets"][0]["listening"], False)

    def test_listening_target_is_clean(self):
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3000}])],
                   apps=[{"name": "api", "port": 3000, "domain": "a.com"}])
        self.assertEqual(p["findings"], [])
        self.assertEqual(p["counts"], {"risk": 0, "warn": 0, "info": 0})

    def test_remote_host_is_never_judged_dead(self):
        """指向别的机器：本机没有监听表可对照，判死会让人去查一个没错的地址。"""
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "10.0.0.9", "port": 8080}])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertEqual(_sev(p, SEV_RISK), [])
        self.assertIsNone(p["links"][0]["targets"][0]["listening"])
        self.assertIn("remote-targets", _ids(p, SEV_INFO))

    def test_target_without_port_is_left_alone(self):
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": None}])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertEqual(_sev(p, SEV_RISK), [])
        self.assertIsNone(p["links"][0]["targets"][0]["listening"])


class BlindListenTableDowngrades(unittest.TestCase):
    """探测失败必须整轮降为未确认，而不是变成一批「没人听」。"""

    def test_no_ss_is_unconfirmed_not_dead(self):
        cfgs = [_cfg("a.conf", doms=["a.com"], targets=[{"host": "localhost", "port": 3100}])]
        p = _build(cfgs, exp=_exp(ss_ok=False), apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertFalse(p["listen"]["ok"])
        self.assertEqual(_sev(p, SEV_RISK), [])
        self.assertIsNone(p["links"][0]["targets"][0]["listening"])
        self.assertEqual(_ids(p, SEV_WARN), ["listen-unknown"])
        self.assertIn("未确认", p["findings"][0]["title"])
        self.assertIn("不等于", p["findings"][0]["detail"], "沉默地少一条判词更糟，要明说这只是没查")

    def test_capped_table_is_also_blind(self):
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3100}])],
                   exp=_exp(capped=True), apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertFalse(p["listen"]["ok"])
        self.assertEqual(_sev(p, SEV_RISK), [])
        self.assertEqual(_ids(p, SEV_WARN), ["listen-unknown"])

    def test_blind_without_local_target_stays_silent(self):
        """本地一个反代目标都没有时，监听表读不到只是背景，不该凭空造一条判词。"""
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "10.0.0.9", "port": 80}])],
                   exp=_exp(ss_ok=False), apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertEqual(_ids(p, SEV_WARN), [])

    def test_listen_count_is_the_real_set(self):
        p = _build([_cfg("a.conf", doms=["a.com"])], exp=_exp(ports=(80, 443, 8080)))
        self.assertEqual(p["listen"]["ports"], 3)


class CertIsCoverageOnly(unittest.TestCase):
    def test_ssl_site_without_matching_cert_warns(self):
        p = _build([_cfg("a.conf", doms=["a.com", "www.a.com"])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}],
                   certs=_certs(domains=["b.com"]))
        self.assertEqual(_ids(p, SEV_WARN), ["cert-coverage"])
        self.assertEqual(p["links"][0]["uncovered"], ["a.com", "www.a.com"])

    def test_it_does_not_recount_expiry_days(self):
        """天数归证书项（同一个 alert_ssl_days）；这里再算一次就是两个出处，阈值一改就分叉。"""
        p = _build([_cfg("a.conf", doms=["a.com"])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}],
                   certs=_certs(domains=["b.com"]))
        f = next(x for x in p["findings"] if x["id"] == "cert-coverage")
        self.assertNotRegex(f["title"] + f["detail"] + f["fix"], r"\d+\s*天")
        self.assertIn("certbot certificates", f["fix"])

    def test_unreadable_cert_list_does_not_accuse(self):
        p = _build([_cfg("a.conf", doms=["a.com"])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}],
                   certs=_certs(source="none", reason="certbot 不在"))
        self.assertFalse(p["certs"]["ok"])
        self.assertEqual(_ids(p, SEV_WARN), ["cert-unknown"])

    def test_confirmed_empty_list_is_a_real_gap(self):
        """source 有值而清单为空 = 本机确实没有证书，这要照判，不能混进「读不到」。"""
        p = _build([_cfg("a.conf", doms=["a.com"])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}],
                   certs={"certs": [], "source": "openssl", "reason": ""})
        self.assertTrue(p["certs"]["ok"])
        self.assertIn("cert-coverage", _ids(p))

    def test_plain_http_site_is_not_checked_for_cert(self):
        p = _build([_cfg("a.conf", doms=["a.com"], ssl=False, listen=(80,))],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}],
                   certs=_certs(domains=["b.com"]))
        self.assertEqual(p["links"][0]["uncovered"], [])


class PlainHttpNeedsBothEnds(unittest.TestCase):
    def test_matrix(self):
        cases = (((80, 443), True, False), ((80, 443), False, True),
                 ((443,), False, False), ((80,), False, False))
        for listen, redirect, flagged in cases:
            p = _build([_cfg("a.conf", doms=["a.com"], listen=listen, redirect=redirect)],
                       apps=[{"name": "api", "port": None, "domain": "a.com"}])
            self.assertEqual("plain-http" in _ids(p), flagged, f"{listen} redirect={redirect}")

    def test_plain_site_without_ssl_is_not_reported(self):
        p = _build([_cfg("a.conf", doms=["a.com"], ssl=False, listen=(80,))],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertFalse(p["links"][0]["no_redirect"])


class AppClaimIsBestEffort(unittest.TestCase):
    def test_domain_first_then_root_port(self):
        """归属仍按「登记的域名优先，其次根路径反代端口」，但判定只出一份：纳管层。

        两个域名各自反代到同一个 3000，靠端口只能认出一个；另一个靠应用自己登记的域名认。
        """
        cfgs = [_cfg("a.conf", doms=["shop.com"], targets=[{"host": "127.0.0.1", "port": 3000}]),
                _cfg("b.conf", doms=["other.com"], targets=[{"host": "127.0.0.1", "port": 3000}])]
        p = _build(cfgs, apps=[{"id": 1, "name": "shop", "port": None, "domain": "shop.com"},
                               {"id": 2, "name": "api", "port": 3000, "domain": ""}])
        self.assertEqual([link["app"] for link in p["links"]], ["shop", "api"])

    def test_registry_beats_the_guess(self):
        """登记说了归谁，链路那一列就照登记走——哪怕判定按端口认出的是另一个应用。

        这条锁的是批次五七的立足点：共用端口与应用登记的端口跟配置对不上（3100 vs 3300）两种
        情况下，人的登记不能被机器的猜测盖掉。认领的文件名故意给另一份，一并验证那一列读的是
        登记里的应用，不会因为「文件对不上」就退回猜端口。
        """
        cfgs = [_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3000}])]
        apps = [{"id": 1, "name": "api", "port": 3000, "domain": ""},
                {"id": 2, "name": "legacy", "port": 3001, "domain": ""}]
        self.assertEqual(_build(cfgs, apps=apps)["links"][0]["app"], "api")
        p = _build(cfgs, apps=apps, reg={"a.com": {"domain": "a.com", "app_id": 2,
                                                   "file": "other.conf", "kind": "app",
                                                   "source": "manual", "note": "手工指定"}})
        self.assertEqual(p["links"][0]["app"], "legacy", "归属跟着登记走")

    def test_unmanaged_vhost_is_info_not_warn(self):
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3000}])],
                   apps=[])
        self.assertEqual(_ids(p), ["unmanaged"])
        self.assertEqual(p["findings"][0]["severity"], SEV_INFO)
        self.assertIn("site claim-all", p["findings"][0]["fix"])

    def test_db_failure_leaves_the_column_empty(self):
        """/api/site-audit 不许因为 apps 表读不到就整轮失败——认领那一列如实留空。"""
        with mock.patch.object(SA.site_claim_service, "app_rows",
                               mock.AsyncMock(side_effect=RuntimeError("locked"))):
            self.assertEqual(asyncio.run(SA._app_rows()), [])
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3000}])])
        self.assertIsNone(p["links"][0]["app"])


class DoctorItemFoldsHonest(unittest.TestCase):
    def test_unreadable_nginx_is_warn_never_pass(self):
        p = _build([], present=False)
        self.assertFalse(p["nginx"]["ok"])
        item = SA.doctor_item(p)
        self.assertEqual(item["status"], "warn")
        self.assertIn("choyeonctl site-audit", item["fix"])

    def test_empty_site_list_says_it_is_empty_not_unreadable(self):
        p = _build([], present=True)
        self.assertIn("一个 server 块都没有", p["nginx"]["reason"])

    def test_risk_fails_and_warn_warns(self):
        risky = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3100}])],
                       apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertEqual(SA.doctor_item(risky)["status"], "fail")
        warned = _build([_cfg("a.conf", doms=["a.com"])],
                        apps=[{"name": "api", "port": None, "domain": "a.com"}],
                        certs=_certs(domains=["b.com"]))
        self.assertEqual(SA.doctor_item(warned)["status"], "warn")

    def test_clean_pass_names_the_counts(self):
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3000}])],
                   apps=[{"name": "api", "port": 3000, "domain": "a.com"}])
        item = SA.doctor_item(p)
        self.assertEqual(item["status"], "pass")
        self.assertIn("1 份配置、1 个域名", item["detail"])

    def test_info_only_still_passes_but_says_it(self):
        p = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "10.0.0.9", "port": 8080}])],
                   apps=[{"name": "api", "port": None, "domain": "a.com"}])
        self.assertEqual(p["counts"]["risk"], 0)
        self.assertGreater(p["counts"]["info"], 0)
        item = SA.doctor_item(p)
        self.assertEqual(item["status"], "pass")
        self.assertIn(f"另有 {p['counts']['info']} 条只是让你知道", item["detail"])

    def test_the_number_and_the_names_come_from_one_list(self):
        """数与点名同源（批次㉔）：判词里的数字必须就是点出来的那几条。"""
        cfgs = [_cfg(f"{c}.conf", doms=[f"{c}.com"], targets=[{"host": "127.0.0.1", "port": 3100}])
                for c in "abc"]
        item = SA.doctor_item(_build(cfgs))
        self.assertEqual(item["status"], "fail")
        self.assertTrue(item["detail"].startswith("3 处站点链路要紧（另有 2 项）："), item["detail"])


class PayloadShape(unittest.TestCase):
    def test_no_absolute_paths_leave_the_server(self):
        p = _build([_cfg("a.conf", doms=["a.com"])])
        self.assertNotIn("/etc/nginx", json.dumps(p, ensure_ascii=False))
        self.assertEqual([link["file"] for link in p["links"]], ["a.conf"])

    def test_counts_and_findings_agree(self):
        cfgs = [_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3100}]),
                _cfg("b.conf", doms=["b.com"], redirect=False),
                _cfg("c.conf", doms=["c.com"], targets=[{"host": "10.0.0.9", "port": 9000}])]
        p = _build(cfgs, certs=_certs(domains=["a.com"]))
        self.assertEqual(sum(p["counts"].values()), len(p["findings"]))
        self.assertGreaterEqual(p["counts"]["risk"], 1)

    def test_limitation_notes_are_documented(self):
        joined = " ".join(_build([_cfg("a.conf", doms=["a.com"])])["notes"])
        self.assertEqual(len(_build([_cfg("a.conf", doms=["a.com"])])["notes"]), 4)
        for phrase in ("不 reload nginx", "到期天数以证书项为准", "标未确认", "加载顺序"):
            self.assertIn(phrase, joined)


class NginxParseFeedsTheJoin(unittest.TestCase):
    """判定用的字段必须由站点列表同一个读者产出：解析错，整层就错。"""

    def test_direct_proxy_pass_pair(self):
        a = nginx_ops.analyze_config(
            "server {\n listen 443 ssl;\n server_name a.com;\n"
            " proxy_pass http://127.0.0.1:3100;\n}\n")
        self.assertEqual(a["listenPorts"], [443])
        self.assertEqual(a["proxyTargets"], [{"host": "127.0.0.1", "port": 3100}])

    def test_upstream_expands_and_unreferenced_is_excluded(self):
        a = nginx_ops.analyze_config(
            "upstream used { server 127.0.0.1:3100; server 127.0.0.1:3101; }\n"
            "upstream draft { server 127.0.0.1:9999; }\n"
            "server {\n listen 80;\n proxy_pass http://used;\n}\n")
        self.assertEqual(a["proxyTargets"], [{"host": "127.0.0.1", "port": 3100},
                                             {"host": "127.0.0.1", "port": 3101}])
        self.assertNotIn(9999, a["proxyPorts"], "没被引用的 upstream 不该参与端口对账")

    def test_listen_line_shapes(self):
        a = nginx_ops.analyze_config(
            "server {\n listen 80 default_server;\n listen [::]:443 ssl;\n"
            " listen 8080;\n listen something-odd;\n}\n")
        self.assertEqual(a["listenPorts"], [80, 443, 8080])

    def test_host_without_port_stays_unknown(self):
        a = nginx_ops.analyze_config("server {\n proxy_pass http://backend.example.com;\n}\n")
        self.assertEqual(a["proxyTargets"], [{"host": "backend.example.com", "port": None}])


class SiteAuditApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.admin = f"b47a-{os.getpid()}"
        cls.viewer = f"b47v-{os.getpid()}"
        for name, role in ((cls.admin, "admin"), (cls.viewer, "viewer")):
            dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                        (name, security.hash_pass("Password123"), role))
        cls.hdr = {"Authorization": f"Bearer {security.sign_token(cls.admin, 'admin')}"}
        cls.vhdr = {"Authorization": f"Bearer {security.sign_token(cls.viewer, 'viewer')}"}

    @classmethod
    def tearDownClass(cls):
        for name in (cls.admin, cls.viewer):
            dbm.execute("DELETE FROM users WHERE username=?", (name,))

    def test_viewer_can_read_and_anonymous_cannot(self):
        payload = _build([_cfg("a.conf", doms=["a.com"])])
        with mock.patch.object(SA, "audit", mock.AsyncMock(return_value=payload)):
            r = self.client.get("/api/site-audit", headers=self.vhdr)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["nginx"]["files"], 1)
            self.assertEqual(self.client.get("/api/site-audit").status_code, 401)

    def test_refresh_flag_reaches_the_service(self):
        audit = mock.AsyncMock(return_value=_build([_cfg("a.conf", doms=["a.com"])]))
        with mock.patch.object(SA, "audit", audit):
            self.client.get("/api/site-audit?refresh=1", headers=self.hdr)
        self.assertEqual(audit.await_args.kwargs["refresh"], True)

    def test_registry_holds_the_item(self):
        # 这里只钉"注册表变大了、没有被替换掉"；确切项数（含文档口径）钉在批次69。
        self.assertGreaterEqual(len(meta._CHECKS), 21, "自检项只增不减")
        row = next(r for r in meta._CHECKS if r[0] == "sites")
        self.assertEqual(row[1], "站点链路")

    def test_check_sites_folds_the_doctor_item(self):
        risky = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3100}])])
        with mock.patch.object(SA, "audit", mock.AsyncMock(return_value=risky)):
            item = asyncio.run(dict((k, f) for k, _, f in meta._CHECKS)["sites"]())
        self.assertEqual(item["status"], "fail")
        self.assertIn("choyeonctl site-audit", item["fix"])


class CliContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._json_mode = cli.Out.json_mode

    @classmethod
    def tearDownClass(cls):
        cli.Out.json_mode = cls._json_mode

    def _run(self, argv):
        buf = io.StringIO()
        code = 0
        cli.Out.json_mode = True
        args = cli.build_parser().parse_args(argv)
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                asyncio.run(args.fn(args))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        finally:
            cli.Out.json_mode = self._json_mode
        out = buf.getvalue()
        return code, (json.loads(out) if out.strip().startswith("{") else {"raw": out})

    def test_parser_defaults_and_refresh(self):
        self.assertFalse(cli.build_parser().parse_args(["site-audit"]).refresh)
        self.assertTrue(cli.build_parser().parse_args(["site-audit", "--refresh"]).refresh)

    def test_schema_declares_the_readonly_command(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            schema = asyncio.run(cli.cmd_schema(cli.build_parser().parse_args(["schema"])))
        entry = next(e for e in schema["commands"] if e["name"] == "site-audit")
        self.assertIn("--refresh", entry["args"])
        self.assertIn("不 reload nginx", entry["desc"])
        self.assertTrue(any("退出码 3" in n for n in entry["notes"]), entry["notes"])

    def test_unreadable_nginx_is_exit_three(self):
        with mock.patch.object(SA, "audit", mock.AsyncMock(return_value=_build([], present=False))):
            code, d = self._run(["site-audit"])
        self.assertEqual(code, cli.EXIT_PRECONDITION, d)

    def test_human_text_shows_the_three_sources(self):
        d = _build([_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3100}])],
                   certs=_certs(source="none", reason="certbot 不在"))
        text = cli._site_audit_text(d)
        for phrase in ("站点链路对账", "配置：1 份", "监听表：3 个端口在听",
                       "证书：读不到（certbot 不在）", "3100", "(没人听)"):
            self.assertIn(phrase, text, phrase)

    def test_human_text_keeps_a_portless_proxy_target(self):
        """proxy_pass 没写端口（默认 80、命名上游、unix 套接字）照样是反代目标。

        渲染层过去按 `port is not None` 筛一遍，整行的目标串就空了，落回 "非反代" 那句——
        与站点页对同一份判定给出相反的话，而"这条 location 没在反代"恰好是误导人的那半句。
        """
        d = _build([_cfg("a.conf", doms=["a.com"],
                         targets=[{"host": "unix:/run/app.sock", "port": None}])])
        text = cli._site_audit_text(d)
        self.assertIn("(未写端口)", text)
        self.assertIn("(未确认)", text)
        self.assertNotIn("· 非反代", text)


class OneFactOneReader(unittest.TestCase):
    def test_reprobe_only_joins_existing_readers(self):
        """配置走 loaded_configs、端口走 exposure_service、证书走 cert_ops：不许自己再扫一遍。

        站点列表与站点链路对账若各扫一次目录，迟早会在「sites-available 里没启用的草稿算不算
        一个站点」上分叉，而分叉的两侧看着都对。
        """
        cfgs = [_cfg("a.conf", doms=["a.com"], targets=[{"host": "127.0.0.1", "port": 3000}])]
        loaded = mock.Mock(return_value=cfgs)
        present = mock.Mock(return_value=True)
        exp = mock.AsyncMock(return_value=_exp())
        certs = mock.AsyncMock(return_value=_certs())
        rows = mock.AsyncMock(return_value=[{"id": 1, "name": "api", "port": 3000, "domain": "a.com"}])
        claims = mock.AsyncMock(return_value={})
        # 默认档同样只走 nginx_ops 那一个读者（它自己去读 `nginx -T` 的加载顺序）。
        # 不 mock 掉的话，这一条用例的答案就取决于跑它的机器上 nginx 配了什么——
        # 本机绿、CI 红，而且红的原因是「这台机器真有一个反射 Host 的默认档」。
        dflt = mock.AsyncMock(return_value=_dflt())
        sc = SA.site_claim_service
        with mock.patch.object(SA.nginx_ops, "loaded_configs", loaded), \
             mock.patch.object(SA.nginx_ops, "config_dirs_present", present), \
             mock.patch.object(SA.nginx_ops, "default_servers", dflt), \
             mock.patch.object(SA.exposure_service, "audit", exp), \
             mock.patch.object(SA.cert_ops, "list_certs", certs), \
             mock.patch.object(sc, "app_rows", rows), \
             mock.patch.object(sc, "claims", claims):
            SA.reset_cache()
            data = asyncio.run(SA._reprobe())
        self.assertEqual(loaded.call_count, 1)
        self.assertEqual(present.call_count, 1)
        dflt.assert_awaited_once()
        exp.assert_awaited_once()
        certs.assert_awaited_once()
        claims.assert_awaited_once()
        self.assertEqual([link["file"] for link in data["links"]], ["a.conf"])
        self.assertEqual(data["links"][0]["app"], "api")
        self.assertEqual(data["findings"], [])


class DocsContract(unittest.TestCase):
    def test_agents_md_row_declares_readonly_and_exit_code(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `site-audit`"))
        for phrase in ("不 reload", "proxy_pass", "退出码 3"):
            self.assertIn(phrase, row)

    def test_readme_documents_the_command(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl site-audit", text)
        self.assertIn(f"共 {len(meta._CHECKS)} 项只读检查", text)

    def test_security_md_documents_the_command(self):
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("choyeonctl site-audit", text)
        self.assertIn(f"{len(meta._CHECKS)} 项只读自检", text)


if __name__ == "__main__":
    unittest.main()
