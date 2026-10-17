#!/usr/bin/env python
"""后端测试入口：把 `conftest.py` 为 pytest 做的两件事也带给 unittest。

`unittest discover` 不会读 conftest.py，所以直接跑它时缺了两道保护：

① **忽略外部 `CP_DATA_DIR`**。各测试模块用的是 `os.environ.setdefault(...)`，而
   `app.config` 的默认值就是 `<安装目录>/data`：只要调用时环境里带着 `CP_DATA_DIR`
   （例如 source 过 `.env`），setdefault 全部失效，整轮用例会直接读写生产库。
   2026-10-11 就这么把线上 panel.db 写坏过一次，靠手工备份才恢复。
② **临时目录归拢后整块删除**。三十几个模块在 import 期各自 `mkdtemp`，用例里还有
   per-test 的 mkdtemp，单看都合理，累起来却没有回收器：每跑一轮就在 /tmp 留下一批
   `cp_*` 目录。这里把它们收进同一个会话目录，退出时删掉。

③ **单条用例挂死有上限**。到点把还卡着的线程栈打出来再退出，而不是让 CI 的 step
   超时替我们猜「哪一条没跑完」。

用法（`-v` 等参数原样转给 unittest）：

    cd backend && .venv/bin/python run_tests.py -v
"""

import atexit
import faulthandler
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


# 最慢的单条用例离这个上限还差两个数量级（整套 2959 条在 2 核机上约 170 秒跑完），
# 所以撞上它基本等于「这里有死循环」，不是「这台机器慢」。设 0 关闭。
HANG_SECONDS = float(os.environ.get("CP_TEST_HANG_SECONDS", "240") or 0)


class _HangWatchdogResult(unittest.TextTestResult):
    """每条用例开始前重新定时、结束就撤销；到点仍未撤销则 dump 全部线程栈并退出。

    为什么要它：2026-10-17 的 run 38984972944 把「单元测试」25 分钟跑到上限被杀，
    日志最后一行只有 ``...test_html_revalidates_and_assets_are_immutable ...``，
    既看不出卡在哪个文件哪一行，也分不清是没跑完还是在空转。加大 step 上限只是把
    静默僵尸换成有名字的失败，这条把名字直接打在日志里。
    """

    def startTest(self, test):
        super().startTest(test)
        if HANG_SECONDS > 0:
            faulthandler.dump_traceback_later(HANG_SECONDS, exit=True)

    def stopTest(self, test):
        if HANG_SECONDS > 0:
            faulthandler.cancel_dump_traceback_later()
        super().stopTest(test)


class _Runner(unittest.TextTestRunner):
    def __init__(self, **kw):
        kw.setdefault("resultclass", _HangWatchdogResult)
        super().__init__(**kw)


def sweep_stale_sessions(max_age_sec: int = 86_400) -> int:
    """收掉上一轮没来得及回收的会话临时目录，返回真的删掉的个数。

    `atexit` 在 `os._exit()` 与 SIGKILL 下不执行：看门狗每抓一次挂死（exit=True 走
    `os._exit`）、每被 `timeout`/Ctrl-C 打断一次，就会在 /tmp 留一个 `cp_unittest_*`
    整树。整套跑完不到 3 分钟，所以 24 小时以外的绝不可能是还在跑的另一轮。
    """
    removed = 0
    for cand in Path(tempfile.gettempdir()).glob("cp_unittest_*"):
        try:
            if not cand.is_dir() or time.time() - cand.stat().st_mtime < max_age_sec:
                continue
            shutil.rmtree(cand, ignore_errors=True)
        except OSError:
            continue
        if not cand.exists():
            removed += 1
    return removed


def main() -> int:
    external_data_dir = os.environ.pop("CP_DATA_DIR", "")
    stale = sweep_stale_sessions()
    if stale:
        print(f"[run_tests] 回收了 {stale} 个上一轮遗留的会话临时目录", flush=True)
    session_root = tempfile.mkdtemp(prefix="cp_unittest_")
    os.environ["TMPDIR"] = session_root
    # tempfile 会缓存解析结果，必须一起改，否则后续 mkdtemp 仍然落在 /tmp 根下。
    tempfile.tempdir = session_root
    os.environ["CP_DATA_DIR"] = tempfile.mkdtemp(prefix="cp_data_guard_")
    if external_data_dir:
        # print 而不是 raise：静默改写更危险，运行的人必须看见"我传的路径被忽略了"。
        print(
            f"[run_tests] 忽略外部 CP_DATA_DIR={external_data_dir!r}，已改用会话临时目录；"
            "要跑线上探针请用 .venv/bin/python <script>，不要跑测试。",
            flush=True,
        )
    atexit.register(shutil.rmtree, session_root, True)

    sys.path.insert(0, str(HERE))
    argv = [sys.argv[0], "discover", "-s", "tests", *sys.argv[1:]]
    return unittest.main(argv=argv, module=None, testRunner=_Runner).exit()


if __name__ == "__main__":
    raise SystemExit(main())
