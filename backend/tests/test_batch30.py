"""批次㉚：文件页的两个资源上限——列目录的条目数、打包的落盘容量。

两处都是"面板自己不先炸，别人先炸"的写法：

1. 一个几十万个 inode 的目录（node_modules、构建缓存）按老写法是全量 stat 再加一份几十 MB
   的 JSON。这个请求走线程池的同步 I/O，多开几个标签页能把线程占满，浏览器也会当场卡死。
2. 打包的临时文件与产物都落在**源目录所在的文件系统**，而面板以 root 运行：一次"打包整个
   应用目录"写满盘，代价是这台机器上所有业务一起停摆，不是这一次请求失败。

两个上限都要发生在省下成本的那一步之前——截断要在 stat 之前，容量预检要在写下第一个字节
之前——否则拦住了响应体积却依旧跑完了几十万次 syscall，等于没拦。
"""

import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b30_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_b30_roots_"))

from app import config  # noqa: E402
from app.services import files_service  # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web")


class _TmpRoot(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cp_b30_"))
        self.old_roots = files_service.ROOTS
        files_service.ROOTS = [self.root]
        self.old_cap = config.MAX_DIR_ENTRIES

    def tearDown(self):
        files_service.ROOTS = self.old_roots
        config.MAX_DIR_ENTRIES = self.old_cap
        shutil.rmtree(self.root, ignore_errors=True)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def _mk(self, rel, data=b"x"):
        fp = self._p(rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as f:
            f.write(data)
        return fp


class TestListDirCap(_TmpRoot):
    def test_truncation_reports_the_directory_not_the_table(self):
        for i in range(10):
            self._mk(f"f{i:02d}.txt")
        config.MAX_DIR_ENTRIES = 3
        r = files_service.list_dir(self.root)
        # total 是"目录里真有多少项"，entries 是"这次给了多少项"：前端那句「当前 N 项」
        # 说的是前者，把后者当总数会把 80 万报成 5000。
        self.assertEqual(r["total"], 10)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["maxEntries"], 3)
        self.assertEqual([e["name"] for e in r["entries"]], ["f00.txt", "f01.txt", "f02.txt"])

    def test_no_truncation_below_the_cap(self):
        self._mk("a.txt")
        self._mk("sub/b.txt")
        config.MAX_DIR_ENTRIES = 3
        r = files_service.list_dir(self.root)
        self.assertFalse(r["truncated"])
        self.assertEqual(r["total"], 2)
        self.assertEqual(len(r["entries"]), 2)

    def test_cap_is_applied_before_the_stat_storm(self):
        """上限要省的就是逐项 stat：先 stat 完再截等于什么都没拦。"""
        for i in range(12):
            self._mk(f"k{i:02d}.txt")
        config.MAX_DIR_ENTRIES = 2
        real_stat = os.stat
        seen = []

        def spy(path, *a, **kw):
            seen.append(str(path))
            return real_stat(path, *a, **kw)

        with mock.patch.object(os, "stat", spy):
            r = files_service.list_dir(self.root)
        listed = {e["name"] for e in r["entries"]}
        omitted = {f"k{i:02d}.txt" for i in range(12)} - listed
        # 目录本身允许 stat（safe_path/isdir 那条链要它），被截掉的条目一个都不许碰
        touched = {os.path.basename(p) for p in seen} & omitted
        self.assertEqual(touched, set(), f"被截掉的条目仍然被 stat 了：{sorted(touched)}")

    def test_cap_comes_from_config_not_a_hardcoded_copy(self):
        self.assertGreaterEqual(config.MAX_DIR_ENTRIES, 100)
        self.assertLessEqual(config.MAX_DIR_ENTRIES, 200000)
        self._mk("only.txt")
        self.assertEqual(files_service.list_dir(self.root)["maxEntries"], config.MAX_DIR_ENTRIES)


class TestArchiveSpacePrecheck(_TmpRoot):
    def test_refuses_when_free_space_is_below_the_source_size(self):
        self._mk("app/index.js", b"z" * 4096)
        self._mk("app/big.js", b"z" * 8192)
        # free 只有 1KB，源内容 12KB：必须在写下第一个字节之前就判掉
        usage = type(shutil.disk_usage(self.root))  # 与真实返回值同一个 namedtuple
        with mock.patch.object(
            files_service.shutil, "disk_usage", return_value=usage(10**9, 10**9, 1024)
        ):
            with self.assertRaises(RuntimeError) as ctx:
                files_service.make_archive([self._p("app", "index.js"), self._p("app", "big.js")])
        self.assertIn("磁盘只剩", str(ctx.exception))
        # 拒绝要留得干净：目录里不能剩下临时文件或半个产物
        left = [n for n in os.listdir(self._p("app")) if n.startswith(".") or n.endswith(".gz")]
        self.assertEqual(left, [], f"预检失败后仍留下了打包残留：{left}")

    def test_estimate_sums_nested_files_and_stops_at_the_bound(self):
        for i in range(40):
            self._mk(f"tree/d{i % 4}/f{i}.bin", b"a" * 1024)
        self.assertEqual(files_service._estimate_archive_bytes([self._p("tree")], 10**9), 40 * 1024)
        # 已经确定装不下时不必把剩下的 inode 数完：结果只要"超过 free"，不要精确总量
        got = files_service._estimate_archive_bytes([self._p("tree")], 3 * 1024)
        self.assertGreater(got, 3 * 1024)
        self.assertLess(got, 12 * 1024)

    def test_estimate_counts_a_symlink_as_a_link_not_its_target(self):
        """tar 存符号链接本身，预检按同一口径算，否则一个指向大文件的链接会被算成几 GB。"""
        self._mk("payload.bin", b"b" * 20000)
        os.symlink(self._p("payload.bin"), self._p("link.bin"))
        n = files_service._estimate_archive_bytes([self._p("link.bin")], 10**9)
        self.assertLess(n, 20000)

    def test_small_archive_still_works(self):
        self._mk("pack/a.txt", b"hello")
        r = files_service.make_archive([self._p("pack", "a.txt")], "out.tar.gz")
        self.assertTrue(os.path.isfile(r["path"]))
        with tarfile.open(r["path"]) as tf:
            self.assertIn("a.txt", [m.name for m in tf.getmembers()])


class TestFrontendUsesServerNumbers(unittest.TestCase):
    def test_files_page_reads_truncation_fields(self):
        src = open(os.path.join(WEB, "src", "views", "Files.vue"), encoding="utf-8").read()
        self.assertIn("data.value?.truncated", src)
        self.assertIn("data?.maxEntries", src)
        # 提示里的"共多少项"必须来自 total，而不是表格行数
        self.assertIn("data.value?.total", src)


if __name__ == "__main__":
    unittest.main()
