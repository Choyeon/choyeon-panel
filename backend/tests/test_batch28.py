"""批次㉘：恢复能撞上部署、凭据落盘有 0644 窗口、名字校验被一个换行过关。

四条互不相干但同一类的缺陷，都是"校验/写入的口径比看起来松一格"：

1. restore_backup 没有任何互斥。两个 admin 各开一页、各点一次「恢复」，两次恢复都会
   停同一个 unit、按各自的归档覆盖同一个目录、再 start：后一次把前一次的成果整份换掉，
   先返回的那句「恢复完成」当场变假的。恢复也没有过 `_ensure_no_deployment` 这道闸——
   而它是最后一个还能改写应用目录的入口（删除、回滚、改配置、改 unit 全都已经挡了）。
2. .panel.env / drop-in / unit 都是 `write_text` + 事后 `chmod 0600`。O_CREAT 按 umask
   建文件（root 常态 022 → 0644），于是新写的 .panel.env 从出现的第一刻起世界可读，
   直到那一下 chmod；同机任何一个被攻破的应用进程在这个窗口 cat 一次就拿到口令。
   就地截断重写被打断还会留下半截文件：.env 当场不可解析，unit 更阴——它当时还没被
   daemon-reload 读进去，要等之后任何一次 reload 才炸成一句和这次保存无关的语法错误。
3. `_tar_checked` 只逐条看归档里的条目类型，看不到目标目录里本来就有的软链：应用目录下
   一条 `data -> /var/lib/app`（安装脚本把可变数据挪出仓库的常见写法）就能让
   `data/x.json` 落到目录之外，而面板是以 root 解包的。
4. 库名/环境变量名这类标识符校验用 `re.match(r"^…$")`：Python 的 `$` 允许结尾多一个换行，
   所以 "appdb\n" 过关。这个名字接着被拼进 `su -s /bin/sh postgres -c 'pg_dump -Fc appdb\n'`
   ——换行在 shell 里断行，等于往 root→postgres 的命令串里多塞了一段。util.py 早就把这条
   口径写在注释里并全线用 fullmatch，这几个模块是漏网的。
"""

import asyncio
import json
import os
import shutil
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app import backup_runner as R
from app import util as U
from app.services import apps_service as A
from app.services import backup_service as B
from app.services import pg_service as P


def _fake_run(calls):
    async def run(_cmd, argv=None, **_kw):
        calls.append(list(argv or []))
        return {"code": 0, "out": "", "err": ""}

    return run


def _member(name):
    """_reject_symlink_ancestors 只用 m.name，不必为了测试造整份归档。"""
    return types.SimpleNamespace(name=name)


def _src(mod):
    return Path(mod.__file__).read_text(encoding="utf-8")


def _window(mod, start, end):
    src = _src(mod)
    return src[src.index(start): src.index(end)]


class Case(unittest.TestCase):
    def setUp(self):
        self.appdir = tempfile.mkdtemp(prefix="cp_app28_")
        self.units = tempfile.mkdtemp(prefix="cp_units28_")
        self.addCleanup(shutil.rmtree, self.appdir, True)
        self.addCleanup(shutil.rmtree, self.units, True)
        p = mock.patch.object(A, "UNIT_DIR", self.units)
        p.start()
        self.addCleanup(p.stop)
        self.systemctl = []
        r = mock.patch.object(A, "run", _fake_run(self.systemctl))
        r.start()
        self.addCleanup(r.stop)

    def app(self, **kw):
        base = {
            "id": 1, "name": "vid", "type": "node", "path": self.appdir, "port": 3100,
            "domain": None, "start_cmd": "npm run start", "install_cmd": None,
            "env": "[]", "unit_override": None, "unit_template": None,
        }
        return {**base, **kw}

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def restore_as(self, bk, fn_name, body):
        """把 restore_backup 的取数与解包段都换成替身，只留互斥与流程本身。"""
        return (
            mock.patch.object(B.dbm, "query_one", lambda *a, **k: bk),
            mock.patch.object(B.dbm, "audit", lambda *a, **k: None),
            mock.patch.object(B, "_backup_file_path", lambda bid, fn: f"/tmp/{fn}"),
            mock.patch.object(B, "_restoreable", lambda kind, name: True),
            mock.patch.object(B, fn_name, body),
        )


class TestSecretFileMode(Case):
    """0600 必须是"从建立起就是"，不是"写完再补一下"。"""

    def test_created_without_relying_on_later_chmod(self):
        a = self.app(env=json.dumps([{"k": "ADMIN_PASSWORD", "v": "s3cret"}]))
        # 把 chmod 打掉：旧写法在这里会留下 0644（或直接抛错），新写法根本不经过 chmod。
        with mock.patch.object(U.os, "chmod", side_effect=AssertionError("不该靠事后 chmod")):
            A.write_env_file(a)
        target = Path(self.appdir, ".panel.env")
        self.assertEqual(self.mode(target), 0o600)
        self.assertIn('ADMIN_PASSWORD="s3cret"', target.read_text())

    def test_umask_cannot_widen_the_mode(self):
        a = self.app(env=json.dumps([{"k": "K", "v": "1"}]))
        old = os.umask(0o000)  # 最宽的 umask：O_CREAT 不给 mode 就等于 0666
        try:
            A.write_env_file(a)
        finally:
            os.umask(old)
        self.assertEqual(self.mode(Path(self.appdir, ".panel.env")), 0o600)

    def test_unit_file_is_0600(self):
        asyncio.run(A.ensure_unit(self.app()))
        self.assertEqual(self.mode(Path(self.units, A.unit_name(self.app()))), 0o600)

    def test_drop_in_is_0600(self):
        # 纳管的 unit 没引用 .panel.env 时，面板补一份 drop-in：里面写着 env 文件的绝对路径。
        unit = A.unit_name(self.app())
        Path(self.units, unit).write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
        self.assertEqual(asyncio.run(A.ensure_env_link(self.app())), "written")
        drop_in = Path(self.units, f"{unit}.d", A.ENV_DROP_IN_NAME)
        self.assertEqual(self.mode(drop_in), 0o600)


class TestAtomicWrite(Case):
    def test_failure_keeps_old_content_and_leaves_no_temp(self):
        a = self.app(env=json.dumps([{"k": "K", "v": "old"}]))
        self.assertEqual(A.write_env_file(a), "written")
        target = Path(self.appdir, ".panel.env")
        before = target.read_text()
        changed = {**a, "env": json.dumps([{"k": "K", "v": "new"}])}
        with mock.patch.object(U.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                A.write_env_file(changed)
        self.assertEqual(target.read_text(), before)  # 没有半截、也没有被截空
        self.assertEqual([p for p in os.listdir(self.appdir) if p.endswith(".tmp")], [])

    def test_temp_lives_next_to_target_and_is_privileged_shaped(self):
        a = self.app(env=json.dumps([{"k": "K", "v": "1"}]))
        seen = {}
        real = os.replace

        def spy(src, dst, *ar):
            seen["src"] = src
            return real(src, dst, *ar)

        with mock.patch.object(U.os, "replace", spy):
            A.write_env_file(a)
        self.assertEqual(os.path.dirname(seen["src"]), self.appdir)  # 同盘才谈得上原子替换
        base = os.path.basename(seen["src"])
        self.assertTrue(base.startswith("."), base)  # 点开头：目录列表与备份都不认它
        self.assertTrue(base.endswith(".tmp"), base)
        self.assertIn(str(os.getpid()), base)  # 多进程/多线程不共用临时名

    def test_unchanged_path_still_reapplies_0600(self):
        a = self.app(env=json.dumps([{"k": "K", "v": "1"}]))
        A.write_env_file(a)
        target = Path(self.appdir, ".panel.env")
        os.chmod(target, 0o644)  # 手工改过或从归档恢复回来的权限，这里补回
        self.assertEqual(A.write_env_file(a), "unchanged")
        self.assertEqual(self.mode(target), 0o600)

    def test_no_bare_write_text_left_on_the_config_paths(self):
        self.assertNotIn(".write_text(", _src(A))
        head = _src(B)[: _src(B).index("async def _restore")]
        self.assertNotIn("Path(path).write_text(", head)


class TestRestoreMutex(Case):
    def test_same_target_restores_never_overlap(self):
        bk = {"id": 7, "kind": "app", "target": "vid"}
        live = {"now": 0, "peak": 0}

        async def slow(_bk, _src, log):
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            await asyncio.sleep(0.02)
            live["now"] -= 1
            log.append("==> 已解包")

        async def go():
            return await asyncio.gather(
                *[B.restore_backup(7, "a.tar.gz", "admin") for _ in range(3)],
                return_exceptions=True,
            )

        patches = self.restore_as(bk, "_restore_app", slow)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            res = asyncio.run(go())
        self.assertEqual(live["peak"], 1)
        ok = [r for r in res if isinstance(r, dict)]
        err = [r for r in res if isinstance(r, RuntimeError)]
        self.assertEqual((len(ok), len(err)), (1, 2))
        for e in err:
            self.assertIn("恢复正在进行", str(e))

    def test_lock_is_released_after_a_successful_restore(self):
        bk = {"id": 7, "kind": "sqlite", "target": "1"}

        async def noop(_bk, _src, log):
            log.append("==> ok")

        patches = self.restore_as(bk, "_restore_sqlite", noop)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            asyncio.run(B.restore_backup(7, "a.sqlite.gz", "admin"))
            self.assertFalse(B._restore_lock("sqlite:1").locked())
            # 第二次还能进来，不是"一次恢复把这个目标永久锁死"
            asyncio.run(B.restore_backup(7, "a.sqlite.gz", "admin"))

    def test_different_targets_do_not_queue_behind_each_other(self):
        seen = []

        async def rec(bk, _src, log):
            seen.append(bk["target"])
            log.append("==> ok")

        for target in ("a", "b"):
            bk = {"id": 1, "kind": "app", "target": target}
            patches = self.restore_as(bk, "_restore_app", rec)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                asyncio.run(B.restore_backup(1, "x.tar.gz", "admin"))
        self.assertEqual(seen, ["a", "b"])

    def test_deploy_guards_are_on_both_restore_paths(self):
        app_fn = _window(B, "async def _restore_app", "async def _restore_pg")
        sql_fn = _window(B, "async def _restore_sqlite", "def _tar_checked")
        for name, fn in (("_restore_app", app_fn), ("_restore_sqlite", sql_fn)):
            self.assertIn("_ensure_no_deployment", fn, name)


class TestSymlinkGate(Case):
    def setUp(self):
        super().setUp()
        self.dest = tempfile.mkdtemp(prefix="cp_dest28_")
        self.outside = tempfile.mkdtemp(prefix="cp_outside28_")
        self.addCleanup(shutil.rmtree, self.dest, True)
        self.addCleanup(shutil.rmtree, self.outside, True)

    def test_symlinked_directory_blocks_whole_archive(self):
        os.symlink(self.outside, os.path.join(self.dest, "data"))
        with self.assertRaises(RuntimeError) as got:
            B._reject_symlink_ancestors(self.dest, [_member("./data/x.json")])
        self.assertIn("data", str(got.exception))
        self.assertEqual(os.listdir(self.outside), [])  # 拒绝是"整份不解"，不是解到一半

    def test_symlinked_file_blocks_too(self):
        victim = os.path.join(self.outside, "passwd")
        Path(victim).write_text("keep", encoding="utf-8")
        os.symlink(victim, os.path.join(self.dest, "config.json"))
        with self.assertRaises(RuntimeError):
            B._reject_symlink_ancestors(self.dest, [_member("./config.json")])
        self.assertEqual(Path(victim).read_text(), "keep")

    def test_plain_tree_passes_and_deep_link_is_named(self):
        os.makedirs(os.path.join(self.dest, "a", "b"))
        Path(self.dest, "a", "b", "c.json").write_text("{}", encoding="utf-8")
        B._reject_symlink_ancestors(
            self.dest, [_member("./a"), _member("./a/b"), _member("./a/b/c.json")]
        )
        os.symlink("/etc", os.path.join(self.dest, "a", "b", "esc"))
        with self.assertRaises(RuntimeError) as got:
            B._reject_symlink_ancestors(self.dest, [_member("./a/b/esc/passwd")])
        self.assertIn("a/b/esc", str(got.exception))

    def test_gate_runs_before_the_service_is_stopped(self):
        fn = _window(B, "async def _restore_app", "async def _restore_pg")
        # 批次㉜ 之后停服务收进了 _stop_for_overwrite：符号链接闸必须仍然排在它前面，
        # 否则校验不过时已经把服务停下来了（现网表现为"点了恢复，应用莫名断了几分钟"）。
        self.assertLess(fn.index("_reject_symlink_ancestors"), fn.index("_stop_for_overwrite(unit, log)"))


class TestIdentifiersUseFullmatch(Case):
    def test_database_name_with_trailing_newline_is_rejected(self):
        self.assertTrue(P.is_ident("appdb"))
        self.assertFalse(P.is_ident("appdb\n"))
        self.assertFalse(P.is_ident("appdb\nX"))
        self.assertFalse(P.is_ident("app;drop db"))

    def test_env_key_with_trailing_newline_is_dropped(self):
        body = A.env_body({"env": json.dumps([{"k": "A\n", "v": "x"}, {"k": "B", "v": "y"}])})
        self.assertEqual(body, 'B="y"')

    def test_no_anchored_match_left_in_the_validator_modules(self):
        # `$` 在 match 下允许结尾多一个换行；这几个模块里的名字全都往 shell/argv/SQL 去。
        for mod in (U, R, P, A, B):
            name = Path(mod.__file__).name
            for line in _src(mod).splitlines():
                if "re.match(" in line:
                    self.fail(f"{name} 里校验又回到 match：{line.strip()}")


class TestPortForPidMultiPidLine(Case):
    LINES = [
        'LISTEN 0 511 *:3306 *:* users:(("mysqld",pid=99,fd=24))',
        # cluster/预派生：同一个监听套接字挂在多个 pid 上，主进程排在后面
        'LISTEN 0 511 *:8080 *:* users:(("node",pid=11,fd=18),("node",pid=7,fd=18))',
    ]

    def probe(self, pids):
        async def go():
            with mock.patch.object(A, "_pid_tree", lambda _m: set(pids)):
                return await A._port_for_pid(next(iter(pids)), self.LINES)

        return asyncio.run(go())

    def test_later_pid_on_the_line_still_matches(self):
        self.assertEqual(self.probe({7, 11}), 8080)

    def test_foreign_lines_are_still_ignored(self):
        self.assertEqual(self.probe({99}), 3306)

    def test_source_uses_findall(self):
        fn = _window(A, "async def _port_for_pid", "async def list_apps")
        self.assertIn("re.findall(", fn)


if __name__ == "__main__":
    unittest.main()
