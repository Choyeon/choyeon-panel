"""批次70 补丁：env_state 的探针在 stat 都不动的位置上要如实一档，而不是把详情接口炸掉。

现场：批次69 上线后 CI 两个后端 job 全红——`test_batch65` 喂给 `app_get` 的应用行是
`/root/www/demo`，GitHub runner 不是 root，`env_state` 第一下 `target.parent.is_dir()`
就 PermissionError，`GET /api/apps/{id}` 直接 500（生产两个批次69 commit 的 CI 也都红，
只是轮到批次70 push 上 main 才被发现）。面板本身以 root 跑，平时踩不到；但同一个函数
还被 doctor 的 `_check_app_env` 用，任何一处读不动都不该把整轮自检带崩。

修的是三处裸 stat：
- `env_state`：父目录 `is_dir()` 抛 OSError → `unreadable`（不是 missing——那会引导人点
  「重新写入」去覆盖一个看不见的东西；也不是 no-dir——那是"没部署过"，会被漂移闸门跳过）；
  `exists()/is_symlink()` 兜底同样 catch；
- `unit_reads_env_file`：drop-in 目录的 glob 迭代会抛，收进 try；
- `env_drift`：unit 文件存在性闸门只是噪声过滤，stat 不动就跳过这一行，不炸整轮。

真实权限用例在无 root 的机器上拿 `su nobody` 起子进程复现 CI 现场；复现不了（没有 nobody
用户、或以 root 跑）就如实 skip，不拿 mock 冒充实测。
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b70p_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import meta as meta_router  # noqa: E402
from app.services import apps_service  # noqa: E402

SRC = Path(__file__).resolve().parents[2]
SERVICE_PY = (SRC / "backend" / "app" / "services" / "apps_service.py").read_text(encoding="utf-8")


def _app(**kw):
    row = {"id": 1, "name": "demo", "unit_override": None, "port": 3100, "domain": None,
           "path": "/root/www/demo", "env": "[]", "type": "node", "start_cmd": "node server.js"}
    row.update(kw)
    return row


def _env(kvs):
    return json.dumps([{"k": k, "v": v} for k, v in kvs])


_SNIPPET = (
    "import sys, json\n"
    "sys.path.insert(0, {backend!r})\n"
    "from app.services import apps_service\n"
    "app = json.loads({app!r})\n"
    "try:\n"
    "    print('STATE=' + apps_service.env_state(app)['disk'])\n"
    "except Exception as e:\n"
    "    print('RAISED=' + type(e).__name__)\n"
)


class UnreadableUnderRealPermissionTests(unittest.TestCase):
    """CI 反馈不回来：在真文件系统上造一个 stat 不动的安装目录，拿无权限用户实测。"""

    def setUp(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root 无视 mode 0，这里造不出 PermissionError")
        outer = tempfile.mkdtemp(prefix="cp_b70p_open_")
        self.addCleanup(shutil.rmtree, outer, True)
        os.chmod(outer, 0o755)
        self.demo = Path(outer, "demo")
        self.demo.mkdir()
        Path(self.demo, ".panel.env").write_text('A="1"\n', encoding="utf-8")
        os.chmod(self.demo, 0o000)
        self.addCleanup(os.chmod, str(self.demo), 0o755)
        self.app = _app(path=str(self.demo), env=_env([("A", "1")]))

    def _run_as_nobody(self):
        for user in ("nobody", "65534"):
            probe = subprocess.run(["id", user], capture_output=True, text=True)
            if probe.returncode != 0:
                continue
            snippet = _SNIPPET.format(
                backend=str(Path(__file__).resolve().parents[1]),
                app=json.dumps(self.app),
            )
            return subprocess.run(
                ["su", "-s", "/bin/sh", user, "-c", snippet],
                capture_output=True, text=True, timeout=60,
            )
        self.skipTest("这台机器上没有 nobody/65534 用户，复现不了非 root 现场")

    def test_env_state_returns_unreadable_instead_of_raising(self):
        r = self._run_as_nobody()
        self.assertEqual(r.returncode, 0, f"子进程没跑起来：{r.stderr[:400]}")
        self.assertIn("STATE=unreadable", r.stdout,
                      f"不崩才是本补丁的要点，实际输出：{r.stdout[:200]}{r.stderr[:400]}")
        self.assertNotIn("RAISED=PermissionError", r.stdout)


class StatErrorMappingTests(unittest.TestCase):
    """stat 抛 OSError 的三条路：都落进诚实档，没有一个向上冒。"""

    def test_parent_stat_denied_is_unreadable_not_missing(self):
        # CI 原景：path=/root/www/demo，非 root 连 /root 都进不去
        app = _app(env=_env([("A", "1")]))
        with mock.patch.object(apps_service.Path, "is_dir",
                               side_effect=PermissionError(13, "Permission denied")):
            st = apps_service.env_state(app)
        self.assertEqual(st["disk"], "unreadable")
        self.assertEqual(st["vars"], 1)

    def test_child_stat_denied_also_unreadable(self):
        # 目录 mode 000：父目录 stat 得动（is_dir True），但里面的文件 stat 不动——
        # is_file()/exists()/is_symlink() 每一个都可能抛，整段兜住统一落 unreadable
        app = _app(env=_env([("A", "1")]))
        with mock.patch.object(apps_service.Path, "is_dir", return_value=True), \
                mock.patch.object(apps_service.Path, "is_file",
                                  side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(apps_service.env_state(app)["disk"], "unreadable")
        with mock.patch.object(apps_service.Path, "is_dir", return_value=True), \
                mock.patch.object(apps_service.Path, "is_file", return_value=False), \
                mock.patch.object(apps_service.Path, "exists",
                                  side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(apps_service.Path, "is_symlink", return_value=False):
            self.assertEqual(apps_service.env_state(app)["disk"], "unreadable")

    def test_drop_in_glob_denied_does_not_crash_reference_check(self):
        units = tempfile.mkdtemp(prefix="cp_b70p_units_")
        self.addCleanup(shutil.rmtree, units, True)
        app = _app(path=units, env=_env([("A", "1")]))
        with mock.patch.object(apps_service, "UNIT_DIR", "/proc/1/nope"), \
                mock.patch.object(apps_service.Path, "glob",
                                  side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(apps_service.unit_reads_env_file(app))

    def test_drift_gate_with_unreadable_unit_dir_skips_instead_of_exploding(self):
        rows = [_app(path="/root/www/demo", env=_env([("A", "1")]))]
        with mock.patch.object(apps_service, "UNIT_DIR", "/proc/1/nope"), \
                mock.patch.object(apps_service.Path, "is_file",
                                  side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(apps_service.dbm, "query", lambda *a, **k: rows):
            got = apps_service.env_drift()
        self.assertEqual(got["drift"], [])
        self.assertEqual(got["total"], 1)


class HonestRowsStillDriftTests(unittest.TestCase):
    """unreadable 照常进漂移与判词——读不到还装绿灯才是自检最坏的样子。"""

    def test_unreadable_is_listed_and_named(self):
        drift = {"total": 1, "drift": [{"id": 1, "name": "demo", "disk": "unreadable",
                                        "vars": 2, "unitReads": True}]}
        with mock.patch.object(apps_service, "env_drift", lambda: drift):
            out = asyncio.run(meta_router._check_app_env())
        self.assertEqual(out["status"], "fail")
        self.assertIn("demo", out["detail"])
        self.assertIn("读不了", out["detail"])

    def test_source_contract_guard_rows(self):
        # 源码契约：裸 stat 的三条路都必须在 try 里（CI 的 PermissionError 全靠这三处兜住）
        self.assertIn("parent_ok = None", SERVICE_PY)
        self.assertRegex(SERVICE_PY, r"try:\s*\n\s*if target\.is_file\(\):")
        self.assertIn("unit_present = False", SERVICE_PY)
        fn = SERVICE_PY.split("def unit_reads_env_file")[1].split("def env_state")[0]
        self.assertIn("files.extend(_env_drop_in_dir(unit).glob", fn)


if __name__ == "__main__":
    unittest.main()
