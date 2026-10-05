from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from ..services import pg_service

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


@router.get("/api/db/pg/databases")
async def pg_databases():
    try:
        return await pg_service.list_dbs()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.get("/api/db/pg/roles")
async def pg_roles():
    try:
        return await pg_service.list_roles()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.post("/api/db/pg/manage")
async def pg_manage(req: Request):
    body = await _body(req)
    action = body.get("action")
    name = body.get("name") or ""
    try:
        if action == "createDb":
            await pg_service.create_db(name, body.get("owner"))
        elif action == "dropDb":
            await pg_service.drop_db(name)
        elif action == "createRole":
            await pg_service.create_role(name, body.get("password") or "")
        elif action == "setPassword":
            await pg_service.set_role_password(name, body.get("password") or "")
        elif action == "dropRole":
            await pg_service.drop_role(name)
        elif action == "grant":
            await pg_service.grant(name, body.get("db") or "")
        else:
            raise RuntimeError("未知操作")
        dbm.audit(req.state.cp_sub, f"db:{action}", name)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/db/pg/query")
async def pg_query(req: Request):
    body = await _body(req)
    sql = body.get("sql") or ""
    try:
        out = await pg_service.query_sql(sql)
        dbm.audit(req.state.cp_sub, "db:sql", sql[:80])
        return {"result": out}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/db/redis")
async def redis():
    try:
        return await pg_service.redis_info()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.post("/api/db/redis/password")
async def redis_password(req: Request):
    body = await _body(req)
    pg_service.set_redis_password(body.get("password") or "")
    return {"ok": True}
