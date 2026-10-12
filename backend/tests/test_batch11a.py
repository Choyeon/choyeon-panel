"""批次⑪A：登录安全对账（sshd 生效配置 / 钥匙与文件权限 / 账号面 / 24h 爆破来源）。

这一层是全站唯一"必须只读"的对账：SSH 是这台机器的正门，判错顶多是一条错建议，
写错一次就把人（包括面板自己）锁在门外。所以除了算得对，测试还盯两件事——执行出去的
命令里没有写操作、读不到东西时如实标注而不是填一个看着像好消息的默认值。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import struct
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli  # noqa: E402
from app.routers import meta  # noqa: E402
from app.services import login_audit_service as svc  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _blob(algo: str, *fields: bytes) -> str:
    parts = [algo.encode(), *fields]
    return base64.b64encode(b"".join(struct.pack(">I", len(p)) + p for p in parts)).decode()


def _key_line(algo="ssh-ed25519", comment="laptop", *, opts=""):
    body = {"ssh-ed25519": b"K" * 32, "ssh-dss": b"p" * 20}.get(algo, b"Q" * 40)
    b64 = _blob(algo, body)
    front = f"{opts} " if opts else ""
    return f"{front}{algo} {b64} {comment}".rstrip(), b64


def _ssh(values=None, source="sshd -T", notes=None):
    return {"source": source, "effective": source == "sshd -T",
            "values": values if values is not None else {}, "notes": notes if notes is not None else []}


def _keys(files=None):
    files = files or []
    return {"files": files, "scanned": len(files),
            "total_keys": sum(f["count"] for f in files),
            "truncated": any(f["count"] > len(f["keys"]) for f in files)}


def _file_entry(user="bob", flags=None, keys=None, count=None, mode="rw-------", symlink=False):
    keys = keys if keys is not None else []
    return {"user": user, "path": f"/home/{user}/.ssh/authorized_keys", "mode": mode, "owner": user,
            "modified_at": "2026-10-01T00:00:00+00:00", "symlink": symlink,
            "count": len(keys) if count is None else count, "flags": flags or [], "keys": keys}


def _key(algo="ssh-ed25519", bits=256, comment="laptop", fp="SHA256:a", weak=False, unparsed=False, options=""):
    return {"algo": algo, "bits": bits, "comment": comment, "fp": fp, "weak": weak,
            "options": options, "unparsed": unparsed}


def _acc(**kw):
    base = {"login_shells": [], "login_count": 0, "uid0_extra": [], "empty_password": [],
            "shadow_unreadable": False, "sudo_groups": {}, "notes": []}
    base.update(kw)
    return base


def _att(**kw):
    base = {"ok": True, "window": "24h", "failed": 0, "distinct_ips": 0, "top": [], "accepted": [], "note": ""}
    base.update(kw)
    return base


def _guard(active=False):
    return {"units": [], "any_active": active, "note": ""}


def _exp(reachable=True):
    return {"port": 22, "reachable": reachable, "verdict": "expected", "reason": "ok"}


# doctor_item 的入参必须是 _collect 那种自洽的形态：counts 三个键齐全、risk>0 时 findings 非空。
_RISK = {"id": "root-password", "severity": "risk", "title": "root 还能用口令登录", "detail": "", "fix": ""}


HARDENED = {
    "port": ["22"], "listenaddress": ["0.0.0.0:22"],
    "passwordauthentication": ["no"], "permitrootlogin": ["prohibit-password"],
    "kbdinteractiveauthentication": ["no"], "pubkeyauthentication": ["yes"],
    "permitemptypasswords": ["no"], "maxauthtries": ["3"], "usepam": ["yes"],
    "x11forwarding": ["no"], "gatewayports": ["no"], "allowusers": ["deploy"],
}


def _ids(findings):
    return [f["id"] for f in findings]


def _find(**kw):
    """按关键字取一条判词，缺了就报错——而不是让断言在空列表上悄悄通过。"""
    out = svc._findings(_ssh(kw.pop("values", HARDENED), kw.pop("source", "sshd -T")),
                        _keys(kw.pop("files", [])), kw.pop("acc", _acc()), kw.pop("att", _att()),
                        kw.pop("guard", _guard()), kw.pop("exp", _exp()))
    return out


def _one(**kw):
    want = kw.pop("id")
    out = _find(**kw)
    got = [f for f in out if f["id"] == want]
    if not got:
        raise AssertionError(f"没有产出判词 {want}，实际：{_ids(out)}")
    return got[0]


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


def _run(fn, *argv):
    """按真实 argv 解析后跑一个 cmd_*，返回 (输出, 退出码)。Out.error 走 stderr，一起接住。"""
    a = _args(*argv)
    cli.Out.json_mode = bool(getattr(a, "json", False))
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            asyncio.run(fn(a))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        cli.Out.json_mode = False
    return out.getvalue() + err.getvalue(), code


class TestParsers(unittest.TestCase):
    def test_effective_collects_repeated_keys_and_lowercases(self):
        got = svc._parse_effective("Port 22\nListenAddress 0.0.0.0\nlistenaddress ::\n")
        self.assertEqual(got["port"], ["22"])
        self.assertEqual(got["listenaddress"], ["0.0.0.0", "::"])

    def test_effective_ignores_lines_without_a_value(self):
        self.assertEqual(svc._parse_effective("Port\n\n# comment\n"), {})

    def test_effective_keeps_values_that_contain_spaces(self):
        self.assertEqual(svc._parse_effective("Banner /etc/issue.net\n"), {"banner": ["/etc/issue.net"]})

    def test_config_skips_comments_and_the_match_block(self):
        got = svc._parse_config(
            "#PasswordAuthentication no\nPasswordAuthentication yes\n"
            "Match User bob\nPasswordAuthentication no\nPubkeyAuthentication no\n"
        )
        self.assertEqual(got["passwordauthentication"], ["yes"])
        self.assertNotIn("pubkeyauthentication", got)

    def test_config_is_empty_when_every_line_is_commented(self):
        self.assertEqual(svc._parse_config("# a\n# b\n"), {})

    def test_read_text_returns_none_instead_of_raising(self):
        self.assertIsNone(svc._read_text("/definitely/not/here"))
        with TemporaryDirectory() as d:
            self.assertIsNone(svc._read_text(d))  # 目录会抛 IsADirectoryError，也是 OSError

    def test_read_text_honours_the_limit(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "big"
            p.write_text("x" * 5000, encoding="utf-8")
            self.assertEqual(len(svc._read_text(str(p), 100)), 100)


class TestReadOnlyPledge(unittest.TestCase):
    """这一层的立身之本：只读。任何写入或 sshd 变更都不该出现在这里。"""

    SRC = Path(svc.__file__).read_text(encoding="utf-8")

    def test_no_file_is_opened_for_writing(self):
        self.assertEqual(re.findall(r"""open\([^)\n]*["'][wax]""", self.SRC), [])

    def test_only_known_read_only_programs_are_executed(self):
        bins = re.findall(r"await run\(\s*[\"']([^\"']+)[\"']", self.SRC)
        self.assertTrue(bins, "至少要真的执行过命令，否则这条断言是空的")
        for b in bins:
            self.assertIn(os.path.basename(b), {"journalctl", "systemctl", "sshd"}, b)

    def test_no_mutating_verb_appears_in_any_executed_argv(self):
        argvs = re.findall(r"await run\([^)]*\[([^\]]*)\]", self.SRC, re.S)
        forbidden = {"reload", "restart", "stop", "start", "enable", "disable", "edit", "-i", "mv", "cp", "tee"}
        for blob in argvs:
            tokens = {t.strip().strip('"\'') for t in blob.split(",")}
            self.assertFalse(forbidden & tokens, tokens)

    def test_executed_commands_are_recorded_at_runtime(self):
        seen = []

        async def spy(cmd, args, **kw):
            seen.append([str(x) for x in [cmd, *args]])
            return {"code": 0, "out": ""}

        with mock.patch.object(svc, "run", spy), mock.patch.object(svc.shutil, "which", lambda _n: True):
            asyncio.run(svc._attempts())
            asyncio.run(svc._guard_status())
        self.assertTrue(seen)
        for argv in seen:
            self.assertIn(os.path.basename(argv[0]), {"journalctl", "systemctl"})
            self.assertNotIn("restart", argv)


class TestBlobMeta(unittest.TestCase):
    def test_ed25519_fingerprint_and_bits(self):
        fp, bits, weak = svc._blob_meta("ssh-ed25519", _blob("ssh-ed25519", b"K" * 32))
        self.assertTrue(fp.startswith("SHA256:"))
        self.assertEqual(bits, 256)
        self.assertFalse(weak)

    def test_fingerprint_is_the_canonical_sha256_of_the_wire_blob(self):
        import hashlib

        b64 = _blob("ssh-ed25519", b"K" * 32)
        fp, _, _ = svc._blob_meta("ssh-ed25519", b64)
        raw = base64.b64decode(b64)
        self.assertEqual(fp, "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="))

    def test_rsa_bits_come_from_the_modulus(self):
        fp, bits, weak = svc._blob_meta("ssh-rsa", _blob("ssh-rsa", b"\x01\x00\x01", b"\xA3" * 384))
        self.assertEqual((bits, weak), (3072, False))
        self.assertTrue(fp)

    def test_small_rsa_is_weak(self):
        _, bits, weak = svc._blob_meta("ssh-rsa", _blob("ssh-rsa", b"\x03", b"\xA3" * 128))
        self.assertEqual((bits, weak), (1024, True))

    def test_sign_padding_byte_does_not_inflate_the_key(self):
        _, bits, _ = svc._blob_meta("ssh-rsa", _blob("ssh-rsa", b"\x03", b"\x00" + b"\xA3" * 127))
        self.assertEqual(bits, 1016)

    def test_dsa_is_weak(self):
        _, bits, weak = svc._blob_meta("ssh-dss", _blob("ssh-dss", b"p" * 20))
        self.assertEqual((bits, weak), (1024, True))

    def test_ecdsa_bits_come_from_the_curve_name(self):
        self.assertEqual(svc._blob_meta("ecdsa-sha2-nistp384", _blob("ecdsa-sha2-nistp384", b"nistp384", b"Q"))[1], 384)
        self.assertIsNone(svc._blob_meta("ecdsa-sha2-nistp999", _blob("ecdsa-sha2-nistp999", b"?", b"Q"))[1])

    def test_security_key_variant_is_recognised(self):
        self.assertEqual(svc._blob_meta("sk-ssh-ed25519", _blob("sk-ssh-ed25519", b"K" * 32, b"a"))[1], 256)

    def test_unparsable_base64_yields_no_fingerprint(self):
        fp, bits, weak = svc._blob_meta("ssh-ed25519", "AAAAA")
        self.assertIsNone(fp)
        self.assertIsNone(bits)
        self.assertFalse(weak)

    def test_truncated_blob_does_not_crash(self):
        self.assertEqual(svc._blob_meta("ssh-rsa", base64.b64encode(b"\x00\x00\x00\x08").decode())[0][:7], "SHA256:")

    def test_is_algo_prefix_whitelist(self):
        for tok in ("ssh-rsa", "ecdsa-sha2-nistp256", "sk-ssh-ed25519", "x509-ssh-ed25519"):
            self.assertTrue(svc._is_algo(tok), tok)
        for tok in ("from=", "command=", "no-pty", ""):
            self.assertFalse(svc._is_algo(tok), tok)


class TestSettingRows(unittest.TestCase):
    def rows(self, values, reachable=True, guarded=False):
        return {r["key"]: r for r in svc._setting_rows(values, reachable, guarded)}

    def test_root_plus_password_plus_reachable_is_risk(self):
        r = self.rows({**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]})
        self.assertEqual(r["permitrootlogin"]["flag"], "risk")

    def test_loopback_root_password_downgrades_to_note(self):
        r = self.rows({**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]}, reachable=False)
        self.assertEqual(r["permitrootlogin"]["flag"], "note")

    def test_password_flag_tracks_the_guard(self):
        v = {**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["prohibit-password"]}
        self.assertEqual(self.rows(v, guarded=False)["passwordauthentication"]["flag"], "risk")
        self.assertEqual(self.rows(v, guarded=True)["passwordauthentication"]["flag"], "warn")

    def test_password_off_leaves_no_flag(self):
        self.assertEqual(self.rows(HARDENED)["passwordauthentication"]["flag"], "")

    def test_kbd_flag_only_matters_next_to_password_auth(self):
        self.assertEqual(self.rows({**HARDENED, "kbdinteractiveauthentication": ["yes"]})
                         ["kbdinteractiveauthentication"]["flag"], "")
        r = self.rows({**HARDENED, "passwordauthentication": ["yes"], "kbdinteractiveauthentication": ["yes"]})
        self.assertEqual(r["kbdinteractiveauthentication"]["flag"], "warn")

    def test_empty_passwords_setting_is_risk(self):
        self.assertEqual(self.rows({**HARDENED, "permitemptypasswords": ["yes"]})["permitemptypasswords"]["flag"], "risk")

    def test_maxauthtries_flags_only_above_six(self):
        self.assertEqual(self.rows({**HARDENED, "maxauthtries": ["6"]})["maxauthtries"]["flag"], "")
        self.assertEqual(self.rows({**HARDENED, "maxauthtries": ["10"]})["maxauthtries"]["flag"], "note")

    def test_x11_gateway_and_pam_flags(self):
        r = self.rows({**HARDENED, "x11forwarding": ["yes"], "gatewayports": ["yes"], "usepam": ["no"]})
        self.assertEqual(r["x11forwarding"]["flag"], "note")
        self.assertEqual(r["gatewayports"]["flag"], "warn")
        self.assertEqual(r["usepam"]["flag"], "note")

    def test_missing_user_allowlist_is_a_note_and_a_present_one_is_clean(self):
        r = self.rows({k: v for k, v in HARDENED.items() if k != "allowusers"})
        self.assertEqual((r["allowusers"]["flag"], r["allowusers"]["value"]), ("note", "未设置"))
        r = self.rows({**HARDENED, "allowgroups": ["wheel"]})
        self.assertEqual(r["allowusers"]["flag"], "")
        self.assertIn("deploy", r["allowusers"]["value"])

    def test_advice_only_accompanies_a_flagged_row(self):
        # 值本来就合规还附一句"设 X no"，读起来像根本没检查过
        rows = self.rows({**HARDENED, "permitrootlogin": ["yes"], "passwordauthentication": ["yes"]},
                         reachable=False, guarded=True)
        self.assertEqual(rows["permitemptypasswords"]["fix"], "")
        self.assertTrue(rows["permitrootlogin"]["fix"])

    def test_setting_absent_from_the_config_produces_no_row(self):
        keys = [r["key"] for r in svc._setting_rows({"pubkeyauthentication": ["yes"]}, True, False)]
        self.assertNotIn("port", keys)
        self.assertIn("pubkeyauthentication", keys)

    def test_setting_rows_themselves_trust_the_reachable_argument(self):
        # 判不出可达性时该由上层按"可达"传 True 进来，而不是把 None 传下来当安全
        v = {**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]}
        self.assertEqual(self.rows(v, reachable=False)["passwordauthentication"]["flag"], "")
        self.assertEqual(self.rows(v, reachable=True)["passwordauthentication"]["flag"], "risk")


class TestAuthorizedKeys(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(parents=True)
        os.chmod(self.home, 0o755)
        os.chmod(self.ssh, 0o700)
        self.path = self.ssh / "authorized_keys"

    def tearDown(self):
        for m in self.root.rglob("*"):
            with __import__("contextlib").suppress(OSError):
                m.chmod(0o700)
        self._tmp.cleanup()

    def user(self, name="bob", uid=1500):
        return {"name": name, "uid": uid, "home": str(self.home), "shell": "/bin/bash", "login": True}

    def write(self, text, mode=0o600):
        if self.path.is_symlink():
            self.path.unlink()
        self.path.write_text(text, encoding="utf-8")
        os.chmod(self.path, mode)

    def scan(self, as_root=True):
        return svc._authorized_keys([self.user()], as_root)

    def test_lists_fingerprints_not_key_bodies(self):
        line, b64 = _key_line()
        self.write(line + "\n")
        out = self.scan()
        self.assertEqual(out["total_keys"], 1)
        got = out["files"][0]["keys"][0]
        self.assertEqual((got["algo"], got["comment"], got["unparsed"]), ("ssh-ed25519", "laptop", False))
        self.assertTrue(got["fp"].startswith("SHA256:"))
        self.assertNotIn(b64, json.dumps(out, ensure_ascii=False))

    def test_comment_and_blank_lines_are_not_keys(self):
        line, _ = _key_line()
        self.write(f"# 这是备注\n\n{line}\n")
        self.assertEqual(self.scan()["total_keys"], 1)

    def test_options_column_is_parsed_off_the_front(self):
        line, _ = _key_line(opts='from="10.0.0.0/8",no-pty')
        self.write(line + "\n")
        got = self.scan()["files"][0]["keys"][0]
        self.assertEqual(got["options"], 'from="10.0.0.0/8",no-pty')
        self.assertEqual(got["algo"], "ssh-ed25519")
        self.assertFalse(got["unparsed"])

    def test_unknown_format_is_counted_and_marked_unparsed(self):
        self.write("这行根本不是公钥\n")
        out = self.scan()
        self.assertEqual(out["total_keys"], 1)
        self.assertTrue(out["files"][0]["keys"][0]["unparsed"])

    def test_cert_authority_directive_is_flagged_not_counted_as_a_key(self):
        self.write("@cert-authority *.example.com ssh-ed25519 AAAA\n")
        f = self.scan()["files"][0]
        self.assertTrue(any("@cert-authority" in x for x in f["flags"]))
        self.assertEqual(f["count"], 1)

    def test_symlink_is_flagged_and_its_target_not_read(self):
        real = self.root / "elsewhere"
        line, b64 = _key_line(comment="hidden")
        real.write_text(line + "\n", encoding="utf-8")
        os.symlink(str(real), str(self.path))
        out = self.scan()
        f = out["files"][0]
        self.assertTrue(f["symlink"])
        self.assertEqual(f["keys"], [])
        self.assertTrue(any("符号链接" in x for x in f["flags"]))
        self.assertNotIn(b64, json.dumps(out, ensure_ascii=False))

    def test_group_or_world_writable_is_flagged(self):
        line, _ = _key_line()
        for mode in (0o664, 0o620, 0o606):
            self.write(line, mode)
            f = self.scan()["files"][0]
            self.assertTrue(any("可写" in x for x in f["flags"]), f"{oct(mode)} 应当被标出来")

    def test_owner_only_mode_is_clean(self):
        line, _ = _key_line()
        self.write(line, 0o600)
        self.assertEqual(self.scan()["files"][0]["flags"], [])

    @unittest.skipUnless(os.geteuid() == 0, "改属主需要 root")
    def test_wrong_owner_is_flagged(self):
        # 属主是"另一个用户"才是要报的异常：那个人就能往里面加钥匙。
        # root 属主留给下面那条测试（sshd 允许，机器上也很常见）。
        line, _ = _key_line()
        self.write(line)
        os.chown(str(self.path), 1501, 1501)
        self.assertTrue(any("属主" in x for x in self.scan()["files"][0]["flags"]))

    @unittest.skipUnless(os.geteuid() == 0, "改属主需要 root")
    def test_root_owned_file_is_allowed(self):
        # root 管理的 authorized_keys 很常见，不该一律报成异常
        line, _ = _key_line()
        self.write(line)
        os.chown(str(self.path), 0, 0)
        f = svc._authorized_keys([self.user(name="root", uid=0)], True)["files"][0]
        self.assertEqual(f["owner"], "root")
        self.assertEqual(f["flags"], [])

    def test_non_root_does_not_read_other_users(self):
        line, _ = _key_line()
        self.write(line)
        out = self.scan(as_root=False)
        self.assertEqual((out["files"], out["scanned"]), ([], 0))

    def test_non_root_still_reads_its_own_file(self):
        line, _ = _key_line()
        self.write(line)
        self.assertEqual(svc._authorized_keys([self.user(uid=os.getuid())], False)["scanned"], 1)

    def test_missing_home_is_skipped(self):
        out = svc._authorized_keys([{"name": "x", "uid": 7, "home": str(self.root / "nope"),
                                     "shell": "/bin/bash", "login": True}], True)
        self.assertEqual(out["files"], [])

    def test_more_keys_than_the_cap_are_counted_but_not_listed(self):
        b64 = _blob("ssh-ed25519", b"K" * 32)
        self.write("".join(f"ssh-ed25519 {b64} k{i}\n" for i in range(5)))
        with mock.patch.object(svc, "MAX_KEYS", 2):
            out = self.scan()
        self.assertEqual(out["total_keys"], 5)
        self.assertEqual(len(out["files"][0]["keys"]), 2)
        self.assertTrue(out["truncated"])

    def test_mode_and_mtime_are_reported(self):
        line, _ = _key_line()
        self.write(line, 0o600)
        f = self.scan()["files"][0]
        self.assertEqual(f["mode"], "rw-------")
        self.assertEqual(f["path"], str(self.path))
        self.assertTrue(f["modified_at"].startswith("20"))

    def test_file_cap_stops_the_scan(self):
        line, _ = _key_line()
        users = []
        for i in range(3):
            home = self.root / f"u{i}"
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "authorized_keys").write_text(line + "\n", encoding="utf-8")
            os.chmod(home / ".ssh" / "authorized_keys", 0o600)
            users.append({"name": f"u{i}", "uid": 2000 + i, "home": str(home), "shell": "/bin/bash", "login": True})
        with mock.patch.object(svc, "MAX_FILES", 2):
            out = svc._authorized_keys(users, True)
        self.assertEqual(len(out["files"]), 2)


class TestAccountsAndPasswd(unittest.TestCase):
    USERS = [
        {"name": "root", "uid": 0, "home": "/root", "shell": "/bin/bash", "login": True},
        {"name": "sync", "uid": 4, "home": "/", "shell": "/bin/sync", "login": False},
        {"name": "ghost", "uid": 0, "home": "/srv", "shell": "/bin/bash", "login": True},
        {"name": "bob", "uid": 1001, "home": "/home/bob", "shell": "/bin/bash", "login": True},
    ]
    SHADOW = "root:x:1:0:99999:7:::\nbob::1:0:99999:7:::\nghost:!:1:0:99999:7:::\nsync:*:1:0:99999:7:::\n"

    def reads(self, shadow=SHADOW, group="sudo:x:27:bob,carol\nwheel:x:10:\n", passwd=None):
        files = {"/etc/shadow": shadow, "/etc/group": group, "/etc/passwd": passwd}

        def fake(path, limit=200000):
            return files.get(path)

        return mock.patch.object(svc, "_read_text", fake)

    def test_login_shells_are_counted_and_non_login_ones_dropped(self):
        with self.reads():
            acc = svc._accounts(self.USERS)
        self.assertEqual(sorted(acc["login_shells"]), ["bob", "ghost", "root"])
        self.assertEqual(acc["login_count"], 3)

    def test_extra_uid_zero_detected(self):
        with self.reads():
            self.assertEqual(svc._accounts(self.USERS)["uid0_extra"], ["ghost"])

    def test_empty_password_detected_from_shadow(self):
        with self.reads():
            acc = svc._accounts(self.USERS)
        self.assertEqual(acc["empty_password"], ["bob"])
        self.assertFalse(acc["shadow_unreadable"])

    def test_locked_and_hashed_accounts_are_not_empty_passwords(self):
        with self.reads(shadow="root:!:1:::\nsync:*:1:::\n"):
            self.assertEqual(svc._accounts(self.USERS)["empty_password"], [])

    def test_unreadable_shadow_is_declared_not_assumed_safe(self):
        with self.reads(shadow=None):
            acc = svc._accounts(self.USERS)
        self.assertTrue(acc["shadow_unreadable"])
        self.assertEqual(acc["empty_password"], [])
        self.assertTrue(any("/etc/shadow" in n for n in acc["notes"]))

    def test_sudo_and_wheel_members(self):
        with self.reads():
            self.assertEqual(svc._accounts(self.USERS)["sudo_groups"], {"sudo": ["bob", "carol"], "wheel": []})

    def test_passwd_tolerates_short_lines_and_exotic_shells(self):
        text = "broken:line\ndata:x:6:12:data:/var/lib/data:/usr/sbin/nologin\nme:x:1000:1000:me:/home/me:/bin/bash\n"
        with self.reads(passwd=text):
            users = svc._passwd_users()
        self.assertEqual([u["name"] for u in users], ["data", "me"])
        self.assertFalse(users[0]["login"])
        self.assertTrue(users[1]["login"])
        self.assertEqual(users[1]["home"], "/home/me")

    def test_passwd_uid_that_is_not_a_number_becomes_minus_one(self):
        with self.reads(passwd="weird:x:notanumber:1::/home/weird:/bin/bash\n"):
            self.assertEqual(svc._passwd_users()[0]["uid"], -1)

    def test_missing_passwd_yields_no_users_instead_of_raising(self):
        with self.reads(passwd=None):
            self.assertEqual(svc._passwd_users(), [])


class TestSshdSettings(unittest.TestCase):
    def test_effective_output_is_preferred(self):
        async def run(cmd, args, **kw):
            return {"code": 0, "out": "port 22\npasswordauthentication yes\n"}

        with mock.patch.object(svc, "run", run), mock.patch.object(svc.shutil, "which", lambda _n: "/usr/sbin/sshd"):
            got = asyncio.run(svc._sshd_settings())
        self.assertEqual(got["source"], "sshd -T")
        self.assertTrue(got["effective"])
        self.assertEqual(got["values"]["passwordauthentication"], ["yes"])

    def test_falls_back_to_the_raw_file_and_says_so(self):
        def fake(path, limit=200000):
            return "# comment\nPermitRootLogin yes\n" if path.endswith("sshd_config") else None

        async def run(cmd, args, **kw):
            return {"code": 1, "out": "Permission denied /etc/ssh/ssh_config"}

        with mock.patch.object(svc, "run", run), mock.patch.object(svc.shutil, "which", lambda _n: "/usr/sbin/sshd"), \
            mock.patch.object(svc, "_read_text", fake):
            got = asyncio.run(svc._sshd_settings())
        self.assertEqual(got["source"], "sshd_config")
        self.assertFalse(got["effective"])
        self.assertTrue(got["notes"])

    def test_no_sshd_at_all_is_missing_not_empty(self):
        with mock.patch.object(svc.shutil, "which", lambda _n: None), \
            mock.patch.object(svc, "_read_text", lambda path, limit=200000: None):
            got = asyncio.run(svc._sshd_settings())
        self.assertEqual(got["source"], "missing")
        self.assertEqual(got["values"], {})

    def test_unreadable_file_is_reported_as_unreadable(self):
        async def run(cmd, args, **kw):
            return {"code": 1, "out": ""}

        with mock.patch.object(svc, "run", run), mock.patch.object(svc.shutil, "which", lambda _n: "/usr/sbin/sshd"), \
            mock.patch.object(svc, "_read_text", lambda path, limit=200000: None):
            got = asyncio.run(svc._sshd_settings())
        self.assertEqual(got["source"], "unreadable")


class TestExposure(unittest.TestCase):
    def test_matches_the_row_for_the_configured_port(self):
        ports = {"ports": [{"port": 2222, "reachable": False, "verdict": "blocked", "reason": "防火墙没放行"}]}
        got = svc._ssh_exposure({"port": ["2222"]}, ports)
        self.assertEqual((got["port"], got["verdict"], got["reachable"]), (2222, "blocked", False))

    def test_missing_row_is_none_plus_an_honest_reason(self):
        got = svc._ssh_exposure({"port": ["22"]}, {"ports": [{"port": 80, "reachable": True,
                                                             "verdict": "exposed", "reason": "x"}]})
        self.assertIsNone(got["reachable"])
        self.assertEqual(got["verdict"], "")
        self.assertIn("端口表", got["reason"])

    def test_ports_layer_absent_entirely(self):
        got = svc._ssh_exposure({"port": ["2222"]}, None)
        self.assertEqual(got["port"], 2222)
        self.assertIsNone(got["reachable"])

    def test_non_numeric_port_falls_back_to_22(self):
        self.assertEqual(svc._ssh_exposure({"port": ["http"]}, None)["port"], 22)
        self.assertEqual(svc._ssh_exposure({}, None)["port"], 22)


class TestFindings(unittest.TestCase):
    def test_hardened_machine_yields_nothing(self):
        self.assertEqual(_ids(_find()), [])

    def test_intrusion_indicators_are_risk(self):
        out = _find(acc=_acc(empty_password=["bob"], uid0_extra=["ghost"]))
        self.assertEqual(set(_ids(out)), {"empty-password", "uid0"})
        self.assertTrue(all(f["severity"] == "risk" for f in out))
        self.assertIn("bob", out[0]["detail"])

    def test_root_password_is_risk_only_when_reachable(self):
        v = {**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]}
        self.assertIn("root-password", _ids(_find(values=v, exp=_exp(True))))
        self.assertNotIn("root-password", _ids(_find(values=v, exp=_exp(False))))

    def test_unproven_reachability_is_treated_as_reachable(self):
        v = {**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]}
        self.assertIn("root-password", _ids(_find(values=v, exp=_exp(None))))

    def test_password_without_root_login_is_a_warn(self):
        v = {**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["prohibit-password"]}
        out = _find(values=v, guard=_guard(True))
        self.assertIn("password-auth", _ids(out))
        self.assertNotIn("root-password", _ids(out))

    def test_no_guard_severity_flips_at_a_hundred_failures(self):
        v = {**HARDENED, "passwordauthentication": ["yes"]}
        self.assertEqual(_one(id="no-guard", values=v, att=_att(failed=99))["severity"], "warn")
        self.assertEqual(_one(id="no-guard", values=v, att=_att(failed=100, distinct_ips=3))["severity"], "risk")

    def test_brute_volume_needs_volume_and_no_guard(self):
        v = {**HARDENED, "passwordauthentication": ["yes"]}
        att = _att(failed=600, distinct_ips=7, top=[{"ip": "1.1.1.1", "count": 300}])
        self.assertIn("brute-volume", _ids(_find(values=v, att=att)))
        self.assertNotIn("brute-volume", _ids(_find(values=v, att=att, guard=_guard(True))))
        self.assertNotIn("brute-volume", _ids(_find(values=v, att=_att(failed=499))))

    def test_logless_window_does_not_invent_numbers(self):
        v = {**HARDENED, "passwordauthentication": ["yes"]}
        got = _one(id="no-guard", values=v, att=_att(ok=False, failed=0, note="读不到"))
        self.assertNotIn("0 次", got["detail"])

    def test_kbd_and_empty_pw_and_gateway_ports(self):
        v = {**HARDENED, "passwordauthentication": ["yes"], "kbdinteractiveauthentication": ["yes"],
             "permitemptypasswords": ["yes"], "gatewayports": ["yes"]}
        out = _find(values=v)
        self.assertTrue({"kbdinteractive", "empty-pw-config"} <= set(_ids(out)))
        self.assertEqual(_one(id="empty-pw-config", values=v)["severity"], "risk")

    def test_key_surface_findings(self):
        files = [_file_entry(keys=[
            _key(algo="ssh-dss", bits=1024, comment="old", weak=True),
            _key(comment="", fp="SHA256:b"),
            _key(algo="?", bits=None, comment="乱码", fp=None, unparsed=True),
        ])]
        self.assertTrue({"weak-key", "no-comment", "ak-unparsed"} <= set(_ids(_find(files=files))))
        self.assertEqual(_one(id="weak-key", files=files)["severity"], "warn")

    def test_authorized_keys_file_severity_depends_on_the_flag_kind(self):
        hard = [_file_entry(flags=["同组或其他人可写：任何本地用户都能往里加钥匙"])]
        soft = [_file_entry(flags=["属主是 root，不是这个用户"])]
        link = [_file_entry(flags=["是指向别处的符号链接，面板没有读它指向的内容"], symlink=True)]
        self.assertEqual(_one(id="ak-file", files=hard)["severity"], "risk")
        self.assertEqual(_one(id="ak-file", files=soft)["severity"], "warn")
        self.assertEqual(_one(id="ak-file", files=link)["severity"], "risk")

    def test_non_effective_config_source_is_a_note(self):
        out = _find(source="sshd_config")
        self.assertEqual(_one(id="config-source", source="sshd_config")["severity"], "note")
        self.assertIn("sshd -T", out[0]["fix"])

    def test_accepted_password_severity_depends_on_guard(self):
        att = _att(accepted=[{"method": "password", "count": 3}])
        self.assertEqual(_one(id="accepted-password", att=att)["severity"], "warn")
        self.assertEqual(_one(id="accepted-password", att=att, guard=_guard(True))["severity"], "note")

    def test_publickey_only_logins_are_quiet(self):
        self.assertNotIn("accepted-password", _ids(_find(att=_att(accepted=[{"method": "publickey", "count": 9}]))))

    def test_sorted_risk_then_warn_then_note(self):
        out = _find(values={**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"],
                            "x11forwarding": ["yes"], "allowusers": []},
                    files=[_file_entry(keys=[_key(algo="ssh-dss", bits=1024, weak=True)])],
                    source="sshd_config", att=_att(failed=800, distinct_ips=2))
        order = [{"risk": 0, "warn": 1, "note": 2}[f["severity"]] for f in out]
        self.assertEqual(order, sorted(order))
        self.assertGreater(len(out), 4)

    def test_every_finding_carries_the_four_fields(self):
        out = _find(values={k: ["yes"] for k in HARDENED}, source="missing",
                    att=_att(failed=900, distinct_ips=9))
        self.assertTrue(out)
        for f in out:
            self.assertEqual(set(f), {"id", "severity", "title", "detail", "fix"})
            self.assertTrue(f["title"])

    def test_summary_counts_match_the_findings(self):
        out = _find(values={**HARDENED, "passwordauthentication": ["yes"]})
        s = svc._summary(out)
        self.assertEqual(sum(s.values()), len(out))
        self.assertEqual(set(s), {"risk", "warn", "note"})


class TestAttempts(unittest.TestCase):
    LOG = "\n".join([
        "Oct 12 10:00:01 h sshd[1]: Failed password for root from 1.2.3.4 port 5 ssh2",
        "Oct 12 10:00:02 h sshd[2]: Failed password for invalid user admin from 1.2.3.4 port 6 ssh2",
        "Oct 12 10:00:03 h sshd[3]: Invalid user oracle from 5.6.7.8 port 7 [preauth]",
        "Oct 12 10:00:04 h sshd[4]: Connection closed by authenticating user root 2001:db8::1 port 22 [preauth]",
        "Oct 12 10:00:05 h sshd[5]: Accepted publickey for root from 9.9.9.9 port 1 ssh2: ED25519 SHA256:x",
        "Oct 12 10:00:06 h sshd[6]: Accepted password for bob from 8.8.8.8 port 1 ssh2",
        "Oct 12 10:00:07 h sshd[7]: pam_unix(sshd:session): session opened for user bob",
        "Oct 12 10:00:08 h sshd[8]: Failed password for root from 1.2.3.4 port 9 ssh2",
    ])

    def fake(self, code=0, out=None):
        async def run(cmd, args, **kw):
            return {"code": code, "out": self.LOG if out is None else out}

        return run

    def collect(self, code=0, out=None, which=True):
        with mock.patch.object(svc, "run", self.fake(code, out)), \
            mock.patch.object(svc.shutil, "which", lambda _n: "/usr/bin/journalctl" if which else None):
            return asyncio.run(svc._attempts())

    def test_counts_failures_ips_and_methods(self):
        got = self.collect()
        self.assertTrue(got["ok"])
        self.assertEqual(got["failed"], 5)
        self.assertEqual(got["distinct_ips"], 3)
        self.assertEqual(got["top"][0], {"ip": "1.2.3.4", "count": 3})
        # 成功登录按方式聚合：次数降序、同次数按方法名排，顺序不能跟着日志走。
        self.assertEqual([a["method"] for a in got["accepted"]], ["password", "publickey"])
        self.assertEqual([a["count"] for a in got["accepted"]], [1, 1])
        self.assertNotIn("pam_unix", json.dumps(got))

    def test_preauth_close_lines_still_yield_their_source(self):
        # 真实日志里三成以上的爆破行是 "Connection closed by authenticating user root <ip>"，
        # 不带 from，按 from 取 IP 会让这些来源从排行里整体消失。
        for line in (
            "h sshd[4]: Connection closed by authenticating user root 2001:db8::1 port 22 [preauth]",
            "h sshd[5]: Connection closed by invalid user docker 2.57.122.150 port 59614 [preauth]",
        ):
            got = self.collect(out=line)
            self.assertEqual(got["top"], [{"ip": re.search(r"(\S+) port", line).group(1), "count": 1}], line)

    def test_ipv6_source_is_captured(self):
        self.assertIn("2001:db8::1", [t["ip"] for t in self.collect()["top"]])

    def test_noise_lines_are_not_counted_as_attempts(self):
        noise = "\n".join([
            "h sshd[7]: pam_unix(sshd:session): session opened for user bob",
            "h sshd[9]: Connection closed by 203.0.113.9 port 52012",
            "h sudo: pam_unix(sudo:session): session closed for user root",
        ])
        got = self.collect(out=noise)
        self.assertEqual((got["failed"], got["top"]), (0, []))
        self.assertNotIn("session opened", json.dumps(got))

    def test_top_is_capped_and_ordered(self):
        lines = [f"x Failed password for root from 10.0.0.{i % 251} port 1 ssh2" for i in range(900)]
        got = self.collect(out="\n".join(lines))
        self.assertEqual(len(got["top"]), 5)
        self.assertLessEqual(got["top"][0]["count"], 900)

    def test_missing_journalctl_is_declared(self):
        got = self.collect(which=False)
        self.assertFalse(got["ok"])
        self.assertIn("journalctl", got["note"])

    def test_failure_exit_code_is_declared(self):
        got = self.collect(code=4, out="")
        self.assertFalse(got["ok"])
        self.assertIn("4", got["note"])

    def test_absent_journal_unit_is_declared(self):
        got = self.collect(code=1, out="")
        self.assertFalse(got["ok"])
        self.assertIn("journal", got["note"])

    def test_quiet_machine_reports_zero_and_ok(self):
        got = self.collect(code=0, out="")
        self.assertTrue(got["ok"])
        self.assertEqual(got["failed"], 0)


class TestGuardStatus(unittest.TestCase):
    def collect(self, out, which=True):
        async def run(cmd, args, **kw):
            return {"code": 0, "out": out}

        with mock.patch.object(svc, "run", run), mock.patch.object(svc.shutil, "which",
                                                                  lambda n: "/usr/sbin/" + n if which else None):
            return asyncio.run(svc._guard_status())

    def test_active_unit_is_detected(self):
        got = self.collect("active\ninactive\n")
        self.assertTrue(got["any_active"])
        self.assertEqual(got["units"][0]["state"], "active")

    def test_nothing_running(self):
        got = self.collect("inactive\ninactive\n")
        self.assertFalse(got["any_active"])
        self.assertTrue(all(u["installed"] for u in got["units"]))

    def test_missing_systemctl_is_declared(self):
        with mock.patch.object(svc.shutil, "which", lambda _n: None):
            got = asyncio.run(svc._guard_status())
        self.assertFalse(got["any_active"])
        self.assertIn("systemctl", got["note"])

    def test_short_output_does_not_index_error(self):
        got = self.collect("active")
        self.assertEqual(got["units"][1]["state"], "unknown")


class TestAuditCache(unittest.TestCase):
    def sample(self, n=1):
        return {"ok": True, "checked_at": "t", "as_root": True, "counts": {"risk": n, "warn": 0, "note": 0},
                "sshd": {"source": "sshd -T", "port": 22, "reachable": True, "effective": True,
                         "verdict": "expected", "reason": ""},
                "settings": [], "guard": [], "keys": _keys([]), "accounts": _acc(), "attempts": _att(),
                "findings": [], "notes": []}

    def go(self, collect, calls=None):
        async def run():
            svc._cache["data"] = None
            svc._cache["at"] = 0.0
            first = await svc.audit()
            second = await svc.audit()
            third = await svc.audit(refresh=True)
            gathered = await asyncio.gather(svc.audit(), svc.audit(), svc.audit())
            return first, second, third, gathered

        with mock.patch.object(svc, "_collect", collect):
            return asyncio.run(run())

    def test_only_the_first_call_hits_the_source(self):
        seen = []

        async def collect():
            seen.append(1)
            return self.sample(len(seen))

        first, second, third, gathered = self.go(collect)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(third["cached"])
        self.assertTrue(all(g["cached"] for g in gathered))
        self.assertEqual(len(seen), 2)

    def test_cached_payload_is_a_copy_not_the_internals(self):
        async def collect():
            return self.sample()

        first, second, _, _ = self.go(collect)
        self.assertEqual(first["counts"], second["counts"])
        second["counts"]["risk"] = 99
        self.assertEqual(svc._cache["data"]["counts"]["risk"], 1)

    def test_concurrent_callers_wait_for_one_collection(self):
        seen = []

        async def collect():
            seen.append(1)
            await asyncio.sleep(0.01)
            return self.sample()

        async def run():
            svc._cache["data"] = None
            svc._cache["at"] = 0.0
            return await asyncio.gather(svc.audit(), svc.audit(), svc.audit())

        with mock.patch.object(svc, "_collect", collect):
            asyncio.run(run())
        self.assertEqual(len(seen), 1)


class TestCollectShape(unittest.TestCase):
    def patched(self, **kw):
        return mock.patch.multiple(
            svc,
            _sshd_settings=_async(_ssh(kw.get("values", HARDENED), kw.get("source", "sshd -T"))),
            _passwd_users=lambda: kw.get("users", []),
            _guard_status=_async(_guard(kw.get("guarded", True))),
            _attempts=_async(kw.get("att", _att())),
            _authorized_keys=lambda users, as_root: kw.get("keys", _keys([])),
        )

    def data(self, **kw):
        async def boom():
            raise RuntimeError("ss 不可用")

        ports = boom if kw.get("ports_fails") else _async(kw.get("ports"))
        with mock.patch.object(svc.exposure_service, "audit", ports), self.patched(**kw):
            return asyncio.run(svc._collect())

    def test_report_has_every_surface_the_ui_needs(self):
        got = self.data()
        for key in ("ok", "checked_at", "as_root", "sshd", "settings", "guard", "keys",
                    "accounts", "attempts", "counts", "findings", "notes"):
            self.assertIn(key, got)
        self.assertTrue(got["ok"])

    def test_exposure_row_is_carried_into_the_summary_line(self):
        got = self.data(ports={"ports": [{"port": 22, "reachable": True, "verdict": "expected", "reason": "r"}]})
        self.assertTrue(got["sshd"]["reachable"])
        self.assertEqual(got["sshd"]["verdict"], "expected")

    def test_ports_layer_failure_does_not_take_the_page_down(self):
        got = self.data(ports_fails=True)
        self.assertTrue(got["ok"])
        self.assertIsNone(got["sshd"]["reachable"])

    def test_non_root_run_says_what_was_not_checked(self):
        # 这一条依赖真实 euid：非 root 跑测试时才会出现，root 时跳过而不是假装通过
        got = self.data()
        if os.geteuid() != 0:
            self.assertTrue(any("不是 root" in n for n in got["notes"]))
            self.assertTrue(got["accounts"]["shadow_unreadable"] or not got["accounts"]["empty_password"])
        else:
            self.assertFalse(any("不是 root" in n for n in got["notes"]))

    def test_unproven_reachability_is_not_marked_safe(self):
        # 端口表里没有 sshd 那一行时 reachable=None；当成"安全"会让整页在探测失败时变绿
        got = self.data(values={**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]},
                        guarded=False)
        self.assertIsNone(got["sshd"]["reachable"])
        self.assertIn("root-password", _ids(got["findings"]))
        by = {r["key"]: r for r in got["settings"]}
        self.assertEqual(by["passwordauthentication"]["flag"], "risk")

    def test_read_only_pledge_is_stated_in_every_report(self):
        notes = self.data()["notes"]
        self.assertTrue(any("sshd_config" in n and "不写" in n for n in notes), notes)

    def test_findings_and_counts_agree(self):
        got = self.data(values={**HARDENED, "passwordauthentication": ["yes"], "permitrootlogin": ["yes"]})
        self.assertEqual(sum(got["counts"].values()), len(got["findings"]))


def _async(value):
    async def f(*a, **k):
        return value

    return f


class TestDoctorItem(unittest.TestCase):
    def data(self, **kw):
        base = {"as_root": True, "sshd": {"source": "sshd -T", "port": 22, "reachable": True},
                "counts": {"risk": 0, "warn": 0, "note": 0}, "findings": [], "notes": ["说明"],
                "accounts": _acc(), "attempts": _att(failed=3)}
        base.update(kw)
        return base

    def test_unreadable_config_is_warn_not_fail(self):
        for src in ("missing", "unreadable"):
            got = svc.doctor_item(self.data(sshd={"source": src, "port": 22, "reachable": None}))
            self.assertEqual(got["status"], "warn", src)
            self.assertIn("sshd", got["detail"])

    def test_intrusion_indicators_are_the_only_fail(self):
        for acc in (_acc(empty_password=["bob"]), _acc(uid0_extra=["ghost"])):
            self.assertEqual(svc.doctor_item(self.data(accounts=acc))["status"], "fail")

    def test_risk_findings_are_warn(self):
        got = svc.doctor_item(self.data(
            counts={"risk": 2, "warn": 1, "note": 0},
            findings=[{"id": "x", "severity": "risk", "title": "口令认证开着", "detail": "", "fix": ""}]))
        self.assertEqual(got["status"], "warn")
        self.assertIn("2 项要紧", got["detail"])
        self.assertIn("口令认证开着", got["detail"])

    def test_warn_only_wording(self):
        got = svc.doctor_item(self.data(counts={"risk": 0, "warn": 3, "note": 0}))
        self.assertEqual(got["status"], "warn")
        self.assertIn("3 项加固建议", got["detail"])

    def test_clean_machine_passes_and_names_the_port(self):
        got = svc.doctor_item(self.data())
        self.assertEqual(got["status"], "pass")
        self.assertIn("22", got["detail"])
        self.assertEqual(got["fix"], "")

    def test_non_root_pass_says_what_was_not_checked(self):
        self.assertIn("非 root", svc.doctor_item(self.data(as_root=False))["detail"])

    def test_never_advertised_as_fixable(self):
        risky = self.data(counts={"risk": 1, "warn": 0, "note": 0}, findings=[_RISK])
        for d in (self.data(), risky, self.data(accounts=_acc(empty_password=["bob"]))):
            self.assertFalse(svc.doctor_item(d)["fixable"])

    def test_fix_line_points_at_the_cli(self):
        got = svc.doctor_item(self.data(counts={"risk": 1, "warn": 0, "note": 0}, findings=[_RISK]))
        self.assertIn("choyeonctl ssh-audit", got["fix"])


class TestCli(unittest.TestCase):
    def audit_data(self, **kw):
        base = {"ok": True, "checked_at": "2026-10-12T00:00:00+00:00", "as_root": True,
                "sshd": {"source": "sshd -T", "effective": True, "port": 22, "reachable": True,
                         "verdict": "expected", "reason": "ok"},
                "settings": [{"key": "permitrootlogin", "label": "root 直接登录", "value": "yes",
                              "flag": "risk", "fix": "改成 prohibit-password"},
                             {"key": "pubkeyauthentication", "label": "公钥认证", "value": "yes",
                              "flag": "", "fix": ""}],
                "guard": [{"name": "fail2ban", "state": "inactive", "active": False, "installed": False}],
                "keys": _keys([_file_entry(user="root", keys=[_key(comment="laptop")])]),
                "accounts": _acc(login_shells=["root"], login_count=1, sudo_groups={"sudo": []}),
                "attempts": _att(failed=1234, distinct_ips=9, top=[{"ip": "1.1.1.1", "count": 900}],
                                 accepted=[{"method": "publickey", "count": 12}]),
                "counts": {"risk": 1, "warn": 0, "note": 0},
                "findings": [{"id": "root-password", "severity": "risk", "title": "root 与口令同时开着",
                              "detail": "撞库最省事的组合", "fix": "先确认钥匙能登进来"}],
                "notes": ["只读说明"]}
        base.update(kw)
        return base

    def test_refresh_flag_is_forwarded(self):
        audit = mock.AsyncMock(return_value=self.audit_data())
        with mock.patch.object(svc, "audit", audit):
            _run(cli.cmd_ssh_audit, "ssh-audit")
            self.assertFalse(audit.await_args.kwargs["refresh"])
            _run(cli.cmd_ssh_audit, "ssh-audit", "--refresh")
            self.assertTrue(audit.await_args.kwargs["refresh"])

    def test_text_mode_reads_like_a_report(self):
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=self.audit_data())):
            out, code = _run(cli.cmd_ssh_audit, "ssh-audit")
        self.assertEqual(code, 0)
        self.assertIn("登录安全对账", out)
        self.assertIn("root 与口令同时开着", out)
        self.assertIn("要紧", out)
        self.assertIn("root 直接登录 = yes", out)
        self.assertIn("laptop", out)
        self.assertIn("1234", out)

    def test_clean_report_says_so_instead_of_printing_nothing(self):
        clean = self.audit_data(counts={"risk": 0, "warn": 0, "note": 0}, findings=[])
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=clean)):
            out, _ = _run(cli.cmd_ssh_audit, "ssh-audit")
        self.assertIn("没有查到需要处理的问题", out)

    def test_compliant_setting_gets_no_advice_line(self):
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=self.audit_data())):
            out, _ = _run(cli.cmd_ssh_audit, "ssh-audit")
        self.assertIn("公钥认证 = yes\n", out)
        self.assertNotIn("公钥认证 = yes  ←", out)

    def test_key_bodies_never_appear_in_output(self):
        _, b64 = _key_line()
        data = self.audit_data()
        data["keys"]["files"][0]["keys"][0]["fp"] = "SHA256:see-below"
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=data)):
            out, _ = _run(cli.cmd_ssh_audit, "ssh-audit")
        self.assertIn("SHA256:see-below", out)
        self.assertNotIn(b64, out)

    def test_json_mode_emits_the_whole_report(self):
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=self.audit_data())):
            out, code = _run(cli.cmd_ssh_audit, "ssh-audit", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["counts"]["risk"], 1)
        self.assertIn("settings", payload)

    def test_unreadable_sshd_config_exits_with_a_reason(self):
        for src in ("missing", "unreadable"):
            bad = self.audit_data(sshd={"source": src, "effective": False, "port": 22,
                                        "reachable": None, "verdict": "", "reason": ""})
            with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=bad)):
                out, code = _run(cli.cmd_ssh_audit, "ssh-audit")
            self.assertEqual(code, 3, src)
            self.assertIn("读不到 sshd 配置", out)

    def test_unreadable_sshd_config_json_says_why(self):
        bad = self.audit_data(sshd={"source": "missing", "effective": False, "port": 22,
                                    "reachable": None, "verdict": "", "reason": ""})
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=bad)):
            out, code = _run(cli.cmd_ssh_audit, "ssh-audit", "--json")
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(out)["ok"])

    def test_read_only_pledge_is_printed_with_every_report(self):
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=self.audit_data())):
            out, _ = _run(cli.cmd_ssh_audit, "ssh-audit")
        self.assertIn("不写 sshd_config", out)

    def test_sudo_group_line_is_printed(self):
        data = self.audit_data(accounts=_acc(login_count=1, sudo_groups={"wheel": ["bob"]}))
        with mock.patch.object(svc, "audit", mock.AsyncMock(return_value=data)):
            out, _ = _run(cli.cmd_ssh_audit, "ssh-audit")
        self.assertIn("wheel 组：bob", out)

    def test_parser_has_the_command_with_refresh(self):
        p = cli.build_parser()
        a = p.parse_args(["ssh-audit", "--refresh", "--json"])
        self.assertTrue(a.refresh and a.json)
        self.assertIs(a.fn, cli.cmd_ssh_audit)

    def test_schema_advertises_the_command(self):
        out, code = _run(cli.cmd_schema, "schema", "--json")
        self.assertEqual(code, 0)
        names = [c["name"] for c in json.loads(out)["commands"]]
        self.assertIn("ssh-audit", names)
        entry = next(c for c in json.loads(out)["commands"] if c["name"] == "ssh-audit")
        self.assertEqual(entry["args"], ["--refresh"])


class TestWiring(unittest.TestCase):
    def test_login_is_the_last_registered_check(self):
        self.assertEqual(meta._CHECKS[-1][:2], ("login", "登录安全"))

    def test_check_ids_stay_unique(self):
        ids = [i[0] for i in meta._CHECKS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_login_is_not_one_click_fixable(self):
        # 一键修复白名单里永远不该有登录安全：动 sshd 配置就是把人锁在门外
        self.assertNotIn("login", meta.FIXABLE)

    def test_check_login_uses_the_service_verdict(self):
        marker = {"status": "warn", "detail": "x", "fix": "f", "fixable": False}
        with mock.patch.object(meta.login_audit_service, "audit", mock.AsyncMock(return_value={"ok": True})), \
            mock.patch.object(meta.login_audit_service, "doctor_item", lambda d: marker):
            self.assertIs(asyncio.run(meta._check_login()), marker)

    def test_route_is_registered_and_get_only(self):
        route = next(r for r in meta.router.routes if r.path == "/api/ssh-audit")
        self.assertEqual(route.methods, {"GET"})

    def test_route_forwards_refresh(self):
        seen = {}

        async def audit(refresh=False):
            seen["refresh"] = refresh
            return {"ok": True}

        with mock.patch.object(meta.login_audit_service, "audit", audit):
            got = asyncio.run(meta.ssh_audit(refresh=1))
        self.assertTrue(seen["refresh"])
        self.assertEqual(got, {"ok": True})

    def test_route_defaults_to_the_cache(self):
        seen = {}

        async def audit(refresh=False):
            seen["refresh"] = refresh
            return {"ok": True}

        with mock.patch.object(meta.login_audit_service, "audit", audit):
            asyncio.run(meta.ssh_audit())
        self.assertFalse(seen["refresh"])


class TestDocsContract(unittest.TestCase):
    def test_agents_md_states_the_real_check_count(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        want = f"当前 {len(meta._CHECKS)} 项"
        self.assertTrue(want in text, f"AGENTS.md 里没有「{want}」，自检项数与代码不一致")

    def test_agents_md_documents_the_command(self):
        text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("ssh-audit", text)

    def test_readme_documents_the_command(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("ssh-audit", text)

    def test_changelog_has_an_entry_for_this_batch(self):
        text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("登录安全", text)


if __name__ == "__main__":
    unittest.main()
