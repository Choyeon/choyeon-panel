"""批次㉛：数据浏览与备份路径的最坏情况收口。

三类缺陷，都是"面板说了但做不到"或"一条路有守卫、另一条没有"：

1. SQL 那侧的超时只杀客户端。`wait_for + kill_group` 干掉的是 psql 进程，postgres
   后端那条语句（pg_sleep、深 OFFSET 的 count(*)、被行锁卡住的 UPDATE）感觉不到连接断开，
   会跑到自然结束，期间占着 max_connections 名额和已拿到的锁；浏览器断开也没人取消。
   所以真正生效的上界必须由服务端执行，而面板的墙钟只能排在它后面——否则面板先对用户
   报"超时"，查询却还在吃 CPU，用户重查一次就翻倍。
2. 回退路线没有字节上界。RESP 客户端读集合类型只按条目数截（200 条），每条 1MB 就是
   200MB；它先撞自己的 MAX_REPLY_BYTES 报 RedisConnectionError，于是**必然**落到
   redis-cli 回退，而那条路上是 `communicate()`——几百 MB 整个进 root 进程内存。
   psql 的 stdout 有 512KB 守卫、stderr 却直接 `.read()` 到无限，是同一个道理。
3. 恢复 SQLite 前要在应用目录里 walk 一遍找候选库。uploads/、media/ 这类树不在排除名单
   里（它们确实可能装着业务库），几百万元素能把一次点击变成几分钟的 scandir 风暴，
   而线程池 worker 还要服务文件页、备份列表与概览。这里命中上界是**报错**而不是
   返回半份结果：恢复靠名字（旧格式靠"最大的库"）定位目标文件，半份结果会把备份
   覆盖到另一个库上，那是数据丢失，不是慢一点。

前端一条同源缺陷：文件页 caption 写着「entries.length > 200 才虚拟滚动」，而表格的
virtual-scroll 是无条件开的——判词描述了一个代码里不存在的阈值。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch31_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch31_roots_"))

from app import backup_runner, config  # noqa: E402
from app.services import pg_service  # noqa: E402

REPO = Path(config.BASE)
PG_SRC = (REPO / "backend" / "app" / "services" / "pg_service.py").read_text(encoding="utf-8")
FILES_VUE = (REPO / "web" / "src" / "views" / "Files.vue").read_text(encoding="utf-8")


class _Stream:
    """假 subprocess 流：按给定块依次返回，之后是 EOF。记录是否被读到 EOF。"""

    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.read_all = False

    async def read(self, _n: int) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        self.read_all = True
        return b""


class _Proc:
    def __init__(self, chunks: list[bytes]):
        self.stdout = _Stream(chunks)
        self.returncode = 0
        self.waited = False

    async def wait(self):
        self.waited = True
        return 0


def _fake_rcli(procs: list[_Proc], limit: int, killed: list):
    """按调用顺序发假 proc，用来证明两条回退路线共用同一个字节上界。"""
    return (
        mock.patch.object(pg_service.asyncio, "create_subprocess_exec",
                          mock.AsyncMock(side_effect=procs)),
        mock.patch.object(pg_service, "redis_password", lambda: ""),
        mock.patch.object(pg_service.redis_client, "MAX_REPLY_BYTES", limit),
        mock.patch.object(pg_service, "kill_group", lambda p: killed.append(p)),
    )


class TestPsqlServerTimeouts(unittest.TestCase):
    def test_options_carry_both_server_timeouts(self):
        opts = pg_service.psql_options()
        self.assertIn("standard_conforming_strings=on", opts)
        self.assertIn(f"statement_timeout={config.PG_STATEMENT_TIMEOUT_MS}", opts)
        self.assertIn(f"lock_timeout={config.PG_LOCK_TIMEOUT_MS}", opts)

    def test_numbers_come_from_config_not_from_a_copy(self):
        with mock.patch.object(config, "PG_STATEMENT_TIMEOUT_MS", 4321), \
             mock.patch.object(config, "PG_LOCK_TIMEOUT_MS", 87):
            opts = pg_service.psql_options()
        self.assertIn("statement_timeout=4321", opts)
        self.assertIn("lock_timeout=87", opts)

    def test_zero_means_do_not_inject(self):
        """0 是"不限制"；写成 statement_timeout=0 在 PG 里同样是"立刻中止"。"""
        with mock.patch.object(config, "PG_STATEMENT_TIMEOUT_MS", 0), \
             mock.patch.object(config, "PG_LOCK_TIMEOUT_MS", 0):
            opts = pg_service.psql_options()
        self.assertNotIn("statement_timeout", opts)
        self.assertNotIn("lock_timeout", opts)
        self.assertIn("standard_conforming_strings=on", opts)

    def test_only_two_quotes_and_only_digits(self):
        """整串要被 shell 单引号包住，且值只能是数字：它会拼进 `sh -c` 的命令串。"""
        with mock.patch.object(config, "PG_STATEMENT_TIMEOUT_MS", 20000), \
             mock.patch.object(config, "PG_LOCK_TIMEOUT_MS", 5000):
            opts = pg_service.psql_options()
        self.assertEqual(opts.count("'"), 2)
        body = opts.strip().removeprefix("PGOPTIONS=").strip("'")
        self.assertTrue(re.fullmatch(r"-c standard_conforming_strings=on(?: -c [a-z_]+=\d+)*", body), body)

    def test_wall_timeout_never_fires_before_the_server(self):
        """墙钟必须排在服务端之后：先杀客户端的话，服务端继续跑而用户已经看到失败。"""
        for ms in (1, 500, 20000, 300000):
            with self.subTest(ms=ms), mock.patch.object(config, "PG_STATEMENT_TIMEOUT_MS", ms):
                self.assertGreater(pg_service.psql_wall_timeout(), ms / 1000)

    def test_wall_timeout_falls_back_when_disabled(self):
        with mock.patch.object(config, "PG_STATEMENT_TIMEOUT_MS", 0):
            self.assertEqual(pg_service.psql_wall_timeout(), pg_service.PSQL_WALL_SEC)


class TestStderrIsBounded(unittest.TestCase):
    def test_keeps_head_but_reads_to_eof(self):
        """超限不能停读：管道填满后 psql 会卡在写下一条错误上，最后只剩一句"超时"。"""
        stream = _Stream([b"A" * 100, b"B" * 100, b"C" * 100])
        out = asyncio.run(pg_service.drain_head(stream, 150))
        self.assertTrue(stream.read_all, "stderr 必须读到 EOF")
        self.assertEqual(out[:150], b"A" * 100 + b"B" * 50)
        self.assertIn(b"stderr", out[-32:], "截断要让用户看得出来")

    def test_under_limit_is_verbatim(self):
        stream = _Stream([b"ERROR:  syntax error\n"])
        self.assertEqual(asyncio.run(pg_service.drain_head(stream, 1024)), b"ERROR:  syntax error\n")

    def test_psql_uses_the_bounded_reader_for_stderr(self):
        self.assertNotIn("proc.stderr.read()", PG_SRC, "stderr 又变成无界 read() 了")
        self.assertIn("drain_head(proc.stderr", PG_SRC)


class TestRedisFallbackBound(unittest.TestCase):
    def test_no_unbounded_communicate_left(self):
        self.assertNotIn("proc.communicate(", PG_SRC, "redis-cli 回退又回到无界的 communicate()")
        self.assertIn("limit = redis_client.MAX_REPLY_BYTES", PG_SRC)

    def test_oversized_reply_is_killed_not_buffered(self):
        killed: list = []
        patches = _fake_rcli([_Proc([b"x" * 1000] * 4)], 1500, killed)
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(pg_service._rcli_raw(["GET", "k"]))
        self.assertEqual(len(killed), 1, "超限必须先把进程组杀掉，不能只报错留着它继续写")
        self.assertIn("MB", str(ctx.exception))

    def test_normal_reply_survives_and_is_reaped(self):
        killed: list = []
        proc = _Proc([b"hello\n"])
        patches = _fake_rcli([proc], 1500, killed)
        with patches[0], patches[1], patches[2], patches[3]:
            code, text = asyncio.run(pg_service._rcli_raw(["GET", "k"]))
        self.assertEqual((code, text), (0, "hello\n"))
        self.assertEqual(killed, [])
        self.assertTrue(proc.waited, "不 reap 就是僵尸进程，反复点击会攒出来")

    def test_stripped_and_exact_semantics_unchanged(self):
        """回退路线读来的 string 值要写回去：strip 会静默吃掉末尾换行。"""
        for fn, want in (("_rcli", "v1"), ("_rcli_exact", " v1 ")):
            with self.subTest(fn=fn):
                killed: list = []
                patches = _fake_rcli([_Proc([b" v1 \n"])], 1 << 20, killed)
                with patches[0], patches[1], patches[2], patches[3]:
                    got = asyncio.run(getattr(pg_service, fn)(["GET", "k"]))
                self.assertEqual(got, want)

    def test_error_reply_still_raises(self):
        killed: list = []
        patches = _fake_rcli([_Proc([b"(error) WRONGTYPE Operation against a key\n"])], 1 << 20, killed)
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(pg_service._rcli(["GET", "k"]))
        self.assertIn("WRONGTYPE", str(ctx.exception))

    def test_timeout_still_kills_the_group(self):
        """慢回包走超时分支时也必须收进程；这条守卫不能被字节上界重写弄丢。"""
        class _Stall:
            async def read(self, _n):
                await asyncio.sleep(5)
                return b""

        proc = _Proc([])
        proc.stdout = _Stall()
        killed: list = []
        patches = _fake_rcli([proc], 1 << 20, killed)

        def expire(coro, **_kw):
            coro.close()  # 不关就是 "coroutine was never awaited"，把警告留给真正的泄漏
            raise TimeoutError

        with patches[0], patches[1], patches[2], patches[3], \
             mock.patch.object(pg_service.asyncio, "wait_for", side_effect=expire):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(pg_service._rcli_raw(["GET", "k"]))
        self.assertIn("超时", str(ctx.exception))
        self.assertEqual(len(killed), 1)


class TestRowLocator(unittest.TestCase):
    def test_ctid_rejects_trailing_junk(self):
        """`$` 在 match 下允许末尾多一个换行过关，和 is_ident 修过的是同一类缺陷。"""
        for bad in ("(1,1)\n", "(1,1) ", "(1,1);", "x", "(1,1)x"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError):
                    pg_service._row_predicate({"_ctid": bad}, [])
        self.assertTrue(pg_service._row_predicate({"_ctid": "(1,1)"}, []))


class TestSqliteScanBound(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cp_batch31_scan_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _make(self, junk: int, dbs: int = 0):
        for i in range(junk):
            Path(self.root, f"asset{i}.bin").write_bytes(b"")
        for i in range(dbs):
            Path(self.root, f"db{i}.db").write_bytes(b"")

    def test_normal_scan_orders_by_size(self):
        self._make(junk=3)
        Path(self.root, "small.db").write_bytes(b"a")
        Path(self.root, "big.db").write_bytes(b"a" * 100)
        out = backup_runner.find_sqlite_dbs(self.root)
        self.assertEqual([os.path.basename(p) for p in out[:2]], ["big.db", "small.db"])

    def test_entry_cap_counts_visited_entries_not_matches(self):
        """上界要拦"扫过多少条目"：只数命中的 .db 时，几十万个 asset.bin 照样全扫完。"""
        self._make(junk=200)
        with mock.patch.object(config, "SQLITE_SCAN_MAX_ENTRIES", 50):
            with self.assertRaises(RuntimeError) as ctx:
                backup_runner.find_sqlite_dbs(self.root)
        self.assertIn("CP_SQLITE_SCAN_MAX_ENTRIES", str(ctx.exception))

    def test_time_cap_also_raises(self):
        self._make(junk=3)
        clock = iter([0] + [5] * 8)
        with mock.patch.object(config, "SQLITE_SCAN_BUDGET_SEC", 1), \
             mock.patch.object(backup_runner.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(RuntimeError) as ctx:
                backup_runner.find_sqlite_dbs(self.root)
        self.assertIn("CP_SQLITE_SCAN_BUDGET_SEC", str(ctx.exception))

    def test_refuses_instead_of_returning_a_partial_list(self):
        """半份候选会被按"最大的库"认错，直接覆盖另一个库——这里必须失败。"""
        self._make(junk=0, dbs=5)
        with mock.patch.object(config, "SQLITE_SCAN_MAX_ENTRIES", 2):
            with self.assertRaises(RuntimeError):
                backup_runner.find_sqlite_dbs(self.root)

    def test_bounds_are_read_at_call_time(self):
        """上界从 config 现取：运维改 .env 与测试改 config 都立刻生效，不是 import 时的快照。"""
        self._make(junk=10)
        with mock.patch.object(config, "SQLITE_SCAN_MAX_ENTRIES", 3):
            with self.assertRaises(RuntimeError):
                backup_runner.find_sqlite_dbs(self.root)


class TestFrontendCopyMatchesBehaviour(unittest.TestCase):
    def test_no_fake_virtual_scroll_threshold(self):
        """caption 说「>200 才虚拟滚动」，而表格永远开着 virtual-scroll：判词得删掉。"""
        self.assertIn("virtual-scroll", FILES_VUE)
        self.assertNotIn("虚拟滚动", FILES_VUE)
        self.assertNotIn("entries.length > 200", FILES_VUE)


class TestKnobsAreDocumented(unittest.TestCase):
    def test_env_example_names_every_new_knob(self):
        text = (REPO / ".env.example").read_text(encoding="utf-8")
        for name in ("CP_PG_STATEMENT_TIMEOUT_MS", "CP_PG_LOCK_TIMEOUT_MS",
                     "CP_SQLITE_SCAN_MAX_ENTRIES", "CP_SQLITE_SCAN_BUDGET_SEC"):
            self.assertIn(f"{name}=", text)


if __name__ == "__main__":
    unittest.main()
