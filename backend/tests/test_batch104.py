"""批次104：.panel.env 里面板记录之外的手写键，不许被任何一次保存静默删掉。

线上现场（这台机器 doctor 的「应用环境变量」项连续 fail 之后逐条查盘）：
`/root/www/choyeon-music/.panel.env` 里除了面板记录的那几个键，还有
`ADMIN_PASSWORD`、`ADMIN_SESSION_SECRET`；`/root/www/rosetta-backend/.panel.env` 里还有
`QQ_COOKIE`。这些是运维绕过面板手写的。而自检给这些 fail 开的修复入口是
「重新写入并重启」，实现是 `write_env_file(app)` —— 按数据库那份整份覆盖。
照做的那一次，管理员改的只是另一个变量，登录用的口令和会话密钥跟着没了：
`write_env_file` 返回 written，界面报"已保存"，note 里没有一个字提被删的是谁。
另一个方向同样缺账：`choyeon-video` 记录里有 1 个变量、盘上没有 .panel.env，
unit 那行带 `-` 前缀，systemd 静默跳过——这一条批次69 已单独成档（missing）。

修的四件事：
1. 加只读读者 `env_extra(app)` / `_env_diff`：把"盘上有、记录里没有"的键按名字点出来
   （只给名字，值一概不回），并新增 disk 档 `foreign`——面板那几行全对、只有外来键时
   这不是漂移，进程两个都读得到，判 fail 只会把真漂移淹在噪声里。
2. `write_env_file(app, keep_extra=True)`：默认原样保留那些行（含注释与畸形行），
   只有界面明确确认过的那颗按钮传 False。盘上那份读不动时拒绝覆盖——
   看不见不等于没有。
3. 三个写入口（PATCH 同步、外部 unit 纳管、部署）都在写入之前取好那份名单，
   把"保留了谁 / 删了谁"写进同一句 note 或部署日志，删的时候审计也留痕。
4. 自检与详情页：foreign 单独一档 warn 并点名，fix 不再引导人"重新写入"去删它们；
   界面给「保留并写入」（=原「重新写入」）和「删除这些变量并写入」两颗按钮，
   后者带二次确认，因为它不可撤销。
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b104_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import meta as meta_router  # noqa: E402
from app.services import apps_service  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
SERVICE_PY = (SRC / "backend" / "app" / "services" / "apps_service.py").read_text(encoding="utf-8")
APPS_PY = (SRC / "backend" / "app" / "routers" / "apps.py").read_text(encoding="utf-8")
META_PY = (SRC / "backend" / "app" / "routers" / "meta.py").read_text(encoding="utf-8")
DETAIL_VUE = (SRC / "web" / "src" / "views" / "AppDetail.vue").read_text(encoding="utf-8")


def _app(**kw):
    row = {"id": 1, "name": "demo", "unit_override": None, "port": 3100, "domain": None,
           "path": "/root/www/demo", "env": "[]", "type": "node", "start_cmd": "node server.js"}
    row.update(kw)
    return row


def _env(kvs):
    return json.dumps([{"k": k, "v": v} for k, v in kvs])


async def _fake_run(_cmd, _args=None, **_kw):
    # 纳管分支会真的补 drop-in 并 daemon-reload；测试跑在生产机上，
    # 这一层必须假掉，否则一次保存真的动了 systemd。
    return {"code": 0, "out": "", "err": ""}


class Case(unittest.TestCase):
    def setUp(self):
        self.units = tempfile.mkdtemp(prefix="cp_b104_units_")
        self.appdir = tempfile.mkdtemp(prefix="cp_b104_app_")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.addCleanup(shutil.rmtree, self.appdir, True)
        for name, value in (("UNIT_DIR", self.units), ("run", _fake_run)):
            p = mock.patch.object(apps_service, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.env_file = Path(self.appdir, apps_service.ENV_FILE_NAME)

    def app(self, env="[]", **kw):
        return _app(**{"path": self.appdir, "env": env, **kw})

    def write_unit(self, name="panel-demo.service", text=None):
        Path(self.units, name).write_text(
            text if text is not None else f"[Service]\nEnvironmentFile=-{self.appdir}/.panel.env\n",
            encoding="utf-8")

    def disk(self, text):
        self.env_file.write_text(text, encoding="utf-8")


class ReaderTests(Case):
    """「盘上有、记录里没有」必须看得见，而且只给名字。"""

    def test_foreign_is_its_own_state_not_differs(self):
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('PORT="3100"\nADMIN_PASSWORD="s3cr3t"\n')
        st = apps_service.env_state(a)
        self.assertEqual(st["disk"], "foreign")
        self.assertEqual(st["extra"], {"names": ["ADMIN_PASSWORD"], "total": 1, "readable": True})

    def test_panel_lines_wrong_is_still_differs_but_names_the_extras(self):
        # 两件事同时成立时以"面板那份不对"为准（要处置的是它），但外来键照样点名
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('PORT="8080"\nQQ_COOKIE="abc"\n')
        st = apps_service.env_state(a)
        self.assertEqual(st["disk"], "differs")
        self.assertEqual(st["extra"]["names"], ["QQ_COOKIE"])

    def test_values_never_leave_the_reader(self):
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('PORT="3100"\nADMIN_PASSWORD="do-not-leak-me"\n')
        blob = json.dumps([apps_service.env_state(a), apps_service.env_extra(a)])
        self.assertNotIn("do-not-leak-me", blob)
        self.assertIn("ADMIN_PASSWORD", blob)

    def test_truncation_reports_the_real_total(self):
        a = self.app(env="[]")
        self.disk("".join(f"K{i}=\"v\"\n" for i in range(11)))
        x = apps_service.env_extra(a)
        self.assertEqual(x["total"], 11)
        self.assertEqual(len(x["names"]), apps_service.ENV_EXTRA_PREVIEW)
        st = apps_service.env_state(a)
        # vars=0 时面板那份是空的：盘上全是外来键，disk 仍是 foreign（面板行一条都没缺）
        self.assertEqual(st["disk"], "foreign")
        self.assertGreater(st["extra"]["total"], len(st["extra"]["names"]))

    def test_comments_and_junk_are_not_variables(self):
        # 注释、空行、畸形行不进 foreign：它们不是变量，没有"被面板删掉"的风险要说。
        # 但文件确实不是面板会写出的那一版，所以 disk 仍是 differs（match 的定义是逐字节
        # 等于面板那份，批次69 锁过这条），判词只能说"与面板那份不同"，不能说变量不一致。
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('# 手工加的注释\nPORT="3100"\n\n   \n')
        self.assertEqual(apps_service.env_state(a)["disk"], "differs")
        self.assertEqual(apps_service.env_state(a)["extra"]["total"], 0)
        self.assertEqual(apps_service.env_extra(a)["total"], 0)

    def test_unreadable_disk_is_reported_as_not_readable(self):
        # 读不到 ≠ 盘上没有：env_extra 必须把这一件事单独说出来
        self.env_file.mkdir()
        a = self.app(env=_env([("PORT", "3100")]))
        self.assertFalse(apps_service.env_extra(a)["readable"])
        self.assertEqual(apps_service.env_state(a)["disk"], "unreadable")


class WriteTests(Case):
    """默认保留；只有关了才删。"""

    def test_default_save_keeps_hand_written_lines_verbatim(self):
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('PORT="2013"\nADMIN_PASSWORD="s3cr3t"\nADMIN_SESSION_SECRET="x"\n')
        self.assertEqual(apps_service.write_env_file(a), "written")
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn('ADMIN_PASSWORD="s3cr3t"', text)
        self.assertIn('ADMIN_SESSION_SECRET="x"', text)
        self.assertIn('PORT="3100"', text)
        self.assertNotIn('PORT="2013"', text, "面板记录的键要以记录为准，否则改了没生效")

    def test_second_save_is_unchanged_not_a_restart_trigger(self):
        # 保留逻辑必须幂等：每次保存都"rewrite 一遍"的话，note 会说已写入、服务也会被抖一下
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('ADMIN_PASSWORD="s3cr3t"\n')
        apps_service.write_env_file(a)
        self.assertEqual(apps_service.write_env_file(a), "unchanged")

    def test_keep_extra_false_is_the_only_destructive_path(self):
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('PORT="3100"\nADMIN_PASSWORD="s3cr3t"\n')
        self.assertEqual(apps_service.write_env_file(a, keep_extra=False), "written")
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), 'PORT="3100"\n')

    def test_blank_lines_and_comments_survive_a_write(self):
        a = self.app(env=_env([("PORT", "3100")]))
        self.disk('# 这份是运维手工维护的\nADMIN_COOKIE="c"\n')
        apps_service.write_env_file(a)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("# 这份是运维手工维护的", text)
        self.assertIn('ADMIN_COOKIE="c"', text)

    def test_unreadable_existing_file_blocks_the_write(self):
        # 覆盖一个自己读不到的文件等于删掉看不见的东西：宁可这次不写
        a = self.app(env=_env([("PORT", "3100")]))
        with mock.patch.object(apps_service, "_disk_env_text", side_effect=OSError("权限不够")), \
             mock.patch.object(apps_service, "write_secret_atomic") as wsa:
            with self.assertRaises(RuntimeError) as cm:
                apps_service.write_env_file(a)
        self.assertIn("没有覆盖它", str(cm.exception))
        wsa.assert_not_called()

    def test_no_dir_does_not_create_the_app_directory(self):
        a = self.app(path=str(Path(self.appdir, "not-yet")), env=_env([("A", "1")]))
        self.assertEqual(apps_service.write_env_file(a), "no-dir")
        self.assertFalse(Path(a["path"]).exists())


class SaveDisclosure:
    """两条写入口共用同一组断言：note 要点名，文件要真的留住（各自提供 _save）。"""

    def test_note_names_the_preserved_keys(self):
        a = self.app(env=_env([("PORT", "3100")]))
        out = self._save(a, disk_text='PORT="2013"\nADMIN_PASSWORD="s3cr3t"\nQQ_COOKIE="c"\n')
        self.assertIn("原样保留", out["note"])
        self.assertIn("ADMIN_PASSWORD", out["note"])
        self.assertIn("QQ_COOKIE", out["note"])
        self.assertIn('ADMIN_PASSWORD="s3cr3t"', self.env_file.read_text(encoding="utf-8"))

    def test_drop_note_names_what_it_deleted(self):
        a = self.app(env=_env([("PORT", "3100")]))
        out = self._save(a, drop=True, disk_text='PORT="3100"\nADMIN_PASSWORD="s3cr3t"\n')
        self.assertIn("删除", out["note"])
        self.assertIn("ADMIN_PASSWORD", out["note"])
        self.assertNotIn("ADMIN_PASSWORD", self.env_file.read_text(encoding="utf-8"))

    def test_clean_disk_note_says_nothing_about_extras(self):
        # 没有外来键时不许凭空多出半句话（原批次20 的契约：首次保存不该带上这句）
        a = self.app(env=_env([("PORT", "3100")]))
        out = self._save(a, disk_text="")
        self.assertNotIn("记录里没有", out["note"])
        self.assertNotIn("原样保留", out["note"])

    def test_truncated_note_still_reports_the_total(self):
        keys = "".join(f'K{i}="v"\n' for i in range(10))
        a = self.app(env=_env([("PORT", "3100")]))
        out = self._save(a, disk_text=f'PORT="3100"\n{keys}')
        self.assertIn("10 个", out["note"])


class ManagedSaveTests(SaveDisclosure, Case):
    """面板托管 unit 那一条保存路径。"""

    def _save(self, a, drop=False, disk_text=None):
        if disk_text is not None:
            self.disk(disk_text)
        self.write_unit()
        old = {**a, "env": "[]"}  # 改名与否、unit 字段差异都按"没动"来，只看环境变量这一段

        async def _noop(*_a, **_k):
            return None

        async def _inactive(_unit):
            return False

        with mock.patch.object(apps_service, "is_active", _inactive), \
             mock.patch.object(apps_service, "ensure_unit", _noop), \
             mock.patch.object(apps_service, "ensure_env_link", _noop), \
             mock.patch.object(apps_service, "restart_and_verify", _noop), \
             mock.patch.object(apps_service.nginx_ops, "apply_vhost", _noop):
            return asyncio.run(apps_service.apply_edited(old, a, drop))


class ExternalSaveTests(SaveDisclosure, Case):
    """纳管外部 unit 那一条：unit 本体不写，只写 .panel.env + drop-in，同样的删除风险。"""

    def _save(self, a, drop=False, disk_text=None):
        if disk_text is not None:
            self.disk(disk_text)
        # 外部 unit：本体里没有 EnvironmentFile 那行，面板靠 drop-in 挂载
        self.write_unit(name="their.service", text="[Service]\nExecStart=/bin/sleep 1\n")
        new = {**a, "unit_override": "their.service"}
        old = {**new, "env": "[]"}

        async def _noop(*_a, **_k):
            return None

        async def _inactive(_unit):
            return False

        with mock.patch.object(apps_service, "is_active", _inactive), \
             mock.patch.object(apps_service, "restart_and_verify", _noop):
            return asyncio.run(apps_service._apply_external_unit(old, new, False, drop))


class DoctorTests(Case):
    def _run(self, drift=None, extra=None, unknown=0):
        d = {"total": 5, "drift": drift or [], "extra": extra or [], "unknown": unknown}
        with mock.patch.object(apps_service, "env_drift", lambda: d):
            return asyncio.run(meta_router._check_app_env())

    def test_extra_only_is_warn_not_fail(self):
        r = self._run(extra=[{"name": "choyeon-music", "names": ["ADMIN_PASSWORD", "ADMIN_SESSION_SECRET"],
                              "total": 2, "readable": True}])
        self.assertEqual(r["status"], "warn")
        self.assertIn("ADMIN_PASSWORD", r["detail"])
        self.assertIn("原样保留", r["detail"])
        # 处置不能再引导人点「重新写入并重启」——那句话在过去等于教人删掉手写密钥
        self.assertNotIn("重新写入并重启", r["fix"])
        self.assertIn("详情 → 环境变量", r["fix"])

    def test_fail_fix_says_the_repair_preserves_extras(self):
        r = self._run(drift=[{"name": "choyeon-video", "disk": "missing", "vars": 1,
                              "unitReads": True, "extra": {"names": [], "total": 0, "readable": True}}])
        self.assertEqual(r["status"], "fail")
        self.assertIn("重新写入并重启", r["fix"])
        self.assertIn("原样保留", r["fix"])

    def test_foreign_row_inside_drift_is_not_silent_about_extras(self):
        # 面板行全对、只有 unit 没引用那一条线把它判进漂移：外来键照样要说
        r = self._run(drift=[{"name": "rosetta-backend", "disk": "foreign", "vars": 2, "unitReads": False,
                              "extra": {"names": ["QQ_COOKIE"], "total": 1, "readable": True}}])
        self.assertEqual(r["status"], "fail")
        self.assertIn("QQ_COOKIE", r["detail"])

    def test_clean_still_passes(self):
        self.assertEqual(self._run()["status"], "pass")

    def test_legacy_projection_without_extra_key_does_not_crash(self):
        # 老写法的 mock 里没有 extra 这一项（批次69/72 还在用），判词不该因此炸
        d = {"total": 2, "drift": [], "unknown": 0}
        with mock.patch.object(apps_service, "env_drift", lambda: d):
            self.assertEqual(asyncio.run(meta_router._check_app_env())["status"], "pass")


class SourceContractTests(Case):
    def test_patch_route_forwards_an_explicit_drop_only(self):
        self.assertIn('body.pop("envExtra", None) == "drop"', APPS_PY)
        self.assertIn("apply_edited(old, a, drop_extra)", APPS_PY)
        self.assertIn('(删了记录外的环境变量)', APPS_PY)

    def test_deploy_write_keeps_by_default_and_logs(self):
        window = SERVICE_PY[SERVICE_PY.index("==> 写入环境变量与 systemd unit"):]
        self.assertIn("write_env_file(app)", window[:400])
        self.assertIn("面板没有删除它们", window[:1600])

    def test_write_env_file_refuses_to_overwrite_what_it_cannot_read(self):
        body = SERVICE_PY.split("def write_env_file")[1].split("def _env_drop_in_dir")[0]
        self.assertIn("keep_extra: bool = True", body)
        self.assertIn("没有覆盖它", body)

    def test_ui_offers_both_choices_and_only_the_second_drops(self):
        block = DETAIL_VUE[DETAIL_VUE.index('<NAlert v-if="envDrift"'):]
        self.assertIn('save({ dropExtra: true })', block[:1800])
        self.assertIn("@click=\"save()\"", block[:1200])
        self.assertIn("删除这些变量并写入", block[:1800])
        self.assertIn('opts.dropExtra === true ? { envExtra: \'drop\' } : {}',
                      DETAIL_VUE[DETAIL_VUE.index("async function save("):])

    def test_env_tab_help_says_the_panel_does_not_delete_them(self):
        # 帮助句是与横幅同一个承诺：不点那颗按钮就不会删。写漏一句，用户就会以为保存会清场。
        self.assertIn("会原样保留", DETAIL_VUE)

    def test_differs_verdict_does_not_blame_the_variables(self):
        # 只有注释、空行或行尾换行之差时同名变量的值其实都对；判词说"这些变量不一致"就是假话。
        self.assertIn("不是面板按这", META_PY)
        self.assertIn("不是面板按这", DETAIL_VUE)


class EnvStateShapeTests(Case):
    def test_state_always_carries_extra(self):
        for a in (self.app(env=_env([("A", "1")])),
                  self.app(path=str(Path(self.appdir, "gone")), env=_env([("A", "1")])),
                  _app(path="", env="[]")):
            self.assertIn("extra", apps_service.env_state(a))

    def test_env_body_still_uses_the_single_renderer(self):
        # 只留一个渲染出口：另起一份拼接就会有两个"面板会写成什么样"的答案
        self.assertIn('return "\\n".join(env_lines(app))', SERVICE_PY)


if __name__ == "__main__":
    unittest.main()
