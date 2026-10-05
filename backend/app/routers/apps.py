from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from ..services import apps_service, nginx_ops, systemd_ops
from ..templates import apply_template
from .system import log_stream_response

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        return {}
    return b if isinstance(b, dict) else {}


@router.get("/api/apps")
async def apps_list():
    return await apps_service.list_apps()


@router.get("/api/apps/{app_id}")
async def app_get(app_id: int):
    a = apps_service.get_app(app_id)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    unit = apps_service.unit_name(a)
    running = await systemd_ops.is_active(unit)
    port = a["port"] if a["port"] is not None else (await apps_service.detect_port(unit) if running else None)
    return {**a, "running": running, "unit": unit, "port": port, "portAuto": a["port"] is None and port is not None}


@router.post("/api/apps")
async def app_create(req: Request):
    try:
        # 先套用一键部署模板默认值，再走统一的 create_app 校验
        a = apps_service.create_app(apply_template(await _body(req)))
        dbm.audit(req.state.cp_sub, "app:create", a["name"])
        return a
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.patch("/api/apps/{app_id}")
async def app_update(app_id: int, req: Request):
    try:
        a = apps_service.update_app(app_id, await _body(req))
        dbm.audit(req.state.cp_sub, "app:update", a["name"])
        return a
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.delete("/api/apps/{app_id}")
async def app_delete(app_id: int, req: Request):
    try:
        await apps_service.delete_app(app_id, req.query_params.get("purge") == "1")
        dbm.audit(req.state.cp_sub, "app:delete", str(app_id))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/apps/{app_id}/action")
async def app_action(app_id: int, req: Request):
    verb = (await _body(req)).get("verb", "")
    try:
        result = await apps_service.app_action(app_id, verb)
        dbm.audit(req.state.cp_sub, f"app:{verb}", str(app_id))
        if verb == "rollback":
            return {"ok": True, **result}
        return {"ok": result, "active": result}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/apps/{app_id}/deploy")
async def app_deploy(app_id: int, req: Request):
    try:
        dep_id = await apps_service.deploy_app(app_id)
        dbm.audit(req.state.cp_sub, "app:deploy", str(app_id))
        return {"deployment": dep_id}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/deployments")
async def app_deployments(app_id: int):
    return dbm.query(
        "SELECT id,app_id,status,commit_sha,started_at,finished_at,length(log) log_len FROM deployments WHERE app_id=?"
        " ORDER BY id DESC LIMIT 20",
        (app_id,),
    )


@router.get("/api/apps/{app_id}/deployments/{dep_id}")
async def app_deployment(app_id: int, dep_id: int):
    row = dbm.query_one(
        "SELECT id,status,commit_sha,started_at,finished_at,log FROM deployments WHERE id=? AND app_id=?",
        (dep_id, app_id),
    )
    return row or JSONResponse(status_code=404, content={"error": "部署记录不存在"})


@router.post("/api/apps/{app_id}/ssl")
async def app_ssl(app_id: int, req: Request):
    try:
        out = await apps_service.attach_ssl(app_id, (await _body(req)).get("email"))
        dbm.audit(req.state.cp_sub, "app:ssl", str(app_id))
        return {"ok": True, "log": out}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/api/apps/{app_id}/nginx")
async def app_nginx(app_id: int):
    a = apps_service.get_app(app_id)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    unit = apps_service.unit_name(a)
    running = await systemd_ops.is_active(unit)
    port = a["port"] if a["port"] is not None else (await apps_service.detect_port(unit) if running else None)
    return {"domain": a["domain"], "port": port, "configs": nginx_ops.find_app_configs(port, a["domain"])}


@router.put("/api/apps/{app_id}/nginx")
async def app_nginx_save(app_id: int, req: Request):
    body = await _body(req)
    try:
        r = await nginx_ops.save_app_config(body.get("file", ""), body.get("content", ""))
        dbm.audit(req.state.cp_sub, "app:nginx-save", body.get("file"))
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/apps/{app_id}/nginx/quick")
async def app_nginx_quick(app_id: int, req: Request):
    body = await _body(req)
    try:
        r = await nginx_ops.quick_edit_config(body["file"], body["kind"], body.get("value"))
        dbm.audit(req.state.cp_sub, f"app:nginx-{body['kind']}", body["file"])
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/unit")
async def app_unit_read(app_id: int):
    try:
        return apps_service.read_unit(app_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=404, content={"error": str(e)})


@router.put("/api/apps/{app_id}/unit")
async def app_unit_save(app_id: int, req: Request):
    try:
        r = await apps_service.save_unit(app_id, (await _body(req)).get("content", ""))
        dbm.audit(req.state.cp_sub, "app:unit", str(app_id))
        return r
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/proc")
async def app_proc(app_id: int):
    try:
        return await apps_service.app_proc(app_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.get("/api/apps/{app_id}/logs")
async def app_logs(app_id: int, request: Request):
    a = apps_service.get_app(app_id)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    lines = int(request.query_params.get("lines") or 200)
    return log_stream_response(apps_service.unit_name(a), lines)
