import asyncio
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


# 上传类端点自带更宽的额度（multipart 的边界与伴随字段要在文件本体之外再留一截），
# 其余 /api/ 一律按"一份表单/一条 SQL"的量给。
UPLOAD_PREFIXES = ("/api/files", "/api/backups", "/api/auth/avatar")
UPLOAD_HEADROOM = 4 * 1024 * 1024


def cap_for(path: str) -> int:
    if path.startswith(UPLOAD_PREFIXES):
        return config.MAX_UPLOAD_BYTES + UPLOAD_HEADROOM
    return config.MAX_BODY_BYTES


def _mb(n: int) -> str:
    return f"{max(1, n // (1024 * 1024))}MB"


# 愿意为"把话说清楚"多读的字节数：超过这个量就当对方是拿尺寸当武器，
# 直接把连接判死，不再替它免费搬运。
DISCARD_SLACK = 32 * 1024 * 1024
# 应用没读正文就答复时，等收尾的天花板：正常情况下一拍就读完（GET 的结尾帧早已入队），
# 只有"发了头就吊着不发正文"的客户端会走满这个超时——那时照常回响应，不陪它挂着。
SETTLE_WAIT_SEC = 5


async def discard_body(receive, ceiling: int) -> bool:
    """把对端还在发的请求体读完并丢弃，返回"是否读到了结尾"。

    响应**不能**在正文没读完时就发出：服务器一发完响应就关连接，仍在写入的
    客户端收到的是 RST，浏览器给出 net::ERR_CONNECTION_RESET——那句精心写的
    「文件超过 8MB 上限」「只读账号不能执行写操作」永远到不了用户眼前，而用户
    只会看到"上传失败"。读完只花带宽，不占内存（一块丢一块），所以对真实的
    大请求要读干净再答。ceiling 是给谎报尺寸或纯粹来打带宽的客户端设的闸：
    超过它就不再配合，接受连接被重置这一后果。
    """
    read = 0
    while read < ceiling:
        message = await receive()
        if message["type"] == "http.disconnect":
            return True
        read += len(message.get("body") or b"")
        if not message.get("more_body"):
            return True
    return False


async def _settle(receive) -> None:
    """尽力把正文收尾，然后无论如何都继续把响应发出去。

    正常请求一拍就读完（GET 的结尾帧早已入队）；走满超时的只有"发了请求头就吊着
    不发正文"的客户端，那种连接不值得让它把已经算好的响应一起拖死。
    """
    try:
        await asyncio.wait_for(discard_body(receive, DISCARD_SLACK), SETTLE_WAIT_SEC)
    except Exception:  # noqa: BLE001
        pass


async def _settled_json(status: int, msg: str, scope, receive, send) -> None:
    """答复之前先收尾：短路响应（401/403）同样不能对端还在发就挂断。"""
    await _settle(receive)
    await _json(status, msg)(scope, receive, send)


class BodyCapMiddleware:
    """全局请求体上限 + "答复之前先把正文收尾"。

    为什么做在中间件而不是每个 handler：JSON 读体现在是 8 个路由各写一遍的
    `try: body = await req.json()`，靠约定逐个加尺寸检查迟早漏一个；而未登录
    也能发一个几百 MB 的 JSON 让面板整个吞进内存，那是 OOM 而不是 400。
    上限本身很宽（默认 2MB，真实表单与 SQL 远在此之下），它量的是尺寸，
    不管格式——格式仍由各路由判。

    顺手兜住的第二类问题：任何在正文没读完时就发出的响应——viewer 上传大文件
    撞到的 403、路由自己按尺寸回的 413、路径不对的 404——都会让仍在写入的
    浏览器收到 RST。逐条路由记得写收尾、下一条忘记写，是必然会发生的事，
    所以把收尾放在 send 的必经之路上，一处兜住全部。

    Content-Length 预检挡掉"老老实实报尺寸"的请求；分块传输或谎报尺寸的走
    计数路径：读到超线就停止把字节交给应用。两条路径都先把 body 处理干净
    再发 413（见 discard_body），并且同一个连接只回一个响应——应用随后自己
    那份一律丢弃。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        cap = cap_for(scope["path"])
        too_big = f"请求体超过 {_mb(cap)}"
        declared = Headers(scope=scope).get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            # 尺寸是对方自己写的，先判断这次值不值得配合：真实上传（几十 MB 以内）
            # 读干净再答 413，用户能看到明确原因；离谱的直接拒，不替攻击者搬运。
            if int(declared) <= cap + DISCARD_SLACK:
                await discard_body(receive, int(declared) + 1)
            await _json(413, too_big)(scope, receive, send)
            return

        # seen：已交给应用的字节；complete：正文读到了底（或客户端提前断开）；
        # answered：本中间件已经替应用发过 413，之后的响应消息一律作废。
        seen, state = 0, {"complete": False, "answered": False}

        async def capped_receive():
            nonlocal seen
            message = await receive()
            if message["type"] != "http.request":
                if message["type"] == "http.disconnect":
                    state["complete"] = True
                return message
            seen += len(message.get("body") or b"")
            if not message.get("more_body"):
                state["complete"] = True
            if state["answered"] or seen > cap:
                if not state["answered"]:
                    # 此刻应用正卡在 receive() 上等 body：把剩下的读完再答，于是既不
                    # 越界进内存，也不会让对端撞上 RST。读不完（对方一直在发）也照回
                    # 413——总好过让这个连接干挂着，谁也不说话。
                    state["answered"] = True
                    await discard_body(receive, DISCARD_SLACK)
                    await _json(413, too_big)(scope, receive, send)
                return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def send_settled(message):
            if state["answered"] and message["type"].startswith("http.response."):
                return
            if message["type"] == "http.response.start" and not state["complete"]:
                # 应用没读正文就答复了：先把对端还在发的收尾，再让响应出门。
                await _settle(receive)
            await send(message)

        await self.app(scope, capped_receive, send_settled)


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
    - 401/403 这些短路响应在发出前先把请求体收尾（见 _settled_json）：本中间件在
      BodyCap 外层，兜不到它的 send 包装，不收尾的话正在上传大文件的浏览器只会
      看到连接被重置
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
                await _settled_json(401, "未登录", scope, receive, send)
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
                await _settled_json(401, "登录已过期", scope, receive, send)
                return
            if (
                scope["type"] == "http"
                and scope["method"] in MUTATIONS
                and role != "admin"
                and not path.startswith(SELF_SERVICE_PREFIXES)
            ):
                await _settled_json(403, "只读账号不能执行写操作", scope, receive, send)
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
