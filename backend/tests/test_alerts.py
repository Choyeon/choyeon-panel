"""告警设置、通知通道与告警历史的回归测试。

覆盖这些真实坏法：
1. 非整数阈值存进去后，每次巡检抛 ValueError 被循环吞掉 → 告警从此静默。
2. webhook 由用户填写后直接进 urllib.urlopen，`file://` 会让面板带着自身权限读本地文件。
3. 「发送测试通知」恒返回 ok，通道全挂用户也看不出来。
4. 投递失败仍写"今天已发"标记，那条告警当天永久丢失。
5. 告警历史：同一场持续中的告警每轮都插一行（历史表被刷满、看不出状态变化），
   以及恢复（crit → ok）不留痕——重启后看不出"现在到底有没有事"。
6. 存活探测把"连不上"和"应答了但状态码不好看"混为一谈，或者反过来：
   应用返回 404 也当成挂掉去推送通知。
7. 告警接口把 SQLite/探测的原始异常文本回吐给前端（绝对路径、errno 一起泄露）。
8. 资源阈值当轮就推送：snapshot() 的 CPU 是"距上次采样的差值"，一次备份压缩的瞬时
   尖峰就能把通知通道刷成噪音，而历史该记的却因去抖被一起延后（网页端看不见）。
"""

import asyncio
import itertools
import os
import sqlite3
import tempfile

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))

import unittest
from unittest import mock

from starlette.testclient import TestClient

from app import config, database as dbm, main, security
from app.routers import alerts as alerts_router
from app.services import alerts_service

ALERT_KEYS = ("alert_telegram_bot", "alert_telegram_chat", "alert_webhook_url",
              "alert_disk_pct", "alert_ssl_days")


_seq = itertools.count()


def _make_user(role: str = "admin") -> tuple[str, str]:
    user = f"alerts_{role}_{os.getpid()}_{next(_seq)}"
    dbm.execute(
        "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
        (user, security.hash_pass("Password123"), role),
    )
    return user, security.sign_token(user, role)


class _AuthedCase(unittest.TestCase):
    """公共夹具：建管理员、提供带鉴权的 POST，退出时清掉用户与告警配置。"""

    def setUp(self):
        self.client = TestClient(main.app)
        self.user, self.token = _make_user("admin")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.addCleanup(self._wipe_settings)

    def _wipe_settings(self):
        for k in ALERT_KEYS:
            dbm.execute("DELETE FROM settings WHERE key=?", (k,))

    def post_alerts(self, body: dict):
        return self.client.post("/api/settings/alerts", json=body,
                                headers={"Authorization": f"Bearer {self.token}"})


class TestAlertSettingsValidation(_AuthedCase):
    def test_valid_values_persist(self):
        r = self.post_alerts({"alert_disk_pct": "90", "alert_ssl_days": "7",
                              "alert_webhook_url": "https://hooks.example.com/x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.get_setting("alert_disk_pct"), "90")
        self.assertEqual(dbm.get_setting("alert_ssl_days"), "7")

    def test_non_numeric_threshold_is_rejected(self):
        """回归：存进 "abc" 之后每次巡检都抛 ValueError 被循环吞掉，告警全线静默失效。"""
        r = self.post_alerts({"alert_disk_pct": "abc"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(dbm.get_setting("alert_disk_pct"), "校验失败时不得写入")

    def test_out_of_range_threshold_is_rejected(self):
        for key, bad in (("alert_disk_pct", "5"), ("alert_ssl_days", "999")):
            r = self.post_alerts({key: bad})
            self.assertEqual(r.status_code, 400, f"{key}={bad} 应被拒绝: {r.text}")

    def test_batch_is_all_or_nothing(self):
        """一半合法一半非法时整批拒绝，避免配置进入说不清的中间态。"""
        r = self.post_alerts({"alert_ssl_days": "7", "alert_disk_pct": "999"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(dbm.get_setting("alert_ssl_days"))

    def test_webhook_scheme_whitelist(self):
        for bad in ("file:///etc/passwd", "ftp://1.2.3.4/x", "/relative/path", "http://"):
            r = self.post_alerts({"alert_webhook_url": bad})
            self.assertEqual(r.status_code, 400, f"{bad} 必须拒绝: {r.text}")
            self.assertIsNone(dbm.get_setting("alert_webhook_url"))
        r = self.post_alerts({"alert_webhook_url": "https://ok.example.com/hook"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_oversized_value_rejected(self):
        """bot token 有长度上限，超长写入只会是误粘贴（还会撑大 settings 表）。"""
        r = self.post_alerts({"alert_telegram_bot": "123456:" + "A" * 4000})
        self.assertEqual(r.status_code, 400, r.text)

    def test_empty_value_clears_setting(self):
        """清空 = 关闭该项通道，是既有语义，必须仍然允许写入。"""
        self.post_alerts({"alert_webhook_url": "https://ok.example.com/hook"})
        r = self.post_alerts({"alert_webhook_url": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(dbm.get_setting("alert_webhook_url"), "")

    def test_unknown_keys_ignored(self):
        r = self.post_alerts({"token_epoch": "evil", "alert_disk_pct": "88"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(dbm.get_setting("token_epoch"), "evil", "白名单外的 key 不得写入")
        self.assertEqual(dbm.get_setting("alert_disk_pct"), "88")

    def test_viewer_cannot_write(self):
        _, tok = _make_user("viewer")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username LIKE ?", ("alerts_viewer_%",))
        r = self.client.post("/api/settings/alerts", json={"alert_disk_pct": "70"},
                             headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 403)

    def test_run_checks_survives_bad_setting_written_by_other_path(self):
        """兜底：即便脏值经其它途径进了库（老数据迁移），巡检也不能整轮崩掉。"""
        dbm.set_setting("alert_disk_pct", "abc")
        dbm.set_setting("alert_ssl_days", "abc")
        with mock.patch.object(alerts_service, "run", _run_stub), \
             mock.patch.object(alerts_service, "run_probes", _no_probes), \
             mock.patch.object(alerts_service, "notify", _no_notify), \
             mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 10):
            try:
                asyncio.run(alerts_service.run_checks())
            except Exception as e:  # noqa: BLE001
                self.fail(f"脏配置不该让整轮巡检崩溃：{type(e).__name__}: {e}")


class TestAlertSettingsExtendedKeys(_AuthedCase):
    """阈值/探活/历史保留这几项是本轮新增的，白名单漏一个就会表现为"网页改了没生效"。"""

    EXTRA = ("alert_cpu_pct", "alert_mem_pct", "alert_load1_x", "alert_probe",
             "alert_probe_expect", "alert_keep_days", "alert_keep_rows")

    def setUp(self):
        super().setUp()
        self.addCleanup(self._wipe_extra)

    def _wipe_extra(self):
        dbm.execute("DELETE FROM settings WHERE key IN ({})".format(",".join("?" * len(self.EXTRA))),
                    self.EXTRA)
        dbm.execute("DELETE FROM settings WHERE key LIKE 'probe_expect:%'")

    def test_each_new_key_persists(self):
        for key, value in (("alert_cpu_pct", "75"), ("alert_mem_pct", "88"), ("alert_load1_x", "3"),
                           ("alert_probe", "0"), ("alert_probe_expect", "200 204"),
                           ("alert_keep_days", "45"), ("alert_keep_rows", "5000")):
            r = self.post_alerts({key: value})
            self.assertEqual(r.status_code, 200, f"{key}: {r.text}")
            self.assertEqual(dbm.get_setting(key), value)

    def test_out_of_range_new_keys_rejected(self):
        for key, bad in (("alert_cpu_pct", "120"), ("alert_load1_x", "0"),
                         ("alert_probe", "2"), ("alert_keep_days", "0"), ("alert_keep_rows", "9")):
            r = self.post_alerts({key: bad})
            self.assertEqual(r.status_code, 400, f"{key}={bad} 应被拒绝: {r.text}")
            self.assertIsNone(dbm.get_setting(key))

    def test_get_returns_defaults_and_overrides(self):
        self.post_alerts({"alert_cpu_pct": "70", "alert_probe": "0"})
        r = self.client.get("/api/settings/alerts",
                            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["alert_cpu_pct"], "70")
        self.assertEqual(body["alert_probe"], "0")
        self.assertIn("alert_keep_rows", body, "新增 key 必须出现在读取列表里，否则网页每次都显示默认值")
        self.assertEqual(body["probe_overrides"], {})

    def test_probe_override_needs_existing_app(self):
        r = self.post_alerts({"probe_expect:不存在的-应用": "200"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(dbm.get_setting("probe_expect:不存在的-应用"))

    def test_probe_override_roundtrip(self):
        name = f"probe_ut_{os.getpid()}_{next(_seq)}"
        app_id = dbm.execute(
            "INSERT INTO apps(name,type,repo_url,path,start_cmd,port) VALUES(?,?,?,?,?,?)",
            (name, "node", "https://example.invalid/r.git", f"/tmp/{name}", "node index.js", 3001),
        )
        self.addCleanup(dbm.execute, "DELETE FROM apps WHERE id=?", (app_id,))
        r = self.post_alerts({f"probe_expect:{name}": "200 204"})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings/alerts",
                              headers={"Authorization": f"Bearer {self.token}"}).json()
        self.assertEqual(got["probe_overrides"][name], "200 204")
        # 巡检侧读的就是这个键名，写读两侧必须一字不差
        self.assertEqual(dbm.get_setting(f"probe_expect:{name}"), "200 204")

    def test_probe_override_rejects_oversized_and_pathish_name(self):
        # 键名正则挡掉超长与带路径分隔符的输入：这类 key 永远读不到，写进库就是垃圾
        for bad_key in ("probe_expect:" + "A" * 80, "probe_expect:a/b", "probe_expect:带 空格"):
            r = self.post_alerts({bad_key: "200"})
            self.assertEqual(r.status_code, 200, "白名单外的 key 是静默忽略，不是报错")
            self.assertIsNone(dbm.get_setting(bad_key))


async def _run_stub(cmd, args, **kw):
    return {"code": 0, "out": ""}


class TestNotifyChannelSafety(unittest.TestCase):
    def test_file_url_is_never_fetched(self):
        """回归：urllib 默认支持 file://，webhook 填本地路径会被真读出来。"""
        fd, secret = tempfile.mkstemp(suffix=".leak")
        os.write(fd, b"TOP-SECRET")
        os.close(fd)
        self.addCleanup(os.unlink, secret)

        with mock.patch.object(dbm, "get_setting",
                               lambda k: f"file://{secret}" if k == "alert_webhook_url" else None):
            self.assertFalse(asyncio.run(alerts_service.notify("hello")), "file:// 不能算投递成功")

    def test_unreachable_and_malformed_urls_report_failure(self):
        for url in ("http://127.0.0.1:1/nope", "not-a-url", "https://"):
            with mock.patch.object(dbm, "get_setting",
                                   lambda k, u=url: u if k == "alert_webhook_url" else None):
                self.assertFalse(asyncio.run(alerts_service.notify("hi")), f"{url} 不该返回成功")

    def test_no_channel_configured(self):
        with mock.patch.object(dbm, "get_setting", lambda k: None):
            self.assertFalse(asyncio.run(alerts_service.notify("hi")))

    def test_swapped_telegram_fields_are_skipped(self):
        """bot token 与 chat id 填反时拼出来的是畸形 URL，跳过比发出去强。"""
        def gs(k):
            return {"alert_telegram_bot": "987654321",
                    "alert_telegram_chat": "123456:ABCDEFabcdef123456"}.get(k)

        with mock.patch.object(dbm, "get_setting", gs):
            self.assertFalse(asyncio.run(alerts_service.notify("hi")))

    def test_valid_telegram_shape_still_attempts(self):
        """形状合法就要真去发（这里用不可达地址，断言"确实尝试过"而非被跳过）。"""
        tried = []

        def fake_post(url, payload):
            tried.append(url)
            raise OSError("network down")

        with (mock.patch.object(dbm, "get_setting",
                                lambda k: "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                                if k == "alert_telegram_bot" else ("-100123" if k == "alert_telegram_chat" else None)),
              mock.patch.object(alerts_service, "_post", fake_post)):
            self.assertFalse(asyncio.run(alerts_service.notify("hi")))
        self.assertEqual(len(tried), 1, "合法配置应发起投递")
        self.assertIn("api.telegram.org", tried[0])


class TestAlertTestEndpoint(_AuthedCase):
    def test_returns_error_when_all_channels_fail(self):
        """回归：该接口曾恒返回 ok，用户以为通道已通，实际全挂。"""
        async def failing(_text):
            return False

        with mock.patch.object(alerts_service, "notify", failing):
            r = self.client.post("/api/settings/alerts/test",
                                 headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("error", r.json())

    def test_returns_ok_when_delivered(self):
        async def ok(_text):
            return True

        with mock.patch.object(alerts_service, "notify", ok):
            r = self.client.post("/api/settings/alerts/test",
                                 headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r.status_code, 200, r.text)


class TestAlertRetrySemantics(unittest.TestCase):
    def setUp(self):
        self.key = f"disk-test-{os.getpid()}"
        dbm.set_setting("sent:" + self.key, "")
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", ("sent:" + self.key,))

    def test_marker_only_written_on_success(self):
        async def failing(_text):
            return False

        with mock.patch.object(alerts_service, "notify", failing):
            asyncio.run(alerts_service._check(self.key, True, "boom"))
        self.assertNotEqual(dbm.get_setting("sent:" + self.key), alerts_service._today(),
                            "投递失败却标记已发 → 这条告警当天永久丢失")

        async def ok(_text):
            return True

        with mock.patch.object(alerts_service, "notify", ok):
            asyncio.run(alerts_service._check(self.key, True, "boom"))
        self.assertEqual(dbm.get_setting("sent:" + self.key), alerts_service._today())

    def test_already_sent_today_is_not_resent(self):
        sent = []

        async def spy(text):
            sent.append(text)
            return True

        dbm.set_setting("sent:" + self.key, alerts_service._today())
        with mock.patch.object(alerts_service, "notify", spy):
            asyncio.run(alerts_service._check(self.key, True, "boom"))
        self.assertEqual(sent, [], "同类告警每天最多一次")


# ---------- 告警历史 / 存活探测 ----------

def _alerts_app():
    """独立的告警路由应用。

    路由由协调方统一挂进 main.app，测试不去改生产 app 的路由表：静态目录挂在 "/" 上，
    `include_router` 追加的路由排在它后面就永远匹配不到（表现是 GET 404 / POST 405），
    而挪动 main.app.routes 属于碰共享状态。这里只挂 AuthMiddleware + 本流路由，
    鉴权口径（viewer 写操作 403、need_admin）与线上一致。
    """
    from fastapi import FastAPI

    from app.deps import AuthMiddleware

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(alerts_router.router)
    return app


def _rows(code: str) -> list[dict]:
    return dbm.query("SELECT * FROM alerts_log WHERE code=? ORDER BY id", (code,))


class _HistoryCase(_AuthedCase):
    """公共夹具：唯一 code、清掉该 code 的历史行与 sent: 标记，避免用例互相串。"""

    def setUp(self):
        self.client = TestClient(_alerts_app())
        self.user, self.token = _make_user("admin")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.addCleanup(self._wipe_settings)
        self.code = f"ut-{os.getpid()}-{next(_seq)}"
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (self.code,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("sent:ut-%",))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("streak:ut-%",))

    def headers(self, token: str | None = None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def get(self, path: str, token: str | None = None):
        return self.client.get(path, headers=self.headers(token))

    def post(self, path: str, token: str | None = None, body: dict | None = None):
        return self.client.post(path, json=body if body is not None else {}, headers=self.headers(token))


class TestAlertHistoryDedupe(_HistoryCase):
    def test_ongoing_alert_is_not_reinserted_each_round(self):
        """回归：状态没变也每轮插一行 → 历史表被 30 分钟一条的重复项刷满。"""
        async def ok(_t):
            return True

        async def rounds():
            for _ in range(3):
                await alerts_service._check(self.code, True, "CPU 持续超过阈值", code=self.code,
                                            level="crit", ok_message="CPU 正常")

        with mock.patch.object(alerts_service, "notify", ok):
            asyncio.run(rounds())
        rows = _rows(self.code)
        self.assertEqual(len(rows), 1, f"同一场告警只应留一行，实际 {len(rows)} 行")
        self.assertEqual(rows[0]["level"], "crit")

    def test_recovery_writes_ok_row(self):
        async def ok(_t):
            return True

        async def seq():
            await alerts_service._check(self.code, True, "磁盘超阈值", code=self.code,
                                        level="warn", ok_message="磁盘正常")
            await alerts_service._check(self.code, False, "", code=self.code,
                                        level="warn", ok_message="磁盘正常")

        with mock.patch.object(alerts_service, "notify", ok):
            asyncio.run(seq())
        rows = _rows(self.code)
        self.assertEqual([r["level"] for r in rows], ["warn", "ok"], "恢复必须留下一条 ok")
        # 再跑一轮正常巡检不该继续追加
        with mock.patch.object(alerts_service, "notify", ok):
            asyncio.run(alerts_service._check(self.code, False, "", code=self.code,
                                              level="warn", ok_message="磁盘正常"))
        self.assertEqual(len(_rows(self.code)), 2)

    def test_changed_message_is_a_new_event(self):
        async def ok(_t):
            return True

        async def seq():
            await alerts_service._check(self.code, True, "证书将在 3 天后到期", code=self.code,
                                        level="warn", ok_message="证书正常")
            await alerts_service._check(self.code, True, "证书已过期或无效", code=self.code,
                                        level="crit", ok_message="证书正常")

        with mock.patch.object(alerts_service, "notify", ok):
            asyncio.run(seq())
        rows = _rows(self.code)
        self.assertEqual([r["level"] for r in rows], ["warn", "crit"])

    def test_notify_behavior_unchanged_by_history_writes(self):
        """写历史不能改动投递语义：每天最多一次，且历史写入失败也不影响通知。"""
        sent = []

        async def spy(text):
            sent.append(text)
            return True

        async def rounds():
            for _ in range(2):
                await alerts_service._check(self.code, True, "boom", code=self.code)

        with (mock.patch.object(alerts_service, "notify", spy),
              mock.patch.object(alerts_service.aio_db, "query_one", self._boom_query)):
            asyncio.run(rounds())
        self.assertEqual(sent, ["boom"], "历史写库失败时仍要发通知，且只发一次")

    @staticmethod
    def _boom_query(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    def test_record_rejects_bad_level_and_empty_code(self):
        async def seq():
            return (await alerts_service.record("fatal", self.code, "x"),
                    await alerts_service.record("crit", "", "x"))

        self.assertEqual(asyncio.run(seq()), (None, None))
        self.assertEqual(_rows(self.code), [])


class TestAppProbe(_HistoryCase):
    """探测语义用假连接验证：绝不真连外部主机，也不在本机开监听端口。"""

    @staticmethod
    def _apps_query(name: str, port: int):
        async def fake(sql, params=()):
            if "FROM apps" in sql:
                return [{"id": 9001, "name": name, "port": port}]
            return []

        return fake

    @staticmethod
    def _streams(status_line: bytes):
        class _Reader:
            async def readline(self):
                return status_line

        class _Writer:
            def __init__(self):
                self.sent = b""

            def write(self, data):
                self.sent += data

            async def drain(self):
                pass

            def close(self):
                pass

        return _Reader(), _Writer()

    def test_refused_connection_is_crit(self):
        name = f"utapp{next(_seq)}"
        code = f"probe:{name}"
        sent = []

        async def refusing(*_a, **_kw):
            raise ConnectionRefusedError(111, "Connection refused")

        async def spy_notify(text):
            sent.append(text)
            return True

        with (mock.patch.object(alerts_service.aio_db, "query", self._apps_query(name, 39999)),
              mock.patch("asyncio.open_connection", refusing),
              mock.patch.object(alerts_service, "notify", spy_notify)):
            asyncio.run(alerts_service.run_probes())
        rows = _rows(code)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["level"], "crit")
        self.assertEqual(len(sent), 1, "连不上要推送通知")
        # 再探测一轮：同一场故障不追加历史行，通知仍受"每天一次"约束
        with (mock.patch.object(alerts_service.aio_db, "query", self._apps_query(name, 39999)),
              mock.patch("asyncio.open_connection", refusing),
              mock.patch.object(alerts_service, "notify", spy_notify)):
            asyncio.run(alerts_service.run_probes())
        self.assertEqual(len(_rows(code)), 1, "持续中的探测告警不得每轮重插")
        self.assertEqual(len(sent), 1, "已标记今天已发，第二轮不再推送")
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", (f"sent:probe-{name}",))

    def test_http_404_counts_as_alive(self):
        name = f"utapp{next(_seq)}"
        code = f"probe:{name}"
        sent = []

        async def notifying(text):
            sent.append(text)
            return True

        reader, writer = self._streams(b"HTTP/1.1 404 Not Found\r\n")

        async def connected(*_a, **_kw):
            return reader, writer

        with (mock.patch.object(alerts_service.aio_db, "query", self._apps_query(name, 39998)),
              mock.patch("asyncio.open_connection", connected),
              mock.patch.object(alerts_service, "notify", notifying)):
            results = asyncio.run(alerts_service.run_probes())
        self.assertTrue(results[0]["alive"], "应答了就不算挂")
        self.assertEqual(results[0]["status"], 404)
        self.assertEqual(sent, [], "状态码不符合预期只记历史，不推送通知")
        rows = _rows(code)
        self.assertEqual([r["level"] for r in rows], ["warn"], rows)
        self.assertNotIn("/root", rows[0]["message"])
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))

    def test_expected_status_writes_ok_and_dedupes(self):
        name = f"utapp{next(_seq)}"
        code = f"probe:{name}"
        reader, writer = self._streams(b"HTTP/1.1 200 OK\r\n")

        async def connected(*_a, **_kw):
            return reader, writer

        with (mock.patch.object(alerts_service.aio_db, "query", self._apps_query(name, 39997)),
              mock.patch("asyncio.open_connection", connected),
              mock.patch.object(alerts_service, "notify", mock.AsyncMock())):
            first = asyncio.run(alerts_service.run_probes())
            second = asyncio.run(alerts_service.run_probes())
        self.assertEqual(first[0]["level"], "ok")
        rows = _rows(code)
        self.assertEqual(len(rows), 1, "存活状态没变化就不该再多一行")
        self.assertEqual(rows[0]["level"], "ok")
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code=?", (code,))

    def test_per_app_expectation_override(self):
        name = f"utapp{next(_seq)}"
        dbm.set_setting(f"probe_expect:{name}", "4xx")
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", (f"probe_expect:{name}",))
        codes, text = alerts_service._app_expectation(name)
        self.assertIn(404, codes)
        self.assertNotIn(200, codes)
        self.assertEqual(text, "4xx")
        self.assertEqual(sorted(alerts_service._parse_expect("garbage")), sorted(range(200, 400)))


METRIC_CODES = ("disk", "cpu", "mem", "load1", "failed-units")


def _metric_rows() -> list[dict]:
    marks = ",".join("?" for _ in METRIC_CODES)
    return dbm.query(f"SELECT * FROM alerts_log WHERE code IN ({marks})", METRIC_CODES)


async def _no_probes():
    return []


async def _no_notify(_text):
    return False


def _spy_notify(sink: list):
    """记录文案但不算投递成功（返回 False）：写了 `sent:` 标记当天就不再重发，
    那样去抖的第二轮会被当日标记挡住，测不到"连续两轮才推"这件事本身。"""
    async def spy(text):
        sink.append(text)
        return False
    return spy


class TestRunChecksHistory(_HistoryCase):
    """整轮巡检的历史写入：状态不变不增行，状态变了才增行，恢复留 ok 痕。"""

    SNAP = {"cpu": 12.0, "memTotal": 1000, "memUsed": 200, "load": [0.4, 0.3, 0.2], "cpuCores": 4,
            # history 要两点：只有一点时 snapshot() 的 cpu 是"还没基线"，不是"0% 空闲"
            "history": [{"t": 0, "cpu": 11.0}, {"t": 3000, "cpu": 12.0}]}

    def setUp(self):
        super().setUp()
        # 别的用例（例如脏配置那轮 run_checks）也会写这五个 code，先清干净再断言行数
        dbm.execute("DELETE FROM alerts_log WHERE code IN (?,?,?,?,?)", METRIC_CODES)
        self.addCleanup(dbm.execute, "DELETE FROM alerts_log WHERE code IN (?,?,?,?,?)", METRIC_CODES)
        # 资源阈值的连续轮数计数存在 settings，留着会让下一个用例的第一轮就"已经攒够两轮"
        dbm.execute("DELETE FROM settings WHERE key LIKE ?", ("streak:%",))
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key LIKE ?", ("streak:%",))
        # 本类的 notify 桩一律返回 False（未投递），正常情况下不会产生 sent: 标记；
        # 这里再兜一层，免得将来改了桩的返回值就出现跨用例的"今天已发过"串味
        marks = ("sent:cpu", "sent:mem", "sent:load1", "sent:failed-units")
        dbm.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(marks))})", marks)
        self.addCleanup(dbm.execute, f"DELETE FROM settings WHERE key IN ({','.join('?' * len(marks))})",
                        marks)

    def _round(self, cpu: float = 12.0, sent: list | None = None):
        snap = dict(self.SNAP, cpu=cpu)
        notify = _no_notify if sent is None else _spy_notify(sent)

        async def fake_snap():
            return snap

        with (mock.patch.object(alerts_service, "run", _run_stub),
              mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 40),
              mock.patch.object(alerts_service, "run_probes", _no_probes),
              mock.patch.object(alerts_service, "notify", notify),
              mock.patch.object(alerts_service.system_metrics, "snapshot", fake_snap)):
            asyncio.run(alerts_service.run_checks())

    def test_no_baseline_sample_writes_no_resource_rows(self):
        """没有差值基线的那一轮（进程刚起来）cpu=0.0：既不报也不记"正常"。"""
        async def cold_snap():
            return dict(self.SNAP, cpu=99.0, history=[{"t": 0, "cpu": 0.0}])

        with (mock.patch.object(alerts_service, "run", _run_stub),
              mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 40),
              mock.patch.object(alerts_service, "run_probes", _no_probes),
              mock.patch.object(alerts_service, "notify", _no_notify),
              mock.patch.object(alerts_service.system_metrics, "snapshot", cold_snap)):
            asyncio.run(alerts_service.run_checks())
        self.assertEqual({r["code"] for r in _metric_rows()}, {"disk", "failed-units"},
                         "资源三项本轮没有可信读数，其它巡检照常留痕")

    def test_resource_notify_is_debounced(self):
        """瞬时尖峰只进历史不推送，连续两轮才发：备份压满 CPU 不该变成通知噪音。

        去抖只作用于推送。历史当轮就要有这一行，否则网页端的告警列表会对着一台
        正在烧 CPU 的机器显示"一切正常"。
        """
        sent: list[str] = []
        self._round(cpu=95.0, sent=sent)
        self.assertEqual(sent, [], "第一轮超标只该记历史，不该推送")
        self.assertEqual([r["level"] for r in _metric_rows() if r["code"] == "cpu"], ["warn"],
                         "历史必须当轮就留下 warn")
        self._round(cpu=96.0, sent=sent)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("96", sent[0], "推送带实时数值，历史那条是稳定文案")
        self.assertEqual([r["level"] for r in _metric_rows() if r["code"] == "cpu"], ["warn"],
                         "推没推送都不影响历史去重")
        # 恢复正常 → 计数归零，再来一轮尖峰要重新数满两轮
        self._round(cpu=5.0, sent=sent)
        self._round(cpu=97.0, sent=sent)
        self.assertEqual(len(sent), 1, "恢复后计数要清零，不能下一轮立刻又发一条")
        # 历史里要重新出现一行 warn：去重是跟「该 code 最近一行」比，不是「最近一行 warn」。
        # 否则一次「超标 → 恢复 → 又超标」在网页端只看得到前两次，第三次烧 CPU 无人知晓。
        self.assertEqual([r["level"] for r in _metric_rows() if r["code"] == "cpu"],
                         ["warn", "ok", "warn"])

    def test_clear_faults_are_not_debounced(self):
        """连不上/单元 failed 这类明确故障必须当轮就推，不等第二轮。"""
        sent: list[str] = []

        async def units(_cmd, _args, **_kw):
            return {"code": 0, "out": "  nginx.service loaded failed failed nginx\n"}

        async def fake_snap():
            return dict(self.SNAP)

        with (mock.patch.object(alerts_service, "run", units),
              mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 40),
              mock.patch.object(alerts_service, "run_probes", _no_probes),
              mock.patch.object(alerts_service, "notify", _spy_notify(sent)),
              mock.patch.object(alerts_service.system_metrics, "snapshot", fake_snap)):
            asyncio.run(alerts_service.run_checks())
        self.assertTrue(any("nginx.service" in t for t in sent), sent)

    def test_resources_are_thresholded_and_logged(self):
        """CPU/内存/load1 三项都在既有巡检集里，且默认阈值 80 / 90 / 2×核数。"""
        self._round()
        rows = {r["code"]: r for r in _metric_rows()}
        self.assertEqual(set(rows) & set(METRIC_CODES), {"disk", "cpu", "mem", "load1", "failed-units"},
                         "一轮巡检五项都要留痕")
        self.assertTrue(all(r["level"] == "ok" for r in rows.values()), rows)
        self.assertIn("80", rows["cpu"]["message"])
        self.assertIn("90", rows["mem"]["message"])
        # 4 核 ×2 = 8：load 阈值按核数换算，不是拍脑袋的固定值
        self.assertIn("8", rows["load1"]["message"])

    def test_ongoing_alert_does_not_grow_across_rounds(self):
        self._round(cpu=95.0)
        after_bad = len(_metric_rows())
        self._round(cpu=97.4)
        self._round(cpu=91.2)
        self.assertEqual(len(_metric_rows()), after_bad, "持续中的 CPU 告警不得每轮再插一行")
        cpu_rows = [r for r in _metric_rows() if r["code"] == "cpu"]
        # 资源阈值是"降级"不是"停机"，级别定 warn；crit 留给连不上/单元 failed/证书过期
        self.assertEqual([r["level"] for r in cpu_rows], ["warn"], cpu_rows)

    def test_recovery_after_high_cpu_appends_ok(self):
        self._round(cpu=95.0)
        self._round(cpu=9.0)
        cpu_rows = [(r["level"], r["message"]) for r in _metric_rows() if r["code"] == "cpu"]
        self.assertEqual([x[0] for x in cpu_rows], ["warn", "ok"], cpu_rows)
        # 再跑一轮仍然正常：ok 也不重复
        self._round(cpu=9.0)
        self.assertEqual(len([r for r in _metric_rows() if r["code"] == "cpu"]), 2)

    def test_threshold_from_settings_is_honoured(self):
        dbm.set_setting("alert_cpu_pct", "10")
        self.addCleanup(dbm.execute, "DELETE FROM settings WHERE key=?", ("alert_cpu_pct",))
        self._round(cpu=12.0)
        row = next(r for r in _metric_rows() if r["code"] == "cpu")
        self.assertEqual(row["level"], "warn", "阈值改成 10 之后 12% 就该报")
        self.assertEqual(alerts_service._int_setting("alert_cpu_pct", 80, 1, 99), 10)


class TestAlertHistoryEndpoints(_HistoryCase):
    def _insert(self, level: str, message: str, code: str | None = None) -> int:
        return dbm.execute(
            "INSERT INTO alerts_log(ts,level,code,message,meta) VALUES(datetime('now'),?,?,?,?)",
            (level, code or self.code, message, '{"value": 42}'),
        )

    def test_history_filters_and_caps_limit(self):
        for i in range(3):
            self._insert("crit", f"crit {i}")
        self._insert("ok", "一切正常")
        r = self.get(f"/api/alerts/history?code={self.code}")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(body["rows"]), 4)
        self.assertEqual(body["rows"][0]["level"], "ok", "默认新→旧")
        r = self.get(f"/api/alerts/history?code={self.code}&level=crit")
        self.assertEqual(len(r.json()["rows"]), 3)
        r = self.get("/api/alerts/history?limit=99999")
        self.assertLessEqual(len(r.json()["rows"]), 500)
        self.assertEqual(r.json()["limit"], 500)
        r = self.get("/api/alerts/history?limit=abc")
        self.assertEqual(r.json()["limit"], 100, "非数字回落默认值而不是 500")
        r = self.get("/api/alerts/history?level=bogus")
        self.assertEqual(r.status_code, 400, r.text)

    def test_open_lists_unacked_with_counts(self):
        self._insert("crit", "crit ongoing")
        self._insert("warn", "warn ongoing")
        r = self.get("/api/alerts/open")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(all(not x["acked"] for x in body["rows"]))
        mine = {x["level"] for x in body["rows"] if x["code"] == self.code}
        self.assertEqual(mine, {"crit", "warn"}, "本用例的两行都要出现在未确认列表里")
        self.assertGreaterEqual(body["counts"]["crit"], 1)
        self.assertGreaterEqual(body["counts"]["warn"], 1)
        self.assertEqual(body["total"], sum(body["counts"].values()))

    def test_ack_flips_only_the_intended_row(self):
        keep1 = self._insert("crit", "keep me unacked")
        target = self._insert("warn", "ack this one")
        keep2 = self._insert("ok", "and keep me too")
        r = self.post(f"/api/alerts/{target}/ack")
        self.assertEqual(r.status_code, 200, r.text)
        rows = {x["id"]: x["acked"] for x in _rows(self.code)}
        self.assertEqual(rows, {keep1: 0, target: 1, keep2: 0}, "只能翻转被点的那一行")
        # 重复确认是幂等的，不改动别人
        again = self.post(f"/api/alerts/{target}/ack")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertTrue(again.json()["already"])
        self.assertEqual({x["id"]: x["acked"] for x in _rows(self.code)}, rows)

    def test_ack_all_only_touches_unacked(self):
        acked_first = self._insert("crit", "already acked")
        dbm.execute("UPDATE alerts_log SET acked=1 WHERE id=?", (acked_first,))
        pending = [self._insert("warn", f"p{i}") for i in range(2)]
        r = self.post("/api/alerts/ack-all")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["acked"], 2, "至少覆盖本用例的两行")
        rows = {x["id"]: x["acked"] for x in _rows(self.code)}
        self.assertEqual([rows[i] for i in pending], [1, 1])
        self.assertEqual(rows[acked_first], 1, "已确认的行不该被动到")
        # 全表口径：ack-all 之后不该再有未确认行
        self.assertEqual(dbm.query_one("SELECT COUNT(*) c FROM alerts_log WHERE acked=0")["c"], 0)

    def test_ack_rejects_bad_id_and_missing_row(self):
        for bad in ("abc", "-1", "1.5", "%20"):
            r = self.post(f"/api/alerts/{bad}/ack")
            self.assertEqual(r.status_code, 400, f"{bad} 应 400: {r.text}")
            self.assertNotIn("/", r.json()["error"])
        r = self.post("/api/alerts/999999999/ack")
        self.assertEqual(r.status_code, 404, r.text)

    def test_viewer_cannot_ack_or_run(self):
        _, tok = _make_user("viewer")
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username LIKE ?", ("alerts_viewer_%",))
        self._insert("crit", "viewer should not ack")
        for path in (f"/api/alerts/{_rows(self.code)[0]['id']}/ack", "/api/alerts/ack-all",
                     "/api/alerts/run", "/api/alerts/probe"):
            r = self.post(path, token=tok)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")

    def test_run_reports_inserted_rows(self):
        async def quiet_checks():
            await alerts_service.record("crit", self.code, "本轮新增")

        with mock.patch.object(alerts_service, "run_checks", quiet_checks):
            r = self.post("/api/alerts/run")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["inserted"][0]["code"], self.code)

    def test_probe_endpoint_returns_results(self):
        async def fake_probes():
            return [{"name": "x", "port": 1, "alive": False, "status": None, "level": "crit"}]

        with mock.patch.object(alerts_service, "run_probes", fake_probes):
            r = self.post("/api/alerts/probe")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["results"][0]["level"], "crit")

    def test_writes_are_audited_with_ids_only(self):
        rid = self._insert("warn", "audit me")
        self.post(f"/api/alerts/{rid}/ack")
        last = dbm.query_one("SELECT * FROM audit ORDER BY id DESC LIMIT 1")
        self.assertEqual(last["action"], "alert:ack")
        self.assertEqual(last["detail"], str(rid), "审计只记对象 id，不记告警内容")

    def test_error_bodies_never_leak_paths(self):
        """回归：把 SQLite/巡检的原始异常文本回吐给前端会泄露绝对路径与文件名。"""
        secret = f"{config.DATA_DIR}/panel.db"

        async def boom_query(*_a, **_kw):
            raise sqlite3.OperationalError(f"unable to open database file: {secret}")

        async def boom_checks():
            raise RuntimeError(f"can't read {secret}")

        for path in ("/api/alerts/history", "/api/alerts/open"):
            with mock.patch.object(alerts_service.aio_db, "query", boom_query):
                r = self.get(path)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertNotIn("/", r.json()["error"], r.text)

        with mock.patch.object(alerts_service.aio_db, "query_one", boom_query):
            r = self.post("/api/alerts/ack-all")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn("/", r.json()["error"], r.text)

        with mock.patch.object(alerts_service, "run_checks", boom_checks):
            r = self.post("/api/alerts/run")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn("/", r.json()["error"], r.text)
        with mock.patch.object(alerts_service, "run_probes", boom_checks):
            r = self.post("/api/alerts/probe")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertNotIn("/", r.json()["error"], r.text)
