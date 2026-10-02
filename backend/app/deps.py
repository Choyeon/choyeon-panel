import json

from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.responses import Response

from . import security

PUBLIC_PREFIXES = ("/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup")
MUTATIONS = {"POST", "PATCH", "PUT", "DELETE"}


def _json(status: int, msg: str) -> Response:
    return Response(json.dumps({"error": msg}), status_code=status, media_type="application/json")


def _extract_token(headers: Headers, query: QueryParams) -> str:
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return query.get("token", "")


class AuthMiddleware:
    """Pure ASGI middleware: JWT auth + viewer write guard + security headers.

    与 Fastify 版 preHandler 行为一一对应：
    - /api/* 全部要求登录（公开白名单除外），支持 Header Bearer 或 ?token=
    - 401 文案区分：未登录 / 登录已过期
    - viewer 角色禁止一切写操作（POST/PATCH/PUT/DELETE），/api/auth/password 自助改密除外
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

        if path.startswith("/api/") and not is_public:
            token = _extract_token(headers, query)
            if not token:
                await _json(401, "未登录")(scope, receive, send)
                return
            try:
                decoded = security.verify_token(token)
            except Exception:
                await _json(401, "登录已过期")(scope, receive, send)
                return
            role = decoded.get("role") or "admin"
            scope["state"]["cp_sub"] = decoded.get("sub")
            scope["state"]["cp_role"] = role
            if (
                scope["type"] == "http"
                and scope["method"] in MUTATIONS
                and role != "admin"
                and not path.startswith("/api/auth/password")
            ):
                await _json(403, "只读账号不能执行写操作")(scope, receive, send)
                return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.append("X-Content-Type-Options", "nosniff")
                h.append("X-Frame-Options", "SAMEORIGIN")
                h.append("Referrer-Policy", "same-origin")
                h.append("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            await send(message)

        await self.app(scope, receive, send_with_headers)
