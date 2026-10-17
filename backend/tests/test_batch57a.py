"""批次五七A：站点纳管（site_claim_service + /api/sites 的纳管写接口 + choyeonctl site）。

这批改的是「域名归谁」这个问题的**出处**：过去由反代端口猜，两台应用共用一个端口、
或应用登记的端口与配置里写的对不上（这台机器上 choyeon-video 登记 3100、配置指 3300）
都会猜错，于是面板只能把所有手工 vhost 一律标成未纳管。现在归属由一张登记表说了算，
判定只作为「建议」出现。用例盯的就是这两件事不许再混：

- **判得出的按强度排序，判不出的必须给出可以直接展示的理由**：批量纳管不许替管理员猜一个应用。
- **纳管只动登记表**：nginx 的配置文件一个字节都不写、不 reload；取消纳管也一样。
- **登记表优先于判定**：人已指定的归属不能被下一轮自动判定盖掉。
- **派生态跟着对象走**：删应用要把它名下的登记一起清掉；配置已删而登记还在的行必须看得见。
- **一个事实一个出处**：站点页、概览条的站点计数与链路对账都读 `site_claim_service`，
  不再各扫一遍 nginx 目录、各按端口认一次应用。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch57a_data_"))

import unittest  # noqa: E402

import cli  # noqa: E402
from app import config  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers import sites  # noqa: E402
from app.services import apps_service, nginx_ops  # noqa: E402
from app.services import site_claim_service as SC  # noqa: E402

ABS_PATH_RE = re.compile(r"/(?:etc|root|usr|var|home|tmp)/")
TMP_DIRS: list[str] = []
PANEL_PORT = 3210


def tearDownModule():
    """本模块写的登记与临时 nginx 目录都要带走：登记表是全站共享的一张表。"""
    dbm.execute("DELETE FROM site_claims")
    for d in TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    TMP_DIRS.clear()


def _dir() -> str:
    d = tempfile.mkdtemp(prefix="cp57a_conf_")
    TMP_DIRS.append(d)
    return d


def _dom(prefix: str) -> str:
    """域名里带 pid：同机并行的测试进程不会撞键（apps.name 是 UNIQUE）。"""
    return f"{prefix}-{os.getpid()}.choyeon.cc"


def _name(prefix: str) -> str:
    return f"{prefix}-{os.getpid()}"


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# `{}` 与 str.format 打架，占位符一律用 __TOKEN__。
TWO_BLOCKS = """server {
    listen 80;
    server_name __DOMAIN__;
    return 301 https://__DOMAIN__$request_uri;
}

server {
    listen 443 ssl;
    server_name __DOMAIN__;
    ssl_certificate /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    location /api {
        proxy_pass http://127.0.0.1:__API__;
    }
    location / {
        proxy_pass http://127.0.0.1:__PORT__;
        proxy_set_header Upgrade $http_upgrade;
    }
}
"""

ROOT_ONLY = """server {
    listen 80;
    server_name __DOMAIN__;
    location / {
        proxy_pass http://127.0.0.1:__PORT__;
    }
}
"""

SUB_PATH_ONLY = """server {
    listen 80;
    server_name __DOMAIN__;
    location /health {
        proxy_pass http://127.0.0.1:__PORT__;
    }
}
"""

STATIC = """server {
    listen 443 ssl;
    server_name __DOMAIN__;
    ssl_certificate /etc/letsencrypt/live/__DOMAIN__/fullchain.pem;
    root /var/www/html;
}
"""

ALIAS = """server {
    listen 80;
    server_name __ALIAS__;
    return 301 https://__TARGET__$request_uri;
}
"""

REMOTE = """server {
    listen 80;
    server_name __DOMAIN__;
    location / {
        proxy_pass http://10.0.0.9:8080;
    }
}
"""


def _write(d: str, fname: str, tpl: str, **kw) -> None:
    text = tpl
    for k, v in kw.items():
        text = text.replace(f"__{k}__", str(v))
    Path(d, fname).write_text(text, encoding="utf-8")


def _facts(d: str) -> list[dict]:
    with mock.patch.object(config, "NGINX_CONF_DIRS", [d]):
        return nginx_ops.loaded_configs()


def _app(name, port=None, domain="", app_id=None) -> dict:
    return {"id": app_id, "name": name, "port": port, "domain": domain, "unit_override": ""}


def _judged(configs, apps, panel_port=PANEL_PORT):
    with mock.patch.object(config, "PORT", panel_port):
        return SC.judge(SC.domain_facts(configs), apps)


def _rows(configs, apps, reg, panel_port=PANEL_PORT):
    with mock.patch.object(config, "PORT", panel_port):
        return SC.build_rows(configs, apps, reg)


class TestSchema(unittest.TestCase):
    """登记表本身也要有回归：键、约束与 upsert 形状。"""

    def test_table_columns(self):
        cols = {r["name"] for r in dbm.query("PRAGMA table_info(site_claims)")}
        self.assertEqual(cols, {"domain", "app_id", "file", "kind", "source", "note", "created_at"})

    def test_kind_and_source_are_checked(self):
        """kind 只有 app/panel 两档：写成别的（比如 proxy）必须被表挡住，而不是存进去再靠代码判。"""
        for bad in ({"kind": "proxy", "source": "auto"}, {"kind": "app", "source": "guess"}):
            with self.assertRaises(Exception):
                dbm.execute(
                    "INSERT INTO site_claims(domain,app_id,file,kind,source) VALUES(?,?,?,?,?)",
                    (_dom("chk"), None, "", bad["kind"], bad["source"]),
                )
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM site_claims WHERE domain=?",
                                       (_dom("chk"),))["c"], 0, "两档 CHECK 都没挡住的话，这里会留下行")

    def test_domain_is_the_primary_key(self):
        """一个域名只能有一条归属：第二条裸 INSERT 必须被表挡住。"""
        d = _dom("pk")
        dbm.execute("INSERT INTO site_claims(domain,app_id,file,kind) VALUES(?,?,?,?)", (d, 1, "a.conf", "app"))
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE domain=?", (d,))
        with self.assertRaises(Exception):
            dbm.execute("INSERT INTO site_claims(domain,app_id,file,kind) VALUES(?,?,?,?)", (d, 2, "b.conf", "app"))
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM site_claims WHERE domain=?", (d,))["c"], 1)

    def test_register_upserts_in_place(self):
        """改归属走 _register 的 ON CONFLICT：换应用是改这一行，不是插出第二行。"""
        d = _dom("ups")
        asyncio.run(SC._register(d, "a.conf", SC.KIND_APP, 1, SC.SOURCE_AUTO, "第一次"))
        asyncio.run(SC._register(d, "b.conf", SC.KIND_APP, 2, SC.SOURCE_MANUAL, "第二次"))
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE domain=?", (d,))
        row = dbm.query_one("SELECT app_id,file,source,note FROM site_claims WHERE domain=?", (d,))
        self.assertEqual((row["app_id"], row["file"], row["source"], row["note"]),
                         (2, "b.conf", "manual", "第二次"))
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM site_claims WHERE domain=?", (d,))["c"], 1)


class TestDomainFacts(unittest.TestCase):
    def test_file_is_the_block_that_actually_serves_the_domain(self):
        """80 只做跳转、443 才反代：认领的文件与目标必须是真正服务它的那一块。"""
        d = _dir()
        dom = _dom("two")
        _write(d, "redir.conf", TWO_BLOCKS, DOMAIN=dom, PORT=3300, API=3000)
        f = SC.domain_facts(_facts(d))[dom]
        self.assertEqual(f["file"], "redir.conf")
        self.assertEqual(f["target"], {"host": "127.0.0.1", "port": 3300}, "归属只看 location / 的那一跳")
        self.assertTrue(f["https"] and f["redirect"] and f["websocket"])
        self.assertIsNone(f["redirect_host"], "跳去的就是这个域名自己，没有可跟随的归属")

    def test_case_is_normalised(self):
        """配置里写 `server_name Blog.Chooyeon.cc` 也要和登记表的小写域名对上。"""
        d = _dir()
        _write(d, "case.conf", ROOT_ONLY, DOMAIN="Case.Choeyeon.CC", PORT=3400)
        f = SC.domain_facts(_facts(d))
        self.assertIn("case.choeyeon.cc", f)
        self.assertNotIn("Case.Choeyeon.CC", f)

    def test_subpath_only_target_has_no_root_attribution(self):
        """只有 /health 反代的探测块：不许把它当成这个域名的主体去认领归属。"""
        d = _dir()
        dom = _dom("sub")
        _write(d, "sub.conf", SUB_PATH_ONLY, DOMAIN=dom, PORT=3500)
        self.assertIsNone(SC.domain_facts(_facts(d))[dom]["target"])

    def test_static_site_has_no_target(self):
        d = _dir()
        dom = _dom("static")
        _write(d, "static.conf", STATIC, DOMAIN=dom)
        f = SC.domain_facts(_facts(d))[dom]
        self.assertIsNone(f["target"])
        self.assertTrue(f["https"])


class TestJudgment(unittest.TestCase):
    """判定强度：面板入口 ＞ 应用登记的域名 ＞ 根路径端口 ＞ 配置文件名 ＞ 跟随 301。"""

    def test_panel_entry_is_its_own_kind(self):
        d = _dir()
        dom = _dom("panel")
        _write(d, "panel.conf", ROOT_ONLY, DOMAIN=dom, PORT=PANEL_PORT)
        v = _judged(_facts(d), [_app("api", PANEL_PORT, dom)])[dom]
        self.assertEqual(v["kind"], SC.KIND_PANEL)
        self.assertIsNone(v["app"], "面板入口不挂在任何应用上")
        self.assertIn("面板自己的入口", v["note"])

    def test_registered_domain_beats_port(self):
        d = _dir()
        dom = _dom("own")
        _write(d, "own.conf", TWO_BLOCKS, DOMAIN=dom, PORT=3300, API=3000)
        v = _judged(_facts(d), [_app("who", 3000, dom)])[dom]
        self.assertEqual((v["kind"], v["app"]), (SC.KIND_APP, "who"))
        self.assertIn("应用登记的域名", v["note"])

    def test_root_port_when_unique(self):
        d = _dir()
        dom = _dom("port")
        _write(d, "port.conf", ROOT_ONLY, DOMAIN=dom, PORT=3600)
        v = _judged(_facts(d), [_app("api", 3600)])[dom]
        self.assertEqual(v["app"], "api")
        self.assertIn("127.0.0.1:3600", v["note"])

    def test_shared_port_is_not_guessed(self):
        """两个应用共用一个端口时，猜哪个都可能错——只给理由，让人指定。"""
        d = _dir()
        dom = _dom("shared")
        _write(d, "shared.conf", ROOT_ONLY, DOMAIN=dom, PORT=3700)
        v = _judged(_facts(d), [_app("a", 3700), _app("b", 3700)])[dom]
        self.assertIsNone(v["kind"])
        self.assertIn("共用", v["reason"])
        self.assertIn("手工指定", v["reason"])

    def test_port_without_app_is_not_guessed(self):
        d = _dir()
        dom = _dom("nobody")
        _write(d, "nobody.conf", ROOT_ONLY, DOMAIN=dom, PORT=3800)
        v = _judged(_facts(d), [_app("api", 3000)])[dom]
        self.assertIsNone(v["kind"])
        self.assertIn("没有应用登记这个端口", v["reason"])

    def test_file_stem_matches_app_name(self):
        """面板自己生成的 panel-<应用名>.conf：没有反代端口也能认回来。"""
        d = _dir()
        name = _name("stem")
        dom = f"{name}.choyeon.cc"
        _write(d, f"panel-{name}.conf", STATIC, DOMAIN=dom)
        v = _judged(_facts(d), [_app(name, None)])[dom]
        self.assertEqual((v["kind"], v["app"]), (SC.KIND_APP, name))
        self.assertIn("与该应用同名", v["note"])

    def test_alias_block_follows_the_redirect(self):
        """www 只做 301：归属跟随它跳去的那个域名，但理由要点名是跟随来的。"""
        d = _dir()
        bare = _dom("bare")
        alias = f"www.{bare}"
        _write(d, "bare.conf", ROOT_ONLY, DOMAIN=bare, PORT=3900)
        _write(d, "alias.conf", ALIAS, ALIAS=alias, TARGET=bare)
        v = _judged(_facts(d), [_app("api", 3900)])
        self.assertEqual(v[alias]["app"], "api")
        self.assertIn("跟随", v[alias]["note"])

    def test_remote_target_is_left_alone(self):
        d = _dir()
        dom = _dom("remote")
        _write(d, "remote.conf", REMOTE, DOMAIN=dom)
        v = _judged(_facts(d), [_app("api", 8080)])[dom]
        self.assertIsNone(v["kind"], "指向别的机器时本机端口账毫无意义，不许按端口认")

    def test_static_site_reason_is_showable(self):
        d = _dir()
        dom = _dom("why")
        _write(d, "why.conf", STATIC, DOMAIN=dom)
        v = _judged(_facts(d), [])[dom]
        self.assertIsNone(v["kind"])
        self.assertTrue(v["reason"])
        self.assertIn("手工指定", v["reason"])


class TestRows(unittest.TestCase):
    def test_registry_wins_and_suggestion_stays(self):
        d = _dir()
        dom = _dom("row")
        _write(d, "manual.conf", ROOT_ONLY, DOMAIN=dom, PORT=4100)
        apps = [_app("right", 4200, app_id=1), _app("wrong", 4100, app_id=2)]
        reg = {dom: {"domain": dom, "app_id": 1, "file": "manual.conf", "kind": "app",
                     "source": "manual", "note": "手工指定为应用 right"}}
        row = _rows(_facts(d), apps, reg)[0]
        self.assertEqual(row["app"], "right", "登记说了归谁就是谁")
        self.assertEqual(row["suggest_app"], "wrong", "判定结果留在 suggest_* 里供质疑")
        self.assertTrue(row["claimed"])
        self.assertTrue(row["port_mismatch"], "配置 4100 与应用登记 4200 的差额要如实标出来")

    def test_unregistered_row_carries_the_suggestion(self):
        d = _dir()
        dom = _dom("sug")
        _write(d, "sug.conf", ROOT_ONLY, DOMAIN=dom, PORT=4300)
        row = _rows(_facts(d), [_app("api", 4300, app_id=1)], {})[0]
        self.assertFalse(row["claimed"])
        self.assertEqual(row["app"], "api")
        self.assertEqual(row["claim_note"], row["suggest_note"])

    def test_registry_only_domain_still_shows(self):
        """配置已经删了但登记还在：这行必须出现在列表里，否则页面上没有入口能看到它。"""
        row = _rows([], [], {"gone.choyeon.cc": {"domain": "gone.choyeon.cc", "app_id": None,
                                                "file": "gone.conf", "kind": "panel",
                                                "source": "auto", "note": ""}})[0]
        self.assertEqual(row["domain"], "gone.choyeon.cc")
        self.assertTrue(row["claimed"])
        self.assertEqual(row["app"], SC.PANEL_LABEL)
        self.assertTrue(row["claim_stale"])

    def test_claimed_app_that_is_gone_does_not_invent_a_name(self):
        """登记指向一个查无此应用的 id（drop_app 没跑成的那类残留）：行要在，但不许编出归属。"""
        row = _rows([], [], {_dom("ghost"): {"domain": _dom("ghost"), "app_id": 77, "file": "",
                                            "kind": "app", "source": "manual", "note": ""}})[0]
        self.assertTrue(row["claimed"], "登记还在，页面就得看得见这一行才能解绑")
        self.assertIsNone(row["app"], "认不出应用时宁可显示空，不要把 id 当成名字")

    def test_attribution_only_returns_rows_with_an_owner(self):
        d = _dir()
        owned, orphan = _dom("owned"), _dom("orphan")
        _write(d, "a.conf", ROOT_ONLY, DOMAIN=owned, PORT=4400)
        _write(d, "b.conf", STATIC, DOMAIN=orphan)
        got = SC.attribution(_facts(d), [_app("api", 4400, app_id=1)], {})
        self.assertEqual(set(got), {owned})

    def test_sort_puts_owned_rows_first(self):
        rows = [{"app": None, "domain": "aaa.com"}, {"app": "api", "domain": "zzz.com"},
                {"app": None, "domain": "mmm.com"}]
        self.assertEqual([r["domain"] for r in SC.sort_rows(rows)], ["zzz.com", "aaa.com", "mmm.com"])


class TestClaimFlow(unittest.TestCase):
    def setUp(self):
        self.d = _dir()

    def _mk_app(self, name, port=None, domain=""):
        app_id = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,start_cmd) VALUES(?,?,?,?,?,?,?,?)",
            (name, "node", "", "main", f"/tmp/cp57a/{name}", port, domain, "node x.js"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        return app_id

    def _claim_row(self, dom):
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE domain=?", (dom,))
        return dbm.query_one("SELECT * FROM site_claims WHERE domain=?", (dom,))

    def test_claim_uses_the_judgment_when_app_id_omitted(self):
        dom = _dom("ca")
        _write(self.d, "ca.conf", ROOT_ONLY, DOMAIN=dom, PORT=4500)
        app_id = self._mk_app(_name("ca"), 4500)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            res = asyncio.run(SC.claim(dom.upper()))
        row = self._claim_row(dom)
        self.assertEqual((res["kind"], res["source"], row["app_id"], row["file"]),
                         (SC.KIND_APP, SC.SOURCE_AUTO, app_id, "ca.conf"))
        self.assertEqual(res["file"], "ca.conf")

    def test_claim_panel_entry_has_no_app(self):
        dom = _dom("panelca")
        _write(self.d, "p.conf", ROOT_ONLY, DOMAIN=dom, PORT=PANEL_PORT)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            res = asyncio.run(SC.claim(dom))
        self.assertEqual((res["kind"], res["app"]), (SC.KIND_PANEL, None))
        self.assertIsNone(self._claim_row(dom)["app_id"])

    def test_claim_refuses_to_guess(self):
        dom = _dom("amb")
        _write(self.d, "amb.conf", STATIC, DOMAIN=dom)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(SC.claim(dom))
        self.assertIn("手工指定", str(cm.exception))
        self.assertIsNone(dbm.query_one("SELECT domain FROM site_claims WHERE domain=?", (dom,)))

    def test_claim_with_app_id_records_the_disagreement(self):
        """人指定成另一个应用时照登，但把面板原本的判定写进 note——事后能查是谁改的口径。"""
        dom = _dom("manual")
        _write(self.d, "m.conf", ROOT_ONLY, DOMAIN=dom, PORT=4600)
        guess_id = self._mk_app(_name("guess"), 4600)
        mine_id = self._mk_app(_name("mine"), 4700)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            res = asyncio.run(SC.claim(dom, mine_id))
        row = self._claim_row(dom)
        self.assertEqual((res["source"], row["app_id"]), (SC.SOURCE_MANUAL, mine_id))
        self.assertIn("手工指定为应用", res["note"])
        self.assertIn("guess", res["note"], "原判定必须留在 note 里")
        self.assertNotEqual(guess_id, mine_id)

    def test_claim_unknown_domain_is_refused(self):
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]):
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(SC.claim("never-in-nginx.choyeon.cc"))
        self.assertIn("没有这个域名的 server 块", str(cm.exception))

    def test_claim_bad_domain_is_refused(self):
        for bad in ("", "not a domain", "本地"):
            with self.assertRaises(RuntimeError):
                asyncio.run(SC.claim(bad))

    def test_claim_app_not_found(self):
        dom = _dom("noapp")
        _write(self.d, "n.conf", ROOT_ONLY, DOMAIN=dom, PORT=4800)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(SC.claim(dom, 999999))
        self.assertIn("应用不存在", str(cm.exception))

    def test_unclaim_only_removes_the_row(self):
        dom = _dom("un")
        _write(self.d, "u.conf", ROOT_ONLY, DOMAIN=dom, PORT=4900)
        self._mk_app(_name("un"), 4900)
        conf = Path(self.d, "u.conf")
        before = conf.read_text(encoding="utf-8")
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            asyncio.run(SC.claim(dom))
            res = asyncio.run(SC.unclaim(dom))
        self.assertTrue(res["removed"])
        self.assertEqual(conf.read_text(encoding="utf-8"), before, "取消纳管不许碰 nginx 的一个字节")
        self.assertIsNone(dbm.query_one("SELECT domain FROM site_claims WHERE domain=?", (dom,)))

    def test_unclaim_without_a_row_says_so(self):
        res = asyncio.run(SC.unclaim("never.choyeon.cc"))
        self.assertFalse(res["removed"])

    def test_unclaim_rejects_junk_domain(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(SC.unclaim(" "))

    def test_claim_all_skips_what_it_cannot_judge(self):
        judged, skipped = _dom("all1"), _dom("all2")
        _write(self.d, "all1.conf", ROOT_ONLY, DOMAIN=judged, PORT=5000)
        _write(self.d, "all2.conf", STATIC, DOMAIN=skipped)
        self._mk_app(_name("all"), 5000)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            res = asyncio.run(SC.claim_all())
        self.assertEqual([r["domain"] for r in res["claimed"]], [judged])
        self.assertEqual([r["domain"] for r in res["skipped"]], [skipped])
        self.assertTrue(res["skipped"][0]["reason"])
        self.assertEqual(res["unmanaged"], 1)
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE domain=?", (judged,))

    def test_claim_all_does_not_overwrite_a_manual_claim(self):
        """批量纳管不许把人手工指定的归属改回按端口猜出来的：第二轮一个都不重写。"""
        dom = _dom("keep")
        _write(self.d, "keep.conf", ROOT_ONLY, DOMAIN=dom, PORT=5100)
        right = self._mk_app(_name("right"), 5200)
        self._mk_app(_name("wrong"), 5100)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            asyncio.run(SC.claim(dom, right))
            res = asyncio.run(SC.claim_all())
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE domain=?", (dom,))
        self.assertEqual(res["claimed"], [], "已登记的不属于这一轮")
        self.assertEqual(dbm.query_one("SELECT app_id FROM site_claims WHERE domain=?", (dom,))["app_id"], right)

    def test_claim_all_is_repeatable(self):
        dom = _dom("twice")
        _write(self.d, "t.conf", ROOT_ONLY, DOMAIN=dom, PORT=5300)
        self._mk_app(_name("twice"), 5300)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            asyncio.run(SC.claim_all())
            second = asyncio.run(SC.claim_all())
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE domain=?", (dom,))
        self.assertEqual(second["claimed"], [])

    def test_delete_app_drops_its_claims(self):
        """批次⑤L 的口径：对象没了，派生态跟着走。"""
        dom = _dom("drop")
        _write(self.d, "d.conf", ROOT_ONLY, DOMAIN=dom, PORT=5400)
        app_id = self._mk_app(_name("drop"), 5400)
        other = self._mk_app(_name("kept"), 5500)
        other_dom = _dom("other")
        with mock.patch.object(config, "NGINX_CONF_DIRS", [self.d]), \
             mock.patch.object(config, "PORT", PANEL_PORT):
            asyncio.run(SC.claim(dom))
        dbm.execute("INSERT INTO site_claims(domain,app_id,file,kind,source) VALUES(?,?,?,?,?)",
                    (other_dom, other, "", "app", "manual"))
        self.addCleanup(dbm.execute, "DELETE FROM site_claims WHERE app_id=?", (other,))
        with mock.patch.object(apps_service, "remove_unit", mock.AsyncMock()), \
             mock.patch.object(apps_service, "remove_env_link", mock.AsyncMock()):
            asyncio.run(apps_service.delete_app(app_id, False))
        self.assertIsNone(dbm.query_one("SELECT domain FROM site_claims WHERE app_id=?", (app_id,)))
        self.assertIsNotNone(dbm.query_one("SELECT domain FROM site_claims WHERE app_id=?", (other,)),
                             "别的应用的登记不能被牵连")


class TestEndpoints(unittest.TestCase):
    def _req(self, body=None, role="admin", sub="57a_tester"):
        class _R:
            state = argparse.Namespace(cp_role=role, cp_sub=sub)

            async def json(self):
                return body or {}

        return _R()

    def test_viewer_cannot_write_claims(self):
        for fn in (sites.site_claim, sites.site_unclaim, sites.site_claim_all):
            r = asyncio.run(fn(self._req({"domain": "x.choyeon.cc"}, role="viewer")))
            self.assertEqual(r.status_code, 403, fn.__name__)

    def test_missing_domain_is_400(self):
        self.assertEqual(asyncio.run(sites.site_claim(self._req())).status_code, 400)
        self.assertEqual(asyncio.run(sites.site_unclaim(self._req())).status_code, 400)

    def test_non_numeric_app_id_is_400(self):
        r = asyncio.run(sites.site_claim(self._req({"domain": "x.choyeon.cc", "app_id": "7;drop table"})))
        self.assertEqual(r.status_code, 400)

    def test_claim_failure_is_audited_without_paths(self):
        """失败也要留痕，而且留的是脱敏后的理由，不是 nginx 报错原文里的绝对路径。"""
        boom = RuntimeError("nginx: /etc/nginx/conf.d/x.conf 权限不足")

        async def _fail(domain, app_id=None):
            raise boom

        with mock.patch.object(SC, "claim", _fail), \
             mock.patch.object(sites.aio_db, "audit", mock.AsyncMock()) as aud:
            r = asyncio.run(sites.site_claim(self._req({"domain": "a.choyeon.cc"})))
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(ABS_PATH_RE.search(r.body.decode(errors="replace")), r.body)
        self.assertEqual(aud.call_args[0][1], "site:claim")
        detail = aud.call_args[0][2]
        self.assertIn("a.choyeon.cc", detail)
        self.assertIn("权限不足", detail, "失败原因要留在审计里，不然事后查不到是为什么")
        self.assertIsNone(ABS_PATH_RE.search(detail), detail)

    def test_claim_success_audits_the_owner_and_file(self):
        async def _ok(domain, app_id=None):
            return {"domain": domain, "app": "api", "kind": "app", "file": "api.conf",
                    "note": "根路径反代 127.0.0.1:3000", "source": "auto"}

        with mock.patch.object(SC, "claim", _ok), \
             mock.patch.object(sites.aio_db, "audit", mock.AsyncMock()) as aud:
            r = asyncio.run(sites.site_claim(self._req({"domain": "a.choyeon.cc"})))
        self.assertTrue(r["ok"])
        self.assertIn("a.choyeon.cc → api（api.conf）", aud.call_args[0][2])

    def test_unclaim_reports_the_empty_case_in_audit(self):
        async def _none(domain):
            return {"domain": domain, "removed": False}

        with mock.patch.object(SC, "unclaim", _none), \
             mock.patch.object(sites.aio_db, "audit", mock.AsyncMock()) as aud:
            r = asyncio.run(sites.site_unclaim(self._req({"domain": "ghost.choyeon.cc"})))
        self.assertTrue(r["ok"])
        self.assertIn("本就没有登记", aud.call_args[0][2])

    def test_claim_all_counts_go_to_audit(self):
        async def _all():
            return {"claimed": [{"domain": "a"}], "skipped": [{"domain": "b", "reason": "r"}], "unmanaged": 1}

        with mock.patch.object(SC, "claim_all", _all), \
             mock.patch.object(sites.aio_db, "audit", mock.AsyncMock()) as aud:
            r = asyncio.run(sites.site_claim_all(self._req()))
        self.assertTrue(r["ok"])
        self.assertIn("纳管 1 个，判不出归属 1 个", aud.call_args[0][2])


class TestCli(unittest.TestCase):
    def test_list_text_says_who_and_why(self):
        rows = [
            {"domain": "a.com", "app": "api", "claimed": True, "claim_stale": False,
             "suggest_kind": "app", "suggest_app": "api", "suggest_note": "根路径反代",
             "suggest_reason": "", "file": "a.conf", "port": 3000, "app_port": 3000,
             "port_mismatch": False},
            {"domain": "b.com", "app": None, "claimed": False, "claim_stale": True,
             "suggest_kind": "", "suggest_app": None, "suggest_note": "",
             "suggest_reason": "归属不唯一", "file": "b.conf", "port": None, "app_port": None,
             "port_mismatch": False},
        ]
        text = cli._site_list_text(rows)
        for phrase in ("站点纳管：2 个域名", "a.com · api（已纳管）", "b.com · —（未纳管）",
                       "归属不唯一", "认领的文件已不在加载列表", "choyeonctl site claim"):
            self.assertIn(phrase, text, phrase)

    def test_list_text_flags_a_port_gap(self):
        rows = [{"domain": "a.com", "app": "api", "claimed": True, "claim_stale": False,
                 "suggest_kind": "app", "suggest_app": "api", "suggest_note": "", "suggest_reason": "",
                 "file": "a.conf", "port": 3300, "app_port": 3100, "port_mismatch": True}]
        self.assertIn("3300≠登记 3100", cli._site_list_text(rows))

    def test_claim_all_text_lists_both_buckets(self):
        text = cli._site_claim_all_text({"claimed": [{"domain": "a.com", "app": "api", "kind": "app",
                                                     "file": "a.conf", "note": "根路径反代"}],
                                         "skipped": [{"domain": "b.com", "reason": "共用端口"}],
                                         "unmanaged": 1})
        for phrase in ("登记 1 个，判不出归属 1 个", "a.com → api", "b.com 未纳管：共用端口",
                       "面板不替你猜"):
            self.assertIn(phrase, text, phrase)

    def test_panel_entry_shows_the_label(self):
        text = cli._site_claim_all_text({"claimed": [{"domain": "p.com", "app": None, "kind": "panel",
                                                     "file": "p.conf", "note": "面板入口"}],
                                         "skipped": [], "unmanaged": 0})
        self.assertIn("p.com → 面板入口", text)

    def test_usage_errors_before_touching_the_service(self):
        """用法错要在读 nginx 之前退出，而且一条登记都不许落库。"""
        before = len(dbm.query("SELECT domain FROM site_claims"))
        cases = ((_ns(site_cmd="claim", domain="", app=None), "claim 需要域名"),
                 (_ns(site_cmd="claim", domain="a.com", app="api"), "应用 id"),
                 (_ns(site_cmd="unclaim", domain="", app=None), "unclaim 需要域名"))
        for ns, _msg in cases:
            with self.assertRaises(SystemExit):
                asyncio.run(cli.cmd_site(ns))
        self.assertEqual(len(dbm.query("SELECT domain FROM site_claims")), before)

    def test_list_without_nginx_is_precondition(self):
        async def _none():
            return [], "未找到 nginx 配置目录"

        with mock.patch.object(SC, "sites_rows", _none), self.assertRaises(SystemExit) as cm:
            asyncio.run(cli.cmd_site(_ns(site_cmd="list", domain=None, app=None)))
        self.assertEqual(cm.exception.code, cli.EXIT_PRECONDITION)


class TestOneFactOneReader(unittest.TestCase):
    """站点计数、站点页与链路对账必须共用同一个读者。"""

    def test_site_domains_uses_the_same_config_reader(self):
        d = _dir()
        dom = _dom("cnt")
        _write(d, "c1.conf", ROOT_ONLY, DOMAIN=dom, PORT=6000)
        _write(d, "c2.conf", ALIAS, ALIAS=f"www.{dom}", TARGET=dom)
        loaded = mock.Mock(wraps=nginx_ops.loaded_configs)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [d]), \
             mock.patch.object(nginx_ops, "loaded_configs", loaded):
            names, reason = SC.site_domains()
        self.assertIsNone(reason)
        self.assertEqual(names, {dom, f"www.{dom}"})
        loaded.assert_called_once_with()

    def test_site_domains_is_honest_when_nginx_missing(self):
        with mock.patch.object(config, "NGINX_CONF_DIRS", ["/nonexistent-57a"]):
            names, reason = SC.site_domains()
        self.assertEqual(names, set())
        self.assertTrue(reason)

    def test_sites_rows_reads_the_directory_once(self):
        d = _dir()
        dom = _dom("src")
        _write(d, "s.conf", ROOT_ONLY, DOMAIN=dom, PORT=6100)
        loaded, present = mock.Mock(wraps=nginx_ops.loaded_configs), mock.Mock(wraps=nginx_ops.config_dirs_present)
        scanned = mock.Mock(wraps=nginx_ops._iter_conf_files)
        with mock.patch.object(config, "NGINX_CONF_DIRS", [d]), \
             mock.patch.object(nginx_ops, "loaded_configs", loaded), \
             mock.patch.object(nginx_ops, "config_dirs_present", present), \
             mock.patch.object(nginx_ops, "_iter_conf_files", scanned), \
             mock.patch.object(SC.systemd_ops, "unit_states", mock.AsyncMock(return_value={})):
            asyncio.run(SC.sites_rows())
        loaded.assert_called_once_with()
        scanned.assert_called_once_with()
        present.assert_called_once_with()


class TestDocsContract(unittest.TestCase):
    """命令进了 schema 就得进 AGENTS.md，否则智能体照抄的那条命令不存在。"""

    def _parser(self):
        root = cli.build_parser()
        action = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
        return action.choices["site"]

    def test_parser_routes_the_four_verbs_to_cmd_site(self):
        sub = self._parser()
        verbs = next(a for a in sub._actions if getattr(a, "dest", "") == "site_cmd")
        self.assertEqual(verbs.choices, ["list", "claim", "unclaim", "claim-all"])
        self.assertIs(sub.get_default("fn"), cli.cmd_site)

    def test_schema_declares_the_command_and_its_option(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            schema = asyncio.run(cli.cmd_schema(argparse.Namespace()))
        entry = next(e for e in schema["commands"] if e["name"] == "site list|claim|unclaim|claim-all")
        self.assertEqual(entry["args"], ["--app"])
        self.assertIn("不写 nginx", entry["desc"])

    def test_agents_md_row_declares_the_contract(self):
        text = (Path(cli.ROOT) / "AGENTS.md").read_text(encoding="utf-8")
        row = next(ln for ln in text.splitlines() if ln.startswith("| `site list"))
        for phrase in ("登记表", "不写 nginx", "--app"):
            self.assertIn(phrase, row, phrase)


if __name__ == "__main__":
    unittest.main()
