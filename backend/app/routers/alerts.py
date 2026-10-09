import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import aio_db
from ..deps import need_admin
from ..log import warn
from ..services import alerts_service

router = APIRouter()

LEVEL_RE = re.compile(r"^(ok|warn|crit)$")
HISTORY_DEFAULT = 100
HISTORY_CAP = 500


async def _fail(detail: str, msg: str = "操作失败，详见面板日志", status: int = 400) -> JSONResponse:
    """对外只给中文提示，原始异常文本（可能带绝对路径、errno、SQLite 文件名）只进面板日志。"""
    warn(detail)
    return JSONResponse(status_code=status, content={"error": msg})


def _limit(raw: str | None) -> int:
    """翻页条数：非数字/越界一律回落，不报错——这只是"看多少条"，不值得让请求失败。"""
    try:
        n = int(raw or "")
    except (TypeError, ValueError):
        return HISTORY_DEFAULT
    return max(1, min(n or HISTORY_DEFAULT, HISTORY_CAP))


@router.get("/api/alerts/history")
async def alerts_history(req: Request):
    level = (req.query_params.get("level") or "").strip()
    code = (req.query_params.get("code") or "").strip()[:64]
    limit = _limit(req.query_params.get("limit"))
    if level and not LEVEL_RE.match(level):
        return JSONResponse(status_code=400, content={"error": "level 只能是 ok / warn / crit"})
    where, params = [], []
    if level:
        where.append("level=?")
        params.append(level)
    if code:
        where.append("code=?")
        params.append(code)
    sql = "SELECT id,ts,level,code,message,meta,acked FROM alerts_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    try:
        rows = await aio_db.query(sql, tuple(params))
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts history 查询失败: {type(e).__name__}: {e}", "告警历史读取失败")
    return {"rows": rows, "limit": limit}


@router.get("/api/alerts/open")
async def alerts_open(req: Request):
    """未确认告警（新→旧）+ 按级别计数。

    计数走一次 GROUP BY 而不是把全表读进内存：ok 级恢复记录也会长期留着，
    未确认行数能到 prune 上限（默认 2000），逐行统计没意义。
    """
    limit = _limit(req.query_params.get("limit"))
    try:
        rows = await aio_db.query(
            "SELECT id,ts,level,code,message,meta,acked FROM alerts_log WHERE acked=0 ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        counts = await aio_db.query(
            "SELECT level, COUNT(*) c FROM alerts_log WHERE acked=0 GROUP BY level"
        )
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts open 查询失败: {type(e).__name__}: {e}", "未确认告警读取失败")
    by_level = {"ok": 0, "warn": 0, "crit": 0}
    for c in counts:
        if c["level"] in by_level:
            by_level[c["level"]] = int(c["c"])
    return {"rows": rows, "counts": by_level, "total": sum(by_level.values())}


@router.post("/api/alerts/ack-all")
async def alerts_ack_all(req: Request):
    if r := need_admin(req):
        return r
    try:
        pending = await aio_db.query_one("SELECT COUNT(*) c FROM alerts_log WHERE acked=0")
        n = int(pending["c"]) if pending else 0
        if n:
            await aio_db.execute("UPDATE alerts_log SET acked=1 WHERE acked=0")
            await aio_db.audit(req.state.cp_sub, "alert:ack-all", str(n))
        return {"ok": True, "acked": n}
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts ack-all 失败: {type(e).__name__}: {e}", "批量确认失败")


@router.post("/api/alerts/{aid}/ack")
async def alerts_ack(aid: str, req: Request):
    if r := need_admin(req):
        return r
    try:
        row_id = int(aid)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"error": "告警 ID 不合法"})
    if row_id <= 0:
        return JSONResponse(status_code=400, content={"error": "告警 ID 不合法"})
    try:
        row = await aio_db.query_one("SELECT id, acked FROM alerts_log WHERE id=?", (row_id,))
        if row is None:
            return JSONResponse(status_code=404, content={"error": "告警不存在"})
        if not row["acked"]:
            await aio_db.execute("UPDATE alerts_log SET acked=1 WHERE id=?", (row_id,))
        await aio_db.audit(req.state.cp_sub, "alert:ack", str(row_id))
        return {"ok": True, "id": row_id, "already": bool(row["acked"])}
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts ack 失败: {type(e).__name__}: {e}", "确认失败")


@router.post("/api/alerts/run")
async def alerts_run(req: Request):
    if r := need_admin(req):
        return r
    try:
        before = await aio_db.query_one("SELECT COALESCE(MAX(id), 0) m FROM alerts_log")
        watermark = int(before["m"]) if before else 0
        await alerts_service.run_checks()
        inserted = await aio_db.query(
            "SELECT id,ts,level,code,message,meta FROM alerts_log WHERE id>? ORDER BY id", (watermark,)
        )
        await aio_db.audit(req.state.cp_sub, "alert:run", str(len(inserted)))
        return {"ok": True, "inserted": inserted, "count": len(inserted)}
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts run 失败: {type(e).__name__}: {e}", "巡检执行失败，详见面板日志")


@router.post("/api/alerts/probe")
async def alerts_probe(req: Request):
    if r := need_admin(req):
        return r
    try:
        before = await aio_db.query_one("SELECT COALESCE(MAX(id), 0) m FROM alerts_log")
        watermark = int(before["m"]) if before else 0
        results = await alerts_service.run_probes()
        inserted = await aio_db.query(
            "SELECT id,ts,level,code,message,meta FROM alerts_log WHERE id>? ORDER BY id", (watermark,)
        )
        await aio_db.audit(req.state.cp_sub, "alert:probe", str(len(results)))
        return {"ok": True, "results": results, "inserted": inserted, "count": len(inserted)}
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts probe 失败: {type(e).__name__}: {e}", "存活探测执行失败，详见面板日志")
