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

用法（`-v` 等参数原样转给 unittest）：

    cd backend && .venv/bin/python run_tests.py -v
"""

import atexit
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    external_data_dir = os.environ.pop("CP_DATA_DIR", "")
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
    return unittest.main(argv=argv, module=None).exit()


if __name__ == "__main__":
    raise SystemExit(main())
