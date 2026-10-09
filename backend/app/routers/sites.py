"""网站与证书总览接口。

职责边界：nginx 的解析/写入/回滚全部委托 nginx_ops，certbot 续期委托它已有的 renew_certs，
本文件只做「读 + join + 把错误翻译成人话」，不再实现第二套 nginx 逻辑。
"""

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import aio_db, config
from ..deps import need_admin as _need_admin
from ..services import apps_service, cert_ops, nginx_ops, systemd_ops
from ..util import redact_creds

router = APIRouter()

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
    detail = _mask(redact_creds(raw))[:400]
    low = raw.lower()
    if "reload" in low:
        return f"nginx 配置校验通过，但重载失败（服务可能未运行）：{detail}"
    if "test failed" in low or "emerg" in low:
        return f"nginx 配置校验未通过，面板已回滚该文件：{detail}"
    return f"操作失败：{detail}"


def _conf_dirs_available() -> bool:
    """nginx 装没装：只看配置目录是否存在（不存在就别把空列表伪装成「没有站点」）。"""
    return any(Path(d).is_dir() for d in config.NGINX_CONF_DIRS)


def _scan_nginx_sites() -> list[dict]:
    """同步扫描 nginx 配置文件，每个 server_name 出一条原始站点记录。

    复用 nginx_ops 的目录列举与 analyze_config：域名/端口/SSL/WebSocket/跳转的判定
    只应该有一份实现，否则面板两处对同一个文件的理解会分叉。
    """
    out = []
    for path, fname in nginx_ops._iter_conf_files():
        try:
            content = Path(path).read_text(errors="replace")
        except OSError:
            continue
        a = nginx_ops.analyze_config(content)
        if not a["serverNames"]:
            continue
        out.append({
            "file": fname,
            "domains": a["serverNames"],
            "https": a["ssl"],
            "redirect": a["httpsRedirect"],
            "websocket": a["websocket"],
            "ports": a["proxyPorts"],
        })
    return out


def _app_index(rows: list[dict]) -> tuple[dict, dict]:
    """域名 → 应用、端口 → 应用。端口表要剔掉被多个应用共用的端口：
    共用时按端口归属会张冠李戴，宁可显示成「未纳管的手工配置」。"""
    by_domain = {}
    port_count: dict[int, int] = {}
    by_port: dict[int, dict] = {}
    for r in rows:
        d = (r.get("domain") or "").strip().lower()
        if d:
            by_domain.setdefault(d, r)
        p = r.get("port")
        if isinstance(p, int):
            port_count[p] = port_count.get(p, 0) + 1
            by_port.setdefault(p, r)
    return by_domain, {p: r for p, r in by_port.items() if port_count[p] == 1}


def _merge_site(seen: dict, row: dict) -> None:
    """同一个域名可能同时出现在手工配置与 panel-*.conf 里：
    保留信息更多的那条（HTTPS 优先，其次面板管理的文件），避免列表出现两行同名站点。"""
    old = seen.get(row["domain"])
    if old is None:
        seen[row["domain"]] = row
        return
    better = (bool(row["https"]), row["file"].startswith("panel-"))
    worse = (bool(old["https"]), old["file"].startswith("panel-"))
    if better > worse:
        seen[row["domain"]] = row


async def _site_rows() -> tuple[list, str | None]:
    if not _conf_dirs_available():
        return [], "未找到 nginx 配置目录（本机可能未安装 nginx，或 CP_NGINX_CONF_DIRS 指向了不存在的路径）"

    scanned = await asyncio.to_thread(_scan_nginx_sites)
    apps = await aio_db.query("SELECT id,name,port,domain,unit_override FROM apps")
    by_domain, by_port = _app_index(apps)
    units = {apps_service.unit_name(a): a for a in apps}
    states = await systemd_ops.unit_states(list(units))

    seen: dict = {}
    for entry in scanned:
        for domain in entry["domains"]:
            app = by_domain.get(domain.lower())
            if app is None and len(entry["ports"]) == 1:
                app = by_port.get(entry["ports"][0])
            port = app["port"] if app and app["port"] is not None else (entry["ports"][0] if entry["ports"] else None)
            active = None
            if app is not None:
                active = bool((states.get(apps_service.unit_name(app)) or {}).get("active"))
            _merge_site(seen, {
                "domain": domain,
                "app": app["name"] if app else None,
                "app_id": app["id"] if app else None,
                "port": port,
                "https": entry["https"],
                "redirect": entry["redirect"],
                "websocket": entry["websocket"],
                "file": entry["file"],
                "active": active,
            })
    return sorted(seen.values(), key=lambda r: (r["app"] is None, r["app"] or r["domain"])), None


@router.get("/api/sites")
async def sites_list():
    """站点总览：nginx 域名映射 join apps 表。nginx 缺失时返回空列表 + reason，不编数据。"""
    rows, reason = await _site_rows()
    return {"sites": rows, "reason": reason}


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
    existed = Path(nginx_ops.vhost_path(app["name"])).exists()
    err = None
    try:
        await nginx_ops.remove_vhost(app["name"])
    except Exception as e:  # noqa: BLE001
        err = _cmd_error(e)
    finally:
        nginx_ops.invalidate_domain_cache()
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    await aio_db.audit(req.state.cp_sub, "site:remove", app["name"])
    if not existed:
        return {"ok": True, "removed": False, "error": None}
    return {"ok": True, "removed": True, "error": None}


@router.get("/api/ssl/certs")
async def ssl_certs():
    """证书清单 + 续期定时器状态。两个来源都拿不到时 certs 为空并附 reason。"""
    data, timer = await asyncio.gather(cert_ops.list_certs(), cert_ops.timer_status())
    earliest = cert_ops.earliest_expiry(data["certs"])
    return {
        **data,
        "timer": timer,
        "expiring_days": cert_ops.EXPIRING_DAYS,
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
        log = await nginx_ops.renew_certs()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": _cmd_error(e)})
    # 审计写在执行之后：续期失败也先记一条"已续期"，事后无法区分成功与失败
    await aio_db.audit(req.state.cp_sub, "site:renew", name)
    return {"ok": True, "log": _mask(redact_creds(log))[:4000]}
