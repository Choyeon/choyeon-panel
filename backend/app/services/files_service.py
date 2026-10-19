import contextlib
import datetime as _dt
import errno
import grp
import os
import pwd
import re
import shutil
import stat
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from .. import config
from ..util import human_bytes

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


# 权限只有 9 个读写执行位。setuid/setgid/sticky 能把任意程序提权成 root，
# 而面板以 root 运行：一个改权限的输入框不该有这条路径。高位由 _clean_mode 拒掉。
MAX_PERM_MODE = 0o777


@lru_cache(maxsize=256)
def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        # 容器与手工建的用户没有 passwd 条目很常见，按数字显示而不是显示成空。
        return str(uid)


@lru_cache(maxsize=256)
def _group_name(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def perm_fields(st: os.stat_result) -> dict:
    """一个条目的权限与属主，列表与改完回读共用同一个出处（格式只在这里定义一次）。"""
    return {
        "perm": stat.filemode(st.st_mode),
        "octal": f"{stat.S_IMODE(st.st_mode):04o}",
        "owner": _user_name(st.st_uid),
        "group": _group_name(st.st_gid),
    }


def _clean_mode(raw) -> int:
    s = str(raw).strip().lower().removeprefix("0o")
    if not re.fullmatch(r"[0-7]{3,4}", s):
        raise RuntimeError("权限要写成 3-4 位八进制，例如 644 或 0644")
    mode = int(s, 8)
    if mode > MAX_PERM_MODE:
        raise RuntimeError("只能改读/写/执行九位（000-777），setuid/sticky 等高位请手工处理")
    return mode


def _resolve_id(raw: str, by_name, by_num, err: str) -> int:
    """名字或数字都要在 passwd/group 里查得到才放行。"""
    s = str(raw).strip()
    if not (s.isdigit() or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", s)):
        raise RuntimeError(err)
    try:
        return by_num(int(s)) if s.isdigit() else by_name(s)
    except KeyError:
        # 不接受"把文件 chown 给一个系统上不存在的 uid"：那通常是打错了，
        # 后果是文件从此没有任何账号能读，而面板给出的成功提示看不出区别。
        raise RuntimeError(err) from None


def set_perms(p: str, mode=None, owner=None, group=None) -> dict:
    """改单个条目的权限与属主。不做递归：递归 chmod 会吃掉整棵树的执行位，
    一次误点的代价远大于在终端里多敲一条命令。"""
    abs_ = safe_entry_path(p)
    if not os.path.lexists(abs_):
        raise RuntimeError("路径不存在")
    if os.path.islink(abs_):
        # Linux 没有 lchmod，os.chmod 会跟着链接走到目标——那可能在白名单外
        # （应用目录里指向 /etc 的链接很常见）。改链接本身也只能改到目标上。
        raise RuntimeError("这是符号链接：权限与属主只能改它指向的目标文件")
    new_mode = _clean_mode(mode) if mode not in (None, "") else None
    uid = (_resolve_id(owner, lambda n: pwd.getpwnam(n).pw_uid, pwd.getpwuid, "用户不存在")
           if owner not in (None, "") else -1)
    gid = (_resolve_id(group, lambda n: grp.getgrnam(n).gr_gid, grp.getgrgid, "组不存在")
           if group not in (None, "") else -1)
    if new_mode is None and uid < 0 and gid < 0:
        raise RuntimeError("没有要修改的内容")
    try:
        if new_mode is not None:
            os.chmod(abs_, new_mode)
        if uid >= 0 or gid >= 0:
            # -1 表示"这一项不动"，内核按字面处理。
            os.chown(abs_, uid, gid)
    except OSError as e:
        raise RuntimeError(f"修改失败：{e.strerror or e}") from e
    # 属主改名后缓存里的 uid→name 可能变（手工 userdel/useradd），改完清一次。
    _user_name.cache_clear()
    _group_name.cache_clear()
    return {"path": abs_} | perm_fields(os.stat(abs_))


def list_dir(p: str) -> dict:
    # 默认目录必须由服务层给出：写死 /root/www 时，改过 CP_FILE_ROOTS 的部署
    # 一进文件页就落在白名单外，报「路径超出允许范围」，用户以为是 bug。
    # roots 一并回给前端，页面据此渲染快捷入口，不再自己猜目录。
    if not ROOTS:
        raise RuntimeError("未配置 CP_FILE_ROOTS，文件管理不可用")
    abs_ = safe_path(p or ROOTS[0])
    if not os.path.isdir(abs_):
        raise RuntimeError("不是目录")
    names = os.listdir(abs_)
    total = len(names)
    cap = config.MAX_DIR_ENTRIES
    # 截断排在 stat 之前：这个上限要省的就是"为几十万个条目逐项 syscall"那一步，
    # 先 stat 完再截等于什么都没拦。按名称排序后取前 N 条，刷新两次结果一致。
    truncated = total > cap
    if truncated:
        names = sorted(names, key=lambda s: str(s).lower())[:cap]
    entries = []
    for name in names:
        is_dir = False
        # 读不到 stat 的条目（正在被删除、权限拒绝）权限列留空，由前端显示成「—」，
        # 不能拿 0 去格式化——那会渲染成 0000 / "----------"，看着像真的没权限。
        # 大小同一个道理：`0` 是"确认这是一个空文件"这个结论，读不到时得回 None，
        # 前端才印得出「—」（与 util.human_bytes 的口径一致）。
        size = None
        mtime = ""
        perm = {"perm": "", "octal": "", "owner": "", "group": ""}
        try:
            st = os.stat(os.path.join(abs_, name))
            is_dir = stat.S_ISDIR(st.st_mode)
            size = st.st_size
            mtime = iso_ms(st.st_mtime)
            perm = perm_fields(st)
        except OSError:
            pass
        entries.append({"name": name, "isDir": is_dir, "size": size, "mtime": mtime, **perm})
    entries.sort(key=lambda e: (0 if e["isDir"] else 1, str(e["name"]).lower()))
    # 顺带回传额度：上传前先在前端拦一道，免得用户看着进度条走完才收到"超过 8MB"。
    # total/truncated/maxEntries 同理：提示文案由服务层的数拼，前端不自己抄一个上限。
    return {
        "path": abs_,
        "roots": list(ROOTS),
        "maxUploadBytes": MAX_FILE_BYTES,
        "total": total,
        "truncated": truncated,
        "maxEntries": cap,
        "entries": entries,
    }


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
        # S_IMODE 含 setuid/setgid/sticky 高位，原样搬过去等于让"编辑一个文本文件"
        # 把高危位重新落一遍（面板以 root 写）。保留 0777 以内的属主权限就够了。
        os.chmod(tmp, stat.S_IMODE(st.st_mode) & 0o0777)
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


def write_budget(dest: str) -> dict:
    """还能往 dest 所在分区写多少字节，以及这条线是谁划的。

    线不是这里新造的：出处是设置页的 `alert_disk_pct`（alerts_service.disk_thresholds()["warn"]），
    自检、存储占用对账、巡检告警用的都是它。面板以 root 运行，写满磁盘的代价是这台机器上
    所有业务一起停摆（第一个写不进去的往往还是面板自己的 SQLite），所以任何"按别人给的
    尺寸往盘上写"的入口都得先问这一句。
    余量按 `f_bavail` 算（写不进去的都算已用，含 ext4 给 root 预留的那 5%），与
    util.disk_usage_pct 同口径；用 `f_bfree` 会凭空多出一块永远写不了的地板。
    """
    from .alerts_service import disk_thresholds

    line = disk_thresholds()["warn"]
    try:
        st = os.statvfs(dest)
    except OSError as exc:
        raise RuntimeError(f"读不到 {dest} 所在分区的剩余空间（{exc.strerror or exc}），无法确认落盘上限") from None
    total = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    if total <= 0:
        raise RuntimeError(f"{dest} 所在分区报出的容量是 0，无法确认落盘上限")
    # 警戒线是「已用量」的百分比（80 = 用到 80% 就该告警），所以能写到的上界是总量的
    # line%，不是剩下的 (100-line)%。写成 (100 - line) 会把方向反掉：一块用了 51% 的
    # 78GB 盘会算出 room=0，于是 992MB 的正当恢复和文件页打包全被自己的预检挡死。
    ceiling = total * line // 100
    return {"dest": dest, "line": line, "avail": avail, "room": max(0, ceiling - (total - avail))}


def assert_write_budget(dest: str, need: int, what: str) -> None:
    """`need` 字节写不进 {dest} 所在分区的余量就直接拒绝，一个字节都不落。

    必须在动手之前判：解包中途失败留下"半份目录"比拒绝更糟（应用目录恢复没有整份快照可退）。
    """
    b = write_budget(dest)
    if need > b["room"]:
        raise RuntimeError(
            f"{what}最多要写 {human_bytes(need)}，而 {dest} 所在分区按磁盘警戒线 {b['line']}% "
            f"只能再容纳 {human_bytes(b['room'])}（实际剩余 {human_bytes(b['avail'])}）："
            "先清理这块盘，或在设置页把磁盘告警阈值调到更贴近 100%；已拒绝，未写入任何文件"
        )


def extract_archive(p: str, into: str | None = None) -> dict:
    """解压 zip/tar(.gz/.bz2/.xz) 到目标目录，只接受落在目标内的常规文件。

    先整体验证成员再动手写：半途抛错会留下已经解压出来的一半文件，
    用户看到失败却以为目录还是干净的。
    总量与条目数另有一道闸：包体受上传上限约束，但压缩比可以做到几千倍，
    一个 8MB 的包就能把 root 进程写满整块盘。
    落盘余量是第三道闸（`assert_write_budget`）：固定上限挡的是"离谱的声明值"，
    而一块本来就快满的盘需要的判据是"还能写多少"，两条都要有。
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
            declared = sum(i.file_size for i in infos)
            _limit(declared, len(infos))
            assert_write_budget(real_dest, declared, "解压这个包")
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
            declared = sum(m.size for m in members if m.isfile())
            _limit(declared, len(members))
            assert_write_budget(real_dest, declared, "解压这个包")
            for m in members:
                if not (m.isfile() or m.isdir()):
                    raise RuntimeError(f"压缩包内含特殊文件（链接/设备），已拒绝：{m.name[:60]}")
                _check_member(m.name, real_dest)
            # filter='data' 是标准库自带的越界/权限防护（3.12+），双保险
            tf.extractall(dest, filter="data")
            names = [m.name for m in members if m.isfile()]
    return {"ok": True, "dest": dest, "files": len(names), "names": names[:50]}


def _estimate_archive_bytes(paths: list, stop_after: int, skip: tuple = ()) -> int:
    """待打包内容的字节数，给 tar.gz 的落盘量一个上界。

    数到 stop_after 就返：这趟 walk 只回答"装不装得下"，既然已经确定装不下，
    就没必要把几百万个 inode 数完。不跟随符号链接——tar 存的是链接本身而不是内容。
    `skip` 是打包方排除掉的目录名（tar 的 `--exclude=node_modules` 按路径分量匹配，
    这里同口径）：被排除的内容不会进归档，把它们算进尺寸会让一个装着 5GB
    `node_modules` 的应用目录被估成"这份备份要写 5GB"，正当备份被自己的预检挡死。
    """
    total = 0
    for x in paths:
        try:
            st = os.lstat(x)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            for root, dirs, files in os.walk(x, followlinks=False):
                if skip:
                    dirs[:] = [d for d in dirs if d not in skip]
                for f in files:
                    try:
                        total += os.lstat(os.path.join(root, f)).st_size
                    except OSError:
                        continue
                    if total > stop_after:
                        return total
        elif stat.S_ISREG(st.st_mode):
            total += st.st_size
        if total > stop_after:
            return total
    return total


def make_archive(paths: list[str], out_name: str = "archive.tar.gz") -> dict:
    """把选中的若干条目打成一个 tar.gz，返回落盘位置供下载。"""
    import tarfile

    picked = [safe_path(x) for x in paths if x]
    if not picked:
        raise RuntimeError("没有选择要打包的路径")
    common = os.path.commonpath([os.path.dirname(x) for x in picked])
    if not any(common == r or common.startswith(r + "/") for r in ROOTS):
        raise RuntimeError("路径超出允许范围")
    # 预检不是可选项：临时文件与产物都落在源目录同一个文件系统里，root 进程把盘写满
    # 的代价是这台机器上所有业务一起停摆，而不是这一次请求失败。gzip 产物不会大于源
    # 内容，所以源尺寸就是上界；真的算漏了（比如中途别人写满）由下面的 OSError 分支
    # 兜住——它会删掉临时文件并把 ENOSPC 原样报出来。
    # 界从 write_budget 取，不再拿"整块盘的空闲"当额度：写到警戒线以下就该停手，
    # 而不是把盘写到只剩 1KB 才撞到 ENOSPC（那时面板自己的 SQLite 也写不进去了）。
    need = _estimate_archive_bytes(picked, write_budget(common)["room"])
    assert_write_budget(common, need, "打包这些内容")
    # 带线程号：打包跑在线程池里，两个并发请求选中同一个目录时只用 pid 会撞在
    # 同一个临时文件上，tar 互相截断，两边都"成功"，产物却都是坏的。
    tmp = os.path.join(common, f".{safe_name(out_name)}.{os.getpid()}.{threading.get_ident()}.tmp")
    # O_EXCL + O_NOFOLLOW：这个临时文件落在**应用目录**里（应用用户可写），而打包由
    # root 进程执行。tarfile.open 默认跟随已存在的符号链接，有人提前在同一路径放一个
    # 指向 /etc/ 的链接，root 就会替他把一份 gzip 写到他选的位置——文件名还是他给的。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            # 上一次进程被强杀留下的半截临时文件。unlink 只作用于这个路径本身，
            # 就算它此刻已被换成链接，删掉的也是链接而不是链接的目标。
            os.unlink(tmp)
            fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "wb") as fobj, tarfile.open(fileobj=fobj, mode="w:gz") as tf:
            for x in picked:
                tf.add(x, arcname=os.path.basename(x), recursive=True)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RuntimeError(f"打包失败：{e.strerror or e}") from e
    final = os.path.join(common, safe_name(out_name))
    os.replace(tmp, final)
    return {"path": final, "size": os.path.getsize(final), "items": [os.path.basename(x) for x in picked]}



# ---------------------------------------------------------------- 搬运（复制 / 移动）
def _entry_bytes(abs_: str) -> int:
    """这个条目大概要占多少字节——只数眼前这一层，不递归。

    不递归是刻意的：数一棵大树要在动手前占住线程池里的一个线程，而少算的差额由
    `_copy_entry` 的临时副本清理兜住——失败留下的是"没有"，不是"半份"。
    """
    if os.path.islink(abs_):
        return 0
    if os.path.isdir(abs_):
        try:
            return sum(os.path.getsize(os.path.join(abs_, n)) for n in os.listdir(abs_))
        except OSError:
            return 0
    try:
        return os.path.getsize(abs_)
    except OSError:
        return 0


def _remove_entry(abs_: str) -> None:
    """删掉一个已经判过根与白名单的条目：目录连内容一起，链接删的是链接本身。"""
    if os.path.isdir(abs_) and not os.path.islink(abs_):
        shutil.rmtree(abs_)
    else:
        os.unlink(abs_)


def _copy_xattrs(src: str, dst: str) -> None:
    """尽力带上扩展属性（SELinux 安全标签与 ACL 都住在这里）。

    内容对、属主对、权限对而标签丢了，在 enforcing 的 RHEL 系机器上文件照样读不到，
    而这台面板明确支持 Rocky/AlmaLinux。不支持 xattr 的挂载点本来也没有标签可丢，
    所以逐个名字 try 过去、失败就跳过——不能因为文件系统没这个能力就宣布搬运失败。
    """
    try:
        names = os.listxattr(src, follow_symlinks=False)
    except OSError:
        return
    for name in names:
        try:
            value = os.getxattr(src, name, follow_symlinks=False)
            os.setxattr(dst, name, value, follow_symlinks=False)
        except OSError:
            continue


def _restore_tree(src: str, dst: str) -> None:
    """把 copytree 出来的副本逐层回到源端的属主，并补扩展属性。

    copytree 只带 mode 与时间戳，属主落的是发起搬运的 root 进程：把 /data/www 下
    www-data 的站点目录搬去另一个挂载点会变成 root 私有，nginx 下一次读就是 500，
    而报错的时机离"拖了个文件夹"这个动作很远，排查对不上。
    属主落不上就是真失败（宁可不搬），所以这里不 suppress。
    """
    for dirpath, dirnames, filenames in os.walk(dst):
        base = src if os.path.samefile(dst, dirpath) else os.path.join(src, os.path.relpath(dirpath, dst))
        for name in dirnames + filenames:
            sp = os.path.join(base, name)
            fp = os.path.join(dirpath, name)
            try:
                st = os.lstat(sp)
                os.lchown(fp, st.st_uid, st.st_gid)
            except OSError as e:
                raise RuntimeError(f"副本属主没落上（{e.strerror or e}），已放弃这次搬运") from e
            _copy_xattrs(sp, fp)
        try:
            st = os.lstat(base)
            os.lchown(dirpath, st.st_uid, st.st_gid)
        except OSError as e:
            raise RuntimeError(f"副本属主没落上（{e.strerror or e}），已放弃这次搬运") from e
        _copy_xattrs(base, dirpath)


def _place(tmp: str, dst: str, is_dir: bool) -> None:
    """把临时副本落到最终名字上，且不许顶掉任何东西。

    "目标不存在"的检查与落位之间总有窗口（两个人同时往一个目录搬同名文件）。
    文件与符号链接用 os.link 收尾：目标已存在时它直接 EEXIST，是这里唯一的排他落位；
    目录没法硬链接，只能回到 rename，窗口和面板既有的 rename 一样宽——如实写在注释里。
    """
    if not is_dir:
        try:
            os.link(tmp, dst, follow_symlinks=False)
        except OSError as e:
            if e.errno == errno.EEXIST:
                raise RuntimeError("目标目录下已有同名条目（落位时才撞见，可能有并发的搬运）") from None
            if e.errno not in (errno.EPERM, errno.EOPNOTSUPP):
                raise RuntimeError(f"落位失败：{e.strerror or e}") from e
            # vfat 一类没有硬链接的文件系统：退回 rename
        else:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return
    try:
        os.rename(tmp, dst)
    except OSError as e:
        raise RuntimeError(f"落位失败：{e.strerror or e}") from e


def _copy_entry(src: str, dst: str) -> None:
    """复制到 dst（调用方已确认 dst 尚不存在），任何一步失败都不留半份目标。

    三点都在防"搬完比搬前更糟"：
    - 符号链接按链接本身搬：跟过去会把"指错路的 symlink"复制成一份真文件，
      目录里的相对链接还会在副本里指向别处；
    - 先写 .cpmove-xxxx 再落位：中途磁盘满/进程被杀时目标位置上什么都没有；
    - 属主与权限（含 setuid 等高位）原样带上：源与目标同属主同模式，才等于
      "这次搬运没有多出任何权限"。`_keep_owner` 给文本编辑掩掉高危位是对的，
      搬运不能掩——掩了等于把二进制与共享库的权限改了。
    """
    st = os.lstat(src)
    parent, base = os.path.split(dst)
    tmp = os.path.join(parent, f".{base}.cpmove-{uuid.uuid4().hex[:8]}")
    is_dir = stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
    try:
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(src), tmp)
            os.lchown(tmp, st.st_uid, st.st_gid)
            _copy_xattrs(src, tmp)
        elif is_dir:
            shutil.copytree(src, tmp, symlinks=True)
            _restore_tree(src, tmp)
        else:
            shutil.copyfile(src, tmp)
            shutil.copystat(src, tmp, follow_symlinks=False)
            os.chown(tmp, st.st_uid, st.st_gid)
            _copy_xattrs(src, tmp)
        _place(tmp, dst, is_dir)
    except BaseException:
        with contextlib.suppress(Exception):
            _remove_entry(tmp)
        raise


def _move_entry(src: str, dst: str) -> None:
    """同分区就是 os.rename；跨分区（EXDEV）退化成"复制 + 删源"。

    CP_FILE_ROOTS 经常跨挂载点（/root/www 软链到 /data/www，或备份盘单独挂载），
    这种布局上 rename 必然 EXDEV。退路要么整体成功，要么留下明确的两份并说破，
    绝不能留下"目标半份 + 源还在"还不吭声。
    """
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if getattr(e, "errno", None) != errno.EXDEV:
            raise RuntimeError(f"移动失败：{e.strerror or e}") from e
    _copy_entry(src, dst)
    try:
        _remove_entry(src)
    except (OSError, shutil.Error) as e:
        raise RuntimeError(
            f"跨分区移动：{dst} 已经写好，但删不掉原件（{e}）——现在同一份内容存在两处，"
            "请先确认再手工删旧的那份"
        ) from e


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
            # 重命名只管"在这个目录里改个名"。跨目录是搬运：要判自身子目录、要处理跨分区、
            # 要保属主与扩展属性，那些都在 move 分支里。放这里过等于给一次手滑开第二条路径。
            raise RuntimeError("重命名只在同一目录内进行，跨目录请改用「移动」")
        try:
            os.rename(abs_, to)
        except OSError as e:
            raise RuntimeError(f"重命名失败：{e.strerror or e}") from e
    elif action in ("copy", "move"):
        verb = "复制" if action == "copy" else "移动"
        if is_root(p):
            raise RuntimeError(f"禁止{verb}根目录")
        abs_ = safe_entry_path(p)
        dest_dir = safe_path(p2 or "")
        if not os.path.lexists(abs_):
            raise RuntimeError("原路径不存在")
        if not os.path.isdir(dest_dir):
            raise RuntimeError("目标不是目录")
        base = os.path.basename(abs_)
        if not base or base in {".", ".."}:
            raise RuntimeError("非法路径")
        # 落点也走 safe_entry_path：只要求父目录在白名单内，链接本身才能按链接搬。
        to = safe_entry_path(os.path.join(dest_dir, base))
        if os.path.realpath(os.path.dirname(abs_)) == os.path.realpath(dest_dir):
            raise RuntimeError(f"已经在目标目录里，无需{verb}")
        if os.path.lexists(to):
            raise RuntimeError("目标目录下已有同名条目")
        if action == "move" and os.path.isdir(abs_) and not os.path.islink(abs_):
            real_src = os.path.realpath(abs_)
            if os.path.realpath(to).startswith(real_src + os.sep):
                # 把目录搬进它自己的子树，结果取决于内核先拆哪一层：轻则结构变了，
                # 重则半棵子树跟着挪。判掉，不赌。
                raise RuntimeError("不能把目录搬进它自己的子目录")
        # 同分区的移动只改目录项，一个字节都不重写，所以只有真要落盘的操作才问额度。
        same_fs = os.stat(os.path.dirname(abs_)).st_dev == os.stat(dest_dir).st_dev
        if action == "copy" or not same_fs:
            assert_write_budget(dest_dir, _entry_bytes(abs_), f"{verb}这个条目")
        try:
            if action == "copy":
                _copy_entry(abs_, to)
            else:
                _move_entry(abs_, to)
        except (OSError, shutil.Error) as e:
            # 搬运中途的 errno 原文（"[Errno 28] No space left..."）不该直接甩给用户：
            # 本仓的错误契约是可读文案，且失败路径已经清掉了临时副本。
            raise RuntimeError(f"{verb}失败：{getattr(e, 'strerror', None) or e}") from e
        return {"ok": True, "path": to}
    else:
        raise RuntimeError("未知操作")
    return {"ok": True}
