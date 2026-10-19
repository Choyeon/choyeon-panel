"""批次 60A：端口上的「默认 server」是一件全站性的事，得看得见。

起因是线上的一次真实错向。为了把 `video.choyeon.cc` 从旧的默认档位置上摘掉，
删掉了它 `listen 80 default_server` 里的 `default_server` —— 但那不是"取消默认档"，
而是把默认档让给了**加载顺序里的下一个块**（Debian 的 nginx.conf 先 include conf.d/*.conf，
再 include sites-enabled/*）。下一个恰好是刚给新站生成的 `conf.d/panel-xxx.conf`，
它写着 `return 301 https://$host$request_uri`。于是从那一刻起：未知 Host 的请求不再 404，
而是被 301 到"请求方自己带来的那个域名"，而面板、站点列表、自检三处全都没有一句话。

所以这一批做四件事，用例就盯着这四件：

- **默认档成为一个事实**：按 nginx -T 报出的加载顺序算出每个端口上谁接住未知 Host，
  是显式 default_server 还是隐式顶上的，server_name 是具名还是 `_`，跳转是不是反射 Host。
  反射 Host 判要紧（自检点红），具名顶替判值得处理，`server_name _` 那种是明摆着的选择，不写判词。
- **面板自己生成的配置不反射 $host**：跳转目标写死成这个块自己的域名。
- **域名大小写一个口径**：读侧折小写（nginx 匹配前本来就把 Host 转小写），比较点也折小写，
  否则会出现「站点页看得见这个域名、查看配置 404」与「apply 时认不出别处已定义、
  写出第二份互相遮蔽」。
- **写完即失效的是同一批缓存**：nginx 配置变了，域名映射、-T 集合与站点链路对账一起作废。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch60a_data_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app.services import nginx_ops  # noqa: E402
from app.services import site_audit_service as SA  # noqa: E402
from app.severity import SEV_RISK, SEV_WARN  # noqa: E402

TMP_DIRS: list[str] = []


def tearDownModule():
    for d in TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    TMP_DIRS.clear()


def _dir() -> str:
    """线上那套布局：conf.d + sites-available/sites-enabled，返回 conf.d。"""
    root = tempfile.mkdtemp(prefix="cp60a_conf_")
    TMP_DIRS.append(root)
    for sub in ("conf.d", "sites-available", "sites-enabled"):
        Path(root, sub).mkdir()
    return f"{root}/conf.d"


def _block(**kw) -> str:
    """一段最小的 server 块，参数按 nginx 的原样写法拼。"""
    lines = (kw.get("listen") or ["listen 80;"]) + (kw.get("names") or ["server_name a.example.com;"])
    lines += kw.get("body") or []
    inner = "\n".join(f"    {x}" for x in lines)
    return f"server {{\n{inner}\n}}\n"


def _coro(value):
    """给被 mock 掉的 async 函数当替身：调用一次就把 value 交回去。"""
    async def inner(*_a, **_kw):
        return value
    return inner


def _row(path: str, text: str) -> tuple[str, str]:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    return path, text


class ListenDetail(unittest.TestCase):
    def test_default_server_and_wildcard_are_separate_facts(self):
        blk = _block(listen=["listen 80 default_server;", "listen [::]:80;"])
        b = nginx_ops.parse_blocks(blk)[0]
        self.assertEqual(b["listenPorts"], [80])
        self.assertEqual(b["wildcardPorts"], [80])
        self.assertEqual(b["defaultPorts"], [80])

    def test_address_bound_listen_is_not_a_default_candidate(self):
        """`listen 127.0.0.1:80 default_server` 只对回环生效，拿来回答"外网未知 Host 归谁"会判错。"""
        b = nginx_ops.parse_blocks(_block(listen=["listen 127.0.0.1:80 default_server;"]))[0]
        self.assertEqual(b["listenPorts"], [80])
        self.assertEqual(b["wildcardPorts"], [])
        self.assertEqual(b["defaultPorts"], [])

    def test_http2_forms_still_parse(self):
        b = nginx_ops.parse_blocks(_block(listen=["listen 443 ssl http2;", "listen [::]:443 ssl;",
                                                 "listen 80;"]))[0]
        self.assertEqual(b["listenPorts"], [80, 443])
        self.assertEqual(b["wildcardPorts"], [80, 443])


class NamesAreNormalized(unittest.TestCase):
    def test_server_name_is_lowercased(self):
        """nginx 匹配前把 Host 转小写：写成 `Video.Example.COM` 的块永远按名字匹配不上。

        读侧不跟着折，就会出现「站点页与纳管登记表（都小写）看得见这个域名，
        而查看配置与域名冲突判断看不见它」。
        """
        b = nginx_ops.parse_blocks(_block(names=["server_name Video.Example.COM other.Example.org;"]))[0]
        self.assertEqual(b["serverNames"], ["video.example.com", "other.example.org"])

    def test_wildcard_name_is_kept_as_its_own_fact(self):
        """`server_name _` 不是域名，但"这个块明摆着要接住所有 Host"是一类必须留下的事实。"""
        b = nginx_ops.parse_blocks(_block(listen=["listen 80 default_server;"], names=["server_name _;"]))[0]
        self.assertEqual(b["serverNames"], [])
        self.assertTrue(b["catchAll"])

    def test_find_app_configs_ignores_case(self):
        d = _dir()
        Path(d, "case.conf").write_text(_block(names=["server_name Mixed.Example.COM;"]), encoding="utf-8")
        with mock.patch.object(nginx_ops.config, "NGINX_CONF_DIRS", [d]):
            self.assertEqual([h["name"] for h in nginx_ops.find_app_configs(None, "mixed.example.com")],
                             ["case.conf"])
            self.assertEqual([h["name"] for h in nginx_ops.find_app_configs(None, "MIXED.Example.COM")],
                             ["case.conf"])

    def test_domain_owner_ignores_case(self):
        """冲突判断漏掉大小写，就会给同一个域名写出第二份配置，然后按加载顺序互相遮蔽。"""
        d = _dir()
        other = _row(f"{d}/case.conf", _block(names=["server_name Mixed.Example.COM;"]))[0]
        real = os.path.realpath(other)

        async def scenario():
            with mock.patch.object(nginx_ops, "loaded_conf_paths", _coro([real])):
                return await nginx_ops.domain_owner("MIXED.example.com", f"{d}/panel-new.conf")

        self.assertEqual(asyncio.run(scenario()), real)

    def test_cert_lineage_ignores_case(self):
        """SAN 写成 `Blog.Example.com` 的证书也要认得出覆盖了这个域名：
        判成"没证书"会让 vhost 降级成明文，比误判成有证书严重得多。"""
        async def scenario():
            idx = {"blog.example.com": "blog.example.com"}
            with mock.patch.object(nginx_ops, "cert_index", _coro(idx)):
                return await nginx_ops.cert_lineage("Blog.Example.COM")

        self.assertEqual(asyncio.run(scenario()), "blog.example.com")


class HostReflection(unittest.TestCase):
    def test_literal_host_is_not_reflection(self):
        blk = _block(body=["return 301 https://a.example.com$request_uri;"])
        self.assertFalse(nginx_ops.parse_blocks(blk)[0]["hostReflect"])

    def test_dollar_host_is_reflection(self):
        for line in ("return 301 https://$host$request_uri;", "return 301 http://$host:8080/x;"):
            self.assertTrue(nginx_ops.parse_blocks(_block(body=[line]))[0]["hostReflect"], line)

    def test_other_variable_names_are_not_matched(self):
        """认的是「Host 位是 $host」这件事，不是「Host 位有 `$`」：`$hostname` 不算。"""
        blk = _block(body=["return 301 https://$hostname$request_uri;"])
        self.assertFalse(nginx_ops.parse_blocks(blk)[0]["hostReflect"])


class DefaultServerPick(unittest.TestCase):
    def test_first_in_load_order_wins_when_nobody_declares_default(self):
        rows = [
            ("/etc/nginx/conf.d/panel-music.conf",
             _block(listen=["listen 80;"], names=["server_name music.example.com;"])),
            ("/etc/nginx/sites-available/other",
             _block(listen=["listen 80;"], names=["server_name other.example.com;"])),
        ]
        out = nginx_ops.pick_default_servers(rows)
        self.assertEqual([r["file"] for r in out["ports"]], ["panel-music.conf"])
        self.assertFalse(out["ports"][0]["explicit"])
        self.assertEqual(out["ports"][0]["domains"], ["music.example.com"])

    def test_explicit_default_wins_even_if_it_loads_later(self):
        rows = [
            ("a.conf", _block(listen=["listen 80;"], names=["server_name a.example.com;"])),
            ("b.conf", _block(listen=["listen 80 default_server;"], names=["server_name catch.example.com;"])),
        ]
        port80 = nginx_ops.pick_default_servers(rows)["ports"][0]
        self.assertEqual((port80["file"], port80["explicit"]), ("b.conf", True))

    def test_only_the_first_default_server_counts(self):
        """nginx 对第二个 default_server 只给一句警告，赢的还是先加载那一份。"""
        rows = [
            ("a.conf", _block(listen=["listen 80 default_server;"], names=["server_name a.example.com;"])),
            ("b.conf", _block(listen=["listen 80 default_server;"], names=["server_name b.example.com;"])),
        ]
        self.assertEqual(nginx_ops.pick_default_servers(rows)["ports"][0]["file"], "a.conf")

    def test_ports_are_judged_apart(self):
        rows = [("m.conf", _block(listen=["listen 80;", "listen 443 ssl;"],
                                 names=["server_name m.example.com;"],
                                 body=["ssl_certificate /x/y.pem;",
                                       "return 301 https://$host$request_uri;"]))]
        out = nginx_ops.pick_default_servers(rows)["ports"]
        self.assertEqual([r["port"] for r in out], [80, 443])
        self.assertTrue(all(r["hostReflect"] for r in out), "同一份文件里的反射跳转，两个口都算")
        self.assertTrue(out[1]["ssl"])

    def test_report_has_no_absolute_paths(self):
        """载荷只出文件名：/etc/nginx 下的绝对路径本身就是攻击面信息（站点页这条规则早就有）。"""
        d = _dir()
        rows = [_row(f"{d}/m.conf", _block(body=["return 301 https://$host$request_uri;"]))]
        out = nginx_ops.pick_default_servers(rows)
        self.assertNotIn(d, json.dumps(out, ensure_ascii=False))
        self.assertNotIn("path", out["ports"][0])


class DefaultServersFetch(unittest.TestCase):
    def test_unreadable_nginx_T_is_reported_as_unknown(self):
        async def scenario():
            with mock.patch.object(nginx_ops, "loaded_conf_paths", _coro(None)):
                return await nginx_ops.default_servers()

        out = asyncio.run(scenario())
        self.assertFalse(out["ok"])
        self.assertIn("读不到", out["reason"])

    def test_missing_files_are_skipped_not_fatal(self):
        d = _dir()
        good = _row(f"{d}/m.conf", _block(listen=["listen 80;"]))[0]
        gone = os.path.realpath(f"{d}/deleted.conf")

        async def scenario():
            with mock.patch.object(nginx_ops, "loaded_conf_paths", _coro([gone, good])):
                return await nginx_ops.default_servers()

        out = asyncio.run(scenario())
        self.assertTrue(out["ok"])
        self.assertEqual([r["file"] for r in out["ports"]], ["m.conf"])

    def test_the_order_the_caller_gives_is_the_order_that_wins(self):
        """谁在列表前面谁顶上来，与文件名顺序无关——列表本身取自 `nginx -T`（见 loaded_conf_paths）。"""
        rows = [("z.conf", _block(listen=["listen 80;"], names=["server_name z.example.com;"])),
                ("a.conf", _block(listen=["listen 80;"], names=["server_name a.example.com;"]))]
        out = nginx_ops.pick_default_servers(rows)
        self.assertEqual(out["ports"][0]["file"], "z.conf", "按给定顺序，不按名字排序")


class RenderedVhost(unittest.TestCase):
    def test_https_redirect_does_not_reflect_the_request_host(self):
        text = nginx_ops.render_vhost({"name": "vid", "domain": "video.example.com", "port": 3100},
                                       ssl_lineage="video.example.com", http2_new=True)
        head = text.split("location")[0]
        self.assertIn("return 301 https://video.example.com$request_uri;", head)
        self.assertNotIn("$host", head, "跳转块里不许再出现由请求方决定的 Host")

    def test_domain_is_rendered_lowercase(self):
        text = nginx_ops.render_vhost({"name": "vid", "domain": "Video.Example.COM", "port": 3100},
                                       ssl_lineage="video.example.com", http2_new=False)
        self.assertIn("server_name video.example.com;", text)
        self.assertIn("return 301 https://video.example.com$request_uri;", text)
        self.assertNotIn("Video.Example.COM", text)

    def test_plain_vhost_has_no_redirect_block(self):
        text = nginx_ops.render_vhost({"name": "vid", "domain": "video.example.com", "port": 3100})
        self.assertEqual(text.count("server {"), 1)


def _cfg(name, doms, targets):
    """与 loaded_configs() 同形的最小条目：链路判定只看 analysis 这一层。"""
    tl = [{"host": h, "port": pt} for h, pt in targets]
    return {"name": name, "file": f"/etc/nginx/conf.d/{name}",
            "analysis": {"serverNames": list(doms), "listenPorts": [80, 443], "ssl": True,
                         "proxyTargets": tl, "httpsRedirect": True,
                         "proxyPorts": [pt for _h, pt in targets]},
            "blocks": []}


EXP = {"ports": [{"port": 80}, {"port": 443}, {"port": 3600}], "ss_available": True, "ports_capped": False}
CERTS = {"certs": [{"domains": ["music.example.com"]}], "source": "certbot", "reason": ""}


def _default(port=80, file="panel-music.conf", domains=("music.example.com",), catch_all=False,
             reflect=False, ssl=False, explicit=False, ok=True, reason=""):
    """`nginx_ops.pick_default_servers` 出来的那一行，按需要拼出各种档位。"""
    return {"ok": ok, "reason": reason, "ports": [] if not ok else [{
        "port": port, "file": file, "domains": list(domains), "catchAll": catch_all,
        "hostReflect": reflect, "ssl": ssl, "explicit": explicit, "targets": ["127.0.0.1:3600"]}]}


def _built(dflt):
    return SA._build([_cfg("m.conf", ["music.example.com"], [("127.0.0.1", 3600)])],
                     EXP, CERTS, [], True, {}, dflt)


class AuditFindings(unittest.TestCase):
    """判定归 site_audit_service：页面与 CLI 只渲染同一份 findings。"""

    def test_reflecting_default_is_the_only_risk_tier(self):
        out = SA._default_findings(_default(reflect=True))
        self.assertEqual([f["severity"] for f in out], [SEV_RISK])
        self.assertIn("$host", out[0]["detail"])
        self.assertIn("music.example.com", out[0]["title"] + out[0]["detail"])

    def test_named_implicit_default_is_a_warn(self):
        out = SA._default_findings(_default())
        self.assertEqual([f["severity"] for f in out], [SEV_WARN])
        self.assertIn("default_server", out[0]["detail"])
        self.assertIn("加载顺序", out[0]["detail"])

    def test_ssl_default_says_what_happens_without_sni(self):
        out = SA._default_findings(_default(port=443, ssl=True))
        self.assertIn("SNI", out[0]["detail"])

    def test_catch_all_default_says_nothing(self):
        """`server_name _` 的默认档是明摆着的选择，不写判词。"""
        self.assertEqual(SA._default_findings(_default(catch_all=True, domains=(), ssl=True)), [])

    def test_explicit_default_still_gets_named(self):
        """显式写了 default_server 也一样要说清是谁——"隐式"只是它的一种成因。"""
        out = SA._default_findings(_default(explicit=True))
        self.assertIn("显式 default_server", out[0]["detail"])

    def test_unreadable_default_is_unconfirmed_not_clean(self):
        out = SA._default_findings(_default(ok=False, reason="nginx -T 这一轮读不到"))
        self.assertEqual([f["severity"] for f in out], [SEV_WARN])
        self.assertIn("没查", out[0]["detail"])

    def test_build_carries_defaults_and_doctor_turns_reflect_red(self):
        built = _built(_default(reflect=True))
        self.assertIn("default-reflect-80", [f["id"] for f in built["findings"]])
        self.assertEqual(built["defaults"]["ports"][0]["file"], "panel-music.conf")
        self.assertEqual(SA.doctor_item(built)["status"], "fail", "反射 Host 的默认档值得把自检点红")
        self.assertNotIn("/etc/nginx", json.dumps(built["defaults"], ensure_ascii=False))

    def test_a_clean_default_leaves_no_finding(self):
        built = _built(_default(catch_all=True, domains=(), file="00-default.conf"))
        self.assertEqual([f["id"] for f in built["findings"] if f["id"].startswith("default")], [])

    def test_no_configs_means_no_default_verdict(self):
        """一个 server 块都没有时不追加"默认档未确认"：nginx.ok 那条已经说清了同样的事。"""
        empty = SA._build([], EXP, {"certs": [], "source": "none", "reason": "没有证书"}, [], True, {},
                          _default(ok=False, reason="读不到"))
        self.assertNotIn("default-server-unknown", [f["id"] for f in empty["findings"]])


class CliText(unittest.TestCase):
    def test_default_line_names_the_port_and_its_owner(self):
        text = cli._site_audit_text(_built(_default(reflect=True)))
        self.assertIn("默认档（", text)
        self.assertIn("80→panel-music.conf(music.example.com, 隐式, 反射Host)", text)

    def test_explicit_default_line_drops_the_implicit_marker(self):
        text = cli._site_audit_text(_built(_default(explicit=True)))
        self.assertIn("80→panel-music.conf(music.example.com)", text)

    def test_unreadable_default_line_says_so(self):
        text = cli._site_audit_text(_built(_default(ok=False, reason="nginx -T 读不到")))
        self.assertIn("默认档：读不到", text)


class OneCachePerBatchOfFacts(unittest.TestCase):
    def test_nginx_write_invalidates_the_audit_cache_too(self):
        """nginx 写完必须连站点链路对账一起失效：同一批事实不许有两个新鲜度。

        漏掉这一处的表现是「套用完域名，站点页与自检还拿 60 秒前的配置说没事」，
        而 CLI 的纳管命令早就在动手后清它了——差的只是面板自己那两条写路径。
        """
        seen = []
        with mock.patch.object(SA, "reset_cache", lambda: seen.append(True)), \
             mock.patch.object(nginx_ops, "_domain_cache", (0, {})), \
             mock.patch.object(nginx_ops, "_loaded_cache", (0, ["/x"])):
            nginx_ops.invalidate_domain_cache()
            # 断言必须在 with 里面：出了那个上下文，mock 会把两个缓存**还原成进来前的值**
            # （单跑这个文件时那是 None，全量跑时是别的用例留下的真实缓存），
            # 在外面断言等于在测 mock 的还原逻辑，而不是测失效。
            self.assertIsNone(nginx_ops._domain_cache)
            self.assertIsNone(nginx_ops._loaded_cache)
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
