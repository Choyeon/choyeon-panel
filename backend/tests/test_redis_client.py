"""RESP2 客户端（app.services.redis_client）与 pg_service Redis 路径的回归测试。

锁定的是"把每请求 fork 一个 redis-cli 换成进程内 socket"之后最容易走样的几处：
1. 回复按任意分片到达都要能增量拼出来，bulk 长度只认协议头不认换行
   （值里本来就可能含 \r\n，靠 readline 会截短）；
2. nil 与空 bulk 是两种结果（None / b""），不能被"空"合并成一个；
3. 服务端错误正文（WRONGTYPE 之类）逐字保留，面板原样展示；
4. 流水线的回复顺序必须等于命令顺序；
5. socket 建不起来时退回 redis-cli 子进程，对外形态与旧实现一致。

不依赖真实 Redis：假服务端按字节回放回复；redis-cli 用桩，并且断言客户端路径
一次也不会去 fork 进程。
"""

import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_redis_ut_"))

from app.services import pg_service, redis_client  # noqa: E402
from app.services.redis_client import MORE, RedisConnectionError, RedisError, RespParser  # noqa: E402


# ------------------------------------------------------------------ RESP 编解码小工具
def bulk(v: bytes | None) -> bytes:
    if v is None:
        return b"$-1\r\n"
    return b"$" + str(len(v)).encode() + b"\r\n" + v + b"\r\n"


def simple(s: str) -> bytes:
    return b"+" + s.encode() + b"\r\n"


def err(s: str) -> bytes:
    return b"-" + s.encode() + b"\r\n"


def integer(i: int) -> bytes:
    return b":" + str(i).encode() + b"\r\n"


def arr(items: list) -> bytes:
    return b"*" + str(len(items)).encode() + b"\r\n" + b"".join(items)


def strarr(vals: list) -> bytes:
    return arr([bulk(v.encode("utf-8", "surrogatepass")) for v in vals])


def chunks(data: bytes, size: int) -> list:
    return [data[i:i + size] for i in range(0, len(data), size)]


class FakeRedis:
    """最小 RESP2 服务端：handler 收到命令（str 列表）返回回复字节，可按小块分片发送。"""

    def __init__(self, handler, frag: int = 0):
        self.handler = handler
        self.frag = frag
        self.commands: list = []
        self.connections = 0
        self.server = None
        self.port = 0
        self._live: list = []

    async def start(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        """只关监听、不 await wait_closed()：客户端连接是池化长连接，还活着的时候
        handler 不会退出，wait_closed() 于是永远等不到——测试会静默挂死在这里。"""
        if self.server is not None:
            self.server.close()
            self.server = None
        for writer in list(self._live):
            writer.close()
        self._live = []

    async def _serve(self, reader, writer):
        self.connections += 1
        self._live.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                args = []
                for _ in range(int(line[1:])):
                    hdr = await reader.readline()
                    n = int(hdr[1:])
                    args.append((await reader.readexactly(n)).decode("utf-8", "surrogateescape"))
                    await reader.readexactly(2)
                self.commands.append(args)
                reply = self.handler(args)
                for piece in chunks(reply, self.frag) if self.frag else [reply]:
                    writer.write(piece)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, ValueError):
            pass
        finally:
            if writer in self._live:
                self._live.remove(writer)
            writer.close()


async def start_fake(handler, frag: int = 0) -> FakeRedis:
    fake = FakeRedis(handler, frag)
    await fake.start()
    return fake


class PgAgainstFake(unittest.IsolatedAsyncioTestCase):
    """pg_service 的 Redis 函数打到假服务端的公共脚手架。

    `run` 被换成会直接失败的桩：客户端可用时任何 redis-cli 调用都是回归。
    """

    async def asyncTearDown(self):
        await redis_client.pool.close_all()

    async def _no_subprocess(self, cmd, args, **kw):
        raise AssertionError(f"客户端可用时不该再执行 {cmd}：{args}")

    async def _call(self, handler, factory, frag: int = 0):
        fake = await start_fake(handler, frag)
        with (mock.patch.object(pg_service, "REDIS_HOST", "127.0.0.1"),
              mock.patch.object(pg_service, "REDIS_PORT", fake.port),
              mock.patch.object(pg_service, "redis_password", lambda: None),
              mock.patch.object(pg_service, "run", self._no_subprocess)):
            try:
                return await factory(), fake
            finally:
                await redis_client.pool.close_all()
                await fake.stop()


# ------------------------------------------------------------------ 增量解析
class TestRespParser(unittest.TestCase):
    WRONGTYPE = "WRONGTYPE Operation against a key holding the wrong kind of value"
    STREAM = b"".join([
        simple("OK"),
        integer(-1),
        bulk(b"wor+ld\r\n"),      # 值里有 RESP 前缀与 CRLF：只能按协议长度取
        bulk(b""),                # 空 bulk 不是 nil
        b"$-1\r\n",
        arr([bulk(b"a\r\nb"), b"$-1\r\n", integer(7)]),
        err(WRONGTYPE),
    ])
    EXPECTED = ["OK", -1, b"wor+ld\r\n", b"", None, [b"a\r\nb", None, 7]]

    def test_every_fragment_boundary_yields_the_same_replies(self):
        for size in (1, 2, 3, 5, 8, 11, 64, len(self.STREAM)):
            with self.subTest(size=size):
                parser = RespParser()
                got = []
                for piece in chunks(self.STREAM, size):
                    parser.feed(piece)
                    while True:
                        v = parser.next_reply()
                        if v is MORE:
                            break
                        got.append(v)
                self.assertEqual(got[:6], self.EXPECTED)
                self.assertIsInstance(got[6], RedisError)
                self.assertEqual(got[6].message, self.WRONGTYPE)
                self.assertEqual(parser.buffered, 0, "解完的回复不该在缓冲区里留残字节")

    def test_nil_and_empty_bulk_are_different_values(self):
        parser = RespParser()
        parser.feed(b"$-1\r\n$0\r\n\r\n")
        self.assertIsNone(parser.next_reply())
        self.assertEqual(parser.next_reply(), b"")

    def test_multiline_value_survives_byte_for_byte(self):
        """多行值必须逐字节完整：靠 readline 会在第一个 \n 处截断。"""
        value = b"line1\nline2\n\n\x00\xff trailing\n"
        parser = RespParser()
        for piece in chunks(bulk(value), 3):
            parser.feed(piece)
        self.assertEqual(parser.next_reply(), value)

    def test_bulk_body_without_crlf_terminator_is_a_protocol_error(self):
        parser = RespParser()
        parser.feed(b"$3\r\nab")            # 正文还没到齐
        self.assertIs(parser.next_reply(), MORE)
        parser.feed(b"cXY")                 # 字节数够了，但结尾不是 CRLF
        with self.assertRaises(RedisConnectionError):
            parser.next_reply()

    def test_error_reply_keeps_code_and_verbatim_message(self):
        parser = RespParser()
        parser.feed(err(self.WRONGTYPE))
        rep = parser.next_reply()
        self.assertIsInstance(rep, RedisError)
        self.assertEqual(rep.code, "WRONGTYPE")
        self.assertEqual(rep.message, self.WRONGTYPE)
        self.assertEqual(str(rep), self.WRONGTYPE)

    def test_oversized_reply_is_abandoned_not_buffered(self):
        parser = RespParser(limit=64)
        with self.assertRaises(RedisConnectionError):
            parser.feed(b"$1000\r\n" + b"x" * 200)

    def test_command_encoding_is_binary_safe(self):
        raw = b"k\r\n\x00\xff"
        self.assertEqual(redis_client.encode_command(["GET", raw]), b"*2\r\n$3\r\nGET\r\n" + bulk(raw))
        self.assertEqual(redis_client.encode_command(["SET", "k", 5]),
                         b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\n5\r\n")


# ------------------------------------------------------------------ 客户端行为
class TestClient(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await redis_client.pool.close_all()

    async def test_pipeline_order_with_byte_by_byte_arrival(self):
        """一条往返发多条：回复必须按命令顺序落到对应位置，
        而回复里嵌着合法 RESP 帧时更不能被误配对（这里每字节发一次）。"""
        def handler(args):
            return {"GET": bulk(b"*3\r\nlooks like a frame"), "TYPE": simple("string"),
                    "TTL": integer(42)}.get(args[0], simple("OK"))

        fake = await start_fake(handler, frag=1)
        try:
            reps = await redis_client.pipeline(
                [["GET", "a"], ["TYPE", "a"], ["TTL", "a"], ["SET", "a", "b"]],
                host="127.0.0.1", port=fake.port, db=0, password=None)
        finally:
            await fake.stop()
        self.assertEqual(reps, [b"*3\r\nlooks like a frame", "string", 42, "OK"])
        self.assertEqual([c[0] for c in fake.commands], ["GET", "TYPE", "TTL", "SET"])
        self.assertEqual(fake.connections, 1, "四条命令应当共用一条连接")

    async def test_connection_reuse_and_single_handshake(self):
        def handler(args):
            return simple("OK") if args[0] in ("AUTH", "SELECT") else integer(1)

        fake = await start_fake(handler)
        conn_kw = {"host": "127.0.0.1", "port": fake.port, "db": 3, "password": "s3cret"}
        try:
            await redis_client.execute(["PING"], **conn_kw)
            await redis_client.execute(["PING"], **conn_kw)
        finally:
            await fake.stop()
        self.assertEqual(fake.connections, 1, "同一 (host, port, db) 必须复用连接")
        self.assertEqual([c[0] for c in fake.commands], ["AUTH", "SELECT", "PING", "PING"])

    async def test_password_change_invalidates_pooled_connection(self):
        """面板可以在页面上换 requirepass：带着旧口令的连接不能继续用。"""
        def handler(args):
            return simple("OK") if args[0] == "AUTH" else integer(1)

        fake = await start_fake(handler)
        try:
            await redis_client.execute(["PING"], host="127.0.0.1", port=fake.port, db=0, password="old")
            await redis_client.execute(["PING"], host="127.0.0.1", port=fake.port, db=0, password="new")
        finally:
            await fake.stop()
        self.assertEqual([c for c in fake.commands if c[0] == "AUTH"], [["AUTH", "old"], ["AUTH", "new"]])
        self.assertEqual(fake.connections, 2)

    async def test_execute_raises_server_error_verbatim(self):
        fake = await start_fake(lambda args: err(TestRespParser.WRONGTYPE))
        try:
            with self.assertRaises(RedisError) as cm:
                await redis_client.execute(["GET", "h"], host="127.0.0.1", port=fake.port, db=0,
                                            password=None)
        finally:
            await fake.stop()
        self.assertEqual(cm.exception.code, "WRONGTYPE")
        self.assertEqual(str(cm.exception), TestRespParser.WRONGTYPE)

    async def test_pipeline_keeps_per_command_errors_in_place(self):
        """流水线里某条命令报错不影响其余回复的归属。"""
        def handler(args):
            return err(TestRespParser.WRONGTYPE) if args[0] == "STRLEN" else simple("hash")

        fake = await start_fake(handler)
        try:
            reps = await redis_client.pipeline([["TYPE", "k"], ["STRLEN", "k"]],
                                               host="127.0.0.1", port=fake.port, db=0, password=None)
        finally:
            await fake.stop()
        self.assertEqual(reps[0], "hash")
        self.assertIsInstance(reps[1], RedisError)

    async def test_binary_value_roundtrips_exactly(self):
        payload = b"multi\nline\r\nwith\x00nul and \xff\xfe near-binary \n"
        fake = await start_fake(lambda args: bulk(payload))
        try:
            got = await redis_client.execute(["GET", "k"], host="127.0.0.1", port=fake.port, db=0,
                                             password=None)
        finally:
            await fake.stop()
        self.assertEqual(got, payload)

    async def test_half_reply_then_close_is_a_connection_error(self):
        """服务端回了半条就断开：必须报"连不上"，不能把残缺字节当数据交给上层。"""
        async def serve(reader, writer):
            await reader.readline()
            writer.write(b"$5\r\nab")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with self.assertRaises(RedisConnectionError):
                await redis_client.execute(["GET", "k"], host="127.0.0.1", port=port, db=0,
                                           password=None, read_timeout=1.0)
        finally:
            server.close()

    async def test_refused_connection_is_a_connection_error(self):
        """端口上没人监听：连不上要报 RedisConnectionError，这是回退 redis-cli 的依据。"""
        with self.assertRaises(RedisConnectionError):
            await redis_client.execute(["PING"], host="127.0.0.1", port=1, db=0, password=None,
                                        connect_timeout=0.5)


# ------------------------------------------------------------------ pg_service：走客户端
def detail_handler(ktype: str, size: int, value):
    """回放 redis_key 那条 13 命令流水线的回复；与 key 类型不匹配的探测命令回 WRONGTYPE。"""
    readers = {"string": ("STRLEN", "GETRANGE"), "hash": ("HLEN", "HSCAN"),
               "list": ("LLEN", "LRANGE"), "set": ("SCARD", "SSCAN"), "zset": ("ZCARD", "ZRANGE")}

    def handler(args):
        cmd = args[0]
        if cmd == "TYPE":
            return simple(ktype)
        if cmd == "TTL":
            return integer(120)
        if cmd == "MEMORY":
            return bulk(None)
        count_cmd, read_cmd = readers.get(ktype, ("", ""))
        if cmd == count_cmd:
            return integer(size)
        if cmd != read_cmd:
            return err(TestRespParser.WRONGTYPE)
        if cmd == "GETRANGE":
            return bulk(value)
        if cmd in ("HSCAN", "SSCAN"):
            return arr([bulk(b"0"), strarr(value)])
        return strarr(value)

    return handler


class TestPgServiceWithClient(PgAgainstFake):
    async def test_string_detail_one_roundtrip_keeps_exact_bytes(self):
        out, fake = await self._call(detail_handler("string", 11, b"line1\nline2\n\n"),
                                     lambda: pg_service.redis_key("k"))
        # 值末尾的换行是旧实现被 strip 吃掉过的：客户端按协议长度取，不碰内容
        self.assertEqual(out, {"key": "k", "type": "string", "ttl": 120, "mem": None,
                               "value": "line1\nline2\n\n", "truncated": False})
        self.assertEqual(len(fake.commands), 13, "详情页应当一条流水线发完，不是逐条往返")

    async def test_hash_detail_uses_hscan_pairs(self):
        out, _ = await self._call(detail_handler("hash", 2, ["f1", "v1", "f2", "v2"]),
                                  lambda: pg_service.redis_key("h"), frag=2)
        self.assertEqual(out["type"], "hash")
        self.assertEqual(out["value"], [{"field": "f1", "value": "v1"}, {"field": "f2", "value": "v2"}])
        self.assertFalse(out["truncated"])

    async def test_list_and_set_and_zset_details_keep_the_old_shapes(self):
        lst, _ = await self._call(detail_handler("list", 5000, ["m0", "m1"]), lambda: pg_service.redis_key("l"))
        self.assertEqual((lst["value"], lst["truncated"]), (["m0", "m1"], True))
        s, _ = await self._call(detail_handler("set", 1, ["x", "y"]), lambda: pg_service.redis_key("s"))
        self.assertEqual((s["value"], s["truncated"]), (["x", "y"], False))
        z, _ = await self._call(detail_handler("zset", 2, ["a", "1", "b", "2"]),
                                lambda: pg_service.redis_key("z"))
        self.assertEqual(z["value"], [{"field": "a", "value": "1"}, {"field": "b", "value": "2"}])

    async def test_missing_key_yields_none_type(self):
        def handler(args):
            if args[0] == "TYPE":
                return simple("none")
            if args[0] == "TTL":
                return integer(-2)
            if args[0] == "MEMORY":
                return bulk(None)
            return err(TestRespParser.WRONGTYPE)

        out, _ = await self._call(handler, lambda: pg_service.redis_key("ghost"))
        self.assertEqual(out["type"], "none")
        self.assertEqual(out["ttl"], -2)
        self.assertIsNone(out["mem"])
        self.assertIsNone(out["value"])

    async def test_key_detail_never_spawns_a_process(self):
        """两次详情页 = 一条连接、两次往返、零 redis-cli 进程（旧实现是每次 5 个进程）。"""
        fake = await start_fake(detail_handler("string", 3, b"abc"))
        with (mock.patch.object(pg_service, "REDIS_HOST", "127.0.0.1"),
              mock.patch.object(pg_service, "REDIS_PORT", fake.port),
              mock.patch.object(pg_service, "redis_password", lambda: None),
              mock.patch.object(pg_service, "run", self._no_subprocess)):
            try:
                await pg_service.redis_key("k")
                await pg_service.redis_key("k2")
            finally:
                await redis_client.pool.close_all()
                await fake.stop()
        self.assertEqual(fake.connections, 1)
        self.assertEqual(len(fake.commands), 26)

    async def test_write_and_scan_helpers_issue_the_expected_commands(self):
        seen = []

        def handler(args):
            seen.append(args)
            return {"TYPE": simple("string"), "PTTL": integer(5000), "SET": simple("OK"),
                    "PEXPIRE": integer(1), "UNLINK": integer(1), "EXPIRE": integer(1),
                    "HSET": integer(1),
                    "SCAN": arr([bulk(b"17"), strarr(["a", "b", ""])])}.get(args[0], simple("OK"))

        fake = await start_fake(handler)
        with (mock.patch.object(pg_service, "REDIS_HOST", "127.0.0.1"),
              mock.patch.object(pg_service, "REDIS_PORT", fake.port),
              mock.patch.object(pg_service, "redis_password", lambda: None),
              mock.patch.object(pg_service, "run", self._no_subprocess)):
            try:
                scanned = await pg_service.redis_scan("0", "*", 200)
                set_out = await pg_service.redis_set("s", "v")
                deleted = await pg_service.redis_del(["a", "b"])
                ttl_out = await pg_service.redis_ttl("s", 30)
                member = await pg_service.redis_member("h", "hash", "add", "v", "f")
            finally:
                await redis_client.pool.close_all()
                await fake.stop()
        self.assertEqual(scanned, {"cursor": "17", "keys": ["a", "b"]})
        self.assertEqual(set_out, {"ok": True, "ttl_kept": True})
        self.assertEqual(deleted, {"deleted": 2})
        self.assertEqual(ttl_out, {"ok": True, "applied": True, "ttl": 30})
        self.assertEqual(member, {"ok": True, "n": 1})
        # SET 与补 TTL 的 PEXPIRE 在同一条往返里，中间插不进别的请求
        self.assertEqual(seen[seen.index(["SET", "s", "v"]) + 1], ["PEXPIRE", "s", "5000"])

    async def test_redis_set_on_key_without_ttl_still_writes(self):
        """永不过期的 key（PTTL -1，最常见的一类）：只有一条 SET， kinds 也必须跟着缩。

        写死 ["text","int"] 时 _rp 因为命令数≠回复数直接抛错，线上表现是
        「编辑任何没有 TTL 的 string 都保存失败」——整条字符串编辑功能不可用。
        """
        seen = []

        def handler(args):
            seen.append(args)
            return {"TYPE": simple("string"), "PTTL": integer(-1)}.get(args[0], simple("OK"))

        out, _ = await self._call(handler, lambda: pg_service.redis_set("p", "v"))
        self.assertEqual(out, {"ok": True, "ttl_kept": False})
        self.assertIn(["SET", "p", "v"], seen)
        self.assertNotIn("PEXPIRE", [c[0] for c in seen], "没有 TTL 就不该补 PEXPIRE")

    async def test_wrong_type_error_text_reaches_the_caller_unmodified(self):
        fake = await start_fake(lambda args: err(TestRespParser.WRONGTYPE))
        with (mock.patch.object(pg_service, "REDIS_HOST", "127.0.0.1"),
              mock.patch.object(pg_service, "REDIS_PORT", fake.port),
              mock.patch.object(pg_service, "redis_password", lambda: None),
              mock.patch.object(pg_service, "run", self._no_subprocess)):
            try:
                with self.assertRaises(RuntimeError) as cm:
                    await pg_service.redis_member("k", "hash", "add", "v", "f")
                # key 详情：TYPE 都返回错误时才报错，正文同样是服务端原文
                with self.assertRaises(RuntimeError) as cm2:
                    await pg_service.redis_key("k")
            finally:
                await redis_client.pool.close_all()
                await fake.stop()
        self.assertEqual(str(cm.exception), TestRespParser.WRONGTYPE)
        self.assertEqual(str(cm2.exception), TestRespParser.WRONGTYPE)

    async def test_non_utf8_string_value_is_replaced_not_dropped(self):
        """非 UTF-8 值：字节进得来，出接口时按旧实现的 errors="replace" 处理。"""
        out, _ = await self._call(detail_handler("string", 4, b"a\xffb\n"), lambda: pg_service.redis_key("k"))
        self.assertEqual(out["value"], "a\ufffdb\n")


# ------------------------------------------------------------------ pg_service：回退 redis-cli
class _FakeProc:
    def __init__(self, out: bytes, code: int = 0):
        self._out = out
        self.returncode = code
        self.pid = 4242

    async def communicate(self):
        return self._out, None


class TestCliFallback(unittest.IsolatedAsyncioTestCase):
    """连不上 socket 时必须退回子进程路线，且对外形态与引入客户端之前一致。"""

    def setUp(self):
        self.reply: dict = {}
        self.calls: list = []

        def name_of(args):
            # _rcli 传 ["--no-auth-warning", 命令, ...]，_rcli_exact 传 ("--no-auth-warning", 命令, ...)
            return args[1] if args and args[0] == "--no-auth-warning" else (args[0] if args else "")

        async def fake_run(cmd, args, **kw):
            self.calls.append(list(args))
            return {"code": 0, "out": self.reply.get(name_of(args), "")}

        async def fake_exec(cmd, *args, **kw):
            self.calls.append(list(args))
            return _FakeProc((self.reply.get(name_of(list(args)), "") + "\n").encode())

        self.patches = [
            mock.patch.object(pg_service, "run", fake_run),
            mock.patch.object(pg_service, "redis_password", lambda: None),
            mock.patch.object(pg_service, "REDIS_HOST", "127.0.0.1"),
            # 端口 1 上没有 Redis：套接字必然建立不起来，触发回退
            mock.patch.object(pg_service, "REDIS_PORT", 1),
            mock.patch("asyncio.create_subprocess_exec", fake_exec),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self._stop)

    def _stop(self):
        for p in reversed(self.patches):
            p.stop()

    async def test_string_detail_falls_back_and_stays_exact(self):
        self.reply = {"TYPE": "string", "TTL": "120", "MEMORY": "(nil)", "STRLEN": "11",
                      "GET": "line1\nline2\n\n"}
        out = await pg_service.redis_key("k")
        self.assertEqual(out["value"], "line1\nline2\n\n", "回退路径也必须保住值末尾的换行")
        self.assertEqual(out["ttl"], 120)
        self.assertIsNone(out["mem"])
        self.assertIn(["--no-auth-warning", "GET", "k"], self.calls)

    async def test_write_and_scan_helpers_fall_back_per_command(self):
        self.reply = {"TYPE": "string", "PTTL": "5000", "SET": "OK", "PEXPIRE": "1",
                      "UNLINK": "1", "EXPIRE": "1", "HSET": "1", "SCAN": "17\na\nb"}
        self.assertEqual(await pg_service.redis_scan("0", "*", 200), {"cursor": "17", "keys": ["a", "b"]})
        self.assertEqual(await pg_service.redis_set("s", "v"), {"ok": True, "ttl_kept": True})
        self.assertEqual(await pg_service.redis_del(["a", "b"]), {"deleted": 2})
        self.assertEqual(await pg_service.redis_ttl("s", 30), {"ok": True, "applied": True, "ttl": 30})
        self.assertEqual(await pg_service.redis_member("h", "hash", "add", "v", "f"), {"ok": True, "n": 1})
        self.assertTrue(self.calls, "回退路径必须真的执行过 redis-cli")

    async def test_hash_detail_falls_back_to_type_specific_commands(self):
        """回退时仍按类型只发必要命令（旧实现的 5 次调用），不是 13 条探测。"""
        self.reply = {"TYPE": "hash", "TTL": "-1", "MEMORY": "256", "HLEN": "2", "HGETALL": "f1\nv1\nf2\nv2"}
        out = await pg_service.redis_key("h")
        self.assertEqual(out["type"], "hash")
        self.assertEqual(out["mem"], 256)
        self.assertEqual(out["value"], [{"field": "f1", "value": "v1"}, {"field": "f2", "value": "v2"}])
        self.assertNotIn(["--no-auth-warning", "STRLEN", "h"], self.calls)

    async def test_server_error_during_fallback_still_raises_verbatim(self):
        self.reply = {"HSET": "(error) WRONGTYPE Operation against a key holding the wrong kind of value"}
        with self.assertRaises(RuntimeError) as cm:
            await pg_service.redis_member("k", "hash", "add", "v", "f")
        self.assertIn("WRONGTYPE Operation against a key holding the wrong kind of value", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
