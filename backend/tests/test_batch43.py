"""批次㊸C：异地备份这一层的契约测试。

㊸A 落地的东西有三重危险叠在一起：签名代码跑在 **root** 进程里，拿着的是**能写别人桶的
长期密钥**，出网目标是用户在表单里填的**任意字符串**。这三样任何一处回归，后果都不是
「备份失败」那么轻：签错的凭据、少遮一处的口令、被人换成元数据地址的端点，分别等于
把密钥交给日志、把口令交给浏览器、把实例角色凭据交给攻击者。

所以这一批按危险排，不按功能排：

1. **签名必须和线上那一份请求严格同源。** 我没有把模块自己的 `authorization()` 拿来当
   期望值——那样这条测试只能证明「代码和它自己一致」。测试里逐字重写了一份 AWS SigV4
   规范（`_ref_*`：四层 HMAC 用循环派生、头值用 `split()` 折叠、路径逐段编码），
   再用**假连接**把真实发出的请求行/请求头录下来，从录到的线上内容反向验签。
2. **凭据不能出现在任何对外文本里。** Secret Key 出接口只有布尔位、Access Key 只有遮罩，
   `redact_creds` 还要能遮住 S3 报错里裸出现的 `AKIA…`/`ASIA…` 和 `Signature=<64hex>`。
3. **出网目标不许是元数据地址。** 建目标时判一次不够，`_request` 每次还要按 DNS 结果再判。
4. **「给了才换」的 PATCH 语义。** 浏览器那个口令框永远是空的，留空必须理解为「没改」。
5. **推送的判重、轮替与失败口径。** 远端 prune 认键的正则必须和 runner 同一个出处；
   异地推不上去只写 last_error，不许把一次成功的本机备份改判成失败。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from urllib.parse import quote, unquote

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_batch43_data_"))
os.environ.setdefault("CP_BACKUP_DIR", tempfile.mkdtemp(prefix="cp_batch43_bak_"))

from app import backup_runner as R  # noqa: E402
from app import config  # noqa: E402
from app import database as dbm
from app.services import backup_service as B  # noqa: E402
from app.services import cloud_backup as C  # noqa: E402
from app.util import redact_creds  # noqa: E402
from cli import EXIT_USAGE, Out, build_parser, cmd_backup, cmd_schema  # noqa: E402

AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
# SigV4 的 scope 里有日期，时刻不锁死就无法比对金标准值
WHEN = datetime(2026, 10, 16, 3, 41, 0, tzinfo=UTC)
GOLDEN_PATH = "/panel/bk7-2026-10-16-03-41-00-app.sqlite.gz"
GOLDEN_HEADERS = {
    "host": "minio.example.com:9000",
    "x-amz-content-sha256": C.EMPTY_SHA256,
    "x-amz-date": "20261016T034100Z",
    "content-type": "application/gzip",
    "content-length": "42",
}

_seq = iter(range(1, 10**6))


def _uid(p: str = "off") -> str:
    """目标名要满足 `^[a-z][a-z0-9-]{1,38}$`；整轮 pytest 共用一个库，必须逐例唯一。"""
    return f"{p}{os.getpid()}{next(_seq):06d}"


def _dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)


# ------------------------------------------------ 规范参考实现（刻意独立于被测模块）

def _ref_canonical_uri(raw_path: str) -> str:
    """规范：路径逐段 URI 编码，段分隔的斜杠保留（S3 明确不许编码斜杠）。"""
    return "/".join(quote(seg, safe="~") for seg in raw_path.split("/")) or "/"


def _ref_canonical_query(query: str) -> str:
    """规范：名字与取值先还原成明文、再按同一规则编码，然后按编码后的串排序。

    「先 unquote 再 quote」不是可省的一步：查询串在发出前已经编码过一次，
    直接再编码会把 `%2F` 变成 `%252F`，签名就对着另一份内容算了。
    """
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((quote(unquote(k), safe="~"), quote(unquote(v), safe="~")))
    return "&".join(f"{a}={b}" for a, b in sorted(pairs))


def _ref_authorization(*, method, raw_path, query, headers, payload_hash,
                       access_key, secret, region, when):
    """照抄 AWS SigV4 的四步伪代码，不调用被测模块的任何函数。"""
    items = sorted((k.lower(), " ".join(str(v).split())) for k, v in headers.items())
    canon_headers = "".join(f"{k}:{v}\n" for k, v in items)
    signed_headers = ";".join(k for k, _ in items)
    creq = "\n".join([
        method.upper(), _ref_canonical_uri(raw_path), _ref_canonical_query(query),
        canon_headers, signed_headers, payload_hash,
    ])
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    datestamp = when.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                     hashlib.sha256(creq.encode()).hexdigest()])
    key = f"AWS4{secret}".encode()
    for part in (datestamp, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")
    return auth, creq, sig


# 金标准：由上面那份规范实现算出并在这里钉死。此后若有人为了让测试通过而同时改
# 参考实现和被测模块，这两个字面量仍然会拒绝。
_GOLDEN_CREQ = "\n".join([
    "PUT",
    GOLDEN_PATH,
    "",
    "content-length:42\ncontent-type:application/gzip\n"
    "host:minio.example.com:9000\n"
    f"x-amz-content-sha256:{C.EMPTY_SHA256}\n"
    "x-amz-date:20261016T034100Z\n",
    "content-length;content-type;host;x-amz-content-sha256;x-amz-date",
    C.EMPTY_SHA256,
])
_GOLDEN_AUTH = ""


def setUpModule():
    global _GOLDEN_AUTH
    auth, creq, sig = _ref_authorization(
        method="PUT", raw_path=GOLDEN_PATH, query="", headers=dict(GOLDEN_HEADERS),
        payload_hash=C.EMPTY_SHA256, access_key=AK, secret=SK, region="us-east-1", when=WHEN)
    assert creq == _GOLDEN_CREQ, f"参考实现算出的规范串与金标准不符：{creq!r}"
    assert re.fullmatch(r"[0-9a-f]{64}", sig)
    _GOLDEN_AUTH = auth


class SignatureSpec(unittest.TestCase):
    """`authorization()` 必须与规范参考实现逐字同结果。"""

    def _call(self, *, method="PUT", path=GOLDEN_PATH, query="", headers=None,
              payload=C.EMPTY_SHA256, region="us-east-1"):
        hs = dict(GOLDEN_HEADERS)
        hs.update(headers or {})
        return C.authorization(method=method, path=path, query=query, headers=hs,
                               payload_hash=payload, access_key=AK, secret=SK,
                               region=region, when=WHEN)

    def test_canonical_request_and_auth_match_the_spec_transcription(self):
        auth, creq = self._call()
        ref_auth, ref_creq, ref_sig = _ref_authorization(
            method="PUT", raw_path=GOLDEN_PATH, query="", headers=dict(GOLDEN_HEADERS),
            payload_hash=C.EMPTY_SHA256, access_key=AK, secret=SK, region="us-east-1", when=WHEN)
        self.assertEqual(creq, ref_creq)
        self.assertEqual(auth, ref_auth)
        self.assertTrue(auth.endswith(f"Signature={ref_sig}"))

    def test_pinned_golden_value(self):
        auth, creq = self._call()
        self.assertEqual(creq, _GOLDEN_CREQ, "规范请求串的形态变了就是签名口径变了，必须显式确认")
        self.assertEqual(auth, _GOLDEN_AUTH)
        self.assertTrue(re.search(r"Signature=[0-9a-f]{64}$", auth), auth)

    def test_scope_and_signed_headers_are_written_into_the_credential(self):
        auth, _ = self._call(region="ap-northeast-1")
        self.assertIn("Credential=AKIAIOSFODNN7EXAMPLE/20261016/ap-northeast-1/s3/aws4_request", auth)
        self.assertIn("SignedHeaders=content-length;content-type;host;"
                      "x-amz-content-sha256;x-amz-date", auth)

    def test_header_whitespace_is_collapsed_and_keys_lowercased(self):
        """`Host: x  ` 与 `host: x`、`b   c` 与 `b c` 必须签出同一个串。"""
        base = {"x-amz-content-sha256": C.EMPTY_SHA256, "x-amz-date": "20261016T034100Z"}
        a1, c1 = C.authorization(method="PUT", path=GOLDEN_PATH, query="", headers={
            **base, "Host": "minio.example.com:9000  ", "x-amz-meta-a": "b   c"},
            payload_hash=C.EMPTY_SHA256, access_key=AK, secret=SK, region="us-east-1", when=WHEN)
        a2, c2 = C.authorization(method="PUT", path=GOLDEN_PATH, query="", headers={
            **base, "host": "minio.example.com:9000", "X-Amz-Meta-A": "b \t c"},
            payload_hash=C.EMPTY_SHA256, access_key=AK, secret=SK, region="us-east-1", when=WHEN)
        self.assertEqual(c1, c2, "首尾空白与连续空白都要折叠成单个空格，少一步就对不上签名")
        self.assertEqual(a1, a2)
        self.assertEqual(c2.count("host:minio.example.com:9000\n"), 1)

    def test_canonical_uri_keeps_slash_and_tilde_and_encodes_the_rest(self):
        self.assertEqual(C._canonical_uri("/a b/c+d/e~f/g.h"), "/a%20b/c%2Bd/e~f/g.h")
        self.assertEqual(C._canonical_uri(""), "/")
        self.assertEqual(C._canonical_uri(GOLDEN_PATH), _ref_canonical_uri(GOLDEN_PATH))
        self.assertEqual(C._canonical_uri("/x%20y"), "/x%2520y",
                         "键里字面的 %20 要重新编码，线上和签名才是同一个串")

    def test_canonical_query_sorts_and_is_idempotent(self):
        q = "list-type=2&max-keys=1000&prefix=panel%2Fdb%2F"
        canon = C._canonical_query(q)
        self.assertEqual(canon, "list-type=2&max-keys=1000&prefix=panel%2Fdb%2F",
                         "已经编码过的取值不能再编码一次：签名必须覆盖线上那一份查询串")
        self.assertEqual(canon, C._canonical_query(canon), "规范化必须幂等")
        self.assertEqual(canon, _ref_canonical_query(q))
        self.assertEqual(C._canonical_query("b=2&a=1"), "a=1&b=2")
        self.assertEqual(C._canonical_query(""), "")


# ---------------------------------------- 线上发出的请求行 == 被签名覆盖的内容

class _Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self._headers = headers or {}

    def read(self, _n=-1):
        return self._body

    def getheaders(self):
        return list(self._headers.items())


_SENT = []


class _Conn:
    """假 http.client 连接：记录真实发出的方法/请求行/头，按预设回一个响应。"""

    responder = None

    def __init__(self, host, port=None, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context

    def request(self, method, url, body=None, headers=None):
        _SENT.append({"method": method, "url": url,
                      "headers": {k.lower(): str(v) for k, v in (headers or {}).items()}})

    def getresponse(self):
        return type(self).responder(**_SENT[-1])

    def close(self):
        pass


class WireMatchesSignature(unittest.TestCase):
    """最容易悄悄坏掉的一环：签名算的内容和对端收到的内容必须是同一份。

    这几条用假连接把**线上请求行**录下来，再用规范参考实现对录到的内容重新算一次签名，
    和 Authorization 头比对。任何一处编码口径分叉（查询串二次编码、路径少编码一段、
    host 与实际请求行不一致）都会在这里变成不等，而不是变成对端那句没有信息量的 403。
    """

    def setUp(self):
        del _SENT[:]
        self.tgt = {"endpoint": "https://minio.example.com:9000", "bucket": "offsite",
                    "region": "us-east-1", "prefix": "panel/db/", "path_style": True,
                    "access_key": AK, "secret_key": SK, "name": "wire"}

    def _run(self, fn, responder):
        _Conn.responder = responder
        with mock.patch.object(C, "HTTPSConnection", _Conn), \
             mock.patch.object(C.config, "CLOUD_PROBE_TIMEOUT_SEC", 20):
            out = fn()
        return out, _SENT[-1]

    def _assert_signed_over_wire(self, sent, secret=SK):
        path, _, query = sent["url"].partition("?")
        headers = {k: v for k, v in sent["headers"].items() if k != "authorization"}
        when = datetime.strptime(sent["headers"]["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        ref_auth, _, _ = _ref_authorization(
            method=sent["method"], raw_path=path, query=query, headers=headers,
            payload_hash=sent["headers"]["x-amz-content-sha256"], access_key=AK,
            secret=secret, region=self.tgt["region"], when=when)
        self.assertEqual(sent["headers"]["authorization"], ref_auth,
                         f"签名没有覆盖线上请求：{sent['url']}")

    def test_head_bucket_signature_covers_the_wire_request(self):
        out, sent = self._run(lambda: C.head_bucket(self.tgt),
                              lambda **kw: _Resp(200, headers={"x-amz-request-id": "RID1"}))
        self.assertEqual(out["addressing"], "path-style")
        self.assertEqual(out["request_id"], "RID1")
        self.assertEqual(sent["url"], "/offsite", "path-style 且无键时桶名就是路径")
        self._assert_signed_over_wire(sent)

    def test_list_keys_query_is_the_signed_one(self):
        """带 `/` 的对象前缀：线上查询串是 panel%2F，签名里也必须是 panel%2F。"""
        xml = (b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
               b'<Contents><Key>panel/db/bk1-a.gz</Key><Size>5</Size>'
               b'<LastModified>2026-10-16T03:41:00.000Z</LastModified></Contents>'
               b'<IsTruncated>false</IsTruncated></ListBucketResult>')
        seen = {}

        def ok(**kw):
            seen["url"] = kw["url"]
            return _Resp(200, body=xml, headers={"x-amz-request-id": "RID2"})

        rows, truncated = self._run(lambda: C.list_keys(self.tgt, self.tgt["prefix"]), ok)[0]
        self.assertEqual([r["key"] for r in rows], ["panel/db/bk1-a.gz"])
        self.assertFalse(truncated)
        self.assertIn("prefix=panel%2Fdb%2F", seen["url"], "线上查询串只编码一次")
        self._assert_signed_over_wire(_SENT[-1])

    def test_list_keys_paginates_and_flags_a_truncated_page(self):
        page1 = (b'<ListBucketResult><Contents><Key>a.gz</Key><Size>1</Size></Contents>'
                 b'<IsTruncated>true</IsTruncated><NextContinuationToken>tok+/=</NextContinuationToken>'
                 b'</ListBucketResult>')
        page2 = b'<ListBucketResult><Contents><Key>b.gz</Key><Size>2</Size></Contents></ListBucketResult>'
        calls = []

        def ok(**kw):
            calls.append(kw["url"])
            return _Resp(200, body=page1 if len(calls) == 1 else page2)

        rows, truncated = self._run(lambda: C.list_keys(self.tgt, ""), ok)[0]
        self.assertEqual([r["key"] for r in rows], ["a.gz", "b.gz"])
        self.assertFalse(truncated)
        self.assertIn("continuation-token=tok%2B%2F%3D", calls[1], "翻页 token 里的 +/= 必须编码")
        self._assert_signed_over_wire(_SENT[-1])

    def test_virtual_host_addressing_moves_the_bucket_into_host(self):
        self.tgt["path_style"] = False
        res, sent = self._run(lambda: C.head_bucket(self.tgt),
                              lambda **kw: _Resp(200, headers={"x-amz-request-id": "RID3"}))
        self.assertEqual(res["addressing"], "virtual-host")
        self.assertEqual(sent["headers"]["host"], "offsite.minio.example.com")
        self.assertEqual(sent["url"], "/", "虚拟主机风格下桶名不在 path 里")
        self._assert_signed_over_wire(sent)

    def test_a_wrong_secret_produces_a_different_signature(self):
        """负向对照：签名确实覆盖凭据本身，这条测试不是恒等式。"""
        self.tgt["secret_key"] = "anothersecretkey1"
        _, sent = self._run(lambda: C.head_bucket(self.tgt),
                            lambda **kw: _Resp(200, headers={"x-amz-request-id": "R"}))
        self._assert_signed_over_wire(sent, secret="anothersecretkey1")
        with self.assertRaises(AssertionError):
            self._assert_signed_over_wire(sent)

    def test_fail_detail_does_not_echo_the_error_body(self):
        """对端报错正文里带 AccessKeyId 与 StringToSign 片段，只取 Code/Message。"""
        body = (b"<Error><Code>SignatureDoesNotMatch</Code><Message>"
                + SK.encode() + b" " + AK.encode() + b"</Message></Error>")
        msg = C._fail_detail(403, {}, body, {"region": "us-east-1"})
        self.assertNotIn(SK, msg)
        self.assertNotIn(AK, msg)
        self.assertIn("凭据被拒", msg)
        msg2 = C._fail_detail(400, {"x-amz-bucket-region": "eu-west-1"}, body, {"region": "us-east-1"})
        self.assertIn("eu-west-1", msg2)


class MetadataRefusal(unittest.TestCase):
    """面板以 root 出网，端点被人塞一个元数据地址就等于借它去取实例角色凭据。"""

    def test_literal_metadata_address_is_refused_when_building_the_target(self):
        for ep in ("http://169.254.169.254", "http://169.254.169.254:80/",
                   "http://metadata.google.internal", "http://instance-data"):
            with self.assertRaises(RuntimeError, msg=ep) as ctx:
                C._check_endpoint(ep)
            self.assertIn("元数据", str(ctx.exception))

    def test_hostname_that_now_resolves_to_link_local_is_refused_before_connect(self):
        """建目标时是好域名、出网时解析到链路本地段：`_request` 这一层必须再判一次。"""
        t = {"endpoint": "https://store.example.com", "bucket": "b", "region": "r",
             "prefix": "", "path_style": True, "access_key": AK, "secret_key": SK, "name": "t"}
        with mock.patch.object(C.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("169.254.169.254", 80))]), \
             mock.patch.object(C, "HTTPSConnection") as conn:
            with self.assertRaises(RuntimeError) as ctx:
                C.head_bucket(t)
        self.assertIn("链路本地", str(ctx.exception))
        conn.assert_not_called()

    def test_dns_failure_is_left_to_the_real_connect(self):
        """解析不动不在这里下结论：否则离线机器连「测试连通性」都点不动，报的还是假错。"""
        with mock.patch.object(C.socket, "getaddrinfo", side_effect=OSError("no dns")):
            C._assert_not_metadata("minio.internal.example")

    def test_private_address_is_still_allowed(self):
        """拒绝的只是元数据段；内网自建 MinIO 是这一层的主要用法之一。"""
        with mock.patch.object(C.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("10.0.0.9", 9000))]):
            C._assert_not_metadata("minio.lan")

    def test_link_local_literal(self):
        with self.assertRaises(RuntimeError):
            C._assert_not_metadata("169.254.10.10")


class EndpointValidation(unittest.TestCase):
    def test_scheme_missing_defaults_to_https(self):
        self.assertEqual(C._check_endpoint("minio.example.com:9000"),
                         {"scheme": "https", "host": "minio.example.com", "port": 9000})

    def test_default_port_is_not_written_into_the_stored_endpoint(self):
        tid = _mktarget(endpoint="https://minio.example.com")
        self.assertEqual(dbm.query_one("SELECT endpoint FROM cloud_targets WHERE id=?",
                                       (tid,))["endpoint"], "https://minio.example.com")

    def test_non_default_port_is_kept(self):
        tid = _mktarget(endpoint="minio.example.com:9000")
        self.assertEqual(dbm.query_one("SELECT endpoint FROM cloud_targets WHERE id=?",
                                       (tid,))["endpoint"], "https://minio.example.com:9000")

    def test_file_scheme_and_credentials_in_url_are_refused(self):
        for ep in ("file:///root/backups/panel", "https://u:p@minio.example.com",
                   "https://minio.example.com?x=1", "https://minio.example.com#f",
                   "https://minio.example.com:0", "https://minio.example.com:99999",
                   "not a url", "", "ftp://minio.example.com"):
            with self.assertRaises(RuntimeError, msg=ep):
                C._check_endpoint(ep)

    def test_username_password_hint_names_the_right_fields(self):
        with self.assertRaises(RuntimeError) as ctx:
            C._check_endpoint("https://root:pw@minio.example.com")
        self.assertIn("Access Key 与 Secret Key 有各自的栏位", str(ctx.exception))


def _mktarget(**kw) -> int:
    body = {"name": _uid("tg"), "endpoint": "https://minio.example.com", "bucket": "offsite",
            "region": "us-east-1", "access_key": AK, "secret_key": SK}
    body.update(kw)
    return int(C.create_target(body)["id"])


class TargetClean(unittest.TestCase):
    """`_clean` 的「给了才换」与字段白名单。"""

    def tearDown(self):
        dbm.execute("DELETE FROM cloud_targets")

    def test_partial_empty_secret_means_unchanged(self):
        tid = _mktarget()
        before = C.get_secret(tid)["secret_key"]
        out = C.update_target(tid, {"secret_key": ""})
        self.assertNotIn("secret_key", out["changed"])
        self.assertEqual(C.get_secret(tid)["secret_key"], before,
                         "口令留空等于没改：浏览器那个框永远是空的，清空它会把唯一一份凭据弄没")

    def test_partial_absent_key_means_unchanged_and_explicit_replaces(self):
        tid = _mktarget()
        out = C.update_target(tid, {"region": "ap-northeast-1"})
        self.assertEqual(out["changed"], ["region"])
        row = dbm.query_one("SELECT * FROM cloud_targets WHERE id=?", (tid,))
        self.assertEqual(row["bucket"], "offsite")
        self.assertEqual(row["region"], "ap-northeast-1")

    def test_partial_with_no_fields_changes_nothing(self):
        tid = _mktarget()
        self.assertEqual(C.update_target(tid, {})["changed"], [])
        with self.assertRaises(RuntimeError):
            C.update_target(999999, {"region": "us-west-2"})

    def test_create_refuses_a_body_missing_any_required_field(self):
        """少任何一栏都只是一句人话的错误，不许变成 500，也不许建出一半的目标。"""
        for k in ("name", "endpoint", "bucket", "access_key", "secret_key"):
            body = {"name": _uid("x"), "endpoint": "https://h.example", "bucket": "bkt",
                    "access_key": AK, "secret_key": SK}
            body.pop(k)
            with self.assertRaises(RuntimeError, msg=k) as ctx:
                C.create_target(body)
            self.assertTrue(str(ctx.exception), k)
        self.assertEqual(C.list_targets(), [], "校验失败的那几次一个都不许落库")

    def test_missing_secret_is_named_by_create(self):
        """缺 Secret 是唯一一个「字段本身合法但没给」的形态，必须点名到栏位。"""
        with self.assertRaises(RuntimeError) as ctx:
            C.create_target({"name": _uid("x"), "endpoint": "https://h.example",
                             "bucket": "bkt", "access_key": AK})
        self.assertIn("新建异地目标需要 secret_key", str(ctx.exception))

    def test_rejects_bad_names_buckets_regions_prefixes_and_secrets(self):
        good = {"name": _uid("n"), "endpoint": "https://h.example", "bucket": "bkt",
                "access_key": AK, "secret_key": SK}
        for bad in ({"name": "UPPER"}, {"name": "1abc"}, {"bucket": "Bad_Bucket"},
                    {"bucket": "a..b"}, {"bucket": "-lead"}, {"region": "us east 1"},
                    {"access_key": "ab"}, {"access_key": "has space"},
                    {"secret_key": "short"}, {"secret_key": "abc\x00defgh"},
                    {"prefix": "a/../b"}, {"prefix": "a?b"}):
            with self.assertRaises(RuntimeError, msg=str(bad)):
                C._clean({**good, **bad})

    def test_prefix_normalisation(self):
        good = {"name": _uid("n"), "endpoint": "https://h.example", "bucket": "bkt",
                "access_key": AK, "secret_key": SK}
        self.assertEqual(C._clean({**good, "prefix": "/panel/db"})["prefix"], "panel/db/")
        self.assertEqual(C._clean({**good, "prefix": ""})["prefix"], "")
        self.assertEqual(C._clean({**good, "prefix": "panel/db/"})["prefix"], "panel/db/")

    def test_flags_accept_the_usual_truthy_forms(self):
        good = {"name": _uid("n"), "endpoint": "https://h.example", "bucket": "bkt",
                "access_key": AK, "secret_key": SK}
        for v in (True, 1, "1", "true", "yes", "on"):
            self.assertEqual(C._clean({**good, "enabled": v})["enabled"], 1, repr(v))
        for v in (False, 0, "0", "no", "", None):
            self.assertEqual(C._clean({**good, "enabled": v})["enabled"], 0, repr(v))

    def test_absent_flags_are_not_written_at_all(self):
        """没给的那一栏交给库的默认值（enabled=1 / path_style=0）。

        这是异地备份最容易踩空的一处：`choyeonctl backup add-target` 不带 --enable 时
        根本不会发 enabled，若 `_clean` 替它补 0，建出来的目标一出生就是停用，
        push_pending 永远回「已停用」，异地那份副本一次都没写过，而界面上只写着「停用」。
        """
        good = {"name": _uid("n"), "endpoint": "https://h.example", "bucket": "bkt",
                "access_key": AK, "secret_key": SK}
        self.assertNotIn("enabled", C._clean(good))
        self.assertNotIn("path_style", C._clean(good))
        tid = C.create_target(good)["id"]
        row = dbm.query_one("SELECT enabled,path_style FROM cloud_targets WHERE id=?", (tid,))
        self.assertEqual((row["enabled"], row["path_style"]), (1, 0))
        # 建完就能真的走推送这条路：判词必须是"没有待推送"，而不是"已停用"。
        # 目录换成空的临时目录，这条断言才不会依赖本机残留的产物。
        d = tempfile.mkdtemp(prefix="cp_b43_flags_")
        try:
            with mock.patch.object(R, "BACKUP_DIR", d), \
                 mock.patch.object(config, "BACKUP_DIR", d), \
                 mock.patch.object(C, "put_object",
                                  side_effect=AssertionError("没有产物就不该出网")):
                res = C.push_pending({"id": 0, "cloud_target_id": tid, "keep": 7})
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertIn("没有待推送", res["skipped"])


class TargetTable(unittest.TestCase):
    def tearDown(self):
        dbm.execute("DELETE FROM backup_pushes")
        dbm.execute("DELETE FROM cloud_targets")
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE cloud_target_id IS NOT NULL")

    def test_list_targets_never_carries_a_secret_or_raw_access_key(self):
        _mktarget()
        row = C.list_targets()[0]
        self.assertNotIn("secret_key", row)
        self.assertNotIn("access_key", row)
        self.assertTrue(row["has_secret"])
        dumped = _dumps(row)
        self.assertNotIn(AK, dumped)
        self.assertNotIn(SK, dumped)
        self.assertEqual(row["access_key_masked"], "AKIA…MPLE")

    def test_mask_short_and_absent(self):
        self.assertEqual(C._mask_akid("abcd"), "****")
        self.assertEqual(C._mask_akid(""), "")
        self.assertEqual(C._mask_akid("abcdefghi"), "abcd…fghi")

    def test_last_error_is_redacted_on_the_way_out(self):
        tid = _mktarget()
        sig = hashlib.sha256(b"z").hexdigest()
        dbm.execute("UPDATE cloud_targets SET last_error=? WHERE id=?",
                    (f"Signature={sig} {AK} secret={SK}", tid))
        row = [r for r in C.list_targets() if r["id"] == tid][0]
        self.assertNotIn(SK, row["last_error"])
        self.assertNotIn(AK, row["last_error"])
        self.assertNotIn(sig, row["last_error"])

    def test_duplicate_name_is_named_not_dumped(self):
        name = _uid("dup")
        _mktarget(name=name)
        with self.assertRaises(RuntimeError) as ctx:
            _mktarget(name=name)
        self.assertIn(name, str(ctx.exception))
        self.assertNotIn("UNIQUE constraint", str(ctx.exception))

    def test_get_secret_requires_a_saved_secret(self):
        tid = _mktarget()
        # 建目标这一层不允许空 Secret（`_secret_ok`），"这一行没有凭据"只能来自
        # 早期写入/手工改库，所以这里直接落一个空串再取。
        dbm.execute("UPDATE cloud_targets SET secret_key='' WHERE id=?", (tid,))
        with self.assertRaises(RuntimeError) as ctx:
            C.get_secret(tid)
        self.assertIn("没有保存 Secret Key", str(ctx.exception))
        with self.assertRaises(RuntimeError):
            C.get_secret(999999)

    def test_delete_refuses_while_a_job_is_linked(self):
        tid = _mktarget()
        bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES('app','demo-app','manual',3,30,7,1,?)", (tid,))
        with self.assertRaises(RuntimeError) as ctx:
            C.delete_target(tid)
        self.assertIn(f"#{bid}", str(ctx.exception))
        self.assertIn("demo-app", str(ctx.exception))
        self.assertTrue(dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (tid,)))
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE id=?", (bid,))
        self.assertEqual(C.delete_target(tid)["id"], tid)

    def test_delete_drops_push_records_so_a_new_target_inherits_nothing(self):
        tid = _mktarget()
        other = _mktarget()
        dbm.execute("INSERT INTO backup_pushes(target_id,backup_id,name,key,bytes) VALUES(?,?,?,?,?)",
                    (tid, 1, "bk1-a.gz", "bk1-a.gz", 10))
        dbm.execute("INSERT INTO backup_pushes(target_id,backup_id,name,key,bytes) VALUES(?,?,?,?,?)",
                    (other, 1, "bk1-b.gz", "bk1-b.gz", 10))
        self.assertEqual(C.delete_target(tid)["records_dropped"], 1)
        self.assertEqual(len(dbm.query("SELECT id FROM backup_pushes WHERE target_id=?", (other,))), 1,
                         "只清理被删目标的记录，别把别人的推送一起带走")
        with self.assertRaises(RuntimeError):
            C.delete_target(987654)

    def test_list_targets_aggregates_links_and_counts(self):
        tid = _mktarget()
        bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES('sqlite','9','manual',3,30,7,1,?)", (tid,))
        dbm.execute("INSERT INTO backup_pushes(target_id,backup_id,name,key,bytes) VALUES(?,?,?,?,?)",
                    (tid, bid, "bk9-a.gz", "bk9-a.gz", 3))
        row = [r for r in C.list_targets() if r["id"] == tid][0]
        self.assertEqual(row["pushed_count"], 1)
        self.assertEqual([x["id"] for x in row["linked"]], [bid])
        self.assertEqual(row["linked"][0]["kind"], "sqlite")


class LinkValidation(unittest.TestCase):
    """关联写的时候就要指对东西：链到不存在的目标，每次备份都会跑一次注定失败的推送。"""

    def tearDown(self):
        dbm.execute("DELETE FROM cloud_targets")
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE cloud_target_id IS NOT NULL")

    def test_unset_keeps_and_none_or_zero_unlinks(self):
        self.assertEqual(B._cloud_target_id(B._UNSET, keep=7), 7)
        self.assertIsNone(B._cloud_target_id(None, keep=7))
        self.assertIsNone(B._cloud_target_id(0, keep=7))

    def test_bool_and_negative_and_str_are_rejected(self):
        for bad in (True, False, -1, "3", 3.0, []):
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                B._cloud_target_id(bad, keep=None)

    def test_missing_target_is_named(self):
        with self.assertRaises(RuntimeError) as ctx:
            B._cloud_target_id(999999, keep=None)
        self.assertIn("999999", str(ctx.exception))

    def test_existing_target_id_is_stored(self):
        tid = _mktarget()
        self.assertEqual(B._cloud_target_id(tid, keep=None), tid)


class PushSemantics(unittest.TestCase):
    """推送的判重、轮替与「没配异地不是故障」。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b43_files_")
        self.tid = _mktarget()
        self.bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES('app','demo-app','manual',3,30,7,1,?)", (self.tid,))
        self.bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,))

    def tearDown(self):
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE id=?", (self.bid,))
        dbm.execute("DELETE FROM backup_pushes WHERE target_id=?", (self.tid,))
        dbm.execute("DELETE FROM cloud_targets WHERE id=?", (self.tid,))
        shutil.rmtree(self.dir, ignore_errors=True)

    def _file(self, name: str, data: bytes = b"x" * 10) -> str:
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    _FORBID = object()
    OK = "ok"

    @contextlib.contextmanager
    def _transport(self, rows=None, put=_FORBID, delete=None):
        """把出网这一层整体换掉：产物在临时目录里，签名与连接都不参与。

        默认是 forbidden：这一组测试里"没配异地/停用了/没有待推"都必须在真正发请求
        之前停下，一旦 put_object 被调到就是出网了，AssertionError 比一句"没断言"清楚。
        要验证"推成功了"的那些用例显式传 `put=PushSemantics.OK` 换成记账版。
        """
        def forbidden(t, key, path, timeout=None):
            raise AssertionError(f"这条路径不该出网：{key}")

        def simple(t, key, path, timeout=None):
            return {"key": key, "bytes": os.path.getsize(path), "request_id": "R"}

        chosen = forbidden if put is self._FORBID else (simple if put == self.OK else put)
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(R, "BACKUP_DIR", self.dir))
            st.enter_context(mock.patch.object(config, "BACKUP_DIR", self.dir))
            st.enter_context(mock.patch.object(C, "put_object", chosen))
            st.enter_context(mock.patch.object(C, "list_keys", lambda t, p: (rows or [], False)))
            st.enter_context(mock.patch.object(C, "delete_object", delete or (lambda t, k: None)))
            yield

    def test_no_target_is_skipped_not_failed(self):
        """runner 每条任务都会走这里，「这个任务本来就没有异地」不是故障。"""
        res = C.push_pending({"id": 12345, "cloud_target_id": None, "keep": 7})
        self.assertTrue(res["ok"])
        self.assertIn("未关联", res["skipped"])

    def test_disabled_target_is_skipped_and_nothing_leaves_the_box(self):
        """界面那把「停用」开关必须真能停住事，否则它只是装饰。"""
        dbm.execute("UPDATE cloud_targets SET enabled=0 WHERE id=?", (self.tid,))
        try:
            with self._transport(), mock.patch.object(
                    R, "auto_files", side_effect=AssertionError("停用了就不该列目录")):
                res = C.push_pending(self.bk)
            self.assertIn("已停用", res["skipped"])
        finally:
            dbm.execute("UPDATE cloud_targets SET enabled=1 WHERE id=?", (self.tid,))

    def test_vanished_target_raises(self):
        bk = {**dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,)),
              "cloud_target_id": 999999}
        with self.assertRaises(RuntimeError) as ctx:
            C.push_pending(bk)
        self.assertIn("已不存在", str(ctx.exception))

    def test_nothing_pending_is_skipped(self):
        with self._transport():
            res = C.push_pending(self.bk)
        self.assertIn("没有待推送", res["skipped"])

    def test_push_writes_key_with_prefix_and_records_once(self):
        name = f"bk{self.bid}-2026-10-16-03-41-00-app.tar.gz"
        self._file(name)
        dbm.execute("UPDATE cloud_targets SET prefix='panel/db' WHERE id=?", (self.tid,))
        seen = []

        def fake_put(t, key, path, timeout=None):
            seen.append((key, os.path.basename(path), t["secret_key"]))
            return {"key": key, "bytes": 10, "request_id": "R"}

        rows = [{"key": f"panel/db/{name}", "size": 10, "modified": ""}]
        with self._transport(rows=rows, put=fake_put):
            res = C.push_pending(dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,)))
        self.assertEqual(seen[0][0], f"panel/db/{name}", "远端键必须是前缀 + 产物名")
        self.assertEqual(seen[0][2], SK, "签名这一条路必须拿到完整凭据")
        self.assertEqual(len(res["pushed"]), 1)
        self.assertEqual(res["remote_count"], 1)
        self.assertEqual(dbm.query_one("SELECT bytes FROM backup_pushes WHERE target_id=? AND name=?",
                                       (self.tid, name))["bytes"], 10)

    def test_repeat_push_is_idempotent(self):
        """重复点「立即推送」不该在同一份产物上记两行，否则「远端有 N 份」越点越多。"""
        name = f"bk{self.bid}-2026-10-16-03-41-01-app.tar.gz"
        self._file(name)
        with self._transport(put=self.OK):
            C.push_file(self.tid, self.bid, name)
            C.push_file(self.tid, self.bid, name)
        self.assertEqual(len(dbm.query("SELECT id FROM backup_pushes WHERE target_id=?", (self.tid,))), 1)
        self.assertTrue(C.already_pushed(self.tid, name))
        with self._transport():
            again = C.push_pending(dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,)))
        self.assertIn("没有待推送", again["skipped"])

    def test_local_file_missing_is_refused_before_connecting(self):
        with self._transport():
            with self.assertRaises(RuntimeError) as ctx:
                C.push_file(self.tid, self.bid, f"bk{self.bid}-2020-01-01-00-00-00-x.gz")
        self.assertIn("不存在", str(ctx.exception))

    def test_push_sets_last_push_at_and_clears_error(self):
        name = f"bk{self.bid}-2026-10-16-03-41-02-app.tar.gz"
        self._file(name)
        dbm.execute("UPDATE cloud_targets SET last_error='上次失败' WHERE id=?", (self.tid,))
        with self._transport(put=self.OK):
            C.push_file(self.tid, self.bid, name)
        row = dbm.query_one("SELECT last_error,last_push_at FROM cloud_targets WHERE id=?", (self.tid,))
        self.assertEqual(row["last_error"], "")
        self.assertTrue(row["last_push_at"])


class RemotePrune(unittest.TestCase):
    """远端轮替只认 runner 那一行规则派生出的形状。"""

    def setUp(self):
        self.tid = _mktarget()
        self.bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES('app','demo-app','manual',3,30,2,1,?)", (self.tid,))
        self.bk = dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,))

    def tearDown(self):
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE id=?", (self.bid,))
        dbm.execute("DELETE FROM backup_pushes WHERE target_id=?", (self.tid,))
        dbm.execute("DELETE FROM cloud_targets WHERE id=?", (self.tid,))

    def test_uses_the_same_pattern_as_the_runner(self):
        self.assertEqual(C._auto_re(self.bid).pattern, R.auto_pattern(self.bid),
                         "两处各写一份正则的话，带同秒序号的那一份会在异地认不出来，远端越留越多")

    def test_only_auto_shaped_keys_are_rotated(self):
        p = "panel/db/"
        keys = [
            f"{p}bk{self.bid}-2026-01-01-00-00-00-app.tar.gz",
            f"{p}bk{self.bid}-2026-01-02-00-00-00-app.tar.gz",
            f"{p}bk{self.bid}-2026-01-03-00-41-00-2-app.tar.gz",
            f"{p}bk{self.bid}-2026-01-04-00-00-00-app.tar.gz",
            f"{p}someone-else.dump",
        ]
        deleted = []
        with mock.patch.object(C, "get_secret", return_value={"prefix": p, "id": self.tid}), \
             mock.patch.object(C, "list_keys",
                               lambda t, pr: ([{"key": k, "size": 1, "modified": ""} for k in keys], False)), \
             mock.patch.object(C, "delete_object", lambda t, k: deleted.append(k)):
            res = C.prune_remote(self.tid, self.bk)
        self.assertEqual(deleted, keys[:2], "按时间从旧到新删到 keep 名额，人工上传的那份不许碰")
        self.assertEqual(sorted(res["deleted"]), [keys[0][len(p):], keys[1][len(p):]])
        self.assertEqual(res["remote_count"], 2)
        self.assertFalse(res["truncated"])

    def test_same_second_suffix_sorts_after_the_plain_one(self):
        a = f"bk{self.bid}-2026-01-02-03-41-00-app.tar.gz"
        b = f"bk{self.bid}-2026-01-02-03-41-00-2-app.tar.gz"
        deleted = []
        with mock.patch.object(C, "get_secret", return_value={"prefix": "", "id": self.tid}), \
             mock.patch.object(C, "list_keys",
                               lambda t, pr: ([{"key": a, "size": 1, "modified": ""},
                                               {"key": b, "size": 1, "modified": ""}], False)), \
             mock.patch.object(C, "delete_object", lambda t, k: deleted.append(k)):
            C.prune_remote(self.tid, dict(self.bk, keep=1))
        self.assertEqual(deleted, [a], "同秒带序号的那一份是后写的，先删的一定是它前面那一份")

    def test_pruned_name_is_dropped_from_the_records(self):
        name = f"bk{self.bid}-2026-01-01-00-00-00-app.tar.gz"
        dbm.execute("INSERT INTO backup_pushes(target_id,backup_id,name,key,bytes) VALUES(?,?,?,?,?)",
                    (self.tid, self.bid, name, name, 1))
        with mock.patch.object(C, "get_secret", return_value={"prefix": "", "id": self.tid}), \
             mock.patch.object(C, "list_keys",
                               lambda t, pr: ([{"key": name, "size": 1, "modified": ""},
                                               {"key": f"bk{self.bid}-2026-01-02-00-00-00-a.tar.gz",
                                                "size": 1, "modified": ""}], False)), \
             mock.patch.object(C, "delete_object", lambda t, k: None):
            C.prune_remote(self.tid, dict(self.bk, keep=1))
        self.assertIsNone(dbm.query_one("SELECT id FROM backup_pushes WHERE name=?", (name,)),
                         "远端删掉了、记录还留着，push_pending 就永远不会再推这一份")

    def test_truncated_listing_is_reported_not_swallowed(self):
        """没读完就宣称 prune 成功，等于把一个可能删错对象的结论当绿灯。"""
        with mock.patch.object(C, "get_secret", return_value={"prefix": "", "id": self.tid}), \
             mock.patch.object(C, "list_keys",
                               lambda t, pr: ([{"key": f"bk{self.bid}-2026-01-01-00-00-00-a.gz",
                                                "size": 1, "modified": ""}], True)), \
             mock.patch.object(C, "delete_object", lambda t, k: None):
            res = C.prune_remote(self.tid, dict(self.bk, keep=7))
        self.assertTrue(res["truncated"])


class PushFailureNeverLeaks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cp_b43_fail_")
        self.tid = _mktarget()
        self.bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES('app','demo-app','manual',3,30,7,1,?)", (self.tid,))

    def tearDown(self):
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE id=?", (self.bid,))
        dbm.execute("DELETE FROM backup_pushes WHERE target_id=?", (self.tid,))
        dbm.execute("DELETE FROM cloud_targets WHERE id=?", (self.tid,))
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_last_error_and_raised_message_are_redacted(self):
        name = f"bk{self.bid}-2026-05-05-00-00-00-app.tar.gz"
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(b"y" * 4)
        sig = hashlib.sha256(b"x").hexdigest()

        def boom(t, key, path, timeout=None):
            raise RuntimeError(f"Signature={sig} Credential={AK}/20261016/us-east-1/s3/aws4_request "
                               f"secret={SK}")

        with mock.patch.object(R, "BACKUP_DIR", self.dir), \
             mock.patch.object(config, "BACKUP_DIR", self.dir), \
             mock.patch.object(C, "put_object", boom):
            with self.assertRaises(RuntimeError) as ctx:
                C.push_pending(dbm.query_one("SELECT * FROM backups WHERE id=?", (self.bid,)))
        msg = str(ctx.exception)
        stored = dbm.query_one("SELECT last_error FROM cloud_targets WHERE id=?",
                              (self.tid,))["last_error"]
        for text in (msg, stored):
            self.assertNotIn(SK, text)
            self.assertNotIn(AK, text)
            self.assertNotIn(sig, text)
        self.assertIn("aws4_request", stored, "排障要看的是签的哪个区，那一段不是凭据")

    def test_audit_detail_is_redacted_the_way_the_runner_writes_it(self):
        """审计明细走的是 `_push_offsite` 那句 redact_creds，不是"裸口令也会被认出来"。

        redact_creds 按键名与形态遮（`secret=`、`Signature=<hex>`、`AKIA…`），
        它认不出一段没有任何标记的随机串——所以这条测试模拟的是真实失败正文：
        对端把 AccessKeyId、Signature 与 StringToSign 片段一起回回来。
        """
        sig = hashlib.sha256(b"x").hexdigest()
        detail = f"{self.bid}(失败: {redact_creds('Signature=' + sig + ' ' + AK + ' secret=' + SK)[:120]})"
        dbm.audit("system", "backup:push-fail", detail)
        row = dbm.query_one("SELECT detail FROM audit WHERE action='backup:push-fail' ORDER BY id DESC")
        self.assertNotIn(SK, row["detail"])
        self.assertNotIn(AK, row["detail"])
        self.assertNotIn(sig, row["detail"])
        self.assertIn(str(self.bid), row["detail"], "任务号要留着，否则审计里看不出是哪条任务失败的")
        self.assertEqual(redact_creds(""), "")

    def test_probe_reports_counts_and_clears_the_error(self):
        dbm.execute("UPDATE cloud_targets SET last_error='上次失败' WHERE id=?", (self.tid,))
        with mock.patch.object(C, "head_bucket",
                               return_value={"ok": True, "addressing": "path-style",
                                             "request_id": "R"}), \
             mock.patch.object(C, "list_keys",
                               return_value=([{"key": "a/bk1-x.gz", "size": 5, "modified": ""},
                                              {"key": "a/bk1-y.gz", "size": 7, "modified": ""}], True)):
            res = C.probe(self.tid)
        self.assertEqual(res["objects_under_prefix"], 2)
        self.assertEqual(res["bytes_under_prefix"], 12)
        self.assertTrue(res["list_truncated"])
        self.assertEqual(res["addressing"], "path-style")
        self.assertEqual(res["request_id"], "R")
        self.assertEqual(res["prefix"], "")
        self.assertEqual(dbm.query_one("SELECT last_error FROM cloud_targets WHERE id=?",
                                       (self.tid,))["last_error"], "")

    def test_probe_result_carries_no_credential(self):
        with mock.patch.object(C, "head_bucket",
                               return_value={"ok": True, "addressing": "path-style",
                                             "request_id": ""}), \
             mock.patch.object(C, "list_keys", return_value=([], False)):
            dumped = _dumps(C.probe(self.tid))
        self.assertNotIn(SK, dumped)
        self.assertNotIn(AK, dumped)


class PushNowEntrypoint(unittest.TestCase):
    """界面上那一下点击的口径：停用要拒绝、没关联要说清、参数缺省用任务自己那个。"""

    def setUp(self):
        self.tid = _mktarget()
        self.off = _mktarget()
        self.bid = dbm.execute(
            "INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,cloud_target_id) "
            "VALUES('app','demo-app','manual',3,30,7,1,?)", (self.tid,))

    def tearDown(self):
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE id=?", (self.bid,))
        for tid in (self.tid, self.off):
            dbm.execute("DELETE FROM backup_pushes WHERE target_id=?", (tid,))
            dbm.execute("DELETE FROM cloud_targets WHERE id=?", (tid,))

    def test_unknown_job(self):
        with self.assertRaises(RuntimeError) as ctx:
            C.push_now(987654)
        self.assertIn("不存在", str(ctx.exception))

    def test_unlinked_job_says_so(self):
        bid = dbm.execute("INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) "
                         "VALUES('app','x','manual',3,30,7,1)")
        try:
            with self.assertRaises(RuntimeError) as ctx:
                C.push_now(bid)
            self.assertIn("没有关联异地目标", str(ctx.exception))
        finally:
            dbm.execute("DELETE FROM backups WHERE id=?", (bid,))

    def test_disabled_target_refuses_instead_of_doing_nothing(self):
        """这里是用户明确点下去的一下，静默「没推」比一句「要先启用」难查得多。"""
        dbm.execute("UPDATE cloud_targets SET enabled=0 WHERE id=?", (self.tid,))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                C.push_now(self.bid)
            self.assertIn("先启用再推送", str(ctx.exception))
        finally:
            dbm.execute("UPDATE cloud_targets SET enabled=1 WHERE id=?", (self.tid,))

    def test_missing_target_id_is_named(self):
        with self.assertRaises(RuntimeError) as ctx:
            C.push_now(self.bid, 999999)
        self.assertIn("999999", str(ctx.exception))

    def test_zero_target_id_falls_back_to_the_row(self):
        """路由里 `int(tid) if tid else 0` 传 0 等于「没给」，必须用任务自己关联的那个。"""
        seen = {}

        def fake(bk):
            seen["tid"] = bk["cloud_target_id"]
            return {"ok": True, "skipped": "没有待推送的产物"}

        with mock.patch.object(C, "push_pending", fake):
            C.push_now(self.bid, 0)
        self.assertEqual(seen["tid"], self.tid)

    def test_explicit_target_id_is_used(self):
        seen = {}

        def fake(bk):
            seen["tid"] = bk["cloud_target_id"]
            return {"ok": True}

        with mock.patch.object(C, "push_pending", fake):
            C.push_now(self.bid, self.off)
        self.assertEqual(seen["tid"], self.off)

    def test_push_now_goes_through_the_pending_path(self):
        ret = {"ok": True, "pushed": [{"key": "k"}], "pruned": [], "remote_count": 1}
        with mock.patch.object(C, "push_pending", return_value=ret) as p:
            out = C.push_now(self.bid)
        self.assertEqual(out["pushed"], [{"key": "k"}])
        self.assertEqual(p.call_args[0][0]["cloud_target_id"], self.tid)


class RunnerNeverFailsAJob(unittest.TestCase):
    """本机归档已经落盘了才推异地：异地没上去是「少了一份副本」，不是「备份没做」。"""

    def setUp(self):
        self.tid = _mktarget()

    def tearDown(self):
        dbm.execute("DELETE FROM cloud_targets WHERE id=?", (self.tid,))

    def test_exception_is_swallowed_and_reported_in_two_places(self):
        with mock.patch.object(C, "push_pending", side_effect=RuntimeError("桶被人删了")), \
             mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err:
            R._push_offsite({"id": 4242, "cloud_target_id": self.tid})
        self.assertIn("offsite:", err.getvalue())
        self.assertIn("桶被人删了", err.getvalue())
        row = dbm.query_one("SELECT detail FROM audit WHERE action='backup:push-fail' ORDER BY id DESC")
        self.assertIsNotNone(row, "journal 会被轮替带走，stderr 与审计两处都留痕才算真的报警")
        self.assertIn("4242", row["detail"])

    def test_unlinked_job_does_not_even_call_the_layer(self):
        with mock.patch.object(C, "push_pending") as p:
            R._push_offsite({"id": 1, "cloud_target_id": None})
            R._push_offsite({"id": 1})
        p.assert_not_called()

    def test_success_is_audited(self):
        with mock.patch.object(C, "push_pending", return_value={
                "ok": True, "pushed": [{"key": "k1"}, {"key": "k2"}], "pruned": ["k0"],
                "remote_count": 2}), \
             mock.patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            R._push_offsite({"id": 4243, "cloud_target_id": self.tid})
        row = dbm.query_one("SELECT detail FROM audit WHERE action='backup:push' ORDER BY id DESC")
        self.assertIn("4243", row["detail"])
        self.assertIn("已推送 2 份", out.getvalue())
        self.assertIn("删掉远端最旧的 1 份", out.getvalue())

    def test_skipped_is_printed_but_not_audited_as_failure(self):
        with mock.patch.object(C, "push_pending",
                              return_value={"ok": True, "skipped": "未关联异地目标"}), \
             mock.patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            R._push_offsite({"id": 4244, "cloud_target_id": self.tid})
        self.assertIn("未关联异地目标", out.getvalue())
        self.assertIsNone(dbm.query_one(
            "SELECT id FROM audit WHERE action='backup:push-fail' AND detail LIKE '%4244%'"))


class CliContract(unittest.TestCase):
    """CLI 是第三个入口：它必须和服务层同一口径，尤其是不许把口令送上 argv。"""

    def setUp(self):
        self.parser = build_parser()

    def tearDown(self):
        dbm.execute("DELETE FROM cloud_targets")
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE cloud_target_id IS NOT NULL")

    def test_argv_has_no_secret_flag(self):
        """不提供 `--secret` 写法：它会留在 ps、/proc/<pid>/cmdline 和 root 的 shell 历史里。"""
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["backup", "add-target", "--name", "t1", "--endpoint",
                                    "https://h.example", "--bucket", "bkt", "--access-key", AK,
                                    "--secret", SK])

    def test_add_target_without_stdin_is_a_usage_error_and_writes_nothing(self):
        name = _uid("cli")
        args = self.parser.parse_args(["backup", "add-target", "--name", name, "--endpoint",
                                        "https://h.example", "--bucket", "bkt", "--access-key", AK])
        with mock.patch.object(Out, "json_mode", True):
            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(cmd_backup(args))
        self.assertEqual(ctx.exception.code, EXIT_USAGE)
        self.assertIsNone(dbm.query_one("SELECT id FROM cloud_targets WHERE name=?", (name,)))

    def test_add_target_reads_the_secret_from_stdin_and_eats_the_newline(self):
        name = _uid("cli")
        args = self.parser.parse_args(["backup", "add-target", "--name", name, "--endpoint",
                                        "https://h.example", "--bucket", "bkt", "--access-key", AK,
                                        "--secret-stdin"])
        with mock.patch.object(sys, "stdin", io.StringIO(SK + "\n")), \
             mock.patch.object(Out, "json_mode", True):
            out = asyncio.run(cmd_backup(args))
        self.assertTrue(out["ok"])
        self.assertEqual(dbm.query_one("SELECT secret_key FROM cloud_targets WHERE name=?",
                                       (name,))["secret_key"], SK)

    def test_add_target_with_empty_stdin_is_a_usage_error(self):
        args = self.parser.parse_args(["backup", "add-target", "--name", _uid("cli"), "--endpoint",
                                        "https://h.example", "--bucket", "bkt", "--access-key", AK,
                                        "--secret-stdin"])
        with mock.patch.object(sys, "stdin", io.StringIO("\n")), \
             mock.patch.object(Out, "json_mode", True):
            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(cmd_backup(args))
        self.assertEqual(ctx.exception.code, EXIT_USAGE)

    def test_targets_listing_shows_last_error_but_never_a_secret(self):
        tid = _mktarget()
        dbm.execute("UPDATE cloud_targets SET last_error='凭据被拒' WHERE id=?", (tid,))
        args = self.parser.parse_args(["backup", "targets"])
        with mock.patch.object(Out, "json_mode", True), \
             mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            data = asyncio.run(cmd_backup(args))
        self.assertIn("凭据被拒", _dumps(data))
        self.assertNotIn(SK, _dumps(data))
        self.assertNotIn(AK, _dumps(data))

    def test_del_target_needs_yes_and_keeps_remote_objects(self):
        tid = _mktarget()
        args = self.parser.parse_args(["backup", "del-target", "--id", str(tid)])
        with mock.patch.object(Out, "json_mode", True):
            with self.assertRaises(SystemExit) as ctx:
                asyncio.run(cmd_backup(args))
        self.assertEqual(ctx.exception.code, EXIT_USAGE)
        self.assertTrue(dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (tid,)),
                        "没确认就不许删")
        args = self.parser.parse_args(["backup", "del-target", "--id", str(tid), "--yes"])
        with mock.patch.object(Out, "json_mode", False), \
             mock.patch.object(sys, "stdout", new_callable=io.StringIO) as out:
            asyncio.run(cmd_backup(args))
        self.assertIsNone(dbm.query_one("SELECT id FROM cloud_targets WHERE id=?", (tid,)))
        self.assertIn("桶里的对象与本机备份都还在", out.getvalue(),
                      "删配置和删数据是两个决定，这句必须让人读得到")

    def test_link_and_unlink_round_trip(self):
        tid = _mktarget()
        bid = dbm.execute("INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled) "
                         "VALUES('app','cli-demo','manual',3,30,7,1)")
        dbm.execute("DELETE FROM backups WHERE target='cli-demo' AND id<>?", (bid,))
        args = self.parser.parse_args(["backup", "link", "--id", str(bid), "--target", str(tid)])
        with mock.patch.object(Out, "json_mode", True):
            asyncio.run(cmd_backup(args))
        self.assertEqual(dbm.query_one("SELECT cloud_target_id FROM backups WHERE id=?",
                                       (bid,))["cloud_target_id"], tid)
        args = self.parser.parse_args(["backup", "link", "--id", str(bid), "--target", "0"])
        with mock.patch.object(Out, "json_mode", True):
            asyncio.run(cmd_backup(args))
        self.assertIsNone(dbm.query_one("SELECT cloud_target_id FROM backups WHERE id=?",
                                       (bid,))["cloud_target_id"])
        dbm.execute("UPDATE backups SET cloud_target_id=? WHERE id=?", (tid, bid))
        args = self.parser.parse_args(["backup", "link", "--id", str(bid), "--target", "999999"])
        with mock.patch.object(Out, "json_mode", True):
            with self.assertRaises(SystemExit):
                asyncio.run(cmd_backup(args))
        dbm.execute("DELETE FROM backups WHERE id=?", (bid,))

    def test_schema_documents_the_stdin_only_rule(self):
        with mock.patch.object(Out, "json_mode", True), \
             mock.patch.object(sys, "stdout", new_callable=io.StringIO):
            text = _dumps(asyncio.run(cmd_schema(argparse.Namespace())))
        self.assertIn("--secret-stdin", text)
        self.assertNotIn('"--secret"', text)
        self.assertIn("del-target", text)


class RoleShape(unittest.TestCase):
    """只读账号看的是「有没有异地副本」，不是「副本落在哪个桶的哪台机器」。"""

    def tearDown(self):
        dbm.execute("DELETE FROM cloud_targets")
        dbm.execute("UPDATE backups SET cloud_target_id=NULL WHERE cloud_target_id IS NOT NULL")

    def test_service_row_keeps_the_full_shape(self):
        tid = _mktarget(name="role-demo")
        bid = dbm.execute("INSERT INTO backups(kind,target,schedule,hour,minute,keep,enabled,"
                         "cloud_target_id) VALUES('app','role-demo','manual',3,30,7,1,?)", (tid,))
        row = [r for r in B.list_backups() if r["id"] == bid][0]
        self.assertEqual(row["cloud_target_id"], tid)
        self.assertEqual(row["cloud_target_name"], "role-demo")

    def test_router_collapses_it_for_non_admin(self):
        src = (Path(config.BASE) / "backend/app/routers/backups.py").read_text(encoding="utf-8")
        self.assertIn('r["cloud_linked"] = bool(r.pop("cloud_target_id", None))', src,
                      "只读账号要能看出有没有异地副本，但不该知道副本落在哪个桶的哪台机器")
        self.assertIn('r.pop("cloud_target_name", None)', src)
        self.assertIn('r.pop("dir", None)', src)
        self.assertIn('if req.state.cp_role != "admin"', src)
        targets = src.split('@router.get("/api/backups/targets")')[1][:300]
        self.assertIn("need_admin", targets)
        self.assertIn('and not str(tid).isdigit()', src,
                      "把 abc 一路传进 int() 只会变成一个看不出所以然的 500")


class CrossLayerTimeout(unittest.TestCase):
    """后端超时与前端超时是同一个等待的两端：前端必须更长，否则用户看到的是「卡住」。"""

    def test_api_timeouts_exceed_backend(self):
        api = (Path(config.BASE) / "web/src/api.ts").read_text(encoding="utf-8")
        probe = int(re.search(r"testCloudTarget:.*?timeout:\s*(\d+)", api, re.S).group(1))
        push = int(re.search(r"pushBackup:.*?timeout:\s*(\d+)", api, re.S).group(1))
        self.assertGreater(probe, config.CLOUD_PROBE_TIMEOUT_SEC * 1000)
        self.assertGreater(push, config.CLOUD_PUSH_TIMEOUT_SEC * 1000)

    def test_transport_limits(self):
        self.assertEqual(C.MAX_SINGLE_PUT, 5 * 1024**3)
        self.assertLessEqual(config.CLOUD_PUSH_TIMEOUT_SEC, 7200)
        self.assertLess(C._check_endpoint("https://h.example")["port"], 65536)
        self.assertEqual(C.LIST_PAGES, 20)

    def test_put_object_guards_size_then_verifies_the_object(self):
        d = tempfile.mkdtemp(prefix="cp_b43_put_")
        p = os.path.join(d, "big.gz")
        with open(p, "wb") as f:
            f.write(b"z" * 7)
        t = {"endpoint": "https://h.example", "bucket": "b", "region": "r", "prefix": "",
             "path_style": True, "access_key": AK, "secret_key": SK, "name": "put"}
        try:
            with mock.patch.object(C, "MAX_SINGLE_PUT", 3), \
                 mock.patch.object(C, "_request") as rq:
                with self.assertRaises(RuntimeError) as ctx:
                    C.put_object(t, "k", p)
                self.assertIn("单文件上传上限", str(ctx.exception))
                rq.assert_not_called()
            with mock.patch.object(C, "_request", return_value=(200, {"x-amz-request-id": "R"}, b"")), \
                 mock.patch.object(C, "head_object", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    C.put_object(t, "k", p)
                self.assertIn("HEAD 读不到", str(ctx.exception))
            with mock.patch.object(C, "_request", return_value=(200, {"x-amz-request-id": "R"}, b"")), \
                 mock.patch.object(C, "head_object", return_value={"key": "k", "size": 6}):
                with self.assertRaises(RuntimeError) as ctx:
                    C.put_object(t, "k", p)
                self.assertIn("不一致", str(ctx.exception))
            with mock.patch.object(C, "_request", return_value=(200, {"x-amz-request-id": "R"}, b"")), \
                 mock.patch.object(C, "head_object", return_value={"key": "k", "size": 7}):
                out = C.put_object(t, "k", p)
            self.assertEqual(out["bytes"], 7)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_deadline_reader_caps_a_slow_upload(self):
        """http.client 的 timeout 只管单次 socket 操作；整场不封顶就能挂到天亮并攥着 flock。"""
        d = tempfile.mkdtemp(prefix="cp_b43_dl_")
        p = os.path.join(d, "a.gz")
        with open(p, "wb") as f:
            f.write(b"q" * 100)
        t = {"endpoint": "https://h.example", "bucket": "b", "region": "r", "prefix": "",
             "path_style": True, "access_key": AK, "secret_key": SK, "name": "dl"}
        captured = {}

        class _Boom(RuntimeError, TimeoutError):
            """真实场景就是这副样子：对端没响应，最后由读时限这一层报超时。"""

        def spy(t, method, key="", query="", body=None, body_len=0,
                payload_hash=C.EMPTY_SHA256, timeout=None, extra=None):
            captured["body"] = body
            raise _Boom("对端没有回应")

        with mock.patch.object(C, "_request", spy), \
             mock.patch.object(C, "head_object", return_value={"key": "", "size": 100}), \
             mock.patch.object(time, "monotonic", lambda: 1000.0):
            with self.assertRaises(_Boom):
                C.put_object(t, "k", p, timeout=5)
            # put_object 交出去的就是带时限的读取壳（不是裸文件句柄）：整场上传才有上界
            self.assertIsInstance(captured["body"], C._DeadlineReader)
            # 时限没到就还得正常读字节。句柄已经被 put_object 的 with 关掉，所以这里
            # 用同一把假钟另开一份：读的是这一次 PutObject 的账，不是别处的。
            with open(p, "rb") as fh:
                self.assertEqual(C._DeadlineReader(fh, 1005.0, 5).read(4), b"qqqq")
        with open(p, "rb") as fh:
            with self.assertRaises(TimeoutError) as ctx:
                C._DeadlineReader(fh, time.monotonic() - 1, 5).read(4)
        self.assertIn("时限", str(ctx.exception))
        shutil.rmtree(d, ignore_errors=True)
