import asyncio
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
from .. import database as dbm
from ..log import warn

router = APIRouter()

_SESSIONS = 0  # 当前终端会话数，避免并发 pty 拖垮机器


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
    await ws.accept()
    user = state.get("cp_sub") or "?"
    dbm.audit(user, "terminal:open")

    master = slave = None
    proc = None
    loop = asyncio.get_running_loop()
    reader_added = False
    pump = None
    try:
        master, slave = pty.openpty()
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
        env = dict(os.environ)
        env.update({"TERM": "xterm-256color", "PS1": "[choyeon-panel \\W]\\# "})
        proc = subprocess.Popen(
            ["/bin/bash"], stdin=slave, stdout=slave, stderr=slave,
            cwd=_shell_cwd(), env=env, start_new_session=True, close_fds=True,
        )
        os.close(slave)
        slave = None
        out_q: asyncio.Queue = asyncio.Queue()

        def on_readable():
            try:
                data = os.read(master, 4096)
            except OSError:
                data = b""
            out_q.put_nowait(data)

        loop.add_reader(master, on_readable)
        reader_added = True

        async def pump_out():
            while True:
                data = await out_q.get()
                if not data:
                    break
                with contextlib.suppress(Exception):
                    await ws.send_json({"d": "out", "data": data.decode(errors="replace")})
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
                cols = min(int(msg.get("cols") or 100), 400)
                rows = min(int(msg.get("rows") or 28), 200)
                with contextlib.suppress(OSError):
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            elif kind == "ping":
                await ws.send_json({"d": "pong"})
    finally:
        _SESSIONS -= 1
        if pump is not None:
            pump.cancel()
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
        try:
            dbm.audit(user, "terminal:close")
        except Exception as e:  # noqa: BLE001
            warn("terminal audit failed:", e)
