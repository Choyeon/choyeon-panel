import asyncio
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, database as dbm, security
from .deps import AuthMiddleware
from .routers import apps, backups, dbops, extras, files, system, terminal


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from .services import alerts_service

    hk = asyncio.create_task(_housekeeping())
    alerts_service.start_checker()
    yield
    hk.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(AuthMiddleware)

START = time.time()


@app.exception_handler(RequestValidationError)
async def validation_handler(_req: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "请求参数不合法"})


@app.exception_handler(404)
async def not_found(req: Request, _exc):
    if req.url.path.startswith("/api"):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception):
    msg = str(exc) if config.LOG else "服务器内部错误"
    return JSONResponse(status_code=500, content={"error": msg})


# ---------- public auth ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "uptime": time.time() - START}


@app.get("/api/auth/status")
async def auth_status():
    u = dbm.query_one("SELECT username FROM users LIMIT 1")
    return {"needsSetup": not u, "username": u["username"] if u else None}


def _user_token(username: str, role: str):
    return {"token": security.sign_token(username, role), "role": role}


@app.post("/api/auth/setup")
async def setup(req: Request):
    body = await _json_body(req)
    count = dbm.query_one("SELECT COUNT(*) c FROM users")["c"]
    if count > 0:
        return JSONResponse(status_code=403, content={"error": "管理员已存在"})
    username = body.get("username") or ""
    password = body.get("password") or ""
    if not re.match(r"^[A-Za-z0-9_-]{3,32}$", username):
        return JSONResponse(status_code=400, content={"error": "用户名不合法"})
    if len(password) < 8:
        return JSONResponse(status_code=400, content={"error": "密码至少 8 位"})
    dbm.execute("INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)", (username, security.hash_pass(password), "admin"))
    dbm.audit(username, "setup", "创建管理员")
    return _user_token(username, "admin")


@app.post("/api/auth/login")
async def login(req: Request):
    ip = client_ip(req)
    if security.rate_limited(ip):
        return JSONResponse(status_code=429, content={"error": "尝试过多，请稍后再试"})
    body = await _json_body(req)
    username = body.get("username") or ""
    user = dbm.query_one("SELECT * FROM users WHERE username=?", (username,))
    password = body.get("password") or ""
    if not user or not password or not security.check_pass(password, user["pass_hash"]):
        dbm.audit(username, "login_failed")
        return JSONResponse(status_code=401, content={"error": "用户名或密码错误"})
    dbm.audit(user["username"], "login")
    role = user["role"] or "admin"
    return _user_token(user["username"], role)


async def _json_body(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:
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
    dbm.execute("UPDATE users SET pass_hash=? WHERE username=?", (security.hash_pass(body["new"]), me["username"]))
    dbm.audit(me["username"], "password_change")
    return {"ok": True}


# ---------- routers ----------
app.include_router(apps.router)
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
        security.cleanup_attempts()


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
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        proxy_headers=config.TRUST_PROXY,
        log_level="info" if config.LOG else "warning",
    )


if __name__ == "__main__":
    run()
