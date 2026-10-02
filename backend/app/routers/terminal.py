import asyncio
import contextlib
import fcntl
import json
import os
import pty
import struct
import subprocess
import termios

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import database as dbm

router = APIRouter()


@router.websocket("/api/terminal")
async def terminal(ws: WebSocket):
    state = ws.scope.get("state", {})
    if state.get("cp_role") != "admin":
        await ws.close(code=4403)
        return
    await ws.accept()
    dbm.audit(state.get("cp_sub") or ws.query_params.get("user") or (ws.client.host if ws.client else "?"), "terminal:open")

    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
    env = dict(os.environ)
    env.update({"TERM": "xterm-256color", "PS1": "[choyeon-panel \\W]\\# "})
    proc = subprocess.Popen(
        ["/bin/bash"], stdin=slave, stdout=slave, stderr=slave,
        cwd="/root", env=env, start_new_session=True, close_fds=True,
    )
    os.close(slave)
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()

    def on_readable():
        try:
            data = os.read(master, 4096)
        except OSError:
            data = b""
        out_q.put_nowait(data)

    loop.add_reader(master, on_readable)

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
    try:
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
                os.write(master, str(msg.get("data", ""))[:65536].encode())
            elif kind == "resize":
                cols = min(int(msg.get("cols") or 100), 400)
                rows = min(int(msg.get("rows") or 28), 200)
                with contextlib.suppress(OSError):
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            elif kind == "ping":
                await ws.send_json({"d": "pong"})
    finally:
        pump.cancel()
        with contextlib.suppress(Exception):
            loop.remove_reader(master)
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            os.close(master)
