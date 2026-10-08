from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from ..services import backup_service

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


@router.get("/api/backups")
async def backups_list():
    return backup_service.list_backups()


@router.post("/api/backups")
async def backups_create(req: Request):
    body = await _body(req)
    try:
        b = await backup_service.create_backup(body)
        dbm.audit(req.state.cp_sub, "backup:create", f"{body.get('kind')}:{body.get('target')}")
        return b
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.patch("/api/backups/{bid}")
async def backups_update(bid: str, req: Request):
    body = await _body(req)
    try:
        r = await backup_service.update_backup(int(bid), body)
        # 计划改成 manual 就等于"从此不再自动备份"，这类变更不记审计事后查不到是谁改的
        dbm.audit(req.state.cp_sub, "backup:update", bid)
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.delete("/api/backups/{bid}")
async def backups_delete(bid: str, req: Request):
    try:
        await backup_service.delete_backup(int(bid))
        dbm.audit(req.state.cp_sub, "backup:delete", bid)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/backups/{bid}/run")
async def backups_run(bid: str, req: Request):
    try:
        r = await backup_service.run_backup_now(int(bid))
        # 带 req：既是为了记"谁手动跑了备份"，也是因为不写审计的写接口
        # 在审计页面上等于没发生过。失败也记一条并带上退出码。
        dbm.audit(req.state.cp_sub, "backup:run", f"{bid}(code={r['code']})")
        return {"code": r["code"], "log": r["out"]}
    except Exception as e:  # noqa: BLE001
        dbm.audit(req.state.cp_sub, "backup:run", f"{bid}(失败: {e})")
        return JSONResponse(status_code=500, content={"error": str(e)})
