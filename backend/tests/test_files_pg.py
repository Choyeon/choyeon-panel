"""文件管理与数据库（PostgreSQL / Redis）服务的回归测试。

覆盖两类此前实测到的缺陷：
1. 底层 OSError 直接冒到 HTTP 层，前端只能看到 "[Errno 2] ..." 这种无人可读的文本；
   更糟的是 shutil.rmtree(ignore_errors=True) 会把失败整段吞掉，接口返回 ok，
   用户以为删除成功（挂载点场景可稳定复现）。
2. pg_service 用 `-F "|"` + split("|") 解析结果：空结果集 IndexError；
   合法角色名里的 `|`/换行导致列错位；psql 默认不因出错语句置非零退出码，
   于是 "DROP ROLE 不存在的角色" 在面板上显示成功。

这些用例不依赖真实 PostgreSQL/Redis：psql、run 均被替换为受控实现。
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_ut_"))

from app.services import files_service, pg_service  # noqa: E402


# ------------------------------------------------------------------ 文件管理
class TestFilesServiceErrors(unittest.TestCase):
    """fs_action / read_text 的错误必须是可读文案，且不能假报成功。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cp_files_")
        self.old_roots = files_service.ROOTS
        files_service.ROOTS = [self.root]
        self.files = files_service

    def tearDown(self):
        files_service.ROOTS = self.old_roots
        shutil.rmtree(self.root, ignore_errors=True)

    def _p(self, *parts):
        return os.path.join(self.root, *parts)

    def _act(self, action, p, p2=None):
        # fs_action 是同步函数，直接调用：包 asyncio.run 会让测试在函数被改成
        # async 时静默失败（"coroutine was never awaited" 之外什么都不测到）。
        return self.files.fs_action(action, p, p2)

    def _assert_readable_error(self, action, p, p2=None):
        """核心断言：失败必须抛 RuntimeError，且不是 "[Errno N] ..." 原文。"""
        with self.assertRaises(RuntimeError) as cm:
            self._act(action, p, p2)
        msg = str(cm.exception)
        self.assertNotIn("Errno", msg, f"错误信息仍是 errno 原文：{msg}")
        self.assertTrue(msg, "错误信息为空")
        return msg

    def test_delete_reports_failure_when_dir_is_busy(self):
        """挂载点场景：目录本身删不掉时绝不能返回 {"ok": True}。

        真实复现是在挂载点里执行删除，rmdir 返回 EBUSY；这里直接让 rmdir 抛同一个
        错误码，保证测试不依赖 root/mount 权限。旧实现用 rmtree(ignore_errors=True)
        会把 EBUSY 整段吞掉并返回 ok。
        """
        target = self._p("mnt")
        os.makedirs(target)
        Path(target, "f").write_text("x")

        def busy_rmdir(path):
            raise OSError(16, "Device or resource busy", path)

        with (mock.patch.object(self.files.os, "rmdir", side_effect=busy_rmdir),
              self.assertRaises(RuntimeError) as cm):
            self._act("delete", target)
        self.assertIn("删除未完成", str(cm.exception))
        self.assertIn("busy", str(cm.exception))
        self.assertTrue(os.path.isdir(target), "失败的删除不该假装已完成")

    def test_delete_reports_failing_children_only(self):
        """部分失败也要如实上报：成功的文件已删，失败项列出来。"""
        target = self._p("tree")
        os.makedirs(os.path.join(target, "sub"))
        Path(target, "sub", "f").write_text("x")

        def busy(path, *a, **kw):
            raise OSError(13, "Permission denied", path)

        with (mock.patch.object(self.files.shutil, "rmtree", side_effect=busy),
              self.assertRaises(RuntimeError) as cm):
            self._act("delete", target)
        msg = str(cm.exception)
        self.assertIn("删除未完成", msg)
        self.assertIn("sub", msg)
        self.assertTrue(os.path.isdir(os.path.join(target, "sub")))

    def test_delete_missing_path(self):
        msg = self._assert_readable_error("delete", self._p("ghost"))
        # 必须命中"不存在"这条专属分支：少了前置判断时虽然也会因为 OSError 转换
        # 变成可读文案，但用户会看到"删除失败：No such file or directory"——
        # 听起来像权限/系统问题，而不是"本来就没有这个文件"。
        self.assertIn("路径不存在", msg)

    def test_delete_failure_message_is_human_readable(self):
        target = self._p("f.txt")
        Path(target).write_text("x")
        with mock.patch.object(self.files.os, "unlink",
                               side_effect=OSError(13, "Permission denied")):
            msg = self._assert_readable_error("delete", target)
        self.assertIn("Permission denied", msg)

    def test_delete_normal_tree_succeeds(self):
        tree = self._p("tree")
        os.makedirs(os.path.join(tree, "sub"))
        Path(tree, "sub", "f").write_text("x")
        self.assertEqual(self._act("delete", tree), {"ok": True})
        self.assertFalse(os.path.exists(tree))

    def test_mkdir_over_existing_file(self):
        f = self._p("afile")
        Path(f).write_text("k")
        msg = self._assert_readable_error("mkdir", f)
        self.assertIn("同名文件", msg)

    def test_mkdir_creates_and_is_idempotent(self):
        d = self._p("newdir")
        self.assertEqual(self._act("mkdir", d), {"ok": True})
        self.assertTrue(os.path.isdir(d))
        self.assertEqual(self._act("mkdir", d), {"ok": True})

    def test_rename_missing_source(self):
        msg = self._assert_readable_error("rename", self._p("ghost"), self._p("dst"))
        self.assertIn("原路径不存在", msg)

    def test_rename_success(self):
        src, dst = self._p("src.txt"), self._p("dst.txt")
        Path(src).write_text("body")
        self.assertEqual(self._act("rename", src, dst), {"ok": True})
        self.assertFalse(os.path.exists(src))
        self.assertEqual(Path(dst).read_text(), "body")

    def test_rename_still_rejects_cross_dir_and_existing_target(self):
        os.makedirs(self._p("d1"))
        os.makedirs(self._p("d2"))
        src = self._p("d1", "a")
        Path(src).write_text("x")
        with self.assertRaises(RuntimeError) as cm:
            self._act("rename", src, self._p("d2", "a"))
        self.assertIn("跨目录", str(cm.exception))
        same_dir = self._p("d1", "b")
        Path(same_dir).write_text("x")
        with self.assertRaises(RuntimeError) as cm:
            self._act("rename", src, same_dir)
        self.assertIn("目标已存在", str(cm.exception))

    def test_read_text_on_directory(self):
        d = self._p("adir")
        os.makedirs(d)
        with self.assertRaises(RuntimeError) as cm:
            self.files.read_text(d)
        self.assertNotIn("Errno", str(cm.exception))
        self.assertIn("不是文件", str(cm.exception))

    def test_symlink_escape_still_blocked(self):
        """加固项本身不能被这次改动削弱：越界软链接仍要拒绝。"""
        outside = tempfile.mkdtemp(prefix="cp_outside_")
        try:
            Path(outside, "secret.txt").write_text("no")
            link = self._p("link.txt")
            os.symlink(os.path.join(outside, "secret.txt"), link)
            for action, args in (("delete", (link,)), ("rename", (link, self._p("x2")))):
                with self.assertRaises(RuntimeError) as cm:
                    self._act(action, *args)
                self.assertIn("超出允许范围", str(cm.exception))
            self.assertTrue(Path(outside, "secret.txt").exists())
        finally:
            shutil.rmtree(outside, ignore_errors=True)


# ------------------------------------------------------------------ PostgreSQL
class TestPgParsing(unittest.TestCase):
    """psql 输出解析：空集、含分隔符的名字、出错语句，都必须有正确行为。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_list_roles_empty_result_is_empty_list(self):
        # 真实 psql 在空结果集上的 stdout 经 strip 后是空串——旧实现正是在这里 IndexError
        with mock.patch.object(pg_service, "psql", self._fake("")):
            self.assertEqual(self._run(pg_service.list_roles()), [])
        with mock.patch.object(pg_service, "psql", self._fake("[]")):
            self.assertEqual(self._run(pg_service.list_dbs()), [])

    def _fake(self, out):
        async def fake_psql(sql):
            fake_psql.sqls.append(sql)
            return out
        fake_psql.sqls = []
        return fake_psql

    def test_list_roles_parses_json_rows(self):
        payload = json.dumps([
            {"name": "a|b", "super": False, "login": True,
             "createrole": False, "createdb": False},
            {"name": "nl\nrole", "super": True, "login": False,
             "createrole": True, "createdb": True},
        ])
        with mock.patch.object(pg_service, "psql", self._fake(payload)):
            rows = self._run(pg_service.list_roles())
        self.assertEqual([r["name"] for r in rows], ["a|b", "nl\nrole"],
                         "含 | 与换行的合法角色名不该被拆列")
        self.assertEqual(rows[0], {"name": "a|b", "super": False, "login": True,
                                   "createrole": False, "createdb": False},
                         "布尔列必须按 JSON 的 true/false 映射，而不是旧的 't' 比较")
        self.assertTrue(rows[1]["super"])
        self.assertTrue(rows[1]["createdb"])
        self.assertFalse(rows[1]["login"])

    def test_list_dbs_keeps_order_and_int_conns(self):
        payload = json.dumps([
            {"name": "app", "owner": "role|x", "size": "8 MB", "conns": 3},
            {"name": "postgres", "owner": "postgres", "size": "9 MB", "conns": None},
        ])
        fake = self._fake(payload)
        with mock.patch.object(pg_service, "psql", fake):
            rows = self._run(pg_service.list_dbs())
        self.assertEqual([r["name"] for r in rows], ["app", "postgres"])
        self.assertEqual(rows[0]["conns"], 3)
        self.assertEqual(rows[1]["conns"], 0, "conns 为 NULL 时不该显示成 None")
        self.assertIn("json_agg", fake.sqls[0])

    def test_psql_rejects_non_list_payload(self):
        with mock.patch.object(pg_service, "psql", self._fake('{"a": 1}')):
            self.assertEqual(self._run(pg_service.psql_json("SELECT 1")), [])

    def test_psql_rejects_garbage_payload(self):
        with (mock.patch.object(pg_service, "psql", self._fake("not json")),
              self.assertRaises(RuntimeError) as cm):
            self._run(pg_service.psql_json("SELECT 1"))
        self.assertIn("无法解析", str(cm.exception))

    def test_error_stop_is_in_psql_command(self):
        """回归：没有 ON_ERROR_STOP，psql 出错仍退出 0，面板会把失败显示成成功。"""
        self.assertIn("ON_ERROR_STOP=1", pg_service.PSQL_BASE)

    def test_on_error_stop_returncode_maps_to_first_error_line(self):
        captured = {}

        class FakeProc:
            pid = 4242
            returncode = 3

            def __init__(self):
                self.stdin = self

            def write(self, data):  # asyncio transport 的 write 是同步的
                captured["sql"] = data

            async def drain(self):
                pass

            def close(self):
                pass

            async def read(self, n):
                return b""

            async def wait(self):
                return 3

        async def fake_exec(*a, **kw):
            captured["args"] = a
            captured["kwargs"] = kw
            return FakeProc()

        err_text = (b'ERROR:  role "ghost" does not exist\n'
                    b'CONTEXT:  while executing command on stdin\n')

        class ErrReader:
            async def read(self):
                return err_text

        proc = FakeProc()
        proc.stderr = ErrReader()
        proc.stdout = proc

        async def fake_exec2(*a, **kw):
            captured["args"] = a
            captured["kwargs"] = kw
            return proc

        with mock.patch("asyncio.create_subprocess_exec", fake_exec2), self.assertRaises(RuntimeError) as cm:
            self._run(pg_service.psql("DROP ROLE ghost;"))
        self.assertEqual(str(cm.exception), 'ERROR:  role "ghost" does not exist')
        self.assertTrue(captured["kwargs"].get("start_new_session"),
                        "必须独立进程组，超时时才能连 psql 子进程一起回收")

    def test_managed_statements_use_validated_names_only(self):
        """标识符白名单不能被 JSON 改动带偏：非法名必须在拼 SQL 前拒绝。"""
        for bad in ("a;DROP ROLE postgres", "B", 'q"uote', "a|b"):
            with self.assertRaises(RuntimeError):
                self._run(pg_service.drop_role(bad))
            with self.assertRaises(RuntimeError):
                self._run(pg_service.create_db(bad))

    def test_drop_db_blocks_system_databases(self):
        called = []

        async def fake_psql(sql):
            called.append(sql)
            return ""

        with mock.patch.object(pg_service, "psql", fake_psql):
            for name in ("postgres", "template0"):
                with self.assertRaises(RuntimeError) as cm:
                    self._run(pg_service.drop_db(name))
                self.assertIn("禁止删除", str(cm.exception))
        self.assertEqual(called, [], "被拦下的删除不该发出 SQL")


# ------------------------------------------------------------------ Redis
class TestRedisInfo(unittest.TestCase):
    def _fake_run(self, replies):
        calls = []

        async def fake_run(cmd, args, **kw):
            calls.append((cmd, args, kw))
            return replies[args[0]]

        fake_run.calls = calls
        return fake_run

    def test_noauth_message(self):
        rep = {"INFO": {"code": 0, "out": "NOAUTH Authentication required."}}
        f = self._fake_run(rep)
        with (mock.patch.object(pg_service, "run", f),
              mock.patch.object(pg_service, "redis_password", lambda: None), self.assertRaises(RuntimeError) as cm):
            asyncio.run(pg_service.redis_info())
        self.assertIn("需要密码", str(cm.exception))

    def test_connection_failure_is_not_reported_as_success(self):
        """redis-cli 不存在/连不上：code=127 且输出里没有错误正文时也要报错。"""
        rep = {"INFO": {"code": 127, "out": "[Errno 2] No such file or directory: 'redis-cli'"}}
        f = self._fake_run(rep)
        with (mock.patch.object(pg_service, "run", f),
              mock.patch.object(pg_service, "redis_password", lambda: None), self.assertRaises(RuntimeError)):
            asyncio.run(pg_service.redis_info())

    def test_error_message_is_never_empty(self):
        """redis-cli 退出码非 0 且没有任何输出时，错误正文不能是空串——
        前端会渲染出一个完全没有原因的红色错误框。"""
        rep = {"INFO": {"code": 1, "out": ""}}
        f = self._fake_run(rep)
        with (mock.patch.object(pg_service, "run", f),
              mock.patch.object(pg_service, "redis_password", lambda: None), self.assertRaises(RuntimeError) as cm):
            asyncio.run(pg_service.redis_info())
        self.assertTrue(str(cm.exception).strip(), "错误信息为空")

    def test_dbsize_failure_yields_placeholder_not_error_text(self):
        """INFO 成功但 DBSIZE 失败：前端是 {{ redis.dbsize ?? '—' }}，
        任何非空字符串都会被当"Key 总数"渲染出来，等于把错误正文显示成数字。"""
        rep = {
            "INFO": {"code": 0, "out": "# Server\nredis_version:7.2.4\n"},
            "DBSIZE": {"code": 1, "out": "Unrecognized database number"},
        }
        f = self._fake_run(rep)
        with (mock.patch.object(pg_service, "run", f),
              mock.patch.object(pg_service, "redis_password", lambda: None)):
            info = asyncio.run(pg_service.redis_info())
        self.assertIsNone(info["dbsize"], "DBSIZE 失败时不该把错误正文当数字返回")
        self.assertEqual(info["info"]["redis_version"], "7.2.4", "INFO 部分仍要可用")

    def test_password_passed_via_env_only(self):
        """密码必须走 REDISCLI_AUTH，不能进 argv——否则会出现在 ps 输出里。"""
        rep = {
            "INFO": {"code": 0, "out": "# Server\nredis_version:7.2.4\n"},
            "DBSIZE": {"code": 0, "out": "12"},
        }
        f = self._fake_run(rep)
        with (mock.patch.object(pg_service, "run", f),
              mock.patch.object(pg_service, "redis_password", lambda: "s3cret")):
            info = asyncio.run(pg_service.redis_info())
        self.assertEqual(info["info"]["redis_version"], "7.2.4")
        self.assertTrue(info["authed"])
        for _cmd, args, kw in f.calls:
            self.assertNotIn("s3cret", args, f"密码泄漏进命令行参数：{args}")
            self.assertEqual(kw["env"]["REDISCLI_AUTH"], "s3cret")


if __name__ == "__main__":
    unittest.main()
