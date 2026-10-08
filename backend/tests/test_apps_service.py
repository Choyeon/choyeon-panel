"""apps_service / routers.apps 的回归测试。

覆盖的都是实测出来的坏法，不是风格问题：
1. EnvironmentFile 里值以 `\\` 结尾会吃掉右引号，把下一行整个吞进前一个变量。
2. 安装目录只做字符白名单，`..` 原样进库：路径既指到系统目录，又能绕过重复检查。
3. delete_app 的 purge 用 startswith 判定"在应用根目录下"，邻居目录一并删掉。
4. 部署进行中的守卫只加在 delete_app 上，回滚/改配置/改 unit/启停服务都能插进来。
5. app_action 把 is_active 当 ok 返回：停止成功后对外报 ok:false。
6. save_unit 文件写一份、库里写另一份（多一个换行），每存一次库里再长一个换行；
   首次保存失败时坏 unit 残留在 UNIT_DIR，之后任何 daemon-reload 都会把它读进去。
"""

import contextlib
import itertools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_apps_ut_"))

import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from app import database as dbm  # noqa: E402
from app import security  # noqa: E402
from app.services import apps_service  # noqa: E402
from app.util import clean_abs_path  # noqa: E402

_seq = itertools.count()


def _uid(prefix: str) -> str:
    return f"{prefix}{os.getpid()}_{next(_seq)}"


def _aname(prefix: str) -> str:
    """应用名要同时满足 is_name 与 unit 文件名的字符要求，所以只用小写字母、数字和连字符。"""
    return f"{prefix}-{os.getpid()}-{next(_seq)}"


def _mk_app(name: str, path: str, **kw) -> int:
    """插一行应用。默认列在前、覆盖列在后，用 dict 去重。

    不要把重复列名直接写进 INSERT：实测 SQLite 按第一次出现的位置取值
    （`insert into t(a,b,a) values('x','y','OVERRIDE')` 存下的是 'x'），
    传进去的 repo_url 会被前面那个空字符串吃掉（本夹具第一次就踩了这个坑）。
    """
    row = {"name": name, "type": "node", "repo_url": "", "branch": "main", "path": path,
           "start_cmd": "node x.js"}
    row.update(kw)
    cols = list(row)
    return dbm.execute(
        f"INSERT INTO apps({','.join(cols)}) VALUES({','.join('?' * len(cols))})", tuple(row.values())
    )


def _drop_app(app_id: int):
    dbm.execute("DELETE FROM deployments WHERE app_id=?", (app_id,))
    dbm.execute("DELETE FROM apps WHERE id=?", (app_id,))


async def _noop(*_args, **_kw):
    """替换 async 函数时用，别用 lambda：lambda 返回 None，await 它会把协程对象丢进事件循环。"""
    return None


async def _active(_unit):
    return True


async def _inactive(_unit):
    return False


# ---------------------------------------------------------------- 环境变量转义
class TestEnvEscape(unittest.TestCase):
    """systemd EnvironmentFile 的引号语义：双引号内 `\\` 是转义符。

    用 systemd 自己验证过（写 unit → EnvironmentFile=-… → journal 里看真实值）：
    值 `abc\\` 写成 `K="abc\\"` 时结尾的 `\\` 吃掉右引号，字符串不闭合，
    systemd 一路读到下一行的引号才停，结果 K 的值变成 `abc"\nNEXT=a\b"`，
    而下一行的 NEXT 根本没有被定义——前一个变量吃掉后一个变量。
    """

    def test_plain_value_untouched(self):
        self.assertEqual(apps_service._env_escape("hello world"), "hello world")

    def test_backslash_is_doubled(self):
        self.assertEqual(apps_service._env_escape("abc\\"), "abc\\\\")

    def test_quote_is_escaped(self):
        self.assertEqual(apps_service._env_escape('a"b'), 'a\\"b')

    def test_backslash_before_quote_needs_both(self):
        # 先转义 \ 再转义 "，否则会漏掉引号前的那个反斜杠
        self.assertEqual(apps_service._env_escape('a\\"'), 'a\\\\\\"')

    def test_newlines_and_cr_flattened(self):
        self.assertEqual(apps_service._env_escape("a\nb\rc"), "a b c")

    def test_dollar_not_expanded_by_systemd(self):
        """实测 systemd 不做变量展开（`cost $HOME` 原样进进程环境），
        所以不能把 $ 转成 $$ ——那只会让用户真的需要 `$$` 时拿到四个符号。"""
        self.assertEqual(apps_service._env_escape("cost $HOME"), "cost $HOME")

    def test_none_and_number(self):
        self.assertEqual(apps_service._env_escape(None), "")
        self.assertEqual(apps_service._env_escape(8000), "8000")


class TestEnvFileBytes(unittest.TestCase):
    """整份文件层面的黄金输出：变量必须一行一个、各自闭合。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_env_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, env):
        apps_service.write_env_file({"name": "e", "path": self.dir, "env": json.dumps(env)})
        return Path(self.dir, ".panel.env").read_text()

    def test_trailing_backslash_does_not_swallow_next_line(self):
        body = self._write([{"k": "TRAILBS", "v": "abc\\"}, {"k": "DOUBLEBS", "v": "a\\b"}])
        lines = body.splitlines()
        self.assertEqual(lines[0], 'TRAILBS="abc\\\\"', f"值必须成对转义，实得 {lines[0]!r}")
        self.assertEqual(lines[1], 'DOUBLEBS="a\\\\b"')
        self.assertEqual(len(lines), 2, "两个变量各自成行")

    def test_embedded_quote_keeps_line_count(self):
        body = self._write([{"k": "Q", "v": 'p"q'}, {"k": "NEXT", "v": "1"}])
        self.assertEqual(body.splitlines(), ['Q="p\\"q"', 'NEXT="1"'])

    def test_newline_in_value_cannot_split_line(self):
        body = self._write([{"k": "MULTI", "v": "first\nsecond"}, {"k": "NEXT", "v": "1"}])
        self.assertEqual(body.splitlines(), ['MULTI="first second"', 'NEXT="1"'])

    def test_mode_is_0600(self):
        self._write([{"k": "SECRET", "v": "s3cr3t"}])
        mode = stat.S_IMODE(os.stat(Path(self.dir, ".panel.env")).st_mode)
        self.assertEqual(mode, 0o600, "环境变量常含密钥，不能让同机其他用户读")

    def test_bad_shape_still_skipped(self):
        """{"key","value"} 这种旧结构会被静默丢弃（既有行为，钉住以免改动时无声扩大影响面）。"""
        self.assertEqual(self._write([{"key": "PORT", "value": "3000"}]), "\n")


# ---------------------------------------------------------------- 路径校验
class TestCleanAbsPath(unittest.TestCase):
    def test_normalizes_harmless_forms(self):
        self.assertEqual(clean_abs_path("/root/www/app/"), "/root/www/app")
        self.assertEqual(clean_abs_path("/root/www//app"), "/root/www/app")
        self.assertEqual(clean_abs_path("/root/www/./app"), "/root/www/app")

    def test_rejects_dotdot_even_if_charset_allows_it(self):
        """PATH_RE 里有 `.` 和 `/`，所以旧校验放行 /root/www/app/../../etc。"""
        for bad in ("/root/www/app/../../etc", "/etc/../../root", "/../etc", "/root/www/.."):
            with self.assertRaises(RuntimeError, msg=bad):
                clean_abs_path(bad)

    def test_rejects_non_absolute_and_root(self):
        for bad in ("relative/path", "/", "", None, 123):
            with self.assertRaises(RuntimeError, msg=str(bad)):
                clean_abs_path(bad)

    def test_rejects_bad_charset(self):
        for bad in ("/root/www;a", "/root/www a", "/root/$(id)", "/root/www|x"):
            with self.assertRaises(RuntimeError, msg=bad):
                clean_abs_path(bad)

    def test_message_names_the_field(self):
        with self.assertRaises(RuntimeError) as ctx:
            clean_abs_path("/root/www/../etc", "安装目录")
        self.assertIn("安装目录", str(ctx.exception))


class TestAppPathValidation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_path_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._old_root = apps_service.APP_ROOT
        apps_service.APP_ROOT = self.dir  # 让默认安装目录落在临时区

    def tearDown(self):
        apps_service.APP_ROOT = self._old_root

    def _payload(self, name, path=None):
        p = {"name": name, "type": "node", "start_cmd": "node x.js", "repo_url": "", "branch": "main"}
        if path is not None:
            p["path"] = path
        return p

    def test_dotdot_path_rejected_on_create(self):
        name = _aname("esc")
        escape = f"{self.dir}/app/../../etc"
        # 旧校验（纯字符白名单）确实放行这条路径 —— 断言新守卫不是空转
        self.assertTrue(bool(re.match(r"^/[A-Za-z0-9._/-]{2,200}$", escape)))
        self.assertEqual(os.path.normpath(escape), f"{os.path.dirname(self.dir)}/etc", "目标是根外的另一个目录")
        with self.assertRaises(RuntimeError) as ctx:
            apps_service.create_app(self._payload(name, escape))
        self.assertIn("不允许 ..", str(ctx.exception))
        self.assertIsNone(dbm.query_one("SELECT id FROM apps WHERE name=?", (name,)))

    def test_stored_path_is_normalized(self):
        app = apps_service.create_app(self._payload(_aname("norm1"), f"{self.dir}/norm1//"))
        self.addCleanup(_drop_app, app["id"])
        self.assertEqual(app["path"], clean_abs_path(f"{self.dir}/norm1"))

    def test_same_directory_written_differently_is_duplicate(self):
        """旧实现按字符串比较 path，`/x/a//` 与 `/x/a` 是两个字符串却同一个目录，
        两个应用共用工作目录时后者覆盖前者的 unit 与 .panel.env。
        （带 .. 的新输入会被"不允许 .."先拦下；库里残留的老写法见下一个用例。）"""
        first = apps_service.create_app(self._payload(_aname("dup"), f"{self.dir}/dup1"))
        self.addCleanup(_drop_app, first["id"])
        for alt in (f"{self.dir}/dup1//", f"{self.dir}/dup1/.", f"{self.dir}//dup1"):
            with self.assertRaises(RuntimeError, msg=alt):
                apps_service.create_app(self._payload(_aname("dup"), alt))
            rows = dbm.query("SELECT id FROM apps WHERE path=?", (clean_abs_path(alt),))
            self.assertEqual([r["id"] for r in rows], [first["id"]], f"{alt} 不该多出第二条记录")

    def test_legacy_untouched_row_cannot_be_evaded_by_new_app(self):
        """库里残留的老写法（带 ..，旧校验放行过）也必须参与重复比对。
        旧实现遇 clean_abs_path 失败就跳过该行，于是新应用照样能和它共用目录。"""
        target = f"{self.dir}/dup3"
        legacy = _mk_app(_aname("legacy"), f"{self.dir}/leg/../dup3")
        self.addCleanup(_drop_app, legacy)
        self.assertEqual(os.path.normpath(f"{self.dir}/leg/../dup3"), target)
        with self.assertRaises(RuntimeError) as ctx:
            apps_service.create_app(self._payload(_aname("dup3"), target))
        self.assertIn(f"应用 #{legacy}", str(ctx.exception))

    def test_update_writes_normalized_path(self):
        app = apps_service.create_app(self._payload(_aname("upd1"), f"{self.dir}/upd1"))
        self.addCleanup(_drop_app, app["id"])
        out = apps_service.update_app(app["id"], {**app, "path": f"{self.dir}/upd1/./"})
        self.assertEqual(out["path"], f"{self.dir}/upd1")

    def test_default_path_from_name(self):
        name = _aname("def")
        app = apps_service.create_app(self._payload(name))
        self.addCleanup(_drop_app, app["id"])
        self.assertEqual(app["path"], f"{self.dir}/{name}")


# ---------------------------------------------------------------- purge 目标
class TestPurgeTarget(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_purge_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._old_root = apps_service.APP_ROOT
        apps_service.APP_ROOT = self.dir

    def tearDown(self):
        apps_service.APP_ROOT = self._old_root

    def test_sibling_prefix_is_refused(self):
        """旧判定是 `path.startswith(APP_ROOT)`，纯字符串前缀：
        APP_ROOT=/root/www 时 /root/www-other、/root/www2 全算"在里面"，
        purge 会把邻居整个删掉。这里构造同样两个目录，验证新守卫不动它们。"""
        sibling = f"{self.dir}-other"
        sibling2 = f"{self.dir}2"
        for p in (sibling, sibling2):
            os.makedirs(p, exist_ok=True)
            Path(p, "precious.txt").write_text("do not delete")
        self.addCleanup(shutil.rmtree, sibling, True)
        self.addCleanup(shutil.rmtree, sibling2, True)
        self.assertTrue(sibling.startswith(self.dir), "前缀关系成立，旧守卫必然放行")
        for p in (sibling, sibling2):
            with self.assertRaises(RuntimeError) as ctx:
                apps_service._purge_app_dir(p)
            self.assertIn("不在应用根目录", str(ctx.exception))
            self.assertTrue(Path(p, "precious.txt").exists(), f"{p} 不该被动过")

    def test_app_root_itself_is_refused(self):
        with self.assertRaises(RuntimeError):
            apps_service._purge_app_dir(self.dir)

    def test_path_outside_root_is_refused(self):
        with self.assertRaises(RuntimeError):
            apps_service._purge_app_dir("/etc/ssh")

    def test_dotdot_target_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            apps_service._purge_app_dir(f"{self.dir}/x/../../etc")
        self.assertIn("拒绝删除应用目录", str(ctx.exception))

    def test_legit_dir_is_deleted(self):
        target = f"{self.dir}/victim"
        os.makedirs(f"{target}/sub")
        Path(target, "sub", "f.txt").write_text("x")
        apps_service._purge_app_dir(target)
        self.assertFalse(os.path.exists(target))

    def test_missing_dir_is_not_an_error(self):
        apps_service._purge_app_dir(f"{self.dir}/never-existed")

    def test_rmtree_failure_is_reported_not_swallowed(self):
        """旧实现 ignore_errors=True：目录还在但记录已删，成了没人管的孤儿目录。"""
        target = f"{self.dir}/busy"
        os.makedirs(target)
        with (
            mock.patch.object(apps_service.shutil, "rmtree", side_effect=OSError("Device or resource busy")),
            self.assertRaises(RuntimeError) as ctx,
        ):
            apps_service._purge_app_dir(target)
        self.assertIn("删除应用目录失败", str(ctx.exception))


class TestDeleteAppOrdering(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_del_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._old_root = apps_service.APP_ROOT
        apps_service.APP_ROOT = self.dir

    def tearDown(self):
        apps_service.APP_ROOT = self._old_root

    async def test_bad_purge_target_aborts_before_any_removal(self):
        sibling = f"{self.dir}-other"
        os.makedirs(sibling)
        self.addCleanup(shutil.rmtree, sibling, True)
        app_id = _mk_app(_aname("ord1"), sibling)
        removed = []

        async def fake_remove(app):
            removed.append(app["name"])

        with (
            mock.patch.object(apps_service, "remove_unit", fake_remove),
            self.assertRaises(RuntimeError),
        ):
            await apps_service.delete_app(app_id, True)
        self.assertEqual(removed, [], "目录不合法时不能先拆 unit")
        self.assertIsNotNone(dbm.query_one("SELECT id FROM apps WHERE id=?", (app_id,)), "记录不能被删")
        self.assertTrue(os.path.isdir(sibling))

    async def test_purge_deletes_after_record_removal(self):
        target = f"{self.dir}/gone"
        os.makedirs(target)
        app_id = _mk_app(_aname("ord2"), target)
        with mock.patch.object(apps_service, "remove_unit", _noop):
            await apps_service.delete_app(app_id, True)
        self.assertFalse(os.path.exists(target))
        self.assertIsNone(dbm.query_one("SELECT id FROM apps WHERE id=?", (app_id,)))

    async def test_without_purge_keeps_directory(self):
        target = f"{self.dir}/keep"
        os.makedirs(target)
        app_id = _mk_app(_aname("ord3"), target)
        with mock.patch.object(apps_service, "remove_unit", _noop):
            await apps_service.delete_app(app_id, False)
        self.assertTrue(os.path.isdir(target))


# ---------------------------------------------------------------- 部署并发守卫
class TestDeploymentGuards(unittest.IsolatedAsyncioTestCase):
    """部署任务跑的时候，同一应用上的其它写操作必须挡住。

    deploy_app 自己那处 check-then-insert 中间没有 await，单进程事件循环下并不真的存在竞态；
    真正的问题是兄弟操作根本没查过 deployments。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_guard_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app_id = _mk_app(_aname("guard1"), self.dir)

    def tearDown(self):
        _drop_app(self.app_id)

    def _running(self):
        dep = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (self.app_id,))
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE id=?", (dep,))
        return dep

    async def test_source_of_each_write_operation_checks_deployments(self):
        """把守卫钉在源码上：新增写操作若忘了查，这条测试要失败。"""
        import inspect

        for fn in (
            apps_service.delete_app,
            apps_service.update_app,
            apps_service.rollback_app,
            apps_service.save_unit,
            apps_service.app_action,
        ):
            src = inspect.getsource(fn)
            self.assertTrue(
                "_ensure_no_deployment" in src or "deploy_app" in src,
                f"{fn.__name__} 必须拒绝与进行中的部署并发",
            )

    async def test_update_app_refused(self):
        self._running()
        cur = apps_service.get_app(self.app_id)
        with self.assertRaises(RuntimeError) as ctx:
            apps_service.update_app(self.app_id, {**cur, "start_cmd": "node y.js"})
        self.assertIn("部署进行中", str(ctx.exception))

    async def test_rollback_refused(self):
        self._running()
        with self.assertRaises(RuntimeError) as ctx:
            await apps_service.rollback_app(self.app_id)
        self.assertIn("回滚", str(ctx.exception))

    async def test_service_action_refused(self):
        self._running()
        called = []

        async def spy(unit, verb):
            called.append((unit, verb))

        with (
            mock.patch.object(apps_service, "service_action", spy),
            self.assertRaises(RuntimeError),
        ):
            await apps_service.app_action(self.app_id, "stop")
        self.assertEqual(called, [], "被拦下时不能真的动 systemctl")

    async def test_save_unit_refused(self):
        self._running()
        with self.assertRaises(RuntimeError):
            await apps_service.save_unit(self.app_id, "[Service]\nExecStart=/bin/true\n")

    async def test_operations_allowed_without_running_deployment(self):
        """非空验证：不能写成"永远拒绝"，否则上面几条也一样"通过"。"""
        with (
            mock.patch.object(apps_service, "service_action", _noop),
            mock.patch.object(apps_service.systemd_ops, "is_active", _active),
        ):
            res = await apps_service.app_action(self.app_id, "restart")
        self.assertTrue(res["ok"])


# ---------------------------------------------------------------- app_action 返回契约
class TestAppActionResult(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_act_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app_id = _mk_app(_aname("act1"), self.dir)

    def tearDown(self):
        _drop_app(self.app_id)

    async def test_stop_reports_ok_true_active_false(self):
        """回归：旧实现 return is_active(...)，router 再包成 {"ok": result, "active": result}。
        停止成功后服务不是 active，于是 HTTP 200 里 ok:false —— 与 AGENTS.md
        "失败走 4xx/5xx，成功响应 ok 为 true" 的约定相反，CLI/前端按字段判断就会误读。"""
        with (
            mock.patch.object(apps_service, "service_action", _noop),
            mock.patch.object(apps_service.systemd_ops, "is_active", _inactive),
        ):
            res = await apps_service.app_action(self.app_id, "stop")
        self.assertEqual(res, {"ok": True, "active": False})

    async def test_start_reports_active_true(self):
        with (
            mock.patch.object(apps_service, "service_action", _noop),
            mock.patch.object(apps_service.systemd_ops, "is_active", _active),
        ):
            res = await apps_service.app_action(self.app_id, "start")
        self.assertEqual(res, {"ok": True, "active": True})

    async def test_failure_is_raised_not_reported_in_ok(self):
        async def boom(unit, verb):
            raise RuntimeError("systemctl stop failed")

        with mock.patch.object(apps_service, "service_action", boom), self.assertRaises(RuntimeError):
            await apps_service.app_action(self.app_id, "stop")

    async def test_unknown_verb_message_uses_chinese_label(self):
        dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (self.app_id,))
        self.addCleanup(dbm.execute, "DELETE FROM deployments WHERE app_id=?", (self.app_id,))
        with self.assertRaises(RuntimeError) as ctx:
            await apps_service.app_action(self.app_id, "restart")
        self.assertIn("重启服务", str(ctx.exception))


class TestActionEndpointShape(unittest.TestCase):
    """HTTP 层：停止成功要返回 ok=true，失败仍是 4xx + error。"""

    def setUp(self):
        from starlette.testclient import TestClient

        from app import main

        self.client = TestClient(main.app)
        self.user = _uid("act_ep_")
        dbm.execute(
            "INSERT OR REPLACE INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (self.user, security.hash_pass("Password123"), "admin"),
        )
        self.addCleanup(dbm.execute, "DELETE FROM users WHERE username=?", (self.user,))
        self.headers = {"Authorization": f"Bearer {security.sign_token(self.user, 'admin')}"}
        self.dir = tempfile.mkdtemp(prefix="cp_ep_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.app_id = _mk_app(_aname("ep1"), self.dir)
        self.addCleanup(_drop_app, self.app_id)

    def test_stop_success_body(self):
        async def noop(unit, verb):
            return ""

        async def inactive(unit):
            return False

        with (
            mock.patch.object(apps_service, "service_action", noop),
            mock.patch.object(apps_service.systemd_ops, "is_active", inactive),
        ):
            r = self.client.post(f"/api/apps/{self.app_id}/action", json={"verb": "stop"}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "active": False})

    def test_failure_still_400_with_error(self):
        async def boom(unit, verb):
            raise RuntimeError("systemctl stop failed")

        with mock.patch.object(apps_service, "service_action", boom):
            r = self.client.post(f"/api/apps/{self.app_id}/action", json={"verb": "stop"}, headers=self.headers)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("error", r.json())

    def test_rollback_shape_preserved(self):
        """前端 rollback() 读 res.commit，响应结构不能因为 ok 收敛而变。"""
        async def rb(app_id):
            return {"ok": True, "commit": "abc123", "deployment": 9, "from": "def456"}

        with mock.patch.object(apps_service, "rollback_app", rb):
            r = self.client.post(f"/api/apps/{self.app_id}/action", json={"verb": "rollback"}, headers=self.headers)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["commit"], "abc123")
        self.assertTrue(r.json()["ok"])


# ---------------------------------------------------------------- save_unit
GOOD_UNIT = "[Service]\nExecStart=/bin/true\n"


class TestSaveUnit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_unit_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._old_dir = apps_service.UNIT_DIR
        apps_service.UNIT_DIR = self.dir
        self.app_dir = tempfile.mkdtemp(prefix="cp_app_")
        self.addCleanup(shutil.rmtree, self.app_dir, True)
        self.app_name = _aname("unit")
        self.app_id = _mk_app(self.app_name, self.app_dir)
        self.path = f"{self.dir}/panel-{self.app_name}.service"

    def tearDown(self):
        apps_service.UNIT_DIR = self._old_dir
        _drop_app(self.app_id)

    async def _save(self, content, verify_code=0):
        async def fake_run(cmd, args, **kw):
            if cmd == "systemd-analyze":
                return {"code": verify_code, "out": "verify error" if verify_code else ""}
            return {"code": 0, "out": ""}

        with mock.patch.object(apps_service, "run", fake_run):
            return await apps_service.save_unit(self.app_id, content)

    def _unit_files(self):
        return sorted(Path(self.dir).glob("*.service"))

    async def test_file_and_db_hold_identical_text(self):
        """回归：旧实现文件写 normalized、库里写 content + "\n"，
        末尾本来就有换行时库里多一个空行，用户原样再存一次就继续累积。
        实测（对 HEAD 版本跑）：库里比文件多 1 个字节，第二次保存再多 1 个。"""
        await self._save(GOOD_UNIT)
        stored = dbm.query_one("SELECT unit_template t FROM apps WHERE id=?", (self.app_id,))["t"]
        file_body = Path(self.path).read_text()
        self.assertEqual(stored, file_body)
        self.assertEqual(stored.count("\n"), 2, "不该有累积出来的空行")

        await self._save(stored)  # 前端把库里的内容原样送回
        again = dbm.query_one("SELECT unit_template t FROM apps WHERE id=?", (self.app_id,))["t"]
        self.assertEqual(again, stored, "重复保存必须幂等")
        self.assertEqual(Path(self.path).read_text(), again)

    async def test_missing_trailing_newline_added_once(self):
        await self._save("[Service]\nExecStart=/bin/true")
        stored = dbm.query_one("SELECT unit_template t FROM apps WHERE id=?", (self.app_id,))["t"]
        self.assertEqual(stored, GOOD_UNIT)

    async def test_failed_first_save_leaves_no_unit_file(self):
        """回归：旧实现只在 old is not None 时回滚，首次保存失败后
        这份没通过 verify 的 unit 留在 UNIT_DIR，之后任何一次 daemon-reload
        （别的应用部署、面板重启）都会把它读进去。"""
        with self.assertRaises(RuntimeError):
            await self._save(GOOD_UNIT, verify_code=1)
        self.assertFalse(os.path.exists(self.path), f"坏 unit 残留：{os.listdir(self.dir)}")

    async def test_failed_save_restores_previous_content(self):
        await self._save(GOOD_UNIT)
        with self.assertRaises(RuntimeError):
            await self._save("[Service]\nExecStart=/bin/false\n", verify_code=1)
        self.assertEqual(Path(self.path).read_text(), GOOD_UNIT)
        self.assertEqual(
            dbm.query_one("SELECT unit_template t FROM apps WHERE id=?", (self.app_id,))["t"], GOOD_UNIT
        )

    async def test_daemon_reload_failure_removes_new_file(self):
        async def fake_run(cmd, args, **kw):
            if cmd == "systemctl":
                return {"code": 1, "out": "reload failed"}
            return {"code": 0, "out": ""}

        with mock.patch.object(apps_service, "run", fake_run), self.assertRaises(RuntimeError):
            await apps_service.save_unit(self.app_id, GOOD_UNIT)
        self.assertFalse(os.path.exists(self.path))

    async def test_validation_still_applied(self):
        for bad in ("", "   ", "[Service]\n", "x" * 9000, "[Service]\nExecStart=$(id)\n"):
            with self.assertRaises(RuntimeError, msg=repr(bad[:20])):
                await self._save(bad)


# ---------------------------------------------------------------- 真实 systemd 语义
class TestSystemdEnvSemantics(unittest.TestCase):
    """把"转义为什么必须这样做"钉在真实 systemd 上，而不只是钉在字符串断言上。

    沙箱里 systemd 作为 PID 1 可用（systemctl is-system-running 非 offline），
    没有就整类跳过——断言本身仍在其它用例里覆盖。
    还要求 root：unit 必须真的写进 /etc/systemd/system 才会被 systemd 读到，
    非 root（GitHub Actions 的 runner 就是普通用户）连文件都创建不了，
    那不是"环境没有 systemd"，而是"没权限用它"，同样只能跳过。
    """

    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest(f"需要 root 才能写 /etc/systemd/system（当前 euid={os.geteuid()}）")
        r = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True)
        # running/degraded/debian 系还可能有 maintenance——只要不是 offline 就能起服务
        if r.returncode not in (0, 1, 2) or r.stdout.strip() == "offline":
            raise unittest.SkipTest(f"本环境没有可用的 systemd：{r.stdout.strip() or r.stderr.strip()}")
        cls.dir = tempfile.mkdtemp(prefix="cp_real_sys_")
        cls.unit = f"cpenvtest{os.getpid()}.service"
        cls.out = f"{cls.dir}/dump.txt"
        cls.env_file = f"{cls.dir}/.panel.env"

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["systemctl", "reset-failed", cls.unit], capture_output=True)
        p = f"/etc/systemd/system/{cls.unit}"
        if os.path.exists(p):
            os.unlink(p)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _run(self):
        Path(f"/etc/systemd/system/{self.unit}").write_text(
            "[Service]\nType=oneshot\n"
            f"EnvironmentFile=-{self.env_file}\n"
            # env -0：用 NUL 分隔。按行解析会被"值里带换行"骗过——
            # 被吞掉的变量正好会表现为一个正常变量，测试就成了假通过（第一次跑就踩到了）。
            f'ExecStart=/bin/sh -c "env -0 > {self.out}"\n'
        )
        for args in (["daemon-reload"], ["restart", self.unit]):
            r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, f"systemctl {args} 失败：{r.stderr}")
        # oneshot 落盘有延迟，等到文件出现为止（最多 5 秒）
        for _ in range(100):
            if os.path.exists(self.out):
                break
            time.sleep(0.05)
        else:
            self.fail("systemd 没有执行 oneshot 服务，环境不可信")
        env = {}
        for item in Path(self.out).read_bytes().split(b"\0"):
            if not item:
                continue
            k, _, v = item.partition(b"=")
            env[k.decode(errors="replace")] = v.decode(errors="replace")
        os.unlink(self.out)
        return env

    def test_escaped_values_arrive_verbatim(self):
        apps_service.write_env_file(
            {
                "name": "x",
                "path": self.dir,
                "env": json.dumps(
                    [
                        {"k": "TRAILBS", "v": "abc\\"},
                        {"k": "DOUBLEBS", "v": "a\\b"},
                        {"k": "QUOTED", "v": 'p"q'},
                        {"k": "DOLLAR", "v": "cost $HOME"},
                        {"k": "NEXT", "v": "1"},
                    ]
                ),
            }
        )
        env = self._run()
        self.assertEqual(env.get("TRAILBS"), "abc\\", "结尾反斜杠必须原样到达，且不吃掉下一行")
        self.assertEqual(env.get("DOUBLEBS"), "a\\b")
        self.assertEqual(env.get("QUOTED"), 'p"q')
        self.assertEqual(env.get("DOLLAR"), "cost $HOME", "systemd 不做变量展开，所以不能转义 $")
        self.assertEqual(env.get("NEXT"), "1", "后面的变量不能被前一个值吞掉")

    def test_unescaped_form_would_have_broken_the_next_variable(self):
        """非空验证：按旧写法（只转义引号、不转义反斜杠）手工写文件，
        证明 TRAILBS 真的会吞掉 DOUBLEBS——否则上面的断言可能只是恰好成立。"""
        Path(self.env_file).write_text('TRAILBS="abc\\"\nDOUBLEBS="a\\\\b"\nNEXT="1"\n', encoding="utf-8")
        env = self._run()
        self.assertNotIn("DOUBLEBS", env, "旧写法下 DOUBLEBS 应当整行消失（本次要复现的就是它）")
        self.assertIn('abc"', env.get("TRAILBS", ""), f"旧值被拼接污染：{env.get('TRAILBS')!r}")


# ---------------------------------------------------------------- 部署失败后的现场说明
class TestDeployFailureNotice(unittest.IsolatedAsyncioTestCase):
    """git 已经切到新版本、但后面的步骤失败时，日志必须说清盘上和服务跑的不是同一份。"""

    def setUp(self):
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.work = tempfile.mkdtemp(prefix="cp_dep7_")
        self.addCleanup(shutil.rmtree, self.work, True)
        # ensure_unit 会往 UNIT_DIR 写文件并 daemon-reload，不重定向就会污染真实 systemd 目录
        self.unit_dir = tempfile.mkdtemp(prefix="cp_dep7_units_")
        self.addCleanup(shutil.rmtree, self.unit_dir, True)
        self._old_unit_dir = apps_service.UNIT_DIR
        apps_service.UNIT_DIR = self.unit_dir
        self.addCleanup(setattr, apps_service, "UNIT_DIR", self._old_unit_dir)
        self.origin = f"{self.work}/origin"
        self.target = f"{self.work}/www/app"
        os.makedirs(self.origin)
        self._git("init", "-q", "--bare", "-b", "main", cwd=self.origin)
        seed = f"{self.work}/seed"
        os.makedirs(seed)
        self._git("init", "-q", "-b", "main", cwd=seed)
        Path(seed, "app.js").write_text("CODE=v1\n")
        self._git("add", "-A", cwd=seed)
        self._git("commit", "-qm", "v1", cwd=seed)
        self._git("remote", "add", "origin", self.origin, cwd=seed)
        self._git("push", "-q", "origin", "main", cwd=seed)
        self.app_name = _aname("dep7")
        self.app_id = _mk_app(
            self.app_name, self.target, repo_url=self.origin, install_cmd="test -f app.js"
        )

    def tearDown(self):
        _drop_app(self.app_id)

    def _git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, env=self.env,
                              capture_output=True, text=True).stdout.strip()

    @contextlib.contextmanager
    def _no_systemctl(self, **extra_mocks):
        """只截住 systemctl，git / bash 照旧真跑。

        这个类测的是"部署失败后日志有没有说清盘上与服务跑的不是同一份代码"，
        靠真实 git clone/checkout；而 ensure_unit 结尾那次 daemon-reload 需要特权，
        普通用户（GitHub Actions 的 runner）拿到的是
        "Failed to reload daemon: Interactive authentication required."，
        整条部署被判 failed，三条用例于是全在测"这台机器不让我 reload"。
        被替掉的只有 systemctl 这一层，unit 文件仍然真写、git 仍然真跑、
        失败分支仍然真失败——漂移日志的断言没有因此变松。
        """
        real_run = apps_service.run

        async def run_no_systemctl(cmd, args, cwd=None, timeout=120, env=None):
            if cmd == "systemctl":
                return {"code": 0, "out": ""}
            return await real_run(cmd, args, cwd=cwd, timeout=timeout, env=env)

        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(apps_service, "run", run_no_systemctl))
            for name, impl in extra_mocks.items():
                st.enter_context(mock.patch.object(apps_service, name, impl))
            yield

    def _push(self, text: str):
        clone = f"{self.work}/push"
        shutil.rmtree(clone, ignore_errors=True)
        self._git("clone", "-q", self.origin, clone, cwd=self.work)
        Path(clone, "app.js").write_text(text)
        self._git("add", "-A", cwd=clone)
        self._git("commit", "-qm", text.strip(), cwd=clone)
        self._git("push", "-q", "origin", "main", cwd=clone)

    async def _deploy(self) -> dict:
        dep = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (self.app_id,))
        app = apps_service.get_app(self.app_id)
        with self._no_systemctl(service_action=_noop):
            await apps_service._run_deployment(app, dep)
        return dbm.query_one("SELECT status, log FROM deployments WHERE id=?", (dep,))

    async def test_first_deploy_then_failing_deploy_reports_the_drift(self):
        row = await self._deploy()
        self.assertEqual(row["status"], "success")
        self.assertEqual(Path(self.target, "app.js").read_text().strip(), "CODE=v1")

        self._push("CODE=v2\n")
        # 安装步骤注定失败：git 已经把工作区切到 v2，服务没有被重启
        dbm.execute("UPDATE apps SET install_cmd=? WHERE id=?", ("exit 7", self.app_id))
        row = await self._deploy()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(Path(self.target, "app.js").read_text().strip(), "CODE=v2", "工作区确实被切走了")
        self.assertIn("工作区已从", row["log"], "日志必须说明盘上代码已变而服务仍跑旧代码")
        self.assertIn("仍在运行旧代码", row["log"])
        self.assertIn("回滚", row["log"], "要给出可执行的下一步")

    async def test_failure_before_any_checkout_says_nothing_about_drift(self):
        """非空验证：拉取阶段就失败（工作区没动过）时不能乱报"代码已切换"。"""
        dbm.execute("UPDATE apps SET repo_url=?, install_cmd=? WHERE id=?",
                    ("/nonexistent-repo.git", "exit 7", self.app_id))
        row = await self._deploy()
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("工作区已从", row["log"])

    async def test_restart_failure_also_reports_drift(self):
        """最后一步 restart 失败时同样要说清：盘上已经是新代码，服务还是旧进程。"""
        row = await self._deploy()
        self.assertEqual(row["status"], "success")
        self._push("CODE=v3\n")

        async def bad_restart(unit, verb):
            raise RuntimeError("systemctl restart failed")

        dep = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (self.app_id,))
        app = apps_service.get_app(self.app_id)
        with self._no_systemctl(service_action=bad_restart):
            await apps_service._run_deployment(app, dep)
        row = dbm.query_one("SELECT status, log FROM deployments WHERE id=?", (dep,))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(Path(self.target, "app.js").read_text().strip(), "CODE=v3")
        self.assertIn("工作区已从", row["log"])
        self.assertIn("systemctl restart failed", row["log"])


# ---------------------------------------------------------------- 回滚仍然正常
class TestRollbackStillWorks(unittest.IsolatedAsyncioTestCase):
    """新增守卫不能把正常回滚路径挡死。"""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="cp_rb2_")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True, env=env)
        self.shas = []
        for msg in ("v1", "v2"):
            Path(self.repo, "VERSION").write_text(msg)
            subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, env=env)
            subprocess.run(["git", "commit", "-m", msg], cwd=self.repo, check=True, env=env,
                           stdout=subprocess.DEVNULL)
            self.shas.append(subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo,
                                            check=True, env=env, capture_output=True, text=True).stdout.strip())
        self.app_id = _mk_app(_aname("rb2"), self.repo)

    def tearDown(self):
        _drop_app(self.app_id)
        shutil.rmtree(self.repo, ignore_errors=True)

    async def test_rollback_succeeds_when_no_deployment_running(self):
        for sha in self.shas:
            dbm.execute("INSERT INTO deployments(app_id,status,commit_sha) VALUES(?,'success',?)",
                        (self.app_id, sha))
        with mock.patch.object(apps_service, "service_action", _noop):
            res = await apps_service.rollback_app(self.app_id)
        self.assertEqual(res["commit"], self.shas[0])
        self.assertEqual(res["ok"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
