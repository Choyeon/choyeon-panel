"""批次93：authorized_keys 扫描的两处静默截断都要自报「没扫到底」。

登录安全这一层是全站唯一"必须只读"的对账，它给出的最要命的一句话其实是负向的——
"这台机器的已授权钥匙就这些，没有别的"。可原来有两处会把这句话变成假话：
① 某个 authorized_keys 正文触到 `_read_text` 的字符上限被切掉（后面的钥匙没数进总数，
   切点还可能劈开一把钥匙）；② 用户目录读满 MAX_FILES 份后 break，剩余用户压根没读。
两处过去都静默。这里补 `partial_files` / `files_capped` / `complete`，让 CLI、自检与
登录安全页签在读到这个下限时如实说"没扫到底"，而不是拿一个下限冒充扫全了（同一批
账号表 scan_partial 的纪律）。测试全部走真实临时文件，不 mock lstat/open。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app.services import login_audit_service as svc  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
VUE = REPO / "web" / "src" / "views" / "Services.vue"


def _blob(algo: str, *fields: bytes) -> str:
    parts = [algo.encode(), *fields]
    return base64.b64encode(b"".join(struct.pack(">I", len(p)) + p for p in parts)).decode()


def _key_line(comment="laptop"):
    b64 = _blob("ssh-ed25519", b"K" * 32)
    return f"ssh-ed25519 {b64} {comment}", b64


LINE, B64 = _key_line()


def _users(root: Path, names: list[str]):
    """每个用户一个 home/.ssh/authorized_keys（真实文件，权限 600），返回喂给扫描的 users 表。"""
    out = []
    for i, name in enumerate(names):
        home = root / "home" / name
        ssh = home / ".ssh"
        ssh.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o755)
        os.chmod(ssh, 0o700)
        ak = ssh / "authorized_keys"
        ak.write_text(LINE + "\n", encoding="utf-8")
        os.chmod(ak, 0o600)
        out.append({"name": name, "uid": 1500 + i, "home": str(home),
                    "shell": "/bin/bash", "login": True})
    return out


class CharClipTests(unittest.TestCase):
    """① 单文件字符上限：读满就标 partial，切半的钥匙不计成一把。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _scan_small_cap(self, cap):
        users = _users(self.root, ["bob"])
        # 把单文件上限压到恰好能读若干字符：真实文件里两行钥匙，cap 只够读一部分。
        body = LINE + "\n" + LINE + "\n"
        (self.root / "home" / "bob" / ".ssh" / "authorized_keys").write_text(body, encoding="utf-8")
        with mock.patch.object(svc, "AK_MAX_CHARS", cap):
            return svc._authorized_keys(users, True)

    def test_full_file_is_complete(self):
        users = _users(self.root, ["bob"])
        out = svc._authorized_keys(users, True)
        self.assertTrue(out["complete"])
        self.assertEqual(out["partial_files"], [])
        self.assertEqual(out["total_keys"], 1)

    def test_char_cap_marks_the_file_partial(self):
        # cap = 一整行 + 换行：读回来恰好一行，正文触到这把尺 → 没读到底，第二把钥匙看不见。
        out = self._scan_small_cap(len(LINE) + 1)
        self.assertFalse(out["complete"])
        self.assertEqual(out["partial_files"], ["bob"])
        f = out["files"][0]
        self.assertTrue(f["partial"])
        self.assertTrue(any("没读到底" in x for x in f["flags"]))

    def test_count_is_honest_lower_bound_not_a_phantom(self):
        # cap 落在一行中间（半把钥匙）：那截残缺不能算成一把，也不能凭空报出。
        out = self._scan_small_cap(len(LINE) + 5)
        f = out["files"][0]
        self.assertTrue(f["partial"])
        self.assertEqual(f["count"], 1, "切在半把钥匙上的那截残行要丢掉，只数完整行")
        self.assertEqual(out["total_keys"], 1)

    def test_no_key_body_leaks(self):
        out = self._scan_small_cap(len(LINE) + 5)
        self.assertNotIn(B64, json.dumps(out, ensure_ascii=False))


class MaxFilesTests(unittest.TestCase):
    """② 用户目录份数上限：读满 MAX_FILES 份后没读的那段要如实标出来。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_more_users_than_cap_is_incomplete(self):
        users = _users(self.root, ["u1", "u2", "u3", "u4"])
        with mock.patch.object(svc, "MAX_FILES", 2):
            out = svc._authorized_keys(users, True)
        self.assertTrue(out["files_capped"])
        self.assertFalse(out["complete"])
        self.assertEqual(out["scanned"], 2, "只读了前 2 份")

    def test_exactly_at_cap_is_still_complete(self):
        users = _users(self.root, ["u1", "u2"])
        with mock.patch.object(svc, "MAX_FILES", 2):
            out = svc._authorized_keys(users, True)
        self.assertFalse(out["files_capped"])
        self.assertTrue(out["complete"])
        self.assertEqual(out["total_keys"], 2)


class CollectNoteTests(unittest.TestCase):
    def _scaffold(self, keys):
        ssh = {"source": "sshd -T", "effective": True, "values": {}, "notes": []}
        guard = {"status": "guarded", "phrase": "在跑", "units": [], "note": None}
        att = {"ok": True, "failed": 0, "distinct_ips": 0, "top": [], "accepted": [],
               "accepted_ips": [], "truncated": False, "window_label": "过去 24 小时", "note": None}
        panel = {"ok": True, "accounts": 0, "with_totp": 0, "recent_failed": 0, "note": None}

        async def r1():
            return ssh

        async def r2():
            return guard

        async def r3():
            return att

        async def ports():
            return {"ports": []}

        return (mock.patch.object(svc, "_sshd_settings", r1),
                mock.patch.object(svc, "_passwd_users", lambda: ([], True)),
                mock.patch.object(svc, "_guard_status", r2),
                mock.patch.object(svc, "_attempts", r3),
                mock.patch.object(svc, "_panel_2fa", lambda: panel),
                mock.patch.object(svc, "_authorized_keys", lambda users, as_root: keys),
                mock.patch.object(svc.exposure_service, "audit", ports))

    def test_partial_scan_reaches_the_page_notes(self):
        keys = {"files": [], "scanned": 1, "total_keys": 40, "truncated": False,
                "partial_files": ["bob"], "files_capped": True, "complete": False}
        patches = self._scaffold(keys)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            got = asyncio.run(svc._collect())
        self.assertFalse(got["keys"]["complete"])
        note = next((n for n in got["notes"] if "已授权钥匙" in n and "没扫到底" in n), "")
        self.assertTrue(note, "没扫到底要进整页 notes")
        self.assertIn("bob", note)

    def test_complete_scan_adds_no_such_note(self):
        keys = {"files": [], "scanned": 3, "total_keys": 3, "truncated": False,
                "partial_files": [], "files_capped": False, "complete": True}
        patches = self._scaffold(keys)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            got = asyncio.run(svc._collect())
        self.assertFalse(any("已授权钥匙" in n and "没扫到底" in n for n in got["notes"]))


class DoctorItemTests(unittest.TestCase):
    def data(self, keys):
        return {
            "sshd": {"source": "sshd -T", "port": 22, "reachable": False},
            "counts": {"risk": 0, "warn": 0, "note": 0},
            "findings": [],
            "accounts": {"login_shells": ["root"], "login_count": 1, "uid0_extra": [],
                         "empty_password": [], "shadow_unreadable": False,
                         "sudo_groups": {}, "scan_partial": [], "complete": True, "notes": []},
            "guard": {"status": "guarded", "phrase": "在跑"},
            "attempts": {"ok": True, "failed": 3, "truncated": False, "window_label": "过去 24 小时"},
            "keys": keys,
            "notes": [],
            "as_root": True,
        }

    def test_partial_files_is_not_a_green_light(self):
        got = svc.doctor_item(self.data(
            {"total_keys": 40, "scanned": 1, "complete": False,
             "partial_files": ["bob"], "files_capped": False}))
        self.assertEqual(got["status"], "warn")
        self.assertIn("已授权钥匙", got["detail"])
        self.assertIn("bob", got["detail"])

    def test_files_capped_is_not_a_green_light(self):
        got = svc.doctor_item(self.data(
            {"total_keys": 9, "scanned": 64, "complete": False,
             "partial_files": [], "files_capped": True}))
        self.assertEqual(got["status"], "warn")
        self.assertIn(str(svc.MAX_FILES), got["detail"])

    def test_complete_scan_is_still_green(self):
        got = svc.doctor_item(self.data(
            {"total_keys": 3, "scanned": 3, "complete": True,
             "partial_files": [], "files_capped": False}))
        self.assertEqual(got["status"], "pass")

    def test_fixture_without_the_new_key_is_not_downgraded(self):
        """老替身里的 keys 没有 complete 这一键，不能被当成"没扫完"而凭空加 warn。"""
        got = svc.doctor_item(self.data({"total_keys": 2, "scanned": 2, "truncated": False}))
        self.assertEqual(got["status"], "pass")

    def test_breaking_in_is_still_fail_before_the_partial_note(self):
        d = self.data({"total_keys": 2, "scanned": 2, "complete": False,
                       "partial_files": ["bob"], "files_capped": False})
        d["accounts"]["uid0_extra"] = ["ghost"]
        # 入侵迹象优先：complete 的 warn 不能盖过 fail。
        self.assertEqual(svc.doctor_item(d)["status"], "fail")


class SshAuditTextTests(unittest.TestCase):
    def _d(self, keys):
        return {"sshd": {"source": "sshd -T", "port": 22, "reachable": False},
                "attempts": {"ok": True, "failed": 1, "distinct_ips": 1, "window_label": "过去 24 小时"},
                "counts": {"risk": 0, "warn": 0, "note": 0},
                "findings": [], "settings": [],
                "keys": keys,
                "accounts": {"login_count": 1, "empty_password": [], "uid0_extra": [], "sudo_groups": {}}}

    def test_cli_prints_the_lower_bound_caveat_when_incomplete(self):
        text = cli._ssh_audit_text(self._d(
            {"total_keys": 40, "scanned": 1, "truncated": False,
             "partial_files": ["bob"], "files_capped": False, "complete": False}))
        self.assertIn("这是下限", text)
        self.assertIn(str(svc.MAX_FILES), text)

    def test_cli_is_quiet_when_the_scan_was_complete(self):
        text = cli._ssh_audit_text(self._d(
            {"total_keys": 3, "scanned": 3, "truncated": False,
             "partial_files": [], "files_capped": False, "complete": True}))
        self.assertNotIn("这是下限", text)


class SingleSourceTests(unittest.TestCase):
    """数字只在服务层定义一次：CLI 文案里出现的是常量引用，不是抄下来的 64 / 200000。"""

    def test_cli_schema_notes_reference_the_constants(self):
        src = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("login_audit_service.MAX_FILES", src)
        self.assertIn("login_audit_service.AK_MAX_CHARS", src)
        self.assertIn("keys.partial_files", src)

    def test_vue_reads_the_same_flags(self):
        body = VUE.read_text(encoding="utf-8")
        self.assertIn("keysIncomplete", body)
        self.assertIn("complete === false", body)
        self.assertIn("keysScanWhy", body)
        self.assertIn("partial_files", body)


if __name__ == "__main__":
    unittest.main()
