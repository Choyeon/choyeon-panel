"""批次⑲A：「还剩几天算快到期」这条线只有一个出处，且天数读不到不许当成"已恢复"。

这条线原本写着两份：`cert_ops.EXPIRING_DAYS = 21`（证书页标"即将到期"用它）和
告警配置 `alert_ssl_days`（默认 14，巡检决定发不发通知用它）。两份数字没有任何机制
一起改，坏法是镜像的两侧都觉得自己对：
- 把配置调到 30：第 30 天通知已经响了，证书页上还是"正常"，人以为面板没当回事；
- 把配置调到 7：证书页满屏"即将到期"，通知却一条都不发，红牌变成装饰；
- 配置调成 0（关掉证书提醒）：页面照样按 21 天标黄，看起来像提醒还在生效。

第二个缺陷更隐蔽。certbot 的输出里少数证书读不到剩余天数（旧版格式、`--staging`
之外的异常行），`parse_certbot` 老实地给 `days=None`，而巡检的分支只有
`invalid / days<=线 / else`，None 一路落进 else，于是往历史里写一行
「证书 X 有效期正常」；上一轮这张证书真报过警的话，还会补推一条「已恢复：证书 X
有效期正常」。同一张证书在站点页显示的是"未知"。读不到被说成了没事——而且是被
推送说成没事，用户不会再去翻页面。这批让 None 只记日志、不动档位，并把"读得到就照常
恢复"的路径一起锁住，别把恢复通知一起掐死。

用例里的数字全部显式钉住，不引用 `EXPIRING_DAYS` 之类的产品常量。
"""

import asyncio
import json
import os
import re
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_19a_"))

from starlette.testclient import TestClient  # noqa: E402

from app import (  # noqa: E402
    config,  # noqa: E402
    main,
    security,
)
from app import database as dbm  # noqa: E402
from app.services import alerts_service as als  # noqa: E402
from app.services import cert_ops  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
CERT_SRC = (SRC / "backend/app/services/cert_ops.py").read_text(encoding="utf-8")
ALERT_SRC = (SRC / "backend/app/services/alerts_service.py").read_text(encoding="utf-8")
SITES_SRC = (SRC / "backend/app/routers/sites.py").read_text(encoding="utf-8")
WEB_SRC = (SRC / "web/src/views/Sites.vue").read_text(encoding="utf-8")

# 巡检与证书页共用同一条线，但两边的数字必须来自配置而不是常量，所以这里给一批
# 与默认值(14)不同的显式配置，证明"读的是那个键"。
LINE = 25


def _cert(name, expiry_line):
    return f"Certificate Name: {name}\n  Domains: {name}\n  Expiry Date: {expiry_line}\n"


# 21 天：配置 25 → 该报；配置 14 → 不该报。这一张是"两个出处会给出不同答案"的样本。
CERT_21 = _cert("n19a21.example.com", "2026-10-31 00:00:00+00:00 (VALID: 21 days)")
# 天数读不到但也没标 INVALID：这一张是粉饰路径的样本。
CERT_NODAYS = _cert("n19aNd.example.com", "(VALID)")
# 过期无效：与天数无关，必须 crit。
CERT_EXPIRED = _cert("n19aEx.example.com", "2026-09-01 00:00:00+00:00 (INVALID: EXPIRED)")

SSL_CODES = ("ssl:n19a21.example.com", "ssl:n19aNd.example.com", "ssl:n19aEx.example.com")


def _run_stub(case, table, calls):
    """按命令名分发假子进程结果，同时记下被调用过什么（用来证明 0 时根本没查 certbot）。"""

    async def _run(cmd, args=(), **kw):
        calls.append(cmd)
        value = table.get(cmd)
        if value is None:
            return {"code": 127, "out": f"{cmd}: command not found"}
        return value if not callable(value) else value(args)

    return _run


def _clear_ssl(case):
    """本批写的历史与投递标记各归各的，不污染同库其它用例，也不继承它们的。"""

    def purge():
        dbm.execute("DELETE FROM alerts_log WHERE code IN ({})".format(",".join("?" * len(SSL_CODES))),
                    SSL_CODES)
        dbm.execute("DELETE FROM settings WHERE key LIKE ?", ("sent:ssl-%",))
        # 告警侧的 code 是 `ssl:<证书名>`，投递标记是 `sent:ssl-<证书名>`：分隔符不一样，
        # 写成 notified:ssl-% 一条也删不掉（LIKE 的参数化写法本身没问题，是模式对不上）。
        dbm.execute("DELETE FROM settings WHERE key LIKE ?", ("notified:ssl:%",))
        dbm.execute("DELETE FROM settings WHERE key IN (?,?)", ("alert_ssl_days", "alert_probe"))
    purge()
    case.addCleanup(purge)


def _set_line(case, value):
    dbm.set_setting("alert_ssl_days", str(value))


def _rows(code):
    return dbm.query("SELECT level, message, meta FROM alerts_log WHERE code=? ORDER BY id", (code,))


def _strip_comments(src):
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


class StatusOfLineTests(unittest.TestCase):
    """判定函数只认入参：线由调用方给，模块自己不藏数字。"""

    def test_the_verdict_moves_with_the_argument(self):
        self.assertEqual(cert_ops.status_of(21, 25), "expiring")
        self.assertEqual(cert_ops.status_of(21, 21), "expiring")
        self.assertEqual(cert_ops.status_of(21, 20), "ok")
        self.assertEqual(cert_ops.status_of(22, 21), "ok")

    def test_zero_line_disables_expiring_only(self):
        """0 = 关掉证书提醒，不是"所有证书都算快到期"。"""
        self.assertEqual(cert_ops.status_of(1, 0), "ok")
        self.assertEqual(cert_ops.status_of(0, 0), "ok")
        self.assertEqual(cert_ops.status_of(-1, 0), "expired")

    def test_missing_days_is_unknown_not_ok(self):
        self.assertEqual(cert_ops.status_of(None, 30), "unknown")
        self.assertEqual(cert_ops.status_of(None, 30, invalid=True), "expired")

    def test_the_negative_days_still_wins_over_the_line(self):
        # 过期了就是过期了，即使配置线很大也不能只标"即将到期"。
        self.assertEqual(cert_ops.status_of(-3, 60), "expired")


class SingleSourceGuardTests(unittest.TestCase):
    """源码级守卫：第二份数字一出现就红，不用等线上差出一周。"""

    def test_cert_ops_no_longer_defines_its_own_constant(self):
        self.assertNotIn("EXPIRING_DAYS", CERT_SRC, "证书页那份 21 天必须是别处来的")
        self.assertIn('setting_int("alert_ssl_days")', CERT_SRC)

    def test_every_status_of_call_passes_the_line(self):
        # 模块文档里也写着 status_of()，那是引用不是调用；只看带参数的调用点。
        calls = [c for c in re.findall(r"(?<!def )status_of\(([^)]*)\)", CERT_SRC) if c.strip()]
        self.assertGreaterEqual(len(calls), 2, "certbot 与 openssl 两条来源都要判定")
        for arg in calls:
            self.assertGreaterEqual(len([p for p in arg.split(",") if p.strip()]), 2,
                                    f"status_of({arg}) 少传线，就会退回某个写死的数字")

    def test_the_ssl_route_does_not_recopy_the_field(self):
        # ⑰B 的老毛病：服务层算好了，路由又自己拼一遍，两份数字在响应里打架。
        self.assertNotIn('"expiring_days"', SITES_SRC, "应由 list_certs() 的载荷带出，不在路由重拼")
        self.assertIn("**data", SITES_SRC)

    def test_the_inspection_branch_has_no_renamed_leftover(self):
        """巡检那段里曾有 `low = ssl_days` 之类的第二跳板；配置线只许直接用 setting_int 的值。"""
        head = ALERT_SRC.index('ssl_days = setting_int("alert_ssl_days")')
        tail = ALERT_SRC.index("await _check_resources()", head)
        self.assertNotIn("low =", ALERT_SRC[head:tail], "多一个中间变量就多一份可能被改的数")
        self.assertIn("c[\"days\"] <= ssl_days", ALERT_SRC[head:tail])


class InspectionSkipTests(unittest.TestCase):
    """run_checks 的证书分支：读不到天数的证书不许进任何一档。"""

    def setUp(self):
        _clear_ssl(self)
        # alert_probe=0：这批不测探活，别让 run_probes 的投递混进 notify 记录。
        dbm.set_setting("alert_probe", "0")
        self.sent = []

    async def _notify(self, text):
        self.sent.append(text)
        return True

    async def _no_resources(self):
        return None

    def _round(self, out):
        calls = []
        table = {
            "certbot": {"code": 0, "out": out},
            "systemctl": {"code": 0, "out": ""},
        }
        with (mock.patch.object(als, "run", _run_stub(self, table, calls)),
              mock.patch.object(als, "disk_usage_pct", lambda p: 10),
              mock.patch.object(als, "_check_resources", self._no_resources),
              mock.patch.object(als, "run_probes", self._no_resources),
              mock.patch.object(als, "notify", self._notify)):
            asyncio.run(als.run_checks())
        return calls

    def _certs_sent(self):
        """只看本批三张证书相关的投递：同一轮里磁盘/failed 单元也可能补发恢复，那与这批无关。"""
        return [t for t in self.sent if "n19a" in t]

    def test_unreadable_days_writes_no_ok_history(self):
        _set_line(self, LINE)
        # 上一轮确实报过警：先留一行 warn，模拟"这张证书本来在被盯着"。
        dbm.execute("INSERT INTO alerts_log(ts,level,code,message,meta) "
                    "VALUES(datetime('now'),'warn','ssl:n19aNd.example.com','证书剩余天数曾超阈值','')")
        dbm.set_setting("notified:ssl:n19aNd.example.com", "2026-10-13")
        self._round(CERT_NODAYS)
        rows = _rows("ssl:n19aNd.example.com")
        self.assertEqual([r["level"] for r in rows], ["warn"],
                         "读不到天数却落一行 ok，告警列表就从「还在盯着」翻成「没事了」")
        self.assertEqual(self._certs_sent(), [],
                         f"给一张状态未知的证书推「已恢复」是把没测到说成测好了：{self._certs_sent()}")

    def test_the_alert_flag_survives_the_skipped_round(self):
        """不发恢复就得留着 notified 标记，否则下一次真恢复时也不发了。"""
        _set_line(self, LINE)
        dbm.set_setting("notified:ssl:n19aNd.example.com", "2026-10-13")
        self._round(CERT_NODAYS)
        self.assertEqual(dbm.get_setting("notified:ssl:n19aNd.example.com"), "2026-10-13")

    def test_a_later_readable_round_still_recovers(self):
        """跳过不是取消恢复：天数能读出来且已回到线上，照旧落 ok 并补一条「已恢复」。"""
        _set_line(self, LINE)
        dbm.execute("INSERT INTO alerts_log(ts,level,code,message,meta) "
                    "VALUES(datetime('now'),'warn','ssl:n19aNd.example.com','证书将在 3 天后到期','')")
        dbm.set_setting("notified:ssl:n19aNd.example.com", "2026-10-13")
        self._round(CERT_NODAYS)
        self.assertEqual(self._certs_sent(), [])
        self._round(_cert("n19aNd.example.com", "2027-06-01 00:00:00+00:00 (VALID: 230 days)"))
        self.assertEqual(_rows("ssl:n19aNd.example.com")[-1]["level"], "ok")
        recovered = [t for t in self._certs_sent() if t.startswith("已恢复")]
        self.assertEqual(len(recovered), 1, self._certs_sent())
        self.assertIn("有效期正常", recovered[0])
        self.assertFalse(dbm.get_setting("notified:ssl:n19aNd.example.com"),
                         "恢复发过就要把标记清成假值（产品写法是置空不是删行），不清会在 settings 里越积越多")

    def test_breach_uses_the_configured_line_in_the_message(self):
        _set_line(self, LINE)
        self._round(CERT_21)
        sent = [t for t in self._certs_sent() if "n19a21" in t]
        self.assertEqual(len(sent), 1, self._certs_sent())
        self.assertIn(f"阈值 {LINE} 天", sent[0])
        row = _rows("ssl:n19a21.example.com")[-1]
        self.assertEqual(row["level"], "warn")
        self.assertEqual(json.loads(row["meta"])["threshold"], LINE)

    def test_a_cert_above_the_line_is_not_alerted(self):
        _set_line(self, 7)
        self._round(CERT_21)
        self.assertEqual([t for t in self._certs_sent() if "n19a21" in t], [],
                         "线调到 7 之后这张 21 天的证书就不该发通知了——页面也必须同样不标黄")

    def test_invalid_cert_is_crit_regardless_of_days(self):
        _set_line(self, LINE)
        self._round(CERT_EXPIRED)
        self.assertEqual(_rows("ssl:n19aEx.example.com")[-1]["level"], "crit")

    def test_other_certs_are_still_bucketed_when_one_is_unreadable(self):
        """一张读不到不能带崩整轮：同批其它证书照常分档。"""
        _set_line(self, LINE)
        self._round(CERT_NODAYS + "\n" + CERT_21 + "\n" + CERT_EXPIRED)
        self.assertEqual(_rows("ssl:n19aNd.example.com"), [])
        self.assertEqual(_rows("ssl:n19a21.example.com")[-1]["level"], "warn")
        self.assertEqual(_rows("ssl:n19aEx.example.com")[-1]["level"], "crit")

    def test_the_round_still_finishes_when_a_cert_is_unreadable(self):
        """跳过一张不等于跳过整轮：同轮里另一张该报的照样报（防止 continue 写成 return）。"""
        _set_line(self, LINE)
        self._round(CERT_NODAYS + "\n" + CERT_EXPIRED)
        self.assertEqual(_rows("ssl:n19aEx.example.com")[-1]["level"], "crit")
        self.assertEqual(len([t for t in self._certs_sent() if "n19aEx" in t]), 1, self._certs_sent())


class ZeroLineInspectionTests(unittest.TestCase):
    """`alert_ssl_days=0` 的巡检语义：整段跳过。"""

    def setUp(self):
        _clear_ssl(self)
        dbm.set_setting("alert_ssl_days", "0")
        dbm.set_setting("alert_probe", "0")

    def test_zero_skips_the_certificate_check(self):
        seen = []

        async def spy(cmd, args=(), **kw):
            seen.append(cmd)
            return {"code": 0, "out": CERT_21} if cmd == "certbot" else {"code": 0, "out": ""}

        async def _notify(_t):
            return True

        async def _none():
            return None

        with (mock.patch.object(als, "run", spy),
              mock.patch.object(als, "disk_usage_pct", lambda p: 10),
              mock.patch.object(als, "_check_resources", _none),
              mock.patch.object(als, "run_probes", _none),
              mock.patch.object(als, "notify", _notify)):
            asyncio.run(als.run_checks())
        self.assertNotIn("certbot", seen)
        self.assertEqual(_rows("ssl:n19a21.example.com"), [])


class PageAndSettingAgreeTests(unittest.TestCase):
    """同一个配置值必须同时决定页面档位与巡检通知——这条才是"两个出处"的真正解药。"""

    NOW = datetime(2026, 10, 10, tzinfo=UTC)   # 2026-10-31 距此 21 天

    def setUp(self):
        _clear_ssl(self)

    def _page_status(self, line):
        _set_line(self, line)
        table = {"certbot": {"code": 0, "out": CERT_21}}
        calls = []
        with mock.patch.object(cert_ops, "run", _run_stub(self, table, calls)):
            data = asyncio.run(cert_ops.list_certs(self.NOW))
        return data["certs"][0]["status"], data["expiring_days"]

    def _notified(self, line):
        _set_line(self, line)   # 与页面读同一个键：不让"页面用了 7、巡检用了上一个用例的 25"这种事溜过去
        sent = []

        async def _notify(t):
            sent.append(t)
            return True

        async def _none():
            return None

        dbm.set_setting("alert_probe", "0")
        table = {"certbot": {"code": 0, "out": CERT_21}, "systemctl": {"code": 0, "out": ""}}
        calls = []
        with (mock.patch.object(als, "run", _run_stub(self, table, calls)),
              mock.patch.object(als, "disk_usage_pct", lambda p: 10),
              mock.patch.object(als, "_check_resources", _none),
              mock.patch.object(als, "run_probes", _none),
              mock.patch.object(als, "notify", _notify)):
            asyncio.run(als.run_checks())
        return [t for t in sent if "n19a21" in t]

    def test_line_25_marks_the_page_and_alerts(self):
        self.assertEqual(self._page_status(25), ("expiring", 25))
        self.assertEqual(len(self._notified(25)), 1, "页面标黄的这张必须真的发通知")

    def test_line_7_neither_marks_the_page_nor_alerts(self):
        self.assertEqual(self._page_status(7), ("ok", 7))
        self.assertEqual(self._notified(7), [], "页面不标黄的这张必须不发通知")

    def test_line_0_disables_both(self):
        self.assertEqual(self._page_status(0), ("ok", 0))
        self.assertEqual(self._notified(0), [])


class SslCertsPayloadTests(unittest.TestCase):
    """接口把线带给浏览器，前端不再自己写数字。"""

    def setUp(self):
        _clear_ssl(self)
        self.client = TestClient(main.app)
        user = "u19a_ssl_viewer"
        dbm.execute("INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
                    (user, security.hash_pass("Password123"), "admin"))
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (user,))
        self.token = security.sign_token(user, "admin")

    def _get(self):
        return self.client.get("/api/ssl/certs",
                               headers={"Authorization": f"Bearer {self.token}"}).json()

    def test_the_route_carries_expiring_days(self):
        _set_line(self, LINE)
        table = {"certbot": {"code": 0, "out": CERT_21}}
        with mock.patch.object(cert_ops, "run", _run_stub(self, table, [])):
            body = self._get()
        self.assertEqual(body["expiring_days"], LINE)
        self.assertEqual(body["source"], "certbot")
        self.assertEqual(body["certs"][0]["status"], "expiring")

    def test_the_payload_always_has_the_line_even_when_empty(self):
        _set_line(self, 9)
        table = {"certbot": {"code": 1, "out": "boom"}}
        with mock.patch.object(cert_ops, "run", _run_stub(self, table, [])), \
             mock.patch.object(config, "TLS_DIR", "/nonexistent-cp-19a"):
            body = self._get()
        self.assertEqual(body["certs"], [])
        self.assertEqual(body["expiring_days"], 9, "一张证书都没有时页面也得知道按哪条线判的")


class FrontendSingleSourceTests(unittest.TestCase):
    """站点页只渲染后端给的那条线。"""

    def test_sites_page_reads_the_payload_value(self):
        self.assertIn("certsInfo.expiring_days", WEB_SRC)
        self.assertIn("剩余 ≤ ${certsInfo.expiring_days} 天", WEB_SRC)

    def test_sites_page_has_no_hardcoded_day_count(self):
        src = _strip_comments(WEB_SRC)
        # \b 让 width:210 这类无关数字不算命中；命中的只会是"天数"字面量。
        hit = re.findall(r"\b21\b", src)
        self.assertEqual(hit, [], "页面里的天数只许来自 certsInfo.expiring_days")
        self.assertNotIn("EXPIRING_DAYS", src)


if __name__ == "__main__":
    unittest.main()
