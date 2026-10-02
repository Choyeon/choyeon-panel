import datetime as _dt
import os
import shutil
import stat
from pathlib import Path

from .. import config

ROOTS = config.FILE_ROOTS


def iso_ms(epoch: float) -> str:
    ms = round(epoch * 1000)
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ms % 1000:03d}Z"


def safe_path(p: str) -> str:
    if not p:
        raise RuntimeError("非法路径")
    abs_ = os.path.abspath(p)
    probe = abs_
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    real = os.path.realpath(probe)
    rest = abs_[len(probe) :]
    full = os.path.abspath(real + rest)
    if not any(full == r or full.startswith(r + "/") for r in ROOTS):
        raise RuntimeError("路径超出允许范围")
    if ".." in full.split("/"):
        raise RuntimeError("非法路径")
    return full


def list_dir(p: str) -> dict:
    abs_ = safe_path(p or "/root/www")
    if not os.path.isdir(abs_):
        raise RuntimeError("不是目录")
    entries = []
    for name in os.listdir(abs_):
        is_dir = False
        size = 0
        mtime = ""
        try:
            st = os.stat(os.path.join(abs_, name))
            is_dir = stat.S_ISDIR(st.st_mode)
            size = st.st_size
            mtime = iso_ms(st.st_mtime)
        except OSError:
            pass
        entries.append({"name": name, "isDir": is_dir, "size": size, "mtime": mtime})
    entries.sort(key=lambda e: (0 if e["isDir"] else 1, e["name"].lower()))
    return {"path": abs_, "entries": entries}


def read_text(p: str) -> dict:
    abs_ = safe_path(p)
    st = os.stat(abs_)
    if st.st_size > 1024 * 1024:
        raise RuntimeError("文件超过 1MB，请用下载")
    return {"path": abs_, "content": Path(abs_).read_text(encoding="utf8", errors="replace")}


def write_text(p: str, content: str) -> dict:
    abs_ = safe_path(p)
    Path(os.path.dirname(abs_)).mkdir(parents=True, exist_ok=True)
    Path(abs_).write_text(content, encoding="utf8")
    return {"path": abs_, "size": len(content.encode("utf8"))}


def fs_action(action: str, p: str, p2: str | None = None) -> dict:
    abs_ = safe_path(p)
    if action == "mkdir":
        os.makedirs(abs_, exist_ok=True)
    elif action == "delete":
        if abs_ in ROOTS:
            raise RuntimeError("禁止删除根目录")
        if os.path.isdir(abs_):
            shutil.rmtree(abs_, ignore_errors=True)
        else:
            os.unlink(abs_)
    elif action == "rename":
        to = safe_path(p2 or "")
        if os.path.exists(to):
            raise RuntimeError("目标已存在")
        os.rename(abs_, to)
    else:
        raise RuntimeError("未知操作")
    return {"ok": True}
