import datetime as _dt
import errno
import os
import shutil
import stat
from pathlib import Path

from .. import config

ROOTS = config.FILE_ROOTS
MAX_FILE_BYTES = config.MAX_UPLOAD_BYTES


def iso_ms(epoch: float) -> str:
    ms = round(epoch * 1000)
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.UTC)
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
    # 默认目录必须由服务层给出：写死 /root/www 时，改过 CP_FILE_ROOTS 的部署
    # 一进文件页就落在白名单外，报「路径超出允许范围」，用户以为是 bug。
    # roots 一并回给前端，页面据此渲染快捷入口，不再自己猜目录。
    if not ROOTS:
        raise RuntimeError("未配置 CP_FILE_ROOTS，文件管理不可用")
    abs_ = safe_path(p or ROOTS[0])
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
    entries.sort(key=lambda e: (0 if e["isDir"] else 1, str(e["name"]).lower()))
    return {"path": abs_, "roots": list(ROOTS), "entries": entries}


def read_text(p: str) -> dict:
    abs_ = safe_path(p)
    if not os.path.isfile(abs_):
        # 目录也会走到这里：不判的话 Path.read_text 抛 IsADirectoryError，
        # 前端只会看到 "[Errno 21] Is a directory"。
        raise RuntimeError("不是文件，无法预览")
    st = os.stat(abs_)
    if st.st_size > MAX_FILE_BYTES:
        raise RuntimeError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB，请用下载")
    return {"path": abs_, "content": Path(abs_).read_text(encoding="utf8", errors="replace")}


def write_text(p: str, content: str | bytes) -> dict:
    """按字节落盘。

    上传侧原先把整份文件 `file.text()` 转成字符串再 PUT，非 UTF-8 内容（图片、zip、
    数据库文件）会在解码时被替换成 U+FFFD，接口照样返回 200 —— 文件已被静默毁掉。
    """
    abs_ = safe_path(p)
    data = content if isinstance(content, bytes) else content.encode("utf8")
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError(f"内容超过 {MAX_FILE_BYTES // 1024 // 1024}MB，拒绝写入")
    Path(os.path.dirname(abs_)).mkdir(parents=True, exist_ok=True)
    # O_NOFOLLOW：safe_path 是按"当前"的符号链接解析的，检查与写入之间换一个链接
    # 就能把内容写到白名单外（TOCTOU）。拒绝跟随最后一层链接，让写入只落在真文件上。
    try:
        fd = os.open(abs_, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise RuntimeError("目标是符号链接，为避免写到别处已拒绝") from None
        raise RuntimeError(f"写入失败：{e.strerror or e}") from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return {"path": abs_, "size": len(data)}


def fs_action(action: str, p: str, p2: str | None = None) -> dict:
    abs_ = safe_path(p)
    if action == "mkdir":
        try:
            os.makedirs(abs_, exist_ok=True)
        except FileExistsError:
            # exist_ok=True 只容忍"已存在且是目录"；同名普通文件仍抛错，
            # 转成可读信息，否则前端拿到 "[Errno 17] File exists: '/root/www/x'"。
            raise RuntimeError("同名文件已存在，无法创建目录") from None
    elif action == "delete":
        if abs_ in ROOTS:
            raise RuntimeError("禁止删除根目录")
        if not os.path.lexists(abs_):
            raise RuntimeError("路径不存在")
        if os.path.isdir(abs_) and not os.path.islink(abs_):
            # 不能用 ignore_errors=True：实测删除挂载点内的目录时它吞掉
            # "Device or resource busy"，接口照样返回 ok，用户以为已删。
            # 逐项删除并收集失败项，最后校验目录是否真的消失。
            leftovers = []
            for name in os.listdir(abs_):
                try:
                    fp = os.path.join(abs_, name)
                    if os.path.isdir(fp) and not os.path.islink(fp):
                        shutil.rmtree(fp)
                    else:
                        os.unlink(fp)
                except OSError as e:
                    leftovers.append(f"{name}（{e.strerror}）")
            try:
                os.rmdir(abs_)
            except OSError as e:
                leftovers.append(f"目录本身（{e.strerror}）")
            if leftovers:
                raise RuntimeError("删除未完成：" + "、".join(leftovers[:5]))
        else:
            try:
                os.unlink(abs_)
            except OSError as e:
                raise RuntimeError(f"删除失败：{e.strerror or e}") from e
    elif action == "rename":
        to = safe_path(p2 or "")
        if not os.path.lexists(abs_):
            raise RuntimeError("原路径不存在")
        if os.path.exists(to):
            raise RuntimeError("目标已存在")
        if os.path.dirname(abs_) != os.path.dirname(to):
            raise RuntimeError("暂不支持跨目录移动")
        try:
            os.rename(abs_, to)
        except OSError as e:
            raise RuntimeError(f"重命名失败：{e.strerror or e}") from e
    else:
        raise RuntimeError("未知操作")
    return {"ok": True}
