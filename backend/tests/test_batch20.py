"""批次⑳：纳管（外部 unit）应用的环境变量必须真正落地并生效。

线上实测的原始缺陷：choyeon-video 是 unit_override 纳管应用，在「环境变量」页把
ADMIN_PASSWORD 改成新值，界面提示"外部 unit：unit 与环境变量文件不由面板写入，本次只改了
记录"，磁盘上 `/root/www/choyeon-video/.panel.env` 压根不存在（面板从来没写过它），
外部 unit 里也没有 EnvironmentFile 那行。于是这个页面对纳管应用等于只读，
而"改了没反应"看起来像 systemd 的玄学问题。

同一件事的第二个入口：自定义 unit_template 漏写 EnvironmentFile 时，面板写的 .panel.env
永远没人读，界面却照样报"已保存并重启"。两者用同一个机制收口——systemd 的 <unit>.d/
drop-in，它不改写 unit 本体，删掉面板那份文件就恢复原样。
"""

import asyncio
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import apps_service as A


def _fake_run(calls):
    async def run(_cmd, argv=None, **_kw):
        calls.append(list(argv or []))
        return {"code": 0, "out": "", "err": ""}

    return run


class Case(unittest.TestCase):
    def setUp(self):
        self.units = tempfile.mkdtemp(prefix="cp_units20_")
        self.appdir = tempfile.mkdtemp(prefix="cp_app20_")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.addCleanup(shutil.rmtree, self.appdir, True)
        for name, value in (("UNIT_DIR", self.units),):
            p = mock.patch.object(A, name, value)
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
            "env": "[]", "unit_override": "their.service", "unit_template": None,
        }
        return {**base, **kw}

    def write_unit(self, text="[Service]\nExecStart=/bin/true\n", name="their.service"):
        Path(self.units, name).write_text(text, encoding="utf-8")

    def drop_in(self, name="their.service"):
        return Path(self.units, f"{name}.d", A.ENV_DROP_IN_NAME)

    def reloads(self):
        return [c for c in self.systemctl if c[:1] == ["daemon-reload"]]


class TestEnvFile(Case):
    def test_body_escapes_and_drops_illegal_names(self):
        a = self.app(env=json.dumps([
            {"k": "OK_KEY", "v": 'a"b\\c'},
            {"k": "9BAD", "v": "x"},
            {"k": "with space", "v": "y"},
        ]))
        body = A.env_body(a)
        self.assertEqual(body.splitlines(), [r'OK_KEY="a\"b\\c"'], body)
        self.assertNotIn("9BAD", body)

    def test_write_reports_written_then_unchanged(self):
        a = self.app(env=json.dumps([{"k": "A", "v": "1"}]))
        self.assertEqual(A.write_env_file(a), "written")
        target = Path(self.appdir) / A.ENV_FILE_NAME
        self.assertEqual(target.read_text().strip(), 'A="1"')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600, "env 里常是密钥，不许同机可读")
        self.assertEqual(A.write_env_file(a), "unchanged", "内容没变必须报 unchanged，否则每次保存都白重启一次")

    def test_missing_dir_is_reported_not_created(self):
        a = self.app(path=str(Path(self.appdir) / "not-yet"))
        self.assertEqual(A.write_env_file(a), "no-dir")
        self.assertFalse(Path(a["path"]).exists(), "部署前的目录不能由这里创建（git clone 会拒绝非空目录）")

    def test_reapplies_mode_when_content_unchanged(self):
        a = self.app(env=json.dumps([{"k": "A", "v": "1"}]))
        A.write_env_file(a)
        os.chmod(Path(self.appdir) / A.ENV_FILE_NAME, 0o644)
        self.assertEqual(A.write_env_file(a), "unchanged")
        self.assertEqual(stat.S_IMODE((Path(self.appdir) / A.ENV_FILE_NAME).stat().st_mode), 0o600)

    def test_default_template_points_at_the_same_file_the_writer_writes(self):
        """模板与写入器必须指向同一个名字：不一致时界面说"已写入"，systemd 读的是另一份。"""
        self.assertIn(
            f"EnvironmentFile=-{self.appdir}/{A.ENV_FILE_NAME}",
            A.render_unit(self.app(unit_override=None)),
        )


class TestUnitReadsEnvFile(Case):
    def test_true_when_unit_body_declares_it(self):
        a = self.app(unit_override=None)
        self.write_unit(A.render_unit(a), name=A.unit_name(a))
        self.assertTrue(A.unit_reads_env_file(a))

    def test_false_for_commented_line(self):
        self.write_unit(f"[Service]\n# EnvironmentFile=-{self.appdir}/{A.ENV_FILE_NAME}\n")
        self.assertFalse(A.unit_reads_env_file(self.app()), "注释掉的行 systemd 不会加载")

    def test_false_for_another_apps_env(self):
        self.write_unit(f"[Service]\nEnvironmentFile=-/root/www/other/{A.ENV_FILE_NAME}\n")
        self.assertFalse(A.unit_reads_env_file(self.app()), "同名不同目录：换了安装目录后旧引用不算生效")

    def test_true_when_only_the_drop_in_has_it(self):
        self.write_unit()
        a = self.app()
        self.assertFalse(A.unit_reads_env_file(a))
        d = Path(self.units, "their.service.d")
        d.mkdir()
        (d / "other.conf").write_text(f"[Service]\nEnvironmentFile=-{self.appdir}/{A.ENV_FILE_NAME}\n")
        self.assertTrue(A.unit_reads_env_file(a))


class TestEnsureEnvLink(Case):
    def test_creates_drop_in_once(self):
        a = self.app(env=json.dumps([{"k": "A", "v": "1"}]))
        self.write_unit()
        self.assertEqual(asyncio.run(A.ensure_env_link(a)), "written")
        text = self.drop_in().read_text()
        self.assertIn(f"EnvironmentFile=-{self.appdir}/{A.ENV_FILE_NAME}", text)
        self.assertTrue(text.startswith("#"), "drop-in 要说明是谁写的、怎么撤")
        self.assertEqual(len(self.reloads()), 1)
        self.assertEqual(stat.S_IMODE(self.drop_in().stat().st_mode), 0o600)
        self.assertEqual(asyncio.run(A.ensure_env_link(a)), "unchanged", "重复保存不该再写一遍、再 reload 一次")
        self.assertEqual(len(self.reloads()), 1)

    def test_nothing_when_unit_already_reads_it(self):
        a = self.app(unit_override=None)
        self.write_unit(A.render_unit(a), name=A.unit_name(a))
        self.assertEqual(asyncio.run(A.ensure_env_link(a)), "unchanged")
        self.assertFalse(Path(self.units, f"{A.unit_name(a)}.d").exists())
        self.assertEqual(self.systemctl, [])

    def test_no_unit_file_leaves_no_orphan_drop_in(self):
        self.assertEqual(asyncio.run(A.ensure_env_link(self.app())), "no-unit")
        self.assertFalse(Path(self.units, "their.service.d").exists(), "没有 unit 还写 drop-in 就是孤儿配置")

    def test_invalid_unit_name_is_refused(self):
        a = self.app(unit_override="../../etc/evil.service")
        self.assertEqual(asyncio.run(A.ensure_env_link(a)), "invalid-unit")
        self.assertEqual(list(Path(self.units).glob("*")), [])

    def test_remove_cleans_file_and_dir_but_not_foreign_drop_in(self):
        a = self.app()
        self.write_unit()
        asyncio.run(A.ensure_env_link(a))
        other = Path(self.units, "their.service.d", "vendor.conf")
        other.write_text("[Service]\nEnvironment=FOO=1\n")
        self.assertTrue(asyncio.run(A.remove_env_link(a)))
        self.assertFalse(self.drop_in().exists())
        self.assertTrue(other.is_file(), "别人的 drop-in 不能跟着删")
        self.assertTrue(other.parent.is_dir())
        self.assertFalse(asyncio.run(A.remove_env_link(a)), "没有面板写的文件时不该报成删过")


class TestApplyEditedExternal(Case):
    """apply_edited 的纳管分支：环境变量写、挂、重启；unit 本体与 nginx 不碰。"""

    def _apply(self, old, new, active=True):
        self.restarts = []

        async def _action(unit, verb):
            self.restarts.append((unit, verb))

        async def _active(_unit):
            return active

        async def _port(_unit, **_kw):
            return new["port"]

        async def _stable(_unit):
            return None

        with mock.patch.object(A, "is_active", _active), \
             mock.patch.object(A, "service_action", _action), \
             mock.patch.object(A, "detect_port", _port), \
             mock.patch.object(A, "_wait_stable", _stable), \
             mock.patch.object(A, "UNIT_SAMPLE_SEC", 0.01), \
             mock.patch.object(A.nginx_ops, "apply_vhost"), \
             mock.patch.object(A.nginx_ops, "remove_vhost"):
            return asyncio.run(A.apply_edited(old, new))



    def test_first_save_writes_env_links_and_restarts(self):
        self.write_unit()
        old = self.app(env="[]")
        new = {**old, "env": json.dumps([{"k": "ADMIN_PASSWORD", "v": "201314"}])}
        out = self._apply(old, new)
        self.assertEqual((Path(self.appdir) / A.ENV_FILE_NAME).read_text().strip(), 'ADMIN_PASSWORD="201314"')
        self.assertTrue(self.drop_in().is_file())
        self.assertEqual(self.restarts, [("their.service", "restart")], "在跑的服务必须重启，否则新值进不了进程")
        self.assertTrue(out["applied"])
        self.assertTrue(out["restarted"])
        self.assertEqual(out["note"], "")

    def test_identical_save_does_not_restart(self):
        self.write_unit()
        env = json.dumps([{"k": "A", "v": "1"}])
        old = self.app(env=env)
        A.write_env_file(old)
        asyncio.run(A.ensure_env_link(old))
        self.systemctl.clear()
        new = {**old, "domain": "x.example.com"}
        out = self._apply(old, new)
        self.assertEqual(self.restarts, [], "内容一个字没变还重启，等于每次点保存都抖一下线上服务")
        self.assertTrue(out["applied"])
        self.assertIn("环境变量没有变化", out["note"])


    def test_stopped_service_is_not_started(self):
        self.write_unit()
        old = self.app(env="[]")
        new = {**old, "env": json.dumps([{"k": "A", "v": "1"}])}
        out = self._apply(old, new, active=False)
        self.assertEqual(self.restarts, [])
        self.assertIn("下一次启动", out["note"])
        self.assertEqual((Path(self.appdir) / A.ENV_FILE_NAME).read_text().strip(), 'A="1"', "文件照样要落盘")

    def test_unit_scoped_fields_are_still_record_only_and_said_so(self):
        self.write_unit()
        old = self.app()
        new = {**old, "start_cmd": "npm run start --port 4000", "env": json.dumps([{"k": "A", "v": "1"}])}
        out = self._apply(old, new)
        self.assertIn("启动命令", out["note"])
        self.assertIn("面板不写外部 unit", out["note"])
        self.assertTrue(out["restarted"], "同一句提醒里不能把已经生效的环境变量也说成没生效")

    def test_missing_app_dir_says_env_waits_for_deploy(self):
        self.write_unit()
        old = self.app()
        new = {**old, "path": str(Path(self.appdir) / "nope"), "env": json.dumps([{"k": "A", "v": "1"}])}
        out = self._apply(old, new)
        self.assertFalse(out["applied"])
        self.assertIn("下一次部署", out["note"])
        self.assertEqual(self.restarts, [])

    def test_switch_to_managed_drops_the_old_link(self):
        self.write_unit()
        old = self.app(env=json.dumps([{"k": "A", "v": "1"}]))
        asyncio.run(A.ensure_env_link(old))
        self.assertTrue(self.drop_in().is_file())
        new = {**old, "unit_override": None, "name": "vid2", "env": json.dumps([{"k": "A", "v": "2"}])}
        out = self._apply(old, new)
        self.assertFalse(self.drop_in().exists(), "unit 名换了，旧 drop-in 没人读，留着就是孤儿")
        self.assertTrue(Path(self.units, "panel-vid2.service").is_file())
        self.assertTrue(out["applied"])


class TestDeployAndDeletePaths(Case):
    def test_deploy_step_also_links_the_env_file(self):
        """部署是环境变量的另一个入口：那里不补 drop-in，写好的 .panel.env 一样没人读。"""
        src = Path(A.__file__).read_text(encoding="utf-8")
        window = src[src.index("==> 写入环境变量与 systemd unit"):]
        self.assertIn("write_env_file(app)", window[:400])
        self.assertIn("ensure_env_link(app)", window[:400])

    def test_delete_app_removes_the_panel_drop_in(self):
        self.write_unit()
        a = self.app(env=json.dumps([{"k": "A", "v": "1"}]))
        asyncio.run(A.ensure_env_link(a))
        self.assertTrue(self.drop_in().is_file())
        with mock.patch.object(A, "get_app", lambda _i: a), \
             mock.patch.object(A, "_ensure_no_deployment"), \
             mock.patch.object(A, "remove_unit"), \
             mock.patch.object(A.dbm, "execute"), \
             mock.patch("app.services.alerts_service.forget_app"):
            asyncio.run(A.delete_app(1, False))
        self.assertFalse(self.drop_in().exists())


class TestSourceContract(Case):
    def test_external_branch_still_writes_and_links(self):

        src = Path(A.__file__).read_text(encoding="utf-8")
        body = src[src.index("async def _apply_external_unit"):src.index("async def apply_edited")]
        for call in ("write_env_file(new_app)", "ensure_env_link(new_app)", "restart_and_verify("):
            self.assertIn(call, body, f"纳管分支少了 {call}，环境变量就又只进记录了")

    def test_env_file_path_is_always_built_from_the_constant(self):
        """拼路径的地方只许用 ENV_FILE_NAME：写死一份，模板与写入器就可能各指一个文件。"""
        src = Path(A.__file__).read_text(encoding="utf-8")
        bad = [ln.strip() for ln in src.splitlines() if "/.panel.env" in ln]
        self.assertEqual(bad, [], bad)
        self.assertEqual(A.ENV_FILE_NAME, ".panel.env")




if __name__ == "__main__":
    unittest.main()
