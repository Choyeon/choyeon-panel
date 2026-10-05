import json
import time

from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.responses import Response

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
)
MUTATIONS = {"POST", "PATCH", "PUT", "DELETE"}

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


def _extract_token(headers: Headers, query: QueryParams) -> str:
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return query.get("token", "")


class AuthMiddleware:
    """Pure ASGI middleware: JWT auth + viewer write guard + security headers.

    - /api/* 全部要求登录（公开白名单除外），支持 Header Bearer 或 ?token=（SSE/下载场景）
    - 401 文案区分：未登录 / 登录已过期
    - viewer 角色禁止一切写操作（POST/PATCH/PUT/DELETE），/api/auth/password 自助改密除外
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
            token = _extract_token(headers, query)
            if not token:
                await _json(401, "未登录")(scope, receive, send)
                return
            try:
                decoded = security.verify_token(token)
                role = decoded.get("role") or "admin"
                scope["state"]["cp_sub"] = decoded.get("sub")
                scope["state"]["cp_role"] = role
            except Exception:  # noqa: BLE001
                role = None
                if scope["type"] == "http":
                    await _json(401, "登录已过期")(scope, receive, send)
                    return
                # WebSocket 不返回 HTTP 响应：保持 state 为空，由路由自行 close（如终端 4403）
            if (
                scope["type"] == "http"
                and scope["method"] in MUTATIONS
                and role != "admin"
                and not path.startswith("/api/auth/password")
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
