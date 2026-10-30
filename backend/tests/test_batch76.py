"""批次76：unit 文件读不到时，详情页不许把它显示成「空文件/面板默认模板」，保存更不许回滚成删除。

现场（生产机同一类缺陷已复现过两次：批次71 的 suggest_unit、批次72 的 env_drift）：
`apps_service.UNIT_DIR` 里那份 unit 在，但这一次读不动（权限 / ACL / 挂载选项 / 读到一半被换掉）。

- 读的一侧：`read_unit` 用 `Path(path).read_text() if os.path.exists(path) else ""`，
  `Path.exists()` 把 PermissionError 折成 False（批次75 已把这条写进 util.path_state），
  于是 content 是空串；前端 `r.content || r.defaultTemplate` 再把空串读成默认模板。
  用户在编辑框里看到的是一份"现网配置"，而它一个字都不是盘上的东西——照着它改、点保存，
  就把自己没见过的内容覆盖了真正在跑的那份 unit。
- 写的一侧更狠：`save_unit` 的回滚快照 `old = ... if os.path.exists(path) else None`。
  读不动 → old=None → `systemd-analyze verify` 一失败，`_restore()` 走 unlink 分支，
  **把正在跑着这个应用的 unit 文件删了**。应用还在跑（systemd 已加载），但 unit 没了，
  下一次 daemon-reload 之后 systemd 直接回答 not-found：启停、日志、环境变量全断。
- 顺带一条：裸 `read_text` 在 stat 能过、open 不能过时（000 文件）原样抛 OSError，
  GET /api/apps/{id}/unit 打成 500，详情页整个页签读不到。

修法：`_unit_text()` 给三态（ok / missing / unreadable，text 只在 ok 时非 None），
read_unit 带 file_state + file_reason 且 unreadable 时 content 为空；前端按状态分支，
unreadable 就锁住编辑与三个写按钮、如实说明读不到；save_unit 在拿不到回滚快照时**先拒绝**，
不写文件也不跑 verify/daemon-reload。
"""

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b76_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import apps_service  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
SERVICE_PY = (SRC / "backend" / "app" / "services" / "apps_service.py").read_text(encoding="utf-8")
VUE = (SRC / "web" / "src" / "views" / "AppDetail.vue").read_text(encoding="utf-8")

# 盘上那份与面板默认模板必须明显不同：本批要断言的正是"编辑框里显示的是前者"
DISK_TEXT = "[Unit]\nDescription=外部手工改过的一份\n\n[Service]\nExecStart=/bin/true\n"
GOOD_UNIT = "[Service]\nExecStart=/bin/true\n"


def _row(**kw):
    base = {"id": 1, "name": "demo", "path": "/srv/cp-b76-demo", "start_cmd": "node i.js",
            "port": 3001, "env": "[]", "unit_override": None, "unit_template": None}
    return {**base, **kw}


def _db(row):
    """query_one 的分流：get_app 要拿应用行，_ensure_no_deployment 要拿"没有在跑的部署"。"""
    def side_effect(sql, *a, **k):
        return None if "deployments" in sql else row
    return side_effect


def _read_at(units_dir, row=None):
    with mock.patch.object(apps_service, "UNIT_DIR", units_dir), \
            mock.patch.object(apps_service.dbm, "query_one", _db(row or _row())):
        return apps_service.read_unit(1)


def _save_at(units_dir, content, row=None, verify_code=0, reload_code=0):
    calls = []

    async def fake_run(cmd, args, **kw):
        calls.append(cmd)
        if cmd == "systemd-analyze":
            return {"code": verify_code, "out": "verify said no"}
        if cmd == "systemctl":
            return {"code": reload_code, "out": "reload said no"}
        return {"code": 0, "out": ""}

    with mock.patch.object(apps_service, "UNIT_DIR", units_dir), \
            mock.patch.object(apps_service.dbm, "query_one", _db(row or _row())), \
            mock.patch.object(apps_service, "run", fake_run), \
            mock.patch.object(apps_service.dbm, "execute", lambda *a, **k: None):
        out = asyncio.run(apps_service.save_unit(1, content))
    return out, calls


class UnitTextTests(unittest.TestCase):
    """_unit_text 的三档：读不动永远不许并进"没有"。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b76_ut_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_ok_returns_the_exact_bytes(self):
        p = Path(self.dir, "panel-demo.service")
        p.write_text(DISK_TEXT, encoding="utf-8")
        state, text, why = apps_service._unit_text(str(p))
        self.assertEqual((state, why), ("ok", ""))
        self.assertEqual(text, DISK_TEXT)

    def test_confirmed_absent_is_missing_not_unreadable(self):
        state, text, why = apps_service._unit_text(f"{self.dir}/never-written.service")
        self.assertEqual((state, text, why), ("missing", None, ""))

    def test_absent_because_parent_is_not_a_directory_is_missing(self):
        # ENOTDIR 是"确定没有"，不是"确认不了"：路径前缀是个普通文件
        f = Path(self.dir, "afile")
        f.write_text("x", encoding="utf-8")
        state, _text, why = apps_service._unit_text(f"{f}/inner.service")
        self.assertEqual(state, "missing")
        self.assertEqual(why, "")

    def test_stat_passes_but_open_fails_is_unreadable_not_missing(self):
        # stat 能过、read_text 抛 IsADirectoryError：这一档过去直接 500
        d = Path(self.dir, "panel-demo.service")
        d.mkdir()
        state, text, why = apps_service._unit_text(str(d))
        self.assertEqual(state, "unreadable")
        self.assertIsNone(text, "读不动时不能交出一份全文，哪怕是空串也不能冒充")
        self.assertEqual(why, "IsADirectoryError")

    def test_reason_never_carries_the_absolute_path(self):
        d = Path(self.dir, "x.service")
        d.mkdir()
        _state, _text, why = apps_service._unit_text(str(d))
        self.assertNotIn(str(d), why, "原因串会进接口响应，只带异常类型")


class ReadUnitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b76_read_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_disk_content_wins_over_the_template(self):
        Path(self.dir, "panel-demo.service").write_text(DISK_TEXT, encoding="utf-8")
        r = _read_at(self.dir)
        self.assertEqual(r["file_state"], "ok")
        self.assertEqual(r["content"], DISK_TEXT)
        self.assertIn("Description=choyeon-panel app demo", r["defaultTemplate"])
        self.assertNotEqual(r["content"], r["defaultTemplate"])

    def test_missing_reports_missing_and_empty_content(self):
        r = _read_at(self.dir)
        self.assertEqual(r["file_state"], "missing")
        self.assertEqual(r["content"], "")
        self.assertEqual(r["file_reason"], "")

    def test_unreadable_does_not_raise_and_does_not_return_a_template(self):
        Path(self.dir, "panel-demo.service").mkdir()  # stat 在，读不动
        r = _read_at(self.dir)  # 旧实现在这里抛 OSError → 路由 500
        self.assertEqual(r["file_state"], "unreadable")
        self.assertEqual(r["content"], "", "只回空串会被前端读成「空文件」，必须配 file_state 一起看")
        self.assertEqual(r["file_reason"], "IsADirectoryError")
        self.assertIn("defaultTemplate", r, "模板本身还要给——missing 那一档前端要用它预填")

    def test_managed_flag_still_reported_in_every_state(self):
        row = _row(unit_override="choyeon-book.service")
        Path(self.dir, "choyeon-book.service").write_text(DISK_TEXT, encoding="utf-8")
        self.assertFalse(_read_at(self.dir, row)["managed"])
        shutil.rmtree(self.dir)
        os.makedirs(self.dir)
        self.assertFalse(_read_at(self.dir, row)["managed"], "missing 也不许把纳管说成面板生成")


class SaveUnitGuardTests(unittest.TestCase):
    """写的一侧：拿不到回滚快照就拒绝，绝不留下"删掉正在生效的 unit"这条路径。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b76_save_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _save(self, **kw):
        return _save_at(self.dir, GOOD_UNIT, **kw)

    def test_unreadable_existing_unit_is_refused_before_any_write(self):
        unit = Path(self.dir, "panel-demo.service")
        unit.mkdir()  # 读不动（本进程内可复现的最轻档）
        with self.assertRaises(RuntimeError) as cm:
            self._save(verify_code=1)
        msg = str(cm.exception)
        self.assertIn("读不动", msg)
        self.assertTrue(unit.is_dir(), "拒绝就是什么都没写：不许把目录换成文件")
        self.assertFalse([p for p in Path(self.dir).glob("*") if p.name == "panel-demo.service.tmp"],
                        "临时文件也不该出现")

    def test_refusal_does_not_run_verify_or_daemon_reload(self):
        Path(self.dir, "panel-demo.service").mkdir()
        with mock.patch.object(apps_service, "_unit_text",
                               lambda _p: ("unreadable", None, "PermissionError")), \
                mock.patch.object(apps_service, "run") as run_mock:
            with self.assertRaises(RuntimeError):
                asyncio.run(apps_service.save_unit(1, GOOD_UNIT))
        run_mock.assert_not_called()

    def test_readable_old_unit_survives_a_failed_verify_as_itself(self):
        # 回归护栏：old 读到过 → 回滚是写回原文，不是删除
        unit = Path(self.dir, "panel-demo.service")
        unit.write_text(DISK_TEXT, encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self._save(verify_code=1)
        self.assertTrue(unit.is_file(), "这份 unit 正在跑着应用，回滚把它删掉是本批的破坏性缺陷")
        self.assertEqual(unit.read_text(encoding="utf-8"), DISK_TEXT)

    def test_first_save_failure_still_removes_the_new_file(self):
        # 批次㉙ 那条不能退让：确认没有（missing）时 old is None，失败就该删掉刚写的
        with self.assertRaises(RuntimeError):
            self._save(verify_code=1)
        self.assertEqual(list(Path(self.dir).glob("panel-demo.service")), [])

    def test_happy_path_still_writes_and_reloads(self):
        out, calls = self._save()
        self.assertTrue(out["ok"])
        self.assertEqual(calls, ["systemd-analyze", "systemctl"])
        self.assertEqual(Path(self.dir, "panel-demo.service").read_text(encoding="utf-8"), GOOD_UNIT)

    def test_override_mode_refusal_unchanged(self):
        with self.assertRaises(RuntimeError):
            _save_at(self.dir, GOOD_UNIT, row=_row(unit_override="book.service"))


class RealPermissionTests(unittest.TestCase):
    """真权限现场：owner 读不动一份存在的 unit，读与写各自诚实。

    root 有 DAC_OVERRIDE，chmod 000 拦不住它，所以按批次70/72 的形状 fork 降权到 nobody，
    在同一条代码路径上跑，把结果通过管道送回父进程断言。
    """

    def setUp(self):
        # 显式落在 /tmp：tempfile 默认的 TMPDIR 在这一套环境里是会话目录（0700 root），
        # nobody 连 traverse 都过不去，那测出来的是"整条路径都进不去"，
        # 而不是本批要钉的"文件在、stat 过得去、open 被拒"。
        self.base = tempfile.mkdtemp(prefix="cp_b76_perm_", dir="/tmp")
        self.units = Path(self.base, "units")
        self.units.mkdir()
        os.chmod(self.base, 0o755)
        os.chmod(str(self.units), 0o755)
        self.unit = Path(self.units, "panel-demo.service")
        self.unit.write_text(DISK_TEXT, encoding="utf-8")
        os.chmod(self.unit, 0o000)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with contextlib.suppress(OSError):
            os.chmod(str(self.unit), 0o644)
        shutil.rmtree(self.base, True)

    def _probe(self):
        """返回一句话描述两个读者的实测行为，在带真权限的进程里跑（不碰盘上那份，父进程验）。"""
        try:
            r = _read_at(str(self.units))
            read = f"STATE={r['file_state']} REASON={r['file_reason']} LEN={len(r['content'])}"
        except Exception as e:  # noqa: BLE001 — 崩了也要送回父进程
            read = "READ_RAISED=" + type(e).__name__
        try:
            _save_at(str(self.units), GOOD_UNIT)
            write = "WROTE_OK"
        except RuntimeError as e:
            write = f"REFUSED {type(e).__name__} {e}"
        except Exception as e:  # noqa: BLE001
            write = "WRITE_RAISED=" + type(e).__name__
        return f"{read} || {write}"

    def test_unreadable_unit_reads_as_unreadable_and_write_refuses(self):
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
        else:
            out = self._probe()
        self.assertNotIn("RAISED=", out, f"两个读者都不该把异常抛给调用方：{out[:600]}")
        self.assertIn("STATE=unreadable", out, f"读不到必须自成一级：{out[:600]}")
        self.assertIn("REASON=PermissionError", out, f"原因要能指到权限：{out[:600]}")
        self.assertIn("LEN=0", out, f"content 必须是空的：{out[:600]}")
        self.assertIn("REFUSED", out, f"读不到回滚快照时保存必须拒绝：{out[:600]}")
        self.assertNotIn("WROTE_OK", out, f"拒绝了就不能真的写：{out[:600]}")
        # 盘上那份由父进程（读得动的人）验：拒绝必须真的什么都没改
        os.chmod(str(self.unit), 0o644)
        self.assertTrue(self.unit.is_file(), "这份 unit 正跑着应用；旧实现回滚时把它 unlink 了")
        self.assertEqual(self.unit.read_text(encoding="utf-8"), DISK_TEXT, "一个字都不许变")
        self.assertEqual([p.name for p in self.units.iterdir()], ["panel-demo.service"],
                         "拒绝不该留下临时文件或新写的那份")


def _code_lines(text):
    """只剩可执行行：这一批把旧写法留在注释与 docstring 里当证据，守卫不许报说明文字。"""
    import ast
    tree = ast.parse(text)
    drop = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    kept = [line for no, line in enumerate(text.splitlines(), 1)
            if no not in drop and not line.strip().startswith("#")]
    return "\n".join(kept)


class SourceContractTests(unittest.TestCase):
    def test_read_unit_gives_three_states_and_no_fallback(self):
        fn = _code_lines(SERVICE_PY).split("def read_unit")[1].split("\nasync def ")[0]
        self.assertIn('_unit_text(path)', fn)
        self.assertIn('"file_state": file_state', fn)
        self.assertIn('"file_reason": why', fn)

    def test_unit_text_helper_keeps_unreadable_text_none(self):
        fn = _code_lines(SERVICE_PY).split("def _unit_text")[1].split("\ndef ")[0]
        self.assertIn("return \"unreadable\", None, why", fn)
        self.assertIn("return \"missing\", None, \"\"", fn)

    def test_save_unit_takes_its_snapshot_from_the_tri_state_reader(self):
        fn = _code_lines(SERVICE_PY).split("async def save_unit")[1].split("\n\nPROC_FIELDS")[0]
        self.assertIn("old_state, old, old_why = _unit_text(path)", fn)
        self.assertNotIn("os.path.exists(path)", fn,
                         "exists 折 PermissionError → old=None → 回滚 unlink 正在生效的 unit")

    def test_frontend_branches_on_file_state(self):
        body = _vue_code(VUE)
        self.assertNotIn("r.content || r.defaultTemplate", body,
                         "这一句把 missing 与 unreadable 读成同一句话（本批的缺陷本体）")
        self.assertIn("r.file_state === 'ok' ? r.content : r.defaultTemplate", body)
        self.assertIn("if (unitUnreadable.value) unitContent.value = '';", body)
        for frag in ('const unitUnreadable = computed(() => unitInfo.value?.file_state === \'unreadable\')',
                     'const unitMissing = computed(() => unitInfo.value?.file_state === \'missing\')',
                     ':readonly="unitUnreadable ||'):
            self.assertIn(frag, VUE, f"前端缺少 {frag}")

    def test_write_buttons_are_locked_when_the_unit_cannot_be_read(self):
        for frag in (':disabled="unitUnreadable" @click="restoreDefaultTpl"',
                     ':disabled="!!acting || unitSaving || unitUnreadable"',
                     ':disabled="!unitInfo?.managed || unitSaving || unitUnreadable"'):
            self.assertIn(frag, VUE, f"写入口缺 unitUnreadable 闸门：{frag}")

    def test_unreadable_and_missing_each_get_their_own_notice(self):
        block = VUE.split("tab === 'unit'")[1].split("tab === 'ssl'")[0]
        self.assertIn("v-if=\"unitUnreadable\"", block)
        self.assertIn("读不到", block)
        self.assertIn("file_reason", block)
        self.assertIn("v-else-if=\"unitMissing\"", block)
        self.assertIn("盘上还没有", block)


def _vue_code(text):
    """去掉注释行的模板文本：本批把旧写法原样留在说明注释里当证据，守卫不许报注释。"""
    kept = [ln for ln in text.splitlines()
            if not ln.strip().startswith(("//", "/*", "*"))]
    return "\n".join(kept)


if __name__ == "__main__":
    unittest.main()
