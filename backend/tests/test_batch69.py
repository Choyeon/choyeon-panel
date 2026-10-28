"""批次69：面板显示的环境变量与进程真正读到的那一份必须能对上账。

线上现场（2026-10-26 报障，管理员截图问"我在面板设的密码为什么登录报密码错误"）：
`choyeon-video`（app id 41 之外的另一个纳管应用）在 2026-10-18 被换成一次全新的
`git clone`，那一步把 `<安装目录>/.panel.env` 删掉了；而面板数据库里这条记录最后一次保存是
10-14，之后没人再点过保存。unit 那行写的是 `EnvironmentFile=-<目录>/.panel.env`，开头的 `-`
表示"文件不存在也不报错"，于是 systemd 静默跳过、进程用的是 `/etc/choyeon-video.env` 里另一份
`ADMIN_PASSWORD`（九个字符），界面却照常显示数据库这一份（六个字符）。照着界面输口令只会
「密码错误」，而这台机器上没有任何一处说过这两份已经分家——四个纳管应用都可能是这种状态。

两半缺陷：
1. **没有读者**。`write_env_file` 只在自己要写的时候顺手比一下内容，且把返回值丢掉；
   `unit_reads_env_file` 只在补 drop-in 时用。全程没有任何代码比较过"数据库那份"与
   "磁盘那份"，漂移在结构上不可见。
2. **界面只会说"已保存"**。环境变量页把 unit 与 .panel.env 的机制解释了一遍，却没有
   一次核实；`保存` 成功回执也不区分"写进磁盘"与"进程读得到"。

修法：只读的 `env_state(app)` 逐字节对比两份，给出 disk 五态、有效键数（不含值）和
`unitReads`；`GET /api/apps/{id}` 以 `envState` 带回（viewer 也能看，因为它不含值）；详情页
据此摆横幅，修复按钮复用既有 PATCH（它会重写 .panel.env、需要时补 drop-in、服务在跑就重启），
不另起会改值的写接口；自检加第 22 项点名漂移的应用。
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b69_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import apps as apps_router  # noqa: E402
from app.routers import meta as meta_router  # noqa: E402
from app.services import apps_service, systemd_ops  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
DETAIL_VUE = (SRC / "web" / "src" / "views" / "AppDetail.vue").read_text(encoding="utf-8")
APPS_PY = (SRC / "backend" / "app" / "routers" / "apps.py").read_text(encoding="utf-8")
META_PY = (SRC / "backend" / "app" / "routers" / "meta.py").read_text(encoding="utf-8")


def _app(**kw):
    row = {"id": 1, "name": "demo", "unit_override": None, "port": 3100, "domain": None,
           "path": "/root/www/demo", "env": "[]", "type": "node", "start_cmd": "node server.js"}
    row.update(kw)
    return row


def _env(kvs):
    import json
    return json.dumps([{"k": k, "v": v} for k, v in kvs])


class EnvStateTests(unittest.TestCase):
    """五态判定：比较的是"面板会写成什么样"，不是"看起来像不像"。"""

    def setUp(self):
        self.units = tempfile.mkdtemp(prefix="cp_b69_units_")
        self.dir = tempfile.mkdtemp(prefix="cp_b69_app_")
        self.p = mock.patch.object(apps_service, "UNIT_DIR", self.units)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.app = _app(path=self.dir, env=_env([("ADMIN_PASSWORD", "hunter2")]))
        # 把 unit 本体摆好：漂移判定的前提是"本机确实有个 unit 在按这份文件起进程"
        Path(self.units, "panel-demo.service").write_text(
            f"[Service]\nEnvironmentFile=-{self.dir}/.panel.env\n", encoding="utf-8")

    def _write_disk(self, text):
        Path(self.dir, ".panel.env").write_text(text, encoding="utf-8")

    def test_match_when_disk_equals_db(self):
        self._write_disk('ADMIN_PASSWORD="hunter2"\n')
        st = apps_service.env_state(self.app)
        self.assertEqual(st["disk"], "match")
        self.assertEqual(st["vars"], 1)
        self.assertTrue(st["unitReads"])

    def test_missing_is_drift_not_empty(self):
        # A/B 反证：文件根本没被创建过 ≠ 这个应用没有变量
        st = apps_service.env_state(self.app)
        self.assertEqual(st["disk"], "missing")
        self.assertEqual(st["vars"], 1)

    def test_no_vars_and_no_file_is_consistent(self):
        # 两边都空是"没有需要同步的东西"，不是漂移
        app = _app(path=self.dir, env="[]")
        self.assertEqual(apps_service.env_state(app)["disk"], "match")

    def test_differs_when_content_drifted(self):
        self._write_disk('ADMIN_PASSWORD="stale-value"\n')
        self.assertEqual(apps_service.env_state(self.app)["disk"], "differs")

    def test_trailing_newline_matters(self):
        # write_env_file 写的是 body + "\n"；少一个换行就是 differs，不是 match
        self._write_disk('ADMIN_PASSWORD="hunter2"')
        self.assertEqual(apps_service.env_state(self.app)["disk"], "differs")

    def test_unreadable_is_its_own_state(self):
        # 是个目录、读不了：不能报 missing 然后引导人去"重新写入"——那等于覆盖一个看不见的东西
        Path(self.dir, ".panel.env").mkdir()
        self.assertEqual(apps_service.env_state(self.app)["disk"], "unreadable")

    def test_no_dir_when_install_path_gone(self):
        app = _app(path=str(Path(self.dir, "not-created-yet")), env=_env([("A", "b")]))
        st = apps_service.env_state(app)
        self.assertEqual(st["disk"], "no-dir")
        self.assertEqual(st["vars"], 1)

    def test_invalid_keys_do_not_count(self):
        # env_body 按 fullmatch 过滤键名：写不进文件的键也不该在判词里被算成"有 N 个变量"
        app = _app(path=self.dir, env=_env([("1BAD", "x"), ("ok", "y"), ("A\\nB", "z")]))
        self.assertEqual(apps_service.env_state(app)["vars"], 1)

    def test_escape_counts_as_same_value(self):
        # 值里带引号/反斜杠时面板写的是转义后的形态，比较必须拿同一份 body 去比
        app = _app(path=self.dir, env=_env([("P", 'a"b\\c')]))
        self._write_disk(apps_service.env_body(app) + "\n")
        self.assertEqual(apps_service.env_state(app)["disk"], "match")

    def test_commented_environment_file_is_not_a_reference(self):
        self._write_disk('ADMIN_PASSWORD="hunter2"\n')
        Path(self.units, "panel-demo.service").write_text(
            f"# EnvironmentFile=-{self.dir}/.panel.env\n", encoding="utf-8")
        self.assertFalse(apps_service.env_state(self.app)["unitReads"])

    def test_drop_in_reference_counts(self):
        self._write_disk('ADMIN_PASSWORD="hunter2"\n')
        Path(self.units, "panel-demo.service").write_text("[Service]\n", encoding="utf-8")
        d = Path(self.units, "panel-demo.service.d")
        d.mkdir()
        Path(d, "panel-env.conf").write_text(
            f"[Service]\nEnvironmentFile=-{self.dir}/.panel.env\n", encoding="utf-8")
        self.assertTrue(apps_service.env_state(self.app)["unitReads"])

    def test_reference_to_an_old_install_dir_is_not_a_reference(self):
        self._write_disk('ADMIN_PASSWORD="hunter2"\n')
        Path(self.units, "panel-demo.service").write_text(
            f"EnvironmentFile=-{self.dir}/../other/.panel.env\n", encoding="utf-8")
        self.assertFalse(apps_service.env_state(self.app)["unitReads"])


class EnvDriftTests(unittest.TestCase):
    """自检的过滤条件：该报的都报，不该报的别吵。"""

    def setUp(self):
        self.units = tempfile.mkdtemp(prefix="cp_b69_units_")
        self.dir = tempfile.mkdtemp(prefix="cp_b69_app_")
        self.p = mock.patch.object(apps_service, "UNIT_DIR", self.units)
        self.p.start()
        self.addCleanup(self.p.stop)
        self.app = _app(path=self.dir, env=_env([("A", "1"), ("B", "2")]))
        Path(self.units, "panel-demo.service").write_text(
            f"EnvironmentFile=-{self.dir}/.panel.env\n", encoding="utf-8")

    def _drift(self, rows=None):
        rows = [self.app] if rows is None else rows
        with mock.patch.object(apps_service.dbm, "query", lambda *a, **k: rows):
            return apps_service.env_drift()

    def test_clean_app_is_not_listed(self):
        Path(self.dir, ".panel.env").write_text('A="1"\nB="2"\n', encoding="utf-8")
        got = self._drift()
        self.assertEqual(got["total"], 1)
        self.assertEqual(got["drift"], [])

    def test_missing_file_is_listed(self):
        got = self._drift()
        self.assertEqual(got["drift"][0]["disk"], "missing")
        self.assertEqual(got["drift"][0]["vars"], 2)
        self.assertEqual(got["drift"][0]["name"], "demo")

    def test_differs_is_listed_even_when_unit_reads_it(self):
        Path(self.dir, ".panel.env").write_text("garbage\n", encoding="utf-8")
        self.assertEqual(self._drift()["drift"][0]["disk"], "differs")

    def test_vars_without_reference_is_the_other_half(self):
        # 磁盘那份一模一样，但没人引用它：文件写得再对也进不了进程
        Path(self.dir, ".panel.env").write_text('A="1"\nB="2"\n', encoding="utf-8")
        Path(self.units, "panel-demo.service").write_text("[Service]\n", encoding="utf-8")
        self.assertEqual(self._drift()["drift"][0]["disk"], "match")

    def test_app_without_unit_file_is_not_noise(self):
        # 建了记录还没部署过：没有进程在说谎，列进失败项只会让人开始忽略这一项
        Path(self.units, "panel-demo.service").unlink()
        self.assertEqual(self._drift()["drift"], [])
        self.assertEqual(self._drift()["total"], 1, "总数仍要如实算上它")

    def test_empty_env_without_reference_is_fine(self):
        Path(self.units, "panel-demo.service").write_text("[Service]\n", encoding="utf-8")
        self.app["env"] = "[]"
        self.assertEqual(self._drift()["drift"], [])

    def test_override_app_is_judged_by_its_real_unit(self):
        """纳管外部 unit 的应用要按 unit_override 那个名字去问，所以那一列必须查出来。

        上线第一天实测：详情接口报 video 是 missing，doctor 却一声不响——SELECT 里没带
        unit_override，于是按 panel-video.service 去问，那个名字压根没有文件，它被
        "还没部署过"那道噪声闸门跳过了。漂移最要命的正是这类应用。
        """
        seen = {}

        def fake_query(stmt):
            seen["stmt"] = stmt
            return []

        with mock.patch.object(apps_service.dbm, "query", fake_query):
            apps_service.env_drift()
        self.assertIn("unit_override", seen["stmt"], "漏了这一列，纳管应用会从自检里整个消失")

        ext = _app(id=7, name="video", path=self.dir, unit_override="video.service",
                   env=_env([("A", "1")]))
        Path(self.units, "video.service").write_text(
            f"[Service]\nEnvironmentFile=-{self.dir}/.panel.env\n", encoding="utf-8")
        Path(self.dir, ".panel.env").write_text('A="999"\n', encoding="utf-8")
        got = self._drift([ext])
        self.assertEqual(got["drift"][0]["disk"], "differs")
        self.assertEqual(got["drift"][0]["name"], "video")


class DoctorItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_as_the_22nd_check(self):
        ids = [c[0] for c in meta_router._CHECKS]
        self.assertIn("app-env", ids)
        self.assertEqual(len(ids), 22, "自检项数与文档口径（AGENTS/README/SECURITY）必须一致")
        self.assertEqual(len(set(ids)), len(ids))
        self.assertNotIn("app-env", meta_router.FIXABLE,
                         "一键修复会重启别人的服务，这一项只能给可复制的指引")

    async def _check(self, drift):
        with mock.patch.object(apps_service, "env_drift", lambda: drift):
            return await meta_router._check_app_env()

    async def test_pass_when_nothing_drifted(self):
        got = await self._check({"total": 4, "drift": []})
        self.assertEqual(got["status"], "pass")
        self.assertIn("4 个纳管应用", got["detail"])

    async def test_fail_names_app_and_reason(self):
        got = await self._check({"total": 4, "drift": [
            {"id": 1, "name": "video", "disk": "missing", "vars": 3, "unitReads": True},
            {"id": 2, "name": "book", "disk": "match", "vars": 1, "unitReads": False},
        ]})
        self.assertEqual(got["status"], "fail")
        self.assertIn("video", got["detail"])
        self.assertIn("video"[:0] or "静默跳过", got["detail"])
        self.assertIn("book", got["detail"])
        self.assertIn("没有引用", got["detail"])
        self.assertIn("不是同一份", got["detail"])
        self.assertNotIn("3\"", got["detail"], "判词里不许出现任何值")

    async def test_no_dir_words_differ_from_missing(self):
        got = await self._check({"total": 1, "drift": [
            {"id": 3, "name": "new", "disk": "no-dir", "vars": 1, "unitReads": False}]})
        self.assertEqual(got["status"], "fail")
        self.assertIn("安装目录已经不存在", got["detail"])


class AppGetRouteTests(unittest.IsolatedAsyncioTestCase):
    def _req(self, role="admin"):
        return SimpleNamespace(state=SimpleNamespace(cp_role=role, cp_sub="t",
                                                     cp_exp=float("inf")), query_params={})

    async def _get(self, role):
        # 安装目录得真实存在，这一路才落在"目录在、文件没了"（missing）上；
        # 连目录都不在是 no-dir，是另一条分支（EnvStateTests 里单独钉）。
        install = tempfile.mkdtemp(prefix="cp_b69_get_")
        self.addCleanup(shutil.rmtree, install, True)
        app = _app(path=install, env=_env([("ADMIN_PASSWORD", "hunter2")]))
        state = systemd_ops.derive_state({"Id": "panel-demo.service", "ActiveState": "inactive",
                                         "LoadState": "loaded", "SubState": "dead", "MainPID": "0"})

        async def unit_state(_unit):
            return state

        with mock.patch.object(apps_service, "get_app", lambda _id: app), \
             mock.patch.object(apps_router.systemd_ops, "unit_state", unit_state), \
             mock.patch.object(apps_router.dbm, "query_one", lambda *a: None), \
             mock.patch.object(apps_router.dbm, "query", lambda *a: []):
            return await apps_router.app_get(1, self._req(role))

    async def test_detail_carries_env_state(self):
        got = await self._get("admin")
        self.assertEqual(got["envState"]["disk"], "missing")
        self.assertEqual(got["envState"]["vars"], 1)

    async def test_viewer_sees_the_drift_but_not_the_values(self):
        # 这条判词说的是"界面上的口令不能信"，把它藏在管理员视线里最容易坏事
        got = await self._get("viewer")
        self.assertEqual(got["env"], "[]")
        self.assertEqual(got["envState"]["vars"], 1)
        self.assertNotIn("hunter2", str(got))


class FrontendContractTests(unittest.TestCase):
    def test_detail_page_reads_env_state(self):
        self.assertIn('"envState": apps_service.env_state(a)', APPS_PY)
        self.assertRegex(DETAIL_VUE.replace(" ", ""), r'consts:any=app\.value\?\.envState;',
                         "横幅必须读后端那份对账结果，而不是在界面里再猜一次")

    def test_banner_offers_the_reuse_of_the_existing_save(self):
        self.assertRegex(DETAIL_VUE, r'<NAlert v-if="envDrift"[\s\S]*?@click="save"')
        self.assertIn("重新写入并重启", DETAIL_VUE)
        self.assertIn("重新写入", DETAIL_VUE)

    def test_banner_does_not_leak_values(self):
        body = DETAIL_VUE.split("const envDrift = computed")[1].split("});")[0]
        for needle in ("env.value", "envItems", ".v}", ".k}"):
            self.assertNotIn(needle, body, f"判词里不该出现 {needle}")

    def test_doctor_item_is_documented_at_the_same_count(self):
        self.assertEqual(META_PY.count('"app-env"'), 1)
        self.assertIn('("app-env", "应用环境变量", _check_app_env)', META_PY)


if __name__ == "__main__":
    unittest.main()
