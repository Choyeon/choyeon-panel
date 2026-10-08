"""告警设置与通知通道的回归测试。

覆盖四个真实坏法：
1. 非整数阈值存进去后，每次巡检抛 ValueError 被循环吞掉 → 告警从此静默。
2. webhook 由用户填写后直接进 urllib.urlopen，`file://` 会让面板带着自身权限读本地文件。
3. 「发送测试通知」恒返回 ok，通道全挂用户也看不出来。
4. 投递失败仍写"今天已发"标记，那条告警当天永久丢失。
"""

import asyncio
import itertools
import os
import tempfile

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))

import unittest
from unittest import mock

from starlette.testclient import TestClient

from app import database as dbm
from app import main, security
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
             mock.patch.object(alerts_service, "disk_usage_pct", lambda p: 10):
            try:
                asyncio.run(alerts_service.run_checks())
            except Exception as e:  # noqa: BLE001
                self.fail(f"脏配置不该让整轮巡检崩溃：{type(e).__name__}: {e}")


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
