"""nginx 访问日志统计：一次有界扫描把 access log 收成可读的流量画像。

这一层存在的理由只有一条：**统计用的日志来源必须和 tail 用的是同一份白名单**
（`web_logs.nginx_log_files()`）。对标 1Panel/宝塔的"网站监控"，但这里不装第三方分析器，
也不写临时文件：面板是 root 进程，多一个读文件的口子就多一条任意文件读取的思路，
所以字节上限与键数上限都得在最前面生效。

三条硬约束（都有对应用例）：
- **读得完**：只从文件尾往前读 `MAX_BYTES`，一个 5GB 的 access.log 不会把事件循环或内存打满；
- **记得住上限**：路径与客户端各自最多 `MAX_KEYS` 个键，爬虫刷唯一 URL 时内存不无界增长，
  超出部分并进"其他"并如实标出来，而不是静默丢掉；
- **不外泄**：聚合只按"去掉查询串的路径"计数，从不返回原始行；查询串里常带 token，
  而统计不需要它。
- **窗口是真的窗口**：尾读到的字节里必然混着比窗口更早的行，所以状态码、榜单、曲线
  只累计窗口内的请求，窗口外被跳过的行数单列在 `outsideWindow`，不静默吞掉。
"""

import re
from datetime import UTC, datetime
from pathlib import Path

# 一次扫描最多往回读这么多字节；再大的文件只看尾部（truncatedRead 会如实说明）。
MAX_BYTES = 8 * 1024 * 1024
# 解析行数硬上限：8MB 全是最短行时也有几十万行，超过这个数就停，别让一次刷新攥住线程池。
MAX_LINES = 200_000
# 路径 / 客户端各自的唯一键上限。
MAX_KEYS = 4000
# 返回给前端的榜单长度与曲线桶数上限。
TOP_N = 15
SERIES_MAX = 1440
# 统计窗口档位：给网页下拉用，不是校验区间（校验用 SERIES_MAX，任何 1-1440 都接受）。
# 放这里而不是前端抄一份：档位一改，接口、CLI、下拉必须一起改。
WINDOW_LADDER = [15, 30, 60, 180, 360, 720, 1440]
# 路径名截断长度：聚合键本身不该比一屏表格宽。
PATH_CHARS = 200
# 扫描器会把 TLS 记录当 HTTP 打到 80 端口，nginx 把里面读不出来的字节写成 \xHH 文本
# （引号是 \x22），也可能直接落裸字节。两种形态都不属于合法请求行，却都会变成榜单上一串
# 谁也读不出的键（375px 实测：方法榜出现 \x16\x03\x01\x05）。先还原转义，再抹控制字节。
_NGINX_ESC_RE = re.compile(r"\\x([0-9A-Fa-f]{2})")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
# HTTP 方法全部是字母（GET/POST/PROPFIND），最短也有 3 个字符，所以单字母、带数字或
# 标点的 token 不可能是方法名，一律归到 "-"，不给垃圾留榜位。
_METHOD_RE = re.compile(r"[A-Za-z]{3,16}")

# combined（带 referer + UA）与 common（只到 bytes）两种 log_format；
# nginx 会把请求行里的引号转义成 \x22，所以 [^"]* 是安全的。
_LINE_RE = re.compile(
    r'^(?P<client>\S+) (?P<ident>\S+) (?P<user>\S+) '
    r'\[(?P<time>[^\]]*)\] '
    r'"(?P<request>[^"]*)" '
    r'(?P<status>\d{3}) '
    r'(?P<bytes>\d+|-)'
)
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
# 16/Oct/2026:09:12:33 +0800
_TIME_RE = re.compile(r"^(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$")


def _parse_time(raw: str) -> float | None:
    """把 nginx 的时间戳换成 epoch 秒；解析不出来就返回 None（计入坏行，不抛）。

    不用 strptime：`%b` 走的是 C 库的当前 locale，容器或 locale 一变就整月识别失败，
    而失败表现是"统计里所有行都算坏行"，比少几个数字更难查。
    """
    m = _TIME_RE.match(raw)
    if not m:
        return None
    day, mon, year, hh, mm, ss, sign, tz_h, tz_m = m.groups()
    month = _MONTHS.get(mon[:3].title())
    if month is None:
        return None
    try:
        offset = (int(tz_h) * 60 + int(tz_m)) * 60
        if sign == "-":
            offset = -offset
        return datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=UTC).timestamp() - offset
    except ValueError:
        return None


def _unescape_nginx(raw: str) -> str:
    r"""nginx 的 `\xHH` 转义还原：可打印档回到原字符，控制档与高位字节直接删掉。

    删而不是留着：`\xE8\x01` 这种是 TLS 记录里的字节，读榜单的人读不出它是什么，
    它占一个 TOP 名额，就少一个真实流量。
    """
    def rep(match):
        b = int(match.group(1), 16)
        return "" if b < 0x20 or b == 0x7F or b >= 0x80 else chr(b)

    return _NGINX_ESC_RE.sub(rep, raw)


def _method_key(token: str) -> str:
    """方法位只认真实方法名：3-16 个字母，其余一律 "-"（读不出）。"""
    t = token[:16]
    return t if _METHOD_RE.fullmatch(t) else "-"


def _split_request(raw: str) -> tuple[str, str]:
    """"GET /path?x=1 HTTP/1.1" → ("GET", "/path")。

    查询串在这里就丢掉：token、签名、手机号最常出现在查询串里，而榜单要回答的是
    "哪个地址被访问得多"，两件事不需要同一份字符串。
    """
    parts = _CTRL_RE.sub("", _unescape_nginx(raw)).strip().split(" ")
    if len(parts) < 2:
        # 断言式请求行（"GET"、空串、被截断的一行）：方法位就是整串，路径留空
        return (_method_key(parts[0]) if parts else "-", "")
    method = _method_key(parts[0])
    target = parts[1]
    if target.startswith("/"):
        path = target.split("?", 1)[0].split("#", 1)[0]
    elif "://" in target:
        # 直连代理形态 http://host/p：去掉 scheme 与 host，只留路径段
        _, sep, tail = target.split("://", 1)[1].partition("/")
        path = (sep + tail).split("?", 1)[0] if sep else ""
    else:
        # OPTIONS 的 "*"、CONNECT 的 host:port 这类形态，路径位给它原样（截断后）
        path = target.split("?", 1)[0]
    return (method, path[:PATH_CHARS])


def _iso(ts) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S+00")


def read_tail(path: str) -> tuple[list[str], int, bool]:
    """从文件尾往前最多读 MAX_BYTES，返回（行, 实际读到的字节, 是否被截断）。

    第一行可能是 seek 落在行中间得到的半行，丢掉它。
    「截断」只看 `start > 0`：一个 18KB 的日志全文读进来，尾部偏移是 0，
    拿 `size > start` 判断会让每个小日志都自称"只看了尾部"。
    """
    size = Path(path).stat().st_size
    start = max(0, size - MAX_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        raw = f.read()
    lines = raw.decode(errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines, len(raw), start > 0


def collect(path: str, minutes: int = 60) -> dict:
    """扫一次访问日志：状态码分布、TOP 路径/客户端、方法分布与每分钟请求曲线。

    时间窗按"最新一行往回 minutes 分钟"算，而不是按现在：站点可能一小时没人访问，
    用当前时间做窗口会得到一张全空的表，看上去像坏了。
    窗口是真的窗口——状态码、榜单、曲线都只累计窗口内的行，窗口外的行数单列在
    `outsideWindow`，既不打折也不静默丢掉（尾读到的字节里混着更早的行是常态）。

    分两趟扫：第一趟只取时间戳定出窗口（日志按时间追加，但最后几行可能是坏行，
    不能拿"最后一行"当最新），第二趟才聚合。合成一趟要把每条记录留在内存里
    （上限 `MAX_LINES` 行 × 每行几个字符串 ≈ 几十 MB），多解析一遍只多花 CPU，
    不改内存上界。
    """
    lines, read_bytes, truncated = read_tail(path)
    window = max(1, min(int(minutes) if minutes else 1, SERIES_MAX))
    limit = min(len(lines), MAX_LINES)
    if len(lines) > MAX_LINES:
        truncated = True

    newest = oldest = None
    malformed = 0
    for line in lines[:limit]:
        m = _LINE_RE.match(line)
        ts = _parse_time(m.group("time")) if m else None
        if ts is None:
            if line.strip():
                malformed += 1
            continue
        newest = ts if newest is None or ts > newest else newest
        oldest = ts if oldest is None or ts < oldest else oldest

    from_ts = None if newest is None else newest - window * 60
    classes = {"0xx": 0, "1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
    codes: dict[str, int] = {}
    paths: dict[str, list] = {}
    clients: dict[str, int] = {}
    methods: dict[str, int] = {}
    minute_buckets: dict[int, int] = {}
    paths_extra = clients_extra = 0
    parsed = outside = 0
    total_bytes = 0

    for line in lines[:limit]:
        m = _LINE_RE.match(line)
        if not m:
            continue
        ts = _parse_time(m.group("time"))
        if ts is None:
            continue
        if from_ts is not None and ts < from_ts:
            outside += 1
            continue
        parsed += 1

        code = m.group("status")
        cls = code[0] + "xx"
        if cls in classes:
            classes[cls] += 1
        codes[code] = codes.get(code, 0) + 1
        sent = int(m.group("bytes")) if m.group("bytes").isdigit() else 0
        total_bytes += sent

        method, p = _split_request(m.group("request"))
        methods[method] = methods.get(method, 0) + 1
        p = p or "-"
        if p in paths:
            paths[p][0] += 1
            paths[p][1] += sent
        elif len(paths) < MAX_KEYS:
            paths[p] = [1, sent]
        else:
            paths_extra += 1

        ip = m.group("client")
        if ip in clients:
            clients[ip] += 1
        elif len(clients) < MAX_KEYS:
            clients[ip] = 1
        else:
            clients_extra += 1

        bucket = int(ts // 60)
        minute_buckets[bucket] = minute_buckets.get(bucket, 0) + 1

    series = [{"ts": k * 60, "count": v} for k, v in sorted(minute_buckets.items())][-window:]

    return {
        "parsed": parsed,
        "outsideWindow": outside,
        "malformed": malformed,
        "scannedLines": limit,
        "bytesRead": read_bytes,
        "truncatedRead": truncated,
        "windowMinutes": window,
        "from": _iso(from_ts),
        "to": _iso(newest),
        "newest": _iso(newest),
        "oldest": _iso(oldest),
        "bytesOut": total_bytes,
        "clients": len(clients),
        "clientsOverflow": clients_extra,
        "pathsOverflow": paths_extra,
        "status": classes,
        "topCodes": [{"code": k, "count": v}
                     for k, v in sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
        "topPaths": _top_paths(paths, paths_extra),
        "topClients": _top_counts(clients, clients_extra),
        "methods": _top_counts(methods, 0),
        "series": series,
    }


def _top_counts(counter: dict, extra: int) -> list:
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
    out = [{"key": k, "count": v} for k, v in items]
    if extra:
        out.append({"key": "…其他", "count": extra})
    return out


def _top_paths(counter: dict, extra: int) -> list:
    rows = sorted(counter.items(), key=lambda kv: (-kv[1][0], kv[0]))[:TOP_N]
    out = [{"path": p, "count": c, "bytes": b} for p, (c, b) in rows]
    if extra:
        out.append({"path": "…其他", "count": extra, "bytes": 0})
    return out
