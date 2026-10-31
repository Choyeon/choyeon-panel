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
- **回看历史也封顶**：按天趋势要翻轮替世代（`.1`、`.2.gz`……），解压是这里唯一贵的动作，
  所以每份最多读 `DAILY_FILE_BYTES`（解压后）、每天最多记 `MAX_UV_PER_DAY` 个客户端，
  读到哪一天（`historyFrom`）与上一周期依据够不够（`previous.partial`）都如实说明。
"""

import gzip
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import web_logs

# 一次扫描最多往回读这么多字节；再大的文件只看尾部（truncatedRead 会如实说明）。
MAX_BYTES = 8 * 1024 * 1024
# 解析行数硬上限：8MB 全是最短行时也有几十万行，超过这个数就停，别让一次刷新攥住线程池。
MAX_LINES = 200_000
# 路径 / 客户端各自的唯一键上限。
MAX_KEYS = 4000
# 返回给前端的榜单长度与曲线桶数上限。
TOP_N = 15
# 状态码榜单比路径/客户端短一截（一行一个码，界面只放得下这么多），
# 但"短的"不等于"没的"：第 11 个码之后的请求照样要并进「…其他」。
TOP_CODES = 10
SERIES_MAX = 1440
# 统计窗口档位：给网页下拉用，不是校验区间（校验用 SERIES_MAX，任何 1-1440 都接受）。
# 放这里而不是前端抄一份：档位一改，接口、CLI、下拉必须一起改。
WINDOW_LADDER = [15, 30, 60, 180, 360, 720, 1440]
# 按天趋势的窗口档位，同一条纪律：只在服务里定义一次，接口、下拉、CLI 都读它。
DAY_LADDER = [3, 7, 14, 30]
MAX_DAYS = 30
# 按天回看的每一份世代最多读这么多字节（解压后的量，因为费内存与 CPU 的是它）。
# 读满就如实标 truncatedFiles，不假装"这就是那一天"。
DAILY_FILE_BYTES = 32 * 1024 * 1024
# 一天里独立客户端的记名上限。最坏情况是 61 个桶 × 这个数 ≈ 12 万个字符串，
# 超过就只报"至少这么多人"（uvFloored），而不是报一个看着精确的假数。
MAX_UV_PER_DAY = 2000
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
    """把 nginx 的时间戳换成 epoch 秒；解析不出来就返回 None（计入坏行，不抛）。"""
    parts = _parse_time_parts(raw)
    return None if parts is None else parts[0]


def _parse_time_parts(raw: str):
    """nginx 时间戳 →（epoch 秒，这一行自己声明的 UTC 偏移秒）；读不出来返回 None。

    偏移要单独留着：按天分桶得按"日志自己写的那天"分。这台机器的 nginx 记 `+0800`，
    23:30 +0800 那一行按 UTC 归桶会算进前一天，看趋势的人会以为昨天凭空多了几百个请求。

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
        epoch = datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=UTC).timestamp() - offset
    except ValueError:
        return None
    return epoch, offset


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


def _unreadable(minutes: int) -> dict:
    """读不动那一份统计结果：键与正常返回完全一致、数值全为 0，另带一句 `readError`。

    为什么不直接抛：这条路挂在 `GET /api/system/nginx-stats` 上，裸 PermissionError 出去就是
    500，而 500 里一个字都没说。为什么不只给 `readError` 不给零值表：这张表的键在界面上
    各有各的位置，缺键的前端就是 undefined 渲染。零值本身绝不能被读成"这个窗口没流量"，
    所以 `readError` 必须一起给，前端与 CLI 见到它就只说"读不到"。
    """
    window = max(1, min(int(minutes) if minutes else 1, SERIES_MAX))
    return {
        "parsed": 0, "outsideWindow": 0, "malformed": 0, "scannedLines": 0,
        "bytesRead": 0, "truncatedRead": False, "windowMinutes": window,
        "from": "", "to": "", "newest": "", "oldest": "", "bytesOut": 0,
        "clients": 0, "paths": 0, "clientsOverflow": 0, "clientsFloored": False,
        "pathsFloored": False, "pathsOverflow": 0,
        "status": {"0xx": 0, "1xx": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0},
        "topCodes": [], "topPaths": [], "topClients": [], "methods": [], "series": [],
        "readError": web_logs.readable_hint(),
    }


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

    来源清单里的文件读不动（0640 root:adm 撞上非 root 进程）时给 `_unreadable()`：
    按天那条路早就把"读不到的世代"单独数进 `unreadableFiles`，分钟窗口原来是裸读，
    一次刷新直接变 500。
    """
    try:
        lines, read_bytes, truncated = read_tail(path)
    except OSError:
        return _unreadable(minutes)
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
    paths_extra_bytes = 0
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
            paths_extra_bytes += sent

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
        "paths": len(paths),
        "clientsOverflow": clients_extra,
        # 唯一键封顶时 len(clients) 就是 MAX_KEYS 本身：真实人数只会更多，不会更少。
        # 不把这一层说出来，界面上的"独立客户端 4000 个"读起来像一个精确数。
        "clientsFloored": len(clients) >= MAX_KEYS,
        "pathsFloored": len(paths) >= MAX_KEYS,
        "pathsOverflow": paths_extra,
        "status": classes,
        "topCodes": _top_counts(codes, key="code", limit=TOP_CODES),
        "topPaths": _top_paths(paths, paths_extra, paths_extra_bytes),
        "topClients": _top_counts(clients, clients_extra),
        "methods": _top_counts(methods),
        "series": series,
    }


def _top_counts(counter: dict, extra: int = 0, *, key: str = "key", limit: int = TOP_N) -> list:
    """榜单留前 `limit` 名，剩下的请求数并进「…其他」而不是悄悄消失。

    原来这里有两处不说：`methods` 恒传 `extra=0`，而第 16 名之后的键根本不在榜上——
    扫描器在 80 端口上刷出来的方法名（PROPFIND、CONNECT、随机串）轻松超过 15 个，
    于是这些请求既不上榜也不出现「…其他」，而 CLI 的 schema 还写着"超出并进…其他并计数"；
    `topCodes` 更直接，`[:10]` 一刀切，第 11 个状态码的请求数从表里凭空蒸发。
    """
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    out = [{key: k, "count": v} for k, v in items[:limit]]
    rest = extra + sum(v for _, v in items[limit:])
    if rest:
        out.append({key: "…其他", "count": rest})
    return out


def _top_paths(counter: dict, extra: int = 0, extra_bytes: int = 0) -> list:
    """路径榜同一条纪律：落榜的请求数与字节数都并进「…其他」这一行。

    字节数原来固定写 0，等于在流量列上说"这些路径没发过东西"；键表封顶时那份
    字节由调用方在循环里单独攒（`extra_bytes`），所以这一行两个数都是全量。
    """
    rows = sorted(counter.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = [{"path": p, "count": c, "bytes": b} for p, (c, b) in rows[:TOP_N]]
    rest = [(c, b) for _, (c, b) in rows[TOP_N:]]
    count = extra + sum(c for c, _ in rest)
    if count:
        out.append({"path": "…其他", "count": count,
                    "bytes": extra_bytes + sum(b for _, b in rest)})
    return out


def read_lines(entry: dict, cap: int = DAILY_FILE_BYTES):
    """读一份轮替世代（`.gz` 也读），返回（行, 是否被封顶）。

    封顶卡在解压后的字节上：`.gz` 在盘上 1MB 也可能解出 40MB，吃内存与 CPU 的是解压后的量。
    被封顶时丢掉最后半行——它不是完整记录，把它当一条请求计入就是编数字。
    """
    path = entry["path"]
    if entry.get("gz"):
        with gzip.open(path, "rb") as f:
            raw = f.read(cap + 1)
    else:
        with open(path, "rb") as f:
            raw = f.read(cap + 1)
    if len(raw) <= cap:
        return raw.decode(errors="replace").splitlines(), False
    lines = raw[:cap].decode(errors="replace").splitlines()
    if lines:
        lines.pop()
    return lines, True


def _day_bucket(buckets: dict, day) -> dict:
    b = buckets.get(day)
    if b is None:
        b = {"pv": 0, "uv": set(), "uvFloored": False, "bytes": 0, "e4xx": 0, "e5xx": 0}
        buckets[day] = b
    return b


def _prune(buckets: dict, keep: int) -> int:
    """只留住最近 `keep` 天，返回被丢掉的请求数。

    一个坏掉的 logrotate（或一份手工 cat 进去的历史）能让一个文件横跨几个月，
    逐日桶就会无界增长。这里封顶，并把丢掉的量如实报成 outside，不静默吞。
    """
    if len(buckets) <= keep:
        return 0
    drop = sorted(buckets.keys())[: len(buckets) - keep]
    gone = 0
    for day in drop:
        gone += buckets.pop(day)["pv"]
    return gone


def collect_days(entries: list, days: int) -> dict:
    """按天聚合访问日志，并和"上一个同样长的周期"比一次。

    锚点是读到的最新一行那天，不是墙上时钟：站点可能两天没人访问，用今天做窗口
    只会得到一列 0，看上去像统计坏了（和分钟窗口同一个理由）。
    只翻到够覆盖两个周期为止——更老的世代对本次对比没有信息量，读到就停。
    `previous.partial` 说的是"上一周期没有足够依据"（reason：日志没那么长 = history，
    扫描被封顶或有世代读不到 = clipped），此时不给百分比：
    拿 3 天的量去比 7 天的量，或者拿半份日志去比整份，得出的涨跌都是假的。
    """
    want = max(1, min(int(days) if days else DAY_LADDER[1], MAX_DAYS))
    buckets: dict = {}
    anchor = None
    history_from = None
    parsed = malformed = scanned_lines = outside = 0
    bytes_scanned = disk_bytes = scanned_files = truncated_files = 0
    unreadable_files = 0
    capped = False

    for entry in entries:
        try:
            # 世代是在这一步之前才发现的，中间正好赶上 logrotate 跑完（改名 + 删最老那份）
            # 完全可能。读不到就点名为"读不到"，别让一次刷新变成 500。
            lines, tr = read_lines(entry)
        except OSError:
            unreadable_files += 1
            continue
        scanned_files += 1
        disk_bytes += int(entry.get("bytes") or 0)
        if tr:
            truncated_files += 1
        limit = min(len(lines), MAX_LINES)
        if len(lines) > limit:
            capped = True
        for line in lines[:limit]:
            m = _LINE_RE.match(line)
            if not m:
                if line.strip():
                    malformed += 1
                continue
            parts = _parse_time_parts(m.group("time"))
            if parts is None:
                malformed += 1
                continue
            ts, offset = parts
            day = datetime.fromtimestamp(ts + offset, UTC).date()
            bytes_scanned += len(line.encode(errors="replace"))
            parsed += 1
            if anchor is None or day > anchor:
                anchor = day
            if history_from is None or day < history_from:
                history_from = day
            b = _day_bucket(buckets, day)
            b["pv"] += 1
            code = m.group("status")
            if code[0] == "4":
                b["e4xx"] += 1
            elif code[0] == "5":
                b["e5xx"] += 1
            b["bytes"] += int(m.group("bytes")) if m.group("bytes").isdigit() else 0
            ip = m.group("client")
            if ip in b["uv"] or len(b["uv"]) < MAX_UV_PER_DAY:
                b["uv"].add(ip)
            else:
                b["uvFloored"] = True
        scanned_lines += limit
        outside += _prune(buckets, 2 * want)
        # 两个周期都已经读到边缘就可以停了：更老的世代不会再往任何一格添数字。
        if (anchor is not None and history_from is not None
                and (anchor - history_from).days >= 2 * want - 1):
            break

    if anchor is None:
        return {"days": want, "anchor": "", "from": "", "prevFrom": "", "rows": [],
                "current": _empty_span(), "previous": {"pv": 0, "uv": 0, "bytes": 0, "days": 0,
                                                        "partial": True, "reason": "no-data"},
                "comparable": False, "deltaPvPct": None,
                "parsed": 0, "malformed": malformed, "outside": outside,
                "scannedLines": scanned_lines, "scannedFiles": scanned_files,
                "totalFiles": len(entries), "truncatedFiles": truncated_files,
                "unreadableFiles": unreadable_files,
                "bytesScanned": bytes_scanned, "diskBytes": disk_bytes,
                "historyFrom": "", "uvFloored": False}

    from_day = anchor - timedelta(days=want - 1)
    prev_from = from_day - timedelta(days=want)
    rows = []
    for back in range(want - 1, -1, -1):
        day = anchor - timedelta(days=back)
        b = buckets.get(day)
        rows.append({"date": day.isoformat(), "pv": b["pv"] if b else 0,
                     "uv": len(b["uv"]) if b else 0, "uvFloored": bool(b and b["uvFloored"]),
                     "bytes": b["bytes"] if b else 0,
                     "e4xx": b["e4xx"] if b else 0, "e5xx": b["e5xx"] if b else 0})
    current = _span_totals(buckets, from_day, anchor)
    prev_days = _span_days(buckets, prev_from, from_day - timedelta(days=1))
    previous = _span_totals(buckets, prev_from, from_day - timedelta(days=1))
    previous["days"] = prev_days
    # 覆盖判断只看"最早读到哪天"：那天晚于 prev_from 就是日志本身没那么长，
    # 中间某天是 0 还是没日志，光看访问日志分不出来，所以这里只认文件边界这一个证据。
    short = history_from is not None and history_from > prev_from
    clipped = capped or truncated_files > 0 or unreadable_files > 0
    previous["partial"] = short or clipped
    # 两个原因同时成立时先说 clipped：「历史没那么长」用户什么也做不了，
    # 「这一份被截断/读不到」才是可以调 logrotate 或读量上限的线索。
    previous["reason"] = "clipped" if clipped else "history" if short else "full"
    comparable = not previous["partial"] and previous["pv"] > 0 and current["pv"] > 0
    delta = None
    if comparable and previous["pv"]:
        delta = round((current["pv"] - previous["pv"]) * 100.0 / previous["pv"], 1)
    window_uv, uv_floored = _union_uv(buckets, from_day, anchor)
    current["uv"] = window_uv
    current["uvFloored"] = uv_floored
    return {"days": want, "anchor": anchor.isoformat(), "from": from_day.isoformat(),
            "prevFrom": prev_from.isoformat(), "rows": rows,
            "current": current, "previous": previous,
            "comparable": comparable, "deltaPvPct": delta,
            "parsed": parsed, "malformed": malformed, "outside": outside,
            "scannedLines": scanned_lines, "scannedFiles": scanned_files,
            "totalFiles": len(entries), "truncatedFiles": truncated_files,
            "unreadableFiles": unreadable_files,
            "bytesScanned": bytes_scanned, "diskBytes": disk_bytes,
            "historyFrom": history_from.isoformat() if history_from else "",
            "uvFloored": uv_floored}


def _empty_span() -> dict:
    return {"pv": 0, "uv": 0, "uvFloored": False, "bytes": 0, "e4xx": 0, "e5xx": 0}


def _span_totals(buckets: dict, lo, hi) -> dict:
    out = _empty_span()
    if hi < lo:
        return out
    for day, b in buckets.items():
        if lo <= day <= hi:
            out["pv"] += b["pv"]
            out["bytes"] += b["bytes"]
            out["e4xx"] += b["e4xx"]
            out["e5xx"] += b["e5xx"]
    uv, floored = _union_uv(buckets, lo, hi)
    out["uv"] = uv
    out["uvFloored"] = floored
    return out


def _span_days(buckets: dict, lo, hi) -> int:
    if hi < lo:
        return 0
    return sum(1 for day in buckets if lo <= day <= hi)


def _union_uv(buckets: dict, lo, hi) -> tuple:
    """窗口级独立客户端：跨天要去重，所以只能把各天的集合并起来数。

    并集也封顶（沿用 `MAX_KEYS` 这个已经存在的数，不再新造一个），超出就标 uvFloored，
    前端写"至少这么多人"。
    """
    if hi < lo:
        return 0, False
    seen: set = set()
    floored = False
    for day in sorted(buckets):
        if not (lo <= day <= hi):
            continue
        b = buckets[day]
        if b["uvFloored"]:
            floored = True
        for ip in b["uv"]:
            if ip in seen or len(seen) < MAX_KEYS:
                seen.add(ip)
            else:
                floored = True
    return len(seen), floored
