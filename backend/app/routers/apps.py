from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from ..services import apps_service, nginx_ops, systemd_ops
from ..templates import apply_template
from ..util import parse_lines, redact_creds
from .system import log_stream_response

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        return {}
    return b if isinstance(b, dict) else {}


@router.get("/api/apps")
async def apps_list(req: Request):
    rows = await apps_service.list_apps()
    if req.state.cp_role != "admin":
        # 仓库地址可能写成 https://user:token@host/x.git，token 不能随列表接口给只读账号
        for r in rows:
            r["repo_url"] = redact_creds(r.get("repo_url"))
    return rows


@router.get("/api/apps/{app_id}")
async def app_get(app_id: int, req: Request):
    a = apps_service.get_app(app_id)
    if not a:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    unit = apps_service.unit_name(a)
    running = await systemd_ops.is_active(unit)
    port = a["port"] if a["port"] is not None else (await apps_service.detect_port(unit) if running else None)
    out = {**a, "running": running, "unit": unit, "port": port, "portAuto": a["port"] is None and port is not None}
    # 组合项目的组件摘要：详情页要展示"这个项目还有哪些前端/后端"
    kids = dbm.query(
        "SELECT id,name,type,parent_id,component_role FROM apps WHERE parent_id=?", (app_id,)
    )
    if kids or a.get("parent_id"):
        fam = list(kids)
        if a.get("parent_id"):
            p = dbm.query_one("SELECT id,name,type,parent_id,component_role FROM apps WHERE id=?", (a["parent_id"],))
            if p:
                fam.insert(0, p)
        if not kids and not any(x["id"] == a["id"] for x in fam):
            fam.insert(0, {"id": a["id"], "name": a["name"], "type": a["type"], "parent_id": a["parent_id"], "component_role": a.get("component_role") or ""})
        out["components"] = fam
    else:
        out["components"] = []
    # env 里是应用密钥；列表接口与告警配置都刻意剥离/脱敏，详情接口不能例外。
    # 保留键名并给空数组，viewer 的前端表单仍能正常解析。
    if req.state.cp_role != "admin":
        out["env"] = "[]"
        out["repo_url"] = redact_creds(out.get("repo_url"))
    return out


@router.post("/api/apps")
async def app_create(req: Request):
    try:
        body = apply_template(await _body(req))
        comps = body.get("components")
        if isinstance(comps, list) and len(comps) >= 2:
            return await _create_group(req, comps)
        # 先套用一键部署模板默认值，再走统一的 create_app 校验
        a = apps_service.create_app(body)
        dbm.audit(req.state.cp_sub, "app:create", a["name"])
        return a
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


async def _create_group(req: Request, comps: list):
    """前后端分离项目：第一个组件为父（入口/后端），其余挂 parent_id。
    列表里合并成一个项目展示，各自的部署/启停仍独立管理。"""
    created = []
    try:
        parent = apps_service.create_app(dict(comps[0]))
        created.append(parent)
        for c in comps[1:]:
            item = dict(c)
            item["parent_id"] = parent["id"]
            created.append(apps_service.create_app(item))
        dbm.audit(req.state.cp_sub, "app:group-create", parent["name"])
        return {"ok": True, "group": parent["id"], "apps": created}
    except Exception as e:  # noqa: BLE001
        # 半途失败要把已建的组件清掉，否则留下没有父的孤儿应用
        for a in created:
            try:
                await apps_service.delete_app(a["id"], False)
            except Exception:  # noqa: BLE001
                pass
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.delete("/api/apps/{app_id}")
async def app_delete(app_id: int, req: Request):
    try:
        await apps_service.delete_app(app_id, req.query_params.get("purge") == "1")
        # 父被删后子组件解除归属而不是连带删除：它们是各自独立的服务
        dbm.execute("UPDATE apps SET parent_id=NULL WHERE parent_id=?", (app_id,))
        dbm.audit(req.state.cp_sub, "app:delete", str(app_id))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.patch("/api/apps/{app_id}")
async def app_update(app_id: int, req: Request):
    try:
        old = apps_service.get_app(app_id)
        a = apps_service.update_app(app_id, await _body(req))
        await apps_service.cleanup_renamed(old, a)
        dbm.audit(req.state.cp_sub, "app:update", a["name"])
        return a
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})


@router.post("/api/apps/{app_id}/action")
async def app_action(app_id: int, req: Request):
    verb = (await _body(req)).get("verb", "")
    try:
        result = await apps_service.app_action(app_id, verb)
        dbm.audit(req.state.cp_sub, f"app:{verb}", str(app_id))
        # 服务层已经统一返回 {"ok": true, ...}：rollback 带 commit/deployment/from，
        # start/stop/restart 带 active。这里不再重复包装，避免出现 ok 语义两套算法。
        return result
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


@router.post("/api/apps/{app_id}/sync")
async def app_sync(app_id: int, req: Request):
    try:
        # 同步会动工作区并重启服务，属于写操作（viewer 已被中间件挡在 POST 之外），
        # 失败也要留下记录，所以审计写在服务层返回之后、只记动作与 commit。
        r = await apps_service.sync_app(app_id)
        dbm.audit(req.state.cp_sub, "app:sync", f"{app_id}({r.get('commit') or '-'})")
        return r
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
    if not row:
        return JSONResponse(status_code=404, content={"error": "部署记录不存在"})
    # 落库前就打码了，这里再打一次是为了清掉修复之前写进库的历史日志
    return {**row, "log": redact_creds(row.get("log"))}


@router.post("/api/apps/{app_id}/ssl")
async def app_ssl(app_id: int, req: Request):
    try:
        out = await apps_service.attach_ssl(app_id, (await _body(req)).get("email"))
        dbm.audit(req.state.cp_sub, "app:ssl", str(app_id))
        return {"ok": True, "log": out}
    except Exception as e:  # noqa: BLE001
        # 与其它 app 路由一致返回 400：certbot 未安装、端口未填、nginx -t 不过
        # 都是用户可修正的配置问题，返 500 时前端只能显示"服务器内部错误"。
        return JSONResponse(status_code=400, content={"error": str(e)})


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
    kind = body.get("kind") or ""
    file = body.get("file") or ""
    if not file:
        return JSONResponse(status_code=400, content={"error": "缺少 file 参数"})
    # 先在这里挡掉未知 kind：一是 body["kind"] 缺失时旧写法抛 KeyError，返回一句
    # "'kind'" 让人摸不着头脑；二是审计动作名直接来自它，不能是任意字符串。
    if kind not in ("ws", "body", "redirect"):
        return JSONResponse(status_code=400, content={"error": "未知的快捷操作类型"})
    try:
        r = await nginx_ops.quick_edit_config(file, kind, body.get("value"))
        dbm.audit(req.state.cp_sub, f"app:nginx-{kind}", file)
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
    lines = parse_lines(request.query_params.get("lines"))
    return log_stream_response(apps_service.unit_name(a), lines)
