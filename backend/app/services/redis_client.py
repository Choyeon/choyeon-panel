"""进程内 RESP2 客户端：把 Redis 读路径从「每次请求 fork 一个 redis-cli」换成复用 socket。

旧实现里一次 key 详情要发 5 条命令，每条都 fork+exec 一个 redis-cli 并重新做 TCP 握手，
面板以 root 跑在客户机上，这部分开销直接落在用户请求上。这里一条流水线一次往返拿全。

写这个模块而不引入 redis-py：面板的依赖表是刻意保持最小的，而 RESP2 的分帧本身
只有五种前缀，百来行就能做到逐字节精确（bulk 长度只信协议头、不信换行），
带 \\r\\n、NUL、非 UTF-8 字节的值因此能原样往返。

redis-cli 子进程路线保留在 pg_service 里作显式回退：连不上 TCP（Redis 只开 unix
socket、强制 TLS、或换了 bind 地址）时功能不能退化，只有"套接字建不起来"才走回退。
"""

import asyncio

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6379
DEFAULT_DB = 0
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0

# 单条回复的缓冲上限：正常业务值远小于此，超了就是协议错位/服务端异常，
# 不能让面板为了一个残缺回复把内存吃掉。
MAX_REPLY_BYTES = 64 * 1024 * 1024
MAX_IDLE_PER_KEY = 4
_READ_CHUNK = 64 * 1024

MORE = object()
"""解析器数据不够时返回的哨兵（已到达的字节不会被消费，下次 feed 后继续解析）。"""


class RedisError(Exception):
    """服务端返回的错误回应。

    message 是服务端正文逐字文本（WRONGTYPE/NOAUTH/WRONGPASS……），面板直接展示，
    不做任何软化或翻译；code 是首个 token，只用来给调用方分类。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RedisConnectionError(Exception):
    """连不上、读超时、协议对不上：调用方可以据此退回 redis-cli 子进程。"""


def _enc_arg(arg) -> bytes:
    if isinstance(arg, bytes):
        return arg
    if isinstance(arg, bytearray):
        return bytes(arg)
    if isinstance(arg, bool):
        raise TypeError("命令参数不接受 bool，请显式传 str/int")
    if isinstance(arg, str):
        # surrogatepass：JSON body 里的孤立代理项在 Python 字符串里是合法的，
        # 严格编码会直接 UnicodeEncodeError；旧路径是把它交给 argv，这里按同一
        # 字节序列原样送出去。
        return arg.encode("utf-8", "surrogatepass")
    if isinstance(arg, int):
        return str(arg).encode()
    raise TypeError(f"命令参数只能是 str/bytes/int：{type(arg).__name__}")


def encode_command(args) -> bytes:
    """RESP2 请求 = `*N\\r\\n` + N 段 `$len\\r\\n<bytes>\\r\\n`。长度按编码后的字节数算。"""
    parts = [b"*" + str(len(args)).encode() + b"\r\n"]
    for a in args:
        b = _enc_arg(a)
        parts.append(b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n")
    return b"".join(parts)


def _to_int(line: bytes) -> int:
    try:
        return int(line)
    except ValueError:
        raise RedisConnectionError(f"RESP 协议错误：长度/整数不是数字：{line[:32]!r}") from None


def _read_line(buf: bytearray, pos: int):
    i = buf.find(b"\r\n", pos)
    if i < 0:
        return None, pos
    return bytes(buf[pos:i]), i + 2


def _read_reply(buf: bytearray, pos: int):
    """从 pos 起解析一条回复，返回 (值, 新位置)；数据不足返回 (MORE, pos)。"""
    if pos >= len(buf):
        return MORE, pos
    typ = buf[pos:pos + 1]
    # 行内容从不包含类型字节本身：$-1 的长度是 -1，不是 "$-1"。
    line, after = _read_line(buf, pos + 1)
    if line is None:
        return MORE, pos
    if typ == b"+":
        return line.decode("utf-8", "replace"), after
    if typ == b"-":
        text = line.decode("utf-8", "replace")
        return RedisError(text.split(None, 1)[0] if text else "ERR", text), after
    if typ == b":":
        return _to_int(line), after
    if typ == b"$":
        n = _to_int(line)
        if n < 0:
            return None, after
        end = after + n
        if end + 2 > len(buf):
            return MORE, pos
        # 终止符必须核对：正文里本来就可能含 CRLF，只看换行会把值截短。
        if buf[end:end + 2] != b"\r\n":
            raise RedisConnectionError("RESP 协议错误：bulk 字符串结尾不是 CRLF")
        return bytes(buf[after:end]), end + 2
    if typ == b"*":
        n = _to_int(line)
        if n < 0:
            return None, after
        items = []
        cur = after
        for _ in range(n):
            v, cur = _read_reply(buf, cur)
            if v is MORE:
                return MORE, pos
            items.append(v)
        return items, cur
    raise RedisConnectionError(f"RESP 协议错误：未知回复类型 {typ!r}")


class RespParser:
    """按到达顺序喂字节、按整条回复取出对象：分片位置完全随意。"""

    def __init__(self, limit: int = MAX_REPLY_BYTES):
        self._buf = bytearray()
        self._limit = limit

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        if len(self._buf) > self._limit:
            self._buf.clear()
            raise RedisConnectionError(f"回复超过 {self._limit // 1048576}MB 上限，已放弃")

    def next_reply(self):
        v, nxt = _read_reply(self._buf, 0)
        if v is MORE:
            return MORE
        del self._buf[:nxt]
        return v

    def reset(self) -> None:
        self._buf.clear()


class RedisConnection:
    """一条 socket 上的 RESP2 会话。

    RESP 是严格一问一答/多问多答的协议，同一条连接上的命令必须整体串行，
    所以写入与读取放在同一把锁里；要并发就向池里多要几条连接。
    """

    def __init__(self, host: str, port: int, db: int, password: str | None,
                 connect_timeout: float = CONNECT_TIMEOUT, read_timeout: float = READ_TIMEOUT):
        self.host = host
        self.port = port
        self.db = db
        # 空串按"没有口令"处理：池按口令判等，"" 与 None 混用会让每条连接都被丢弃重建。
        self.password = password or None
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._reader = None
        self._writer = None
        self._parser = RespParser()
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _ensure(self) -> None:
        """建 socket 并做完握手。调用方必须已持有 self._lock——握手本身也是一问一答。"""
        if self.connected:
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.connect_timeout)
        except (OSError, TimeoutError, ValueError) as e:
            raise RedisConnectionError(f"无法连接 Redis {self.host}:{self.port}（{e}）") from None
        self._reader, self._writer = reader, writer
        self._parser = RespParser()
        handshake = []
        if self.password:
            handshake.append(encode_command(["AUTH", self.password]))
        if self.db:
            handshake.append(encode_command(["SELECT", str(self.db)]))
        for payload in handshake:
            try:
                self._writer.write(payload)
                await self._writer.drain()
                rep = await self._next_reply()
            except RedisConnectionError as e:
                await self.close()
                raise RedisConnectionError(f"Redis 握手失败：{e}") from None
            if isinstance(rep, RedisError):
                # 握手被拒的 socket 不能留在手里：AUTH 失败后 Redis 仍接受后续命令，
                # 复用它就是一堆 NOAUTH 错误正文，而不是一个明确的拒绝。
                await self.close()
                raise rep

    async def connect(self) -> None:
        async with self._lock:
            await self._ensure()

    async def close(self) -> None:
        writer = self._writer
        self._reader = self._writer = None
        self._parser.reset()
        if writer is None:
            return
        try:
            writer.close()
            # wait_closed() 在对端已经消失时可能不再返回（3.12 之后它会等写出队列排空），
            # 而关连接永远不该把一个用户请求挂住；给它半秒，剩下交给 GC。
            await asyncio.wait_for(writer.wait_closed(), 0.5)
        except (OSError, TimeoutError, EOFError):
            pass

    async def _next_reply(self):
        while True:
            v = self._parser.next_reply()
            if v is not MORE:
                return v
            try:
                chunk = await asyncio.wait_for(self._reader.read(_READ_CHUNK), self.read_timeout)
            except TimeoutError:
                raise RedisConnectionError("读取 Redis 回复超时") from None
            except OSError as e:
                raise RedisConnectionError(f"读取 Redis 回复失败：{e}") from None
            if not chunk:
                raise RedisConnectionError("Redis 关闭了连接")
            self._parser.feed(chunk)

    async def pipeline(self, cmds: list) -> list:
        """写完 N 条命令再读 N 条回复：一次往返。

        回复顺序与命令顺序一致。服务端错误**不抛**，以 RedisError 实例放在对应位置
        返回——探测型批量（见 pg_service.redis_key）里类型不匹配的命令本来就该报错，
        那些错误不能把整批结果作废。连接层问题抛 RedisConnectionError，并且这条连接
        直接作废：读指针已经对不上任何命令了，还回池子会毒害后面的请求。
        """
        if not cmds:
            return []
        payload = b"".join(encode_command(c) for c in cmds)
        async with self._lock:
            try:
                await self._ensure()
                self._writer.write(payload)
                await self._writer.drain()
                return [await self._next_reply() for _ in cmds]
            except RedisConnectionError:
                await self.close()
                raise
            except (OSError, TimeoutError) as e:
                await self.close()
                raise RedisConnectionError(f"Redis 通信中断（{e}）") from None


class ConnectionPool:
    """按 (host, port, db) 复用连接，每个目标最多留 MAX_IDLE_PER_KEY 条空闲。

    上限刻意很小：面板的请求量本来就不大，放开上限只会变成 Redis 侧的连接风暴，
    而 Redis 的 `maxclients` 被打满时最先掉线的正是面板自己。
    """

    def __init__(self, max_idle: int = MAX_IDLE_PER_KEY):
        self.max_idle = max_idle
        self._idle: dict[tuple, list] = {}
        self._lock: asyncio.Lock | None = None
        self._loop = None

    def _current_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # 事件循环换了（测试的每个 asyncio.run、进程内的 loop 重建）：旧连接绑在
            # 旧 loop 的 transport 上，在新 loop 里读它只会得到 "attached to a different
            # loop"，所以整批丢掉，交给 GC 关 socket。
            self._loop = loop
            self._lock = asyncio.Lock()
            self._idle = {}
        return self._lock

    async def acquire(self, host: str, port: int, db: int, password: str | None,
                      connect_timeout: float = CONNECT_TIMEOUT,
                      read_timeout: float = READ_TIMEOUT) -> RedisConnection:
        key = (host, port, db)
        stale = []
        async with self._current_lock():
            bucket = self._idle.setdefault(key, [])
            while bucket:
                conn = bucket.pop()
                if conn.connected and conn.password == (password or None):
                    return conn
                stale.append(conn)
        # 口令换过了就不能再用：面板允许在页面上改 requirepass，旧连接会带着旧凭据。
        for conn in stale:
            await conn.close()
        conn = RedisConnection(host, port, db, password, connect_timeout, read_timeout)
        await conn.connect()
        return conn

    async def release(self, conn: RedisConnection, keep: bool = True) -> None:
        if not keep or not conn.connected:
            await conn.close()
            return
        key = (conn.host, conn.port, conn.db)
        async with self._current_lock():
            bucket = self._idle.setdefault(key, [])
            full = len(bucket) >= self.max_idle
            if not full:
                bucket.append(conn)
        if full:
            await conn.close()

    async def close_all(self) -> None:
        async with self._current_lock():
            conns = [c for bucket in self._idle.values() for c in bucket]
            self._idle = {}
        for conn in conns:
            await conn.close()


pool = ConnectionPool()


async def pipeline(cmds: list, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                   db: int = DEFAULT_DB, password: str | None = None,
                   connect_timeout: float = CONNECT_TIMEOUT,
                   read_timeout: float = READ_TIMEOUT) -> list:
    """一次往返执行多条命令，返回按命令顺序排列的回复。

    空闲连接可能已被服务端静默断开（Redis 的 timeout、NAT、重启），从池里取出来时
    writer 还看着像活的，直到读不到东西才发现——所以读失败先换新连接重试一次，
    仍然失败才判定"连不上"，让调用方回退 redis-cli。
    """
    if not cmds:
        return []
    last: RedisConnectionError | None = None
    for _ in range(2):
        conn = await pool.acquire(host, port, db, password, connect_timeout, read_timeout)
        try:
            reps = await conn.pipeline(cmds)
        except RedisConnectionError as e:
            await pool.release(conn, keep=False)
            last = e
            continue
        except RedisError:
            await pool.release(conn, keep=False)
            raise
        await pool.release(conn)
        return reps
    raise last or RedisConnectionError(f"无法连接 Redis {host}:{port}")


async def execute(args, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  db: int = DEFAULT_DB, password: str | None = None,
                  connect_timeout: float = CONNECT_TIMEOUT,
                  read_timeout: float = READ_TIMEOUT):
    """执行单条命令。服务端错误按 RedisError 抛出（正文逐字保留）。"""
    rep = (await pipeline([list(args)], host=host, port=port, db=db, password=password,
                          connect_timeout=connect_timeout, read_timeout=read_timeout))[0]
    if isinstance(rep, RedisError):
        raise rep
    return rep
