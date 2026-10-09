"""指标历史接口：仪表盘曲线读持久化行，而不是某个进程的内存环形缓冲。

路由与采样任务由 main.py 各一行接入：include_router(metrics.router) 与
asyncio.create_task(metrics_store.loop())。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services import metrics_store

router = APIRouter()


@router.get("/api/system/history")
async def sys_history(req: Request):
    rng = (req.query_params.get("range") or "1h").strip()
    if rng not in metrics_store.RANGE_SECONDS:
        return JSONResponse(status_code=400, content={"error": "range 仅支持 1h / 6h / 24h / 7d"})
    return await metrics_store.history(rng)


@router.get("/api/system/metrics-status")
async def sys_metrics_status():
    return await metrics_store.status()
