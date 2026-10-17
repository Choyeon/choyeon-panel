"""批次㊿B：告警配置键只有一个登记表。

过去「面板认识哪些 alert_ 键」写着三份：`alerts_service.ALERT_LIMITS`（数值）、
`extras` 里一条正则的 alternation（保存白名单）、`extras.ALERT_KEYS`（GET 的读取范围）。
三处之间没有任何机制保证一起改，而漏掉任何一处都不报错：

- 新增键只进登记表 → 不在读取范围里，GET 永远回空串，页面显示的就是「改了没生效」；
- 新增键只进路由白名单 → `setting_int()` 在巡检里 KeyError，被循环整轮吞掉，
  表现为「告警从此不再响」，网页端一切正常。

现在登记表（ALERT_LIMITS + ALERT_TEXT_MAX）是唯一的键名出处，`alert_keys()` 给并集，
路由在**请求时**向它要（不是 import 期快照），所以本文件能当场往登记表里加一个键，
再证明保存、读取两条路都跟着它走——这是这三份抄本合一时唯一有意义的验收。
"""

import ast
import itertools
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from app import database as dbm
from app import main, security
from app.routers import extras
from app.services import alerts_service as als

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

_seq = itertools.count()


def _code_lines(text: str) -> str:
    """只剩可执行行：注释与 docstring 里引用的旧写法是这一批的证据，不是复发。"""
    tree = ast.parse(text)
    drop: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [line for no, line in enumerate(text.splitlines(), 1)
            if no not in drop and not line.strip().startswith("#")]
    return "\n".join(kept)


def _app_code() -> dict[str, str]:
    out = {}
    for p in sorted(APP.rglob("*.py")):
        out[str(p.relative_to(BACKEND))] = _code_lines(p.read_text(encoding="utf-8"))
    return out


# 登记表里每个键的合法样例值（数值取登记的默认值，文本给一个一定过形状校验的串）
def _sample(key: str) -> str:
    if key in als.ALERT_LIMITS:
        return str(als.ALERT_LIMITS[key][0])
    if key in als.ALERT_URL_KEYS:
        return "https://hooks.example.com/panel"
    return "sample"


class RegistryShapeTests(unittest.TestCase):
    """登记表自身的形状：并集不许重叠、派生集合必须是登记表的子集。"""

    def test_并集等于数值加文本且没有重复(self):
        keys = als.alert_keys()
        self.assertEqual(keys, tuple(als.ALERT_LIMITS) + tuple(als.ALERT_TEXT_MAX))
        self.assertEqual(len(set(keys)), len(keys), "同一个键登记两次，读取列表会出现两遍")

    def test_数值与文本两张表互斥(self):
        both = set(als.ALERT_LIMITS) & set(als.ALERT_TEXT_MAX)
        self.assertEqual(both, set(), f"{sorted(both)} 既有区间又有长度上限，校验会走哪条说不清")

    def test_url与密钥集合都已登记(self):
        keys = set(als.alert_keys())
        self.assertTrue(keys >= als.ALERT_URL_KEYS, als.ALERT_URL_KEYS - keys)
        self.assertTrue(keys >= als.ALERT_SECRET_KEYS, als.ALERT_SECRET_KEYS - keys)

    def test_路由里的表是同一份对象而不是抄本(self):
        """路由的 ALERT_MAX_LEN 必须就是服务层那张表：复制一份的话，
        改了登记表而路由留着旧数字，长度校验会按老规矩拦人。"""
        self.assertIs(extras.ALERT_MAX_LEN, als.ALERT_TEXT_MAX)

    def test_含密钥的通道就是那三项(self):
        """产品口径：bot token / chat id / webhook URL 是秘密，只读账号一律看到空串。
        新增通道键必须同时决定它算不算秘密，这条用例逼迫当时就做这个决定。"""
        self.assertEqual(set(als.ALERT_SECRET_KEYS),
                         {"alert_telegram_bot", "alert_telegram_chat", "alert_webhook_url"})

    def test_长度上限都是正数(self):
        for k, v in als.ALERT_TEXT_MAX.items():
            self.assertIsInstance(v, int, k)
            self.assertGreater(v, 0, k)


class ReaderCoverageTests(unittest.TestCase):
    """静态扫源码：键名只许出现在登记表里，且每个登记的键都真的有人读。"""

    @classmethod
    def setUpClass(cls):
        cls.code = _app_code()

    def test_代码里出现的alert键名都必须已登记(self):
        known = set(als.alert_keys())
        bad = {}
        for rel, code in self.code.items():
            for key in re.findall(r'"(alert_[a-z0-9_]+)"', code):
                if key not in known:
                    bad.setdefault(rel, []).append(key)
        self.assertEqual(bad, {}, f"未登记的键读出来就是 KeyError：{bad}")

    def test_每个登记的键都有读者(self):
        """没用的设置项会一直留在设置页上，管理员改了它、什么也不会发生——
        这种键比缺键更难发现，所以反向也要查。"""
        entry = re.compile(r'^\s*"(alert_[a-z0-9_]+)":\s')
        for key in als.alert_keys():
            hits = []
            for rel, code in self.code.items():
                for no, line in enumerate(code.splitlines(), 1):
                    if entry.match(line) and key in line:
                        continue  # 登记表自身那一行
                    if line.startswith(("ALERT_URL_KEYS", "ALERT_SECRET_KEYS")):
                        continue  # 形态/密钥集合也只是再点名一次，不是读取
                    if f'"{key}"' in line:
                        hits.append(f"{rel}:{no}")
            self.assertTrue(hits, f"{key} 登记了却没有任何代码读它")

    def test_路由里不再出现任何alert键名(self):
        """三份抄本合上的判据：extras 的源码（剔掉注释）里一个 `alert_` 字面量都不许有，
        白名单、读取列表、长度上限、URL 形态全部问服务层要。"""
        code = _code_lines((APP / "routers" / "extras.py").read_text(encoding="utf-8"))
        self.assertEqual(re.findall(r'"alert_[a-z0-9_]+"', code), [],
                         "路由里又写了一份键名，登记表就又不是唯一出处了")
        self.assertNotIn("ALLOWED_ALERT_KEYS = re.compile", code)

    def test_登记表在整个后端只定义一次(self):
        for name in ("ALERT_LIMITS", "ALERT_TEXT_MAX", "ALERT_URL_KEYS", "ALERT_SECRET_KEYS"):
            where = [rel for rel, code in self.code.items()
                     if re.search(rf"^{name}\s*[:=]", code, re.M)]
            self.assertEqual(where, ["app/services/alerts_service.py"], f"{name} 的定义处：{where}")


class RouteFollowsRegistryTests(unittest.TestCase):
    """线上行为：路由跟着登记表走，而不是跟着自己那份名单走。"""

    def setUp(self):
        self.client = TestClient(main.app)
        self.user = f"b50b_{os.getpid()}_{next(_seq)}"
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE 'alert_%'")
        self.h = {"Authorization": f"Bearer {security.sign_token(self.user, 'admin')}"}

    def test_登记表新增的键立刻能存能读(self):
        """这条是整批的核心：过去要同时改正则和列表才生效，现在只改登记表。"""
        with mock.patch.dict(als.ALERT_TEXT_MAX, {"alert_zzz_probe": 16}):
            r = self.client.post("/api/settings/alerts", json={"alert_zzz_probe": "hello"},
                                 headers=self.h)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(dbm.get_setting("alert_zzz_probe"), "hello", "白名单没跟着登记表")
            g = self.client.get("/api/settings/alerts", headers=self.h)
            self.assertEqual(g.status_code, 200, g.text)
            self.assertIn("alert_zzz_probe", g.json(), "读取列表没跟着登记表：页面会显示默认值")

    def test_不在登记表里的键依旧静默忽略(self):
        """设置页有整包提交的调用（会把 limits/crit_step 一起带上），
        所以未知键不许变成 400；这条把「为什么不做严格校验」钉住。"""
        r = self.client.post("/api/settings/alerts", json={"limits": {"a": 1}, "crit_step": 10},
                             headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(dbm.get_setting("limits"))

    def test_每个登记的键都能存进去(self):
        for key in als.alert_keys():
            with self.subTest(key=key):
                r = self.client.post("/api/settings/alerts", json={key: _sample(key)},
                                     headers=self.h)
                self.assertEqual(r.status_code, 200, f"{key}: {r.text}")
                self.assertEqual(dbm.get_setting(key), _sample(key))

    def test_每个登记的键都出现在GET载荷里(self):
        g = self.client.get("/api/settings/alerts", headers=self.h).json()
        missing = [k for k in als.alert_keys() if k not in g]
        self.assertEqual(missing, [], "页面拿不到的键，改了就是没生效")

    def test_区间随载荷给出的就是登记表(self):
        g = self.client.get("/api/settings/alerts", headers=self.h).json()
        self.assertEqual(set(g["limits"]), set(als.ALERT_LIMITS))
        for k, (d, lo, hi) in als.ALERT_LIMITS.items():
            self.assertEqual(g["limits"][k], {"default": d, "min": lo, "max": hi}, k)

    def test_超区的键依旧被拒(self):
        hi = als.ALERT_LIMITS["alert_disk_pct"][2]
        r = self.client.post("/api/settings/alerts", json={"alert_disk_pct": str(hi + 1)},
                             headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(dbm.get_setting("alert_disk_pct"))

    def test_超长文本依旧被拒(self):
        n = als.ALERT_TEXT_MAX["alert_telegram_chat"]
        r = self.client.post("/api/settings/alerts",
                             json={"alert_telegram_chat": "9" * (n + 1)}, headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)

    def test_URL形态键仍要求http开头(self):
        r = self.client.post("/api/settings/alerts", json={"alert_webhook_url": "ftp://x/y"},
                             headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(dbm.get_setting("alert_webhook_url"))

    def test_只读角色读不到密钥键(self):
        dbm.set_setting("alert_telegram_bot", "123456:SECRET")
        viewer = f"b50b_v_{os.getpid()}_{next(_seq)}"
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (viewer, security.hash_pass("Password123"), "viewer"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (viewer,))
        hdr = {"Authorization": f"Bearer {security.sign_token(viewer, 'viewer')}"}
        body = self.client.get("/api/settings/alerts", headers=hdr).json()
        for k in als.ALERT_SECRET_KEYS:
            self.assertEqual(body[k], "", f"{k} 的值对只读账号可见")


class ProbeExpectPrefixTests(unittest.TestCase):
    """㊿E：`probe_expect:` 前缀与它的长度上限，一个事实一个出处（服务层）。

    这一批延续 ㊿B 的方向：㊿B 把「面板认识哪些 alert_ 键」收成一张登记表，而按应用覆写
    那条通道（`probe_expect:<应用名>`）当时留在外面——前缀在服务层与路由里抄了六处
    （键名构造两处、正则一处、SQL 的 LIKE 一处、切片与 startswith 各一处），长度上限更是
    在登记表、路由校验、告警正文里各写死了一份 32，页面还自己拼了一份键名。
    """

    WEB = BACKEND.parent / "web" / "src"

    @classmethod
    def setUpClass(cls):
        cls.code = _app_code()

    def _literal_count(self, rel):
        """只数带引号的前缀（注释与 docstring 已被 _code_lines 剔掉，提及不算抄本）。"""
        code = self.code[rel]
        return code.count('"probe_expect:"') + code.count("'probe_expect:'")

    def test_前缀字面量只剩服务层那一处定义(self):
        hits = {rel: n for rel in self.code if (n := self._literal_count(rel))}
        self.assertEqual(hits, {"app/services/alerts_service.py": 1},
                         f"前缀又被人抄了一份，换名字就会只改到一半：{hits}")

    def test_路由的正则由同一个前缀派生(self):
        """换前缀时正则必须跟着走，否则覆写能存进库、却再也读不出来。"""
        self.assertIn(re.escape(als.PROBE_EXPECT_PREFIX), extras.PROBE_OVERRIDE_KEY.pattern)
        self.assertTrue(extras.PROBE_OVERRIDE_KEY.match(als.probe_expect_key("my-api")))

    def test_键名构造与清理用的是同一个出处(self):
        """probe_marker_keys 是 forget_app 的清理依据，两处名字不同就清不掉正要清的那条。"""
        self.assertEqual(als.probe_expect_key("api"), als.PROBE_EXPECT_PREFIX + "api")
        self.assertEqual(als.probe_marker_keys("api")[0], als.probe_expect_key("api"))

    def test_长度上限只有一个函数在给(self):
        """路由侧不许再出现自己那份 32，也不许自己点全局键的名字（㊿B 的守卫会连带拦住键名）。"""
        code = _code_lines((APP / "routers" / "extras.py").read_text(encoding="utf-8"))
        self.assertIn("alerts_service.probe_expect_max()", code)
        self.assertNotIn("if len(val) > 32:", code, "路由里还留着那份 32 的抄本")
        svc = self.code["app/services/alerts_service.py"]
        self.assertEqual(svc.count('ALERT_TEXT_MAX["alert_probe_expect"]'), 1,
                         "上限的出处必须只有 probe_expect_max() 一处")

    def test_页面不再自己拼键名(self):
        """前端整棵 src 里一个前缀字面量都不许有，且必须读后端那个字段：
        未知告警键是静默忽略的，页面抄错只会「保存成功、永不生效」。"""
        hits = [str(p.relative_to(self.WEB)) for p in self.WEB.rglob("*")
                if p.suffix in (".vue", ".ts", ".js", ".mjs")
                and als.PROBE_EXPECT_PREFIX in p.read_text(encoding="utf-8")]
        self.assertEqual(hits, [], f"页面又自己拼了一份键名：{hits}")
        page = (self.WEB / "views" / "Settings.vue").read_text(encoding="utf-8")
        self.assertIn(als.PROBE_PREFIX_FIELD, page, "页面没读后端给的前缀字段")


class ProbeExpectRouteTests(unittest.TestCase):
    """线上行为：覆写键的写入、读出、上限都认 `probe_expect_key()` 与登记表那一个数。"""

    def setUp(self):
        self.client = TestClient(main.app)
        self.user = f"b50e_{os.getpid()}_{next(_seq)}"
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (self.user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?",
                        (als.PROBE_EXPECT_PREFIX + "%",))
        self.h = {"Authorization": f"Bearer {security.sign_token(self.user, 'admin')}"}

    def _mkapp(self, name: str) -> int:
        """覆写要求应用真实存在（路由会查 apps 表），所以先造一行再测。"""
        aid = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,path,start_cmd) VALUES(?,'python',?,?,?)",
            (name, "https://example.invalid/r.git", f"/tmp/{name}", "python3 x.py"))
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (aid,))
        return aid

    def test_覆写存得进也读得出(self):
        """写用 probe_expect_key()，GET 的 LIKE/切片也要按同一个前缀把它捞回来——
        这两处任何一处没跟着前缀走，页面都会显示成「没有覆写」。"""
        name = f"b50e-app-{next(_seq)}"
        self._mkapp(name)
        key = als.probe_expect_key(name)
        r = self.client.post("/api/settings/alerts", json={key: "200 204"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.get_setting(key), "200 204")
        g = self.client.get("/api/settings/alerts", headers=self.h).json()
        self.assertEqual(g["probe_overrides"], {name: "200 204"})

    def test_GET载荷把前缀交给页面(self):
        g = self.client.get("/api/settings/alerts", headers=self.h).json()
        self.assertEqual(g[als.PROBE_PREFIX_FIELD], als.PROBE_EXPECT_PREFIX)

    def test_服务侧读的就是路由写的那条(self):
        """两头落在同一个键名上：写进去读不出来，覆写等于没配。"""
        name = f"b50e-read-{next(_seq)}"
        self._mkapp(name)
        self.client.post("/api/settings/alerts",
                         json={als.probe_expect_key(name): "418"}, headers=self.h)
        codes, shown = als._app_expectation(name)
        self.assertEqual(codes, {418}, shown)
        self.assertEqual(shown, "418")

    def test_告警显示的期望码不会被另一个数截短(self):
        """展示截断以前钉死在 32：放宽登记表后，配了 40 字符的应用在通知里只发来前 32 个。"""
        name = f"b50e-show-{next(_seq)}"
        with mock.patch.dict(als.ALERT_TEXT_MAX, {"alert_probe_expect": 40}):
            dbm.set_setting(als.probe_expect_key(name), "2" * 40)
            _, shown = als._app_expectation(name)
            self.assertEqual(shown, "2" * 40)

    def test_长度上限跟着全局登记表放宽(self):
        """这条是㊿E的核心验收：路由里那份写死的 32 换成登记表的值之后，
        放宽 `alert_probe_expect` 时按应用覆写立刻同步放宽；旧写法会一边放宽一边继续拦。"""
        name = f"b50e-cap-{next(_seq)}"
        self._mkapp(name)
        key = als.probe_expect_key(name)
        maxlen = als.probe_expect_max()
        r = self.client.post("/api/settings/alerts", json={key: "2" * (maxlen + 1)}, headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn(str(maxlen), r.json()["error"])
        self.assertIsNone(dbm.get_setting(key), "拒绝时不许留下半个键")
        with mock.patch.dict(als.ALERT_TEXT_MAX, {"alert_probe_expect": maxlen + 8}):
            w = self.client.post("/api/settings/alerts", json={key: "2" * (maxlen + 1)},
                                 headers=self.h)
            self.assertEqual(w.status_code, 200, w.text)
            self.assertEqual(dbm.get_setting(key), "2" * (maxlen + 1))


if __name__ == "__main__":
    unittest.main()
