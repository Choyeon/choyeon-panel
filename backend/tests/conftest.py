"""把整轮 pytest 的临时目录收进一个会话根目录，跑完当场删掉。

三十几个测试模块都在 **import 期** 就 `tempfile.mkdtemp(prefix="cp_…")` 当 CP_DATA_DIR，
用例里还有 per-test 的 mkdtemp：单独看每一步都合理，累起来却没有回收器——⑤J 交付时
在生产机上实测到 3913 个目录 / 117MB（跑了约 30 轮），而面板的正式数据目录是
`<安装目录>/data`，这些纯属测试残留。

做法：本轮所有 tempfile 调用都落到同一个会话目录，会话结束整块删除。
`tempfile.tempdir` 必须一起改：它一旦被任何先于 conftest 运行的代码解析过就会被缓存，
那时只设 TMPDIR 是无效的。外部已经指定 TMPDIR 时不去动它（可能是 CI 的共享目录），
改为只删本轮新出现的那些 `cp_*` 条目，避免把别人的临时文件一起带走。

另外无条件改写 `CP_DATA_DIR`：`app.config` 的默认值就是 `<安装目录>/data`，而各测试模块
用的是 `os.environ.setdefault(...)`——只要调用 pytest 时环境里带着 CP_DATA_DIR（哪怕是
`cd backend && CP_DATA_DIR=… .venv/bin/python -m pytest` 这种为线上探针写下的习惯用法），
setdefault 就全部失效，整轮用例会直接读写生产库。2026-10-11 就这么把线上 panel.db 写坏了
一次（audit 795→11、settings 丢键、token_epoch 被抬高把真人会话吊销），靠
`/root/backups/panel/manual/panel-pre-update-*.tar.gz` 才恢复。这里不接受任何外部值。
"""

import os
import shutil
import tempfile

_SESSION_ROOT = None
_SNAPSHOT = None
if os.environ.get("TMPDIR"):
    _WATCHED = os.environ["TMPDIR"]
    _SNAPSHOT = set(os.listdir(_WATCHED))
else:
    _SESSION_ROOT = tempfile.mkdtemp(prefix="cp_session_")
    _WATCHED = _SESSION_ROOT
    os.environ["TMPDIR"] = _SESSION_ROOT
    tempfile.tempdir = _SESSION_ROOT

_external_data_dir = os.environ.pop("CP_DATA_DIR", "")
os.environ["CP_DATA_DIR"] = tempfile.mkdtemp(prefix="cp_data_guard_")
if _external_data_dir:
    # print 而不是 raise：静默改写更危险，运行的人必须看得见"我传的路径被忽略了"。
    print(
        "[conftest] 忽略外部 CP_DATA_DIR=%r，已改用会话临时目录；"
        "要跑线上探针请用 .venv/bin/python <script>，不要 -m pytest。" % (_external_data_dir,)
    )


def pytest_sessionfinish(session, exitstatus):
    if _SESSION_ROOT:
        shutil.rmtree(_SESSION_ROOT, ignore_errors=True)
        return
    before = _SNAPSHOT or set()
    try:
        now = set(os.listdir(_WATCHED))
    except OSError:
        return
    for name in now - before:
        if not name.startswith("cp_"):
            continue
        p = os.path.join(_WATCHED, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
