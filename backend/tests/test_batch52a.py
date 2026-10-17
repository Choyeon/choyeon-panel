"""批次52A：告警通知补上邮件（SMTP）通道。

面板的出站通知此前只有 Telegram 与通用 Webhook 两条，而 `POST /api/settings/alerts/test`
上面那条注释写着「它往管理员的邮箱/飞书里真发一条消息」——邮箱那半句是假的。1Panel 与
宝塔都把邮件当默认通道（很多人根本不装 Telegram），所以这一批补的是对标缺口，顺手把那句
过期说明兑现掉。

邮件比 http 通道多两处真实风险，测试就照着这两处放：
1. **凭据过明文网**。587/25 这类端口上，服务器没声明 STARTTLS 就直接 `login()` 等于把
   SMTP 口令广播给链路上任何一个抓包的人，而面板拿的是 root。所以这里选 fail-closed：
   不支持 STARTTLS 就一个字节都不发，而不是"降级发送"。
2. **配置写坏比不发更难发现**。填了主机漏了收件人时，"不发"是对的，"什么都不说"是错的；
   半配置必须留下一条日志，否则管理员会一直以为告警正在往邮箱里飞。

键名、掩码、长度、区间仍由 ㊿B 那张登记表给（本批只加行，不加名单）；邮件特有的语义
（哪个键是主机、哪个是地址列表）放在 `extra_validate()` 一个函数里，路由问它要答案。
"""

import asyncio
import itertools
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from app import database as dbm
from app import main, security
from app.services import alerts_service as als

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
SETTINGS_VUE = (BACKEND.parent / "web" / "src" / "views" / "Settings.vue").read_text(encoding="utf-8")
README = (BACKEND.parent / "README.md").read_text(encoding="utf-8")

_seq = itertools.count()

# 一份"配好了"的邮件配置：用例只改自己关心的那一项，其余照这份给。
# 正文一律用 ASCII——主题与正文走 MIME 编码，中文会被折成 =?utf-8?b?…?=，
# 那证明的是标准库的编码器，不是这里的通道选择。
GOOD = {
    "alert_email_host": "smtp.example.com",
    "alert_email_port": "587",
    "alert_email_from": "panel@example.com",
    "alert_email_to": "ops@example.com, oncall@example.com",
    "alert_email_user": "panel@example.com",
    "alert_email_pass": "app-password",
}
TEXT = "disk 91% over threshold 85%\nsecond line of the alert"
_UNSET = object()


def _table(case, overrides=None):
    """把 `dbm.get_setting` 换成一张内存表。

    这些用例要证的是"选了哪条通道、连出去长什么样"，不是设置表；用真库会把线上管理员
    的通道配置卷进测试里——跑一次测试就发一封真邮件。
    """
    table = dict(GOOD)
    table.update(overrides or {})
    patcher = mock.patch.object(dbm, "get_setting", lambda k, *d: table.get(k))
    patcher.start()
    case.addCleanup(patcher.stop)
    return table


class _Conn:
    """假 SMTP 连接：只记录调用顺序，服务器能力由类属性配。"""

    starttls_offered = True
    created = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        _Conn.created.append(self)

    def has_extn(self, name):
        return name == "starttls" and self.starttls_offered

    def starttls(self, *a, **kw):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def sendmail(self, sender, to, message):
        self.calls.append(("sendmail", sender, tuple(to), message))

    def quit(self):
        self.calls.append("quit")


class _Plain(_Conn):
    pass


class _Secure(_Conn):
    pass


def _names(conn):
    """调用序列里的事件名（login/sendmail 是元组，取第一位）。"""
    return [c[0] if isinstance(c, tuple) else c for c in conn.calls]


class ConnectShapeTests(unittest.TestCase):
    """连接形态：465 用隐式 TLS，其余端口 STARTTLS 谈不成就不发。"""

    def setUp(self):
        _Conn.created = []
        for target in (_Conn, _Plain, _Secure):
            patcher = mock.patch.object(target, "starttls_offered", True)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, fake in (("SMTP", _Plain), ("SMTP_SSL", _Secure)):
            patcher = mock.patch.object(als.smtplib, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _send(self, overrides=None, text=TEXT):
        """真走 `_send_email`，只是把 socket 换成记录器。"""
        _table(self, overrides)
        als._send_email(text)
        return _Conn.created[-1]

    def test_465端口走隐式TLS且不再starttls(self):
        conn = self._send({"alert_email_port": "465"})
        self.assertIsInstance(conn, _Secure, "465 必须用 SMTP_SSL：明文连上再升级是另一种协议")
        self.assertEqual(conn.host, "smtp.example.com")
        self.assertNotIn("starttls", _names(conn))
        self.assertEqual(_names(conn)[:2], ["login", "sendmail"])

    def test_其余端口先谈成STARTTLS再登录再发(self):
        conn = self._send()
        self.assertIsInstance(conn, _Plain)
        self.assertEqual(_names(conn), ["starttls", "login", "sendmail", "quit"])
        self.assertEqual(conn.calls[1], ("login", "panel@example.com", "app-password"))

    def test_服务器不支持STARTTLS时一个字节都不发(self):
        """fail-closed 的判据：口令在这种联机上 login() 就是广播，
        宁可这条告警发不出去，也不拿明文换"看起来发了"。"""
        with mock.patch.object(_Plain, "starttls_offered", False):
            _table(self)
            with self.assertRaises(ValueError) as got:
                als._send_email(TEXT)
        self.assertIn("STARTTLS", str(got.exception))
        conn = _Conn.created[-1]
        self.assertNotIn("sendmail", _names(conn))
        self.assertNotIn("login", _names(conn))

    def test_拒绝发送之后也会quit(self):
        """连接还开着没关，就是每轮巡检攒一个 socket。"""
        with mock.patch.object(_Plain, "starttls_offered", False):
            _table(self)
            with self.assertRaises(ValueError):
                als._send_email(TEXT)
        self.assertIn("quit", _names(_Conn.created[-1]))

    def test_没有用户名就不登录(self):
        """内网中继常常允许匿名投递，"用户名留空"必须是可用配置而不是报错。"""
        conn = self._send({"alert_email_user": "", "alert_email_pass": ""})
        self.assertNotIn("login", _names(conn))
        self.assertIn("sendmail", _names(conn))

    def test_收件人按分隔符拆开进envelope(self):
        conn = self._send({"alert_email_to": "a@example.com;b@example.com c@example.com"})
        sent = next(c for c in conn.calls if isinstance(c, tuple) and c[0] == "sendmail")
        self.assertEqual(sent[2], ("a@example.com", "b@example.com", "c@example.com"))

    def test_超时用的是通道预算而不是smtplib默认(self):
        """smtplib 默认没有超时：一台半死的邮件服务器能把整轮巡检挂在交付线程里。"""
        self.assertEqual(self._send({"alert_email_port": "465"}).timeout, als.NOTIFY_TIMEOUT)
        self.assertEqual(self._send().timeout, als.NOTIFY_TIMEOUT)

    def test_主题取正文首行而正文是全文(self):
        conn = self._send()
        sent = next(c for c in conn.calls if isinstance(c, tuple) and c[0] == "sendmail")
        self.assertEqual(sent[1], "panel@example.com")
        message = sent[3]
        self.assertIn("Subject: [choyeon-panel] disk 91% over threshold 85%", message)
        self.assertIn("second line of the alert", message)
        self.assertIn("ops@example.com", message)

    def test_配不全时根本不去连服务器(self):
        """没有主机就不要发起一次出站连接——那是一次由别人填的目标决定的握手。"""
        _table(self, {"alert_email_host": ""})
        with self.assertRaises(ValueError) as got:
            als._send_email(TEXT)
        self.assertIn("配置不全", str(got.exception))
        self.assertEqual(_Conn.created, [])


class NotifyBranchTests(unittest.TestCase):
    """`notify()` 里的邮件分支：成功算投递、失败留话、半配置不静默。"""

    def setUp(self):
        self.logs = []
        patcher = mock.patch.object(als, "warn", lambda *a: self.logs.append(" ".join(str(x) for x in a)))
        patcher.start()
        self.addCleanup(patcher.stop)
        # http 通道一律不换真网络：这些用例只关心邮件分支与 delivered 的组合。
        patcher = mock.patch.object(als, "_post", lambda url, payload: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, overrides=None, send=_UNSET):
        """`send` 不给就用真的 `_send_email`（半配置那条要证的就是它抛的错误）。"""
        _table(self, overrides)
        if send is _UNSET:
            return asyncio.run(als.notify("disk 91% over threshold 85%"))
        with mock.patch.object(als, "_send_email", send):
            return asyncio.run(als.notify("disk 91% over threshold 85%"))

    def test_只有邮件通道时它成功就是投递成功(self):
        self.assertTrue(self._run(send=lambda _t: None))

    def test_邮件失败但webhook成功仍然算已投递(self):
        """任一通道成功即成功：巡检据此才敢写"今天已发过"。全失败时那行标记不许写。"""
        def boom(_t):
            raise OSError("connection refused")
        self.assertTrue(self._run({"alert_webhook_url": "https://hooks.example.com/x"}, send=boom))

    def test_全部通道失败就是没投递成功(self):
        def boom(_t):
            raise OSError("connection refused")
        self.assertFalse(self._run(send=boom))

    def test_邮件失败的原因里不带口令(self):
        """SMTP 服务器的回显经常带上线信息，出模块前必须过一道脱敏再截断。"""
        def boom(_t):
            raise OSError("535 login refused for panel@example.com password=S3cret")
        self.assertFalse(self._run(send=boom))
        joined = " ".join(self.logs)
        self.assertIn("email notify failed", joined)
        self.assertNotIn("S3cret", joined)
        self.assertIn("password=***", joined)

    def test_什么都没填时不去连也不谎报(self):
        _Conn.created = []
        with mock.patch.object(als.smtplib, "SMTP", _Plain), \
             mock.patch.object(als.smtplib, "SMTP_SSL", _Secure):
            self.assertFalse(self._run({k: "" for k in als.EMAIL_TEXT_KEYS}))
            self.assertEqual(_Conn.created, [])
        self.assertNotIn("email notify failed", " ".join(self.logs))

    def test_填了一半会留下一条为什么没发(self):
        """填了主机漏了收件人是最常见的一种配错；静默不发等于让管理员一直等邮件。"""
        self.assertFalse(self._run({"alert_email_to": ""}))
        joined = " ".join(self.logs)
        self.assertIn("email notify failed", joined)
        self.assertIn("配置不全", joined)
        self.assertEqual(_Conn.created, [], "配置不全不该已经发起连接")


class RegistryDerivationTests(unittest.TestCase):
    """邮件那组的键名、掩码、区间仍然只有登记表一个出处。"""

    def test_邮件整组都按秘密下发(self):
        """产品决定：怎么连到管理员的邮件服务器整体算配置秘密，只读账号一律空串。"""
        email_keys = [k for k in als.alert_keys() if k.startswith(als.ALERT_EMAIL_PREFIX)]
        self.assertGreaterEqual(len(email_keys), 6, f"邮件键应当不止这 {len(email_keys)} 个")
        missing = [k for k in email_keys if k not in als.ALERT_SECRET_KEYS]
        self.assertEqual(missing, [], f"这些邮件键没进掩码名单：{missing}")

    def test_文本键由前缀派生(self):
        self.assertEqual(als.EMAIL_TEXT_KEYS,
                         tuple(k for k in als.ALERT_TEXT_MAX if k.startswith(als.ALERT_EMAIL_PREFIX)))

    def test_端口是数值键且默认值落在区间内(self):
        default, lo, hi = als.ALERT_LIMITS["alert_email_port"]
        self.assertEqual((default, lo, hi), (587, 1, 65535))
        self.assertNotIn("alert_email_port", als.ALERT_TEXT_MAX, "同一个键两张表，校验走哪条说不清")

    def test_被豁免的前缀常量下面确实有登记的键(self):
        """㊿B 那条「带引号的 alert_ 串必须是已登记的键」放行了 `*_PREFIX` 常量行；
        放行不等于闭眼——这类常量必须真对应着登记表里的键，否则豁免就成了
        「把一个拼错的键名藏在常量里」的通道。"""
        consts = []
        for p in sorted(APP.rglob("*.py")):
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r'^([A-Z][A-Z0-9_]*_PREFIX)\s*=\s*"(alert_[a-z0-9_]+)"', line)
                if m:
                    consts.append((p.name, m.group(1), m.group(2)))
        self.assertTrue(consts, "一条前缀常量都没扫到，多半是这里的匹配写坏了")
        for where, name, prefix in consts:
            hit = [k for k in als.alert_keys() if k.startswith(prefix)]
            self.assertTrue(hit, f"{where} 的 {name}=\"{prefix}\" 底下没有任何已登记的键")


class ShapeValidateTests(unittest.TestCase):
    """`extra_validate()`：邮件那组特有的语义，而且只管邮件那组。"""

    def _bad(self, key, val):
        return als.extra_validate(key, val)

    def test_正常值一律放行(self):
        for key, val in GOOD.items():
            if key == "alert_email_port":
                continue
            with self.subTest(key=key):
                self.assertIsNone(self._bad(key, val))

    def test_非邮件键这里一律不管(self):
        """越界校验会把 Telegram 与 webhook 的既有语义抢过来；两处规则一旦不同步，
        就成了「页面按 A 拦、巡检按 B 发」。"""
        self.assertIsNone(self._bad("alert_telegram_chat", "不是数字"))
        self.assertIsNone(self._bad("alert_webhook_url", "ftp://x/y"))
        self.assertIsNone(self._bad("alert_probe_expect", "任意文本"))

    def test_主机名里不许夹端口空白或尖括号(self):
        for bad in ("smtp.example.com:587", "smtp exam.com", "a@b.com", "<script>",
                    "smtp.example.com/", '"quoted"', "smtp\r\nX-Evil: 1"):
            with self.subTest(bad=bad):
                self.assertIsNotNone(self._bad("alert_email_host", bad), f"应当拒绝 {bad!r}")

    def test_发件人只能一个地址(self):
        self.assertIsNotNone(self._bad("alert_email_from", "a@example.com, b@example.com"))
        self.assertIsNotNone(self._bad("alert_email_from", "not-an-address"))

    def test_收件人数量有上限(self):
        ok = ", ".join(f"u{i}@example.com" for i in range(als.EMAIL_MAX_TO))
        self.assertIsNone(self._bad("alert_email_to", ok))
        over = ", ".join(f"u{i}@example.com" for i in range(als.EMAIL_MAX_TO + 1))
        self.assertIsNotNone(self._bad("alert_email_to", over))

    def test_畸形收件人只点那一个而不回显整串(self):
        err = self._bad("alert_email_to", "ops@example.com, <bounced@x")
        self.assertIsNotNone(err)
        self.assertIn("<bounced@x", err)
        self.assertNotIn("ops@example.com", err, "好的那一个不该被卷进报错里")

    def test_口令与用户名不许带换行(self):
        """SMTP 头部注入的入口就是"值里能塞 CRLF"；这里拦住，`sendmail`
        才拿不到可以伪造头行的字符串。"""
        self.assertIsNotNone(self._bad("alert_email_pass", "pw\r\nX-Evil: 1"))
        self.assertIsNotNone(self._bad("alert_email_user", "user\nX-Evil: 1"))

    def test_错误信息本身不带换行(self):
        """报错文本会回到页面与审计里，把粘贴进来的换行原样带回就是给自己挖坑。"""
        err = self._bad("alert_email_to", "ops@example.com\nbad\nline")
        self.assertIsNotNone(err)
        self.assertNotIn("\n", err)
        self.assertNotIn("\r", err)


def _make_user(role: str) -> tuple[str, dict]:
    user = f"b52a_{role}_{os.getpid()}_{next(_seq)}"
    dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                (user, security.hash_pass("Password123"), role))
    return user, {"Authorization": f"Bearer {security.sign_token(user, role)}"}


class RouteTests(unittest.TestCase):
    """线上行为：邮件键跟着登记表进 GET/POST，掩码与原子写都按既有口径。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def setUp(self):
        self.admin, self.h = _make_user("admin")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.admin,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE 'alert_email_%'")

    def test_邮件键出现在GET载荷里(self):
        g = self.client.get("/api/settings/alerts", headers=self.h).json()
        for key in als.alert_keys():
            if key.startswith(als.ALERT_EMAIL_PREFIX):
                self.assertIn(key, g, f"{key} 读不到，页面显示的就永远是默认值")
        self.assertIn("alert_email_port", g["limits"])

    def test_只读角色读不到邮件配置(self):
        for key, val in GOOD.items():
            dbm.set_setting(key, val)
        viewer, hv = _make_user("viewer")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (viewer,))
        body = self.client.get("/api/settings/alerts", headers=hv).json()
        leaked = {k: v for k, v in body.items()
                  if k.startswith(als.ALERT_EMAIL_PREFIX) and v != ""}
        self.assertEqual(leaked, {}, f"这些邮件配置对只读账号可见：{sorted(leaked)}")

    def test_管理员读到自己配的原值(self):
        dbm.set_setting("alert_email_host", "mail.internal.example")
        dbm.set_setting("alert_email_to", "ops@example.com")
        body = self.client.get("/api/settings/alerts", headers=self.h).json()
        self.assertEqual(body["alert_email_host"], "mail.internal.example")
        self.assertEqual(body["alert_email_to"], "ops@example.com")

    def test_畸形值被拒且一个键都没落库(self):
        """半套通道配置比拒绝更难解释：主机是新写的、收件人还是旧的。"""
        r = self.client.post("/api/settings/alerts",
                             json={"alert_email_host": "smtp.example.com:587",
                                   "alert_email_from": "panel@example.com"},
                             headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(dbm.get_setting("alert_email_host"))
        self.assertIsNone(dbm.get_setting("alert_email_from"))

    def test_空值仍然是关通道而不是报错(self):
        """既有语义：清空等于停用这条通道，邮件那组不许把它变成"必须先配全才能存"。"""
        dbm.set_setting("alert_email_to", "ops@example.com")
        r = self.client.post("/api/settings/alerts", json={"alert_email_to": ""}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.get_setting("alert_email_to"), "")

    def test_端口越界沿用登记表的区间(self):
        hi = als.ALERT_LIMITS["alert_email_port"][2]
        r = self.client.post("/api/settings/alerts", json={"alert_email_port": str(hi + 1)},
                             headers=self.h)
        self.assertEqual(r.status_code, 400, r.text)


class CopyAlignmentTests(unittest.TestCase):
    """页面与 README 说的话要和实现一致（㊼A 那一类的守卫，这次盯着通道清单）。"""

    def test_每个邮件配置项在设置页都有一行(self):
        for key in als.alert_keys():
            if not key.startswith(als.ALERT_EMAIL_PREFIX):
                continue
            with self.subTest(key=key):
                if key == "alert_email_port":
                    self.assertIn(f"commitNum('{key}'", SETTINGS_VUE,
                                  "端口是数值项，得走数字提交那条路")
                else:
                    self.assertIn(f"openChan('{key}')", SETTINGS_VUE, f"{key} 在登记表里却没有编辑入口")

    def test_前端标了秘密的键后端也按秘密处理(self):
        """抽屉里当密码框遮的东西，后端必须也不会把它发给只读账号；
        反向不要求——管理员自己配的主机名不必再对他遮一遍。"""
        front_secret = set(re.findall(
            r"^  (alert_[a-z0-9_]+): \{ label:.*secret: true", SETTINGS_VUE, re.M))
        self.assertTrue(front_secret, "CHAN_META 里没扫到 secret 标记，多半是那一块改写坏了")
        extra = front_secret - set(als.ALERT_SECRET_KEYS)
        self.assertEqual(extra, set(), f"页面当秘密、后端却明发给只读账号：{sorted(extra)}")

    def test_通道清单不再只说Telegram和Webhook(self):
        for src, name in ((SETTINGS_VUE, "Settings.vue"), (README, "README.md")):
            self.assertNotIn("Telegram Bot 或 Webhook 推送", src, f"{name} 还在说只有两条通道")
            self.assertIn("邮件", src, f"{name} 的通道清单里没有邮件")

    def test_明文风险写在页面上而不是只写在代码注释里(self):
        """STARTTLS 是硬约束：管理员把端口填成 25 之后只会得到"发了但收不到"，
        所以这一行的说明必须自己说清楚。"""
        self.assertIn("STARTTLS", SETTINGS_VUE)


if __name__ == "__main__":
    unittest.main()
