"""批次77：nginx 配置快照的三态读者——读不到不许当成「盘上没有」，再让回滚把它删掉。

被修掉的形状在两条写入路径上一模一样（apply_vhost / save_app_config）：

    old = Path(p).read_text(errors="replace") if os.path.exists(p) else None
    _write_conf(p, content)                    # os.replace，覆盖盘上那份真实文件
    if nginx -t 不过:
        _write_conf(p, old) if old is not None else os.unlink(p)   # ← 要害在这一步

`os.path.exists()` 把 PermissionError 折成 False，于是 old=None，那份「读不动但确实存在」的
vhost 被当成本次新建：校验一不过就走删除分支，用户手工维护的站点配置被面板 unlink 掉，
而抛出的错误信息还写着「已回滚」。

remove_vhost 是反方向的同一个折叠：`if not os.path.exists(path): return` —— 读不到时一个字节
都不动、安静返回成功；而 routers/sites.py 又用第二个读者 `Path(vhost_path(name)).exists()`
决定响应里的 removed，界面于是显示「该应用没有由面板生成的 vhost」，明明只是读不到。
现在 removed 只来自 remove_vhost 自己的返回值（"removed" / "absent"），读不到那一档在动手前拒绝。

quick_edit_config 的裸 `Path(real).read_text()`，与 _safe_conf_path 的 `os.path.exists`
（悬空符号链接会被它读成「文件不存在」从而跳过 realpath）在同一批收掉。
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_77_ut_"))

from app import config  # noqa: E402
from app.services import nginx_ops  # noqa: E402

APP_DIR = Path(__file__).resolve().parent.parent / "app"
NEW_TEXT = "server {\n    listen 8080;\n}\n"


def _confdir(case):
    """把 CONF_DIR 与 config.NGINX_CONF_DIRS 一起挪进临时目录，一个都不许漏。

    少挪一个就会真的往 /etc/nginx 里写半截 server 块，测试机上那台真 nginx 会读到它。
    """
    d = tempfile.mkdtemp(prefix="cp_77_conf_")
    case.addCleanup(shutil.rmtree, d, True)
    old_dirs = config.NGINX_CONF_DIRS
    config.NGINX_CONF_DIRS = [d]
    case.addCleanup(setattr, config, "NGINX_CONF_DIRS", old_dirs)
    p = mock.patch.object(nginx_ops, "CONF_DIR", d)
    p.start()
    case.addCleanup(p.stop)
    for name in ("_loaded_cache", "_cert_cache", "_ver_cache"):
        setattr(nginx_ops, name, None)
        case.addCleanup(setattr, nginx_ops, name, None)
    return d


def _writers(case):
    """记录 _write_conf 的调用，别让被测路径真的落盘。"""
    calls = []

    def rec(path, content):
        calls.append((path, content))

    p = mock.patch.object(nginx_ops, "_write_conf", rec)
    p.start()
    case.addCleanup(p.stop)
    return calls


def _runs(case, code=0):
    calls = []

    async def rec(cmd, args, **_kw):
        calls.append(cmd)
        return {"code": code, "out": "syntax is ok" if code == 0 else "nginx: [emerg] bad"}

    p = mock.patch.object(nginx_ops, "run", rec)
    p.start()
    case.addCleanup(p.stop)
    return calls


async def _stub_none(*_a, **_k):
    return None


async def _stub_paths(*_a, **_k):
    return []


def _apply_stubs(case):
    """apply_vhost 里证书 / nginx 版本 / 域名归属那几个探测：本批只关心覆盖前的那一次读。"""
    for fn in ("cert_lineage", "http2_uses_new_syntax", "domain_owner"):
        p = mock.patch.object(nginx_ops, fn, _stub_none)
        p.start()
        case.addCleanup(p.stop)
    p = mock.patch.object(nginx_ops, "loaded_conf_paths", _stub_paths)
    p.start()
    case.addCleanup(p.stop)


class ConfTextTests(unittest.TestCase):
    def setUp(self):
        self.dir = _confdir(self)

    def test_ok_reads_the_file(self):
        path = f"{self.dir}/panel-a.conf"
        Path(path).write_text("server {\n    listen 80;\n}\n")
        state, text, why = nginx_ops._conf_text(path)
        self.assertEqual(state, "ok")
        self.assertIn("listen 80", text)
        self.assertEqual(why, "")

    def test_missing_is_its_own_state(self):
        state, text, why = nginx_ops._conf_text(f"{self.dir}/panel-none.conf")
        self.assertEqual(state, "missing")
        self.assertIsNone(text, 'missing 的正文必须是 None：「空文件」与「没有文件」不能都读成 ""')
        self.assertEqual(why, "")

    def test_unreadable_carries_only_the_exception_type(self):
        path = f"{self.dir}/panel-b.conf"
        Path(path).write_text("内部端口与证书路径")
        with mock.patch.object(nginx_ops, "path_state", return_value=(None, "PermissionError")):
            state, text, why = nginx_ops._conf_text(path)
        self.assertEqual(state, "unreadable")
        self.assertIsNone(text)
        self.assertEqual(why, "PermissionError")
        self.assertNotIn(path, why, "原因串会进接口响应，不能带绝对路径")

    def test_stat_ok_but_open_refused_is_still_unreadable(self):
        """stat 过得去而 open 被拒（权限在文件自己身上）：同样不许回落到 None。"""
        path = f"{self.dir}/panel-c.conf"
        Path(path).write_text("x")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            state, text, why = nginx_ops._conf_text(path)
        self.assertEqual(state, "unreadable")
        self.assertIsNone(text)
        self.assertEqual(why, "PermissionError")


class WriteRefusalTests(unittest.TestCase):
    """「读不到」必须在动笔之前拒绝：覆盖之后就没有快照可言了。"""

    def setUp(self):
        self.dir = _confdir(self)
        self.path = f"{self.dir}/panel-live.conf"
        Path(self.path).write_text("# 手工维护的现网配置\nserver {\n    listen 443 ssl;\n}\n")
        self.disk = Path(self.path).read_text()
        self.writes = _writers(self)
        self.runs = _runs(self)

    def _refuse(self, coro, expect):
        with mock.patch.object(
            nginx_ops, "_conf_text", return_value=("unreadable", None, "PermissionError")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(coro)
        msg = str(ctx.exception)
        self.assertIn("读不动", msg)
        self.assertIn("PermissionError", msg)
        self.assertIn(expect, msg)
        self.assertEqual(self.writes, [], "拒绝不能只是最后抛错：覆盖那一步一个字节都不能做")
        self.assertEqual(self.runs, [], "没写就不该跑 nginx -t / reload")
        self.assertEqual(Path(self.path).read_text(), self.disk, "被拒绝的写不许改动现有配置")

    def test_save_app_config_refuses_before_touching_anything(self):
        self._refuse(nginx_ops.save_app_config(self.path, NEW_TEXT), "覆盖")

    def test_apply_vhost_refuses_before_touching_anything(self):
        _apply_stubs(self)
        spec = {"name": "live", "domain": "live.example.com", "port": 3100}
        self._refuse(nginx_ops.apply_vhost(spec), "覆盖")

    def test_quick_edit_refuses_before_generating_new_content(self):
        self._refuse(nginx_ops.quick_edit_config(self.path, "ws"), "改")

    def test_missing_still_treated_as_first_write(self):
        """盘上确实没有：仍按新建处理——这就是 missing 与 unreadable 必须分成两档的理由。"""
        _apply_stubs(self)
        gone = f"{self.dir}/panel-new.conf"
        self.assertEqual(nginx_ops._conf_text(gone)[0], "missing")
        out = asyncio.run(nginx_ops.save_app_config(gone, NEW_TEXT))
        self.assertTrue(out["ok"])
        self.assertEqual(self.writes, [(gone, NEW_TEXT)])

    def test_verified_snapshot_is_put_back_when_nginx_test_fails(self):
        """能读到旧内容时，校验不过仍然放回旧内容——本次改动必须保住的行为。"""
        with mock.patch.object(nginx_ops, "run", _bad_nginx), self.assertRaises(RuntimeError):
            asyncio.run(nginx_ops.save_app_config(self.path, "server { broken"))
        self.assertEqual([c[1] for c in self.writes][-1], self.disk, "回滚放回去的必须是动笔前读到的那一份")
        self.assertEqual(Path(self.path).read_text(), self.disk)


def _bad_nginx(cmd, _args, **_kw):
    async def _r():
        return {"code": 0, "out": "ok"} if cmd == "systemctl" else {"code": 1, "out": "nginx: [emerg] bad"}

    return _r()


class RemoveVhostStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = _confdir(self)
        self.path = f"{self.dir}/panel-site.conf"

    def test_absent_returns_absent_without_any_nginx_call(self):
        runs = _runs(self)
        self.assertEqual(asyncio.run(nginx_ops.remove_vhost("site")), "absent")
        self.assertEqual(runs, [], "从未配过域名的应用被清理时，不该白跑一次全站 reload")

    def test_unreadable_refuses_instead_of_lying_about_absence(self):
        Path(self.path).write_text("# 读不动但存在\n")
        disk = Path(self.path).read_text()
        runs = _runs(self)
        unlinks = []
        with mock.patch.object(nginx_ops, "path_state", return_value=(None, "PermissionError")):
            with mock.patch("os.unlink", side_effect=lambda p: unlinks.append(p)):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(nginx_ops.remove_vhost("site"))
        self.assertIn("读不动", str(ctx.exception))
        self.assertEqual(unlinks, [], "没有快照就不能删：删掉之后 nginx -t 不过就什么都还不回来")
        self.assertEqual(runs, [])
        self.assertEqual(Path(self.path).read_text(), disk)

    def test_removed_reports_removed(self):
        Path(self.path).write_text("server { listen 80; }\n")
        runs = _runs(self)
        self.assertEqual(asyncio.run(nginx_ops.remove_vhost("site")), "removed")
        self.assertFalse(os.path.lexists(self.path))
        self.assertEqual(runs, ["nginx", "systemctl"])

    def test_failed_nginx_test_puts_the_file_back(self):
        content = "# 别的文件引用了这里的 upstream\n"
        Path(self.path).write_text(content)
        with mock.patch.object(nginx_ops, "run", _bad_nginx), self.assertRaises(RuntimeError) as ctx:
            asyncio.run(nginx_ops.remove_vhost("site"))
        self.assertIn("恢复回去", str(ctx.exception))
        self.assertEqual(Path(self.path).read_text(), content, "校验不过时那次「删除」必须整笔撤销")


class SafeConfPathSymlinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = _confdir(self)

    def test_dangling_symlink_no_longer_skips_realpath(self):
        """悬空链接在 exists() 眼里等于「没有文件」，旧写法因此跳过 realpath，守卫改拿链接自己的
        路径判断：同一个链接在目标存在与不存在时给出两种判定，口径跟着第三个文件的状态漂。"""
        link = f"{self.dir}/panel-link.conf"
        outside = tempfile.mkdtemp(prefix="cp_77_outside_")
        self.addCleanup(shutil.rmtree, outside, True)
        target = f"{outside}/no-such-file.conf"
        os.symlink(target, link)
        self.assertFalse(os.path.exists(link), "前提：这是一个 exists() 读成 False 的悬空链接")
        with self.assertRaises(RuntimeError) as ctx:
            nginx_ops._safe_conf_path(link)
        self.assertIn("仅支持", str(ctx.exception), "指向目录外的链接，无论目标在不在都按真实位置拒掉")
        self.assertFalse(os.path.lexists(target), "拒绝本身不该把外面的目标文件创建出来")


class RealPermissionTests(unittest.TestCase):
    """真实权限下的复现：chmod 000 + fork 成 nobody（root 有 DAC_OVERRIDE，进程内 chmod 挡不住自己）。

    两种读不到分开验：文件自己 000（stat 过得去、open 被拒）与父目录 000（连 stat 都被拒）。
    后者正是被折叠的那一档——旧写法在这里拿到 False，然后一切按「盘上没有」继续走。
    """

    DISK_TEXT = "# 现网手工维护的 vhost，面板一个字都不该动\nserver {\n    listen 443 ssl;\n}\n"

    def setUp(self):
        # 必须放在 /tmp 下：CLI 传进来的 TMPDIR 是 0700 root，nobody 连目录都进不去，
        # 那样复现出来的就不是「配置读不到」而是「路径不存在」。
        self.base = tempfile.mkdtemp(prefix="cp_77_perm_", dir="/tmp")
        os.chmod(self.base, 0o755)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.conf = f"{self.base}/conf.d"
        os.makedirs(self.conf)
        os.chmod(self.conf, 0o755)
        self.path = f"{self.conf}/panel-demo.conf"
        Path(self.path).write_text(self.DISK_TEXT)
        os.chmod(self.path, 0o000)
        self.old_dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [self.conf]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self.old_dirs)
        p = mock.patch.object(nginx_ops, "CONF_DIR", self.conf)
        p.start()
        self.addCleanup(p.stop)

    def _probe(self):
        # 子进程里不碰文件系统去「确认」状态：那一次 stat 本身就是被测对象。
        out = []

        async def rec(*_a, **_k):
            out.append("RAN")
            return {"code": 0, "out": "ok"}

        with mock.patch.object(nginx_ops, "run", rec):
            state, text, why = nginx_ops._conf_text(self.path)
            out.append(f"STATE={state}")
            out.append(f"REASON={why}")
            out.append(f"LEN={0 if text is None else len(text)}")
            for name, coro in (
                ("SAVE", nginx_ops.save_app_config(self.path, NEW_TEXT)),
                ("REMOVE", nginx_ops.remove_vhost("demo")),
                ("QUICK", nginx_ops.quick_edit_config(self.path, "ws")),
            ):
                try:
                    asyncio.run(coro)
                    out.append(f"{name}=WROTE_OK")
                except RuntimeError as e:
                    # 拒绝的理由也要验：守卫如果先按路径拒掉，这条用例就变成空过的绿灯
                    out.append(f"{name}=REFUSED-{'nosnapshot' if '读不动' in str(e) else 'other'}")
        return " ".join(out)

    def _fork_probe(self):
        """root 时降权到 nobody 跑探针，让真实的 DAC 判定参与；非 root 时直接跑。"""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            try:
                import pwd

                pw = pwd.getpwnam("nobody")
                uid, gid = pw.pw_uid, pw.pw_gid
            except (ImportError, KeyError):
                self.skipTest("root 但查不到 nobody，复现不了非 root 现场")
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.dup2(w, 1)
                    os.dup2(w, 2)
                    os.close(r)
                    os.setgid(gid)
                    os.setuid(uid)
                    os.write(w, (self._probe() or "").encode("utf-8") + b"\n")
                except BaseException:  # noqa: BLE001
                    import traceback

                    traceback.print_exc()
                finally:
                    os._exit(9)
            os.close(w)
            with os.fdopen(r, "r", encoding="utf-8", errors="replace") as f:
                out = f.read()
            os.waitpid(pid, 0)
            return out
        return self._probe()

    def _assert_refused(self, out):
        self.assertNotIn("Traceback", out, f"读者不该把异常抛给调用方：{out[:600]}")
        self.assertNotIn("WROTE_OK", out, f"读不到时写与删都必须拒绝：{out[:600]}")
        self.assertNotIn("RAN", out, f"拒绝不能先跑一次 nginx：{out[:600]}")
        self.assertIn("STATE=unreadable", out, f"读不到必须自成一级：{out[:600]}")
        self.assertIn("REASON=PermissionError", out, f"原因要能指到权限：{out[:600]}")
        self.assertIn("LEN=0", out, f"正文必须是空的：{out[:600]}")
        self.assertNotIn("REFUSED-other", out, f"拒绝的理由必须是「拿不到快照」：{out[:600]}")
        self.assertEqual(out.count("=REFUSED-nosnapshot"), 3, f"三条读写路径都要拒绝：{out[:600]}")

    def test_file_itself_unreadable(self):
        if os.path.realpath(self.path) != self.path:
            self.skipTest(f"{self.base} 里有符号链接组件，守卫会先按路径拒绝，复现不到读不到这一档")
        out = self._fork_probe()
        self._assert_refused(out)
        self.assertEqual(Path(self.path).read_text(), self.DISK_TEXT, "面板一个字都没动盘上那份")
        self.assertEqual(sorted(os.listdir(self.conf)), ["panel-demo.conf"], "不留临时文件")

    def test_parent_dir_unreadable_is_not_read_as_absent(self):
        os.chmod(self.conf, 0o000)
        try:
            out = self._fork_probe()
        finally:
            os.chmod(self.conf, 0o755)
        self._assert_refused(out)
        self.assertEqual(Path(self.path).read_text(), self.DISK_TEXT)


class SourceContractTests(unittest.TestCase):
    """把这一批修掉的形状钉住：模块里不许再出现「stat 折成 False 就当没有」的读者。"""

    def _code(self, rel):
        """只剩可执行行：这一批把旧写法留在注释与 docstring 里当证据，守卫不许报说明文字。

        只摘 docstring，不摘全部字符串常量——`return "absent"` 这类契约本身就写在字符串里。
        """
        import ast

        p = APP_DIR / rel
        text = p.read_text(encoding="utf-8")
        hide = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if ast.get_docstring(node) is None:
                    continue
                first = node.body[0]
                for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                    hide.add(ln)
        return "\n".join(line for i, line in enumerate(text.splitlines(), 1) if i not in hide)

    def test_no_exists_folded_snapshot_left_in_nginx_ops(self):
        code = self._code("services/nginx_ops.py")
        self.assertNotIn("os.path.exists", code, "exists() 把 PermissionError 折成 False：这一批之后只留 lexists")
        self.assertIn("os.path.lexists", code)
        self.assertGreaterEqual(code.count("_conf_text("), 5, "四条读写路径都要走同一个三态读者")

    def test_remove_vhost_is_the_single_source_of_removed(self):
        code = self._code("services/nginx_ops.py")
        self.assertIn('return "absent"', code)
        self.assertIn('return "removed"', code)
        router = self._code("routers/sites.py")
        self.assertNotIn("vhost_path(app[\"name\"])).exists()", router, "removed 不许有第二个读者")
        self.assertIn('remove_vhost(app["name"]) == "removed"', router)

    def test_snapshot_comes_from_the_three_state_reader(self):
        code = self._code("services/nginx_ops.py")
        self.assertNotIn('read_text(errors="replace") if os.path', code)


if __name__ == "__main__":
    unittest.main()
