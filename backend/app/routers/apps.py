from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from ..services import apps_service, nginx_ops, systemd_ops
from .system import log_stream_response

router = APIRouter()


def _app_id(raw: str):
    try:
        return int(raw)
    except ValueError:
        return None


@router.get("/api/apps")
async def apps_list():
    return await apps_service.list_apps()


@router.get("/api/apps/{app_id}")
async def app_get(app_id: str):
    a = apps_service.get_app(_app_id(app_id) or -1)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    unit = apps_service.unit_name(a)
    running = await systemd_ops.is_active(unit)
    port = a["port"] if a["port"] is not None else (await apps_service.detect_port(unit) if running else None)
    return {**a, "running": running, "unit": unit, "port": port, "portAuto": a["port"] is None and port is not None}


@router.post("/api/apps")
async def app_create(req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    try:
        a = apps_service.create_app(body)
        dbm.audit(req.state.cp_sub, "app:create", a["name"])
        return a
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.patch("/api/apps/{app_id}")
async def app_update(app_id: str, req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    try:
        a = apps_service.update_app(_app_id(app_id) or -1, body)
        dbm.audit(req.state.cp_sub, "app:update", a["name"])
        return a
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.delete("/api/apps/{app_id}")
async def app_delete(app_id: str, req: Request):
    try:
        await apps_service.delete_app(_app_id(app_id) or -1, req.query_params.get("purge") == "1")
        dbm.audit(req.state.cp_sub, "app:delete", app_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/apps/{app_id}/action")
async def app_action(app_id: str, req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    verb = (body or {}).get("verb", "")
    try:
        ok = await apps_service.app_action(_app_id(app_id) or -1, verb)
        dbm.audit(req.state.cp_sub, f"app:{verb}", app_id)
        return {"ok": ok, "active": ok}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.post("/api/apps/{app_id}/deploy")
async def app_deploy(app_id: str, req: Request):
    try:
        dep_id = await apps_service.deploy_app(_app_id(app_id) or -1)
        dbm.audit(req.state.cp_sub, "app:deploy", app_id)
        return {"deployment": dep_id}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/deployments")
async def app_deployments(app_id: str):
    return dbm.query(
        "SELECT id,app_id,status,started_at,finished_at,length(log) log_len FROM deployments WHERE app_id=?"
        " ORDER BY id DESC LIMIT 20",
        (_app_id(app_id) or -1,),
    )


@router.get("/api/apps/{app_id}/deployments/{dep_id}")
async def app_deployment(app_id: str, dep_id: str):
    return dbm.query_one(
        "SELECT id,status,started_at,finished_at,log FROM deployments WHERE id=? AND app_id=?",
        (_app_id(dep_id) or -1, _app_id(app_id) or -1),
    )


@router.post("/api/apps/{app_id}/ssl")
async def app_ssl(app_id: str, req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    try:
        out = await apps_service.attach_ssl(_app_id(app_id) or -1, (body or {}).get("email"))
        dbm.audit(req.state.cp_sub, "app:ssl", app_id)
        return {"ok": True, "log": out}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/apps/{app_id}/nginx")
async def app_nginx(app_id: str):
    a = apps_service.get_app(_app_id(app_id) or -1)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    unit = apps_service.unit_name(a)
    running = await systemd_ops.is_active(unit)
    port = a["port"] if a["port"] is not None else (await apps_service.detect_port(unit) if running else None)
    return {"domain": a["domain"], "port": port, "configs": nginx_ops.find_app_configs(port, a["domain"])}


@router.put("/api/apps/{app_id}/nginx")
async def app_nginx_save(app_id: str, req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    try:
        r = await nginx_ops.save_app_config((body or {}).get("file", ""), (body or {}).get("content", ""))
        dbm.audit(req.state.cp_sub, "app:nginx-save", body["file"])
        return r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/apps/{app_id}/nginx/quick")
async def app_nginx_quick(app_id: str, req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    try:
        r = await nginx_ops.quick_edit_config(body["file"], body["kind"], body.get("value"))
        dbm.audit(req.state.cp_sub, f"app:nginx-{body['kind']}", body["file"])
        return r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/unit")
async def app_unit_read(app_id: str):
    try:
        return apps_service.read_unit(_app_id(app_id) or -1)
    except Exception as e:
        return JSONResponse(status_code=404, content={"error": str(e)})


@router.put("/api/apps/{app_id}/unit")
async def app_unit_save(app_id: str, req: Request):
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    try:
        r = await apps_service.save_unit(_app_id(app_id) or -1, (body or {}).get("content", ""))
        dbm.audit(req.state.cp_sub, "app:unit", app_id)
        return r
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/proc")
async def app_proc(app_id: str):
    try:
        return await apps_service.app_proc(_app_id(app_id) or -1)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/logs")
async def app_logs(app_id: str, request: Request):
    a = apps_service.get_app(_app_id(app_id) or -1)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    lines = int(request.query_params.get("lines") or 200)
    return log_stream_response(apps_service.unit_name(a), lines)
