"""上传/解压/打包与对外错误脱敏的回归测试。

覆盖三条真实缺陷面：
1. multipart 上传的文件名可以带 `../`，解压包里可以藏越界路径与符号链接——
   两个都能把 root 写进白名单外，而面板是 root 进程。
2. 上传必须按字节落盘：把图片/zip 读成文本会静默替换成 U+FFFD 并返回 200。
3. 路由把 `str(e)` 原样回给浏览器时，OSError 会公开绝对路径与依赖结构。
4. 备份产物上传：文件名不带 `bk<id>-` 前缀就落在目录里却恢复不了，扩展名不校验
   就能把 .txt 塞进 pg 任务，目标若是符号链接则等于把内容写到备份目录外。
"""

import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_up_ut_"))

from app import config, errors  # noqa: E402
from app import database as dbm  # noqa: E402
from app.services import backup_service, files_service  # noqa: E402


class TestUploadAndExtract(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp_up_"))
        self.old_roots = files_service.ROOTS
        files_service.ROOTS = [self.root]

    def tearDown(self):
        files_service.ROOTS = self.old_roots
        shutil.rmtree(self.root, ignore_errors=True)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_safe_name_keeps_basename_only(self):
        self.assertEqual(files_service.safe_name("../../etc/cron.d/evil"), "evil")
        self.assertEqual(files_service.safe_name("C:\\temp\\ok.txt"), "ok.txt")
        for bad in ("", ".", "..", "/"):
            with self.assertRaises(RuntimeError):
                files_service.safe_name(bad)

    def test_save_upload_is_byte_exact(self):
        blob = bytes(range(256)) + "\u4e2d".encode() + b"\x00\xff"
        r = files_service.save_upload(self.root, "raw.bin", blob)
        self.assertEqual(open(r["path"], "rb").read(), blob)
        self.assertFalse(r["overwrote"])

    def test_save_upload_refuses_then_allows_overwrite(self):
        files_service.save_upload(self.root, "a.txt", b"1")
        with self.assertRaises(RuntimeError) as ctx:
            files_service.save_upload(self.root, "a.txt", b"2")
        self.assertIn("同名文件已存在", str(ctx.exception))
        self.assertEqual(open(self._p("a.txt"), "rb").read(), b"1")
        r = files_service.save_upload(self.root, "a.txt", b"2", overwrite=True)
        self.assertTrue(r["overwrote"])
        self.assertEqual(open(self._p("a.txt"), "rb").read(), b"2")

    def test_save_upload_refuses_symlink_target(self):
        link = self._p("link.txt")
        outside = tempfile.mktemp(prefix="cp_outside_")
        open(outside, "w").write("victim")
        self.addCleanup(lambda: os.path.exists(outside) and os.unlink(outside))
        os.symlink(outside, link)
        with self.assertRaises(RuntimeError):
            files_service.save_upload(self.root, "link.txt", b"pwn")
        self.assertEqual(open(outside).read(), "victim")

    def _zip(self, name, entries):
        """entries: (arcname, bytes, external_mode)"""
        path = self._p(name)
        with zipfile.ZipFile(path, "w") as z:
            for arc, data, mode in entries:
                info = zipfile.ZipInfo(arc)
                info.external_attr = mode << 16
                z.writestr(info, data)
        return path

    def test_extract_zip_rejects_traversal(self):
        self._zip(
            "evil.zip",
            [("escape.txt", b"boom", 0o100644), ("../../outside_escape.txt", b"boom", 0o100644)],
        )
        with self.assertRaises(RuntimeError) as ctx:
            files_service.extract_archive(self._p("evil.zip"))
        self.assertIn("越界", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.root, "..", "outside_escape.txt")))

    def test_extract_zip_rejects_symlink_member(self):
        path = self._p("links.zip")
        with zipfile.ZipFile(path, "w") as z:
            info = zipfile.ZipInfo("sneaky")
            info.external_attr = (0o120777 << 16)
            z.writestr(info, "/etc/passwd")
        with self.assertRaises(RuntimeError) as ctx:
            files_service.extract_archive(path)
        self.assertIn("符号链接", str(ctx.exception))

    def test_extract_tar_rejects_absolute_and_outer(self):
        src = self._p("payload.txt")
        open(src, "w").write("x")
        tgz = self._p("evil.tar.gz")
        with tarfile.open(tgz, "w:gz") as tf:
            tf.add(src, arcname="../escaped.txt")
        dest = self._p("out")
        with self.assertRaises(Exception):
            files_service.extract_archive(tgz, dest)
        self.assertFalse(os.path.exists(os.path.join(self.root, "escaped.txt")))

    def test_extract_zip_ok_roundtrip(self):
        self._zip("good.zip", [("a.txt", b"A", 0o100644), ("sub/b.txt", b"B", 0o100644)])
        dest = self._p("out")
        r = files_service.extract_archive(self._p("good.zip"), dest)
        self.assertEqual(r["files"], 2)
        self.assertEqual(open(os.path.join(dest, "sub", "b.txt"), "rb").read(), b"B")

    def test_make_archive_contains_selected_items_only(self):
        os.makedirs(self._p("d"))
        open(self._p("d", "one.txt"), "w").write("1")
        open(self._p("skip.txt"), "w").write("2")
        r = files_service.make_archive([self._p("d")], "pack.tar.gz")
        self.assertEqual(r["items"], ["d"])
        with tarfile.open(r["path"]) as tf:
            names = tf.getnames()
        self.assertIn("d/one.txt", names)
        self.assertNotIn("skip.txt", [os.path.basename(n) for n in names])

    def test_paths_outside_roots_rejected(self):
        for bad in ("/etc/passwd", "/root/www/x"):
            with self.assertRaises(RuntimeError):
                files_service.safe_path(bad)


class TestPublicErrorSurface(unittest.TestCase):
    def test_absolute_paths_are_masked(self):
        msg = errors.public_message(OSError("[Errno 13] Permission denied: '/root/www/app/data.db'"))
        self.assertNotIn("/root/www", msg)
        self.assertIn("<路径>", msg)

    def test_password_kv_is_masked(self):
        msg = errors.public_message(RuntimeError('connection failed password=Sup3rS3cr3t user="app"'))
        self.assertNotIn("Sup3rS3cr3t", msg)

    def test_programmer_errors_use_fallback(self):
        self.assertEqual(errors.public_message(AttributeError("'NoneType' object has no attribute 'x'")), "操作失败，请查看面板日志")

    def test_panel_error_text_survives(self):
        self.assertEqual(errors.public_message(errors.BadInput("kind 必须是 pg 或 app")), "kind 必须是 pg 或 app")

    def test_err_json_shape(self):
        resp = errors.err_json(ValueError("目标不存在 /var/lib/pg/x"))
        self.assertEqual(resp.status_code, 400)


class TestBackupUpload(unittest.TestCase):
    """backup_service.save_upload：产物要能被恢复逻辑认出来，否则等于没传。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_bk_up_")
        self._p_dir = mock.patch.object(backup_service, "BACKUP_DIR", self.dir)
        self._p_dir.start()
        self.addCleanup(self._p_dir.stop)
        self.bids = []

    def tearDown(self):
        for bid in self.bids:
            dbm.execute("DELETE FROM backups WHERE id=?", (bid,))
        shutil.rmtree(self.dir, ignore_errors=True)

    def _job(self, kind: str, target: str) -> int:
        bid = dbm.execute("INSERT INTO backups(id,kind,target) VALUES(?,?,?)", (900 + len(self.bids) + 1, kind, target))
        self.bids.append(bid)
        return bid

    def test_every_accepted_suffix_is_restoreable(self):
        """上传白名单和恢复判定必须同一口径，否则传进来的文件只能干放着。"""
        for kind, suffixes in backup_service._UPLOAD_SUFFIXES.items():
            for suf in suffixes:
                name = f"bk1-manual-x{suf}"
                self.assertTrue(backup_service._restoreable(kind, name), f"{kind} 允许上传 {suf} 却恢复不了")

    def test_missing_job_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            backup_service.save_upload(999999, "bk999999-a.dump.gz", b"x")
        self.assertIn("备份任务不存在", str(ctx.exception))

    def test_prefix_is_added_so_restore_can_find_it(self):
        bid = self._job("pg", "appdb")
        r = backup_service.save_upload(bid, "/tmp/../../etc/my.dump.gz", b"gz")
        self.assertEqual(r["name"], f"bk{bid}-manual-my.dump.gz")
        self.assertEqual(r["size"], 2)
        self.assertFalse(r["overwrote"])
        self.assertEqual(open(os.path.join(self.dir, r["name"]), "rb").read(), b"gz")
        # 恢复走的这条路径不能再抛"该文件不属于这个备份任务"
        self.assertEqual(backup_service._backup_file_path(bid, r["name"]), os.path.join(self.dir, r["name"]))
        self.assertTrue(backup_service._restoreable("pg", r["name"]))

    def test_existing_prefix_is_kept(self):
        bid = self._job("sqlite", "panel")
        r = backup_service.save_upload(bid, f"bk{bid}-2026-01-01-03-30-00-panel.sqlite.gz", b"a")
        self.assertEqual(r["name"], f"bk{bid}-2026-01-01-03-30-00-panel.sqlite.gz")

    def test_suffix_must_match_job_kind(self):
        bid = self._job("pg", "appdb")
        with self.assertRaises(RuntimeError) as ctx:
            backup_service.save_upload(bid, "notes.txt", b"x")
        self.assertIn(".dump.gz", str(ctx.exception))
        self.assertEqual(os.listdir(self.dir), [])

    def test_bad_file_name_rejected(self):
        bid = self._job("pg", "appdb")
        for bad in ("", "   ", ".dump.gz", "..", "../"):
            with self.assertRaises(RuntimeError):
                backup_service.save_upload(bid, bad, b"x")

    def test_refuses_then_allows_overwrite(self):
        bid = self._job("pg", "appdb")
        backup_service.save_upload(bid, "a.dump.gz", b"1")
        with self.assertRaises(RuntimeError) as ctx:
            backup_service.save_upload(bid, "a.dump.gz", b"2")
        self.assertIn("同名文件已存在", str(ctx.exception))
        self.assertEqual(open(os.path.join(self.dir, f"bk{bid}-manual-a.dump.gz"), "rb").read(), b"1")
        r = backup_service.save_upload(bid, "a.dump.gz", b"2", overwrite=True)
        self.assertTrue(r["overwrote"])
        self.assertEqual(open(os.path.join(self.dir, r["name"]), "rb").read(), b"2")

    def test_symlink_target_not_followed(self):
        bid = self._job("pg", "appdb")
        name = f"bk{bid}-manual-a.dump.gz"
        outside = tempfile.mktemp(prefix="cp_bk_out_")
        open(outside, "w").write("victim")
        self.addCleanup(lambda: os.path.exists(outside) and os.unlink(outside))
        os.symlink(outside, os.path.join(self.dir, name))
        with self.assertRaises(RuntimeError):
            backup_service.save_upload(bid, "a.dump.gz", b"pwn")
        self.assertEqual(open(outside).read(), "victim")
        # overwrite 也不允许把符号链接当成写入目标
        with self.assertRaises(RuntimeError):
            backup_service.save_upload(bid, "a.dump.gz", b"pwn", overwrite=True)
        self.assertEqual(open(outside).read(), "victim")

    def test_size_cap_is_enforced_before_writing(self):
        bid = self._job("pg", "appdb")
        with mock.patch.object(config, "MAX_UPLOAD_BYTES", 4):
            with self.assertRaises(RuntimeError) as ctx:
                backup_service.save_upload(bid, "big.dump.gz", b"12345")
        self.assertIn("MB", str(ctx.exception))
        self.assertEqual(os.listdir(self.dir), [])

    def test_unsupported_kind_rejected(self):
        """kind 与扩展名白名单对不上时不能落盘（backups 表有 CHECK，这里改字典）。"""
        bid = self._job("sqlite", "panel")
        with mock.patch.dict(backup_service._UPLOAD_SUFFIXES, {"sqlite": ()}):
            with self.assertRaises(RuntimeError) as ctx:
                backup_service.save_upload(bid, "a.sqlite.gz", b"x")
        self.assertIn("不支持", str(ctx.exception))
        self.assertEqual(os.listdir(self.dir), [])


if __name__ == "__main__":
    unittest.main()
