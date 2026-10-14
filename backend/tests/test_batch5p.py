"""批次⑤P：备份产物到底落在哪里、保留名额被谁占掉、恢复结果有多诚实。

四条都有代码级依据，其中 1、2 会由线上探针再打一次：

1. `_restore_sqlite` 的暂存写在**备份目录**里（旧写法 `f"{src_file}.restoring-<ts>"`），
   下一步 `os.replace(tmp, dst)` 把它搬进应用目录。`CP_BACKUP_DIR` 默认 `/root/backups/panel`，
   `APP_ROOT` 默认 `/root/www` —— 本来就是两棵树，把备份目录放到另一块盘/NFS 恰恰是
   用它的主要理由，这时 `os.replace` 直接 OSError(EXDEV)。而失败点在最坏的位置：服务已停、
   目标库的 `-wal/-shm` 已删，用户只看到一句 "Cross-device link"。
2. `backup_runner.prune` 按 `bk<id>-` 前缀收集文件、按名字排序删最旧的。人工上传的产物
   叫 `bk<id>-manual-xxx`（见 `save_upload`），`'m'` 排在时间戳的 `'2'` 之后 = "最新"：
   keep=1 时刚写完的真备份会被当作"最旧那一份"当场删掉，界面却报「备份完成」。
3. `_restore_app` 的 `.panel.env` 回拷包在 `suppress(Exception)` 里，之后**无条件**记
   「保留当前 .panel.env」。回写失败时这句话是假的，实际生效的是归档里备份当时的口令。
4. `restore_backup` 不抛异常就返回 `ok: true`，日志里的 `!!` 行（服务没起来、按最大库定位、
   归档环境变量生效）对返回值毫无影响；`BackupFiles.vue` 收到 ok 一律 `msg.success('恢复完成')`，
   并把整段日志渲染成 `type="success"` 的框 —— 要人接手的那一行正好被绿色框住。
   失败路径更糟：`_restore_app` 半程失败时写进 log 的「服务保持停止、目录处于半恢复状态」
   随异常一起丢掉，界面上只剩一行 "恢复失败: …"。
"""

import asyncio
import contextlib
import errno
import io
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch5p_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch5p_roots_"))
from app import backup_runner as R
from app import config
from app import database as dbm
from app.services import apps_service, systemd_ops
from app.services import backup_service as B


def _mk_db(path: str, value: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(f"CREATE TABLE t(v TEXT); INSERT INTO t VALUES('{value}');")
    con.commit()
    con.close()


def _rows(path: str):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM t ORDER BY v").fetchall()
    finally:
        con.close()


def _idle(*_a, **_kw):
    """批次㉜ 之后恢复路径问的是 unit_state（"此刻有没有活着的进程占着文件"），替身要回整份状态表。

    has_systemd 一律钉成"有"：这几条用例测的是"没在跑就跳过停止"，不是容器分支。
    """
    st = dict(systemd_ops.UNKNOWN_STATE)
    st.update(state="inactive")
    return asyncio.sleep(0, st)


def _none(*_a, **_kw):
    return asyncio.sleep(0, None)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.bkdir = tempfile.mkdtemp(prefix="cp_p5p_bk_")
        self.appdir = tempfile.mkdtemp(prefix="cp_p5p_app_")
        for mod, attr in ((R, "BACKUP_DIR"), (B, "BACKUP_DIR")):
            p = mock.patch.object(mod, attr, self.bkdir)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for d in (self.bkdir, self.appdir):
            shutil.rmtree(d, ignore_errors=True)


class TestRestoreStaysOnSameFilesystem(_Tmp):
    """暂存必须在目标库同目录：跨挂载的 os.replace 不是原子搬移，是直接失败。"""

    def _run(self, *extra):
        _mk_db(os.path.join(self.appdir, "app.db"), "current")
        snap = os.path.join(self.bkdir, "src.db")
        _mk_db(snap, "from-snapshot")
        src = os.path.join(self.bkdir, "bk7-2026-01-01-00-00-00-app.sqlite.gz")
        R.sqlite_snapshot(snap, src)
        bk = {"id": 7, "kind": "sqlite", "target": "7"}
        log: list[str] = []
        with contextlib.ExitStack() as st:
            st.enter_context(
                mock.patch.object(B, "_sqlite_app", lambda *_a, **_kw: {"id": 7, "name": "p5p", "path": self.appdir})
            )
            st.enter_context(mock.patch.object(systemd_ops, "has_systemd", lambda: True))
            st.enter_context(mock.patch.object(systemd_ops, "unit_state", _idle))
            st.enter_context(mock.patch.object(systemd_ops, "service_action", _none))
            for p in extra:
                st.enter_context(p)
            asyncio.run(B._restore_sqlite(bk, src, log))
        return log

    def test_replace_never_crosses_directories(self):
        """把 os.replace 换成"跨目录就抛 EXDEV"，模拟备份目录在另一块盘的真实后果。"""
        real = os.replace
        crossed = []

        def strict(src, dst, *a, **kw):
            if os.path.dirname(src) != os.path.dirname(dst):
                crossed.append((src, dst))
                raise OSError(errno.EXDEV, "Cross-device link or similar Error")
            return real(src, dst, *a, **kw)

        with mock.patch.object(os, "replace", side_effect=strict):
            log = self._run()
        self.assertEqual(crossed, [], f"暂存跨了目录，换挂载点就会 EXDEV：{crossed}")
        self.assertEqual(_rows(os.path.join(self.appdir, "app.db")), [("from-snapshot",)], log)

    def test_staging_file_is_hidden_and_not_a_db_candidate(self):
        seen = {}
        real_gunzip = B._gunzip_to

        def spy(src_file, dest):
            seen["dest"] = dest
            # 暂存正躺在目录里时，它不能被 find_sqlite_dbs 认成候选库
            seen["cands"] = [os.path.basename(c) for c in R.find_sqlite_dbs(self.appdir)]
            return real_gunzip(src_file, dest)

        self._run(mock.patch.object(B, "_gunzip_to", side_effect=spy))
        tmp = seen["dest"]
        self.assertEqual(os.path.dirname(tmp), self.appdir)
        self.assertEqual(seen["cands"], ["app.db"], f"中间产物被当成候选库：{seen['cands']}")
        # 用完即消失，且绝不落进备份目录（那会出现在备份文件页上）
        self.assertFalse(os.path.exists(tmp))
        self.assertEqual([f for f in os.listdir(self.bkdir) if ".cp-restore-" in f], [])

    def test_gunzip_is_flushed_to_disk_before_replace(self):
        """os.replace 只保证重命名原子，不保证内容离开页缓存。"""
        hits = []
        real = os.fsync

        def spy(fd):
            hits.append(fd)
            return real(fd)

        self._run(mock.patch.object(os, "fsync", side_effect=spy))
        self.assertTrue(hits, "_gunzip_to 没有 fsync，断电后库里是个截断文件")


class TestPruneQuota(_Tmp):
    """保留名额只在"这个任务自动生成的备份"之间轮替。"""

    def _touch(self, *names):
        for n in names:
            with open(os.path.join(self.bkdir, n), "wb") as f:
                f.write(b"x")

    def _left(self):
        return sorted(os.listdir(self.bkdir))

    def test_manual_upload_does_not_evict_the_new_backup(self):
        # 线上事故形状：keep=1 + 一个早前上传的产物 + 刚跑完的真备份
        self._touch("bk7-manual-from-other-box.dump.gz", "bk7-2026-01-02-03-00-00.dump.gz")
        R.prune({"id": 7, "keep": 1})
        self.assertIn("bk7-2026-01-02-03-00-00.dump.gz", self._left(), "新备份被上传产物挤掉了")
        self.assertIn("bk7-manual-from-other-box.dump.gz", self._left(), "上传产物由用户自己删，不该自动清")

    def test_real_backups_still_rotate(self):
        self._touch(
            "bk7-2026-01-01-03-00-00.dump.gz",
            "bk7-2026-01-02-03-00-00.dump.gz",
            "bk7-2026-01-03-03-00-00.dump.gz",
        )
        R.prune({"id": 7, "keep": 2})
        self.assertEqual(
            self._left(),
            ["bk7-2026-01-02-03-00-00.dump.gz", "bk7-2026-01-03-03-00-00.dump.gz"],
        )

    def test_pre_restore_snapshot_counts_as_a_backup(self):
        """恢复留档是 bk<id>-<当前时间戳>-…-pre-restore.*，它占 keep 名额、随轮替被清。

        这是既有设计（⑤C/⑤G 的注释里写明了）：留档不能永不轮替，否则名额被吃干。
        """
        self._touch(
            "bk7-2026-01-01-03-00-00.dump.gz",
            "bk7-2026-01-02-03-00-00-x-pre-restore.dump.gz",
        )
        R.prune({"id": 7, "keep": 1})
        self.assertEqual(self._left(), ["bk7-2026-01-02-03-00-00-x-pre-restore.dump.gz"])

    def test_other_tasks_untouched(self):
        self._touch("bk8-2026-01-01-03-00-00.dump.gz", "bk7-2026-01-01-03-00-00.dump.gz")
        R.prune({"id": 7, "keep": 1})
        self.assertIn("bk8-2026-01-01-03-00-00.dump.gz", self._left())

    def test_string_id_still_works(self):
        """id 走 int()，来自 JSON 的字符串 id 也要能裁（且不当成正则用）。"""
        self._touch("bk7-2026-01-01-03-00-00.dump.gz", "bk7-2026-01-02-03-00-00.dump.gz")
        R.prune({"id": "7", "keep": 1})
        self.assertEqual(self._left(), ["bk7-2026-01-02-03-00-00.dump.gz"])

    def test_same_second_sequence_is_the_newer_backup(self):
        """同一秒跑两次备份时 `-2` 那份是**后**写的，不能被当成最旧的剪掉。

        线上探针实测：连点两次"立即备份"落在同一秒，第二条产物是
        `bk<id>-<同一秒>-2-app.sqlite.gz`，而按文件名字典序 `-2` 排在基础名之前
        （`2` < `a`），旧排序把刚写完的这份删掉、留下更早的那一份，界面却报备份完成。
        """
        self._touch(
            "bk7-2026-01-02-03-00-00.dump.gz",
            "bk7-2026-01-02-03-00-00-2.dump.gz",
        )
        R.prune({"id": 7, "keep": 1})
        self.assertEqual(self._left(), ["bk7-2026-01-02-03-00-00-2.dump.gz"])

    def test_same_second_rotates_in_write_order(self):
        self._touch(
            "bk7-2026-01-02-03-00-00.dump.gz",
            "bk7-2026-01-02-03-00-00-2.dump.gz",
            "bk7-2026-01-02-03-00-00-3.dump.gz",
        )
        R.prune({"id": 7, "keep": 2})
        self.assertEqual(
            self._left(),
            ["bk7-2026-01-02-03-00-00-2.dump.gz", "bk7-2026-01-02-03-00-00-3.dump.gz"],
        )

    def test_crash_leftover_tmp_is_not_a_quota_holder(self):
        """`sqlite_snapshot` 的中间库叫 `….sqlite.gz.tmp`：恢复逻辑读不了它，不配占名额。

        它带完整的时间戳前缀，只认前缀的筛选会把它算进 keep 名额，于是崩过一次之后
        每一份真备份都会被这个半成品"挤"掉一轮。
        """
        self._touch(
            "bk7-2026-01-01-03-00-00-app.sqlite.gz.tmp",
            "bk7-2026-01-02-03-00-00-app.sqlite.gz",
            "bk7-2026-01-03-03-00-00-app.sqlite.gz",
        )
        R.prune({"id": 7, "keep": 1})
        self.assertEqual(
            self._left(),
            ["bk7-2026-01-01-03-00-00-app.sqlite.gz.tmp", "bk7-2026-01-03-03-00-00-app.sqlite.gz"],
        )


class TestAppRestoreEnvHonesty(_Tmp):
    def setUp(self):
        super().setUp()
        self.bk = {"id": 7, "kind": "app", "target": "p5p"}
        self.src = os.path.join(self.bkdir, "bk7-2026-01-01-00-00-00.tar.gz")
        with tarfile.open(self.src, "w:gz") as tf:
            buf = io.BytesIO(b"restored")
            info = tarfile.TarInfo("./a.txt")
            info.size = len(buf.getvalue())
            tf.addfile(info, buf)
        with open(os.path.join(self.appdir, ".panel.env"), "w") as f:
            f.write('PW="current"\n')
        app = {"id": 7, "name": "p5p", "path": self.appdir, "unit_override": None}
        for p in (
            mock.patch.object(dbm, "query_one", return_value=app),
            # 这里的 query_one 是无条件返回 app，恢复新增的"部署进行中"闸会把 app 字典
            # 误读成一条 running 部署记录。本用例测的是 .panel.env 的回写口径，闸本身
            # 由批次㉘（test_batch28）单独覆盖。
            mock.patch.object(apps_service, "_ensure_no_deployment", lambda *_a, **_kw: None),
            mock.patch.object(systemd_ops, "has_systemd", lambda: True),
            mock.patch.object(systemd_ops, "unit_state", _idle),
            mock.patch.object(systemd_ops, "service_action", _none),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _run(self):
        log: list[str] = []
        asyncio.run(B._restore_app(self.bk, self.src, log))
        return "\n".join(log)

    def test_kept_env_is_reported_as_kept(self):
        out = self._run()
        self.assertIn("保留当前 .panel.env", out)
        self.assertNotIn("!!", out)
        self.assertIn('PW="current"', open(os.path.join(self.appdir, ".panel.env")).read())

    def test_failed_env_writeback_is_reported_as_failed(self):
        """旧写法这里无条件记「保留当前 .panel.env」，而那正好是反的。"""
        real = shutil.copy2

        def boom(src, dst, *a, **kw):
            if os.path.basename(dst) == ".panel.env":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(src, dst, *a, **kw)

        with mock.patch.object(B.shutil, "copy2", side_effect=boom):
            out = self._run()
        self.assertNotIn("保留当前 .panel.env", out)
        self.assertIn("当前 .panel.env 回写失败", out)


class TestRestoreResultContract(_Tmp):
    def _job(self, kind="sqlite"):
        name = {
            "sqlite": "bk7-2026-01-01-00-00-00-app.sqlite.gz",
            "app": "bk7-2026-01-01-00-00-00.tar.gz",
        }[kind]
        with open(os.path.join(self.bkdir, name), "wb") as f:
            f.write(b"x")
        bk = {"id": 7, "kind": kind, "target": "p5p", "schedule": "manual", "enabled": 1, "keep": 7}
        return bk, name

    def _row(self, bk):
        return mock.patch.object(
            dbm, "query_one", lambda sql, *a: bk if "FROM backups" in str(sql) else None
        )

    def test_warn_lines_are_returned_separately(self):
        bk, name = self._job()

        async def fake(_bk, _src, log):
            log.append("!! 服务启动失败，请手工启动 p5p.service")

        with self._row(bk), mock.patch.object(B, "_restore_sqlite", fake), \
                mock.patch.object(dbm, "audit", lambda *a, **kw: None):
            r = asyncio.run(B.restore_backup(bk["id"], name))
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["warn"]), 1, r)
        self.assertIn("!!", r["log"])

    def test_clean_restore_has_no_warn(self):
        bk, name = self._job()

        async def fake(_bk, _src, log):
            log.append("==> 数据已写回")

        with self._row(bk), mock.patch.object(B, "_restore_sqlite", fake), \
                mock.patch.object(dbm, "audit", lambda *a, **kw: None):
            r = asyncio.run(B.restore_backup(bk["id"], name))
        self.assertEqual(r["warn"], [], r)

    def test_failure_keeps_the_scene_notes(self):
        """半恢复时 log 里那句"服务保持停止"是唯一能照着处理的说明，不能丢。"""
        bk, name = self._job("app")

        async def fake(_bk, _src, log):
            log.append("!! 解包中断：目录可能处于半恢复状态，服务保持停止")
            raise RuntimeError("tar 解压失败")

        with self._row(bk), mock.patch.object(B, "_restore_app", fake), \
                mock.patch.object(dbm, "audit", lambda *a, **kw: None), self.assertRaises(RuntimeError) as cm:
            asyncio.run(B.restore_backup(bk["id"], name))
        msg = str(cm.exception)
        self.assertIn("tar 解压失败", msg)
        self.assertIn("服务保持停止", msg, f"失败响应里丢了现场说明：{msg}")
        self.assertIn("执行记录", msg)


class TestFrontendContract(unittest.TestCase):
    """两个前端约定只有读源码才能锁住：真 DOM 探针在批次⑤P 的线上探针里另打一次。"""

    def _read(self, view: str) -> str:
        path = os.path.join(config.BASE, "web", "src", "views", view)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_no_hardcoded_pg_target(self):
        """Backups.vue 曾把 pg 的默认 target 硬编码成别的项目的库名。"""
        src = self._read("Backups.vue")
        self.assertNotIn("'rosetta'", src)
        self.assertRegex(src, r"form\.target = null")
        self.assertIn("api.pgDbs()", src, "pg 目标下拉要来自实例里真实存在的库")

    def test_app_restore_does_not_promise_a_snapshot(self):
        """应用目录恢复没有整份快照，确认文案不能承诺"可退回"。"""
        src = self._read("BackupFiles.vue")
        self.assertIn("RESTORE_ROLLBACK", src)
        self.assertNotIn("仅能再退回本次自动留存的 pre-restore 快照", src)
        self.assertIn("r.warn", src, "恢复告警要按 warn 决定提示调性")


if __name__ == "__main__":
    unittest.main()
