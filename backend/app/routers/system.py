import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import database as dbm
from ..services import system_metrics, systemd_ops
from ..util import is_unit, run

router = APIRouter()


@router.get("/api/system/info")
async def sys_info():
    return await system_metrics.info()


@router.get("/api/system/stats")
async def sys_stats():
    return await system_metrics.snapshot()


@router.get("/api/system/services")
async def services():
    return await systemd_ops.list_services()


@router.post("/api/system/services/{unit}")
async def service_action(unit: str, req: Request):
    if not is_unit(unit):
        return JSONResponse(status_code=400, content={"error": "unit 名不合法"})
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    verb = (body or {}).get("verb", "")
    try:
        await systemd_ops.service_action(unit, verb)
        dbm.audit(req.state.cp_sub, f"service:{verb}", unit)
        return {"ok": True, "active": await systemd_ops.is_active(unit)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


async def _sse_journal(unit: str, lines: int):
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso", "-f",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=25)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if not raw:
                break
            yield f"data: {json.dumps(raw.decode(errors='replace').rstrip(chr(10)))}\n\n"
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def log_stream_response(unit: str, lines: int) -> StreamingResponse:
    return StreamingResponse(
        _sse_journal(unit, min(lines, 2000)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/api/system/services/{unit}/logs")
async def service_logs(unit: str, request: Request):
    if not is_unit(unit):
        return JSONResponse(status_code=400, content={"error": "unit 名不合法"})
    lines = int(request.query_params.get("lines") or 200)
    return log_stream_response(unit, lines)


@router.get("/api/firewall")
async def firewall():
    has = await run("which", ["ufw"])
    if has["code"] != 0:
        return {"tool": "none", "active": False, "output": "未安装 ufw"}
    st = await run("ufw", ["status", "verbose"])
    import re

    active = bool(re.search(r"Status: active", st["out"]))
    return {"tool": "ufw", "active": active, "output": "\n".join(st["out"].split("\n")[:80])}
