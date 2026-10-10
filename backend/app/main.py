import asyncio
import mimetypes
import os
import re
import stat
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, errors, security
from . import database as dbm
from .deps import AuthMiddleware
from .log import warn
from .routers import (
    alerts, apps, backups, cron, dbops, extras, files, meta, metrics, sites, system, terminal,
)
from .services import alerts_service, metrics_store

START = time.time()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 上次进程退出可能留下 running 状态的部署记录，标记为失败避免前端一直转圈
    try:
        dbm.execute("UPDATE deployments SET status='failed', finished_at=datetime('now') WHERE status='running'")
    except Exception as e:  # noqa: BLE001
        warn("reset running deployments failed:", e)

    try:
        from .services import backup_service

        await backup_service.sync_all_timers()
    except Exception as e:  # noqa: BLE001
        warn("backup timers sync failed:", e)

    alerts_service.start_checker()
    hk = asyncio.create_task(_housekeeping())
    # 指标采样单独成任务：历史曲线是"越多越有用"的数据，一次偶发异常不能把
    # 清理任务一起拖停，反之亦然。
    sampler = asyncio.create_task(metrics_store.loop())
    try:
        yield
    finally:
        sampler.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await sampler
        hk.cancel()
        alerts_service.stop_checker()


app = FastAPI(
    title="choyeon-panel",
    version=config.VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(AuthMiddleware)
errors.install_handlers(app)


@app.exception_handler(RequestValidationError)
async def validation_handler(_req: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "请求参数不合法"})


@app.exception_handler(404)
async def not_found(_req: Request, _exc):
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception):
    # 原文一律只进日志：CP_LOG 打开时把 traceback 贴给浏览器，等于向任何登录用户
    # （以及他们截图里的会话）公开内部路径与依赖结构。
    warn("unhandled", type(exc).__name__, exc)
    return JSONResponse(
        status_code=500,
        content={"error": errors.public_message(exc, fallback="服务器内部错误"), "code": "internal"},
    )


# ---------- public auth ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "uptime": round(time.time() - START), "version": config.VERSION}


@app.get("/api/auth/status")
async def auth_status():
    # 未登录可访问：只告知是否需要初始化，不回显已有账号名
    return {"needsSetup": not dbm.query_one("SELECT 1 FROM users LIMIT 1")}


def _user_token(username: str, role: str, nickname: str = '', avatar: str = '') -> dict:
    return {"token": security.sign_token(username, role), "role": role, "username": username, "nickname": nickname, "avatar": avatar}


@app.post("/api/auth/setup")
async def setup(req: Request):
    ip = client_ip(req)
    if security.rate_limited(ip):
        return JSONResponse(status_code=429, content={"error": "尝试过多，请稍后再试"})
    body = await _json_body(req)
    existing = dbm.query_one("SELECT COUNT(*) c FROM users")
    if existing and existing["c"] > 0:
        return JSONResponse(status_code=403, content={"error": "管理员已存在"})
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not security.USERNAME_RE.match(username):
        return JSONResponse(status_code=400, content={"error": "用户名只能包含字母、数字、下划线与连字符（3-32 位）"})
    if len(password) < 8:
        return JSONResponse(status_code=400, content={"error": "密码至少 8 位"})
    dbm.execute(
        "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
        (username, await security.hash_pass_async(password), "admin"),
    )
    dbm.audit(username, "setup", "创建管理员")
    security.clear_attempts(ip)
    return _user_token(username, "admin")


@app.post("/api/auth/login")
async def login(req: Request):
    ip = client_ip(req)
    if security.rate_limited(ip):
        return JSONResponse(status_code=429, content={"error": "尝试过多，请稍后再试"})
    body = await _json_body(req)
    username = (body.get("username") or "").strip()
    user = dbm.query_one("SELECT * FROM users WHERE username=?", (username,))
    password = body.get("password") or ""
    # scrypt(n=2^14) 单次约 16MB/几十毫秒：直接在 async 里跑会冻住整个事件循环，
    # 两三个人同时登录就能把仪表盘轮询一起拖死。
    ok = bool(user) and bool(password) and await security.check_pass_async(password, user["pass_hash"])
    if not ok:
        dbm.audit(username or "?", "login_failed", ip)
        return JSONResponse(status_code=401, content={"error": "用户名或密码错误"})
    role = user["role"] or "admin"
    if not security.USERNAME_RE.match(user["username"]):
        # 老版本放行过的用户名可能不符合当前规则，而 verify_token 会拒绝这种主体。
        # 照签的话用户看到的是"登录成功，然后每个请求 401"，无从下手；在这里说清楚。
        dbm.audit(user["username"], "login_rejected", ip)
        return JSONResponse(status_code=403, content={"error": "该账号的用户名不符合当前规则，请由管理员用 choyeonctl user create 重建账号"})
    dbm.audit(user["username"], "login")
    security.clear_attempts(ip)
    return _user_token(user["username"], role, user.get("nickname", ""), user.get("avatar", ""))


async def _json_body(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = None
    return body if isinstance(body, dict) else {}


def client_ip(req: Request) -> str:
    """取用于限速的客户端 IP。

    反向代理场景必须解析 XFF，否则所有登录请求都记在 127.0.0.1 这一个桶里。
    但**最左**值是客户端自带的、可任意伪造（`X-Forwarded-For: 1.2.3.4` 每次换一个，
    限速就形同虚设）。nginx 的 `$proxy_add_x_forwarded_for` 会把真实对端追加到**最右**，
    所以只信最右一段；同时优先用本面板 nginx 模板里显式设置、客户端无法覆盖的 X-Real-IP。
    """
    if config.TRUST_PROXY:
        real = req.headers.get("x-real-ip")
        if real and (ip := real.strip()):
            return ip
        fwd = req.headers.get("x-forwarded-for")
        if fwd:
            for chunk in reversed(fwd.split(",")):
                if (ip := chunk.strip()):
                    return ip
    return req.client.host if req.client else "?"


# ---------- self-service password (authenticated) ----------
@app.post("/api/auth/password")
async def change_password(req: Request):
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    body = await _json_body(req)
    old = body.get("old") or ""
    ok = bool(me) and bool(old) and await security.check_pass_async(old, me["pass_hash"])
    if not ok:
        return JSONResponse(status_code=400, content={"error": "原密码错误"})
    if not body.get("new") or len(body["new"]) < 8:
        return JSONResponse(status_code=400, content={"error": "新密码至少 8 位"})
    role = me["role"] or "admin"
    new_hash = await security.hash_pass_async(body["new"])
    dbm.execute("UPDATE users SET pass_hash=? WHERE username=?", (new_hash, me["username"]))
    # 仅让当前用户的旧 token 失效，并立即签发新 token，避免用户被踢下线
    security.bump_user_epoch(me["username"])
    dbm.audit(me["username"], "password_change")
    return _user_token(me["username"], role, me.get("nickname", ""), me.get("avatar", ""))


@app.post("/api/auth/profile")
async def update_profile(req: Request):
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    body = await _json_body(req)
    nickname = body.get("nickname")
    avatar = body.get("avatar")
    if nickname is not None:
        nickname = str(nickname).strip()[:32]
        dbm.execute("UPDATE users SET nickname=? WHERE id=?", (nickname, me["id"]))
    if avatar is not None:
        avatar = str(avatar).strip()[:256]
        dbm.execute("UPDATE users SET avatar=? WHERE id=?", (avatar, me["id"]))
    dbm.audit(req.state.cp_sub, "profile:update")
    updated = dbm.query_one("SELECT nickname,avatar FROM users WHERE id=?", (me["id"],))
    return {"ok": True, "nickname": updated["nickname"], "avatar": updated["avatar"]}


# ---------- avatar upload (authenticated) ----------
# 只认这四类位图的魔数：接口收的是原始字节，若信 Content-Type 或文件名，
# 任何人都能以"头像"名义把 HTML/脚本写进 data 目录，再由同源的 GET 原样回吐——
# 那就是存储型 XSS 的起点（面板还带 admin 会话）。
_AVATAR_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_AVATAR_NAME_RE = re.compile(r"^(\d{1,10})\.(png|jpe?g|webp|gif)$")


def _sniff_image(b: bytes) -> str | None:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


@app.post("/api/auth/avatar")
async def upload_avatar(req: Request):
    me = dbm.query_one("SELECT id FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    limit = config.AVATAR_MAX_BYTES
    declared = req.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return JSONResponse(status_code=413, content={"error": f"图片超过 {limit // (1024 * 1024)}MB"})
    # 流式读并边读边判：await req.body() 会先把整个请求体吃进内存，
    # 上限靠 Content-Length 判断又能被伪造（不发该头即分块传输），必须在读取过程中截断。
    buf = bytearray()
    async for chunk in req.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            return JSONResponse(status_code=413, content={"error": f"图片超过 {limit // (1024 * 1024)}MB"})
    data = bytes(buf)
    if len(data) < 32:
        return JSONResponse(status_code=400, content={"error": "图片内容为空或过小"})
    ext = _sniff_image(data)
    if not ext:
        return JSONResponse(status_code=415, content={"error": "只支持 PNG / JPEG / WebP / GIF"})

    os.makedirs(config.AVATAR_DIR, exist_ok=True)
    # 文件名只用用户自增 id：不采用用户名（可能含 . 与 ..），也不采用客户端给的扩展名。
    for f in os.listdir(config.AVATAR_DIR):
        if f.startswith(f"{me['id']}."):
            with suppress(OSError):
                os.unlink(os.path.join(config.AVATAR_DIR, f))
    path = os.path.join(config.AVATAR_DIR, f"{me['id']}.{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o644)
    url = f"/api/avatars/{me['id']}.{ext}?v={int(time.time())}"
    dbm.execute("UPDATE users SET avatar=? WHERE id=?", (url, me["id"]))
    dbm.audit(req.state.cp_sub, "avatar:upload", f"{len(data)}B")
    return {"avatar": url}


@app.get("/api/avatars/{name}")
async def get_avatar(name: str):
    # 免登录（<img> 带不了 Authorization），所以只按白名单形状取文件：
    # 文件名完全由正则捕获组重建，请求里的任何路径片段都进不了 os.path.join。
    m = _AVATAR_NAME_RE.match(name or "")
    if not m:
        return JSONResponse(status_code=404, content={"error": "not found"})
    ext = "jpg" if m.group(2) in ("jpg", "jpeg") else m.group(2)
    path = os.path.join(config.AVATAR_DIR, f"{m.group(1)}.{ext}")
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(path, media_type=_AVATAR_TYPES[ext], headers={"Cache-Control": "public, max-age=86400"})


# ---------- routers ----------
app.include_router(apps.router)
app.include_router(meta.router)
app.include_router(system.router)
app.include_router(terminal.router)
app.include_router(dbops.router)
app.include_router(backups.router)
app.include_router(files.router)
app.include_router(extras.router)
app.include_router(alerts.router)
app.include_router(cron.router)
app.include_router(sites.router)
app.include_router(metrics.router)


# ---------- background housekeeping ----------
def _prune_all() -> None:
    # 四个 prune 都是带子查询的 DELETE，走 database.py 那把全局 RLock 的同步 sqlite。
    dbm.prune_audit()
    dbm.prune_deployments()
    dbm.prune_alerts()
    dbm.prune_metrics()


async def _housekeeping():
    while True:
        await asyncio.sleep(600)
        try:
            security.cleanup_attempts()
            # 必须在别的工作线程里跑：直接在循环上 prune，审计/指标表一大就会把整站
            # 冻住几十秒（metrics_store.loop 里的同类清理早已走 to_thread）。
            await asyncio.to_thread(_prune_all)
        except Exception as e:  # noqa: BLE001
            warn("housekeeping failed:", e)


# ---------- static frontend ----------
def _accepts_gzip(raw: bytes) -> bool:
    """Accept-Encoding 是否接受 gzip（`gzip;q=0` 是显式拒绝，不能只 grep 子串）。"""
    for part in raw.decode("latin-1", "replace").split(","):
        token, _, params = part.strip().partition(";")
        if token.lower() not in ("gzip", "*"):
            continue
        for p in params.split(";"):
            key, _, val = p.strip().partition("=")
            if key.lower() == "q":
                try:
                    return float(val) > 0
                except ValueError:
                    pass
        return True
    return False


class DistFiles(StaticFiles):
    """哈希产物长缓存、HTML 每次回源。

    StaticFiles 不发 Cache-Control，浏览器就按 Last-Modified 做启发式缓存（约"年龄的 10%"），
    发版后旧标签页仍拿到旧 index.html：里面引用的 chunk 已被新构建删掉，
    线上表现是一串 /assets/Apps-xxxx.js 404、动态 import 失败、页面白屏只剩控制台报错。
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code=status_code)
        name = str(full_path).replace("\\", "/")
        if name.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        elif "/assets/" in name:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            # 未压缩的首屏是 956KB（framework 单文件就 856KB），协商到预压缩副本后是 273KB。
            resp.headers["Vary"] = "Accept-Encoding"
            gz = self._precompressed(name, scope, resp)
            if gz is not None:
                return gz
        else:
            resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    def _precompressed(self, name: str, scope, resp):
        """命中构建期产出的 `<file>.gz`（web/scripts/gzip-assets.mjs）。

        刻意不在请求路径上压缩：日志接口是 text/event-stream，任何 gzip 中间件都会
        把流攒成一块，日志中心就变成"卡住不动"。Etag/Last-Modified 沿用原文件——两种
        编码是同一份内容的两种表示，客户端带旧 ETag 来验证时应得到 304 而不是整份重传。
        """
        if name.endswith(".gz") or resp.status_code != 200:
            return None
        if not any(k == b"accept-encoding" and _accepts_gzip(v) for k, v in scope.get("headers", [])):
            return None
        try:
            gz_stat = os.stat(f"{name}.gz")
        except OSError:
            return None
        if not stat.S_ISREG(gz_stat.st_mode):
            return None
        media, _ = mimetypes.guess_type(name)
        out = FileResponse(f"{name}.gz", stat_result=gz_stat, media_type=media)
        for h in ("etag", "last-modified", "cache-control"):
            if h in resp.headers:
                out.headers[h] = resp.headers[h]
        out.headers["Content-Encoding"] = "gzip"
        out.headers["Vary"] = "Accept-Encoding"
        return out


if config.WEB_DIST.exists():
    app.mount("/", DistFiles(directory=str(config.WEB_DIST), html=True), name="static")
else:

    @app.get("/")
    async def no_frontend():
        return JSONResponse(status_code=404, content={"error": "前端未构建，请先在 web/ 执行 npm run build"})


def run():
    import uvicorn

    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        proxy_headers=config.TRUST_PROXY,
        forwarded_allow_ips="*" if config.TRUST_PROXY else None,
        log_level="info" if config.LOG else "warning",
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    run()
