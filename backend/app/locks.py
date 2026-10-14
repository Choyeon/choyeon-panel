"""跨进程的目标锁：同一个对象上不允许有两个会改现场的操作同时在跑。

为什么是 flock，而不是 asyncio.Lock 或数据库里的 status='running'（批次㉝）：

- 面板的写操作有三个入口——网页请求、`choyeonctl`、systemd timer/oneshot 起的独立进程。
  三者各跑自己的事件循环，asyncio.Lock 只在这一个进程里有效，于是"手动恢复"与
  "定时备份"、"网页部署"与"CLI 回滚"互相看不见对方。
- 数据库状态位是"上次有人记得写、下次没人记得清"的状态：进程被 OOM 或升级杀掉，
  那一行永远停在 running，应用从此删不掉、回滚不了，得靠看门狗按最坏耗时回收。
  flock 由内核在进程死亡时释放，不需要任何人记得清。
- 锁文件里留着持有者的动作、人名和发起时间。抢不到时直接告诉用户「谁在占用、占了多久」，
  而不是"请稍后再试"——运维正在盯的那个应用凭什么不能部署，这个问题值得当场回答。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

# 键的命名空间只有这几个函数：apps_service 写 "app-5" 而 backup_service 写 "app:5"
# 就等于给同一个应用配了两把锁，互不可见——那正是本批次收口的缺陷，所以键只能从这里出。
# 名字类（应用名、库名）进文件名前先过一遍白名单字符：它们都由 is_name/is_ident 校验过，
# 但把没校验过的值拼进路径是不该依赖上游的。
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def key_app(app_id) -> str:
    return f"app-{int(app_id)}"


def key_name(name) -> str:
    """应用记录已经不在（改名后的历史备份、已删除的目标）时，退回按名字锁。"""
    return f"name-{_UNSAFE.sub('_', str(name or ''))[:80]}"


def key_pg(db) -> str:
    return f"pg-{_UNSAFE.sub('_', str(db or ''))[:80]}"


def key_backup(bid) -> str:
    return f"backup-{int(bid)}"


def path_of(key: str) -> Path:
    """DATA_DIR 现取，不做成模块常量：CLI、测试与运维换目录时要立刻生效。"""
    return Path(config.DATA_DIR, "locks", f"{key}.lock")


class Handle:
    __slots__ = ("fd", "key")

    def __init__(self, fd: int, key: str):
        self.fd = fd
        self.key = key

    def release(self) -> None:
        # 先把内容清空再关描述符：留在文件里的"谁在跑"会在下一次读取时被当成
        # 还有人在跑，而那个人的锁其实已经释放了。
        with contextlib.suppress(OSError):
            os.ftruncate(self.fd, 0)
        _close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()
        return False


def _close(fd: int) -> None:
    # 描述符关不掉就没有锁可言，但锁本身已经随进程退出交给内核了，不在这儿再抛一次
    with contextlib.suppress(OSError):
        os.close(fd)


def holder_of(key: str) -> dict | None:
    """读当前持有者的留痕；空文件/坏内容返回 None（崩溃前没来得及写）。"""
    try:
        with open(path_of(key), encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except ValueError:
        return None
    return val if isinstance(val, dict) else None


def acquire(key: str, action: str, actor: str = "system") -> Handle:
    """拿到就返回 Handle，拿不到就抛 RuntimeError（文案里点名是谁在占用）。

    不排队：这些锁保护的是"改线上现场"的操作，用户连点三次不该排上三次覆盖。
    抢不到就当场说清楚，让第二次点击失败比让两份归档叠着写同一个目录便宜得多。
    """
    p = path_of(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _close(fd)
        raise RuntimeError(refused(key, action)) from None
    info = {
        "action": action,
        "actor": actor or "system",
        "pid": os.getpid(),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "since": time.time(),
    }
    _write(fd, info)
    return Handle(fd, key)


def _write(fd: int, info: dict) -> None:
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info, ensure_ascii=False).encode("utf-8"))
    except OSError:
        pass  # 留痕写不进去不该让一次合法的加锁失败：锁本身已经在描述符上


def refused(key: str, action: str) -> str:
    h = holder_of(key)
    if not h:
        return f"{action} 无法开始：同一个目标上还有另一个操作在进行，请等它结束"
    mins = max(0, int((time.time() - float(h.get("since") or 0)) / 60))
    return (
        f"{action} 无法开始：同一个目标上「{h.get('action') or '未知操作'}」正在进行"
        f"（{h.get('actor') or '?'} 于 {h.get('at') or '?'} 发起，已经 {mins} 分钟），请等它结束"
    )


@contextmanager
def hold(key: str, action: str, actor: str = "system"):
    h = acquire(key, action, actor)
    try:
        yield h
    finally:
        h.release()
