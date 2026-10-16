"""批次㊽A：文件复制与跨目录移动（后端搬运）。

文件页此前只有"同目录改名"，`fs_action` 明写「暂不支持跨目录移动」，也没有复制：
1Panel/宝塔 的剪切/复制/粘贴是文件管理的日常动作，缺了它用户只能回终端 `mv`/`cp`，
文件页存在的意义就没了。而搬运比改名危险的地方有四处，全部钉在下面：

1. 符号链接按链接本身搬。跟着链接走会把"指错路的 symlink"复制成一份真文件，
   顺带把白名单外的那份内容抄进白名单里；目录内的相对链接还会在副本里指向别处。
2. 跨分区要有退路。CP_FILE_ROOTS 经常跨挂载（/root/www 软链到 /data/www、备份盘单独挂），
   这种布局上 `os.rename` 必然 EXDEV；退路要么整体成功，要么明确说"现在有两份"，
   不能留下"目标半份 + 源还在"还不吭声。
3. 属主、权限（含高危位）与扩展属性原样落回。root 进程写盘默认 root:root 0600，
   搬一次站点目录就把 nginx 的读权限搬没了，而报错发生在下一次请求，对不上这次操作。
4. 副本先落临时名再排他落位：中途失败或并发同名时，目标位置上要么是完整的一份，
   要么什么都没有。

失败路径统一是 RuntimeError，且不能把 `[Errno 28] ...` 原文甩给用户（本仓既有的错误契约）。
"""

import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))

from app.services import files_service  # noqa: E402


class TestCopyMove(unittest.TestCase):
    def setUp(self):
        # 必须 realpath：/tmp 在部分平台是软链接，塞进去会让白名单判定全灭。
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp49_root_"))
        self.old_roots = files_service.ROOTS
        files_service.ROOTS = [self.root]

    def tearDown(self):
        files_service.ROOTS = self.old_roots
        shutil.rmtree(self.root, ignore_errors=True)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def _act(self, action, p, p2=None):
        return files_service.fs_action(action, p, p2)

    # ---------------------------------------------------------------- 基本语义
    def test_copy_keeps_source_and_content(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("body")
        r = self._act("copy", src, self._p("b"))
        self.assertEqual(r["path"], os.path.join(self._p("b"), "f.txt"), "落点要回给审计")
        self.assertTrue(os.path.exists(src), "复制不许动原件")
        self.assertEqual(Path(r["path"]).read_text(), "body")

    def test_move_relocates_across_dirs(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("x")
        r = self._act("move", src, self._p("b"))
        self.assertFalse(os.path.lexists(src))
        self.assertEqual(Path(r["path"]).read_text(), "x")

    def test_move_a_whole_directory(self):
        os.makedirs(self._p("a", "sub"))
        Path(self._p("a", "sub", "1.txt")).write_text("one")
        r = self._act("move", self._p("a", "sub"), self.root)
        self.assertEqual(Path(r["path"], "1.txt").read_text(), "one")
        self.assertFalse(os.path.exists(self._p("a", "sub")))

    def test_copy_preserves_mode_including_high_bits(self):
        src = self._p("s.txt")
        Path(src).write_text("x")
        os.chmod(src, 0o2640)  # setgid：掩掉就等于搬一次改一次权限
        os.makedirs(self._p("d"))
        r = self._act("copy", src, self._p("d"))
        self.assertEqual(os.stat(r["path"]).st_mode & 0o7777, 0o2640)

    def test_symlink_moves_as_the_link_not_the_target(self):
        outside = self._p("real.txt")
        Path(outside).write_text("secret")
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        link = self._p("a", "l")
        os.symlink(outside, link)
        r = self._act("move", link, self._p("b"))
        self.assertTrue(os.path.islink(r["path"]), "搬完必须是链接，不是跟过去的副本")
        self.assertEqual(os.readlink(r["path"]), outside)
        self.assertTrue(os.path.exists(outside), "链接指向的内容不该被搬走")
        self.assertFalse(os.path.lexists(link))

    def test_dangling_symlink_still_moves(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        link = self._p("a", "gone")
        os.symlink(self._p("nowhere"), link)
        r = self._act("move", link, self._p("b"))
        self.assertTrue(os.path.islink(r["path"]))

    # ---------------------------------------------------------------- 拒绝路径
    def test_move_into_own_subdirectory_is_refused(self):
        os.makedirs(self._p("a", "b"))
        Path(self._p("a", "b", "keep.txt")).write_text("k")
        with self.assertRaises(RuntimeError) as cm:
            self._act("move", self._p("a"), self._p("a", "b"))
        self.assertIn("子目录", str(cm.exception))
        self.assertTrue(Path(self._p("a", "b", "keep.txt")).exists(), "拒绝之后一棵树都不该动")

    def test_moving_into_its_own_directory_says_so_not_target_exists(self):
        os.makedirs(self._p("a"))
        src = self._p("a", "f.txt")
        Path(src).write_text("x")
        with self.assertRaises(RuntimeError) as cm:
            self._act("move", src, self._p("a"))
        self.assertIn("已经在目标目录里", str(cm.exception))

    def test_existing_target_refused_for_both_actions(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        Path(self._p("a", "f.txt")).write_text("new")
        Path(self._p("b", "f.txt")).write_text("old")
        for action in ("copy", "move"):
            with self.assertRaises(RuntimeError) as cm:
                self._act(action, self._p("a", "f.txt"), self._p("b"))
            self.assertIn("已有同名条目", str(cm.exception))
        self.assertEqual(Path(self._p("b", "f.txt")).read_text(), "old", "拒绝不能顶掉既有文件")
        self.assertTrue(os.path.exists(self._p("a", "f.txt")), "拒绝也不能动源")

    def test_root_and_outside_paths_are_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            self._act("copy", self.root, self.root)
        self.assertIn("禁止", str(cm.exception))
        os.makedirs(self._p("a"))
        Path(self._p("a", "f.txt")).write_text("x")
        with self.assertRaises(RuntimeError) as cm:
            self._act("move", self._p("a", "f.txt"), "/etc")
        self.assertIn("超出允许范围", str(cm.exception))
        self.assertTrue(os.path.exists(self._p("a", "f.txt")))

    def test_rename_still_only_renames_in_place(self):
        """跨目录只有一条路（move）。给 rename 开后门就是同一个动作两个出处。"""
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("x")
        with self.assertRaises(RuntimeError) as cm:
            self._act("rename", src, self._p("b", "f.txt"))
        msg = str(cm.exception)
        self.assertIn("跨目录", msg)
        self.assertIn("移动", msg, "报错要指到真的能做这件事的那个动作")

    # ---------------------------------------------------------------- 额度与失败
    def test_same_fs_move_does_not_ask_for_disk_budget(self):
        """同分区移动只改目录项，一个字节都不重写，不该被磁盘预检挡住。"""
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("x")
        with mock.patch.object(files_service, "assert_write_budget", side_effect=AssertionError("不该被调用")):
            self._act("move", src, self._p("b"))
        self.assertTrue(os.path.exists(self._p("b", "f.txt")))

    def test_copy_is_refused_by_budget_and_leaves_nothing(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        Path(self._p("a", "f.txt")).write_text("x" * 4096)
        with mock.patch.object(files_service, "write_budget",
                               lambda _d: {"dest": _d, "line": 80, "avail": 0, "room": 0}):
            with self.assertRaises(RuntimeError) as cm:
                self._act("copy", self._p("a", "f.txt"), self._p("b"))
        self.assertIn("未写入任何文件", str(cm.exception))
        self.assertEqual(os.listdir(self._p("b")), [], "预检失败后目录里连临时文件都不该有")

    def test_failed_copy_leaves_no_temp_and_reports_readably(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("x")
        with mock.patch.object(files_service.shutil, "copyfile",
                               side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(RuntimeError) as cm:
                self._act("copy", src, self._p("b"))
        msg = str(cm.exception)
        self.assertIn("复制失败", msg)
        self.assertFalse(msg.startswith("["), f"errno 原文不该直接甩给用户：{msg}")
        self.assertEqual(os.listdir(self._p("b")), [], "临时副本必须清掉")
        self.assertTrue(os.path.exists(src), "复制失败不该动原件")

    def test_cross_fs_move_falls_back_to_copy_then_delete(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("payload")
        real_rename = os.rename

        def exdev(a, b, *args, **kw):
            if os.path.realpath(a) == src:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(a, b, *args, **kw)

        with mock.patch.object(files_service.os, "rename", side_effect=exdev):
            r = self._act("move", src, self._p("b"))
        self.assertEqual(Path(r["path"]).read_text(), "payload")
        self.assertFalse(os.path.lexists(src), "跨分区退路要走完：源删掉")

    def test_cross_fs_move_that_cannot_delete_source_says_two_copies(self):
        os.makedirs(self._p("a"))
        os.makedirs(self._p("b"))
        src = self._p("a", "f.txt")
        Path(src).write_text("x")
        real_rename = os.rename

        def exdev(a, b, *args, **kw):
            if os.path.realpath(a) == src:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_rename(a, b, *args, **kw)

        with (
            mock.patch.object(files_service.os, "rename", side_effect=exdev),
            mock.patch.object(files_service, "_remove_entry", side_effect=OSError(errno.EBUSY, "busy")),
        ):
            with self.assertRaises(RuntimeError) as cm:
                self._act("move", src, self._p("b"))
        msg = str(cm.exception)
        self.assertIn("两处", msg, "留下两份时必须说破，不能假装移动完成")
        self.assertTrue(os.path.lexists(src) and os.path.lexists(self._p("b", "f.txt")))

    def test_placement_is_exclusive_even_if_the_target_appeared_late(self):
        """预检与落位之间有窗口：os.link 撞 EEXIST 时报并发，不许顶掉别人刚放好的文件。"""
        os.makedirs(self._p("d"))
        tmp = self._p("d", ".work")
        dst = self._p("d", "out")
        Path(tmp).write_text("mine")
        Path(dst).write_text("someone else")
        with self.assertRaises(RuntimeError) as cm:
            files_service._place(tmp, dst, False)
        self.assertIn("并发", str(cm.exception))
        self.assertEqual(Path(dst).read_text(), "someone else")
        self.assertTrue(os.path.lexists(tmp), "落位失败时临时副本还在，由调用方清理")

    def test_copy_restores_ownership_of_the_whole_tree(self):
        """目录副本逐层 chown 回源属主：只 chown 顶层等于把子文件全留给 root。"""
        body = Path("app/services/files_service.py").read_text(encoding="utf-8")
        restore = body[body.index("def _restore_tree("):body.index("def _place(")]
        self.assertIn("os.walk(dst)", restore)
        self.assertIn("os.lchown(fp", restore)
        self.assertIn("copytree(src, tmp, symlinks=True)", body)
        # 属主落不上就是真失败，不能 suppress 之后交出一份权限错了的副本
        self.assertNotIn("contextlib.suppress(OSError)", restore)


if __name__ == "__main__":
    unittest.main()
