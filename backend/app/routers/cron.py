"""定时任务中心的 HTTP 契约。

错误一律 `{"error": "中文提示"}`：表达式写错、命令不在白名单内都是 400，
id 不存在是 404。服务层抛的是 PanelError/ValueError，这里只负责转成响应体，
不在路由里重复实现校验——两处校验迟早会漂移成一套放行另一处拦不住。
"""

from fastapi import APIRouter, Request

from .. import aio_db
from ..deps import need_admin
from ..errors import BadInput, err_json
from ..services import cron_service

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


def _tid(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise BadInput("任务 id 必须是数字") from None


@router.get("/api/cron/preview")
async def cron_preview(req: Request):
    """把表达式翻译成中文说明与 OnCalendar，不写任何东西。

    必须声明在 /{tid} 之前，否则 preview 会被当成 id 吃掉。
    """
    if r := need_admin(req):
        return r
    try:
        return cron_service.preview(req.query_params.get("expr") or "")
    except ValueError as e:
        return err_json(e)


@router.get("/api/cron/options")
async def cron_options(req: Request):
    """白名单与可重启 unit 由服务端给出，前端不抄常量（抄了就会和后端漂移）。"""
    if r := need_admin(req):
        return r
    return cron_service.command_options()


@router.get("/api/cron")
async def cron_list(req: Request):
    # 列表里带每条任务的完整命令行与最近输出，属于运维信息，按现有口径只给 admin
    if r := need_admin(req):
        return r
    return {"items": await cron_service.list_tasks()}


@router.post("/api/cron")
async def cron_create(req: Request):
    if r := need_admin(req):
        return r
    body = await _body(req)
    try:
        row = await cron_service.create_task(body)
    except Exception as e:  # noqa: BLE001
        return err_json(e, log_tag="cron:create")
    # 审计只记任务名：argv 里可能带 unit 名与业务参数，出事时按名字回查就够用
    await aio_db.audit(req.state.cp_sub, "cron:create", str(body.get("name") or ""))
    return row


@router.patch("/api/cron/{tid}")
async def cron_update(tid: str, req: Request):
    if r := need_admin(req):
        return r
    body = await _body(req)
    try:
        row = await cron_service.update_task(_tid(tid), body)
    except Exception as e:  # noqa: BLE001
        return err_json(e, log_tag="cron:update")
    await aio_db.audit(req.state.cp_sub, "cron:update", row["name"])
    return row


@router.delete("/api/cron/{tid}")
async def cron_delete(tid: str, req: Request):
    if r := need_admin(req):
        return r
    try:
        name = await cron_service.delete_task(_tid(tid))
    except Exception as e:  # noqa: BLE001
        return err_json(e, log_tag="cron:delete")
    await aio_db.audit(req.state.cp_sub, "cron:delete", name)
    return {"ok": True, "name": name}


@router.post("/api/cron/{tid}/run")
async def cron_run(tid: str, req: Request):
    if r := need_admin(req):
        return r
    # 立即执行也记审计（服务层带任务名与退出码）：手动跑过一次和 timer 跑过是完全不同的事
    try:
        return await cron_service.run_now(_tid(tid), actor=req.state.cp_sub)
    except Exception as e:  # noqa: BLE001
        return err_json(e, log_tag="cron:run")


@router.get("/api/cron/{tid}/log")
async def cron_log(tid: str, req: Request):
    """最近一次执行的输出。timer 唤醒的那一轮不经过面板，只在 journald 里，
    列表带回的 log_tail 是上一次手动执行的，两者不是一回事，所以单独开一个读接口。"""
    if r := need_admin(req):
        return r
    try:
        return await cron_service.run_log(_tid(tid))
    except Exception as e:  # noqa: BLE001
        return err_json(e, log_tag="cron:log")
