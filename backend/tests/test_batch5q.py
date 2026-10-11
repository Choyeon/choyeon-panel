"""批次⑤Q：备份产物的"预检"与 nginx vhost 的实测渲染。

这一轮的缺陷全都是同一类——**面板把一次没有效果的覆盖报成成功**，而且都是在这台机器上
拿真产物量出来的，不是从文档推的：

1. 0 字节的 .sqlite.gz 解出来是 0 字节文件，`PRAGMA quick_check` 回 **ok**（SQLite 眼里
   空文件就是合法空库）。旧流程于是停服务、删 -wal、把这个空文件 os.replace 到有数据的库上，
   然后打印「数据已写回」。
2. 空 tar.gz（`tarfile.open(...,'w:gz')` 的产物）里 0 个条目，`extractall` 顺利结束、
   退出码 0，界面显示恢复完成，应用目录一个字都没变；而服务已经被恢复流程停掉了。
   面板自己的归档来自 `tar -C <app> -cf - .`，一定含 `.` 这一条，所以 0 条目=坏产物。
3. 空库的 `pg_dump -Fc | gzip` 产物灌进刚 drop/create 出来的空库，`pg_restore` 退出码 **0**、
   建出 0 张表。备份 runner 自己的注释就写着 pg_dumpall 崩溃时会留下 20 字节空档，
   而这些空档过去要等到真恢复、库已经被 drop 之后才暴露。
4. `render_vhost` 无条件写 `http2 on;`：这是 nginx 1.25.1 才有的指令，Ubuntu 22.04（1.18）
   / Debian 12（1.22）上是 `[emerg] unknown directive`，面板在那类机器上永远套不上域名；
   而本机 1.26.3 实测它对明文 :80 的 server 块也**照样通过 nginx -t**，等于顺手给 :80 开了 h2c。
5. 旧判定只看 `live/<域名>/` 目录存不存在：一张多 SAN 证书（example.com + www.example.com
   共用 live/example.com/）会让第二个域名被判"没证书"，面板据此给一个本来能上 HTTPS
   的域名写出明文 vhost。
6. `certbot --nginx` / `certbot renew` 会改写 /etc/nginx，却跑在⑤H 那把串行锁**外面**。
"""

import asyncio
import errno
import gzip
import hashlib
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from app import backup_runner as R
from app import config
from app import database as dbm
from app.services import backup_service as B
from app.services import cert_ops, nginx_ops, systemd_ops


def _db_with_rows(path: str, value: str = "current") -> None:
    con = sqlite3.connect(path)
    con.executescript(f"CREATE TABLE t(v TEXT); INSERT INTO t VALUES('{value}');")
    con.commit()
    con.close()


def _db_no_tables(path: str) -> None:
    """有合法头部、但没有一张用户表的库（先建后删即得）。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gone(x)")
    con.execute("DROP TABLE gone")
    con.commit()
    con.close()


def _table_count(path: str) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]
    finally:
        con.close()


def _gz(data: bytes, path: str) -> str:
    Path(path).write_bytes(gzip.compress(data))
    return path


def _db_rows(path: str):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM t ORDER BY v").fetchall()
    finally:
        con.close()


def _passthrough(fn, *a, **kw):
    """把 run_in_threadpool 变成同步执行：用例关心的是先后顺序，不是线程。"""
    return asyncio.sleep(0, fn(*a, **kw))


async def _no_domains(_path: str) -> dict:
    return {"domains": [], "issuer": None, "expiry": None}


def _false(*_a, **_kw):
    return asyncio.sleep(0, False)


def _none(*_a, **_kw):
    return asyncio.sleep(0, None)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.bkdir = tempfile.mkdtemp(prefix="cp_q5_bk_")
        self.appdir = tempfile.mkdtemp(prefix="cp_q5_app_")
        for mod, attr in ((R, "BACKUP_DIR"), (B, "BACKUP_DIR")):
            p = mock.patch.object(mod, attr, self.bkdir)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for d in (self.bkdir, self.appdir):
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- SQLite 产物预检
class TestSqliteStats(unittest.TestCase):
    def test_zero_byte_file_is_not_a_database(self):
        p = os.path.join(tempfile.mkdtemp(prefix="cp_q5_s_"), "e.db")
        Path(p).write_bytes(b"")
        self.addCleanup(shutil.rmtree, os.path.dirname(p), True)
        self.assertEqual(B._sqlite_stats(p), (False, None))

    def test_table_counts(self):
        d = tempfile.mkdtemp(prefix="cp_q5_s_")
        self.addCleanup(shutil.rmtree, d, True)
        full, empty = f"{d}/full.db", f"{d}/empty.db"
        _db_with_rows(full)
        _db_no_tables(empty)
        self.assertEqual(B._sqlite_stats(full), (True, 1))
        self.assertEqual(B._sqlite_stats(empty), (True, 0))

    def test_checking_does_not_initialize_the_file(self):
        """以读写方式打开 0 字节文件，SQLite 会当场把它建成空库——"检查"本身就改了产物。"""
        d = tempfile.mkdtemp(prefix="cp_q5_s_")
        self.addCleanup(shutil.rmtree, d, True)
        p = f"{d}/e.db"
        Path(p).write_bytes(b"")
        before = (os.path.getsize(p), hashlib.sha256(Path(p).read_bytes()).hexdigest())
        B._sqlite_stats(p)
        self.assertEqual((os.path.getsize(p), hashlib.sha256(Path(p).read_bytes()).hexdigest()), before)

    def test_garbage_header_rejected(self):
        d = tempfile.mkdtemp(prefix="cp_q5_s_")
        self.addCleanup(shutil.rmtree, d, True)
        p = f"{d}/x.db"
        Path(p).write_bytes(b"not a database at all, but long enough")
        self.assertEqual(B._sqlite_stats(p)[0], False)


class TestSqliteRestorePreflight(_Tmp):
    def _restore(self, snapshot_bytes: bytes, *, seed_target: bool = True):
        dst = os.path.join(self.appdir, "app.db")
        if seed_target:
            _db_with_rows(dst, "current")
        src = _gz(snapshot_bytes, os.path.join(self.bkdir, "bk7-2026-01-01-00-00-00-app.sqlite.gz"))
        actions: list[str] = []
        log: list[str] = []

        async def rec_action(_unit, action, *_a, **_kw):
            actions.append(action)
            return None

        app = {"id": 7, "name": "p5q", "path": self.appdir}
        with ExitStack() as st:
            st.enter_context(mock.patch.object(B, "_sqlite_app", lambda *_a, **_kw: dict(app)))
            st.enter_context(mock.patch.object(systemd_ops, "is_active", _false))
            st.enter_context(mock.patch.object(systemd_ops, "service_action", rec_action))
            st.enter_context(mock.patch.object(B, "run_in_threadpool", _passthrough))
            try:
                asyncio.run(B._restore_sqlite({"id": 7, "kind": "sqlite", "target": "7"}, src, log))
                err = None
            except RuntimeError as e:
                err = e
        return log, actions, dst, err

    def test_empty_snapshot_is_refused_and_target_untouched(self):
        """0 字节产物：quick_check 回 ok，旧流程照样停服务并覆盖真库。"""
        log, actions, dst, err = self._restore(b"")
        self.assertIn("不是合法的 SQLite", str(err))
        self.assertEqual(_table_count(dst), 1, "目标库必须还是原样")
        self.assertEqual(_db_rows(dst), [("current",)])
        self.assertEqual(actions, [], "预检在停服务之前：一次都不该碰 systemd")
        self.assertEqual([f for f in os.listdir(self.appdir) if ".cp-restore-" in f], [], "暂存要清掉")
        self.assertEqual([x for x in log if x.startswith("数据已写回")], [])

    def test_zero_table_snapshot_warns_but_still_restores(self):
        """空库快照在结构上合法：不拦（用户可能就是要退回空库），但界面必须知道结果是"空"。"""
        d = tempfile.mkdtemp(prefix="cp_q5_snap_")
        self.addCleanup(shutil.rmtree, d, True)
        snap = f"{d}/s.db"
        _db_no_tables(snap)
        log, actions, dst, err = self._restore(Path(snap).read_bytes())
        self.assertIsNone(err, log)
        warns = [x for x in log if x.startswith("!!")]
        self.assertTrue(warns, f"没有 `!!` 行，界面会一律报「恢复完成」：{log}")
        self.assertIn("0 张表", warns[0])
        self.assertIn("pre-restore", warns[0], "要指认得回退的那一份")
        self.assertEqual(_table_count(dst), 0)

    def test_normal_snapshot_logs_preflight(self):
        d = tempfile.mkdtemp(prefix="cp_q5_snap_")
        self.addCleanup(shutil.rmtree, d, True)
        snap = f"{d}/s.db"
        _db_with_rows(snap, "from-snapshot")
        log, _actions, dst, err = self._restore(Path(snap).read_bytes())
        self.assertIsNone(err, log)
        self.assertTrue([x for x in log if "产物预检通过" in x], log)
        self.assertEqual([x for x in log if x.startswith("!!")], [])
        self.assertEqual(_db_rows(dst), [("from-snapshot",)])


# ---------------------------------------------------------------- PG 产物预检
_TOC_OK = """;
; Archive TOC
;
3420; 0 18851 TABLE DATA public t postgres
3421; 0 16384 TABLE public t postgres
226; 16384 613 SCHEMA public postgres
3422; 0 18852 TABLE DATA public u postgres
"""
_TOC_EMPTY = """; Summary
; Dumpables TOC Entries: 0
;
; Selected TOC Entries:
;
"""


class TestPgPreflight(unittest.TestCase):
    def _run(self, code: int, out: str):
        async def fake_script(script, timeout=1800):
            self.script = script
            return {"code": code, "out": out}

        with mock.patch.object(B, "_sh_script", fake_script):
            return asyncio.run(B._pg_preflight("/tmp/x.dump.gz"))

    def test_parses_object_and_data_counts(self):
        objs, data, err = self._run(0, _TOC_OK)
        self.assertIsNone(err)
        self.assertEqual((objs, data), (4, 2))

    def test_multiword_tags_do_not_confuse_the_count(self):
        """`TABLE DATA` 是两个词：按第 4 个字段切就会一项都数不到，把有数据说成没数据。"""
        _objs, data, _err = self._run(0, "3420; 0 18851 TABLE DATA public t postgres\n")
        self.assertEqual(data, 1)

    def test_bad_archive_returns_error_text(self):
        _o, _d, err = self._run(1, "pg_restore: error: input file is too short (read 0, expected 5)")
        self.assertIn("too short", err)

    def test_script_is_read_only_and_shell_escaped(self):
        self._run(0, _TOC_OK)
        self.assertIn("pg_restore -l", self.script)
        self.assertNotIn("-O", self.script, "预检只读目录，绝不能带 restore 参数")

    def test_exit_code_comes_from_pg_restore_not_the_pipe(self):
        """`-l` 拿到目录就不再读 stdin，gzip 会被 SIGPIPE 打死（合法备份实测 left=141
        right=0）。脚本开着 pipefail，照整条管道判退出码就等于把每份正常备份都拒掉。"""
        self._run(0, _TOC_OK)
        self.assertIn("${PIPESTATUS[1]}", self.script, self.script)
        self.assertTrue(self.script.rstrip().endswith("exit ${PIPESTATUS[1]}"), self.script)


class TestPgRestorePreflightOrder(_Tmp):
    def _run(self, toc: str | None, calls: list):
        """toc=None 表示产物读不出来（pg_restore -l 非零退出）。"""
        bk = {"id": 8, "kind": "pg", "target": "p5qdb"}

        async def fake_script(script, timeout=1800):
            calls.append(script)
            if "pg_restore -l" in script:
                if toc is None:
                    return {"code": 1, "out": "pg_restore: error: input file is too short (read 0, expected 5)"}
                return {"code": 0, "out": toc}
            return {"code": 0, "out": ""}

        log: list[str] = []
        err = None
        with ExitStack() as st:
            st.enter_context(mock.patch.object(B, "_sh_script", fake_script))
            st.enter_context(mock.patch.object(B, "psql", _empty_psql))
            st.enter_context(mock.patch.object(B, "is_ident", lambda *_a, **_kw: True))
            try:
                asyncio.run(B._restore_pg(bk, os.path.join(self.bkdir, "bk8-2026-01-01-00-00-00.dump.gz"), log))
            except RuntimeError as e:
                err = e
        return log, err

    def test_unreadable_archive_touches_nothing(self):
        """坏产物必须在留档 dump 之前就被判掉：那一刻起 drop/create 就要开始了。"""
        calls: list[str] = []
        log, err = self._run(None, calls)
        self.assertIn("读不出归档目录", str(err))
        self.assertEqual(len(calls), 1, f"除了预检还跑了别的命令：{calls}")
        self.assertNotIn("dropdb", " ".join(calls))
        self.assertEqual(os.listdir(self.bkdir), [], "连留档都不该建出来")

    def test_empty_archive_still_warns_after_success(self):
        """空库 -Fc 产物灌进空库退出码是 0（本机实测）：只看退出码=把"库被清空"报成成功。"""
        calls: list[str] = []
        log, err = self._run(_TOC_EMPTY, calls)
        self.assertIsNone(err, log)
        self.assertTrue([x for x in log if x.startswith("!!")], log)
        self.assertIn("无数据状态", log[-1])
        self.assertIn("pre-restore", log[-1], "要指认得回退的那一份")

    def test_good_archive_logs_preflight_and_no_warning(self):
        calls: list[str] = []
        log, err = self._run(_TOC_OK, calls)
        self.assertIsNone(err, log)
        self.assertTrue([x for x in log if "产物预检通过：归档含 4 个对象（其中 TABLE DATA 2 项）" in x], log)
        self.assertEqual([x for x in log if x.startswith("!!")], [])


async def _empty_psql(*_a, **_kw):
    return "postgres"


# ---------------------------------------------------------------- 应用归档条目数
class TestAppArchiveMustHaveMembers(_Tmp):
    def _app(self, bid: int):
        return {"id": bid, "name": "p5q", "path": self.appdir, "unit_override": None}

    def test_empty_archive_refused_before_stopping_service(self):
        p = os.path.join(self.bkdir, "bk9-2026-01-01-00-00-00.tar.gz")
        tf = tarfile.open(p, "w:gz")
        tf.close()
        marker = Path(self.appdir, "keep.txt")
        marker.write_text("original")
        actions: list[str] = []

        async def rec_action(_unit, action, *_a, **_kw):
            actions.append(action)
            return None

        log: list[str] = []
        with ExitStack() as st:
            st.enter_context(mock.patch.object(dbm, "query_one", return_value=self._app(9)))
            st.enter_context(mock.patch.object(systemd_ops, "is_active", _false))
            st.enter_context(mock.patch.object(systemd_ops, "service_action", rec_action))
            st.enter_context(mock.patch.object(B, "run_in_threadpool", _passthrough))
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(B._restore_app({"id": 9, "kind": "app", "target": "p5q"}, p, log))
        self.assertIn("没有任何条目", str(ctx.exception))
        self.assertEqual(actions, [], "0 条目不该把服务停掉")
        self.assertEqual(marker.read_text(), "original")
        self.assertEqual(log, [], "被硬挡的恢复不该留下一行成功说明")

    def test_panel_archive_with_only_root_dir_passes(self):
        """面板自己的 `tar -cf - .` 一定含 `.` 这一条：这种产物是真备份，不能误伤。"""
        p = os.path.join(self.bkdir, "bk10-2026-01-01-00-00-00.tar.gz")
        tf = tarfile.open(p, "w:gz")
        tf.add(self.appdir, arcname=".", recursive=False)
        tf.close()
        log: list[str] = []
        with ExitStack() as st:
            st.enter_context(mock.patch.object(dbm, "query_one", return_value=self._app(10)))
            st.enter_context(mock.patch.object(systemd_ops, "is_active", _false))
            st.enter_context(mock.patch.object(systemd_ops, "service_action", _none))
            asyncio.run(B._restore_app({"id": 10, "kind": "app", "target": "p5q"}, p, log))
        self.assertTrue([x for x in log if "已解包" in x], log)


# ---------------------------------------------------------------- nginx 版本与 http2 写法
class TestHttp2SyntaxGate(unittest.TestCase):
    def setUp(self):
        nginx_ops._ver_cache = None
        self.addCleanup(setattr, nginx_ops, "_ver_cache", None)

    async def _ver(self, out: str, code: int = 0):
        async def fake_run(*_a, **_kw):
            return {"code": code, "out": out}

        with mock.patch.object(nginx_ops, "run", fake_run):
            return await nginx_ops.nginx_version(), await nginx_ops.http2_uses_new_syntax()

    def test_old_uses_inline_syntax(self):
        ver, new = asyncio.run(self._ver("nginx version: nginx/1.18.0 (Ubuntu)\n"))
        self.assertEqual(ver, (1, 18, 0))
        self.assertFalse(new)

    def test_new_uses_separate_directive(self):
        ver, new = asyncio.run(self._ver("nginx version: nginx/1.26.3\n"))
        self.assertEqual(ver, (1, 26, 3))
        self.assertTrue(new)

    def test_boundary_version(self):
        _v, new = asyncio.run(self._ver("nginx/1.25.1"))
        self.assertTrue(new)
        nginx_ops._ver_cache = None
        _v, new = asyncio.run(self._ver("nginx/1.25.0"))
        self.assertFalse(new)

    def test_unreadable_version_falls_back_to_old_syntax_and_is_not_cached(self):
        """拿不到版本时用覆盖面最广的旧写法，并且下次还要再问一次（不能把失败固化）。"""
        ver, new = asyncio.run(self._ver("", code=127))
        self.assertIsNone(ver)
        self.assertFalse(new)
        self.assertIsNone(nginx_ops._ver_cache)
        ver2, _new2 = asyncio.run(self._ver("nginx/1.26.3"))
        self.assertEqual(ver2, (1, 26, 3))


class TestRenderVhostMatrix(unittest.TestCase):
    SPEC = {"name": "demo", "domain": "demo.example.com", "port": 3000}

    def _r(self, **kw):
        return nginx_ops.render_vhost(self.SPEC, **kw)

    def test_plain_http_has_no_http2_at_all(self):
        out = self._r()
        self.assertIn("listen 80;", out)
        self.assertIn("listen [::]:80;", out)
        self.assertNotIn("http2", out, "明文 :80 上开 http2 等于开 h2c；旧版 nginx 还会 [emerg]")
        self.assertNotIn("ssl_certificate", out)

    def test_ssl_old_nginx_uses_inline_http2(self):
        out = self._r(ssl_lineage="demo.example.com", http2_new=False)
        self.assertIn("listen 443 ssl http2;", out)
        self.assertIn("listen [::]:443 ssl http2;", out)
        self.assertNotIn("http2 on;", out, "旧版不认 `http2 on;`，写进去 nginx -t 直接不过")

    def test_ssl_new_nginx_uses_directive(self):
        out = self._r(ssl_lineage="demo.example.com", http2_new=True)
        self.assertIn("listen 443 ssl;", out)
        self.assertIn("http2 on;", out)
        self.assertNotIn("ssl http2;", out)

    def test_cert_paths_use_lineage_not_domain(self):
        """一张多 SAN 证书只躺在第一个域名的目录里。"""
        out = self._r(ssl_lineage="demo.example.com", http2_new=True)
        self.assertIn(f"ssl_certificate {nginx_ops.TLS_DIR}/demo.example.com/fullchain.pem;", out)
        www = nginx_ops.render_vhost(
            {"name": "demo", "domain": "www.demo.example.com", "port": 3000},
            ssl_lineage="demo.example.com", http2_new=True)
        self.assertIn("server_name www.demo.example.com;", www)
        self.assertIn(f"ssl_certificate {nginx_ops.TLS_DIR}/demo.example.com/fullchain.pem;", www,
                      "SAN 里的第二个域名要用同一个 lineage，否则会被写成明文 vhost")
        self.assertNotIn(f"{nginx_ops.TLS_DIR}/www.demo.example.com", www)

    def test_redirect_block_stays_plain(self):
        out = self._r(ssl_lineage="demo.example.com", http2_new=True)
        _comment, redirect, main = out.split("server {", 2)
        self.assertIn("listen 80;", redirect)
        self.assertIn("return 301 https://", redirect)
        self.assertNotIn("http2", redirect)
        self.assertIn("listen 443 ssl;", main)
        self.assertIn("http2 on;", main)

    def test_hsts_only_with_ssl(self):
        self.assertNotIn("Strict-Transport-Security", self._r())
        self.assertIn("Strict-Transport-Security", self._r(ssl_lineage="demo.example.com"))


# ---------------------------------------------------------------- 证书索引
class TestCertIndex(unittest.TestCase):
    def setUp(self):
        self.tls = tempfile.mkdtemp(prefix="cp_q5_tls_")
        self.addCleanup(shutil.rmtree, self.tls, True)
        p = mock.patch.object(nginx_ops, "TLS_DIR", self.tls)
        p.start()
        self.addCleanup(p.stop)
        nginx_ops._cert_cache = None
        self.addCleanup(setattr, nginx_ops, "_cert_cache", None)

    def _lineage(self, name: str, domains: list[str]) -> None:
        d = Path(self.tls, name)
        d.mkdir(parents=True)
        (d / "fullchain.pem").write_text("PEM")
        self.meta = {name: domains}

    async def _fake_x509(self, path: str) -> dict:
        name = os.path.basename(os.path.dirname(path))
        return {"domains": self.meta.get(name, [name]), "issuer": "LE", "expiry": None}

    def test_multi_san_domain_maps_to_first_lineage(self):
        self._lineage("example.com", ["example.com", "www.example.com"])
        with mock.patch.object(cert_ops, "_openssl_x509", self._fake_x509):
            self.assertEqual(asyncio.run(nginx_ops.cert_lineage("www.example.com")), "example.com")
            self.assertEqual(asyncio.run(nginx_ops.cert_lineage("example.com")), "example.com")

    def test_domain_without_cert_is_none(self):
        self._lineage("example.com", ["example.com"])
        with mock.patch.object(cert_ops, "_openssl_x509", self._fake_x509):
            self.assertIsNone(asyncio.run(nginx_ops.cert_lineage("other.example.com")))

    def test_manual_directory_named_after_domain_still_counts(self):
        """live/<域名>/ 存在但 SAN 读不出来（手工放的证书）：不能判成没证书。"""
        d = Path(self.tls, "manual.example.com")
        d.mkdir(parents=True)
        (d / "fullchain.pem").write_text("PEM")
        with mock.patch.object(cert_ops, "_openssl_x509", _no_domains):
            self.assertEqual(asyncio.run(nginx_ops.cert_lineage("manual.example.com")), "manual.example.com")

    def test_cert_dir_without_fullchain_is_still_a_lineage(self):
        """cert_index 只收 fullchain 齐全的行，目录存在就得兜住（旧行为）。"""
        Path(self.tls, "half.example.com").mkdir(parents=True)
        with mock.patch.object(cert_ops, "_openssl_x509", _no_domains):
            self.assertEqual(asyncio.run(nginx_ops.cert_lineage("half.example.com")), "half.example.com")

    def test_index_is_cached_until_invalidated(self):
        self._lineage("example.com", ["example.com"])
        calls: list[str] = []

        async def counting(path: str) -> dict:
            calls.append(path)
            return await self._fake_x509(path)

        with mock.patch.object(cert_ops, "_openssl_x509", counting):
            asyncio.run(nginx_ops.cert_index())
            asyncio.run(nginx_ops.cert_index())
            self.assertEqual(len(calls), 1, f"60 秒内重复读证书：{calls}")
            nginx_ops.invalidate_cert_cache()
            asyncio.run(nginx_ops.cert_index())
            self.assertEqual(len(calls), 2)

    def test_unreadable_tls_dir_yields_empty_index_without_raising(self):
        with mock.patch.object(nginx_ops, "TLS_DIR", "/nonexistent-letsencrypt-dir"):
            self.assertEqual(asyncio.run(nginx_ops.cert_index()), {})


class TestApplyVhostUsesMeasuredFacts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_q5_conf_")
        self.tls = tempfile.mkdtemp(prefix="cp_q5_tls_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.addCleanup(shutil.rmtree, self.tls, True)
        self._dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [self.dir]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self._dirs)
        for mod, attr, val in ((nginx_ops, "CONF_DIR", self.dir), (nginx_ops, "TLS_DIR", self.tls)):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)
        for attr in ("_loaded_cache", "_cert_cache", "_ver_cache", "_domain_cache"):
            setattr(nginx_ops, attr, None)
            self.addCleanup(setattr, nginx_ops, attr, None)

    def _lineage(self, name: str, domains: list[str]) -> None:
        d = Path(self.tls, name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "fullchain.pem").write_text("PEM")
        self.meta = {name: domains}

    def _fake_run(self, version: str):
        async def fake(cmd, args, **_kw):
            if cmd == "nginx" and args == ["-v"]:
                return {"code": 0, "out": version}
            if cmd == "nginx" and args == ["-T"]:
                return {"code": 0, "out": f"# configuration file {self.dir}/nginx.conf:\n"}
            return {"code": 0, "out": "syntax is ok"}
        return fake

    def _apply(self, domain: str, version: str = "nginx version: nginx/1.26.3\n"):
        with ExitStack() as st:
            st.enter_context(mock.patch.object(nginx_ops, "run", self._fake_run(version)))
            st.enter_context(mock.patch.object(cert_ops, "_openssl_x509", self._fake_x509))
            asyncio.run(nginx_ops.apply_vhost({"name": "vid", "domain": domain, "port": 3100}))
        return Path(nginx_ops.vhost_path("vid")).read_text()

    async def _fake_x509(self, path: str) -> dict:
        name = os.path.basename(os.path.dirname(path))
        return {"domains": getattr(self, "meta", {}).get(name, [name]), "issuer": None, "expiry": None}

    def test_san_domain_gets_https_from_shared_lineage(self):
        self._lineage("demo.example.com", ["demo.example.com", "www.demo.example.com"])
        out = self._apply("www.demo.example.com")
        self.assertIn("listen 443 ssl;", out)
        self.assertIn(f"ssl_certificate {self.tls}/demo.example.com/fullchain.pem;", out)

    def test_old_nginx_receives_inline_http2(self):
        self._lineage("demo.example.com", ["demo.example.com"])
        out = self._apply("demo.example.com", "nginx version: nginx/1.18.0 (Ubuntu)\n")
        self.assertIn("listen 443 ssl http2;", out)
        self.assertNotIn("http2 on;", out)

    def test_no_cert_renders_plain_http_without_http2(self):
        self.meta = {}
        out = self._apply("nocert.example.com")
        self.assertIn("listen 80;", out)
        self.assertNotIn("http2", out)

    def test_unknown_domain_ownership_is_logged_as_warn_but_still_applies(self):
        """查不了 ≠ 没冲突：照常写入（有 nginx -t 兜底），但这条必须在面板日志里留下痕迹。"""
        self.meta = {}
        seen: list[str] = []

        async def unreadable():
            return None

        with ExitStack() as st:
            st.enter_context(mock.patch.object(nginx_ops, "loaded_conf_paths", unreadable))
            st.enter_context(mock.patch.object(nginx_ops, "run", self._fake_run("nginx/1.26.3")))
            st.enter_context(mock.patch.object(cert_ops, "_openssl_x509", self._fake_x509))
            st.enter_context(mock.patch.object(nginx_ops, "warn", seen.append))
            asyncio.run(nginx_ops.apply_vhost({"name": "vid", "domain": "x.example.com", "port": 3100}))
        self.assertTrue(os.path.exists(nginx_ops.vhost_path("vid")), "不能因为读不到 -T 就拒绝工作")
        self.assertTrue([s for s in seen if "未能确认" in s], f"没有如实 warn：{seen}")


# ---------------------------------------------------------------- 回滚失败要说清楚
class TestRollbackFailureIsHonest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_q5_conf_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [self.dir]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self._dirs)
        p = mock.patch.object(nginx_ops, "CONF_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        for attr in ("_loaded_cache", "_cert_cache", "_ver_cache"):
            setattr(nginx_ops, attr, None)
            self.addCleanup(setattr, nginx_ops, attr, None)

    def test_new_file_that_cannot_be_deleted_is_reported(self):
        """坏配置删不掉是最严重的分支：吞掉它再说"已回滚"等于指着一颗地雷说地面是平的。"""

        async def fake(cmd, args, **_kw):
            if cmd == "nginx" and args == ["-v"]:
                return {"code": 0, "out": "nginx/1.26.3"}
            if cmd == "nginx" and args == ["-t"]:
                return {"code": 1, "out": "[emerg] unknown directive"}
            return {"code": 0, "out": "ok"}

        def stuck(path, *a, **kw):
            raise OSError(errno.EACCES, "Permission denied")

        with mock.patch.object(nginx_ops, "run", fake), mock.patch.object(os, "unlink", side_effect=stuck):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(nginx_ops.save_app_config(f"{self.dir}/new.conf", "server { bad }"))
        msg = str(ctx.exception)
        self.assertIn("删除失败", msg)
        self.assertIn("请手工删除", msg)
        self.assertNotIn("已回滚", msg, "这时候说已回滚是假的")

    def test_apply_vhost_reports_stuck_file_too(self):
        async def fake(cmd, args, **_kw):
            if cmd == "nginx" and args == ["-v"]:
                return {"code": 0, "out": "nginx/1.26.3"}
            if cmd == "nginx" and args == ["-T"]:
                return {"code": 1, "out": "unreadable"}
            if cmd == "nginx":
                return {"code": 1, "out": "[emerg] boom"}
            return {"code": 0, "out": "ok"}

        def stuck(path, *a, **kw):
            if str(path).endswith("panel-vid.conf"):
                raise OSError(errno.EACCES, "Permission denied")
            return None

        with mock.patch.object(nginx_ops, "run", fake), mock.patch.object(os, "unlink", side_effect=stuck):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(nginx_ops.apply_vhost({"name": "vid", "domain": "s.example.com", "port": 3100}))
        self.assertIn("删除失败", str(ctx.exception))


# ---------------------------------------------------------------- certbot 也走同一把锁
class TestCertbotHoldsNginSection(unittest.TestCase):
    def test_issue_cert_waits_for_the_config_write_section(self):
        """certbot --nginx 会改写 /etc/nginx；它绕开锁就等于把 ⑤H 的串行前提拆了。"""
        d = tempfile.mkdtemp(prefix="cp_q5_conf_")
        self.addCleanup(shutil.rmtree, d, True)
        self._dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [d]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self._dirs)
        seq: list[str] = []

        async def slow(cmd, args, **_kw):
            if cmd == "nginx" and args == ["-v"]:
                return {"code": 0, "out": "nginx/1.26.3"}
            seq.append(cmd)
            if cmd == "nginx" and args == ["-t"]:
                await asyncio.sleep(0.1)  # 校验期间 certbot 若插进来就会读到半份配置
            return {"code": 0, "out": "ok"}

        async def both():
            writer = asyncio.create_task(nginx_ops.save_app_config(f"{d}/panel-a.conf", "server {}"))
            await asyncio.sleep(0.03)  # 让写方确实已经进到临界区
            certbot = asyncio.create_task(nginx_ops.issue_cert("a.example.com"))
            await asyncio.sleep(0.03)
            self.assertEqual(seq, ["nginx"], f"certbot 插进了配置写的临界区：{seq}")
            await asyncio.gather(writer, certbot)
            return seq

        with mock.patch.object(nginx_ops, "run", slow):
            seq = asyncio.run(both())
        self.assertEqual(seq, ["nginx", "systemctl", "certbot"], f"顺序不对：{seq}")

    def test_renew_holds_lock_and_invalidates_cert_cache(self):
        async def fake(cmd, args, **_kw):
            return {"code": 1 if cmd == "certbot" else 0, "out": "boom"}

        async def certs():
            return {"certs": []}

        nginx_ops._cert_cache = (0.0, {"stale": "x"})
        self.addCleanup(setattr, nginx_ops, "_cert_cache", None)
        with mock.patch.object(nginx_ops, "run", fake), mock.patch.object(cert_ops, "list_certs", certs):
            with self.assertRaises(RuntimeError):
                asyncio.run(nginx_ops.renew_certs())
        self.assertIsNone(nginx_ops._cert_cache, "续期跑过之后证书索引必须重读")

    def test_renewal_invalidates_domain_cache(self):
        """证书从无到有会改变 vhost 内容，域名映射缓存里那份端口→域名不能留着。"""
        calls: list[str] = []

        async def fake(cmd, args, **_kw):
            calls.append(" ".join([cmd, *args]))
            return {"code": 0, "out": "done"}

        seq = iter(["2026-10-20", "2027-01-19"])

        async def certs():
            return {"certs": [{"name": "demo.example.com", "expiry": next(seq)}]}

        nginx_ops._domain_cache = (0.0, {3100: ["stale.example.com"]})
        self.addCleanup(setattr, nginx_ops, "_domain_cache", None)
        with mock.patch.object(nginx_ops, "run", fake), mock.patch.object(cert_ops, "list_certs", certs):
            r = asyncio.run(nginx_ops.renew_certs())
        self.assertEqual(r["renewed"], ["demo.example.com"])
        self.assertIsNone(nginx_ops._domain_cache)
        self.assertIn("systemctl reload nginx", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
