import asyncio
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, security
from . import database as dbm
from .deps import AuthMiddleware
from .log import warn
from .routers import apps, backups, dbops, extras, files, meta, system, terminal
from .services import alerts_service

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
    try:
        yield
    finally:
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


@app.exception_handler(RequestValidationError)
async def validation_handler(_req: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "请求参数不合法"})


@app.exception_handler(404)
async def not_found(_req: Request, _exc):
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception):
    if config.LOG:
        warn("unhandled", type(exc).__name__, exc)
    msg = str(exc) if config.LOG else "服务器内部错误"
    return JSONResponse(status_code=500, content={"error": msg})


# ---------- public auth ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "uptime": round(time.time() - START), "version": config.VERSION}


@app.get("/api/auth/status")
async def auth_status():
    u = dbm.query_one("SELECT username FROM users LIMIT 1")
    return {"needsSetup": not u, "username": u["username"] if u else None}


def _user_token(username: str, role: str) -> dict:
    return {"token": security.sign_token(username, role), "role": role}


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
    if not re.match(r"^[A-Za-z0-9_-]{3,32}$", username):
        return JSONResponse(status_code=400, content={"error": "用户名只能包含字母、数字、下划线与连字符（3-32 位）"})
    if len(password) < 8:
        return JSONResponse(status_code=400, content={"error": "密码至少 8 位"})
    dbm.execute(
        "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
        (username, security.hash_pass(password), "admin"),
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
    if not user or not password or not security.check_pass(password, user["pass_hash"]):
        dbm.audit(username or "?", "login_failed", ip)
        return JSONResponse(status_code=401, content={"error": "用户名或密码错误"})
    dbm.audit(user["username"], "login")
    security.clear_attempts(ip)
    role = user["role"] or "admin"
    return _user_token(user["username"], role)


async def _json_body(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = None
    return body if isinstance(body, dict) else {}


def client_ip(req: Request) -> str:
    if config.TRUST_PROXY:
        fwd = req.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return req.client.host if req.client else "?"


# ---------- self-service password (authenticated) ----------
@app.post("/api/auth/password")
async def change_password(req: Request):
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    body = await _json_body(req)
    if not me or not body.get("old") or not security.check_pass(body["old"], me["pass_hash"]):
        return JSONResponse(status_code=400, content={"error": "原密码错误"})
    if not body.get("new") or len(body["new"]) < 8:
        return JSONResponse(status_code=400, content={"error": "新密码至少 8 位"})
    role = me["role"] or "admin"
    dbm.execute("UPDATE users SET pass_hash=? WHERE username=?", (security.hash_pass(body["new"]), me["username"]))
    # 仅让当前用户的旧 token 失效，并立即签发新 token，避免用户被踢下线
    security.bump_user_epoch(me["username"])
    dbm.audit(me["username"], "password_change")
    return _user_token(me["username"], role)


# ---------- routers ----------
app.include_router(apps.router)
app.include_router(meta.router)
app.include_router(system.router)
app.include_router(terminal.router)
app.include_router(dbops.router)
app.include_router(backups.router)
app.include_router(files.router)
app.include_router(extras.router)


# ---------- background housekeeping ----------
async def _housekeeping():
    while True:
        await asyncio.sleep(600)
        try:
            security.cleanup_attempts()
            dbm.prune_audit()
            dbm.prune_deployments()
        except Exception as e:  # noqa: BLE001
            warn("housekeeping failed:", e)


# ---------- static frontend ----------
if config.WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(config.WEB_DIST), html=True), name="static")
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
