from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import aio_db
from ..deps import need_admin
from ..errors import err_json
from ..services import backup_service, cloud_backup
from ..util import redact_creds

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
        # 异地目标同理——目标是哪一个、挂在哪个桶上属于"副本落在哪"，
        # 而只读账号看这条列表要回答的只是"这个任务有没有异地副本"（下面那两个键
        # 换成一个布尔位，界面上的"已配异地"标签照样成立）。
        for r in rows:
            r.pop("dir", None)
            r["cloud_linked"] = bool(r.pop("cloud_target_id", None))
            r.pop("cloud_target_name", None)
    return rows


@router.get("/api/backups/targets")
async def backups_targets_list(req: Request):
    """异地备份目标列表（不含 Secret）。放在 /{bid} 之前，避免 "targets" 被当成任务 id。"""
    if r := need_admin(req):
        return r
    return await run_in_threadpool(cloud_backup.list_targets)


@router.post("/api/backups/targets")
async def backups_targets_create(req: Request):
    if r := need_admin(req):
        return r
    body = await _body(req)
    try:
        t = await run_in_threadpool(cloud_backup.create_target, body)
        await aio_db.audit(req.state.cp_sub, "backup:target:create", str(t["name"]))
        return t
    except Exception as e:  # noqa: BLE001
        # 只记目标名：报错正文里可能带 endpoint，而 create 失败常见的正是"这个桶名不对"
        await aio_db.audit(req.state.cp_sub, "backup:target:create", f"(失败: {redact_creds(str(e))[:120]})")
        return err_json(e)


@router.patch("/api/backups/targets/{tid}")
async def backups_targets_update(tid: str, req: Request):
    if r := need_admin(req):
        return r
    if not tid.isdigit():
        return JSONResponse(status_code=400, content={"error": "目标 id 要是数字"})
    body = await _body(req)
    try:
        t = await run_in_threadpool(cloud_backup.update_target, int(tid), body)
        # 改动的字段名要记下来（尤其"有没有换凭据"），但不记任何值
        await aio_db.audit(req.state.cp_sub, "backup:target:update",
                           f"{tid}({','.join(sorted(k for k in body if k != 'secret_key'))}"
                           f"{'，含新 Secret Key' if str(body.get('secret_key') or '') else ''})")
        return t
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.delete("/api/backups/targets/{tid}")
async def backups_targets_delete(tid: str, req: Request):
    if r := need_admin(req):
        return r
    if not tid.isdigit():
        return JSONResponse(status_code=400, content={"error": "目标 id 要是数字"})
    try:
        r = await run_in_threadpool(cloud_backup.delete_target, int(tid))
        await aio_db.audit(req.state.cp_sub, "backup:target:delete", f"{tid}(远端对象未删除)")
        return r
    except Exception as e:  # noqa: BLE001
        return err_json(e)


@router.post("/api/backups/targets/{tid}/test")
async def backups_targets_test(tid: str, req: Request):
    """连通性测试：桶在不在、凭据对不对、我们写的键看不看得见。只读，不动数据。"""
    if r := need_admin(req):
        return r
    if not tid.isdigit():
        return JSONResponse(status_code=400, content={"error": "目标 id 要是数字"})
    try:
        res = await run_in_threadpool(cloud_backup.probe, int(tid))
        await aio_db.audit(req.state.cp_sub, "backup:target:test", str(res["target"]))
        return res
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "backup:target:test", f"{tid}(失败: {redact_creds(str(e))[:120]})")
        return err_json(e)


@router.post("/api/backups/{bid}/push")
async def backups_push(bid: str, req: Request):
    """把这个任务尚未推上去的产物补推到异地（已推过的不重传）。

    `target_id` 可省，缺省就用任务自己关联的那个目标；给了就必须是数字——
    把 "abc" 一路传进 `int()` 只会变成一个看不出所以然的 500。
    """
    if r := need_admin(req):
        return r
    if not bid.isdigit():
        return JSONResponse(status_code=400, content={"error": "任务 id 要是数字"})
    body = await _body(req)
    tid = body.get("target_id")
    if tid is not None and not str(tid).isdigit():
        return JSONResponse(status_code=400, content={"error": "target_id 要是数字"})
    try:
        res = await run_in_threadpool(cloud_backup.push_now, int(bid), int(tid) if tid else 0)
        await aio_db.audit(req.state.cp_sub, "backup:push",
                           f"{bid}({len(res.get('pushed') or [])} 份，删 {len(res.get('pruned') or [])} 份)")
        return res
    except Exception as e:  # noqa: BLE001
        # 失败也要留痕：这条路上"没推上去"最容易悄悄发生（对端换了密钥、桶被删）
        await aio_db.audit(req.state.cp_sub, "backup:push", f"{bid}(失败: {redact_creds(str(e))[:120]})")
        return err_json(e, 500)


@router.get("/api/backups/files")
async def backups_files(req: Request):
    """全部备份产物平铺（备份文件管理页）。放在 /{bid} 之前，避免被当成 id。"""
    res = await run_in_threadpool(backup_service.list_files)
    if req.state.cp_role != "admin":
        res.pop("dir", None)
    return res


@router.get("/api/backups/{bid}/cloud-files")
async def backups_cloud_files(bid: str, req: Request):
    """这条任务在异地桶里有哪几份产物、本机有没有同一份。只读，不动桶里的对象。

    `target=` 必填而不缺省：一条任务关联的目标可能被换成另一个桶，"看哪个桶"这件事
    必须由点进来的人说清楚，否则界面写着目标 A、列出来的却是 B 的对象。
    """
    if r := need_admin(req):
        return r
    if not bid.isdigit():
        return JSONResponse(status_code=400, content={"error": "任务 id 要是数字"})
    tid = req.query_params.get("target") or ""
    if not tid.isdigit():
        return JSONResponse(status_code=400, content={"error": "要用 target= 指定异地目标 id"})
    try:
        return await run_in_threadpool(cloud_backup.list_for_job, int(tid), int(bid))
    except Exception as e:  # noqa: BLE001
        return err_json(e, 500)


@router.post("/api/backups/{bid}/cloud-pull")
async def backups_cloud_pull(bid: str, req: Request):
    """把远端的一份产物拉回本机备份目录（本机同名已存在则拒绝，不覆盖）。

    拉回来的目的就是恢复，所以这一步要留审计：桶里那一份还在，本机多出的是副本，
    出事时得能分清"谁在什么时候把它搬到这台机器上的"。
    """
    if r := need_admin(req):
        return r
    if not bid.isdigit():
        return JSONResponse(status_code=400, content={"error": "任务 id 要是数字"})
    body = await _body(req)
    tid, name = body.get("target"), str(body.get("name") or "")
    if not str(tid or "").isdigit():
        return JSONResponse(status_code=400, content={"error": "target 要是异地目标 id"})
    if not name:
        return JSONResponse(status_code=400, content={"error": "要指定拉回哪个产物（name）"})
    try:
        res = await run_in_threadpool(cloud_backup.pull_one, int(tid), int(bid), name)
        await aio_db.audit(req.state.cp_sub, "backup:cloud-pull", f"{bid}({res['name']})")
        return res
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "backup:cloud-pull",
                           f"{bid}(失败: {redact_creds(str(e))[:120]})")
        return err_json(e, 500)


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
        # runner 的 stdout 是 pg_dump/tar 的原文，失败行里可能带出连接串里的口令；
        # 定时任务侧（cron_service）早已统一遮过，这个手动入口是同一个口径。
        return {"code": r["code"], "log": redact_creds(r["out"])}
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "backup:run", f"{bid}(失败: {e})")
        return err_json(e, 500)
