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
- `env_drift`：unit 文件存在性闸门只是噪声过滤，stat 不动就跳过这一行，不炸整轮
  （批次72 又把「跳过」细化成三态：读不动要单独数进 unknown，不许算成查过且干净）。
- 批次71 补的第四条 `suggest_unit`：它挂在 GET /api/apps（列表）与详情页两条读路径上，
  原来也是裸 `is_file()`——同一类缺陷在这个文件里的漏网之鱼，读不动如实「不建议」。

真实权限用例 fork 出子进程 `setgid/setuid` 成 nobody、在子进程里就地调 `env_state`
复现 CI 现场。不用 `su`：CI 里 root 起 su 会被 PAM 挡成 Authentication failure（nobody
没有口令），第一版就是这么红的；也不用 fork 后 execv 重启解释器：生产机上 `.venv` 在
`/root` 下（0700），降权后的子进程连解释器都执行不了，第二版就是这么红的。模块在 fork 前
已 import 完整，子进程只跑读路径。复现不了（无法降权、以非 root 跑）就如实 skip，
不拿 mock 冒充实测。
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

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_b70p_"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 第一版在这里无条件改写 os.environ["CP_DATA_DIR"]（为了让 su/execv 起的降权子进程进得来），
# 副作用是把 discover 排序在自己之后的所有模块的会话数据目录一起带进自己 addCleanup 的
# rmtree——run 全量时 batch33 的 flock 与 alerts 全被扫掉，单跑怎么试都绿。fork 版子进程
# 在进程内调用，根本不需要环境对 nobody 友好，所以这条覆写整个删掉，只留模块自己的 setdefault。

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


class UnreadableUnderRealPermissionTests(unittest.TestCase):
    """CI 反馈不回来：造一个 stat 不动的安装目录，fork 出降权到 nobody 的子进程实测。

    不用 `su nobody`：CI 里 root 起 su 会被 PAM 挡成 Authentication failure（nobody 没有
    口令），第一版就是这么在 CI 上红的。第二版 fork 后 execv 重启解释器，在生产机上又红了：
    `.venv/bin/python` 在 /root（0700）下面，降权后的进程连解释器路径都进不去。所以降权后
    在子进程里就地调用——apps_service 在 fork 前已 import 完整，跑的还是那条真实读路径。
    非 root 的机器上父目录压根造不出来，如实 skip 而不是拿 mock 冒充实测。
    """

    def test_env_state_returns_unreadable_instead_of_raising(self):
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            self.skipTest("需要 root 才能造『父目录 700、内层 000』再降权的现场")
        try:
            import pwd
            pw = pwd.getpwnam("nobody")
            uid, gid = pw.pw_uid, pw.pw_gid
        except (ImportError, KeyError):
            self.skipTest("查不到 nobody 用户，复现不了非 root 现场")
        base = tempfile.mkdtemp(prefix="cp_b70p_open_")
        self.addCleanup(shutil.rmtree, base, True)
        outer = Path(base, "outer")
        outer.mkdir()
        os.chmod(base, 0o755)
        demo = Path(outer, "demo")
        demo.mkdir()
        Path(demo, ".panel.env").write_text('A="1"\n', encoding="utf-8")
        os.chmod(demo, 0o000)
        os.chmod(outer, 0o700)
        self.addCleanup(os.chmod, str(outer), 0o755)
        app = _app(path=str(demo), env=_env([("A", "1")]))
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
                    res = "STATE=" + apps_service.env_state(app)["disk"]
                except Exception as e:  # noqa: BLE001 — 崩了也要把异常名送回父进程断言
                    res = "RAISED=" + type(e).__name__
                os.write(w, res.encode("utf-8") + b"\n")
            except BaseException:  # noqa: BLE001 — fork 子进程的兜底：任何失败都要带着 traceback 进管道
                import traceback
                traceback.print_exc()
            finally:
                os._exit(9)
        os.close(w)
        with os.fdopen(r, "r", encoding="utf-8", errors="replace") as f:
            out = f.read()
        os.waitpid(pid, 0)
        os.chmod(str(outer), 0o755)  # 先放开再 rmtree
        self.assertIn("STATE=unreadable", out,
                      f"不崩才是本补丁的要点，子进程输出：{out[:600]}")
        self.assertNotIn("RAISED=PermissionError", out)


class StatErrorMappingTests(unittest.TestCase):
    """stat 抛 OSError 的四条路：都落进诚实档，没有一个向上冒。"""

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
        # 批次72：不炸整轮之外还得说实话——这一行是「没查成」，不是「查过且干净」
        self.assertEqual(got["unknown"], 1)

    def test_suggest_unit_with_denied_stat_returns_none_instead_of_500(self):
        # 批次71：同一个缺陷的漏网之鱼——suggest_unit 挂在 GET /api/apps（列表）和
        # GET /api/apps/{id}（详情）两条读路径上，裸 is_file() 一抛就是整页 500。
        # 生产机 root 下 /etc/systemd/system 读得到，踩不到；CI 之外还有跑在容器/降权
        # 服务里的场景，读不动就如实「不建议」，不猜也不崩。
        app = _app()
        with mock.patch.object(apps_service.Path, "is_file",
                               side_effect=PermissionError(13, "Permission denied")):
            self.assertIsNone(apps_service.suggest_unit(app, True))
        # 诚实档的另一头：读得动、文件真在，建议照旧给出来——catch 不许把功能吞了
        units = tempfile.mkdtemp(prefix="cp_b70p_sug_")
        self.addCleanup(shutil.rmtree, units, True)
        Path(units, "demo.service").write_text("[Unit]\n", encoding="utf-8")
        with mock.patch.object(apps_service, "UNIT_DIR", units):
            self.assertEqual(apps_service.suggest_unit(app, True), "demo.service")


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
        # 源码契约：裸 stat 的四条路都必须在 try 里（CI 的 PermissionError 全靠这几处兜住）
        self.assertIn("parent_ok = None", SERVICE_PY)
        self.assertRegex(SERVICE_PY, r"try:\s*\n\s*if target\.is_file\(\):")
        # 批次72 把这道闸门改成三态（None = 确认不了），契约跟着改：仍必须在 try 里
        self.assertIn("unit_present = None", SERVICE_PY)
        fn = SERVICE_PY.split("def unit_reads_env_file")[1].split("def env_state")[0]
        self.assertIn("files.extend(_env_drop_in_dir(unit).glob", fn)
        fn = SERVICE_PY.split("def suggest_unit")[1].split("def render_unit_from")[0]
        self.assertRegex(fn, r"try:\s*\n\s*present = \(Path\(UNIT_DIR\) / cand\)\.is_file\(\)")


if __name__ == "__main__":
    unittest.main()
