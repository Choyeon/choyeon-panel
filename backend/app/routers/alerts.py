import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import aio_db
from ..deps import need_admin
from ..log import warn
from ..services import alerts_service
from ..util import LIST_CAP, SEARCH_Q_MAX, like_contains, list_ladder, parse_cursor, parse_limit

router = APIRouter()

LEVEL_RE = re.compile(r"^(ok|warn|crit)$")


async def _fail(detail: str, msg: str = "操作失败，详见面板日志", status: int = 400) -> JSONResponse:
    """对外只给中文提示，原始异常文本（可能带绝对路径、errno、SQLite 文件名）只进面板日志。"""
    warn(detail)
    return JSONResponse(status_code=status, content={"error": msg})


@router.get("/api/alerts/history")
async def alerts_history(req: Request):
    qp = req.query_params
    level = (qp.get("level") or "").strip()
    code = (qp.get("code") or "").strip()[:64]
    # 关键字也在服务端过滤：页面一屏只有 limit 行，在浏览器里 filter 的话更早的命中
    # 永远搜不到，还会被写成「没有符合筛选条件的告警记录」——把「这一屏没有」说成「没有」。
    q = (qp.get("q") or "").strip()[:SEARCH_Q_MAX]
    limit = parse_limit(qp.get("limit"))
    before, cursor_err = parse_cursor(qp.get("before"))
    if cursor_err:
        return JSONResponse(status_code=400, content={"error": cursor_err, "code": "bad_cursor"})
    if level and not LEVEL_RE.match(level):
        return JSONResponse(status_code=400, content={"error": "level 只能是 ok / warn / crit"})
    where, params = [], []
    if level:
        where.append("level=?")
        params.append(level)
    if code:
        where.append("code=?")
        params.append(code)
    if q:
        pat = like_contains(q)
        where.append("(code LIKE ? ESCAPE '\\' OR message LIKE ? ESCAPE '\\')")
        params += [pat, pat]
    filters = (" WHERE " + " AND ".join(where)) if where else ""
    # 游标只收窄这一页，不进 COUNT：matched 是「这个过滤条件下历史上一共多少条」，
    # 翻到第二页时数字不能跟着变小，否则「匹配 5 条」会变成「匹配 3 条」。
    page = filters
    page_params = list(params)
    if before is not None:
        page = filters + (" AND " if filters else " WHERE ") + "id < ?"
        page_params.append(before)
    try:
        # 多取一行只为判断「还有更早的匹配没加载」，它不进响应
        rows = await aio_db.query(
            "SELECT id,ts,level,code,message,meta,acked FROM alerts_log" + page
            + " ORDER BY id DESC LIMIT ?",
            (*page_params, limit + 1),
        )
        # matched 是这个过滤条件下整张表的真实条数：页面上的「匹配 N 条」不能拿本页行数充数
        total = await aio_db.query_one("SELECT COUNT(*) AS n FROM alerts_log" + filters, tuple(params))
    except Exception as e:  # noqa: BLE001
        return await _fail(f"alerts history 查询失败: {type(e).__name__}: {e}", "告警历史读取失败")
    has_more = len(rows) > limit
    # limit / ladder 随结果一起下发：下拉档位过去由页面自己抄（[100,200,500]），
    # 上限一改，页面还能选到服务端已经给不出的那一档。
    return {"rows": rows[:limit], "limit": limit, "ladder": list_ladder(),
            "q": q, "matched": total["n"] if total else None, "hasMore": has_more}


@router.get("/api/alerts/open")
async def alerts_open(req: Request):
    """未确认告警（新→旧）+ 按级别计数。

    计数走一次 GROUP BY 而不是把全表读进内存：ok 级恢复记录也会长期留着，
    未确认行数能到 prune 上限（默认 2000），逐行统计没意义。

    不传 limit 时读到上限为止。过去默认 100，而告警页自己带 `?limit=500` 绕过它：
    同一个「一次能取多少条」在后端和页面各有一份抄本。概览页仍按自己的需要传 limit=6，
    并用 total 与行数之差如实写出「另有 N 条未确认」。
    """
    limit = parse_limit(req.query_params.get("limit"), default=LIST_CAP)
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
        ran = await alerts_service.run_checks()
        inserted = await aio_db.query(
            "SELECT id,ts,level,code,message,meta FROM alerts_log WHERE id>? ORDER BY id", (watermark,)
        )
        await aio_db.audit(req.state.cp_sub, "alert:run",
                           f"{len(inserted)} 条" + (f"({ran['skipped']})" if ran.get("skipped") else ""))
        out = {"ok": True, "inserted": inserted, "count": len(inserted)}
        if ran.get("skipped"):
            out["skipped"] = ran["skipped"]
        return out
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
