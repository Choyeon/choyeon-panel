"""批次⑤D：全局请求体上限（BodyCapMiddleware）。

覆盖面：
- cap_for 的分区：上传类前缀宽（CP_MAX_UPLOAD_MB + 余量），其余按 CP_MAX_BODY_MB
- Content-Length 预检：老实报尺寸的直接 413，且不进应用
- 分块/谎报尺寸：边读边量，只回一个 413，应用自己那份响应必须被丢弃
- 未超限的 body 要逐字节原样送达——中间件不能改变正常请求的语义
- 非 /api/ 路径与 websocket 不包 body
- 答复之前先收尾：短路响应（403/404/超限 413）不许在正文还在发时挂断连接
- 真栈：超大 JSON 登录得到 413（不是 500/200），且响应头仍然齐全
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch5d_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_batch5d_roots_"))
from app import config, deps, security
from app.deps import AuthMiddleware, BodyCapMiddleware, cap_for


def _scope(path="/api/auth/login", method="POST", headers=None, type="http"):
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    return {
        "type": type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 3210),
        "scheme": "http",
        "root_path": "",
    }


class Feed:
    """按脚本喂 http.request，并在脚本用尽后补一个终止块。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.n = 0

    async def __call__(self):
        if self.n < len(self.chunks):
            body = self.chunks[self.n]
            self.n += 1
            return {"type": "http.request", "body": body, "more_body": self.n < len(self.chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}


class Collector:
    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def starts(self):
        return [m for m in self.messages if m["type"] == "http.response.start"]

    @property
    def body(self):
        return b"".join(m.get("body") or b"" for m in self.messages if m["type"] == "http.response.body")


class EchoApp:
    """假应用：把收到的 body 全量读出来，然后回一个 200。"""

    def __init__(self):
        self.received = bytearray()
        self.calls = 0
        self.terminators = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            self.received.extend(message.get("body") or b"")
            if not message.get("more_body"):
                self.terminators += 1
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class TestCapPartition(unittest.TestCase):
    def test_upload_prefixes_get_the_wide_cap(self):
        wide = config.MAX_UPLOAD_BYTES + 4 * 1024 * 1024
        for p in ("/api/files", "/api/files/upload", "/api/backups", "/api/backups/3/upload", "/api/auth/avatar"):
            self.assertEqual(cap_for(p), wide, p)

    def test_other_routes_get_the_json_cap(self):
        for p in ("/api/auth/login", "/api/auth/setup", "/api/db/query", "/api/settings", "/api/apps"):
            self.assertEqual(cap_for(p), config.MAX_BODY_BYTES, p)

    def test_json_cap_leaves_real_headroom(self):
        # 默认值既要能兜住"没人认领的内存"，也要宽到不会挡掉真实表单/SQL。
        self.assertGreaterEqual(config.MAX_BODY_BYTES, 1024 * 1024)
        self.assertLess(config.MAX_BODY_BYTES, config.MAX_UPLOAD_BYTES)


class TestMiddleware(unittest.TestCase):
    def _run(self, app, scope, chunks, headers=None):
        feed = Feed(chunks)
        out = Collector()
        asyncio.run(BodyCapMiddleware(app)(scope, feed, out))
        return out, feed

    def test_declared_length_over_cap_short_circuits_without_calling_app(self):
        cap = config.MAX_BODY_BYTES
        app = EchoApp()
        out, feed = self._run(
            app, _scope(headers={"content-length": str(cap + 1)}), [b"x" * (cap // 2), b"y" * (cap // 2)]
        )
        self.assertEqual(app.calls, 0, "预检拦住就不该把请求交给路由")
        self.assertEqual(len(out.starts), 1)
        self.assertEqual(out.starts[0]["status"], 413)
        self.assertIn("请求体超过", json.loads(out.body.decode())["error"])
        self.assertEqual(feed.n, 2, "答复前要把对端还在发的 body 读干净，否则客户端收到的是 RST")

    def test_absurd_declared_length_is_refused_without_being_carried(self):
        cap = config.MAX_BODY_BYTES
        app = EchoApp()
        # 尺寸本身就离谱（上限 + 32MB 余量之外）：不做免费搬运，直接拒
        out, feed = self._run(app, _scope(headers={"content-length": str(cap + 40 * 1024 * 1024)}), [b"z" * 10])
        self.assertEqual(out.starts[0]["status"], 413)
        self.assertEqual(feed.n, 0)
        self.assertEqual(app.calls, 0)

    def test_chunked_overflow_answers_once_and_drops_the_apps_response(self):
        cap = config.MAX_BODY_BYTES
        app = EchoApp()
        chunk = b"y" * (cap // 3)
        out, feed = self._run(app, _scope(), [chunk] * 6)
        self.assertEqual(app.calls, 1)
        self.assertEqual(len(out.starts), 1, f"同一个连接只能有一个响应：{out.messages}")
        self.assertEqual(out.starts[0]["status"], 413)
        self.assertIn("请求体超过", json.loads(out.body.decode())["error"])
        # 超限后立刻收尾：应用读到的总量只可能在上限附近，绝不可能是全部 6 块
        self.assertLess(len(app.received), cap + len(chunk))
        self.assertEqual(app.terminators, 1, "必须补一个 more_body=False，否则路由会一直等")
        self.assertEqual(feed.n, 6, "答复前要先把剩下的块读完，否则对端会撞 RST")

    def test_under_cap_body_is_delivered_verbatim(self):
        cap = config.MAX_BODY_BYTES
        want = b"z" * (cap // 2)
        app = EchoApp()
        out, feed = self._run(app, _scope(headers={"content-length": str(len(want))}), [want[:1000], want[1000:]])
        self.assertEqual(bytes(app.received), want)
        self.assertEqual(len(out.starts), 1)
        self.assertEqual(out.starts[0]["status"], 200)
        self.assertEqual(out.body, b"ok")

    def test_non_api_paths_are_not_wrapped(self):
        app = EchoApp()
        big = b"a" * (config.MAX_BODY_BYTES * 2)
        out, _ = self._run(app, _scope(path="/assets/app.js"), [big])
        self.assertEqual(len(app.received), len(big))
        self.assertEqual(out.starts[0]["status"], 200)

    def test_websocket_scope_passes_through(self):
        seen = {}

        async def ws_app(scope, receive, send):
            seen["type"] = scope["type"]
            await send({"type": "websocket.accept"})

        async def ws_receive():
            return {"type": "websocket.connect"}

        async def ws_send(message):
            seen["message"] = message

        asyncio.run(BodyCapMiddleware(ws_app)(_scope(path="/api/terminal/ws", type="websocket"), ws_receive, ws_send))
        self.assertEqual(seen.get("type"), "websocket")
        self.assertEqual(seen.get("message"), {"type": "websocket.accept"})


class TestUploadRouteCaps(unittest.TestCase):
    """files 路由自己的 8MB 判定仍要成立（超限的字节不能进内存、不许落盘）。"""

    def _write(self, chunks, declared=None):
        from starlette.requests import Request

        from app.routers import files

        headers = [(b"host", b"127.0.0.1")]
        if declared is not None:
            headers.append((b"content-length", str(declared).encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "path": "/api/files/content",
            "raw_path": b"/api/files/content",
            "query_string": b"path=/root/www/p5d.bin",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 3210),
            "scheme": "http",
            "state": {"cp_sub": "probe", "cp_role": "admin"},
        }
        feed = Feed(chunks)
        # need_admin 读的是 req.state，路由里再自行 stream()：这里直接把 Request 递给 handler
        resp = asyncio.run(files.files_write(Request(scope, feed)))
        return resp, feed

    def test_declared_oversize_returns_413_without_touching_the_disk(self):
        big = config.MAX_UPLOAD_BYTES + 1024 * 1024
        chunk = b"a" * (512 * 1024)
        resp, _ = self._write([chunk] * (big // len(chunk)), declared=big)
        self.assertEqual(resp.status_code, 413)
        self.assertIn("文件超过", json.loads(resp.body.decode())["error"])

    def test_lying_content_length_is_still_capped(self):
        chunk = b"b" * (config.MAX_UPLOAD_BYTES // 8)
        chunks = [chunk] * 12  # 总量远超 8MB，但 Content-Length 谎报为 1KB
        resp, feed = self._write(chunks, declared=1024)
        self.assertEqual(resp.status_code, 413)
        self.assertLess(feed.n, len(chunks), "读到超限就停手，剩下的字节由中间件收尾")


class RejectApp:
    """假应用：正文一眼都不看就直接答复——403、404、路由的超限 413 都是这个形状。"""

    def __init__(self, status=403):
        self.status = status
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b'{"error":"no"}', "more_body": False})


class OrderCollector(Collector):
    """记下响应发出时"对端已经交出了几块"，用来证明收尾发生在答复之前。"""

    def __init__(self, feed):
        super().__init__()
        self.feed = feed
        self.chunks_at_start = None

    async def __call__(self, message):
        if message["type"] == "http.response.start" and self.chunks_at_start is None:
            self.chunks_at_start = self.feed.n
        self.messages.append(message)


class TestRespondAfterDraining(unittest.TestCase):
    """答复之前先把正文收尾，否则仍在上传的浏览器只看到连接重置，看不见错误文案。"""

    def _run(self, app, chunks, *, wrap=BodyCapMiddleware, path="/api/files/content", method="PUT", headers=None):
        feed = Feed(chunks)
        out = OrderCollector(feed)
        asyncio.run(wrap(app)(_scope(path=path, method=method, headers=headers), feed, out))
        return out, feed

    def test_short_circuit_reply_waits_for_the_pending_body(self):
        chunks = [b"a" * 4096] * 8
        out, _ = self._run(RejectApp(), chunks)
        self.assertEqual(out.starts[0]["status"], 403)
        self.assertEqual(out.chunks_at_start, len(chunks), "正文没读干净之前，响应不许发出")

    def test_request_without_body_is_not_delayed(self):
        # 无正文时收尾只是取一次早已入队的终止块，不该变成一次等待。
        out, _ = self._run(RejectApp(), [])
        self.assertEqual(len(out.starts), 1)
        self.assertEqual(out.chunks_at_start, 0)

    def test_viewer_403_from_auth_drains_too(self):
        # Auth 在 BodyCap 外层，它那三个短路响应到不了上面的 send 包装，只能自己收尾。
        token = security.sign_token("probe", "viewer")
        chunks = [b"u" * 4096] * 5
        out, _ = self._run(
            RejectApp(),
            chunks,
            wrap=AuthMiddleware,
            headers={"authorization": f"Bearer {token}"},
        )
        self.assertEqual(out.starts[0]["status"], 403)
        self.assertEqual(out.chunks_at_start, len(chunks))

    def test_stalled_client_still_gets_its_response(self):
        """发了请求头就吊着不写完的客户端，不该把已经算好的响应一起拖死。"""

        class Hang:
            def __init__(self):
                self.n = 0

            async def __call__(self):
                if self.n == 0:
                    self.n += 1
                    return {"type": "http.request", "body": b"x" * 1024, "more_body": True}
                await asyncio.sleep(3600)

        out = Collector()
        with mock.patch.object(deps, "SETTLE_WAIT_SEC", 0.05):
            asyncio.run(BodyCapMiddleware(RejectApp())(_scope(path="/api/files/content", method="PUT"), Hang(), out))
        self.assertEqual(out.starts[0]["status"], 403, "收尾超时也要照常答复")


class TestRealStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from starlette.testclient import TestClient

        from app import main

        cls.client = TestClient(main.app)
        cls.app = main.app

    def setUp(self):
        security.clear_attempts("testclient")

    def test_auth_stays_outermost_so_413_keeps_security_headers(self):
        # add_middleware 是 insert(0)，所以列表里靠前的才是外层：Auth 在 BodyCap 之外，
        # 上限拦下来的 413 仍会经过 Auth 的响应头注入与访问日志。
        names = [m.cls.__name__ for m in self.app.user_middleware]
        self.assertEqual(names, ["AuthMiddleware", "BodyCapMiddleware"], names)

    def test_oversized_login_body_is_413_not_500(self):
        r = self.client.post("/api/auth/login", content=b'{"a": "b' + b" " * (3 * 1024 * 1024) + b'}')
        self.assertEqual(r.status_code, 413, r.text[:200])
        self.assertIn("请求体超过", r.json()["error"])
        self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.headers.get("X-Frame-Options"), "SAMEORIGIN")

    def test_ordinary_large_json_still_reaches_the_route(self):
        r = self.client.post("/api/auth/login", json={"username": "nobody", "password": "p" * 3000})
        self.assertEqual(r.status_code, 401, r.text)

    def test_get_without_body_is_untouched(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
