"""批次⑬A：挂载点与 inode 对账。

夹具里的 /proc/mounts 是从生产机原样抓回来的（28 行：5 个只读 tmpfs 的
/run/credentials/*、带 errors=remount-ro 的 ext4 根、没有 inode 表的 vfat ESP）。
自己编样例行就造不出这几个坑，所以一条都不改。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch13a_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch13a_r_"))

import cli  # noqa: E402
from app.services import storage_service as ss  # noqa: E402

GIB = 1024**3

# ---- 生产机 /proc/mounts 原文（2026-10-12 抓取）----
REAL_MOUNTS = """sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
udev /dev devtmpfs rw,nosuid,relatime,size=1995536k,nr_inodes=498884,mode=755,inode64 0 0
devpts /dev/pts devpts rw,nosuid,noexec,relatime,gid=5,mode=600,ptmxmode=000 0 0
tmpfs /run tmpfs rw,nosuid,nodev,noexec,relatime,size=401656k,mode=755,inode64 0 0
/dev/sda1 / ext4 rw,relatime,discard,errors=remount-ro 0 0
securityfs /sys/kernel/security securityfs rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /dev/shm tmpfs rw,nosuid,nodev,inode64 0 0
cgroup2 /sys/fs/cgroup cgroup2 rw,nosuid,nodev,noexec,relatime,nsdelegate,memory_recursiveprot 0 0
pstore /sys/fs/pstore pstore rw,nosuid,nodev,noexec,relatime 0 0
bpf /sys/fs/bpf bpf rw,nosuid,nodev,noexec,relatime,mode=700 0 0
systemd-1 /proc/sys/fs/binfmt_misc autofs rw,relatime,fd=41,pgrp=1,timeout=0,minproto=5,maxproto=5,direct,pipe_ino=1527 0 0
hugetlbfs /dev/hugepages hugetlbfs rw,nosuid,nodev,relatime,pagesize=2M 0 0
mqueue /dev/mqueue mqueue rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /run/lock tmpfs rw,nosuid,nodev,noexec,relatime,size=5120k,inode64 0 0
debugfs /sys/kernel/debug debugfs rw,nosuid,nodev,noexec,relatime 0 0
tracefs /sys/kernel/tracing tracefs rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev,size=2008268k,nr_inodes=1048576,inode64 0 0
tmpfs /run/credentials/systemd-journald.service tmpfs ro,nosuid,nodev,noexec,relatime,nosymfollow,size=1024k,nr_inodes=1024,mode=700,inode64,noswap 0 0
fusectl /sys/fs/fuse/connections fusectl rw,nosuid,nodev,noexec,relatime 0 0
configfs /sys/kernel/config configfs rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /run/credentials/systemd-resolved.service tmpfs ro,nosuid,nodev,noexec,relatime,nosymfollow,size=1024k,nr_inodes=1024,mode=700,inode64,noswap 0 0
/dev/sda15 /boot/efi vfat rw,relatime,fmask=0077,dmask=0077,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro 0 0
binfmt_misc /proc/sys/fs/binfmt_misc binfmt_misc rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /run/credentials/systemd-networkd.service tmpfs ro,nosuid,nodev,noexec,relatime,nosymfollow,size=1024k,nr_inodes=1024,mode=700,inode64,noswap 0 0
tmpfs /run/credentials/getty@tty1.service tmpfs ro,nosuid,nodev,noexec,relatime,nosymfollow,size=1024k,nr_inodes=1024,mode=700,inode64,noswap 0 0
tmpfs /run/credentials/serial-getty@ttyS0.service tmpfs ro,nosuid,nodev,noexec,relatime,nosymfollow,size=1024k,nr_inodes=1024,mode=700,inode64,noswap 0 0
tmpfs /run/user/0 tmpfs rw,nosuid,nodev,relatime,size=401652k,nr_inodes=100413,mode=700,inode64 0 0
"""


def vfs(blocks=100, bfree=30, bavail=25, files=1000, ffree=500, frsize=4096, bsize=4096):
    return types.SimpleNamespace(f_bsize=bsize, f_frsize=frsize, f_blocks=blocks,
                                 f_bfree=bfree, f_bavail=bavail, f_files=files, f_ffree=ffree)


def one_mount(path="/data", fstype="ext4", dev="/dev/sdb1", opts="rw,relatime"):
    return {"path": path, "device": dev, "fstype": fstype, "ro": "ro" in opts.split(","),
            "unknown": False, "total": 100 * GIB, "total_human": "100.0GB",
            "used": 10 * GIB, "used_human": "10.0GB", "free": 90 * GIB, "free_human": "90.0GB",
            "used_pct": 10, "inode_pct": 5, "panel": False}


class UnescapeTests(unittest.TestCase):
    def test_octal_escapes(self):
        self.assertEqual(ss._unescape_mount("/mnt/my\\040disk"), "/mnt/my disk")
        self.assertEqual(ss._unescape_mount("/a\\011b"), "/a\tb")
        self.assertEqual(ss._unescape_mount("/a\\012b"), "/a\nb")
        self.assertEqual(ss._unescape_mount("/back\\134slash"), "/back\\slash")

    def test_plain_and_trailing_backslash_pass_through(self):
        self.assertEqual(ss._unescape_mount("/root/www"), "/root/www")
        self.assertEqual(ss._unescape_mount("/trail\\"), "/trail\\")
        # 不合法的转义（三位不全是八进制数字）原样留着，不能把它变成别的字符。
        self.assertEqual(ss._unescape_mount("/x\\999y"), "/x\\999y")


class ParseMountsTests(unittest.TestCase):
    def test_real_table_keeps_only_the_seven_blocks_that_matter(self):
        paths = {m["path"] for m in ss._parse_mounts(REAL_MOUNTS)}
        self.assertEqual(paths, {"/", "/run", "/dev/shm", "/run/lock", "/tmp", "/boot/efi", "/run/user/0"})

    def test_tmpfs_is_not_pseudo(self):
        # RAM 盘写满会真的把构建和临时文件打死，必须进表；把它归进伪文件系统就等于又瞎了。
        self.assertNotIn("tmpfs", ss.PSEUDO_FSTYPES)
        self.assertIn("overlay", ss.PSEUDO_FSTYPES)

    def test_remount_ro_option_is_not_read_only(self):
        by = {m["path"]: m for m in ss._parse_mounts(REAL_MOUNTS)}
        self.assertFalse(by["/"]["ro"])
        self.assertFalse(by["/boot/efi"]["ro"])

    def test_read_only_ram_mounts_are_dropped(self):
        # 只读的 RAM 盘写不进东西，永远不会成为"这块盘满了"；列出五个 credentials 挂载点是噪声。
        self.assertNotIn("/run/credentials/systemd-journald.service",
                         {m["path"] for m in ss._parse_mounts(REAL_MOUNTS)})

    def test_true_read_only_survives(self):
        rows = ss._parse_mounts("/dev/sdc1 /mnt/backup ext4 ro,nosuid 0 0\n")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ro"])

    def test_last_mount_wins(self):
        text = ("/dev/sda1 /srv ext4 rw 0 0\n"
                "/dev/sdb1 /srv ext4 rw 0 0\n")
        rows = ss._parse_mounts(text)
        self.assertEqual([r["device"] for r in rows], ["/dev/sdb1"])

    def test_skips_relative_and_short_lines(self):
        rows = ss._parse_mounts("tmpfs tmpfs tmpfs rw 0 0\nbogus line\n/dev/x /x\n")
        self.assertEqual(rows, [])


class MountOfTests(unittest.TestCase):
    def test_longest_prefix_and_root(self):
        items = [{"path": "/"}, {"path": "/root"}, {"path": "/root/www"}]
        self.assertEqual(ss._mount_of("/root/www/choyeon-panel/data", items), "/root/www")
        self.assertEqual(ss._mount_of("/etc/hosts", items), "/")

    def test_sibling_with_shared_prefix_is_not_matched(self):
        self.assertIsNone(ss._mount_of("/data2/x", [{"path": "/data"}]))

    def test_exact_match(self):
        self.assertEqual(ss._mount_of("/data", [{"path": "/"}, {"path": "/data"}]), "/data")


class MountsTests(unittest.TestCase):
    def _run(self, text, table=None, data_dir="/root/www/choyeon-panel/data"):
        def fake(path):
            if table is not None and path in table:
                return table[path]
            return vfs()
        with mock.patch.object(ss.os, "statvfs", side_effect=fake), \
             mock.patch.object(ss.config, "DATA_DIR", data_dir):
            return ss._mounts(text)

    def test_reserved_blocks_count_as_used(self):
        # 全盘 100 块：空闲 30，非 root 可用 25（5 块是 ext4 预留）。面板按"写不进去的就算已用"
        # 报 75%，与磁盘余量那一栏、概览仪表同一个口径；df 的 Use% 会说 55%，因为它不把预留算数。
        out = self._run("/dev/sda1 / ext4 rw 0 0\n",
                        {"/": vfs(blocks=100, bfree=30, bavail=25, files=1000, ffree=500, frsize=1)})
        m = out["items"][0]
        self.assertEqual((m["total"], m["used"], m["free"]), (100, 75, 25))
        self.assertEqual(m["used_pct"], 75)
        self.assertEqual(m["inode_pct"], 50)

    def test_empty_filesystem_has_no_percentage(self):
        out = self._run("/dev/sda1 /snap/x squashfs ro 0 0\n",
                        {"/snap/x": vfs(blocks=0, bfree=0, bavail=0, files=0, ffree=0)})
        self.assertIsNone(out["items"][0]["used_pct"])
        self.assertIsNone(out["items"][0]["inode_pct"])

    def test_no_inode_table_reports_none_not_zero(self):
        out = self._run("/dev/sda15 /boot/efi vfat rw 0 0\n", {"/boot/efi": vfs(files=0, ffree=0)})
        self.assertIsNone(out["items"][0]["inode_pct"])

    def test_frsize_zero_falls_back_to_bsize(self):
        out = self._run("/dev/sda1 / ext4 rw 0 0\n", {"/": vfs(frsize=0, bsize=4096, blocks=10)})
        self.assertEqual(out["items"][0]["total"], 10 * 4096)

    def test_unreadable_mount_is_still_listed(self):
        def boom(path):
            raise OSError(13, "Permission denied")
        with mock.patch.object(ss.os, "statvfs", side_effect=boom), \
             mock.patch.object(ss.config, "DATA_DIR", "/data"):
            out = ss._mounts(REAL_MOUNTS)
        self.assertTrue(out["ok"])
        self.assertTrue(all(m["unknown"] for m in out["items"]))

    def test_panel_flag_and_mount(self):
        out = self._run(REAL_MOUNTS)
        self.assertEqual(out["panel_mount"], "/")
        self.assertEqual([m["path"] for m in out["items"] if m["panel"]], ["/"])

    def test_tightest_first_and_unknown_last(self):
        text = ("/dev/sda1 /a ext4 rw 0 0\n/dev/sdb1 /b ext4 rw 0 0\n/dev/sdc1 /c ext4 rw 0 0\n")
        table = {"/a": vfs(blocks=100, bfree=90, bavail=90, frsize=1),
                 "/b": vfs(blocks=100, bfree=10, bavail=10, frsize=1),
                 "/c": None}

        def fake(path):
            v = table[path]
            if v is None:
                raise OSError(6, "No such device or address")
            return v
        with mock.patch.object(ss.os, "statvfs", side_effect=fake), \
             mock.patch.object(ss.config, "DATA_DIR", "/a"):
            out = ss._mounts(text)
        self.assertEqual([m["path"] for m in out["items"]], ["/b", "/a", "/c"])

    def test_cap_and_omitted_count(self):
        text = "".join(f"/dev/sd{i} /m{i:02d} ext4 rw 0 0\n" for i in range(30))
        out = self._run(text)
        self.assertEqual(out["total"], 30)
        self.assertEqual(len(out["items"]), ss.TOP_MOUNTS)
        self.assertEqual(out["omitted"], 30 - ss.TOP_MOUNTS)

    def test_missing_proc_mounts_is_reported_not_raised(self):
        def boom(*_a, **_kw):
            raise OSError(2, "No such file or directory")
        with mock.patch("builtins.open", side_effect=boom):
            out = ss._mounts()
        self.assertFalse(out["ok"])
        self.assertEqual(out["items"], [])
        self.assertIn("/proc/mounts", out["reason"])

    def test_reads_the_real_file_by_default(self):
        if not os.path.exists(ss.MOUNTS_PATH):
            self.skipTest("这台机器上没有 /proc/mounts")
        out = ss._mounts()
        self.assertTrue(out["ok"], out.get("reason"))
        self.assertTrue(any(m["path"] == "/" for m in out["items"]))


class MountFindingsTests(unittest.TestCase):
    def _f(self, mounts):
        return ss._findings([], {"bytes": None}, {"ok": False, "path": "/data"}, [], mounts)

    def _ids(self, mounts):
        return [f["id"] for f in self._f(mounts)]

    def test_inode_thresholds(self):
        self.assertEqual(self._ids([dict(one_mount(), inode_pct=89)]), [])
        self.assertIn("inode:/data", self._ids([dict(one_mount(), inode_pct=90)]))
        risky = self._f([dict(one_mount(), inode_pct=97, used_pct=41)])
        self.assertEqual(risky[0]["severity"], ss.SEV_RISK)
        self.assertIn("容量才用 41%", risky[0]["title"])

    def test_inode_none_never_alarms(self):
        self.assertEqual(self._ids([dict(one_mount(), inode_pct=None, used_pct=10)]), [])

    def test_capacity_finding_skips_panel_mount_and_snapshots(self):
        tight = dict(one_mount("/mnt/spare"), used_pct=93, free="1.0GB", free_human="1.0GB")
        self.assertIn("mount:/mnt/spare", self._ids([tight]))
        self.assertNotIn("mount:/mnt/spare", self._ids([dict(tight, panel=True)]))
        self.assertNotIn("mount:/mnt/spare", self._ids([dict(tight, fstype="squashfs")]))

    def test_read_only_reports_dmesg_not_remount(self):
        out = self._f([dict(one_mount(), ro=True)])
        self.assertEqual([f["id"] for f in out], ["ro:/data"])
        self.assertIn("dmesg", out[0]["fix"])
        self.assertNotIn("mount -o remount", out[0]["fix"])

    def test_unknown_mount_is_a_usability_finding(self):
        out = self._f([dict(one_mount(), unknown=True, used_pct=None, free_human="—")])
        self.assertEqual(out[0]["id"], "mount:/data")
        self.assertIn("挂着却读不到", out[0]["title"])

    def test_clean_mounts_produce_nothing(self):
        self.assertEqual(self._f([one_mount(), dict(one_mount("/boot/efi"), fstype="vfat",
                                                   inode_pct=None, used_pct=7)]), [])

    def test_no_duplicate_ids(self):
        mounts = [dict(one_mount(f"/m{i}"), used_pct=95) for i in range(4)]
        ids = self._ids(mounts)
        self.assertEqual(len(ids), len(set(ids)))

    def test_findings_still_callable_without_mounts(self):
        # 老签名调用（⑫B 的用例就这么调）不能被新形参打断。
        self.assertEqual(ss._findings([], {"bytes": None}, {"ok": False}, []), [])


class AuditWiringTests(unittest.TestCase):
    def test_payload_carries_mounts_and_counts(self):
        mounts = {"ok": True, "items": [dict(one_mount("/mnt/spare"), used_pct=95,
                                             free_human="1.0GB")],
                  "total": 3, "omitted": 0, "panel_mount": "/"}

        def fake_collect():
            return {"roots": [], "disk": {"ok": False, "path": "/data"}, "big_files": [],
                    "mounts": mounts}

        async def fake_journal():
            return None, ""

        ss.reset_cache()
        self.addCleanup(ss.reset_cache)
        with mock.patch.object(ss, "_collect", fake_collect), \
             mock.patch.object(ss, "_journal_bytes", fake_journal):
            d = asyncio.run(ss.audit())
        self.assertEqual(d["counts"]["mounts"], 1)
        self.assertIs(d["mounts"], mounts)
        self.assertEqual([f["id"] for f in d["findings"]], ["mount:/mnt/spare"])

    def test_notes_and_docstring_drop_the_old_blind_claim(self):
        joined = " ".join(ss.NOTES)
        self.assertNotIn("其他挂载盘不在这一页视野里", joined)
        self.assertIn("挂载点", joined)
        self.assertNotIn("别的挂载盘在视野之外", ss.__doc__ or "")


class CliTextTests(unittest.TestCase):
    def _d(self, mounts):
        return {"as_of": "x", "cached": False, "disk": {"ok": False},
                "journal": {"bytes": None, "bytes_human": None},
                "roots": [], "big_files": [], "findings": [], "notes": [],
                "counts": {"roots": 0, "risk": 0, "warn": 0, "info": 0,
                           "bytes_total": 0, "bytes_total_human": "0B", "mounts": 2},
                "mounts": mounts}

    def test_rows_marks_and_missing_inode(self):
        text = cli._storage_text(self._d({
            "ok": True, "total": 2, "omitted": 0, "panel_mount": "/",
            "items": [dict(one_mount(), path="/", panel=True, inode_pct=None),
                      dict(one_mount("/mnt/backup"), ro=True, used_pct=93, inode_pct=97)]}))
        self.assertIn("面板数据在这", text)
        self.assertIn("inode —", text)
        self.assertIn("只读", text)
        self.assertIn("已用 93%", text)
        self.assertNotIn("None", text)

    def test_omitted_tail(self):
        text = cli._storage_text(self._d({
            "ok": True, "total": 40, "omitted": 16, "panel_mount": "/",
            "items": [one_mount()]}))
        self.assertIn("另有 16 个挂载点没列出", text)

    def test_unreadable_table_says_so(self):
        text = cli._storage_text(self._d({"ok": False, "items": [], "reason": "读不到 /proc/mounts"}))
        self.assertIn("读不到 /proc/mounts", text)

    def test_no_mount_key_is_silent(self):
        d = self._d({})
        del d["mounts"]
        self.assertNotIn("挂载点", cli._storage_text(d))


if __name__ == "__main__":
    unittest.main()
