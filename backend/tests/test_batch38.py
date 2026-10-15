"""批次㊳：备份自己要写多少字节，也由同一条线说了算。

㊲ 把「还能往这块盘写多少」收成唯一的读者 `write_budget()`，接的三个写入者（文件页打包、
解压、应用目录恢复解包）都是**按别人给的尺寸**往盘上写的入口。剩下的那一类不一样：字节数是
数据自己决定的，所以全文零判据 —— `app/backup_runner.py`（定时备份的 tar、SQLite 快照）
和 `backup_service._gunzip_to`（SQLite 恢复的解压），一行预算都没问过。
判据缺失的后果各不相同，但都指向同一个方向：

- `_gunzip_to` 最尖。产物受上传上限约束（`CP_MAX_UPLOAD_MB` 默认 8MB），而 gzip 压缩比能到
  上千倍，一个 8MB 的包可以解出 8GB；更要紧的是它的落点不是面板自己的备份目录，而是
  **目标库所在的那个应用目录**（暂存必须与真库同目录才能 `os.replace`）。root 进程把那块盘
  写满，代价不是这次恢复失败，是这台机器上所有业务一起写不进去。
  这里只能用「声明值 + 真实字节」两道：gzip 尾部声明的原始尺寸（ISIZE，模 2^32）可以先判一次，
  但它是**声明**——伪造的尾部会偏小；而 4GiB 以上的正当库又会把它绕回小数字，所以它不能当上界。
  开头那道挡「这盘本来就装不下」，边解边数那道挡「头部撒谎」。
- `sqlite_snapshot` 的中间库是未压缩的整份副本（≈源库），gz 产物在数据本来就压不动时≈中间库，
  而中间库要等 gz 写完才删：峰值是 2× 源库。它还被 `_restore_sqlite` 用来写恢复前的留档，
  那份留档是用户唯一的退路——留档写到一半撞墙，比留档被拒更糟。
- 应用 tar 的字节数由目录内容决定，而 `prune()` 在写完才跑，所以峰值是新归档 + keep 份旧归档。
  这里不能复用文件页那对固定上限（`CP_MAX_EXTRACT_MB=512`），也不能把 `node_modules` 算进尺寸：
  tar 本来就不打包它。排除名单传同一份 `EXCLUDES`，界还是 `write_budget` 那条。

顺序也是这一批的内容：**预算必须判在任何占位动作之前**。`_free_path` 是 O_CREAT|O_EXCL 原子占名，
判晚了就会留下一个 0 字节的 `bk<id>-<时间戳>.tar.gz`——它长得像一份备份、会被 `prune` 认成 keep
名额里的一份，而这次其实一个字节都没写。`sqlite_snapshot` 是唯一被放在 `try` 里判的：它的调用方
可能已经占了名，被拒时要走它自己那条清理分支把占名一起删掉。恢复侧同理——`_gunzip_to` 在
`_stop_for_overwrite` 之前，被拒时服务还在跑、目标库还是原样。
"""

import gzip
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch38_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch38_roots_"))

from app import backup_runner as R
from app.services import alerts_service, files_service
from app.services import backup_service as B

BACKEND = Path(__file__).resolve().parents[1]

# 假分区：总量固定 100 GiB、警戒线钉在 80%，于是 room = 80GiB - 已用。
# 想要 room=want 就令 avail = want + 20GiB（已用 = 100GiB - avail = 80GiB - want）。
_TOTAL = 100 * 1024**3
_FRSIZE = 4096


class _Vfs:
    f_frsize = _FRSIZE
    f_blocks = _TOTAL // _FRSIZE

    def __init__(self, avail: int):
        self.f_bavail = avail // _FRSIZE


def _body(rel: str, header: str) -> str:
    src = (BACKEND / rel).read_text(encoding="utf-8")
    start = src.index(header) + len(header)
    rest = src[start:]
    m = re.search(r"\n(?=(async\s+def |def |class ))", rest)
    return rest[: m.start()] if m else rest


def _mk_db(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE t(v TEXT); INSERT INTO t VALUES('x');")
    con.commit()
    con.close()


def _write_tree(root: str, files: dict) -> None:
    for rel, size in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\0" * size)


def _fake_dbm(rows: list):
    """`main` 在备份执行器里只用到 query_one 与 audit，两个都给替身。"""
    return mock.patch.object(
        R,
        "dbm",
        type(
            "_Dbm",
            (),
            {
                "query_one": staticmethod(lambda sql, _a=(): rows.pop(0) if "FROM apps" in str(sql) else None),
                "audit": staticmethod(lambda *_a: None),
            },
        ),
    )


class BudgetRoom(unittest.TestCase):
    """把 `write_budget()` 的读数钉成一个想要的余量（不依赖测试跑在哪块盘上）。"""

    def _budget(self, want: int):
        avail = want + (_TOTAL - _TOTAL * 80 // 100)
        line = mock.patch.object(alerts_service, "disk_thresholds", lambda: {"warn": 80, "crit": 90})
        vfs = mock.patch.object(files_service.os, "statvfs", lambda *_a: _Vfs(avail))
        line.start()
        vfs.start()
        self.addCleanup(line.stop)
        self.addCleanup(vfs.stop)


class GunzipIsBoundedByTheSameLine(BudgetRoom):
    """恢复解压：声明值先判，真实字节再数，两道都要有。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp_b38_gz_")
        self.addCleanup(shutil.rmtree, self.d, True)

    def _gz(self, data: bytes, name: str, declared: int | None = None) -> str:
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(gzip.compress(data))
        if declared is not None:
            # 合法 gz 的尾部声明恒等于实际尺寸，改它就是造一个"头部撒谎"的产物
            with open(p, "r+b") as f:
                f.seek(-4, os.SEEK_END)
                f.write(declared.to_bytes(4, "little"))
        return p

    def test_refuses_before_writing_when_the_declaration_does_not_fit(self):
        self._budget(512 * 1024)
        src = self._gz(b"y" * (4 * 1024 * 1024), "ok.sqlite.gz")
        dest = os.path.join(self.d, "staged.db")
        with self.assertRaises(RuntimeError) as ctx:
            B._gunzip_to(src, dest)
        self.assertIn("警戒线", str(ctx.exception))
        self.assertIn("未写入任何文件", str(ctx.exception))
        self.assertFalse(os.path.exists(dest), "开头就拒时一个字节都不该落进应用目录")

    def test_a_lying_trailer_is_stopped_while_counting_real_bytes(self):
        self._budget(2 * 1024 * 1024)
        src = self._gz(b"z" * (6 * 1024 * 1024), "bomb.sqlite.gz", declared=1024)
        dest = os.path.join(self.d, "staged.db")
        with self.assertRaises(RuntimeError) as ctx:
            B._gunzip_to(src, dest)
        self.assertIn("压缩炸弹", str(ctx.exception))
        self.assertLessEqual(os.path.getsize(dest), 2 * 1024 * 1024, "停下时落地的字节不能超过余量")

    def test_a_legitimate_snapshot_still_expands(self):
        self._budget(20 * 1024**3)
        data = b"select * from t;" * 200_000
        src = self._gz(data, "real.sqlite.gz")
        dest = os.path.join(self.d, "staged.db")
        B._gunzip_to(src, dest)
        self.assertEqual(os.path.getsize(dest), len(data))

    def test_no_copyfileobj_left_in_the_gunzip_body(self):
        body = _body("app/services/backup_service.py", "def _gunzip_to(")
        self.assertIn("written += len(chunk)", body)
        self.assertIn("write_budget(", body)
        self.assertNotIn("copyfileobj", body, "不数真实字节的 copyfileobj 就是无界写入")


class GzipIsizeIsADeclarationNotACeiling(unittest.TestCase):
    def test_reads_the_tail_of_a_real_gz(self):
        d = tempfile.mkdtemp(prefix="cp_b38_isize_")
        self.addCleanup(shutil.rmtree, d, True)
        data = b"a" * 7777
        p = os.path.join(d, "x.gz")
        with open(p, "wb") as f:
            f.write(gzip.compress(data))
        self.assertEqual(B._gzip_isize(p), len(data))

    def test_unreadable_or_short_file_means_no_declaration(self):
        d = tempfile.mkdtemp(prefix="cp_b38_isize2_")
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(B._gzip_isize(os.path.join(d, "missing.gz")), 0)
        short = os.path.join(d, "short.gz")
        with open(short, "wb") as f:
            f.write(b"ab")
        self.assertEqual(B._gzip_isize(short), 0)


class SnapshotAndAppBackupAskBeforeTheyOccupyNames(BudgetRoom):
    """备份执行器从「全文零判据」变成「只问同一个读者」。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="cp_b38_run_")
        self.bkdir = os.path.join(self.d, "bk")
        self.appdir = os.path.join(self.d, "app")
        os.makedirs(self.bkdir)
        os.makedirs(self.appdir)
        self.addCleanup(shutil.rmtree, self.d, True)
        p = mock.patch.object(R, "BACKUP_DIR", self.bkdir)
        p.start()
        self.addCleanup(p.stop)

    def test_sqlite_snapshot_refuses_without_leaving_the_placeholder(self):
        src = os.path.join(self.appdir, "app.db")
        _mk_db(src)
        # 调用方已经原子占名（0 字节的 dest 就在盘上），被拒时得一起清掉
        dest = R._free_path(9, "2026-01-01-00-00-00", ".sqlite.gz", self.bkdir)
        self.assertEqual(os.listdir(self.bkdir), [os.path.basename(dest)])
        self._budget(0)
        with self.assertRaises(RuntimeError) as ctx:
            R.sqlite_snapshot(src, dest)
        self.assertIn("警戒线", str(ctx.exception))
        self.assertEqual(os.listdir(self.bkdir), [], "被拒后留下了占名产物或中间库")

    def test_sqlite_snapshot_asks_for_twice_the_source(self):
        src = os.path.join(self.appdir, "app.db")
        _mk_db(src)
        need: list[int] = []
        p = mock.patch.object(R, "assert_write_budget", lambda dest, n, what: need.append(n))
        p.start()
        self.addCleanup(p.stop)
        R.sqlite_snapshot(src, os.path.join(self.bkdir, "bk9-2026-01-01-00-00-00-app.sqlite.gz"))
        self.assertEqual(need, [os.path.getsize(src) * 2], "中间库 ≈ 源库，gz 产物最坏 ≈ 中间库")
        self.assertEqual([f for f in os.listdir(self.bkdir) if f.endswith(".tmp")], [])

    def test_app_backup_checks_the_budget_before_occupying_a_name(self):
        _write_tree(self.appdir, {"keep.bin": 2 * 1024 * 1024})
        self._budget(512 * 1024)
        ran: list[int] = []
        with _fake_dbm([{"path": self.appdir}]), mock.patch.object(
            R, "run_sh", side_effect=lambda *_a: ran.append(1) or 0
        ):
            with self.assertRaises(RuntimeError):
                R.main({"id": 3, "kind": "app", "target": "p38", "keep": 3})
        self.assertEqual(ran, [], "预算不够还跑了 tar")
        self.assertEqual(os.listdir(self.bkdir), [], "拒了却留下一个 0 字节的占名产物")

    def test_app_backup_does_not_count_the_excluded_directories(self):
        # tar 不打包 node_modules，尺寸也就不该把它算进去
        _write_tree(self.appdir, {"node_modules/pad.bin": 5 * 1024 * 1024, "keep.bin": 1024 * 1024})
        self._budget(2 * 1024 * 1024)
        ran: list[int] = []
        with _fake_dbm([{"path": self.appdir}]), mock.patch.object(
            R, "run_sh", side_effect=lambda *_a: ran.append(1) or 0
        ):
            R.main({"id": 3, "kind": "app", "target": "p38", "keep": 3})
        self.assertEqual(len(ran), 1, "排除名单没生效，正当备份被自己的预检挡死")
        self.assertEqual(len(os.listdir(self.bkdir)), 1)

    def test_missing_app_directory_is_named_not_measured_as_zero(self):
        with _fake_dbm([{"path": os.path.join(self.d, "gone")}]), mock.patch.object(
            R, "run_sh", side_effect=AssertionError("目录不在时 tar 不该跑")
        ):
            with self.assertRaises(SystemExit) as ctx:
                R.main({"id": 3, "kind": "app", "target": "p38", "keep": 3})
        self.assertIn("应用目录不存在", str(ctx.exception))


class OneReaderNoSecondCeiling(unittest.TestCase):
    """形态守卫：这一批的写入者不许另造一套「还剩多少空间」。"""

    def test_backup_runner_uses_the_shared_reader_twice(self):
        src = (BACKEND / "app/backup_runner.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("assert_write_budget("), 2, "tar 分支与快照各一处，别再多也别更少")
        self.assertIn("write_budget(BACKUP_DIR)", src)
        self.assertNotIn("disk_usage", src, "shutil.disk_usage().free 是第二个读者，它不含警戒线")

    def test_gunzip_body_never_invents_its_own_line(self):
        body = _body("app/services/backup_service.py", "def _gunzip_to(")
        self.assertIn("assert_write_budget(", body)
        self.assertNotIn("disk_usage", body)
        self.assertNotIn("statvfs", body, "statvfs 的口径只有 write_budget() 一处")

    def test_budget_is_asked_before_the_destructive_step(self):
        src = (BACKEND / "app/backup_runner.py").read_text(encoding="utf-8")
        self.assertLess(
            src.index("assert_write_budget(BACKUP_DIR"),
            src.index('_free_path(bk["id"], ts, ".tar.gz"'),
            "tar 分支的预算判在占名之后，拒了会留下 0 字节产物",
        )
        snap = _body("app/backup_runner.py", "def sqlite_snapshot(")
        self.assertLess(snap.index("assert_write_budget"), snap.index("sqlite3.connect(src"))
        rs = _body("app/services/backup_service.py", "async def _restore_sqlite(")
        self.assertLess(rs.index("_gunzip_to"), rs.index("_stop_for_overwrite"), "解压被拒时服务不该已经被停")

    def test_estimate_honours_the_exclude_list(self):
        d = tempfile.mkdtemp(prefix="cp_b38_est_")
        self.addCleanup(shutil.rmtree, d, True)
        _write_tree(d, {"node_modules/pad.bin": 4096, "keep.bin": 1024})
        skip = tuple(R.EXCLUDES)
        self.assertEqual(files_service._estimate_archive_bytes([d], 10**9, skip=skip), 1024)
        self.assertEqual(files_service._estimate_archive_bytes([d], 10**9), 5120)
