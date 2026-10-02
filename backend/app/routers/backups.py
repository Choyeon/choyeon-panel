from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from ..services import backup_service

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:
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
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.patch("/api/backups/{bid}")
async def backups_update(bid: str, req: Request):
    body = await _body(req)
    try:
        return await backup_service.update_backup(int(bid), body)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.delete("/api/backups/{bid}")
async def backups_delete(bid: str, req: Request):
    try:
        await backup_service.delete_backup(int(bid))
        dbm.audit(req.state.cp_sub, "backup:delete", bid)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/backups/{bid}/run")
async def backups_run(bid: str):
    try:
        r = await backup_service.run_backup_now(int(bid))
        return {"code": r["code"], "log": r["out"]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
