"""Stable automation API.

This router deliberately returns operation ids for long-running writes.  The
browser API remains unchanged and continues to call the existing service layer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import agent_auth
from .. import database as dbm
from ..errors import err_json, public_message
from ..services import apps_service, backup_service, site_claim_service, systemd_ops
from ..templates import apply_template
from ..util import redact_creds

router = APIRouter()


def _deny(req: Request, scope: str):
    return agent_auth.require_scope(req, scope)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _operation_view(row: dict | None) -> dict:
    if not row:
        raise LookupError("操作不存在")
    try:
        result = json.loads(row.get("result") or "{}")
    except (TypeError, ValueError):
        result = {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "resource_type": row.get("resource_type") or "",
        "resource_id": row.get("resource_id") or "",
        "deployment_id": row.get("deployment_id"),
        "status": row["status"],
        "result": result,
        "error": public_message(RuntimeError(row.get("error") or ""), fallback="操作失败") if row.get("error") else "",
        "actor": row.get("actor") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
    }


def _create_operation(kind: str, resource_type: str, resource_id: str, actor: str) -> str:
    op_id = agent_auth.operation_id()
    dbm.execute(
        "INSERT INTO operations(id,kind,resource_type,resource_id,actor) VALUES(?,?,?,?,?)",
        (op_id, kind, resource_type, resource_id, actor),
    )
    return op_id


def _claim_idempotency(req: Request, op_id: str) -> dict | None:
    key = req.headers.get("Idempotency-Key", "").strip()
    if not key:
        return None
    if len(key) > 128:
        raise ValueError("Idempotency-Key 不能超过 128 个字符")
    principal = agent_auth.actor(req)
    route = req.url.path
    row = dbm.query_one(
        "SELECT response,status_code FROM idempotency_keys WHERE principal=? AND route=? AND idem_key=?",
        (principal, route, key),
    )
    if row:
        return {"replayed": True, **json.loads(row["response"]), "status_code": row["status_code"]}
    payload = json.dumps({"operation_id": op_id, "status": "queued"}, ensure_ascii=False)
    dbm.execute(
        "INSERT INTO idempotency_keys(principal,route,idem_key,operation_id,response) VALUES(?,?,?,?,?)",
        (principal, route, key, op_id, payload),
    )
    return {"replayed": False, "operation_id": op_id, "status": "queued", "status_code": 202}


def _new_operation(req: Request, kind: str, resource_type: str, resource_id: str) -> dict:
    key = req.headers.get("Idempotency-Key", "").strip()
    if key and len(key) > 128:
        raise ValueError("Idempotency-Key 不能超过 128 个字符")
    principal = agent_auth.actor(req)
    route = req.url.path
    with dbm._lock:
        try:
            if key:
                existing = dbm.conn.execute(
                    "SELECT response,status_code FROM idempotency_keys WHERE principal=? AND route=? AND idem_key=?",
                    (principal, route, key),
                ).fetchone()
                if existing:
                    return {
                        "replayed": True,
                        **json.loads(existing["response"]),
                        "status_code": existing["status_code"],
                    }
            op_id = agent_auth.operation_id()
            dbm.conn.execute(
                "INSERT INTO operations(id,kind,resource_type,resource_id,actor) VALUES(?,?,?,?,?)",
                (op_id, kind, resource_type, resource_id, principal),
            )
            payload = {"operation_id": op_id, "status": "queued"}
            if key:
                dbm.conn.execute(
                    "INSERT INTO idempotency_keys(principal,route,idem_key,operation_id,response) VALUES(?,?,?,?,?)",
                    (principal, route, key, op_id, json.dumps(payload)),
                )
            dbm.conn.commit()
            return {"replayed": False, **payload, "status_code": 202}
        except Exception:
            dbm.conn.rollback()
            raise


async def _run_operation(op_id: str, work):
    dbm.execute(
        "INSERT INTO operation_events(operation_id,event,data) VALUES(?,?,?)",
        (op_id, "running", "{}"),
    )
    dbm.execute("UPDATE operations SET status='running',updated_at=? WHERE id=?", (_now(), op_id))
    try:
        result = await work()
        dbm.execute(
            "UPDATE operations SET status='succeeded',result=?,updated_at=?,finished_at=? WHERE id=?",
            (json.dumps(result or {}, ensure_ascii=False), _now(), _now(), op_id),
        )
        dbm.execute(
            "INSERT INTO operation_events(operation_id,event,data) VALUES(?,?,?)",
            (op_id, "succeeded", json.dumps(result or {}, ensure_ascii=False)),
        )
    except Exception as exc:  # noqa: BLE001
        dbm.execute(
            "UPDATE operations SET status='failed',error=?,updated_at=?,finished_at=? WHERE id=?",
            (redact_creds(public_message(exc, fallback="操作失败"))[:500], _now(), _now(), op_id),
        )
        dbm.execute(
            "INSERT INTO operation_events(operation_id,event,data) VALUES(?,?,?)",
            (op_id, "failed", json.dumps({"error": str(exc)[:500]}, ensure_ascii=False)),
        )


def _queued(req: Request, kind: str, resource_type: str, resource_id: str, work):
    idem = _new_operation(req, kind, resource_type, resource_id)
    if idem["replayed"]:
        return JSONResponse(
            status_code=idem["status_code"], content={k: v for k, v in idem.items() if k != "status_code"}
        )
    op_id = idem["operation_id"]
    asyncio.create_task(_run_operation(op_id, work))
    return JSONResponse(status_code=202, content={"operation_id": op_id, "status": "queued"})


@router.get("/api/v1/health")
async def health(req: Request):
    if r := _deny(req, "system:read"):
        return r
    row = dbm.query_one("SELECT COUNT(*) AS users FROM users")
    from .. import config

    return {"ok": True, "version": config.VERSION, "users": row["users"] if row else 0}


@router.get("/api/v1/overview")
async def overview(req: Request):
    if r := _deny(req, "system:read"):
        return r
    apps = await apps_service.list_apps()
    return {
        "apps": {"total": len(apps), "running": sum(bool(a.get("running")) for a in apps)},
        "service": await systemd_ops.unit_state(systemd_ops.PANEL_SERVICE),
    }


@router.get("/api/v1/apps")
async def apps_list(req: Request):
    if r := _deny(req, "apps:read"):
        return r
    rows = await apps_service.list_apps()
    for row in rows:
        row.pop("env", None)
        row.pop("start_cmd", None)
        row.pop("install_cmd", None)
        row["repo_url"] = redact_creds(row.get("repo_url"))
    return {"items": rows}


@router.get("/api/v1/apps/{app_id}")
async def app_get(app_id: int, req: Request):
    if r := _deny(req, "apps:read"):
        return r
    app = apps_service.get_app(app_id)
    if not app:
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})
    app = dict(app)
    app.pop("env", None)
    app.pop("start_cmd", None)
    app.pop("install_cmd", None)
    app["repo_url"] = redact_creds(app.get("repo_url"))
    return app


@router.get("/api/v1/apps/{app_id}/deployments")
async def app_deployments(app_id: int, req: Request):
    if r := _deny(req, "apps:read"):
        return r
    return {"items": dbm.query("SELECT * FROM deployments WHERE app_id=? ORDER BY id DESC LIMIT 50", (app_id,))}


@router.get("/api/v1/apps/{app_id}/logs")
async def app_logs(app_id: int, req: Request):
    if r := _deny(req, "apps:read"):
        return r
    if not apps_service.get_app(app_id):
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})
    return {
        "items": dbm.query(
            "SELECT id,status,started_at,finished_at,log FROM deployments WHERE app_id=? ORDER BY id DESC LIMIT 10",
            (app_id,),
        )
    }


@router.get("/api/v1/apps/{app_id}/sites")
async def app_sites(app_id: int, req: Request):
    if r := _deny(req, "apps:read"):
        return r
    if not apps_service.get_app(app_id):
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})
    return {
        "items": dbm.query(
            "SELECT domain,file,kind,source,note,created_at FROM site_claims WHERE app_id=? ORDER BY domain", (app_id,)
        )
    }


@router.get("/api/v1/audit")
async def audit(req: Request, limit: int = 100, before: int | None = None):
    if r := _deny(req, "audit:read"):
        return r
    limit = max(1, min(int(limit), 200))
    if before is None:
        rows = dbm.query("SELECT id,username,action,detail,created_at FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = dbm.query(
            "SELECT id,username,action,detail,created_at FROM audit WHERE id<? ORDER BY id DESC LIMIT ?",
            (int(before), limit),
        )
    return {"items": rows, "next_before": rows[-1]["id"] if len(rows) == limit else None}


@router.get("/api/v1/operations/{operation_id}")
async def operation_get(operation_id: str, req: Request):
    if r := _deny(req, "system:read"):
        return r
    try:
        row = dbm.query_one("SELECT * FROM operations WHERE id=? AND actor=?", (operation_id, agent_auth.actor(req)))
        return _operation_view(row)
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc), "code": "not_found"})


@router.get("/api/v1/operations/{operation_id}/events")
async def operation_events(operation_id: str, req: Request, after: int = 0):
    if r := _deny(req, "system:read"):
        return r
    if not dbm.query_one("SELECT id FROM operations WHERE id=? AND actor=?", (operation_id, agent_auth.actor(req))):
        return JSONResponse(status_code=404, content={"error": "操作不存在", "code": "not_found"})
    rows = dbm.query(
        "SELECT id,event,data,created_at FROM operation_events WHERE operation_id=? AND id>? ORDER BY id LIMIT 200",
        (operation_id, max(0, int(after))),
    )
    for row in rows:
        try:
            row["data"] = json.loads(row["data"] or "{}")
        except (ValueError, TypeError):
            row["data"] = {}
    return {"items": rows, "next_after": rows[-1]["id"] if len(rows) == 200 else None}


@router.post("/api/v1/apps")
async def app_create(req: Request):
    if r := _deny(req, "apps:write"):
        return r
    try:
        body = await req.json()
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        app = apps_service.create_app(apply_template(body))
        dbm.audit(agent_auth.actor(req), "api-v1:app:create", app["name"])
        app.pop("env", None)
        app.pop("start_cmd", None)
        app.pop("install_cmd", None)
        app["repo_url"] = redact_creds(app.get("repo_url"))
        return JSONResponse(status_code=201, content={"app": app})
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.post("/api/v1/apps/{app_id}/sites/claim")
async def site_claim(app_id: int, req: Request):
    if r := _deny(req, "sites:write"):
        return r
    app = apps_service.get_app(app_id)
    if not app:
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})
    body = await req.json()
    domain = str(body.get("domain") or app.get("domain") or "").strip()
    try:
        result = await site_claim_service.claim(domain, app_id)
        dbm.audit(agent_auth.actor(req), "api-v1:site:claim", f"{domain}/{app_id}")
        return result
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.post("/api/v1/apps/{app_id}/ssl/renew")
async def ssl_renew(app_id: int, req: Request):
    if r := _deny(req, "sites:write"):
        return r
    app = apps_service.get_app(app_id)
    if not app or not app.get("domain"):
        return JSONResponse(status_code=404, content={"error": "应用不存在或未配置域名", "code": "not_found"})
    from ..services import nginx_ops

    async def work():
        result = await nginx_ops.renew_certs()
        dbm.audit(agent_auth.actor(req), "api-v1:ssl:renew", app["domain"])
        return result

    try:
        return _queued(req, "ssl.renew", "app", str(app_id), work)
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.get("/api/agent-keys")
async def agent_keys_list(req: Request):
    if req.state.cp_role != "admin":
        return JSONResponse(status_code=403, content={"error": "需要管理员权限", "code": "forbidden"})
    return {"items": agent_auth.list_keys(), "available_scopes": sorted(agent_auth.ALL_SCOPES)}


@router.post("/api/agent-keys")
async def agent_keys_create(req: Request):
    if req.state.cp_role != "admin":
        return JSONResponse(status_code=403, content={"error": "需要管理员权限", "code": "forbidden"})
    try:
        body = await req.json()
        result = agent_auth.create_key(
            body.get("name"), body.get("scopes"), body.get("expires_days", 90), agent_auth.actor(req)
        )
        return JSONResponse(status_code=201, content=result)
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.delete("/api/agent-keys/{key_id}")
async def agent_keys_revoke(key_id: int, req: Request):
    if req.state.cp_role != "admin":
        return JSONResponse(status_code=403, content={"error": "需要管理员权限", "code": "forbidden"})
    try:
        return agent_auth.revoke_key(key_id, agent_auth.actor(req))
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc), "code": "not_found"})


@router.post("/api/v1/apps/{app_id}/deploy")
async def app_deploy(app_id: int, req: Request):
    if r := _deny(req, "deploy:write"):
        return r
    if not apps_service.get_app(app_id):
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})

    async def work():
        dep_id = await apps_service.deploy_app(app_id, actor=agent_auth.actor(req))
        row = await apps_service.wait_deployment(dep_id, app_id)
        dbm.audit(agent_auth.actor(req), "api-v1:app:deploy", f"{app_id}/{dep_id}")
        return {"deployment_id": dep_id, **row}

    try:
        return _queued(req, "app.deploy", "app", str(app_id), work)
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.post("/api/v1/apps/{app_id}/actions")
async def app_action(app_id: int, req: Request):
    if r := _deny(req, "apps:write"):
        return r
    if not apps_service.get_app(app_id):
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})
    body = await req.json()
    verb = body.get("verb") if isinstance(body, dict) else ""
    if verb not in {"start", "stop", "restart"}:
        return JSONResponse(
            status_code=400, content={"error": "verb 必须是 start、stop 或 restart", "code": "bad_input"}
        )

    async def work():
        result = await apps_service.app_action(app_id, verb, actor=agent_auth.actor(req))
        dbm.audit(agent_auth.actor(req), f"api-v1:app:{verb}", str(app_id))
        return result

    try:
        return _queued(req, f"app.{verb}", "app", str(app_id), work)
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.post("/api/v1/apps/{app_id}/rollback")
async def app_rollback(app_id: int, req: Request):
    if r := _deny(req, "deploy:write"):
        return r
    if not apps_service.get_app(app_id):
        return JSONResponse(status_code=404, content={"error": "应用不存在", "code": "not_found"})

    async def work():
        result = await apps_service.rollback_app(app_id, actor=agent_auth.actor(req))
        dbm.audit(agent_auth.actor(req), "api-v1:app:rollback", str(app_id))
        return result

    try:
        return _queued(req, "app.rollback", "app", str(app_id), work)
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)


@router.get("/api/v1/backups")
async def backups(req: Request):
    if r := _deny(req, "backups:read"):
        return r
    return {"items": await asyncio.to_thread(backup_service.list_backups)}


@router.post("/api/v1/backups/{backup_id}/run")
async def backup_run(backup_id: int, req: Request):
    if r := _deny(req, "backups:write"):
        return r
    if not dbm.query_one("SELECT id FROM backups WHERE id=?", (backup_id,)):
        return JSONResponse(status_code=404, content={"error": "备份任务不存在", "code": "not_found"})

    async def work():
        result = await backup_service.run_backup_now(backup_id)
        dbm.audit(agent_auth.actor(req), "api-v1:backup:run", str(backup_id))
        return result

    try:
        return _queued(req, "backup.run", "backup", str(backup_id), work)
    except Exception as exc:  # noqa: BLE001
        return err_json(exc)
