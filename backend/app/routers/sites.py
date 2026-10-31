"""网站与证书总览接口。

职责边界：nginx 的解析/写入/回滚全部委托 nginx_ops，站点归属与纳管登记委托
site_claim_service，certbot 续期委托它已有的 renew_certs；本文件只做「把请求接住、
把错误翻译成人话、写审计」，不实现第二套 nginx 逻辑，也不在这里猜域名归谁。
"""

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import aio_db
from ..deps import need_admin as _need_admin
from ..errors import err_json
from ..services import cert_ops, nginx_ops, site_audit_service, site_claim_service
from ..services.site_claim_service import PANEL_LABEL
from ..util import clip_text, redact_creds

router = APIRouter()


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


# nginx/certbot 的报错原文里满是 /etc/nginx/conf.d/xxx.conf 这种绝对路径。
# 面板以 root 跑，本机目录结构本身就是攻击面信息，出接口前统一换成文件名。
# 前导位置用否定预查收口：`https://host/x.git` 里的 `//` 与 `example.com/x.git`
# 不是本机绝对路径，误当路径抹掉会把可诊断的仓库/主机信息也一起吃掉。
_ABS_PATH_RE = re.compile(r"(?<![\w./:%-])/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")


def _mask(text: str) -> str:
    """绝对路径 → 只留最后一段文件名。"""
    return _ABS_PATH_RE.sub(lambda m: m.group(0).rsplit("/", 1)[-1], text or "")


def _cmd_error(exc: Exception) -> str:
    """把子进程/校验异常翻译成中文短消息，且不泄露本机路径。"""
    raw = str(exc).strip() or exc.__class__.__name__
    detail = clip_text(_mask(redact_creds(raw)), 400, what="错误信息")
    low = raw.lower()
    if "配置已删除" in raw:
        # remove_vhost 已经把状态讲清楚了（文件删了、面板没重载）。这里不能再套
        # "已回滚该文件"的模板：删除场景没有回滚可言，说反了用户会以为旧配置还在。
        return detail
    if "reload" in low:
        return f"nginx 配置校验通过，但重载失败（服务可能未运行）：{detail}"
    if "test failed" in low or "emerg" in low:
        return f"nginx 配置校验未通过，面板已回滚该文件：{detail}"
    return f"操作失败：{detail}"


@router.get("/api/sites")
async def sites_list():
    """站点总览：nginx 里实际加载的域名，join 纳管登记表与 apps 表。

    归属取自 site_claim_service 的登记表（批次五七）：过去这里按「反代端口等于哪个应用的端口」
    猜归属，共用端口与端口登记不符两种情况都会猜错，于是只能把判不出的都标成未纳管。
    判不出归属的那些现在带着 `suggest_reason` 返回理由，前端逐行说清"为什么认不出来、
    要怎么才认得出"，而不是给一个看不出所以然的标签。
    nginx 缺失时返回空列表 + reason，不编数据。
    """
    rows, reason = await site_claim_service.sites_rows()
    return {"sites": rows, "reason": reason}


@router.post("/api/sites/claim")
async def site_claim(req: Request):
    """纳管一个域名：登记归属 + 认领它所在的配置文件。**不写 nginx、不 reload**。

    `app_id` 可省——省下面板按判定结果落（面板入口会落成 kind=panel）；判定给不出归属时
    如实报错并说明该怎么办，不替管理员猜一个应用。
    """
    if r := _need_admin(req):
        return r
    body = await _body(req)
    domain = str(body.get("domain") or "").strip()
    if not domain:
        return JSONResponse(status_code=400, content={"error": "缺少 domain"})
    app_id = body.get("app_id")
    if app_id in (None, ""):
        app_id = None
    elif not str(app_id).isdigit():
        return JSONResponse(status_code=400, content={"error": "app_id 要是数字"})
    try:
        res = await site_claim_service.claim(domain, int(app_id) if app_id is not None else None)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "site:claim", f"{domain}(失败: {_mask(redact_creds(str(e)))[:120]})")
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "site:claim",
                       f"{domain} → {res['app'] or PANEL_LABEL}（{res['file'] or '未认领文件'}）")
    return {"ok": True, **res}


@router.post("/api/sites/unclaim")
async def site_unclaim(req: Request):
    """取消纳管：只删那一行登记，nginx 的配置一个字节都不动，域名照旧服务。"""
    if r := _need_admin(req):
        return r
    body = await _body(req)
    domain = str(body.get("domain") or "").strip()
    if not domain:
        return JSONResponse(status_code=400, content={"error": "缺少 domain"})
    try:
        res = await site_claim_service.unclaim(domain)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "site:unclaim", f"{domain}(失败: {_mask(redact_creds(str(e)))[:120]})")
        return err_json(e)
    # removed=False 也要写审计：页面上的按钮只给已纳管的行，点了却发现没有登记，
    # 说明登记表和 nginx 已经对不上，这是要查的事，不是可以静默成功的空操作。
    await aio_db.audit(req.state.cp_sub, "site:unclaim",
                       f"{domain}（{'已删除登记' if res['removed'] else '本就没有登记'}）")
    return {"ok": True, **res}


@router.post("/api/sites/claim-all")
async def site_claim_all(req: Request):
    """批量纳管：判得出归属的逐条落登记，判不出的原样列出理由，一个都不猜。

    已经纳管过的域名不在这一轮里重复写：登记表里那条可能就是管理员手工指定的应用，
    批量"全部纳管"把它改写回按端口猜出来的结果，等于把人的判断覆盖成机器的判断。
    """
    if r := _need_admin(req):
        return r
    try:
        res = await site_claim_service.claim_all()
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "site:claim-all", f"(失败: {_mask(redact_creds(str(e)))[:120]})")
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "site:claim-all",
                       f"纳管 {len(res['claimed'])} 个，判不出归属 {len(res['skipped'])} 个")
    return {"ok": True, **res}


@router.get("/api/site-audit")
async def site_audit(refresh: int = 0):
    """站点链路对账：只读探测，viewer 也能看；reload、改 vhost、签证书一律回终端手工做。

    载荷里只有文件名，没有 /etc/nginx 下的绝对路径——本机目录结构本身就是攻击面信息。
    """
    return await site_audit_service.audit(refresh=bool(refresh))


@router.get("/api/sites/{domain}/config")
async def site_config(domain: str, req: Request):
    """读某个域名的 vhost 原文（仅管理员：配置里可能有内部端口与调试注释）。"""
    if r := _need_admin(req):
        return r
    d = (domain or "").strip().lower()
    if not d or "/" in d:
        return JSONResponse(status_code=400, content={"error": "域名不合法"})
    hits = await asyncio.to_thread(nginx_ops.find_app_configs, None, d)
    if not hits:
        return JSONResponse(status_code=404, content={"error": "未找到该域名的 nginx 配置"})
    hit = hits[0]
    return {
        "domain": d,
        # 只回文件名：绝对路径对前端没用，却是本机目录结构泄露
        "file": Path(hit["file"]).name,
        "content": hit["content"],
        "analysis": hit["analysis"],
    }


@router.post("/api/sites/{app_id}/apply")
async def site_apply(app_id: int, req: Request):
    """按 apps 表的域名/端口重新渲染并套用 vhost（委托 nginx_ops，含 nginx -t 回滚）。"""
    if r := _need_admin(req):
        return r
    app = await aio_db.query_one("SELECT * FROM apps WHERE id=?", (app_id,))
    if not app:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    if not app["domain"]:
        return JSONResponse(status_code=400, content={"error": "该应用未配置域名，无法生成反向代理"})
    if app["port"] is None:
        return JSONResponse(status_code=400, content={"error": "该应用未固定端口，请先在应用里填监听端口"})
    spec = {"name": app["name"], "domain": app["domain"], "port": app["port"]}
    err = None
    try:
        await nginx_ops.apply_vhost(spec)
    except Exception as e:  # noqa: BLE001
        err = _cmd_error(e)
    finally:
        # apply_vhost 只在成功路径失效缓存；失败时文件也可能已被写过一次，
        # 这里无条件失效，域名映射最多 10 秒自愈，不会长期显示旧端口。
        nginx_ops.invalidate_domain_cache()
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    await aio_db.audit(req.state.cp_sub, "site:apply", app["name"])
    return {"ok": True, "file": Path(nginx_ops.vhost_path(app["name"])).name}


@router.delete("/api/sites/{app_id}/vhost")
async def site_remove(app_id: int, req: Request):
    """拆掉该应用的 vhost（应用记录保留）。"""
    if r := _need_admin(req):
        return r
    app = await aio_db.query_one("SELECT id,name FROM apps WHERE id=?", (app_id,))
    if not app:
        return JSONResponse(status_code=404, content={"error": "应用不存在"})
    # removed 只能来自 remove_vhost 自己的返回值：这里以前额外 stat 一次
    # （Path(vhost_path(name)).exists()），同一个事实两个读者，而第二个读者把 PermissionError
    # 折成 False，读不到的那份配置会被说成「该应用没有由面板生成的 vhost」。
    err = None
    removed = False
    try:
        removed = await nginx_ops.remove_vhost(app["name"]) == "removed"
    except Exception as e:  # noqa: BLE001
        err = _cmd_error(e)
    finally:
        nginx_ops.invalidate_domain_cache()
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    await aio_db.audit(req.state.cp_sub, "site:remove", app["name"])
    return {"ok": True, "removed": removed, "error": None}


@router.get("/api/ssl/certs")
async def ssl_certs():
    """证书清单 + 续期定时器状态。两个来源都拿不到时 certs 为空并附 reason。"""
    data, timer = await asyncio.gather(cert_ops.list_certs(), cert_ops.timer_status())
    earliest = cert_ops.earliest_expiry(data["certs"])
    return {
        **data,
        "timer": timer,
        "earliest": {"name": earliest["name"], "days_left": earliest["days_left"], "status": earliest["status"]}
        if earliest else None,
        "hint": cert_ops.renewal_hint(timer, data["certs"]),
    }


@router.post("/api/ssl/certs/{name}/renew")
async def ssl_renew(name: str, req: Request):
    """手动触发一次续期，委托 nginx_ops.renew_certs（面板既有的实现）。

    注意其语义是 `certbot renew`（检查全部 lineage、只续该续的），不是只续 {name}：
    certbot 的 --cert-name 需要额外的参数拼装与错误处理，而仓库规则是复用已有实现，
    所以这里把 name 记进审计用于定位"是谁为哪张证书点的按钮"。
    """
    if r := _need_admin(req):
        return r
    if not cert_ops.valid_cert_name(name):
        return JSONResponse(status_code=400, content={"error": "证书名称不合法"})
    try:
        res = await nginx_ops.renew_certs()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": _cmd_error(e)})
    # 审计写在执行之后：续期失败也先记一条"已续期"，事后无法区分成功与失败
    # detail 除了 name（是谁为哪张证书点的按钮）还带上这次结论：另一个续期入口
    # （/api/system/certbot-renew）记的就是 summary，两边都只记名字的话，
    # 事后翻审计仍然分不清这一次是"续到了"、"没到期"还是"清单根本没读到"。
    await aio_db.audit(req.state.cp_sub, "site:renew", f"{name}：{res['summary']}")
    # summary/renewed 说明"这次到底续到了没有"：certbot 对没到期的证书同样返回 0，
    # 只回一句"续期已执行"用户会以为证书换新了，实际上什么都没发生。
    return {
        "ok": True,
        "log": clip_text(_mask(redact_creds(res["log"])), 4000, what="certbot 输出"),
        "summary": res["summary"],
        "renewed": res["renewed"],
        "reloaded": res["reloaded"],
        # 与 certbot-renew 接口同一字段：False 说明这次是"没读到清单"而非"确实没到期"，
        # summary 已如实改写，这里再把判据原样带出去，别让两个续期入口对同一事实各说各话。
        "confirmed": res.get("confirmed", True),
    }
