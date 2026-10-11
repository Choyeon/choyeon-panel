import asyncio
import codecs
import contextlib
import fcntl
import json
import os
import pty
import struct
import subprocess
import termios
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import config
from .. import aio_db
from .. import database as dbm
from .. import security
from ..log import warn

router = APIRouter()

_SESSIONS = 0  # 当前终端会话数，避免并发 pty 拖垮机器
_OVERFLOW_NOTE = "\r\n\x1b[31m[面板] 输出积压超过上限，已断开该终端会话。\x1b[0m\r\n".encode()

# 长连接只在握手那一刻校验过角色，之后 JWT 里的 role 声明是快照、不再回头看库。
# 于是"把这个人降为只读 / 删号 / 改密吊销旧 token"之后，那台 root shell 照样活着，
# 直到浏览器标签关掉——这是面板里唯一一条权限已收回但会话还继续可用的通道。
_RECHECK_SEC = 60


def _identity(user: str) -> tuple[str, str]:
    """身份快照 =（库里的角色，token 版本号）。同步 sqlite，调用方放线程里跑。

    账号不存在按 LookupError 抛：调用方把"查不到人"和"变了"同样当成失效。
    """
    row = dbm.query_one("SELECT role FROM users WHERE username=?", (user,))
    if not row:
        raise LookupError(f"用户 {user} 不存在")
    return str(row["role"] or "admin"), security.session_epoch(user)


async def _revalidate(ws: WebSocket, user: str, identity: tuple[str, str]) -> None:
    while True:
        await asyncio.sleep(_RECHECK_SEC)
        try:
            stale = await asyncio.to_thread(_identity, user) != identity
        except Exception:  # noqa: BLE001
            stale = True
        if not stale:
            continue
        with contextlib.suppress(Exception):
            await aio_db.audit(user, "terminal:revoked")
            await ws.close(code=4401)
        return


def _dim(raw: object, fallback: int, cap: int) -> int:
    # 浏览器传来的尺寸先校验再写进 ioctl：负数/非数字会让 struct.pack 抛错。
    # 一帧坏数据不该打死整个会话，所以回落默认值而不是往上抛。
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if n <= 0:
        return fallback
    return min(n, cap)


class _Backlog:
    """终端未消费输出的字节水位。

    浏览器停住时（切后台、断网但连接没关）pty 还在生产输出，无界队列会把
    `yes`/`cat 大文件` 的输出全堆进内存，最终 OOM 带走的是整个面板。
    按"已入队 - 已消费"计量，超过上限就主动收尾这一个会话。
    """

    def __init__(self, cap: int):
        self.cap = cap
        self.pending = 0

    def add(self, n: int) -> bool:
        """记录新输出，返回是否已越过水位（EOF 帧 n=0 永不触发）。"""
        self.pending += n
        return n > 0 and self.pending > self.cap

    def drain(self, n: int) -> None:
        self.pending = max(0, self.pending - n)


def _reap_process(proc: subprocess.Popen) -> None:
    """在独立线程里回收 bash 及其整个进程组（TERM → 3s → KILL）。

    放到线程而不是 finally 里 await：finally 中一旦 await，WS 断开带来的
    任务取消会把后面的清理（审计等）整段跳过，实测泄漏 /dev/ptmx 主端 fd。
    """
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(proc.pid), 15)
    try:
        proc.wait(timeout=3)
        return
    except Exception:  # noqa: BLE001 超时即视为需要强杀
        pass
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(proc.pid), 9)
    with contextlib.suppress(Exception):
        proc.wait(timeout=3)


def _shell_cwd() -> str:
    """bash 的起始目录：root 跑面板时是 /root，否则逐级回落。

    旧写法写死 `cwd="/root"`。面板以 root 运行（deploy/choyeon-panel.service）时没问题，
    但 README 承诺"本机也可开发……Linux 专属能力会降级为提示"，而普通用户跑后端时
    `/root` 是不可搜索目录：subprocess.Popen 直接抛 PermissionError，WS 建连后立刻
    异常关闭，前端只看到"终端断开"，没有任何原因可查（非 root 实测复现）。
    这里按 X_OK（chdir 需要的权限）挑选第一个可用目录，都不行时退回 "/"。
    """
    for cand in ("/root", os.path.expanduser("~"), config.APP_ROOT, config.DATA_DIR, "/"):
        if cand and os.path.isdir(cand) and os.access(cand, os.X_OK):
            return cand
    return "/"


@router.websocket("/api/terminal")
async def terminal(ws: WebSocket):
    global _SESSIONS
    state = ws.scope.get("state", {})
    if state.get("cp_role") != "admin":
        await ws.close(code=4403)
        return
    if _SESSIONS >= config.TERMINAL_MAX_SESSIONS:
        await ws.close(code=4503, reason="too many sessions")
        return

    # 占额度必须紧接检查、中间不引入 await：`await ws.accept()` 与审计写入都会让出事件循环，
    # 两个并发握手可同时通过上面的上限检查，实际会话数突破 TERMINAL_MAX_SESSIONS 并泄漏 pty。
    _SESSIONS += 1
    master = slave = None
    proc = None
    opened = False
    user = state.get("cp_sub") or "?"
    loop = None
    reader_added = False
    pump = None
    guard = None
    try:
        # accept/审计必须在 try 内：客户端在握手中途断开时 accept() 会抛异常，
        # 旧写法此时已经占了一个额度却永远不会释放（finally 还没进入），
        # 反复几次后 TERMINAL_MAX_SESSIONS 被占满，终端整个功能永久不可用直到重启。
        await ws.accept()
        # 以库为准再认一次身份（中间件的 role 来自 token 声明，是签发时的快照），
        # 并把这一份作为后续复验的基线。查不到人就失败关闭：这是 root shell。
        try:
            identity = await asyncio.to_thread(_identity, user)
        except Exception:  # noqa: BLE001
            await ws.close(code=4401)
            return
        if identity[0] != "admin":
            await ws.close(code=4403)
            return
        # opened 只管"要不要补一条 terminal:close 审计"：被上面两条拒绝的握手
        # 连 pty 都没建，写一条 close 会在审计里凭空多出一次"关闭"。
        opened = True
        await aio_db.audit(user, "terminal:open")
        guard = asyncio.create_task(_revalidate(ws, user, identity))

        loop = asyncio.get_running_loop()
        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
        env = dict(os.environ)
        env.update({"TERM": "xterm-256color", "PS1": "[choyeon-panel \\W]\\# "})
        # systemd 环境默认没有 LANG：bash 里 echo 中文、git log 中文作者、
        # vim 状态栏都会变成乱码问号——网页终端"编码有问题"的真正根因
        # 不在 WebSocket/JSON 层（那里全程 UTF-8），而在 shell 侧没有 UTF-8 locale。
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.Popen(
            ["/bin/bash"], stdin=slave, stdout=slave, stderr=slave,
            cwd=_shell_cwd(), env=env, start_new_session=True, close_fds=True,
        )
        os.close(slave)
        slave = None
        out_q: asyncio.Queue = asyncio.Queue()
        backlog = _Backlog(config.TERMINAL_OUT_BUFFER)

        def on_readable():
            nonlocal reader_added
            try:
                data = os.read(master, 4096)
            except OSError:
                data = b""
            if backlog.add(len(data)):
                # 先把"为什么断了"塞进同一条流，再放 EOF 哨兵让 pump 正常收尾
                with contextlib.suppress(Exception):
                    out_q.put_nowait(_OVERFLOW_NOTE)
                    out_q.put_nowait(b"")
                with contextlib.suppress(Exception):
                    loop.remove_reader(master)
                reader_added = False
                return
            out_q.put_nowait(data)

        loop.add_reader(master, on_readable)
        reader_added = True

        async def pump_out():
            # PTY 是按 4096 字节切块读出来的，一个中文字符（3 字节）可能正好跨在两块边界上。
            # 直接 data.decode(errors="replace") 会把那半截变成 U+FFFD，网页终端里的中文
            # 就永久乱码了；增量解码器把残字节留到下一块，会话结束再收尾。
            decoder = codecs.getincrementaldecoder("utf-8")()
            while True:
                data = await out_q.get()
                if not data:
                    break
                backlog.drain(len(data))
                text = decoder.decode(data)
                if not text:
                    continue  # 整块都是半个字符，等下一块凑齐再发
                with contextlib.suppress(Exception):
                    await ws.send_json({"d": "out", "data": text})
            with contextlib.suppress(Exception):
                await ws.send_json({"d": "out", "data": decoder.decode(b"", final=True)})
            with contextlib.suppress(Exception):
                await ws.close()

        pump = asyncio.create_task(pump_out())
        while not pump.done():
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("d")
            if kind == "input":
                with contextlib.suppress(OSError):
                    os.write(master, str(msg.get("data", ""))[:65536].encode())
            elif kind == "resize":
                # 帧来自浏览器，坏一帧不该杀掉整个会话：旧写法裸调 int()，
                # 收到 {"cols":"abc"} 直接抛 ValueError，终端无故断开且看不到原因。
                cols = _dim(msg.get("cols"), 100, 400)
                rows = _dim(msg.get("rows"), 28, 200)
                with contextlib.suppress(OSError):
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            elif kind == "ping":
                await ws.send_json({"d": "pong"})
    finally:
        _SESSIONS -= 1
        if pump is not None:
            pump.cancel()
        if guard is not None:
            guard.cancel()
        if reader_added and master is not None:
            with contextlib.suppress(Exception):
                loop.remove_reader(master)
        # fd 必须在任何 await 之前关闭：finally 里一旦 await，任务取消
        # 会从这里打断整段清理（实测 WS 断开时 /dev/ptmx 主端 fd 泄漏，
        # 反复开关终端会耗尽 fd 让面板整个不可用）。
        # slave 正常路径下已在 Popen 后关掉并置 None，这里只兜住失败分支。
        for fd in (slave, master):
            if fd is not None:
                with contextlib.suppress(Exception):
                    os.close(fd)
        # 收尾交给独立线程，finally 里完全不自旋等待：
        # 既不用 await（避免取消打断后面的审计），也不会留僵尸进程。
        if proc is not None:
            threading.Thread(target=_reap_process, args=(proc,), daemon=True).start()
        if opened:
            try:
                await aio_db.audit(user, "terminal:close")
            except Exception as e:  # noqa: BLE001
                warn("terminal audit failed:", e)
