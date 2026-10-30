"""批次72：`app-env` 自检在 unit 目录读不动时不许报绿灯。

现场（生产机实测复现）：把 apps_service 的 stat 全上总闸（is_file/is_dir/exists/
is_symlink/glob/iterdir/read_text 一律 PermissionError），`_check_app_env` 返回的是
`pass` + 「5 个纳管应用的环境变量落盘一致」——它一个应用都没读。根因在 `env_drift`
的噪声闸门：批次70 补丁为了「不炸整轮」把 `except OSError` 折叠成 `unit_present = False`，
而 False 在下游的意思是「这台机器上没有这个 unit 文件（还没部署过，跳过不算漂移）」，
于是"确认不了"被翻译成"确认没有"，drift 为空 → 绿灯。整轮自检不炸了，但它开始说谎。

修法：闸门给三态。`None`（确认不了）单独数进返回值里的 `unknown`，
`_check_app_env` 无漂移但有 unknown 时降为 warn 并点名个数与目录，有漂移时也在判词里
补一句「这几个也没查成」——读不动永远不许被说成"一致"。

真实权限用例走两条路：非 root 的机器上 `chmod 000` 一个目录就能让 owner 自己 stat 不动
（Linux 目录缺 x 连里面的文件都 stat 不到），当场进程内实测；root 的机器（生产机）有
DAC_OVERRIDE 拦不住，就 fork 降权到 nobody 再跑同一条读路径，沿用批次70 补丁2 的形状
（不 su、不 execv）。两条都不满足才 skip。
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b72_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import meta as meta_router  # noqa: E402
from app.services import apps_service  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
SERVICE_PY = (SRC / "backend" / "app" / "services" / "apps_service.py").read_text(encoding="utf-8")
META_PY = (SRC / "backend" / "app" / "routers" / "meta.py").read_text(encoding="utf-8")

# patch 之前先抓住原始实现：混合场景里"读得动的那一行"要走真 stat，不能递归回替身
_ORIG_IS_FILE = Path.is_file
_DENY = PermissionError(13, "Permission denied")
_STAT_METHODS = ("is_file", "is_dir", "exists", "is_symlink", "glob", "iterdir", "read_text")

_ROWS = [{"id": 1, "name": "demo", "path": "/tmp/cp_b72_never_created", "env": "[]",
          "unit_override": None}]
_ALL_ROWS = [
    {"id": 1, "name": "a", "path": "/root/www/a", "env": '[{"k":"A","v":"1"}]',
     "unit_override": None},
    {"id": 2, "name": "b", "path": "/root/www/b", "env": '[{"k":"B","v":"2"}]',
     "unit_override": "b.service"},
]


def _denied_drift(rows, units_dir="/proc/1/nope"):
    """UNIT_DIR 与所有路径探测一起拒绝时跑一遍 env_drift（生产机踩过的那一档）。"""
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(apps_service, "UNIT_DIR", units_dir))
        stack.enter_context(mock.patch.object(apps_service.dbm, "query",
                                              lambda *a, **k: list(rows)))
        for m in _STAT_METHODS:
            stack.enter_context(mock.patch.object(apps_service.Path, m, side_effect=_DENY))
        return apps_service.env_drift()


def _drift_at(units_dir: str, rows) -> dict:
    """在指定 UNIT_DIR 上真跑一遍 env_drift（只换掉数据库那一份输入）。"""
    with mock.patch.object(apps_service, "UNIT_DIR", units_dir), \
            mock.patch.object(apps_service.dbm, "query", lambda *a, **k: rows):
        return apps_service.env_drift()


def _fmt(d: dict) -> str:
    return f"TOTAL={d['total']} UNKNOWN={d['unknown']} DRIFT={len(d['drift'])}"


def _gate_denier(deny_fragment: str):
    """is_file 的替身（配 `autospec=True` 用）：路径含 deny_fragment 就抛，其余走真实现。

    必须 autospec：直接 patch 一个普通 MagicMock 时，不同解释器传进来的参数形状不一样
    （3.13 实测 0 个参数、3.11/3.12 走描述符协议带上实例），写死或靠 args[0] 都会
    「本机绿、CI 红」。autospec 按原函数签名建替身，实例永远是第一个位置参数。
    """
    def effect(self_p):
        if deny_fragment in str(self_p):
            raise _DENY
        return _ORIG_IS_FILE(self_p)
    return effect


class RealPermissionTests(unittest.TestCase):
    """闸门那条 OSError 分支：用真权限造现场，不拿 mock 冒充实测。"""

    def test_unreadable_unit_dir_yields_unknown_not_a_green_pass(self):
        base = tempfile.mkdtemp(prefix="cp_b72_open_")
        self.addCleanup(shutil.rmtree, base, True)
        units = Path(base, "units")
        units.mkdir()
        os.chmod(base, 0o755)
        os.chmod(units, 0o000)
        self.addCleanup(os.chmod, str(units), 0o755)
        want = "TOTAL=1 UNKNOWN=1 DRIFT=0"
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # root 有 DAC_OVERRIDE，000 目录拦不住它：降到 nobody 才复现得出来
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
                    try:
                        res = _fmt(_drift_at(str(units), _ROWS))
                    except Exception as e:  # noqa: BLE001 — 崩了也要把异常名送回父进程断言
                        res = "RAISED=" + type(e).__name__
                    os.write(w, res.encode("utf-8") + b"\n")
                except BaseException:  # noqa: BLE001 — fork 子进程兜底：失败带 traceback 进管道
                    import traceback
                    traceback.print_exc()
                finally:
                    os._exit(9)
            os.close(w)
            with os.fdopen(r, "r", encoding="utf-8", errors="replace") as f:
                out = f.read()
            os.waitpid(pid, 0)
            os.chmod(str(units), 0o755)  # 先放开再 rmtree
        else:
            try:
                out = _fmt(_drift_at(str(units), _ROWS))
            except Exception as e:  # noqa: BLE001
                out = "RAISED=" + type(e).__name__
        self.assertIn(want, out, f"闸门该数出 unknown，实测输出：{out[:600]}")
        self.assertNotIn("RAISED=", out)


class TotalDenialTests(unittest.TestCase):
    """整台机器读不动时，这一项必须从绿灯变成「没查成」。"""

    def test_env_drift_counts_rows_it_could_not_decide(self):
        d = _denied_drift(_ALL_ROWS)
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["drift"], [])
        self.assertEqual(d["unknown"], 2, "两行都没读动就得是「两个没查成」，不是「两个都干净」")

    def test_doctor_item_is_not_green_when_nothing_was_read(self):
        drift = {"total": 5, "drift": [], "unknown": 5}
        with mock.patch.object(apps_service, "env_drift", lambda: drift):
            out = asyncio.run(meta_router._check_app_env())
        self.assertEqual(out["status"], "warn")
        self.assertIn("5", out["detail"])
        self.assertIn("没查成", out["detail"])

    def test_end_to_end_denial_never_reports_consistent(self):
        # 真跑服务层 + 真判词：这一批的缺陷正是这条链跑通后报 pass
        d = _denied_drift(_ALL_ROWS)
        with mock.patch.object(apps_service, "env_drift", lambda: d):
            out = asyncio.run(meta_router._check_app_env())
        self.assertNotEqual(out["status"], "pass")
        self.assertNotIn("落盘一致", out["detail"])


class GateThreeStatesTests(unittest.TestCase):
    """confirmed present / confirmed absent / 确认不了，三态各走各的路。"""

    def setUp(self):
        self.units = tempfile.mkdtemp(prefix="cp_b72_units_")
        self.addCleanup(shutil.rmtree, self.units, True)
        self.dir = tempfile.mkdtemp(prefix="cp_b72_app_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        Path(self.units, "panel-demo.service").write_text(
            f"EnvironmentFile=-{self.dir}/.panel.env\n", encoding="utf-8")
        Path(self.dir, ".panel.env").write_text('A="1"\n', encoding="utf-8")
        self.good = {"id": 1, "name": "demo", "path": self.dir,
                     "env": '[{"k":"A","v":"1"}]', "unit_override": None}

    def test_confirmed_absent_unit_is_still_silently_skipped(self):
        # 还没部署过：没有进程在说谎，列进失败项只会让人开始忽略这一项（批次69 的原意不动）
        rows = [{"id": 2, "name": "never", "path": self.dir, "env": "[]", "unit_override": None}]
        got = _drift_at(self.units, rows)
        self.assertEqual(got["drift"], [])
        self.assertEqual(got["unknown"], 0, "读得动、确认没有，不该混进「没查成」")

    def test_clean_row_and_denied_row_only_count_the_denied(self):
        bad = {"id": 3, "name": "bad", "path": self.dir, "env": "[]", "unit_override": None}
        with mock.patch.object(apps_service, "UNIT_DIR", self.units), \
                mock.patch.object(apps_service.dbm, "query",
                                  lambda *a, **k: [self.good, bad]), \
                mock.patch.object(apps_service.Path, "is_file", autospec=True,
                                  side_effect=_gate_denier("panel-bad")):
            got = apps_service.env_drift()
        self.assertEqual(got["total"], 2)
        self.assertEqual(got["drift"], [], "干净那一行照常查完并且没问题")
        self.assertEqual(got["unknown"], 1, "只有 unit stat 不动的那一行进 unknown")

    def test_unknown_does_not_swallow_a_real_drift(self):
        # 一行真有漂移 + 一行读不动：fail 里两件事都要说出来，不能只报绿灯的反面
        bad = {"id": 4, "name": "ghost", "path": self.dir, "env": "[]", "unit_override": None}
        drift_rows = [{**self.good, "env": '[{"k":"A","v":"9"}]'}, bad]

        with mock.patch.object(apps_service, "UNIT_DIR", self.units), \
                mock.patch.object(apps_service.dbm, "query", lambda *a, **k: drift_rows), \
                mock.patch.object(apps_service.Path, "is_file", autospec=True,
                                  side_effect=_gate_denier("panel-ghost")):
            got = apps_service.env_drift()
        self.assertEqual([x["name"] for x in got["drift"]], ["demo"])
        self.assertEqual(got["unknown"], 1)
        with mock.patch.object(apps_service, "env_drift", lambda: got):
            out = asyncio.run(meta_router._check_app_env())
        self.assertEqual(out["status"], "fail")
        self.assertIn("demo", out["detail"])
        self.assertIn("另有 1 个", out["detail"])


class VerdictTests(unittest.TestCase):
    def test_fail_verdict_names_the_unreadable_ones_too(self):
        drift = {"total": 3, "drift": [{"id": 1, "name": "demo", "disk": "differs",
                                        "vars": 2, "unitReads": True}], "unknown": 2}
        with mock.patch.object(apps_service, "env_drift", lambda: drift):
            out = asyncio.run(meta_router._check_app_env())
        self.assertEqual(out["status"], "fail")
        self.assertIn("另有 2 个", out["detail"])
        self.assertIn("没查成", out["detail"])

    def test_green_verdict_untouched_when_nothing_unknown(self):
        # 回归护栏：正常机器上（unknown=0）判词与状态一个字都不变
        with mock.patch.object(apps_service, "env_drift",
                               lambda: {"total": 4, "drift": [], "unknown": 0}):
            out = asyncio.run(meta_router._check_app_env())
        self.assertEqual(out["status"], "pass")
        self.assertIn("4 个纳管应用", out["detail"])

    def test_legacy_shape_without_unknown_key_still_passes(self):
        # 老替身/老调用方没有 unknown 字段：读成 0，不许 KeyError
        with mock.patch.object(apps_service, "env_drift", lambda: {"total": 4, "drift": []}):
            out = asyncio.run(meta_router._check_app_env())
        self.assertEqual(out["status"], "pass")


class SourceContractTests(unittest.TestCase):
    def test_gate_is_three_state_and_verdict_reads_unknown(self):
        fn = SERVICE_PY.split("def env_drift")[1].split("\nasync def ")[0]
        self.assertIn("unit_present = None", fn)
        self.assertIn("unknown += 1", fn)
        self.assertIn('"unknown": unknown', fn)
        self.assertNotIn("unit_present = False", fn,
                         "折叠成 False 就是「读不动 = 没部署过」，本批修的正是这个")
        self.assertIn('d.get("unknown", 0)', META_PY)


if __name__ == "__main__":
    unittest.main()
