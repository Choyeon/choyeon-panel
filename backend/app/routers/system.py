import asyncio
import json
import re
import time
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import aio_db, security
from ..deps import need_admin
from ..errors import BadInput, err_json, public_message
from ..services import (
    access_stats,
    alerts_service,
    ban_service,
    firewall_service,
    site_claim_service,
    system_metrics,
    systemd_ops,
    web_logs,
)
from ..util import HTTP_LOG_LINES_MAX, LOG_LINES_DEFAULT, is_unit, log_ladder, parse_lines, redact_creds, run

router = APIRouter()

# 长连接（日志 SSE）的身份复验间隔，与网页终端 terminal._RECHECK_SEC 同一套理由。
_RECHECK_SEC = 60


@router.get("/api/system/info")
async def sys_info():
    return await system_metrics.info()


@router.get("/api/system/stats")
async def sys_stats():
    data = await system_metrics.snapshot()
    # 概览那四块仪表的配色跟着设置页的线走：仪表/自检/告警/存储页过去各写一份 80、90、75、90，
    # 把线调到 70 之后告警在响，概览的磁盘仪表仍是品牌色。CPU 与内存同理——
    # 仪表上写着 70/85、80/90 两档，而设置页配的是另外两个数，改了配置仪表不动。
    dt = alerts_service.disk_thresholds()
    data["diskWarnPct"], data["diskCritPct"] = dt["warn"], dt["crit"]
    rt = alerts_service.resource_thresholds()
    data["cpuWarnPct"], data["cpuCritPct"] = rt["cpu_warn"], rt["cpu_crit"]
    data["memWarnPct"], data["memCritPct"] = rt["mem_warn"], rt["mem_crit"]
    return data


@router.get("/api/system/overview")
async def sys_overview():
    """仪表盘「概览」条：一次请求拿齐四个计数，取代前端各页各自拉全表再数长度。

    站点数只扫 nginx 配置（纯文件读），不走站点页那条链路——后者还要查 apps 表与纳管登记表、
    并向 systemd 逐个问单元状态，那是打开网站页才需要的开销。
    域名集合用的是同一个读者（`site_claim_service.site_domains`），概览条与网站页不会各数一套。
    nginx 不在时如实返回 null + reason，前端显示「—」而不是 0。
    """
    apps = await aio_db.query_one("SELECT COUNT(*) c FROM apps")
    cron = await aio_db.query_one("SELECT COUNT(*) c, COALESCE(SUM(enabled),0) e FROM cron_task")
    backups = await aio_db.query_one("SELECT COUNT(*) c FROM backups WHERE enabled=1")
    alerts = await aio_db.query_one("SELECT COUNT(*) c FROM alerts_log WHERE acked=0")

    try:
        names, site_reason = await asyncio.to_thread(site_claim_service.site_domains)
        sites = None if site_reason else len(names)
    except Exception as e:  # noqa: BLE001 - 计数失败不该让整条概览消失
        sites, site_reason = None, f"站点扫描失败：{type(e).__name__}"

    return {
        "apps": int(apps["c"]),
        "cron": int(cron["c"]),
        "cronEnabled": int(cron["e"] or 0),
        "backups": int(backups["c"]),
        "openAlerts": int(alerts["c"]),
        "sites": sites,
        "sitesReason": site_reason,
    }


@router.get("/api/system/processes")
async def sys_processes(req: Request):
    """Top 进程表。整表扫描比 stats 贵，服务层带 3 秒缓存。"""
    try:
        n = max(1, min(int(req.query_params.get("limit") or 12), 50))
    except ValueError:
        n = 12
    return {"items": await system_metrics.top_processes(n)}


@router.get("/api/system/services")
async def services():
    return await systemd_ops.list_services()


@router.get("/api/system/unit-files")
async def unit_files():
    """unit → 开机自启状态（enabled/disabled/masked/…）。

    单独一条而不并进 /system/services：list-units 的输出里没有这一列，补上它就得每个
    周期多起一次 list-unit-files，而 Dashboard 每 15s 也在打 /system/services。
    """
    return await systemd_ops.unit_files()


@router.post("/api/system/services/{unit}")
async def service_action(unit: str, req: Request):
    if not is_unit(unit):
        return JSONResponse(status_code=400, content={"error": "unit 名不合法"})
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    verb = (body or {}).get("verb", "")
    try:
        await systemd_ops.service_action(unit, verb)
        await aio_db.audit(req.state.cp_sub, f"service:{verb}", unit)
        st = await systemd_ops.unit_state(unit)
        # active 之外再带上 systemd 自己的 ActiveState：restart 成功不等于起来了，
        # "activating" 与 "failed" 对用户是两句话，前端 reload 之前至少能原样转述。
        out: dict = {"ok": True, "active": st["active"], "state": st["state"], "unitMissing": st["missing"]}
        if verb in ("enable", "disable"):
            # 前端「自启」按钮点完要当场变状态，不能等 30s 缓存过期
            out["enabled"] = (await systemd_ops.unit_files(force=True)).get(unit, "")
        return out
    except Exception as e:  # noqa: BLE001
        # systemctl 的报错常带 unit 路径与 errno，原样回给浏览器等于公开服务器结构
        return err_json(e, 500, log_tag="service:action")


async def _sse_journal(unit: str, lines: int, user: str = "", exp: float = 0.0):
    """journalctl -f 的 SSE 流。

    `user`/`exp` 是握手时那枚 token 的主体与到期时间：长连接只在 GET 那一刻验过一次身份，
    之后改密/降权/删号/票过期都不会惊动这条流，而它推的是**全机** journal。所以按
    _RECHECK_SEC 复验一次，对不上就收尾。留空表示"不复核"，只给单测用——两个路由
    都必须把 request.state 里的值传进来。
    """
    # 身份基线赶在起进程之前取。原来这两句是反的：create_subprocess_exec 与 try 之间
    # 夹着一个 await（session_identity 走 SQLite），它一抛——库锁、任务被取消都算——
    # journalctl -f 就成了没人认领的长命进程，finally 根本轮不到执行，而浏览器端
    # EventSource 断了就自动重连，每一次重连多留一个，直到把面板的 fd 吃光。
    base = await asyncio.to_thread(security.session_identity, user) if user else None
    until = time.monotonic() + _RECHECK_SEC
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso", "-f",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout = proc.stdout
        if stdout is None:  # stdout 未接管时直接结束，避免 AttributeError 打断 SSE 流
            return
        while True:
            try:
                raw = await asyncio.wait_for(stdout.readline(), timeout=25)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if not raw:
                break
            if base is not None and time.monotonic() >= until:
                until = time.monotonic() + _RECHECK_SEC
                if time.time() >= exp:
                    stale = True
                else:
                    try:
                        stale = await asyncio.to_thread(security.session_identity, user) != base
                    except Exception:  # noqa: BLE001
                        stale = True
                if stale:
                    with suppress(Exception):
                        await aio_db.audit(user, "sse:revoked")
                    yield f"data: {json.dumps('登录状态已变更，日志流已停止。请重新连接。')}\n\n"
                    return
            # 整机 journal 里漂着的东西不受我们控制：业务应用把口令写进启动命令、
            # 框架把连接串打 INFO 日志都会顺着这条流出去。日志检索（本文件下面一节）
            # 早就统一过 redact_creds，SSE 是唯一漏网的一条流，这里补上同一个口径。
            line = redact_creds(raw.decode(errors="replace").rstrip(chr(10)))
            yield f"data: {json.dumps(line)}\n\n"
    finally:
        # 客户端断开时进程可能已退出，kill 报 ProcessLookupError 属正常
        with suppress(ProcessLookupError):
            proc.kill()


def log_stream_response(unit: str, lines: int, user: str, exp: float) -> StreamingResponse:
    # 兜底的是"调用方忘了 parse_lines"，数字仍是接口那一档的上限（⑲B 收进 util；
    # 这里过去写着第二个 2000，上限一改就是两条不同的线）。
    return StreamingResponse(
        _sse_journal(unit, min(lines, HTTP_LOG_LINES_MAX), user, exp),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/api/system/services/{unit}/logs")
async def service_logs(unit: str, request: Request):
    # 与 /api/system/logs 同一口径：journal 里能读出别的服务的环境变量、请求 URL
    # 乃至口令，"viewer 只读"不等于"能读全机日志"，所以这里显式要求 admin。
    if r := need_admin(request):
        return r
    if not is_unit(unit):
        return JSONResponse(status_code=400, content={"error": "unit 名不合法"})
    lines = parse_lines(request.query_params.get("lines"))
    return log_stream_response(unit, lines, request.state.cp_sub, float(request.state.cp_exp or 0))


@router.get("/api/firewall")
async def firewall(req: Request):
    """防火墙状态与规则表（结构化 + 原文）。

    规则表等于一张"这台机器开了哪些端口、从哪些地址段能连"的地图，只读账号不需要它
    （防火墙页本来就只对 admin 显示），而拿到它的人可以是任何一个用 viewer 账号登录成功的人。
    `protected` 是"删掉会把自己锁在外面"的端口清单，前端据此把删除按钮置灰；这只是显示层
    的提示，写接口每次都会重新判定，不信任前端传来的那份状态。
    """
    if r := need_admin(req):
        return r
    return await firewall_service.read(request=req)


def _rule_desc(body: dict, outcome: str) -> str:
    """审计里的一句话：把请求体收成一行可审计文本，不接受未过滤的原样 dict。"""
    keys = ("action", "proto", "port", "port_to", "source", "comment", "rate")
    parts = [f"{k}={body.get(k)}" for k in keys if body.get(k) not in (None, "", False)]
    return " ".join(parts)[:200] + f" -> {outcome[:120]}"


@router.post("/api/firewall/rules")
async def firewall_add(req: Request):
    """新增放行/拒绝规则。拒绝理由（含自锁保护）原样回给前端，成功与失败都进审计。"""
    if r := need_admin(req):
        return r
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    tool = await firewall_service.detect()
    try:
        out = await firewall_service.add_rule(body or {}, tool, req)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "firewall:add", _rule_desc(body or {}, f"拒绝：{public_message(e)}"))
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "firewall:add", _rule_desc(body or {}, "已生效"))
    return out


@router.delete("/api/firewall/rules/{num}")
async def firewall_remove(num: int, req: Request):
    """按编号删除规则。`expect` 是前端那次列表里读到的签名，服务端会重新列表比对，
    比对不上就拒绝——编号会随其他规则的新增/删除移动，靠它寻址而不核对等于盲删。"""
    if r := need_admin(req):
        return r
    expect = req.query_params.get("expect", "")[:160]
    tool = await firewall_service.detect()
    try:
        out = await firewall_service.remove_rule(num, expect, tool, req)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "firewall:remove", f"num={num} -> 拒绝：{public_message(e)}")
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "firewall:remove", f"num={num} -> 已删除 {out.get('removed', '')}")
    return out


def _hits_only(raw) -> int:
    """`hits` 只用来写进备注，不参与任何判定：它是客户端上一次列表里看到的显示值，
    拿它做安全决策等于让浏览器替服务端决定"这个人是不是在爆破"。读不出就当 0。"""
    text = str(raw if raw is not None else "").strip()
    return max(0, min(int(text), ban_service.MAX_HITS)) if text.isdigit() else 0


async def _ban_body(req: Request) -> dict:
    """POST 的请求体：只认 `ip` 与可选的 `hits`，其余字段一律忽略（不接受调用方塞进来的动作）。"""
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    body = body if isinstance(body, dict) else {}
    return {"ip": str(body.get("ip") or "")[:64], "hits": _hits_only(body.get("hits"))}


@router.get("/api/bans")
async def bans_list(req: Request):
    """面板认得的封禁地址（备注 / 命名集合里带 choyeon-ban 标记的那些）。

    只读账号看不到：它和 `/api/firewall` 同一个理由——这张表等于"这台机器在挡谁"。
    `ok=false` 时 `reason` 说清是"没有防火墙"还是"这份表没读到"，前端据此锁住写操作。
    """
    if r := need_admin(req):
        return r
    return await ban_service.list_bans()


@router.post("/api/bans")
async def bans_add(req: Request):
    """封一个来源地址。成功与拒绝都进审计：这条动作挡的是别人，留痕比一般写操作更要紧。"""
    if r := need_admin(req):
        return r
    body = await _ban_body(req)
    ip, hits = body["ip"], body["hits"]
    try:
        out = await ban_service.ban(ip, req, hits)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "ban:add", f"{ip} -> 拒绝：{public_message(e)}")
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "ban:add", f"{ip} -> {out.get('detail', '')[:160]}")
    return out


@router.delete("/api/bans")
async def bans_remove(req: Request):
    """解封。ufw 那一路仍然走防火墙的按编号 + 签名删除（编号会在两次请求之间移动，
    靠它寻址而不核对签名等于盲删——和删放行规则是同一件事）。"""
    if r := need_admin(req):
        return r
    ip = req.query_params.get("ip", "")[:64]
    raw_num = req.query_params.get("num", "")
    num = int(raw_num) if raw_num.isdigit() else 0
    expect = req.query_params.get("expect", "")[:160]
    try:
        out = await ban_service.unban(ip, num, expect, req)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "ban:remove", f"{ip}#{num} -> 拒绝：{public_message(e)}")
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "ban:remove", f"{ip}#{num} -> {out.get('detail', '')[:160]}")
    return out


@router.post("/api/firewall/state")
async def firewall_state(req: Request):
    """启用/停用防火墙。启用前必须已经放行 SSH 与当前访问面板的端口，否则直接拒绝。"""
    if r := need_admin(req):
        return r
    body = await req.json() if "application/json" in (req.headers.get("content-type") or "") else {}
    raw = (body or {}).get("active")
    if not isinstance(raw, bool):
        # 缺字段或字符串以前会被 bool() 读成 False，于是"一次写歪的请求"就等于"把防火墙关掉"。
        # 关防火墙是这条路径上最危险的副作用，宁可拒绝，不猜意图。
        return err_json(BadInput("active 必须是 true 或 false"))
    active = raw
    verb = "启用" if active else "停用"
    tool = await firewall_service.detect()
    try:
        out = await firewall_service.set_enabled(active, tool, req)
    except Exception as e:  # noqa: BLE001
        await aio_db.audit(req.state.cp_sub, "firewall:state", f"{verb} -> 拒绝：{public_message(e)}")
        return err_json(e)
    await aio_db.audit(req.state.cp_sub, "firewall:state", f"{verb}成功")
    return out


# ------------------------------------------------------------------ 日志检索
# 这一节的三条纪律：argv 里绝不出现 shell、参数值先校验再交给 journalctl、
# 输出一律 redact_creds。日志检索是只读接口，但它能把整台机器的 journal 读出来，
# 所以按现有口径要求 admin（deps.need_admin），而不是靠 HTTP 方法猜。
PRIORITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
MAX_GREP = 200
MAX_ITEM_CHARS = 1500
_ABS_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(:\d{2})?$")
_REL_TIME_RE = re.compile(r"^(\d{1,6})(min|h|d)$")
_REL_SEC = {"min": 60, "h": 3600, "d": 86400}


def _resolve_time(raw: str | None, label: str) -> tuple[str | None, str | None]:
    """since/until → journalctl 要的绝对本地时间。

    相对写法（30min/6h/2d）在这里自己算成 `YYYY-MM-DD HH:MM:SS`：journalctl 对
    `-30min`、`30 minutes ago` 之类写法的接受度随版本变化，传绝对时间才和面板上
    显示的时刻是同一个值。
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    if _ABS_TIME_RE.match(text):
        return text.replace("T", " "), None
    m = _REL_TIME_RE.match(text)
    if not m:
        return None, f"{label} 只接受「YYYY-MM-DD HH:MM」或相对写法（如 30min、6h、2d）"
    n = int(m.group(1))
    if n < 1 or (m.group(2) == "d" and n > 365) or (m.group(2) != "d" and n > 9999):
        return None, f"{label} 的相对量级过大（最多 365 天）"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - n * _REL_SEC[m.group(2)])), None


def _bad(msg: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": msg})


def _journal_items(raw: str) -> list:
    """journalctl -o json 的逐行结果 → 前端要用的条目；解析不了一律按原文回退。"""
    items = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            items.append({"ts": "", "unit": "", "prio": -1, "text": redact_creds(line)[:MAX_ITEM_CHARS]})
            continue
        if not isinstance(e, dict):
            continue
        ts = ""
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(e["__REALTIME_TIMESTAMP"]) / 1_000_000))
        except (KeyError, TypeError, ValueError):
            ts = str(e.get("t", ""))[:32]
        text = e.get("MESSAGE") or ""
        if not isinstance(text, str):  # 二进制字段会被编码成对象，按空处理而不是贴出 repr
            text = ""
        prio = e.get("PRIORITY")
        items.append({
            "ts": ts,
            "unit": str(e.get("_SYSTEMD_UNIT") or e.get("SYSLOG_IDENTIFIER") or "")[:64],
            "prio": int(prio) if str(prio or "").isdigit() else -1,
            "text": redact_creds(str(text))[:MAX_ITEM_CHARS],
        })
    return items


@router.get("/api/system/log-options")
async def log_options(request: Request):
    """日志中心那份"看多少行"的清单：数字只有 `app/util.py` 一个出处。

    页面此前自己抄了 200/500/1000/2000 与默认 500，还把 nginx 尾读的字节上限写成
    "512KB" 抄进文案。三处数字各抄一遍，改一处就整体失真，而且失真很安静——
    下拉里那一档还在，服务端已经按自己的上限截断了。
    """
    if r := need_admin(request):
        return r
    return {
        "lines_default": LOG_LINES_DEFAULT,
        "lines_max": HTTP_LOG_LINES_MAX,
        "ladder": log_ladder(),
        "nginx_tail_bytes": web_logs.TAIL_BYTES,
        "stats_minutes": access_stats.WINDOW_LADDER,
        "stats_days": access_stats.DAY_LADDER,
    }


@router.get("/api/system/logs")
async def logs_search(request: Request):
    """journalctl 检索：unit / 时间段 / 关键词 / 级别 / 行数，全部走固定 argv。"""
    if r := need_admin(request):
        return r
    qp = request.query_params
    unit = (qp.get("unit") or "").strip()
    if unit and not is_unit(unit):
        return _bad("unit 名不合法：只能使用字母、数字、@ : . _ -，且必须以字母或数字开头")
    since, err = _resolve_time(qp.get("since"), "since")
    if err:
        return _bad(err)
    until, err = _resolve_time(qp.get("until"), "until")
    if err:
        return _bad(err)
    if since and until and since > until:
        return _bad("since 不能晚于 until")
    level = (qp.get("level") or "").strip()
    if level and level not in PRIORITIES:
        return _bad(f"level 只能是 {'、'.join(PRIORITIES)}")
    grep = qp.get("grep") or ""
    if len(grep) > MAX_GREP:
        return _bad(f"关键词最长 {MAX_GREP} 个字符")
    if any(c in grep for c in ("\n", "\r", "\x00")):
        return _bad("关键词不能包含换行或空字符")
    limit = parse_lines(qp.get("limit"))

    argv = ["--no-pager", "-o", "json", "-n", str(limit)]
    if unit:
        argv += ["-u", unit]
    if level:
        argv += ["-p", level]
    if since:
        argv += ["--since", since]
    if until:
        argv += ["--until", until]
    if grep:
        # -g 是 journalctl 自己的 PCRE 匹配，值作为独立 argv 元素传入：
        # 不拼进 shell，也就不存在注入；正则语法错误由 journalctl 报错，我们只转成提示。
        argv += ["-g", grep]
    r = await run("journalctl", argv, timeout=40)
    if r["code"] != 0:
        msg = redact_creds(r["out"])[:400]
        if grep and "regex" in msg.lower():
            return _bad("关键词的正则写法不被 journalctl 接受（PCRE），请简化后重试")
        return JSONResponse(status_code=502, content={"error": msg or "journalctl 执行失败"})
    items = _journal_items(r["out"])
    return {
        "items": items,
        "count": len(items),
        "truncated": len(items) >= limit,
        "filters": {"unit": unit, "since": since or "", "until": until or "", "level": level, "grep": grep,
                    "limit": limit},
    }


@router.get("/api/system/nginx-log")
async def nginx_log(request: Request):
    """tail 日志白名单（CP_LOG_ROOTS ∪ CP_FILE_ROOTS）下的 access/error 日志。

    journal 里没有的那部分——nginx 按站点写的文件日志——只能从这里读。
    来源发现、普通文件判定与序号口径都在 `web_logs`，`/api/system/nginx-stats` 用的是同一份列表。
    """
    if r := need_admin(request):
        return r
    qp = request.query_params
    lines = parse_lines(qp.get("lines"))
    filt = qp.get("filter") or ""
    if len(filt) > MAX_GREP:
        return _bad(f"过滤词最长 {MAX_GREP} 个字符")
    try:
        idx = int(qp.get("file") or 0)
    except ValueError:
        return _bad("file 参数是日志文件的序号（整数）")
    files = await run_in_threadpool(web_logs.nginx_log_files)
    if not files:
        return {
            "items": [],
            "files": [],
            "hint": web_logs.discovery_hint(),
        }
    if not 0 <= idx < len(files):
        return _bad(f"file 序号超出范围（可用 0-{len(files) - 1}）")
    picked = files[idx]
    tail = await run_in_threadpool(web_logs.tail_file, picked["path"], lines, filt, MAX_ITEM_CHARS)
    # 来源在清单里 ≠ 读得动（0640 root:adm 的日志只要目录可 traversal 就会被列出来）。
    # 读不动时如实带 readError 并把四个数说成"没读到"，不许画一张空表让人以为日志是空的。
    read_error = "" if tail.get("readable", True) else web_logs.readable_hint()
    return {
        "items": tail["items"],
        "count": len(tail["items"]),
        "matched": tail["matched"],
        "dropped": tail["dropped"],
        "window_truncated": tail["window_truncated"],
        "readError": read_error,
        "source": picked["label"],
        "files": [{"i": i, "label": f["label"], "kind": f["kind"], "site": f["site"]} for i, f in enumerate(files)],
        # 「这一页是不是全部」要同时看两把刀：行数上限裁掉的命中（dropped），和整个文件比
        # 单次读量大、连窗口都没读到底（window_truncated）。按返回条数判会把两者都说成完整答案。
        # 读不动是第三种事实，不是"没裁掉所以完整"。
        "truncated": bool(read_error) or tail["dropped"] > 0 or tail["window_truncated"],
    }


def _file_choices(files: list) -> list:
    """给浏览器的来源清单：只有序号、展示名与类型，没有绝对路径。"""
    return [{"i": i, "label": f["label"], "kind": f["kind"], "site": f["site"]} for i, f in enumerate(files)]


def _pick_access_source(files: list, want):
    """把 `?file=` 序号换成一个"能统计的访问日志"。

    返回（要直接回给浏览器的响应或 None, （序号, 文件）或 None）。分钟窗口和按天趋势必须
    共用这一段：两处各挑各的，"同一个 3 号文件"就会在两个页签里指两个来源。
    """
    if not files:
        return {"hint": web_logs.discovery_hint(), "files": [], "source": ""}, None
    if want is None:
        # 不带 file 时取第一个"能统计的"来源：页面的下拉要先有一次响应才有清单，
        # 而第一枪撞进 error.log 会变成 400，用户看到的是"点了统计却报错"。
        idx = next((i for i, f in enumerate(files) if f["kind"] == "access"), -1)
        if idx < 0:
            return {"hint": "白名单里只有错误日志，没有可统计的访问日志；给站点开一份 access.log，"
                            "或把它的目录加进 CP_LOG_ROOTS",
                    "files": _file_choices(files), "source": ""}, None
    else:
        idx = want
    if not 0 <= idx < len(files):
        return _bad(f"file 序号超出范围（可用 0-{len(files) - 1}）"), None
    picked = files[idx]
    if picked["kind"] != "access":
        return _bad(f"{picked['label']} 是错误日志，里面没有请求行可统计；"
                    f"可选的访问日志序号："
                    f"{', '.join(str(i) for i, f in enumerate(files) if f['kind'] == 'access') or '无'}"), None
    return None, (idx, picked)


def _file_index(qp):
    """解析 `?file=`；返回（要直接回给浏览器的响应或 None, 序号或 None）。"""
    raw = (qp.get("file") or "").strip()
    try:
        return None, (int(raw) if raw else None)
    except ValueError:
        return _bad("file 参数是日志文件的序号（整数）"), None


@router.get("/api/system/nginx-stats")
async def nginx_stats(request: Request):
    """访问日志流量统计：状态码分布、TOP 路径/客户端、方法占比与每分钟请求曲线。

    对标 1Panel/宝塔的「网站监控」，但不装第三方分析器：一次有界尾读 + 内存聚合，
    不落临时文件、不返回原始日志行。`file` 序号与 `/api/system/nginx-log` 同一份列表，
    所以「在日志页签看到的 3 号文件」和「统计页签的 3 号」是同一个来源。
    """
    if r := need_admin(request):
        return r
    qp = request.query_params
    bad, want = _file_index(qp)
    if bad is not None:
        return bad
    minutes = 60
    raw_minutes = (qp.get("minutes") or "").strip()
    if raw_minutes:
        try:
            minutes = int(raw_minutes)
        except ValueError:
            return _bad(f"minutes 参数是分钟数（整数，1-{access_stats.SERIES_MAX}）")
    if not 1 <= minutes <= access_stats.SERIES_MAX:
        return _bad(f"minutes 只能是 1-{access_stats.SERIES_MAX}（最多统计 24 小时）")
    files = await run_in_threadpool(web_logs.nginx_log_files)
    bad, picked_source = _pick_access_source(files, want)
    if bad is not None:
        return bad
    idx, picked = picked_source
    data = await run_in_threadpool(access_stats.collect, picked["path"], minutes)
    data["source"] = picked["label"]
    data["site"] = picked["site"]
    data["file"] = idx
    data["files"] = _file_choices(files)
    return data


@router.get("/api/system/nginx-daily")
async def nginx_daily(request: Request):
    """按天的访问量与上一周期环比：读轮替出来的历史世代（`.1`、`.2.gz`……）。

    分钟窗口只能回答"这一小时忙不忙"，"这个星期比上个工作日多还是少"要看轮替掉的那几份，
    而那一层正是 tail 故意不读的 gzip（解压完再截尾，读的是随机中段）。所以按天趋势单独走
    `web_logs.log_series()`：同一份白名单、同一个序号，只是换了一种读法，世代数与每份字节都有封顶。

    `days` 只接受 1-30，档位由 `/api/system/log-options` 的 `stats_days` 给前端，别处不抄。
    """
    if r := need_admin(request):
        return r
    qp = request.query_params
    bad, want = _file_index(qp)
    if bad is not None:
        return bad
    days = access_stats.DAY_LADDER[1]
    raw_days = (qp.get("days") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            return _bad(f"days 参数是天数（整数，1-{access_stats.MAX_DAYS}）")
    if not 1 <= days <= access_stats.MAX_DAYS:
        return _bad(f"days 只能是 1-{access_stats.MAX_DAYS}（最多回看 {access_stats.MAX_DAYS} 天）")
    files = await run_in_threadpool(web_logs.nginx_log_files)
    bad, picked_source = _pick_access_source(files, want)
    if bad is not None:
        return bad
    idx, picked = picked_source
    entries = await run_in_threadpool(web_logs.log_series, picked["path"])
    data = await run_in_threadpool(access_stats.collect_days, entries, days)
    data["source"] = picked["label"]
    data["site"] = picked["site"]
    data["file"] = idx
    data["files"] = _file_choices(files)
    # 世代只报"读到了哪几份、多大"，路径不出这道门
    data["series"] = [{"name": e["name"], "gen": e["gen"], "gz": e["gz"], "bytes": e["bytes"]}
                      for e in entries]
    return data
