import asyncio
import json
import os
import re
import time
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import aio_db
from .. import config
from ..deps import need_admin
from ..errors import err_json
from ..services import system_metrics, systemd_ops
from ..util import is_unit, parse_lines, redact_creds, run

router = APIRouter()


@router.get("/api/system/info")
async def sys_info():
    return await system_metrics.info()


@router.get("/api/system/stats")
async def sys_stats():
    return await system_metrics.snapshot()


@router.get("/api/system/overview")
async def sys_overview():
    """仪表盘「概览」条：一次请求拿齐四个计数，取代前端各页各自拉全表再数长度。

    站点数只扫 nginx 配置（纯文件读），不走 _site_rows() 的那条链路——后者还要查
    apps 表并向 systemd 逐个问单元状态，那是打开网站页才需要的开销。
    nginx 不在时如实返回 null + reason，前端显示「—」而不是 0。
    """
    from .sites import _conf_dirs_available, _scan_nginx_sites  # 路由互引只在需要时建立

    apps = await aio_db.query_one("SELECT COUNT(*) c FROM apps")
    cron = await aio_db.query_one("SELECT COUNT(*) c, COALESCE(SUM(enabled),0) e FROM cron_task")
    backups = await aio_db.query_one("SELECT COUNT(*) c FROM backups WHERE enabled=1")
    alerts = await aio_db.query_one("SELECT COUNT(*) c FROM alerts_log WHERE acked=0")

    def _site_count():
        if not _conf_dirs_available():
            return None, "未检测到 nginx 配置目录"
        names = set()
        for entry in _scan_nginx_sites():
            names.update(d.lower() for d in entry["domains"])
        return len(names), None

    try:
        sites, site_reason = await asyncio.to_thread(_site_count)
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
        out: dict = {"ok": True, "active": await systemd_ops.is_active(unit)}
        if verb in ("enable", "disable"):
            # 前端「自启」按钮点完要当场变状态，不能等 30s 缓存过期
            out["enabled"] = (await systemd_ops.unit_files(force=True)).get(unit, "")
        return out
    except Exception as e:  # noqa: BLE001
        # systemctl 的报错常带 unit 路径与 errno，原样回给浏览器等于公开服务器结构
        return err_json(e, 500, log_tag="service:action")


async def _sse_journal(unit: str, lines: int):
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
            # 整机 journal 里漂着的东西不受我们控制：业务应用把口令写进启动命令、
            # 框架把连接串打 INFO 日志都会顺着这条流出去。日志检索（本文件下面一节）
            # 早就统一过 redact_creds，SSE 是唯一漏网的一条流，这里补上同一个口径。
            line = redact_creds(raw.decode(errors="replace").rstrip(chr(10)))
            yield f"data: {json.dumps(line)}\n\n"
    finally:
        # 客户端断开时进程可能已退出，kill 报 ProcessLookupError 属正常
        with suppress(ProcessLookupError):
            proc.kill()


def log_stream_response(unit: str, lines: int) -> StreamingResponse:
    return StreamingResponse(
        _sse_journal(unit, min(lines, 2000)),
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
    return log_stream_response(unit, lines)


@router.get("/api/firewall")
async def firewall(req: Request):
    # 规则表等于一张"这台机器开了哪些端口、从哪些地址段能连"的地图，
    # 只读账号不需要它（设置页的防火墙区块本来就只对 admin 显示），
    # 而拿到它的人可以是任何一个用 viewer 账号登录成功的人。
    if r := need_admin(req):
        return r
    has = await run("which", ["ufw"])
    if has["code"] != 0:
        return {"tool": "none", "active": False, "output": "未安装 ufw"}
    st = await run("ufw", ["status", "verbose"])
    active = bool(re.search(r"Status: active", st["out"]))
    return {"tool": "ufw", "active": active, "output": "\n".join(st["out"].split("\n")[:80])}


# ------------------------------------------------------------------ 日志检索
# 这一节的三条纪律：argv 里绝不出现 shell、参数值先校验再交给 journalctl、
# 输出一律 redact_creds。日志检索是只读接口，但它能把整台机器的 journal 读出来，
# 所以按现有口径要求 admin（deps.need_admin），而不是靠 HTTP 方法猜。
PRIORITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")
MAX_GREP = 200
MAX_LOG_LINES = 2000
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
    limit = parse_lines(qp.get("limit"), default=200, maximum=MAX_LOG_LINES)

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


NGINX_LOG_NAMES = ("access.log", "error.log")
# 扫描 CP_FILE_ROOTS 时跳过的目录：应用依赖目录里同名文件成千上万，
# 既慢又只会把真正想看的日志挤出结果列表。
_SKIP_DIRS = {"node_modules", ".git", ".venv", "dist", "build", "__pycache__", ".next", ".output"}
TAIL_BYTES = 512 * 1024


def _inside_roots(real: str) -> bool:
    for root in config.FILE_ROOTS:
        if real == root or real.startswith(root.rstrip("/") + "/"):
            return True
    return False


def nginx_log_files() -> list:
    """CP_FILE_ROOTS 允许范围内的 nginx/应用访问日志（最多扫到一级子目录）。

    只从这份白名单里找文件，等价于「文件管理页能打开的目录才能被 tail」：
    新加一个日志接口就新開一条任意文件读取的思路，不复用白名单迟早出事。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for root in config.FILE_ROOTS:
        bases = [root]
        try:
            with os.scandir(root) as it:
                bases += sorted(e.path for e in it if e.is_dir() and e.name not in _SKIP_DIRS)
        except OSError:
            pass
        for base in bases:
            for name in NGINX_LOG_NAMES:
                cand = os.path.join(base, name)
                real = os.path.realpath(cand)
                if real in seen or not _inside_roots(real):
                    continue
                if not os.path.isfile(real):
                    continue
                seen.add(real)
                out.append({"path": real, "label": f"{os.path.basename(base) or 'root'}/{name}"})
    return out


def _tail_file(path: str, lines: int, filt: str) -> list:
    """从文件末尾最多读 TAIL_BYTES，再取后 lines 行（大日志不会把内存打满）。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        raw = f.read()
    out = []
    for line in raw.decode(errors="replace").splitlines()[-lines:]:
        if filt and filt.lower() not in line.lower():
            continue
        out.append(redact_creds(line)[:MAX_ITEM_CHARS])
    return out


@router.get("/api/system/nginx-log")
async def nginx_log(request: Request):
    """tail 白名单目录下的 access/error 日志（journal 里没有的那部分，比如 nginx 的文件日志）。"""
    if r := need_admin(request):
        return r
    qp = request.query_params
    lines = parse_lines(qp.get("lines"), default=200, maximum=MAX_LOG_LINES)
    filt = qp.get("filter") or ""
    if len(filt) > MAX_GREP:
        return _bad(f"过滤词最长 {MAX_GREP} 个字符")
    try:
        idx = int(qp.get("file") or 0)
    except ValueError:
        return _bad("file 参数是日志文件的序号（整数）")
    files = await run_in_threadpool(nginx_log_files)
    if not files:
        return {
            "items": [],
            "files": [],
            "hint": "CP_FILE_ROOTS 下没有找到 access.log / error.log。nginx 默认日志在 /var/log/nginx，"
                    "需要把它加进 CP_FILE_ROOTS 才允许被 tail。",
        }
    if not 0 <= idx < len(files):
        return _bad(f"file 序号超出范围（可用 0-{len(files) - 1}）")
    picked = files[idx]
    items = await run_in_threadpool(_tail_file, picked["path"], lines, filt)
    return {
        "items": items,
        "count": len(items),
        "source": picked["label"],
        "files": [{"i": i, "label": f["label"]} for i, f in enumerate(files)],
        "truncated": len(items) >= lines,
    }

