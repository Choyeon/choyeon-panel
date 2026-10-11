import contextlib
import datetime as _dt
import os
import shutil
import stat
import threading
from pathlib import Path

from .. import config

# 白名单要在源头归一成真实路径：CP_FILE_ROOTS 写 /root/www 而 www 本身是指向
# /data/www 的符号链接时，safe_path 解析出的真实路径永远不可能以 /root/www 开头，
# 文件页从第一个请求起就全报「路径超出允许范围」，看着像面板坏了。
ROOTS = list(dict.fromkeys(os.path.realpath(r) for r in config.FILE_ROOTS if r))
MAX_FILE_BYTES = config.MAX_UPLOAD_BYTES


def iso_ms(epoch: float) -> str:
    ms = round(epoch * 1000)
    dt = _dt.datetime.fromtimestamp(ms / 1000, tz=_dt.UTC)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{ms % 1000:03d}Z"


def _in_roots(p: str) -> bool:
    return any(p == r or p.startswith(r + "/") for r in ROOTS)


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
    if not _in_roots(full):
        raise RuntimeError("路径超出允许范围")
    return full


def safe_entry_path(p: str) -> str:
    """删除/改名用：判的是"条目本身"，不能按符号链接解析后的目标来算白名单。

    用 safe_path 删一个指向白名单外的链接（应用目录里指错路的 symlink 很常见）
    会永远得到「路径超出允许范围」——链接删不掉，用户只能进终端手工 rm，
    文件页的存在意义就没了。这里只要求父目录落在白名单内，末段名字保持原样，
    os.unlink/os.rename 本来就不跟随最后一层符号链接，删的就是链接自己。
    """
    if not p:
        raise RuntimeError("非法路径")
    abs_ = os.path.abspath(p)
    parent, base = os.path.split(abs_)
    if not base or base in {".", ".."}:
        raise RuntimeError("非法路径")
    if not _in_roots(os.path.realpath(parent)):
        raise RuntimeError("路径超出允许范围")
    return abs_


def is_root(p: str) -> bool:
    """p 是否就是某个白名单根目录（含通过符号链接写法传入）。"""
    return os.path.realpath(p) in ROOTS if p else False


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
    # 顺带回传额度：上传前先在前端拦一道，免得用户看着进度条走完才收到"超过 8MB"。
    return {"path": abs_, "roots": list(ROOTS), "maxUploadBytes": MAX_FILE_BYTES, "entries": entries}


def read_text(p: str) -> dict:
    abs_ = safe_path(p)
    if not os.path.isfile(abs_):
        # 目录也会走到这里：不判的话 Path.read_text 抛 IsADirectoryError，
        # 前端只会看到 "[Errno 21] Is a directory"。
        raise RuntimeError("不是文件，无法预览")
    st = os.stat(abs_)
    if st.st_size > MAX_FILE_BYTES:
        raise RuntimeError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB，请用下载")
    raw = Path(abs_).read_bytes()
    # NUL 在文本文件里几乎不出现，但 UTF-16 源码和多数二进制都有；按二进制处理，
    # 免得编辑器显示成「一串空字符」后又被当成合法内容保存回去。
    text: str | None = None
    if b"\x00" not in raw:
        try:
            # 严格解码：只有真的是 UTF-8 文本才交给编辑器。用 errors="replace" 的话，
            # 图片/zip/db 会被显示成一堆 U+FFFD，而「保存」就是把这份丢失的文本写回去，
            # 原文件不可恢复。判不出编码的二进制一律不下发内容，让前端走下载。
            text = raw.decode("utf8")
        except UnicodeDecodeError:
            text = None
    return {"path": abs_, "content": text if text is not None else "", "binary": text is None}


def _keep_owner(tmp: str, target: str) -> None:
    """把原文件的属主与权限搬到临时文件上，再让 replace 顶替。

    面板以 root 运行，新建文件默认 0600 root:root。直接 os.replace 而不搬属性的话，
    改一次应用目录里的文件就可能把 appuser 可读的 0644 变成谁都读不到的 0600，
    而应用是在下一次重启时才炸，排查起来完全对不上"我只是编辑了一个配置"。
    """
    try:
        st = os.stat(target)
    except OSError:
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o644)  # 新文件：给一个应用进程通常能读的权限
        return
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
    with contextlib.suppress(OSError):
        os.chown(tmp, st.st_uid, st.st_gid)


def _write_atomic(abs_: str, data: bytes) -> None:
    """同目录临时文件 + os.replace：要么旧内容，要么新内容，不会出现半截文件。

    O_TRUNC 就地改写时，写一半被断电/磁盘满/服务重启打断，目标文件就剩半截，
    应用目录里的 .env 当场变成不可解析的配置——比写失败更难解释。
    """
    parent = os.path.dirname(abs_) or "/"
    os.makedirs(parent, exist_ok=True)
    if os.path.islink(abs_):
        # 只可能来自竞态：safe_path 给出的是已解析的路径，正常不是链接。
        # 若检查与写入之间有人把它换成指向别处的链接，replace 会把链接顶掉
        # 并在原位置放一个真文件——内容没写到白名单外，但链接背后的语义没了。
        raise RuntimeError("目标是符号链接，为避免写到别处已拒绝")
    tmp = os.path.join(parent, f".{os.path.basename(abs_)}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            raise RuntimeError(f"写入失败：{e.strerror or e}") from None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            raise RuntimeError(f"写入失败：{e.strerror or e}") from None
        _keep_owner(tmp, abs_)
        try:
            os.replace(tmp, abs_)
        except OSError as e:
            raise RuntimeError(f"写入失败：{e.strerror or e}") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def write_text(p: str, content: str | bytes) -> dict:
    """按字节落盘。

    上传侧原先把整份文件 `file.text()` 转成字符串再 PUT，非 UTF-8 内容（图片、zip、
    数据库文件）会在解码时被替换成 U+FFFD，接口照样返回 200 —— 文件已被静默毁掉。
    """
    abs_ = safe_path(p)
    data = content if isinstance(content, bytes) else content.encode("utf8")
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError(f"内容超过 {MAX_FILE_BYTES // 1024 // 1024}MB，拒绝写入")
    _write_atomic(abs_, data)
    return {"path": abs_, "size": len(data)}


def safe_name(name: str) -> str:
    """上传文件名只留 basename，拒绝点号目录；点文件（.env/.gitignore）是合法的。

    一开始连 `.env` 都拒绝过：部署应用时点文件本来就要上传，一刀切只会逼用户
    先传成 `env` 再改名，而改名走的是另一条代码路径，出错信息也对不上。
    """
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    if not base or base in {".", ".."} or "/" in base or "\\" in base:
        raise RuntimeError(f"文件名不合法：{base[:40] or '空'}")
    if len(base) > 180:
        raise RuntimeError("文件名过长")
    return base


def save_upload(dir_path: str, filename: str, data: bytes, overwrite: bool = False) -> dict:
    """把 multipart 的一个字段写进白名单目录。

    和 write_text 一样拒绝跟随最后一层符号链接；文件名先净化，否则
    `../../etc/cron.d/x` 会绕过 safe_path 的目录检查。
    """
    target_dir = safe_path(dir_path)
    if not os.path.isdir(target_dir):
        raise RuntimeError("目标不是目录")
    abs_ = os.path.join(target_dir, safe_name(filename))
    if os.path.lexists(abs_) and not overwrite:
        # lexists：悬空链接也要算"已存在"，否则用户以为会覆盖，实际删掉了链接
        raise RuntimeError(f"同名文件已存在：{os.path.basename(abs_)}")
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError(f"单个文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限")
    existed = os.path.lexists(abs_)
    return write_text(abs_, data) | {"overwrote": existed}


def _check_member(name: str, real_dest: str) -> str:
    """压缩包成员名要能在目标目录内落地，否则整包先拒绝。"""
    if not name or name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
        raise RuntimeError(f"压缩包路径越界，已拒绝：{name[:60]}")
    out = os.path.realpath(os.path.join(real_dest, name))
    if out != real_dest and not out.startswith(real_dest + os.sep):
        raise RuntimeError(f"压缩包路径越界，已拒绝：{name[:60]}")
    return out


def _limit(total: int, count: int) -> None:
    if count > config.MAX_EXTRACT_FILES:
        raise RuntimeError(f"压缩包条目数 {count} 超过上限 {config.MAX_EXTRACT_FILES}，已拒绝")
    if total > config.MAX_EXTRACT_BYTES:
        raise RuntimeError(
            f"解压后总大小超过上限 {config.MAX_EXTRACT_BYTES // 1024 // 1024}MB，已拒绝（疑似压缩炸弹）"
        )


def extract_archive(p: str, into: str | None = None) -> dict:
    """解压 zip/tar(.gz/.bz2/.xz) 到目标目录，只接受落在目标内的常规文件。

    先整体验证成员再动手写：半途抛错会留下已经解压出来的一半文件，
    用户看到失败却以为目录还是干净的。
    总量与条目数另有一道闸：包体受上传上限约束，但压缩比可以做到几千倍，
    一个 8MB 的包就能把 root 进程写满整块盘。
    """
    src = safe_path(p)
    if not os.path.isfile(src):
        raise RuntimeError("不是文件")
    dest = safe_path(into) if into else os.path.dirname(src)
    os.makedirs(dest, exist_ok=True)
    real_dest = os.path.realpath(dest)
    names: list[str] = []
    if src.lower().endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(src) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            _limit(sum(i.file_size for i in infos), len(infos))
            for info in infos:
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise RuntimeError(f"压缩包内含符号链接，已拒绝：{info.filename[:60]}")
                _check_member(info.filename, real_dest)
            total = 0
            for info in infos:
                out = _check_member(info.filename, real_dest)
                os.makedirs(os.path.dirname(out), exist_ok=True)
                # 头部声明的尺寸可以是假的，所以边写边统计真实字节数
                with z.open(info) as fp, open(out, "wb") as fo:
                    while True:
                        chunk = fp.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > config.MAX_EXTRACT_BYTES:
                            os.unlink(out)
                            raise RuntimeError(
                                f"解压超过上限 {config.MAX_EXTRACT_BYTES // 1024 // 1024}MB，已中止（疑似压缩炸弹）"
                            )
                        fo.write(chunk)
                names.append(info.filename)
    else:
        import tarfile

        try:
            # open 失败要单独转成"只支持"文案；with 包住整个 body 会把解包中途的 ReadError 也吞进这个文案
            tf = tarfile.open(src)  # noqa: SIM115
        except tarfile.ReadError:
            raise RuntimeError("只支持 .zip 与 .tar/.tar.gz/.tar.bz2/.tar.xz") from None
        with tf:
            members = tf.getmembers()
            _limit(sum(m.size for m in members if m.isfile()), len(members))
            for m in members:
                if not (m.isfile() or m.isdir()):
                    raise RuntimeError(f"压缩包内含特殊文件（链接/设备），已拒绝：{m.name[:60]}")
                _check_member(m.name, real_dest)
            # filter='data' 是标准库自带的越界/权限防护（3.12+），双保险
            tf.extractall(dest, filter="data")
            names = [m.name for m in members if m.isfile()]
    return {"ok": True, "dest": dest, "files": len(names), "names": names[:50]}


def make_archive(paths: list[str], out_name: str = "archive.tar.gz") -> dict:
    """把选中的若干条目打成一个 tar.gz，返回落盘位置供下载。"""
    import tarfile

    picked = [safe_path(x) for x in paths if x]
    if not picked:
        raise RuntimeError("没有选择要打包的路径")
    common = os.path.commonpath([os.path.dirname(x) for x in picked])
    if not any(common == r or common.startswith(r + "/") for r in ROOTS):
        raise RuntimeError("路径超出允许范围")
    # 带线程号：打包跑在线程池里，两个并发请求选中同一个目录时只用 pid 会撞在
    # 同一个临时文件上，tar 互相截断，两边都"成功"，产物却都是坏的。
    tmp = os.path.join(common, f".{safe_name(out_name)}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tarfile.open(tmp, "w:gz") as tf:
            for x in picked:
                tf.add(x, arcname=os.path.basename(x), recursive=True)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RuntimeError(f"打包失败：{e.strerror or e}") from e
    final = os.path.join(common, safe_name(out_name))
    os.replace(tmp, final)
    return {"path": final, "size": os.path.getsize(final), "items": [os.path.basename(x) for x in picked]}


def fs_action(action: str, p: str, p2: str | None = None) -> dict:
    if action == "mkdir":
        abs_ = safe_path(p)
        try:
            os.makedirs(abs_, exist_ok=True)
        except FileExistsError:
            # exist_ok=True 只容忍"已存在且是目录"；同名普通文件仍抛错，
            # 转成可读信息，否则前端拿到 "[Errno 17] File exists: '/root/www/x'"。
            raise RuntimeError("同名文件已存在，无法创建目录") from None
    elif action == "delete":
        if is_root(p):
            raise RuntimeError("禁止删除根目录")
        abs_ = safe_entry_path(p)
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
        if is_root(p) or is_root(p2 or ""):
            raise RuntimeError("禁止重命名根目录")
        abs_ = safe_entry_path(p)
        to = safe_entry_path(p2 or "")
        if not os.path.lexists(abs_):
            raise RuntimeError("原路径不存在")
        # 用 lexists 而不是 exists：目标是个悬空符号链接时 exists 为 False，
        # 检查放行后 os.rename 会把这个链接直接顶掉，用户以为"目标已存在"被拦了。
        if os.path.lexists(to):
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
