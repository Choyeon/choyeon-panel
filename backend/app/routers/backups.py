from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import aio_db
from ..deps import need_admin
from ..services import backup_service
from ..errors import err_json

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


@router.get("/api/backups")
async def backups_list(req: Request):
    # 列表要 stat 每个产物文件，同步磁盘 I/O 放线程里做
    rows = await run_in_threadpool(backup_service.list_backups)
    if req.state.cp_role != "admin":
        # dir 是 /root/backups/panel：只读账号要能看"有没有备份、多大、什么时候"，
        # 不需要知道备份落在哪台机器的哪个目录下。
        for r in rows:
            r.pop("dir", None)
    return rows


@router.get("/api/backups/files")
async def backups_files(req: Request):
    """全部备份产物平铺（备份文件管理页）。放在 /{bid} 之前，避免被当成 id。"""
    res = await run_in_threadpool(backup_service.list_files)
    if req.state.cp_role != "admin":
        res.pop("dir", None)
    return res


@router.post("/api/backups/{bid}/restore")
async def backups_restore(bid: str, req: Request):
    """用指定备份文件覆盖目标（不可逆）。

    pg 与 sqlite 会先自动留存当前副本（pre-restore 快照）；应用目录恢复是增量覆盖，
    只保留当前 .panel.env，不做整目录快照（几百 MB 的 node_modules 再存一份可能当场写满盘）。
    返回体的 warn 非空表示"数据已装上但有人要接手的一项"（服务没起来等），前端据此报警示。
    """
    if r := need_admin(req):
        return r
    body = await _body(req)
    name = str(body.get("file") or "")
    try:
        return await backup_service.restore_backup(int(bid), name, actor=req.state.cp_sub)
    except Exception as e:  # noqa: BLE001
        # 只记任务与文件名，备份内容本身不进审计
        await aio_db.audit(req.state.cp_sub, "backup:restore", f"{bid}:{name}(失败: {e})")
        return err_json(e)


@router.delete("/api/backups/{bid}/files/{name}")
async def backups_file_delete(bid: str, name: str, req: Request):
    if r := need_admin(req):
        return r
    try:
        r = await backup_service.delete_file(int(bid), name)
        await aio_db.audit(req.state.cp_sub, "backup:delete-file", f"{bid}:{r['name']}")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/backups/{bid}/upload")
async def backups_upload(bid: str, req: Request):
    """把人工准备好的产物放进某个任务名下（跨机器迁移一份备份时用）。

    文件名由服务端补 `bk<id>-` 前缀，恢复与删除才认得出它属于哪个任务。
    """
    if r := need_admin(req):
        return r
    if not bid.isdigit():
        return JSONResponse(status_code=400, content={"error": "任务 id 要是数字"})
    try:
        form = await req.form()
        f = form.get("file")
        if not hasattr(f, "read"):
            return JSONResponse(status_code=400, content={"error": "没有收到文件"})
        data = await f.read()
        overwrite = str(form.get("overwrite") or "") in {"1", "true", "yes"}
        r = await run_in_threadpool(
            backup_service.save_upload, int(bid), str(getattr(f, "filename", "") or ""), data, overwrite
        )
        await aio_db.audit(req.state.cp_sub, "backup:upload", f"{bid}:{r['name']}")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/backups")
async def backups_create(req: Request):
    body = await _body(req)
    try:
        b = await backup_service.create_backup(body)
        await aio_db.audit(req.state.cp_sub, "backup:create", f"{body.get('kind')}:{body.get('target')}")
        return b
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.patch("/api/backups/{bid}")
async def backups_update(bid: str, req: Request):
    body = await _body(req)
    try:
        r = await backup_service.update_backup(int(bid), body)
        # 计划改成 manual 就等于"从此不再自动备份"，这类变更不记审计事后查不到是谁改的
        await aio_db.audit(req.state.cp_sub, "backup:update", bid)
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.delete("/api/backups/{bid}")
async def backups_delete(bid: str, req: Request):
    try:
        await backup_service.delete_backup(int(bid))
        await aio_db.audit(req.state.cp_sub, "backup:delete", bid)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/backups/{bid}/run")
async def backups_run(bid: str, req: Request):
    try:
        r = await backup_service.run_backup_now(int(bid))
        # 带 req：既是为了记"谁手动跑了备份"，也是因为不写审计的写接口
        # 在审计页面上等于没发生过。失败也记一条并带上退出码。
        await aio_db.audit(req.state.cp_sub, "backup:run", f"{bid}(code={r['code']})")
        return {"code": r["code"], "log": r["out"]}
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "backup:run", f"{bid}(失败: {e})")
        return err_json(e, 500)
