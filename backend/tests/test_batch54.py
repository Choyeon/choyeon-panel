"""批次54：文件页的权限与属主（chmod/chown）。

对标 1Panel/宝塔时这一页只有「大小 / 时间 / 操作」，看不出应用进程能不能读这个文件，
也没有改权限的入口——排查「应用起不来，权限不对」只能进终端。这一层的红线有两条：

1. Linux 没有 lchmod，os.chmod 会跟着符号链接落到目标上，而应用目录里指向白名单外的
   链接很常见。所以对链接必须直接拒绝，而不是"改成功"后悄悄改了别处的文件。
2. setuid/setgid/sticky 是提权位，一个输入框不该有这条路径；uid/gid 也要在
   passwd/group 里查得到才放行，否则打错一个数字就把文件交给一个不存在的账号。
"""

import grp
import os
import pwd
import shutil
import stat
import tempfile
import unittest

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_files_perm_"))

from app.services import files_service  # noqa: E402


class TestPermFields(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp_perm_"))
        self.old_roots = files_service.ROOTS
        files_service.ROOTS = [self.root]

    def tearDown(self):
        files_service.ROOTS = self.old_roots
        shutil.rmtree(self.root, ignore_errors=True)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_list_dir_carries_perm_owner_group(self):
        """列表必须自带权限与属主：前端自己拼不出这九位，也不该去猜属主叫什么。"""
        fp = self._p("app.env")
        with open(fp, "w", encoding="utf8") as f:
            f.write("A=1\n")
        os.chmod(fp, 0o640)
        os.mkdir(self._p("src"))
        listing = files_service.list_dir(self.root)
        by_name = {e["name"]: e for e in listing["entries"]}
        got = by_name["app.env"]
        self.assertEqual(got["octal"], "0640")
        self.assertEqual(got["perm"], stat.filemode(0o100640))
        self.assertEqual(got["owner"], pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(got["group"], grp.getgrgid(os.getgid()).gr_name)
        self.assertEqual(by_name["src"]["perm"][0], "d")

    def test_set_perms_applies_mode_and_echoes_new_state(self):
        fp = self._p("run.sh")
        with open(fp, "w", encoding="utf8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fp, 0o600)
        r = files_service.set_perms(fp, mode="755")
        self.assertEqual(stat.S_IMODE(os.stat(fp).st_mode), 0o755)
        # 返回值就是列表要画的那几个字段：改完不必再列一次目录才知道成了什么。
        self.assertEqual(r["octal"], "0755")
        self.assertEqual(r["perm"], stat.filemode(0o100755))
        self.assertTrue(r["owner"] and r["group"])

    def test_mode_accepts_common_spellings(self):
        fp = self._p("a.txt")
        with open(fp, "w", encoding="utf8") as f:
            f.write("x")
        for raw, want in (("644", 0o644), ("0644", 0o644), (" 0600 ", 0o600), ("0o600", 0o600)):
            r = files_service.set_perms(fp, mode=raw)
            self.assertEqual(stat.S_IMODE(os.stat(fp).st_mode), want, raw)
            self.assertEqual(r["octal"], f"{want:04o}", raw)

    def test_high_bits_are_refused(self):
        """4755 这类 setuid 写法必须挡住，而不是"输入合法"就落盘。"""
        fp = self._p("setuid.sh")
        with open(fp, "w", encoding="utf8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fp, 0o755)
        for raw in ("4755", "7777", "2755", "1644"):
            with self.assertRaises(RuntimeError, msg=raw) as cm:
                files_service.set_perms(fp, mode=raw)
            self.assertIn("高位", str(cm.exception))
        self.assertEqual(stat.S_IMODE(os.stat(fp).st_mode), 0o755, "被拒的写法不该留下痕迹")

    def test_garbage_mode_is_refused(self):
        fp = self._p("b.txt")
        with open(fp, "w", encoding="utf8") as f:
            f.write("x")
        before = stat.S_IMODE(os.stat(fp).st_mode)
        for raw in ("rwxr-xr-x", "888", "64", "999", "abc", "6 4 4"):
            with self.assertRaises(RuntimeError, msg=raw):
                files_service.set_perms(fp, mode=raw)
        self.assertEqual(stat.S_IMODE(os.stat(fp).st_mode), before)

    def test_unknown_owner_or_group_is_refused(self):
        """chown 给系统上不存在的账号 = 文件从此没人能读，必须当场拦下。"""
        fp = self._p("c.txt")
        with open(fp, "w", encoding="utf8") as f:
            f.write("x")
        me = pwd.getpwuid(os.getuid()).pw_name
        bogus_user = "cp-no-such-user-54"
        bogus_group = "cp-no-such-group-54"
        with self.assertRaises(RuntimeError) as cm:
            files_service.set_perms(fp, owner=bogus_user)
        self.assertIn("用户不存在", str(cm.exception))
        with self.assertRaises(RuntimeError) as cm:
            files_service.set_perms(fp, group=bogus_group)
        self.assertIn("组不存在", str(cm.exception))
        # 数字也一样：一个没人认领的 uid 不是"高级用法"，是打错了。
        with self.assertRaises(RuntimeError):
            files_service.set_perms(fp, owner="4294967295")
        r = files_service.set_perms(fp, owner=me)
        self.assertEqual(r["owner"], me)

    def test_nothing_to_change_is_refused(self):
        fp = self._p("d.txt")
        with open(fp, "w", encoding="utf8") as f:
            f.write("x")
        with self.assertRaises(RuntimeError) as cm:
            files_service.set_perms(fp)
        self.assertIn("没有要修改的内容", str(cm.exception))
        with self.assertRaises(RuntimeError):
            files_service.set_perms(fp, mode="", owner="", group="")

    def test_symlink_is_refused_and_target_untouched(self):
        """最要的一条：链接一律不动。改成功就等于改到链接指向的目标，
        而那个目标在本测试里就在白名单外（真实机器上是指向 /etc 的链接）。"""
        outside = os.path.realpath(tempfile.mkdtemp(prefix="cp_perm_out_"))
        try:
            victim = os.path.join(outside, "secret")
            with open(victim, "w", encoding="utf8") as f:
                f.write("s")
            os.chmod(victim, 0o600)
            link = self._p("link-to-secret")
            os.symlink(victim, link)
            with self.assertRaises(RuntimeError) as cm:
                files_service.set_perms(link, mode="644")
            self.assertIn("符号链接", str(cm.exception))
            self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o600, "目标被动过")
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_path_outside_roots_is_refused(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix="cp_perm_out2_"))
        try:
            fp = os.path.join(outside, "x.txt")
            with open(fp, "w", encoding="utf8") as f:
                f.write("x")
            os.chmod(fp, 0o600)
            with self.assertRaises(RuntimeError):
                files_service.set_perms(fp, mode="644")
            self.assertEqual(stat.S_IMODE(os.stat(fp).st_mode), 0o600)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_missing_entry_says_so(self):
        with self.assertRaises(RuntimeError) as cm:
            files_service.set_perms(self._p("gone"), mode="644")
        self.assertIn("路径不存在", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
