"""异地备份目标：S3 兼容对象存储，签名只用标准库。

原来备份只落在本机 `/root/backups/panel`：盘坏、机器被删、整台还给云厂商，
"最近 7 份"就跟着一起没了——而那正是备份要防的那件事。这一层补上异地一份。

为什么不引 boto3/aioboto：`requirements.txt` 只有 fastapi/uvicorn/websockets/PyJWT/
psutil/python-multipart，install.sh 要在最小发行版上一次跑通，多一个 SDK 就多一条
"依赖装不上所以部署失败"的路径（webhook 那侧同样只用 urllib）。SigV4 本身是四层
HMAC，标准库够用。
"""

import hashlib
import hmac
import ipaddress
import os
import re
import socket
import sqlite3
import ssl
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import quote, unquote, urlsplit

from .. import config
from .. import database as dbm
from ..util import human_bytes, is_http_url, is_name, name_list, redact_creds

# 单文件 PutObject 的上限是 5GiB（S3 与 MinIO 同规则）。分块上传是另一件事，
# 面板的产物是 gz 归档，真到 5GiB 该改的是备份任务的口径而不是在这里塞一套 multipart。
MAX_SINGLE_PUT = 5 * 1024 * 1024 * 1024
# 拉回本机时同一条线：面板不做分块下载/断点续传，超过这条线的产物本来也不该由
# 这条路径搬（该改的是备份范围）。拉的方向还多一道闸——声明值可以假，实读字节才算数。
MAX_SINGLE_GET = MAX_SINGLE_PUT
GET_CHUNK = 1024 * 1024  # 1MiB：一次 read 太少会被 socket 超时切不断，太大则上限检查粒度粗
# 列举响应读多少字节为止：list-type=2 一页 1000 条时 XML 可能上百 KB，
# 但我们只认自己那批 `bk<id>-` 键，读超了就如实说"没读完"，不假装 prune 成功。
MAX_LIST_BYTES = 4 * 1024 * 1024
LIST_PAGES = 20  # 上界 20 页＝2 万个对象：翻不满就如实报 truncated，而不是把 prune 当成功
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_AKID_RE = re.compile(r"^[A-Za-z0-9._-]{4,128}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9._/-]{0,200}$")
# 主机名形态：RFC 域名与 IP 字面量的字符集。含空格/下划线/中文的写法在建目标时就拒掉，
# 而不是留下一条"每次备份都失败在连接阶段"的目标。
_HOST_SHAPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
# 云厂商的元数据服务地址：这类主机名永远不可能是对象存储端点，
# 而面板以 root 出网，被人往 endpoint 里塞一个 `169.254.169.254` 就等于借它去取
# 实例角色凭据。这一条与"允许内网自建 MinIO"无关，永远拒。
_DENY_HOSTS = {
    "169.254.169.254", "metadata", "metadata.google.internal", "metadata.goog",
    "metadata.amazon.com", "instance-data",
}


# ---------------------------------------------------------------- 校验与目标表

def _size_or_none(raw) -> int | None:
    """对端没给大小就是 None，不许折成 0。

    S3 兼容实现并不都守约定：有的网关 LIST 不带 `<Size>`，有的 HEAD 不带
    `Content-Length`。折成 0 之后，「远端清单」那一格会印成看着正常的「0 B」，
    拉回的落盘余量预检会因为"只要 0 字节"而当场通过，上传后的字节核对会变成
    「远端 0 与本地 N 不一致」——三种症状都是把「读不到」说成了一个具体的数。
    """
    s = str(raw or "").strip()
    if not (s.isascii() and s.isdigit()):
        return None
    return int(s)


def _norm_prefix(p) -> str:
    s = str(p or "").strip().lstrip("/")
    if not s:
        return ""
    if not _PREFIX_RE.fullmatch(s) or ".." in s.split("/"):
        raise RuntimeError("对象前缀只允许字母、数字与 . _ / -，且不能含 ..")
    return s if s.endswith("/") else s + "/"


def _check_endpoint(endpoint: str) -> dict:
    """把端点拆成 {scheme, host, port}，顺手挡掉不可能是对象存储的写法。

    只允许 http/https（`file://` 会让 root 身份的面板去读本地文件，见 util.is_http_url）；
    不接受 URL 里的用户名口令与查询串——凭据应当来自 access key/secret 两个栏位，
    混在 URL 里既会进审计，又会让人误以为口令已经保存好了。
    """
    s = str(endpoint or "").strip()
    if not s:
        raise RuntimeError("缺少端点（endpoint）")
    # 没写 scheme 时按 https 补齐再判：只填 `minio.example.com:9000` 是最常见的写法，
    # 直接拒掉会把人引去猜语法，而补成 http 又等于默认放弃 TLS。
    if "//" not in s:
        s = f"https://{s}"
    if not is_http_url(s):
        raise RuntimeError("端点必须是 http/https 地址，例如 https://minio.example.com:9000")
    u = urlsplit(s)
    if u.username or u.password or "@" in (u.netloc or ""):
        raise RuntimeError("端点里不要带用户名口令，Access Key 与 Secret Key 有各自的栏位")
    if u.query or u.fragment:
        raise RuntimeError("端点里不要带查询串或 # 片段")
    host = u.hostname or ""
    if not host:
        raise RuntimeError("端点里读不出主机名")
    if not _HOST_SHAPE_RE.fullmatch(host):
        # urlsplit 把 IPv6 的方括号去掉了，所以 `::1` 这类字面量走 ipaddress 判。
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise RuntimeError("端点里的主机名不合法：请用域名或 IP，例如 https://minio.example.com:9000") from None
    if host.lower() in _DENY_HOSTS:
        raise RuntimeError(f"{host} 是云厂商的元数据地址，不是对象存储端点")
    try:
        port = u.port
    except ValueError:
        raise RuntimeError("端点里的端口不是数字") from None
    if port is not None and not 1 <= port <= 65535:
        raise RuntimeError("端点里的端口超出范围")
    return {"scheme": u.scheme, "host": host, "port": port or (443 if u.scheme == "https" else 80)}


def _assert_not_metadata(host: str) -> None:
    """出网前把解析结果再看一眼：主机名指向链路本地/元数据段就拒绝。

    建目标时判一次不够——`store.example.com` 今天解析到自家 MinIO、明天被改成
    169.254.169.254，请求照样是面板以 root 发出去的。解析失败不在这里下结论，
    让真正的连接去报（否则离线机器上连"测试连通性"都点不动）。
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addrs = [literal] if literal else []
    if literal is None:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return
        addrs = [ipaddress.ip_address(i[4][0]) for i in infos if i[4]]
    for a in addrs:
        if a.is_link_local:
            raise RuntimeError(f"{host} 解析到 {a}，那是链路本地/元数据段，面板不会替谁去取实例凭据")


def _secret_ok(secret: str) -> str:
    s = str(secret or "")
    if len(s) < 8 or len(s) > 256:
        raise RuntimeError("Secret Key 长度要在 8-256 之间")
    if any(ord(c) < 32 or ord(c) == 127 for c in s):
        raise RuntimeError("Secret Key 里不允许控制字符")
    return s


def _clean(body: dict, *, partial: bool = False) -> dict:
    """把请求体收成可入库的字段；partial=True 时只动出现过的键（改目标用）。

    口令走"给了才换"：列表与详情从不回显 Secret Key，浏览器那个输入框永远是空的，
    保存时留空必须理解为"没改"，否则一次普通编辑就把手上唯一那份凭据清没了。
    """
    out: dict = {}
    keys = set(body or {})

    def want(k: str) -> bool:
        return not partial or k in keys

    if want("name"):
        name = str(body.get("name") or "").strip()
        if not is_name(name):
            raise RuntimeError("目标名只允许小写字母、数字与连字符，2-39 位，且以字母开头")
        out["name"] = name
    if want("endpoint"):
        ep = _check_endpoint(body.get("endpoint"))
        extra = f":{ep['port']}" if ep["port"] not in (80, 443) else ""
        out["endpoint"] = f"{ep['scheme']}://{ep['host']}{extra}"
    if want("bucket"):
        b = str(body.get("bucket") or "").strip()
        if not _NAME_RE.fullmatch(b) or ".." in b or ".-" in b or "-." in b:
            raise RuntimeError("桶名不合法（小写字母、数字、点、连字符，3-63 位，不能以点或连字符收尾）")
        out["bucket"] = b
    if want("region"):
        rg = str(body.get("region") or "us-east-1").strip()
        if not _REGION_RE.fullmatch(rg):
            raise RuntimeError("区域名不合法")
        out["region"] = rg
    if want("access_key"):
        ak = str(body.get("access_key") or "").strip()
        if not _AKID_RE.fullmatch(ak):
            raise RuntimeError("Access Key 只允许字母、数字与 . _ -，4-128 位")
        out["access_key"] = ak
    if "secret_key" in keys:
        sk = str(body.get("secret_key") or "")
        if not (partial and not sk):
            out["secret_key"] = _secret_ok(sk)
    if want("prefix"):
        out["prefix"] = _norm_prefix(body.get("prefix"))
    for flag in ("path_style", "enabled"):
        # 只看"这次给了没有"，不用 want()：新建时缺省也要写一列的话，写进去的就是 0，
        # 而库里这两列的默认值是 path_style=0 / enabled=1。实测后果是
        # `choyeonctl backup add-target`（不带 --enable）建出来的目标一出生就停用，
        # push_pending 永远返回"已停用"，异地那份副本一次都没写过。
        if flag in keys:
            out[flag] = 1 if body.get(flag) in (1, True, "1", "true", "yes", "on") else 0
    return out


def list_targets() -> list[dict]:
    rows = dbm.query("SELECT * FROM cloud_targets ORDER BY id")
    # 关联任务与已推送份数各来一次 GROUP BY，别按目标逐条查（目标页每 15 秒自动刷新）
    links: dict[int, list] = {}
    for x in dbm.query("SELECT id, kind, target, cloud_target_id FROM backups WHERE cloud_target_id IS NOT NULL"):
        links.setdefault(int(x["cloud_target_id"]), []).append(
            {"id": x["id"], "kind": x["kind"], "target": x["target"]})
    counts = {int(r["target_id"]): int(r["c"]) for r in
              dbm.query("SELECT target_id, COUNT(*) c FROM backup_pushes GROUP BY target_id")}
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "name": r["name"], "endpoint": r["endpoint"], "bucket": r["bucket"],
            "region": r["region"], "prefix": r["prefix"], "path_style": bool(r["path_style"]),
            "enabled": bool(r["enabled"]), "created_at": r["created_at"],
            "last_push_at": r["last_push_at"], "last_error": redact_creds(r["last_error"] or ""),
            "access_key_masked": _mask_akid(r["access_key"]), "has_secret": bool(r["secret_key"]),
            "pushed_count": counts.get(int(r["id"]), 0), "linked": links.get(int(r["id"]), []),
        })
    return out


def _mask_akid(ak: str) -> str:
    s = str(ak or "")
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}…{s[-4:]}"


def get_secret(tid: int) -> dict:
    """内部取完整凭据：只有签名这一条路用，任何对外结构都不许带上它。"""
    r = dbm.query_one("SELECT * FROM cloud_targets WHERE id=?", (int(tid),))
    if not r:
        raise RuntimeError(f"异地目标 {tid} 不存在")
    if not r["secret_key"]:
        raise RuntimeError(f"异地目标 {r['name']} 没有保存 Secret Key，请重新填一次")
    return r


def create_target(body: dict) -> dict:
    c = _clean(body)
    for k in ("name", "endpoint", "bucket", "access_key", "secret_key"):
        if k not in c:
            raise RuntimeError(f"新建异地目标需要 {k}")
    c.setdefault("prefix", "")
    cols = ", ".join(c)
    ph = ", ".join("?" * len(c))
    try:
        tid = dbm.execute(f"INSERT INTO cloud_targets({cols}) VALUES({ph})", tuple(c.values()))
    except sqlite3.IntegrityError:
        raise RuntimeError(f"目标名 {c['name']} 已存在") from None
    return {"ok": True, "id": int(tid), "name": c["name"]}


def update_target(tid: int, body: dict) -> dict:
    c = _clean(body, partial=True)
    if not dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (int(tid),)):
        raise RuntimeError(f"异地目标 {tid} 不存在")
    if not c:
        return {"ok": True, "id": int(tid), "changed": []}
    sets = ", ".join(f"{k}=?" for k in c)
    try:
        dbm.execute(f"UPDATE cloud_targets SET {sets} WHERE id=?", (*c.values(), int(tid)))
    except sqlite3.IntegrityError:
        # 只 patch 别的列也会走到这里：判词不能写死 `c["name"]`，否则名字冲突以外的
        # 约束问题会被读成一句内部错误（KeyError: 'name'），而那次 UPDATE 其实被拒了。
        raise RuntimeError(
            f"目标名 {c['name']} 已存在" if "name" in c else "这次改动撞上库里的唯一约束，已拒绝") from None
    return {"ok": True, "id": int(tid), "changed": sorted(c)}


def delete_target(tid: int) -> dict:
    """删目标只是不再往它那儿推，**不删桶里已有的对象**。

    远端对象是一份可能正被人用来恢复的数据，"删掉这条配置"和"删掉那 7 份异地备份"
    是两个决定，不该由一次点击一起做。所以还挂着任务时先拒绝；删完把推送记录一起清掉，
    免得下一张同名目标继承"已推送 N 份"的旧状态（批次㉜同一条理由）。
    """
    linked = dbm.query("SELECT id, kind, target FROM backups WHERE cloud_target_id=?", (int(tid),))
    if linked:
        names = name_list([f"#{x['id']}（{x['kind']}:{x['target']}）" for x in linked], 5, unit="条")
        raise RuntimeError(f"还有备份任务挂在这个目标上：{names}。先取消关联，再删目标")
    if not dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (int(tid),)):
        raise RuntimeError(f"异地目标 {tid} 不存在")
    n = dbm.query_one("SELECT COUNT(*) c FROM backup_pushes WHERE target_id=?", (int(tid),))
    # 台账与目标同一事务：先删台账后删目标若第二条失败，目标照旧在列表里、
    # 却已经"从没推过任何东西"，而 records_dropped 那句承诺也一并落空。
    dbm.execute_many([
        ("DELETE FROM backup_pushes WHERE target_id=?", (int(tid),)),
        ("DELETE FROM cloud_targets WHERE id=?", (int(tid),)),
    ])
    return {"ok": True, "id": int(tid), "records_dropped": int((n or {}).get("c") or 0)}


# ------------------------------------------------------------------ SigV4 签名

def _sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _file_sha256(path: str) -> str:
    """流式算 payload 哈希：归档可能几百 MB，读进内存再哈希会当场把面板顶到 OOM。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_uri(raw_path: str) -> str:
    # S3 的 canonical URI 保留斜杠，其余按 RFC3986；`~` 不编码是签名规范里的例外。
    # 同一个串既用来签名又用来发请求线，两边不一致只会得到一句 SignatureDoesNotMatch。
    return quote(raw_path or "/", safe="/~")


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        # 先还原再编码：查询串在发出前已经编码过一次，直接再编码会把 %2F 变成 %252F，
        # 于是线上发的是一个串、签名算的是另一个串，对端只回一句 SignatureDoesNotMatch。
        # 带 `/` 的对象前缀（panel/db/）与翻页 token 里的 +/= 是最常见的两种写法。
        pairs.append((quote(unquote(k), safe="-_.~"), quote(unquote(v), safe="-_.~")))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def signing_key(secret: str, datestamp: str, region: str, service: str = "s3") -> bytes:
    k = hmac.new(f"AWS4{secret}".encode(), datestamp.encode(), hashlib.sha256).digest()
    k = hmac.new(k, region.encode(), hashlib.sha256).digest()
    k = hmac.new(k, service.encode(), hashlib.sha256).digest()
    return hmac.new(k, b"aws4_request", hashlib.sha256).digest()


def authorization(*, method: str, path: str, query: str, headers: dict, payload_hash: str,
                  access_key: str, secret: str, region: str, when: datetime) -> tuple[str, str]:
    """按 SigV4 规范算出 Authorization 值，同时回传规范请求串供测试核对。

    headers 传小写键；值里的连续空白压成单个空格、首尾空白裁掉，这是规范里
    "canonical header value" 的定义，少这一步签名就对不上，而对端只回一句
    SignatureDoesNotMatch，看不出是哪一行的锅。
    """
    items = sorted((k.lower(), re.sub(r"\s+", " ", str(v).strip())) for k, v in headers.items())
    canon_headers = "".join(f"{k}:{v}\n" for k, v in items)
    signed_headers = ";".join(k for k, _ in items)
    canonical = "\n".join([
        method.upper(), _canonical_uri(path), _canonical_query(query),
        canon_headers, signed_headers, payload_hash,
    ])
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    datestamp = when.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical.encode())])
    sig = hmac.new(signing_key(secret, datestamp, region), string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")
    return auth, canonical


# -------------------------------------------------------------------- 传输层

class _DeadlineReader:
    """带总时限的文件读取壳：http.client 的 timeout 只管单次 socket 操作。

    对端收得不快（带宽被限速、连接半死）时，`send()` 会在每一次 write 里等，
    单次都不超时、整场却可以挂到天亮；而 runner 是被 systemd oneshot 拉起的，
    实测这一层不封顶，卡住的推送会攥着 flock 让之后每一次定时备份都"跳过"。
    """

    def __init__(self, fh, deadline: float, timeout: float):
        self._fh = fh
        self._deadline = deadline
        self._timeout = timeout

    def read(self, n: int = -1) -> bytes:
        if time.monotonic() > self._deadline:
            raise TimeoutError(f"异地上传超过 {self._timeout:g}s 时限，已中断这次 PutObject")
        return self._fh.read(n)


def _request(t: dict, method: str, key: str = "", query: str = "", body=None,
             body_len: int = 0, payload_hash: str = EMPTY_SHA256,
             timeout: float | None = None, extra: dict | None = None) -> tuple[int, dict, bytes]:
    """发一个已签名的请求，回 (状态码, 响应头, 正文)。

    寻址方式在这里一次定死：虚拟主机风格把桶名放到域名最左、路径就是 `/键`；
    path-style 把桶名放进路径第一段。填错方向的症状是"桶不存在"，
    所以测试连通性的结果里会写清楚这次用的是哪一种。
    """
    ep = _check_endpoint(t["endpoint"])
    _assert_not_metadata(ep["host"])
    timeout = timeout or config.CLOUD_PUSH_TIMEOUT_SEC
    host = ep["host"] if t["path_style"] else f"{t['bucket']}.{ep['host']}"
    bucket = str(t["bucket"])
    if t["path_style"]:
        raw = f"/{bucket}/{key}" if key else f"/{bucket}"
    else:
        raw = f"/{key}" if key else "/"
    uri = _canonical_uri(raw)
    headers = {"host": host, "x-amz-content-sha256": payload_hash}
    when = datetime.now(UTC)
    headers["x-amz-date"] = when.strftime("%Y%m%dT%H%M%SZ")
    for k, v in (extra or {}).items():
        headers[k.lower()] = str(v)
    if body is not None:
        headers["content-length"] = str(body_len)
    auth, _ = authorization(method=method, path=raw, query=query, headers=headers,
                            payload_hash=payload_hash, access_key=t["access_key"],
                            secret=t["secret_key"], region=t["region"], when=when)
    headers["authorization"] = auth
    conn = (HTTPSConnection(ep["host"], ep["port"], timeout=timeout, context=ssl.create_default_context())
            if ep["scheme"] == "https" else HTTPConnection(ep["host"], ep["port"], timeout=timeout))
    try:
        conn.request(method, f"{uri}?{query}" if query else uri, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read(MAX_LIST_BYTES + 1)
        rheaders = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, rheaders, data
    finally:
        conn.close()


def _fail_detail(status: int, headers: dict, body: bytes, t: dict) -> str:
    """把对端的 <Code>/<Message> 收成一句判词，区域不对时点名正确的区域。

    只取 Code/Message，不整段回显：S3 的错误 XML 里会带上这次签名用的 AccessKeyId
    与 StringToSign 片段，那段文本会进审计、也会进浏览器。
    """
    region_hint = headers.get("x-amz-bucket-region") or ""
    code = message = ""
    try:
        root = ET.fromstring(body[:200000].decode("utf-8", "replace"))
        ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
        code = (root.findtext(f"{ns}Code") or root.findtext("Code") or "").strip()
        message = (root.findtext(f"{ns}Message") or root.findtext("Message") or "").strip()[:200]
    except ET.ParseError:
        pass
    if status in (301, 302, 307, 400) and region_hint and region_hint != str(t["region"]):
        return f"桶在 {region_hint} 区，目标里填的是 {t['region']}：改区域后重试"
    if status in (401, 403):
        return (f"凭据被拒（HTTP {status}"
                f"{'/' + code if code else ''}）：核对 Access Key、Secret Key 与这个桶的写入权限")
    if status == 404:
        return "桶或对象不存在（HTTP 404）：核对桶名与寻址方式（虚拟主机 / path-style）"
    if status == 400 and code == "SignatureDoesNotMatch":
        return "签名对不上（SignatureDoesNotMatch）：Secret Key 少一位或多一位都会是这个结果"
    return f"HTTP {status}" + (f"/{code}" if code else "") + (f"：{message}" if message else "")


# ---------------------------------------------------------------- 对象存储动作

def head_bucket(t: dict) -> dict:
    status, headers, body = _request(t, "HEAD", timeout=config.CLOUD_PROBE_TIMEOUT_SEC)
    if status != 200:
        raise RuntimeError(_fail_detail(status, headers, body, t))
    return {"ok": True, "addressing": "path-style" if t["path_style"] else "virtual-host",
            "request_id": headers.get("x-amz-request-id", "")}


def list_keys(t: dict, prefix: str) -> tuple[list[dict], bool]:
    """列一个前缀下的对象（list-type=2，翻页到底），返回 (条目, 是否可能没读完)。"""
    out: list[dict] = []
    token = ""
    truncated = False
    for _ in range(LIST_PAGES):
        q = f"list-type=2&max-keys=1000&prefix={quote(prefix, safe='')}"
        if token:
            q += f"&continuation-token={quote(token, safe='')}"
        status, headers, body = _request(t, "GET", query=q, timeout=config.CLOUD_PROBE_TIMEOUT_SEC)
        if status != 200:
            raise RuntimeError(_fail_detail(status, headers, body, t))
        root = ET.fromstring(body.decode("utf-8", "replace"))
        ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
        for c in root.findall(f"{ns}Contents"):
            out.append({
                "key": c.findtext(f"{ns}Key") or "",
                "size": _size_or_none(c.findtext(f"{ns}Size")),
                "modified": c.findtext(f"{ns}LastModified") or "",
            })
        truncated = (root.findtext(f"{ns}IsTruncated") or "false").lower() == "true"
        if not truncated:
            return out, False
        token = root.findtext(f"{ns}NextContinuationToken") or ""
        if not token:
            return out, True
    return out, True


def head_object(t: dict, key: str) -> dict | None:
    status, headers, body = _request(t, "HEAD", key=key, timeout=config.CLOUD_PROBE_TIMEOUT_SEC)
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(_fail_detail(status, headers, body, t))
    return {"key": key, "size": _size_or_none(headers.get("content-length"))}


def put_object(t: dict, key: str, path: str, timeout: float | None = None) -> dict:
    size = os.path.getsize(path)
    if size > MAX_SINGLE_PUT:
        raise RuntimeError(
            f"这份产物 {human_bytes(size)} 超过单文件上传上限 {human_bytes(MAX_SINGLE_PUT)}，"
            "面板只做一次 PutObject、不做分块上传：请缩小备份范围（只备库，不备整目录）"
        )
    payload_hash = _file_sha256(path)
    to = timeout or config.CLOUD_PUSH_TIMEOUT_SEC
    deadline = time.monotonic() + to
    with open(path, "rb") as f:
        status, headers, body = _request(
            t, "PUT", key=key, body=_DeadlineReader(f, deadline, to), body_len=size,
            payload_hash=payload_hash, timeout=to, extra={"content-type": "application/gzip"},
        )
    if status not in (200, 201, 204):
        raise RuntimeError(_fail_detail(status, headers, body, t))
    got = head_object(t, key)
    if not got:
        raise RuntimeError("上传后 HEAD 读不到这个对象：对端收了请求却没留下文件，先查桶权限与生命周期规则")
    if got["size"] != size:
        if got["size"] is None:
            # 传了却不给核对：这一份是不是按原样落进桶里，面板无法确认。
            # 报成功会把"异地有副本"写成假账，而备份这件事的全部意义就是这本账。
            raise RuntimeError(
                f"上传后 HEAD 没给大小：{human_bytes(size)} 的本地文件没法与远端核对字节，"
                "本次不算推送成功（对端 HEAD 应返回 Content-Length，请检查网关或换 path-style 再试）")
        raise RuntimeError(f"远端大小 {got['size']} 与本地 {size} 不一致，本次不算推送成功")
    return {"key": key, "bytes": size, "request_id": headers.get("x-amz-request-id", "")}


def delete_object(t: dict, key: str) -> None:
    status, headers, body = _request(t, "DELETE", key=key, timeout=config.CLOUD_PROBE_TIMEOUT_SEC)
    if status not in (200, 204, 404):
        raise RuntimeError(_fail_detail(status, headers, body, t))


def get_object(t: dict, key: str, dest: str, *, limit: int = MAX_SINGLE_GET,
               timeout: float | None = None, budget_dir: str = "",
               budget_label: str = "") -> dict:
    """流式 GET 一个对象写到 dest，声明大小与实读字节都判；任何中途失败都不留半份文件。

    不能走 `_request`：它把响应整段读进内存（且只读 MAX_LIST_BYTES 那么多），
    一份几百 MB 的归档要么先把面板进程撑爆，要么被截成一份"看着完整"的半成品。
    这里自己开连接、按块落盘，并且判两次大小：
    Content-Length 超过上限就在写第一个字节之前拒绝，实读字节越过上限（对端把
    Content-Length 报小、实际发得更多）就断开删掉临时文件——后者正是"半份备份
    躺在目录里等着被恢复"的来路。
    """
    ep = _check_endpoint(t["endpoint"])
    _assert_not_metadata(ep["host"])
    to = timeout or config.CLOUD_PUSH_TIMEOUT_SEC
    deadline = time.monotonic() + to
    host = ep["host"] if t["path_style"] else f"{t['bucket']}.{ep['host']}"
    raw = f"/{t['bucket']}/{key}" if t["path_style"] else f"/{key}"
    uri = _canonical_uri(raw)
    headers = {"host": host, "x-amz-content-sha256": EMPTY_SHA256}
    when = datetime.now(UTC)
    headers["x-amz-date"] = when.strftime("%Y%m%dT%H%M%SZ")
    auth, _ = authorization(method="GET", path=raw, query="", headers=headers,
                            payload_hash=EMPTY_SHA256, access_key=t["access_key"],
                            secret=t["secret_key"], region=t["region"], when=when)
    headers["authorization"] = auth
    conn = (HTTPSConnection(ep["host"], ep["port"], timeout=to, context=ssl.create_default_context())
            if ep["scheme"] == "https" else HTTPConnection(ep["host"], ep["port"], timeout=to))
    written = 0
    try:
        conn.request("GET", uri, headers=headers)
        resp = conn.getresponse()
        rheaders = {k.lower(): v for k, v in resp.getheaders()}
        if resp.status != 200:
            raise RuntimeError(_fail_detail(resp.status, rheaders, resp.read(200000), t))
        declared = _size_or_none(rheaders.get("content-length"))
        if declared is not None and declared > limit:
            raise RuntimeError(
                f"远端这份对象声明 {human_bytes(declared)}，超过单文件拉回上限 "
                f"{human_bytes(limit)}：面板不做分块下载，请先在桶侧裁剪这个备份"
            )
        if budget_dir and declared:
            # 对端 HEAD 不给大小时，落盘余量这道闸就在第一个字节写下去之前在这里补上；
            # 这里也读不到大小（分块传输）就只能靠上限与写失败的报错兜底，不假装检过了。
            from .files_service import assert_write_budget
            assert_write_budget(budget_dir, declared, budget_label or "写入这份对象")
        try:
            with open(dest, "wb") as f:
                while True:
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"传输超过 {to} 秒未完成，已中断（对端太慢或被掐断）")
                    chunk = resp.read(GET_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise RuntimeError(
                            f"实际已传 {human_bytes(written)}，超过上限 {human_bytes(limit)}"
                            f"（对端声明的是"
                            f"{'「读不到」' if declared is None else human_bytes(declared)}）：已中断"
                        )
                    f.write(chunk)
        except OSError as e:
            raise RuntimeError(f"写到 {dest} 失败：{e.strerror or e}") from None
        # 两条完整性判据必须在上面那个 try 之内：走到这一步的文件已经躺在磁盘上，
        # 判完再抛就等于"报错了但半成品还在"——而它正是会被恢复按钮挑中的那一份。
        if not written:
            raise RuntimeError("对端返回 200 却是空对象：这份备份在桶里是 0 字节，拉回来也没用")
        if declared is not None and written != declared:
            raise RuntimeError(
                f"传输被截断：对端声明 {human_bytes(declared)}，实际收到 {human_bytes(written)}"
                "（连接中途断开时会出现这种账），已删除半成品")
    except Exception:  # noqa: BLE001
        # 失败路径上这个文件一定是半成品——不管它是被写到一半、还是一个字节都没写进去
        # 就报错（open 之后才有句柄，open 之前 dest 还不存在）。留不得。
        with suppress(OSError):
            os.unlink(dest)
        raise
    finally:
        conn.close()
    return {"key": key, "bytes": written, "declared": declared,
            "request_id": rheaders.get("x-amz-request-id", "")}


# ------------------------------------------------------------------- 业务动作

def _auto_re(bid: int):
    """远端键的"本任务自动生成"形状与 runner 的 prune 用同一份正则。

    两处各写一份的话，异地 prune 会认不出带同秒序号的那一份（`bk1-…-03-41-00-2-…`），
    或者反过来把人工上传的产物当最旧的删掉——批次㉝/㊳ 已经实测过形状不一致的代价。
    """
    from ..backup_runner import auto_pattern  # 函数级导入：runner 也引这一层，模块级会成环

    return re.compile(auto_pattern(int(bid)))


def already_pushed(target_id: int, name: str) -> bool:
    return bool(dbm.query_one("SELECT id FROM backup_pushes WHERE target_id=? AND name=?",
                              (int(target_id), str(name))))


def push_file(target_id: int, bid: int, name: str) -> dict:
    """把一份本地产物推到异地目标；成功后记一行，失败把原因留在 last_error（由调用方负责）。"""
    t = get_secret(target_id)
    local = os.path.join(config.BACKUP_DIR, name)
    if not os.path.isfile(local):
        raise RuntimeError(f"本地产物 {name} 不存在，无法推送")
    key = f"{_norm_prefix(t['prefix'])}{name}"
    res = put_object(t, key, local)
    dbm.execute(
        "INSERT INTO backup_pushes(target_id,backup_id,name,key,bytes) VALUES(?,?,?,?,?) "
        "ON CONFLICT(target_id,name) DO UPDATE SET bytes=excluded.bytes, pushed_at=datetime('now')",
        (int(target_id), int(bid), name, key, int(res["bytes"])),
    )
    dbm.execute("UPDATE cloud_targets SET last_push_at=datetime('now'), last_error='' WHERE id=?",
                (int(target_id),))
    return {"ok": True, "target": t["name"], "key": key, "bytes": res["bytes"]}


def prune_remote(target_id: int, bk: dict) -> dict:
    """按同一个 keep 名额删远端最旧的几份，只碰本前缀下形状认得出的键。"""
    t = get_secret(target_id)
    rx = _auto_re(int(bk["id"]))
    prefix = _norm_prefix(t["prefix"])
    rows, truncated = list_keys(t, prefix)
    if truncated:
        # 名册没翻完就照着它删，删的是"看得见的最旧"而不是"真的最旧"：前缀下堆了几万个
        # 对象时（同桶多个任务、多年产物、或这前缀还放着别的东西），`while len(mine) > keep`
        # 会把翻到的近两万份一路删到只剩 keep 份——一次异地剪枝变成清空这个前缀。
        # 所以这里不动手，只把"为什么没剪"如实交回调用方：少剪几份只是多占桶，
        # 误删是不可逆的。
        return {"ok": False, "deleted": [], "remote_count": len(rows), "truncated": True,
                "skipped": f"远端清单没翻完（最多 {LIST_PAGES} 页 × 1000 个对象），"
                           f"按不完整的名册删最旧的会删错，本轮不删"}
    mine = []
    for r in rows:
        name = r["key"][len(prefix):] if r["key"].startswith(prefix) else ""
        m = rx.match(name) if name else None
        if m:
            mine.append(((m.group(1), int(m.group(2) or 0), name), r["key"]))
    mine.sort()
    deleted = []
    while len(mine) > int(bk["keep"]):
        _, key = mine.pop(0)
        delete_object(t, key)
        dbm.execute("DELETE FROM backup_pushes WHERE target_id=? AND name=?",
                    (int(target_id), key[len(prefix):]))
        deleted.append(key[len(prefix):])
    return {"ok": True, "deleted": deleted, "remote_count": len(mine), "truncated": truncated}


def probe(target_id: int) -> dict:
    """测试连通性：桶在、凭据对、我们写的键看得见，三件事分别说清。"""
    t = get_secret(target_id)
    res = head_bucket(t)
    rows, truncated = list_keys(t, _norm_prefix(t["prefix"]))
    # 合计只有在每一条都读到大小时才是"这个前缀下的总量"：少一条就只能整体报未知，
    # 并说清是几条没给——报一个偏小的数会让人以为桶里就这么多。
    unknown_sizes = sum(1 for r in rows if r["size"] is None)
    res.update({
        "target": t["name"], "bucket": t["bucket"], "endpoint": t["endpoint"], "region": t["region"],
        "prefix": t["prefix"], "objects_under_prefix": len(rows),
        "bytes_under_prefix": None if unknown_sizes else sum(r["size"] for r in rows),
        "size_unknown_objects": unknown_sizes, "list_truncated": truncated,
    })
    dbm.execute("UPDATE cloud_targets SET last_error='' WHERE id=?", (int(target_id),))
    return res


def push_pending(bk: dict) -> dict:
    """备份跑完后把这份任务尚未推送的产物都推上去（正常情况下就是刚写的那一份）。

    没挂目标时返回 skipped 而不是报错：runner 每条任务都会走这里，
    "这个任务本来就没有异地"不是故障，把它记成失败会让 journal 里全是假警报。
    目标被停用也是 skipped：界面那把"停用"开关必须真能停住事，否则它只是个装饰——
    用户以为副本不再往这个桶里写了，runner 却照推不误。
    """
    bid = int(bk["id"])
    tid = bk.get("cloud_target_id")
    if not tid:
        return {"ok": True, "skipped": "未关联异地目标"}
    t = dbm.query_one("SELECT id,name,enabled FROM cloud_targets WHERE id=?", (int(tid),))
    if not t:
        raise RuntimeError(f"备份任务 {bid} 关联的异地目标 {tid} 已不存在")
    if not t["enabled"]:
        return {"ok": True, "skipped": f"异地目标「{t['name']}」已停用"}
    from ..backup_runner import auto_files  # 函数级导入：避开与 backup_runner 的模块级循环

    todo = [n for n in auto_files(bid) if not already_pushed(int(tid), n)]
    if not todo:
        return {"ok": True, "skipped": "没有待推送的产物"}
    pushed = []
    try:
        for name in todo:
            pushed.append(push_file(int(tid), bid, name))
        pruned = prune_remote(int(tid), bk)
        out = {"ok": True, "pushed": pushed, "pruned": pruned["deleted"],
               "remote_count": pruned["remote_count"]}
        if pruned.get("skipped"):
            # LIST_PAGES 那句注释要落到这里才有意义："翻不满就如实报 truncated，而不是把
            # prune 当成功"。副本一份都没少删是"远端还在长"，不是"剪枝做完了"。
            out["prune_skipped"] = pruned["skipped"]
        return out
    except Exception as e:  # noqa: BLE001
        note = redact_creds(str(e))[:300]
        dbm.execute("UPDATE cloud_targets SET last_error=? WHERE id=?", (note, int(tid)))
        raise RuntimeError(note) from None


def push_now(bid: int, target_id: int | None = None) -> dict:
    """界面上的"立即推送"：只推这个任务尚未推过的那几份，已推过的不重传。

    目标缺省时用这条任务自己关联的那个——推送按钮就长在任务行上，那一行已经写着
    它挂在哪个桶；让前端再传一次 id 只是多一个能传错的参数（传成 0 会报成
    "异地目标 0 不存在"，看起来像数据坏了，其实是按钮没带上参数）。
    停用中的目标直接拒绝：`push_pending` 对定时任务把它算作 skipped，而这里是用户
    明确点下去的一下，静默"没推"比一句"要先启用"难查得多。
    """
    bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (int(bid),))
    if not bk:
        raise RuntimeError(f"备份任务 {bid} 不存在")
    tid = int(target_id) if target_id else int(bk["cloud_target_id"] or 0)
    if not tid:
        raise RuntimeError(f"备份任务 {bid} 没有关联异地目标")
    t = dbm.query_one("SELECT name,enabled FROM cloud_targets WHERE id=?", (tid,))
    if not t:
        raise RuntimeError(f"异地目标 {tid} 不存在")
    if not t["enabled"]:
        raise RuntimeError(f"异地目标「{t['name']}」已停用，先启用再推送")
    return push_pending({**bk, "cloud_target_id": tid})


def list_for_job(target_id: int, bid: int) -> dict:
    """这条任务在远端桶里有哪几份产物，本机有没有同一份。

    为什么要有这一句：`probe` 只回「这个前缀下有 N 个对象」，机器重装之后本机目录是空的，
    管理员站在面板前看得见"桶里有 7 份"，却看不见是哪 7 份、多大、哪一份最新，
    更没法把任何一份拉回来——而恢复只能读本机的文件。列出来才谈得上拉回。

    归属判据用 `backup_service.name_owner`（本机列表用的就是它），不是 prune 那条
    `auto_pattern`：手工上传进任务的 `bk3-manual-dump.sql` 在本机列得出来、
    也被推到了桶里，但它不属于"自动生成的那批"，prune 永远不该删它，
    列表与拉回却必须认它。两处判据不同是有意的，各写各的会把这个区别抹平。
    """
    from .backup_service import name_owner  # 函数级导入：backup_service 顶部就引了 runner

    t = get_secret(target_id)
    prefix = _norm_prefix(t["prefix"])
    rows, truncated = list_keys(t, prefix)
    try:
        local = set(os.listdir(config.BACKUP_DIR))
    except OSError:
        local = set()  # 本机目录还不存在（重装后第一次）：一份都没有，全部可拉
    files = []
    for r in rows:
        name = r["key"][len(prefix):] if r["key"].startswith(prefix) else ""
        if name_owner(name) != int(bid):
            continue  # 同桶里别的任务、别人的对象、以及带子目录的键都不算这一份的
        files.append({"name": name, "key": r["key"], "size": r["size"],
                      "modified": str(r["modified"] or ""), "local": name in local})
    files.sort(key=lambda f: (f["modified"], f["name"]), reverse=True)
    return {"ok": True, "target": t["name"], "bucket": t["bucket"], "prefix": t["prefix"],
            "job": int(bid), "files": files, "remote_total": len(rows), "truncated": truncated}


def pull_one(target_id: int, bid: int, name: str) -> dict:
    """把远端的一份产物拉回本机备份目录（同名已存在就拒绝，不覆盖）。

    这是「看得见」的下一步：桶里唯一的副本要能落到本机，恢复按钮才点得动。
    四道闸都在动手之前——名字归属（不是这条任务的一律不拉，顺带挡住 `../`）、
    远端声明大小、落盘余量（设置页那条 alert_disk_pct，与上传/解包同一个读者）、
    以及实读字节上界。落盘走「同目录临时文件 + os.replace」：中途断线留下的是
    一个不带 `bk<id>-` 前缀的隐藏 .tmp，列表与恢复都认不到它，不会出现
    "半份备份躺在目录里等着被恢复"。
    """
    from .backup_service import _restoreable, name_owner
    from .files_service import assert_write_budget

    job = dbm.query_one("SELECT id,kind FROM backups WHERE id=?", (int(bid),))
    if not job:
        raise RuntimeError(f"备份任务 {bid} 不存在")
    if name_owner(name) != int(bid):
        raise RuntimeError(f"{name} 不是任务 {bid} 名下的产物形状（要以 bk{bid}- 开头且不带目录）")
    t = get_secret(target_id)
    key = f"{_norm_prefix(t['prefix'])}{name}"
    head = head_object(t, key)
    if not head:
        raise RuntimeError(f"远端没有这个对象：桶 {t['bucket']} 前缀下的 {name}")
    size = head["size"]
    if size is not None and size > MAX_SINGLE_GET:
        raise RuntimeError(
            f"远端这份产物 {human_bytes(size)} 超过单文件拉回上限 {human_bytes(MAX_SINGLE_GET)}，"
            "面板不做分块下载：请先在桶侧裁剪这个备份")
    dest = os.path.join(config.BACKUP_DIR, name)
    if os.path.islink(dest):
        # 与 save_upload 同一口径：replace 会把链接换成真文件，等于替用户把
        # "这份备份其实躺在别处"的线索抹掉。
        raise RuntimeError("本机同名路径是符号链接，为避免写到别处已拒绝")
    if os.path.exists(dest):
        raise RuntimeError(f"本机已有同名产物：{name}（要先覆盖请先删掉本机那一份）")
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    if size is not None:
        # 对端 HEAD 没给大小时这里判不了，交给 get_object 在 GET 声明大小之后、
        # 第一个字节落盘之前补判——拿 0 去预检等于把这道闸假装检过了。
        assert_write_budget(config.BACKUP_DIR, size, f"从远端拉回 {name}")
    fd, tmp = tempfile.mkstemp(dir=config.BACKUP_DIR, prefix=f".{name}.", suffix=".pull.tmp")
    os.close(fd)
    try:
        res = get_object(t, key, tmp, budget_dir=config.BACKUP_DIR,
                         budget_label=f"从远端拉回 {name}")
        os.chmod(tmp, 0o640)
        os.replace(tmp, dest)
    finally:
        # replace 成功后这个 unlink 只会碰到"路径已经不在了"；真残留只发生在
        # 进程被掐在两步之间，那几字节由下一次清理目录的人处理（同 save_upload）。
        with suppress(OSError):
            os.unlink(tmp)
    return {"ok": True, "name": name, "bytes": res["bytes"], "target": t["name"],
            "key": key, "job": int(bid), "kind": job["kind"],
            # 能不能直接点恢复，用本机列表那同一个判据（bk0-/panel- 与 .sql.gz 走不了自动恢复）
            "restoreable": bool(_restoreable(job["kind"], name))}
