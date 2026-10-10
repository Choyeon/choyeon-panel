import json
import time
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import config, security
from .log import log

# 免认证端点：health（存活）与 ready（就绪）必须放通，否则反代/容器探针拿不到真实状态。
# 两者都只返回"能不能服务"这一层信息，不泄露任何业务数据。
PUBLIC_PREFIXES = (
    "/api/health",
    "/api/ready",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/setup",
    # 头像由 <img> 直接加载，带不了 Authorization；路由本身只按白名单文件名回读图片。
    "/api/avatars",
)
MUTATIONS = {"POST", "PATCH", "PUT", "DELETE"}

# 只读账号唯一能写的东西是"自己的账号"：改密、改资料、换头像。
# 头像必须和 profile 同批放行——上传入口就在资料弹窗里，漏掉它的话 viewer
# 点"上传头像"得到的是「只读账号不能执行写操作」，而同一个弹窗里改昵称是好的。
SELF_SERVICE_PREFIXES = ("/api/auth/password", "/api/auth/profile", "/api/auth/avatar")

# 面板自身托管静态资源，全部同源加载；禁用 object/frame 嵌入与外域脚本
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "  # vue SFC 内联样式脚本 + echarts 运行时
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self' ws: wss:; "
    "font-src 'self' data:; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("Referrer-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy", CSP),
]


def _json(status: int, msg: str) -> Response:
    return Response(json.dumps({"error": msg}, ensure_ascii=False), status_code=status, media_type="application/json")


def need_admin(req: Request) -> JSONResponse | None:
    """中间件只挡得住写操作，读接口得自己表态。

    viewer 账号的"只读"含义是"看不到不该看的"，不是"能读所有 GET"：
    文件接口能读任意 .env、备份接口能下载含口令的 dump，
    所以这类端点必须显式要求 admin，而不是靠 HTTP 方法猜。
    """
    if req.state.cp_role != "admin":
        return JSONResponse(status_code=403, content={"error": "需要管理员权限"})
    return None


def _extract_token(headers: Headers, query: QueryParams, allow_query: bool) -> str:
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # ?token= 只用于浏览器无法带 Header 的场景（<a> 下载、EventSource、WebSocket），
    # 它们全是 GET。写操作如果也认 URL 里的 token，口令化的长期凭证就会原样出现在
    # nginx access log、浏览器历史和代理日志里——那些地方的留存策略没人替你负责。
    return query.get("token", "") if allow_query else ""


def _same_origin(headers: Headers) -> bool:
    """WebSocket 握手没有 Authorization，也没有 CORS 保护，只能自己验 Origin。

    浏览器发起跨站 ws:// 连接时照样带上 cookie/URL 里的凭证，面板的终端是 root shell，
    一个恶意网页只要诱导已登录管理员点开，就能拿到交互终端。同源检查是这道防线。
    """
    origin = headers.get("origin")
    if not origin:
        return True  # 非浏览器客户端（自检脚本）不带 Origin
    return (urlsplit(origin).netloc or "").lower() == (headers.get("host") or "").lower()


async def _ws_close(send, code: int) -> None:
    """先 accept 再 close，才能把 close code 送到浏览器。

    握手阶段直接发 websocket.close 时 uvicorn 回的是 HTTP 403，客户端只会看到
    code=1006 的"连接异常断开"，于是 4401/4498/4403 这几种原因在前端长成同一句话
    （重新登录 / 换管理员账号 / 访问来源不对，用户根本分不出来）。
    接受连接后立即关闭不读任何帧，代价只是一次空会话。
    """
    await send({"type": "websocket.accept", "subprotocol": None, "headers": []})
    await send({"type": "websocket.close", "code": code, "reason": ""})


class AuthMiddleware:
    """Pure ASGI middleware: JWT auth + viewer write guard + security headers.

    - /api/* 全部要求登录（公开白名单除外），支持 Header Bearer；
      ?token= 只在 GET 与 WebSocket 握手被接受（下载/SSE/终端带不了 Header）
    - WebSocket 握手额外校验 Origin 同源，挡跨站网页接管 root 终端
    - 401 文案区分：未登录 / 登录已过期；WS 用 close 码 4401/4498 表达同样的区分
    - viewer 角色禁止一切写操作（POST/PATCH/PUT/DELETE），SELF_SERVICE_PREFIXES
      列出的"改自己"接口除外
    - 统一注入安全响应头；WebSocket 连接同样写入 scope["state"]，供终端鉴权
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        scope.setdefault("state", {})
        headers = Headers(scope=scope)
        query = QueryParams(scope.get("query_string", b""))
        is_public = any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES)
        started = time.perf_counter()

        if path.startswith("/api/") and not is_public:
            is_ws = scope["type"] == "websocket"
            if is_ws and not _same_origin(headers):
                await _ws_close(send, 4498)
                return
            allow_query = is_ws or scope.get("method") in ("GET", "HEAD")
            token = _extract_token(headers, query, allow_query)
            if not token:
                if is_ws:
                    await _ws_close(send, 4401)
                    return
                await _json(401, "未登录")(scope, receive, send)
                return
            try:
                decoded = security.verify_token(token)
                role = decoded.get("role") or "admin"
                scope["state"]["cp_sub"] = decoded.get("sub")
                scope["state"]["cp_role"] = role
            except Exception:  # noqa: BLE001
                role = None
                # 过期会话在 WS 上要如实说"登录已过期"：旧写法让握手继续、由终端路由
                # 按角色关闭（4403），前端把一次重新登录就能解决的事报成"无终端权限"。
                if is_ws:
                    await _ws_close(send, 4401)
                    return
                await _json(401, "登录已过期")(scope, receive, send)
                return
            if (
                scope["type"] == "http"
                and scope["method"] in MUTATIONS
                and role != "admin"
                and not path.startswith(SELF_SERVICE_PREFIXES)
            ):
                await _json(403, "只读账号不能执行写操作")(scope, receive, send)
                return

        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS:
                    h.append(key, value)
                if config.HSTS:
                    h.append("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                if config.LOG:
                    cost = (time.perf_counter() - started) * 1000
                    log(f"{scope['method']} {path} -> {message['status']} {cost:.1f}ms")
            await send(message)

        await self.app(scope, receive, send_with_headers)
